import json
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from test_nba_markets import Venue

from polymarket861.nba import data, markets as m
from polymarket861.nba.markets_contracts import stamp
from scripts.verify_nba_market_lines import DEFAULT_FIXTURE, replay


class MarketReviewChecks(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "nba"
        self.venue = Venue()
        self.get = patch.object(m.api, "get", self.venue.get)
        self.get.start()
        self.addCleanup(self.get.stop)

    def tick(self):
        return m.collect(self.root, now=self.venue.clock)

    def compare(self, tip, at=None):
        return m.schedule_match(
            self.root, "NYK", "MIA", tip, as_of=at or self.venue.now
        )

    def book(self, tip, at=None):
        return m.benchmark(
            self.root, "NYK", "MIA", tip, "T-60m", as_of=at or self.venue.now
        )

    def score_version(
        self, tip, seen, game_id="bdl:704", version=1, status="scheduled"
    ):
        values = {
            "source": "balldontlie",
            "game_id": game_id,
            "stage": "regular",
            "stage_basis": "disjoint_phase_queries",
            "game_date": tip.astimezone(m.EAST).date().isoformat(),
            "evidence_grade": "local_receipt",
            "kickoff": stamp(tip),
            "home_abbr": "NY",
            "away_abbr": "MIA",
            "status": status,
        }
        with closing(m.storage.connect(self.root)) as db:
            db.executescript(data.SCHEMA)
            with db:
                db.execute(
                    "INSERT INTO nba_game_versions VALUES(?,?,?,?,?,?,?)",
                    (
                        "balldontlie",
                        game_id,
                        version,
                        stamp(seen),
                        stamp(seen),
                        "regression",
                        json.dumps(values),
                    ),
                )

    def test_actual_public_receipts_replay_full_lines(self):
        expected = json.loads(
            (
                DEFAULT_FIXTURE.parents[3]
                / "reports/nba-phase1/market-lines-20261128.json"
            ).read_text()
        )
        self.assertEqual(replay(), expected)
        self.assertEqual(expected["counts"]["events"], 11)
        self.assertEqual(
            expected["counts"]["selected_families"]
            + expected["counts"]["no_selection"],
            11 * 4,
        )
        self.assertGreater(expected["counts"]["alternate_selections"], 0)

    def test_two_native_cutoffs_and_signed_mismatch(self):
        v = self.venue
        self.tick()
        for minutes in (-30, -10, -2, 0, 2, 10, 30, 31, 180):
            tip = v.tip + timedelta(minutes=minutes)
            with self.subTest(minutes=minutes):
                r = self.compare(tip)
                self.assertEqual(r["delta_seconds"], -minutes * 60)
                self.assertEqual(
                    r["status"],
                    "exact"
                    if minutes == 0
                    else (
                        "within_tolerance"
                        if abs(minutes) <= 30
                        else "outside_tolerance"
                    ),
                )
                self.assertIsNone(
                    m.fixture_conflict(self.root, "NY", "MIA", tip, as_of=v.now)
                )
                # Observation and publication must fit both providers' named windows.
                paired = self.book(tip)
                self.assertEqual(bool(paired), abs(minutes) <= 2)
                if paired:
                    self.assertEqual(paired["win"]["venue_event_id"], v.event["id"])
                    self.assertEqual(paired["win"]["source_kickoff"], stamp(tip))
        tip = v.tip + timedelta(minutes=10)
        self.assertEqual(
            m.fixture_conflict(self.root, "NY", "MIA", tip, as_of=v.tip),
            "venue_scheduled_tip_reached",
        )

    def test_duplicate_on_either_schedule_is_ambiguous(self):
        v = self.venue
        self.tick()
        self.score_version(v.tip, v.now)
        self.score_version(v.tip + timedelta(minutes=20), v.now, game_id="bdl:705")
        self.assertEqual(
            self.compare(v.tip)["reason"], "ambiguous_score_source_fixture"
        )
        self.assertFalse(self.book(v.tip))
        self.root = Path(self.root.parent) / "venue-duplicate"
        self.tick()
        with closing(m.storage.connect(self.root)) as db, db:
            other = dict(
                deepcopy(v.event),
                id="49",
                gameId="124",
                startTime=stamp(v.tip + timedelta(hours=3)),
            )
            m._upsert(db, other, stamp(v.now))
        self.assertEqual(self.compare(v.tip)["reason"], "ambiguous_venue_fixture")
        self.assertFalse(self.book(v.tip))

    def test_score_early_tip_veto_even_when_provider_times_differ(self):
        v = self.venue
        self.tick()
        score_tip = v.tip - timedelta(minutes=10)
        self.score_version(score_tip, v.now)
        v.now = v.tip - timedelta(minutes=2)
        self.assertIn("score_source_scheduled_tip_reached", str(self.tick()["errors"]))
        with closing(m.storage.open_existing(self.root)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM nba_market_close").fetchone()[0], 0
            )

    def test_listed_rule_change_persists_separately_from_absent_and_inactive(self):
        v = self.venue
        # No away market, withdrawn home market, one damaged total and one usable equivalent family.
        v.event["markets"].pop()
        v.event["markets"][3]["closed"] = True
        v.event["markets"][2]["description"] += " Including exhibition quarters."
        self.assertEqual(self.tick()["state"], "degraded")
        v.now += timedelta(minutes=1)
        self.assertEqual(self.tick()["state"], "ok")
        stat = m.status(self.root, now=v.clock)
        self.assertEqual(stat["state"], "degraded")
        self.assertEqual(
            [(a["family"], a["reason"], a["count"]) for a in stat["contract_alerts"]],
            [("total", "rule_template_mismatch", 1)],
        )
        self.assertEqual(set(self.book(v.tip)), {"win", "total"})
        with closing(m.storage.open_existing(self.root)) as db:
            quality = dict(db.execute("SELECT family,quality FROM nba_market_choices"))
        self.assertEqual(quality["home_total"], "listed_inactive")
        self.assertEqual(quality["away_total"], "unlisted")
        v.event["markets"][2]["description"] = v.event["markets"][2][
            "description"
        ].removesuffix(" Including exhibition quarters.")
        v.now = v.tip - timedelta(minutes=17)
        self.tick()
        self.assertEqual(m.status(self.root, now=v.clock)["contract_alerts"], [])

    def test_spreads_are_raw_only_and_do_not_issue_another_request(self):
        v = self.venue
        market = dict(
            v.event["markets"][0],
            id="8",
            conditionId=f"0x{8:064x}",
            clobTokenIds='["80","81"]',
            slug=v.slug + "-spread-home-4pt5",
            sportsMarketType="spreads",
            marketMetadata={"line_kind": "consensus", "line_family": "full.spread"},
        )
        v.event["markets"].append(market)
        self.assertEqual(self.tick()["state"], "ok")
        batches = [
            params["token_ids"] for url, params in v.calls if url.endswith("/books")
        ]
        self.assertEqual(len(batches), 1)
        self.assertTrue({"80", "81"} <= set(batches[0]))
        self.assertEqual(set(self.book(v.tip)), set(m.FAMILIES))
        with closing(m.storage.open_existing(self.root)) as db:
            r = db.execute(
                "SELECT a.condition_id,a.window,b.sha,e.sha FROM nba_market_spread_archives a "
                "JOIN nba_market_requests b ON b.id=a.book_receipt JOIN nba_market_requests e ON e.id=a.metadata_receipt"
            ).fetchone()
            self.assertEqual(tuple(r[:2]), (market["conditionId"], "T-60m"))
            self.assertTrue(all(len(s) == 64 for s in r[2:]))
            self.assertFalse(
                db.execute(
                    "SELECT 1 FROM nba_market_choices WHERE contract LIKE ?",
                    ("%" + market["conditionId"] + "%",),
                ).fetchone()
            )

    def test_diff_study_requires_completed_days_and_pregame_versions(self):
        v = self.venue
        self.tick()
        self.assertEqual(
            m.kickoff_differences(self.root, as_of=v.now)["counts"],
            {"missing_pregame_source": 1},
        )
        self.score_version(v.tip + timedelta(minutes=10), v.now)
        study = m.kickoff_differences(self.root, as_of=v.now)
        self.assertEqual(study["counts"], {"within_tolerance": 1})
        self.assertEqual(study["days"][0]["fixtures"][0]["delta_seconds"], -600)
        self.assertEqual(study["paired_days"], 0)
        # A late source receipt cannot retroactively turn a venue-only day into a validated day.
        self.root = Path(self.root.parent) / "late-final"
        self.tick()
        late = v.tip + timedelta(hours=3)
        self.score_version(v.tip, late, status="final")
        self.assertEqual(
            m.kickoff_differences(self.root, as_of=late)["counts"],
            {"missing_pregame_source": 1},
        )
        self.assertFalse(m.kickoff_differences(self.root, as_of=late)["enough_days"])

    def test_three_complete_regular_dates_need_both_original_scan_proofs(self):
        import os
        import httpx

        v = self.venue
        base = v.tip
        for i in range(4):
            tip = base + timedelta(days=i)
            v.now = tip - timedelta(days=1)
            day = tip.astimezone(m.EAST).date().isoformat()
            v.event.update(
                id=str(9000 + i),
                gameId=str(3000 + i),
                startTime=stamp(tip),
                slug="nba-mia-nyk-" + day,
            )
            with closing(m.storage.connect(self.root)) as db:
                db.executescript(m.SCHEMA)
                self.assertEqual(m._discover(db, v.clock), [])
            phase = "preseason" if i == 3 else "regular"
            raw = dict(
                id=8000 + i,
                season=2026,
                date=day,
                datetime=stamp(tip),
                status="Scheduled",
                season_type=phase,
                home_team={
                    "id": 2,
                    "abbreviation": "NYK",
                    "full_name": "New York Knicks",
                },
                visitor_team={
                    "id": 4,
                    "abbreviation": "MIA",
                    "full_name": "Miami Heat",
                },
            )

            def get(params, _key):
                rows = [raw] if params["season_type"] == phase else []
                return httpx.Response(
                    200, json={"data": rows, "meta": {"next_cursor": None}}
                )

            def advance(secs):
                v.now += timedelta(seconds=secs)

            with patch.dict(
                os.environ, {"BALLDONTLIE_API_KEY": "synthetic-regression"}
            ):
                self.assertTrue(
                    data.sync_bdl(
                        self.root, day, day, fetch=get, clock=v.clock, sleep=advance
                    )["complete"]
                )
        after = tip + timedelta(days=1)
        study = m.kickoff_differences(self.root, as_of=after)
        self.assertTrue(study["enough_days"])
        self.assertEqual(study["paired_days"], 3)
        self.assertEqual(study["counts"], {"exact": 4})
        self.assertFalse(study["days"][-1]["complete_day"])
        first = study["days"][0]
        self.assertTrue(first["score_scan_sha256"])
        self.assertEqual(len(first["catalogue_proof"]["page_sha256"]), 1)
        self.assertTrue(first["fixtures"][0]["venue_receipt_sha256"])
        with closing(m.storage.connect(self.root)) as db, db:
            db.execute(
                "DELETE FROM nba_market_catalogues WHERE completed=?",
                (first["catalogue_proof"]["completed"],),
            )
        study = m.kickoff_differences(self.root, as_of=after)
        self.assertEqual(study["paired_days"], 2)
        self.assertFalse(study["enough_days"])

    def test_real_fixed_windows_are_independent_of_cross_section(self):
        import hashlib
        import zipfile
        from scripts.verify_nba_market_lines import body, center, native_family
        from polymarket861.nba.markets_contracts import (
            contract_inventory,
            fixture,
            spread_archives,
        )

        folder = DEFAULT_FIXTURE.parents[3] / "reports/nba-phase1"
        report = json.loads(
            (folder / "market-collector-20261128T0126Z.json").read_text()
        )
        path = DEFAULT_FIXTURE.parent / report["fixture"]
        self.assertEqual(
            hashlib.sha256(path.read_bytes()).hexdigest(), report["fixture_sha256"]
        )
        valid = unlisted = spreads = 0
        with zipfile.ZipFile(path) as archive:
            for row in report["event_windows"]:
                e = body(archive, row["gamma"])
                g = fixture(e)
                response = body(archive, row["clob"])
                observed = m.api.moment(row["clob"]["received"])
                deadline = m.api.moment(row["kickoff"]) - timedelta(
                    minutes=m.WINDOWS[row["window"]]
                )
                self.assertTrue(g["pregame"])
                self.assertEqual(g["id"], row["id"])
                self.assertTrue(
                    deadline - m.WIDTH
                    <= m.api.moment(row["gamma"]["sent"])
                    <= observed
                    < deadline
                )
                books = {str(b["asset_id"]): b for b in response}
                self.assertEqual(len(books), len(response))
                core, _ = contract_inventory(e, g, main_only=False, native_only=True)
                sides = spread_archives(e, g)
                self.assertEqual(
                    set(books),
                    {t for c in core for t in (c["token_id"], c["opposite"])}
                    | {t for _, a, b in sides for t in (a, b)},
                )
                self.assertEqual(
                    set(row["spread_conditions_archived"]), {r[0] for r in sides}
                )
                spreads += len(sides)
                for family, picked in row["choices"].items():
                    self.assertTrue(
                        observed <= m.api.moment(picked["available_at"]) < deadline
                    )
                    self.assertEqual(m.api.moment(picked["observed_at"]), observed)
                    label = (
                        g["clubs"]["home"]["name"].casefold()
                        if family == "win"
                        else "over"
                    )
                    native = [
                        (q, c)
                        for c in e["markets"]
                        if native_family(e, c) == family
                        and c.get("acceptingOrders") is True
                        and c.get("closed") is False
                        and (q := center(c, books, label, observed)) is not None
                    ]
                    if native:
                        (rank, mid, token), c = min(native, key=lambda x: x[0][0])
                        self.assertEqual(
                            (
                                picked["quality"],
                                picked["condition_id"],
                                picked["token_id"],
                                picked["midpoint"],
                            ),
                            ("ok", c["conditionId"], token, float(mid)),
                        )
                        valid += 1
                    else:
                        self.assertEqual(picked["quality"], "unlisted")
                        unlisted += 1
        self.assertEqual((valid, unlisted, spreads), (17, 3, 4))

    def test_latest_score_cancellation_withdrawal_moved_day_and_after_tip_final(self):
        v = self.venue
        base = self.root
        now = v.now
        for case in ("postponed", "withdrawn", "unknown", "moved_day", "later_final"):
            with self.subTest(case=case):
                self.root = base.parent / case
                v.now = now
                self.tick()
                self.score_version(v.tip, now)
                seen = v.tip - timedelta(minutes=20)
                new_tip = v.tip + timedelta(days=1) if case == "moved_day" else v.tip
                state = {
                    "withdrawn": "unknown",
                    "moved_day": "scheduled",
                    "later_final": "final",
                }.get(case, case)
                if case == "later_final":
                    seen = v.tip + timedelta(hours=3)
                self.score_version(new_tip, seen, version=2, status=state)
                if case == "withdrawn":
                    with closing(m.storage.connect(self.root)) as db, db:
                        db.execute(
                            "UPDATE nba_game_versions SET payload=json_set(payload,'$.absence','absent_from_complete_range_scan') WHERE version=2"
                        )
                result = m.kickoff_differences(
                    self.root, as_of=v.tip + timedelta(days=2)
                )
                before = result["days"][0]["fixtures"][0]
                self.assertEqual(
                    before["status"],
                    {
                        "postponed": "score_schedule_unavailable",
                        "withdrawn": "score_schedule_withdrawn",
                        "unknown": "score_schedule_unavailable",
                        "moved_day": "score_schedule_replaced",
                        "later_final": "exact",
                    }[case],
                )
                if case == "moved_day":
                    self.assertEqual(
                        result["days"][1]["fixtures"][0]["status"], "missing_venue"
                    )

    def test_positive_in_progress_without_tip_vetoes_books_and_duplicate_model_even_without_gamma(
        self,
    ):
        v = self.venue
        self.score_version(v.tip, v.now, status="live", game_id="bdl:42")
        with closing(m.storage.connect(self.root)) as db, db:
            db.execute(
                "UPDATE nba_game_versions SET payload=json_set(payload,'$.kickoff',NULL,'$.play_observed',1)"
            )
        self.assertEqual(
            m.fixture_conflict(self.root, "NY", "MIA", v.tip, as_of=v.now),
            "score_source_observed_play",
        )
        self.assertEqual(self.tick()["state"], "degraded")
        self.assertFalse(any(url.endswith("/books") for url, _ in v.calls))
        self.score_version(v.tip, v.now)
        self.assertEqual(
            m.fixture_conflict(self.root, "NY", "MIA", v.tip, as_of=v.now),
            "ambiguous_score_source_fixture",
        )
        self.assertIsNone(
            m.fixture_conflict(self.root, "CHI", "IND", v.tip, as_of=v.now)
        )

    def test_original_basket_before_overlap_gets_separate_pair_inside_overlap(self):
        v = self.venue
        source = v.tip + timedelta(minutes=2)
        self.score_version(source, v.tip - timedelta(minutes=75))
        v.now = v.tip - timedelta(minutes=64)
        self.assertEqual(self.tick()["state"], "ok")
        self.assertFalse(self.book(source))
        v.event["markets"][0]["test_cents"] = 74
        v.now = v.tip - timedelta(minutes=62)
        self.assertEqual(self.tick()["state"], "ok")
        self.assertEqual(self.book(source)["win"]["probability"], 0.74)
        with closing(m.storage.open_existing(self.root)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT probability FROM nba_market_choices WHERE window='T-60m' AND family='win'"
                ).fetchone()[0],
                0.63,
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM nba_market_choices WHERE window LIKE 'paired|%'"
                ).fetchone()[0],
                4,
            )
        n = len(v.calls)
        self.tick()
        self.assertEqual(len(v.calls), n)

    def test_revised_epoch_cannot_use_earlier_price_even_inside_both_windows(self):
        v = self.venue
        self.score_version(v.tip, v.tip - timedelta(minutes=85))
        v.now = v.tip - timedelta(minutes=62)
        self.tick()
        self.assertTrue(self.book(v.tip))
        source = v.tip + timedelta(minutes=1)
        v.now += timedelta(seconds=20)
        self.score_version(source, v.now, version=2)
        self.assertFalse(self.book(source))
        v.event["markets"][0]["test_cents"] = 71
        v.now += timedelta(seconds=5)
        self.assertEqual(self.tick()["state"], "ok")
        self.assertEqual(self.book(source)["win"]["probability"], 0.71)
        self.assertEqual(self.book(source)["win"]["observed_at"], stamp(v.now))

    def test_legacy_phase_guess_cannot_count_as_a_paired_regular_game(self):
        v = self.venue
        self.tick()
        self.score_version(v.tip, v.now)
        with closing(m.storage.connect(self.root)) as db, db:
            db.execute(
                "UPDATE nba_game_versions SET payload=json_remove(payload,'$.stage_basis')"
            )
        result = m.kickoff_differences(self.root, as_of=v.tip + timedelta(days=1))
        self.assertEqual(result["counts"], {"unverified_score_stage": 1})
        self.assertEqual(
            result["days"][0]["fixtures"][0]["reason"], "legacy_requires_rescan"
        )
        self.assertFalse(result["enough_days"])
