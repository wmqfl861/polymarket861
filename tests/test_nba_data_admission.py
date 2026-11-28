"""Exercise source admission using only HTTP-boundary fixtures and real SQLite."""

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from polymarket861.nba import (
    data,
    data_capabilities,
    source_acceptance,
    source_probes,
    storage,
)
from test_nba_data import TEST_KEY, game, page


def source_game(gid, day, *, hour=23, **fields):
    return game(
        gid,
        **{
            "season": day.year - int(day.month < 7),
            "date": str(day),
            "datetime": f"{day}T{hour:02}:00:00Z",
            **fields,
        },
    )


class SourceAdmission(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.tmp / "state"
        self.now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    k: TEST_KEY
                    for k in (
                        "BALLDONTLIE_API_KEY",
                        "BIGBALLS_API_KEY",
                        "DUNKAPI_API_KEY",
                    )
                },
            )
        )

    def clock(self):
        return self.now

    def advance(self, secs):
        self.now += timedelta(seconds=secs)

    def sync(self, day, responses, end=None):
        def get(params, key):
            self.assertEqual(key, TEST_KEY)
            phase = params["season_type"]
            rows = responses(phase) if callable(responses) else responses.get(phase, [])
            return rows if isinstance(rows, httpx.Response) else page(rows)

        return data.sync_bdl(
            self.root, day, end or day, fetch=get, clock=self.clock, sleep=self.advance
        )

    def test_ignored_filters_cannot_publish_a_transient_or_trainable_regular_version(
        self,
    ):
        day = date(2026, 10, 14)
        row = source_game(
            42,
            day,
            status_state="final",
            status="Final",
            period=4,
            home_team_score=113,
            visitor_team_score=110,
        )

        def ignores(phase):
            self.assertFalse(
                any(r["stage"] == "regular" for r in data.game_versions(self.root))
            )
            return [row]

        r = self.sync(day, ignores)
        self.assertEqual(
            (r["state"], r["complete"], r["quality"]["overlapping_ids"]),
            ("source_quality", False, 1),
        )
        self.assertEqual(r["new_versions"], 1)
        self.assertEqual(
            data.game_versions(self.root)[0]["quarantine"],
            "season_type_filter_conflict",
        )
        self.assertEqual(self.sync(day, lambda p: [row])["new_versions"], 0)
        before = self.now
        recovered = self.sync(day, {"preseason": [row]})
        self.assertTrue(recovered["complete"])
        self.assertEqual(data.games_as_of(self.root)[0]["stage"], "preseason")
        self.assertEqual(data.games_as_of(self.root, before)[0]["stage"], "unknown")

    def test_partial_phase_failures_do_not_admit_guesses_and_protect_corrections(self):
        day = self.now.date()
        regular = source_game(1, day)
        first = self.sync(day, {"regular": [regular], "preseason": httpx.Response(503)})
        self.assertFalse(first["complete"])
        self.assertFalse(data.game_versions(self.root))
        self.assertTrue(self.sync(day, {"regular": [regular]})["complete"])
        failed = self.sync(
            day,
            {"regular": [dict(regular, postponed=True)], "ist": httpx.Response(503)},
        )
        self.assertFalse(failed["complete"])
        self.assertEqual(data.games_as_of(self.root)[0]["status"], "postponed")
        self.assertEqual(data.games_as_of(self.root)[0]["stage"], "unknown")

    def test_source_quality_clocks_and_stage_are_diagnostics(self):
        today = self.now.date()
        for row, issue in [
            (source_game(1, today, datetime=None), "scheduled_without_kickoff"),
            (source_game(1, today, season_type="exhibition"), "unknown_stages"),
            (
                source_game(1, today, status="invalid", status_state="invalid"),
                "unrecognized_statuses",
            ),
            (source_game(1, today, season_type="preseason"), "phase_mismatches"),
        ]:
            with self.subTest(issue=issue):
                result = self.sync(today, {"regular": [row]})
                self.assertFalse(result["complete"])
                self.assertGreater(result["quality"][issue], 0)
        historical = source_game(
            2,
            today - timedelta(days=1),
            datetime=None,
            status="Final",
            status_state="final",
            home_team_score=105,
            visitor_team_score=100,
        )
        r = self.sync(today - timedelta(days=1), {"regular": [historical]})
        self.assertTrue(r["complete"])
        accepted = data.game_versions(self.root, game_id="bdl:2")[0]
        self.assertEqual(
            (accepted["status"], accepted["stage"], accepted["kickoff"]),
            ("final", "regular", None),
        )

    def test_legacy_inferred_stage_waits_for_a_new_verified_scan(self):
        today = self.now.date()
        raw = source_game(5, today)
        old = data.normalize_bdl(raw, "regular")
        old.pop("stage_basis")
        with closing(data._open(self.root)) as db:
            data._capture(
                db,
                {},
                json.dumps(raw).encode(),
                [old],
                200,
                "game_page",
                self.now,
                self.clock,
            )
        self.assertEqual(
            data.games_as_of(self.root)[0]["stage_basis"], "legacy_requires_rescan"
        )
        self.advance(60)
        self.assertTrue(self.sync(today, {"regular": [raw]})["complete"])
        self.assertEqual(
            data.games_as_of(self.root)[0]["stage_basis"], "disjoint_phase_queries"
        )

    def test_missing_kickoff_future_et_final_cannot_train_and_can_be_reobserved(self):
        # UTC is already tomorrow while the deployment is still on the previous ET day.
        self.now = datetime(2026, 11, 2, 2, tzinfo=timezone.utc)
        day = date(2026, 11, 2)
        raw = source_game(
            42,
            day,
            datetime=None,
            status="Final",
            status_state="final",
            period=5,
            home_team_score=113,
            visitor_team_score=110,
        )
        row = data.normalize_bdl(raw, "regular")
        row["stage_basis"] = "disjoint_phase_queries"
        received = data._iso(self.now)
        with closing(data._open(self.root)) as db, db:
            db.execute(
                "INSERT INTO nba_game_versions VALUES(?,?,?,?,?,?,?)",
                (
                    "balldontlie",
                    "bdl:42",
                    1,
                    received,
                    received,
                    "legacy",
                    data._encoded(row),
                ),
            )
        old = data.games_as_of(self.root)[0]
        self.assertEqual(
            (old["status"], old["home_score"], old["final_period"]),
            ("unknown", None, None),
        )
        self.assertEqual(
            old["timing_problem"], "play_received_before_eastern_game_date"
        )
        self.now += timedelta(days=2)
        self.assertTrue(self.sync(day, {"regular": [raw]})["complete"])
        repaired = data.games_as_of(self.root)[0]
        self.assertEqual(
            (
                repaired["version"],
                repaired["status"],
                repaired["home_score"],
                repaired["final_period"],
            ),
            (2, "final", 113, 5),
        )
        self.assertEqual(data.games_as_of(self.root, received)[0]["status"], "unknown")

    def test_auth_three_ranges_and_exception_witnesses(self):
        self.now = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
        today = self.now.date()
        upcoming = source_game(101, today)
        # Previously received tip, so a later change can be authenticated with two receipts.
        self.sync(today, {"regular": [dict(upcoming, datetime=f"{today}T22:00:00Z")]})
        self.advance(120)
        recent = [
            upcoming,
            source_game(102, today - timedelta(days=1), postponed=True),
            source_game(103, today - timedelta(days=2)),
        ]

        def get(p, key):
            phase = p["season_type"]
            if p["start_date"] == "2026-10-01":
                rows = {
                    "regular": [source_game(201, date(2026, 10, 25))],
                    "preseason": [source_game(202, date(2026, 10, 10))],
                }
            elif p["start_date"] == "2025-12-01":
                rows = {
                    "regular": [source_game(301, date(2025, 12, 4))],
                    "ist": [
                        source_game(302, date(2025, 12, 18), ist_stage="Championship")
                    ],
                }
            else:
                rows = {"regular": recent}
            return page(rows.get(phase, []))

        ok = source_acceptance.verify_schedule(
            self.root, fetch=get, clock=self.clock, sleep=self.advance
        )
        self.assertTrue(ok["verified"], ok)
        self.assertEqual(len(ok["ranges"]), 3)
        for area in ok["ranges"]:
            self.assertEqual(
                {p["phase"] for p in area["receipts"]}, {"regular", "preseason", "ist"}
            )
            self.assertTrue(all(len(p["sha256"]) == 64 for p in area["receipts"]))
        changes = ok["checks"]["reschedule_observed"]["examples"]
        self.assertTrue(
            changes and changes[0]["before_sha256"] != changes[0]["after_sha256"]
        )
        change = changes[0]
        self.assertEqual((change["home"], change["away"]), ("bdl:1", "bdl:2"))
        self.assertLess(change["before_available_at"], change["previous_kickoff"])
        self.assertLess(change["after_available_at"], change["kickoff"])
        self.assertLessEqual(
            change["before_received_at"], change["before_available_at"]
        )
        self.assertLessEqual(change["after_received_at"], change["after_available_at"])
        with patch.object(data, "_now", self.clock):
            state = source_acceptance.admission_summary(self.root, today)
            self.assertTrue(state["schedule_verified"])
            self.assertFalse(
                source_acceptance.admission_summary(
                    self.root, today - timedelta(days=1)
                )["schedule_verified"]
            )
        # The report follows the newest evidence; an empty authenticated provider fails.
        empty = source_acceptance.verify_schedule(
            self.root, fetch=lambda *a: page([]), clock=self.clock, sleep=self.advance
        )
        self.assertFalse(empty["verified"])
        self.assertEqual(
            empty["checks"]["postponement_observed"]["state"], "not_observed"
        )
        self.assertFalse(empty["ranges"][0]["checks"]["three_recent_game_days"]["days"])

    def test_reschedule_requires_independent_pregame_publication_and_same_teams(self):
        day = date(2026, 11, 3)
        for scenario, observed in (
            ("both_after_tip", "2026-11-07T14:00Z"),
            ("first_commit_after_tip", "2026-11-03T20:59:55Z"),
            ("new_commit_after_tip", "2026-11-03T19:00Z"),
            ("different_teams", "2026-11-03T19:00Z"),
        ):
            with self.subTest(scenario=scenario):
                self.root = self.tmp / scenario
                self.now = data._utc(observed)
                raw = source_game(501, day, hour=21)
                self.sync(day, {"regular": [raw]})
                self.advance(120)
                new = dict(raw, datetime=f"{day}T23:00:00Z")
                if scenario == "different_teams":
                    new["home_team"], new["visitor_team"] = (
                        new["visitor_team"],
                        new["home_team"],
                    )
                if scenario == "new_commit_after_tip":
                    self.now = data._utc(f"{day}T22:59:55Z")
                self.sync(day, {"regular": [new]})
                self.assertEqual(
                    source_acceptance._changes(self.root, {"bdl:501"}, self.now), []
                )

    def test_missing_credentials_do_not_create_receipts(self):
        with patch.dict(os.environ, {}, clear=True):
            v = source_acceptance.verify_schedule(
                self.root,
                clock=self.clock,
                fetch=lambda *_: self.fail("unrequested HTTP"),
            )
            self.assertEqual(v["state"], "missing_key")
            self.assertEqual(len(v["ranges"]), 3)
            self.assertTrue(all(s["receipts"] == [] for s in v["ranges"]))
            self.assertFalse(
                source_acceptance.admission_summary(self.root)[
                    "eligible_player_sources"
                ]
            )
            self.assertFalse(self.root.exists())

    def terms(self, source, field=None):
        field = field or ("data.date" if source == "bigballs" else "date")
        terms = self.tmp / "terms.txt"
        proof = self.tmp / "origin.txt"
        terms.write_text("archived local internal prediction and retention permission")
        proof.write_text(
            "responsible examiner identified example-source and day-field meaning"
        )
        return source_probes.record_terms(
            self.root,
            source,
            "operator",
            terms,
            "example-source",
            proof,
            "allowed",
            "allowed",
            field,
            proof,
            clock=self.clock,
        )

    def run_day(self, day, fetch, *, skip=()):
        self.now = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(
            hours=14
        )
        gameid = day.toordinal()
        rows = [source_game(gameid, day), source_game(gameid + 1000, day, hour=22)]
        self.sync(day, {"regular": rows})
        for mins in (90, 60, 15):
            if mins in skip:
                continue
            self.now = datetime.combine(
                day, datetime.min.time(), timezone.utc
            ) + timedelta(hours=22, minutes=-mins - 2)
            result = source_probes.probe_due(self.root, clock=self.clock, fetch=fetch)
            self.assertEqual(result["first_game"]["game_id"], f"bdl:{gameid + 1000}")
            # Timer retries or two service instances cannot spend the same slot twice.
            duplicate = source_probes.probe_due(
                self.root, clock=self.clock, fetch=fetch
            )
            self.assertIn(
                duplicate["sources"]["bigballs"]["state"],
                ("already_attempted", "outside_window"),
            )
        self.now = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(
            hours=23
        )

    def provider(self, source, key, *, day=None, clocks=False):
        entry = {"status": "Out", "player_id": 7, "note": "private narrative"}
        if clocks:
            entry["published_at"] = data._iso(self.now - timedelta(minutes=5))
        date_field = str(day or self.now.astimezone(data.EASTERN).date())
        body = {"date": date_field, "injuries": [entry]}
        return httpx.Response(
            200, json={"data": body} if source == "bigballs" else body
        )

    def test_seven_days_per_source_all_windows_and_named_terms(self):
        start = date(2026, 11, 1)
        decision = self.terms("bigballs")
        self.assertEqual(
            decision["evidence_sha256"]["terms"],
            hashlib.sha256((self.tmp / "terms.txt").read_bytes()).hexdigest(),
        )
        calls = {"bigballs": 0, "dunkapi": 0}

        def fetch(source, key):
            calls[source] += 1
            return self.provider(source, key)

        for i in range(7):
            self.run_day(start + timedelta(days=i), fetch)
        self.assertEqual(calls, {"bigballs": 21, "dunkapi": 7})
        caps = source_probes.player_summary(self.root, self.now)
        big, dunk = caps["bigballs"], caps["dunkapi"]
        self.assertEqual(
            (big["game_days"], big["t60_fresh_days"], big["source_verified"]),
            (7, 7, True),
        )
        self.assertEqual(len(big["days"][-1]["samples"]), 3)
        self.assertEqual(len({r["probe_id"] for r in big["days"][-1]["samples"]}), 3)
        self.assertEqual(
            (
                dunk["t60_fresh_days"],
                dunk["seven_day_same_day_status"],
                dunk["source_verified"],
            ),
            (7, False, False),
        )
        self.assertEqual(len(dunk["days"][-1]["samples"]), 1)
        cutoff = self.now
        self.run_day(start + timedelta(days=7), fetch, skip=(60,))
        after = source_probes.player_summary(self.root, self.now)
        self.assertFalse(after["bigballs"]["seven_day_coverage"])
        self.assertFalse(after["dunkapi"]["days"][-1]["t60_fresh"])
        self.assertTrue(
            source_probes.player_summary(self.root, cutoff)["bigballs"][
                "source_verified"
            ]
        )
        with closing(storage.open_existing(self.root)) as db:
            dump = "\n".join(db.iterdump())
        self.assertNotIn(TEST_KEY, dump)
        self.assertNotIn("private narrative", dump)

    def test_old_upstream_snapshot_empty_or_late_receipts_never_verify_today(self):
        self.terms("bigballs")
        self.terms("dunkapi")
        day = date(2026, 11, 1)

        def fetch(source, key):
            if source == "bigballs":
                return self.provider(source, key, day=day - timedelta(days=1))
            self.advance(180)
            return self.provider(source, key)

        self.run_day(day, fetch)
        caps = source_probes.player_summary(self.root, self.now)
        self.assertTrue(caps["bigballs"]["days"][0]["t60_fresh"])
        self.assertFalse(caps["bigballs"]["days"][0]["same_day_status"])
        self.assertFalse(caps["dunkapi"]["days"][0]["t60_fresh"])

    def test_exact_approved_native_field_and_explicit_nba_scope(self):
        day = date(2026, 11, 1)
        self.terms("bigballs", "snapshot_date")
        self.terms("dunkapi", "date")

        def wrong(source, key):
            if source == "bigballs":
                return self.provider(
                    source, key
                )  # Only $.data.date, no $.data.snapshot_date.
            return httpx.Response(
                200,
                json={
                    "date": str(day),
                    "league": "nba",
                    "injuries": [{"league": "nfl", "player_id": 42, "status": "Out"}],
                },
            )

        self.run_day(day, wrong)
        report = source_probes.player_summary(self.root, self.now)
        b = report["bigballs"]["days"][0]
        self.assertTrue(b["t60_fresh"])
        self.assertFalse(b["same_day_status"])
        self.assertEqual(b["samples"][0]["source_dates"], {"data.date": str(day)})
        d = report["dunkapi"]["days"][0]
        self.assertFalse(d["t60_fresh"])
        self.assertEqual(d["samples"][0]["state"], "scope_mismatch")

    def test_calendar_keeps_native_only_and_missing_days_and_proves_a_rest_day(self):
        from polymarket861.nba import markets

        begin = date(2026, 11, 1)
        self.run_day(begin, self.provider)
        # Second date has a native event but the schedule collector never ran.
        native_day = begin + timedelta(days=1)
        private_tip = data._iso(f"{native_day}T23:00:00Z")
        seen = data._iso(self.now)
        with closing(storage.connect(self.root)) as db, db:
            db.executescript(markets.SCHEMA)
            db.execute(
                "INSERT INTO nba_market_games VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "event",
                    "game",
                    "BOS",
                    "ATL",
                    "1",
                    "2",
                    private_tip,
                    1,
                    seen,
                    seen,
                    False,
                    None,
                ),
            )
            db.execute(
                "INSERT INTO nba_market_schedule VALUES(?,?,?,?)",
                ("event", 1, private_tip, seen),
            )
        # Third date is completely unobserved. Fourth is a verified NBA rest day.
        rest = begin + timedelta(days=3)
        self.now = (
            datetime.combine(rest, datetime.min.time(), data.EASTERN)
            + timedelta(minutes=30)
        ).astimezone(timezone.utc)
        self.sync(rest, {})

        def empty_catalogue(url, params, *, timeout):
            ts = data._iso(self.now)
            return markets.api.Reply(
                b'{"events":[]}',
                ts,
                ts,
                {"date": format_datetime(self.now, usegmt=True), "age": "0"},
                url,
            )

        with patch.object(markets.api, "get", empty_catalogue):
            self.assertEqual(markets.collect(self.root, now=self.clock)["state"], "ok")
        self.run_day(begin + timedelta(days=4), self.provider)
        report = source_probes.player_summary(self.root, self.now)["bigballs"]
        states = {d["game_day"]: d["calendar_status"] for d in report["days"]}
        self.assertEqual(
            states[str(native_day)], "native_listing_without_verified_first_tip"
        )
        self.assertEqual(
            report["unverified_calendar_dates"], [str(begin + timedelta(days=2))]
        )
        self.assertEqual(report["verified_rest_days"], [str(rest)])
        self.assertFalse(report["seven_day_coverage"])

    def test_partial_native_date_coverage_and_early_play_cannot_admit(self):
        from polymarket861.nba import markets

        begin = date(2026, 11, 1)
        self.run_day(begin, self.provider)
        clock = self.now
        source_tip = data._iso(f"{begin}T22:00:00Z")
        seen = data._iso(f"{begin}T14:00:00Z")
        with closing(storage.connect(self.root)) as db, db:
            db.executescript(markets.SCHEMA)
            db.execute(
                "INSERT INTO nba_market_games VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "event",
                    "game",
                    "ATL",
                    "BOS",
                    "1",
                    "2",
                    source_tip,
                    1,
                    seen,
                    seen,
                    False,
                    None,
                ),
            )
            db.execute(
                "INSERT INTO nba_market_schedule VALUES(?,?,?,?)",
                ("event", 1, source_tip, seen),
            )
            db.execute(
                "INSERT INTO nba_market_play VALUES(?,?,?)",
                ("event", data._iso(f"{begin}T20:45:00Z"), "observed_play"),
            )
        report = source_probes.player_summary(self.root, clock)["bigballs"]["days"][0]
        by_slot = {r["slot"]: r["window_usable"] for r in report["samples"]}
        self.assertEqual(by_slot, {"T-90": True, "T-60": False, "T-15": False})
        # Same teams but an independent tip four hours earlier also blocks T-60.
        with closing(storage.connect(self.root)) as db, db:
            db.execute("DELETE FROM nba_market_play")
            db.execute(
                "UPDATE nba_market_schedule SET kickoff=?",
                (data._iso(f"{begin}T18:00:00Z"),),
            )
        report = source_probes.player_summary(self.root, clock)["bigballs"]["days"][0]
        self.assertEqual(report["calendar_status"], "native_kickoff_conflict")
        self.assertFalse(report["t60_fresh"])
        # A late-night catalogue's lower bound misses the rest of the ET date.
        rest = begin + timedelta(days=1)
        self.now = (
            datetime.combine(rest, datetime.min.time(), data.EASTERN)
            + timedelta(hours=22, minutes=45)
        ).astimezone(timezone.utc)
        self.sync(rest, {})

        def late_partial(url, params, *, timeout):
            ts = data._iso(self.now)
            return markets.api.Reply(
                b'{"events":[]}',
                ts,
                ts,
                {"date": format_datetime(self.now, usegmt=True), "age": "0"},
                url,
            )

        with patch.object(markets.api, "get", late_partial):
            self.assertEqual(markets.collect(self.root, now=self.clock)["state"], "ok")
        self.now += timedelta(days=1)
        report = source_probes.player_summary(self.root, self.now)["bigballs"]
        self.assertNotIn(str(rest), report["verified_rest_days"])
        self.assertIn(str(rest), report["unverified_calendar_dates"])

    def test_quota_and_post_commit_publication_survive_restart(self):
        stamp = self.now
        responses = iter(
            [httpx.Response(429, headers={"Retry-After": "7200"}), httpx.Response(402)]
        )
        limited = data_capabilities.probe(
            self.root, "bigballs", fetch=lambda *a: next(responses), clock=self.clock
        )
        self.assertEqual(limited["retry_at"], data._iso(stamp + timedelta(hours=2)))
        self.advance(3600)
        self.assertEqual(
            data_capabilities.probe(
                self.root, "bigballs", fetch=lambda *_: self.fail(), clock=self.clock
            )["state"],
            "budget_wait",
        )
        self.advance(3601)
        self.assertEqual(
            data_capabilities.probe(
                self.root,
                "bigballs",
                fetch=lambda *a: next(responses),
                clock=self.clock,
            )["state"],
            "quota_exhausted",
        )
        original = data_capabilities._witness
        attempts = [0]

        def die_on_response(db, clock):
            attempts[0] += 1
            if attempts[0] == 2:
                raise OSError("interrupted after durable receipt")
            original(db, clock)

        with (
            patch.object(data_capabilities, "_witness", die_on_response),
            self.assertRaises(OSError),
        ):
            data_capabilities.probe(
                self.root, "dunkapi", fetch=self.provider, clock=self.clock
            )
        self.assertEqual(
            data.capabilities(self.root)["sources"]["dunkapi"][
                "authenticated_business_receipts"
            ],
            0,
        )
        self.advance(120)
        self.assertEqual(
            data_capabilities.probe(
                self.root, "dunkapi", fetch=lambda *_: self.fail(), clock=self.clock
            )["state"],
            "budget_wait",
        )
        self.assertEqual(
            data.capabilities(self.root)["sources"]["dunkapi"]["latest_receipt"][
                "ingested_at"
            ],
            data._iso(self.now),
        )
