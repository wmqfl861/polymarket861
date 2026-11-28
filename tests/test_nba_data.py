import csv
import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from polymarket861.nba import data, data_capabilities, storage


TEST_KEY = "synthetic-source-credential-12345"


def game(gid=1, **updates):
    base = {
        "id": gid,
        "season": 2021,
        "date": "2021-11-10",
        "datetime": "2021-11-11T00:30:00Z",
        "status_state": "scheduled",
        "status": "7:30 pm ET",
        "period": 0,
        "home_team_score": 0,
        "visitor_team_score": 0,
        "postseason": False,
        "home_team": {"id": 1, "abbreviation": "ATL", "full_name": "Atlanta Hawks"},
        "visitor_team": {"id": 2, "abbreviation": "BOS", "full_name": "Boston Celtics"},
    }
    return {**base, **updates}


def page(rows, cursor=None):
    return httpx.Response(200, json={"data": rows, "meta": {"next_cursor": cursor}})


class NbaData(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "nba"
        self.at = datetime(2021, 11, 10, 12, tzinfo=timezone.utc)
        self.addCleanup(patch.stopall)
        patch.dict(
            os.environ,
            {
                "BALLDONTLIE_API_KEY": TEST_KEY,
                "DUNKAPI_API_KEY": TEST_KEY,
                "BIGBALLS_API_KEY": TEST_KEY,
            },
        ).start()
        self.calls = []

    def clock(self):
        return self.at

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)

    def sync(self, *responses, day="2021-11-10"):
        replies = iter(responses)

        def fetch(params, key):
            self.assertEqual(key, TEST_KEY)
            self.calls.append((self.at, params))
            if params["season_type"] != "regular":
                return page([])
            return next(replies)

        return data.sync_bdl(
            self.root, day, day, fetch=fetch, clock=self.clock, sleep=self.advance
        )

    def test_phase_score_and_ambiguous_time(self):
        self.assertEqual(data.normalize_bdl(game())["stage"], "unknown")
        for cup, expected in (
            ("Championship", "cup_final"),
            ("West Semifinal", "regular"),
            (None, "unknown"),
        ):
            self.assertEqual(
                data.normalize_bdl(game(ist_stage=cup), "ist")["stage"], expected
            )
        self.assertIsNone(
            data.normalize_bdl(game(datetime="2021-11-11T00:30"))["kickoff"]
        )
        self.assertIsNone(
            data.normalize_bdl(game(datetime="2022-03-11T00:30Z"))["kickoff"]
        )
        for raw in (
            game(home_team_score=4),
            game(period=1),
            game(status="2nd Quarter"),
            game(
                status_state="final",
                status="Final",
                home_team_score=100,
                visitor_team_score=100,
            ),
        ):
            row = data.normalize_bdl(raw, "regular")
            self.assertEqual(row["status"], "unknown")
            self.assertIsNone(row["home_score"])
        self.assertEqual(
            data.normalize_bdl(game(status_state="scheduled", status="Postponed"))[
                "status"
            ],
            "postponed",
        )
        raw = game(
            status_state="final",
            status="Final/2OT",
            home_team_score=101,
            visitor_team_score=99,
            period=4,
        )
        row = data.normalize_bdl(raw, "regular")
        self.assertEqual(
            (row["status"], row["score_basis"], row["home_score"]),
            ("final", "final_includes_ot", 101),
        )

    def test_postponed_and_live_details_block_even_with_a_scheduled_label(self):
        for fields in (
            {"time": "3:44"},
            {"period_detail": "4:12 - 1st Qtr"},
            {"period_detail": "1st Half"},
            {"visitor_q1": 4},
            {"home_ot1": 8},
            {"time": "Final"},
        ):
            with self.subTest(fields=fields):
                row = data.normalize_bdl(game(**fields), "regular")
                self.assertEqual(row["status"], "unknown")
                self.assertTrue(row["play_observed"])
        postponed = data.normalize_bdl(game(postponed=True), "regular")
        self.assertEqual(postponed["status"], "postponed")
        self.assertFalse(postponed["play_observed"])
        self.assertEqual(
            data.normalize_bdl(game(time=" ", postponed=False), "regular")["status"],
            "scheduled",
        )
        premature = game(
            status_state="final",
            status="Final",
            period=4,
            home_team_score=102,
            visitor_team_score=91,
        )
        self.sync(page([premature]))
        row = data.games_as_of(self.root)[0]
        self.assertEqual(
            (row["status"], row["home_score"], row["timing_problem"]),
            ("unknown", None, "play_received_before_reported_kickoff"),
        )

    def test_pagination_budget_versions_and_real_cutoff(self):
        first = game()
        initial = self.sync(page([first], 97), page([game(2)]))
        self.assertTrue(initial["complete"])
        self.assertEqual(self.calls[1][1]["cursor"], 97)
        self.assertGreaterEqual(
            (self.calls[1][0] - self.calls[0][0]).total_seconds(), 15
        )
        cut1 = self.clock()
        self.sync(page([game(datetime="2021-11-11T01:00:00Z"), game(2)]))
        cut2 = self.clock()
        self.sync(page([first, game(2)]))
        self.sync(page([first, game(2)]))
        versions = data.game_versions(self.root, game_id="bdl:1")
        self.assertEqual([r["version"] for r in versions], [1, 2, 3])
        self.assertEqual(versions[0]["kickoff"], versions[2]["kickoff"])
        self.assertEqual(data.games_as_of(self.root, cut1)[0]["version"], 1)
        self.assertEqual(
            data.game_versions(self.root, game_id="bdl:1", as_of=cut2)[-1]["version"], 2
        )
        self.assertFalse(data.games_as_of(self.root, cut1 - timedelta(days=1)))
        with closing(storage.open_existing(self.root)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM nba_game_requests").fetchone()[0], 9
            )
            blob = db.execute("SELECT sha,body FROM nba_game_blobs LIMIT 1").fetchone()
            self.assertEqual(
                hashlib.sha256(zlib.decompress(blob["body"])).hexdigest(), blob["sha"]
            )
            self.assertNotIn(TEST_KEY, "\n".join(db.iterdump()))

    def test_final_corrections_and_complete_scan_absence(self):
        self.advance(86400)
        final = game(
            status_state="final",
            status="Final",
            period=4,
            home_team_score=101,
            visitor_team_score=99,
        )
        self.sync(page([final]))
        before = self.clock()
        self.sync(page([dict(final, home_team_score=102)]))
        corrected = self.clock()
        partial = self.sync(page([game(2)], 12), httpx.Response(403, text=TEST_KEY))
        self.assertTrue(partial["partial"])
        self.assertEqual(
            data.game_versions(self.root, game_id="bdl:1")[-1]["status"], "final"
        )
        self.sync(page([game(2)]))
        missing = data.game_versions(self.root, game_id="bdl:1")[-1]
        self.assertEqual(
            (missing["absence"], missing["home_score"]),
            ("absent_from_complete_range_scan", None),
        )
        self.assertEqual(data.games_as_of(self.root, before)[0]["home_score"], 101)
        self.assertEqual(data.games_as_of(self.root, corrected)[0]["home_score"], 102)

    def test_bad_cursor_quota_and_inert_no_key(self):
        with patch.dict(os.environ, {}, clear=True):
            result = self.sync()
            self.assertEqual(result["state"], "missing_key")
            self.assertFalse(self.root.exists())
            caps = data.capabilities(self.root)
            self.assertEqual(caps["player_model_gate"], "closed")
            self.assertTrue(
                all(not x["key_configured"] for x in caps["sources"].values())
            )
        bad = self.sync(page([game()], 10), page([game(2)], 10))
        self.assertEqual((bad["state"], bad["complete"]), ("invalid_response", False))
        limited = self.sync(
            httpx.Response(429, headers={"Retry-After": "120"}, text=TEST_KEY)
        )
        self.assertEqual(limited["state"], "rate_limited")
        n = len(self.calls)
        self.assertEqual(self.sync()["retry_at"], limited["retry_at"])
        self.assertEqual(len(self.calls), n)

    def test_probes_summarize_only_and_respect_persistent_budget(self):
        self.at = datetime(2026, 11, 18, 19, tzinfo=timezone.utc)
        row = {
            "player_id": "80765432",
            "name": "Example Player",
            "position": "G",
            "team": "ATL",
            "status": "Day-To-Day",
            "type": "Ankle",
            "detail": "private injury detail",
            "return_date": "2026-11-25",
            "unlisted_vendor_field": "private value",
        }
        resp = httpx.Response(
            200,
            json={"data": {"date": "2026-11-18", "count": 1, "injuries": [row]}},
        )

        def fetch(source, key):
            self.assertEqual((source, key), ("dunkapi", TEST_KEY))
            return resp

        sample = data_capabilities.probe(
            self.root, "dunkapi", fetch=fetch, clock=self.clock
        )
        self.assertEqual(sample["state"], "sample_received")
        self.assertTrue(sample["authenticated"])
        self.assertEqual((sample["sample_rows"], sample["player_status_rows"]), (1, 1))
        self.assertEqual(sample["source_dates"], {"data.date": "2026-11-18"})
        self.assertEqual(sample["league_scope_basis"], "nba_scoped_request")
        self.assertEqual(sample["upstream_clocks"], {})
        self.assertFalse(sample["upstream_published_at_semantics_verified"])
        self.assertFalse(sample["sample_contains_all_nba_players"])
        self.assertEqual(
            sample["response_sha256"], hashlib.sha256(resp.content).hexdigest()
        )
        self.assertFalse(sample["player_model_eligible"])
        blocked = data_capabilities.probe(
            self.root,
            "dunkapi",
            fetch=lambda *_: self.fail("duplicate probe reached transport"),
            clock=self.clock,
        )
        self.assertEqual(blocked["state"], "budget_wait")
        caps = data.capabilities(self.root)
        dunk = caps["sources"]["dunkapi"]
        self.assertEqual(
            dunk["observed_injury_fields"],
            [
                "detail",
                "name",
                "player_id",
                "position",
                "return_date",
                "status",
                "team",
                "type",
            ],
        )
        self.assertEqual(dunk["distinct_sample_hashes_same_utc_day"], 1)
        with closing(storage.open_existing(self.root)) as db:
            dump = "\n".join(db.iterdump())
        for private in (
            TEST_KEY,
            "80765432",
            "Example Player",
            "private injury detail",
            "unlisted_vendor_field",
            "private value",
            "2026-11-25",
        ):
            self.assertNotIn(private, dump)
            self.assertNotIn(private, json.dumps(caps))

    def test_dunkapi_empty_and_legacy_reports_preserve_date_location(self):
        day = "2026-11-18"
        for case, body, state, dates, count in (
            (
                "empty",
                {"data": {"date": day, "count": 0, "injuries": []}},
                "empty_sample",
                {"data.date": day},
                0,
            ),
            (
                "unknown_snapshot_day",
                {
                    "date": day,
                    "data": {
                        "date": None,
                        "count": 0,
                        "injuries": [],
                        "note": "no report",
                    },
                },
                "empty_sample",
                {},
                0,
            ),
            (
                "legacy_bare",
                {"date": day, "injuries": [{"player_id": "7", "status": "Out"}]},
                "sample_received",
                {"date": day},
                1,
            ),
        ):
            with self.subTest(case=case):
                response = httpx.Response(200, json=body)
                sample = data_capabilities.probe(
                    self.root / case,
                    "dunkapi",
                    fetch=lambda *_: response,
                    clock=self.clock,
                )
                self.assertEqual(sample["state"], state)
                self.assertTrue(sample["authenticated"])
                self.assertEqual(sample["source_dates"], dates)
                self.assertEqual(sample["player_status_rows"], count)
                self.assertEqual(sample["upstream_clocks"], {})
                self.assertNotIn("no report", json.dumps(sample))

    def test_dunkapi_invalid_envelopes_do_not_accept_outer_decoy_rows(self):
        report = {"date": "2026-11-18", "count": 0, "injuries": []}
        for case, body in (
            ("missing_rows", {"data": {"date": "2026-11-18", "count": 0}}),
            ("missing_count", {"data": {"date": "2026-11-18", "injuries": []}}),
            ("count_mismatch", {"data": {**report, "count": 1}}),
            ("string_count", {"data": {**report, "count": "0"}}),
            ("boolean_count", {"data": {**report, "count": False}}),
            ("non_array", {"data": {**report, "injuries": {}}}),
            ("non_object_row", {"data": {**report, "count": 1, "injuries": [None]}}),
            ("invalid_inner_with_outer_rows", {**report, "data": None}),
        ):
            with self.subTest(case=case):
                response = httpx.Response(200, json=body)
                sample = data_capabilities.probe(
                    self.root / case,
                    "dunkapi",
                    fetch=lambda *_: response,
                    clock=self.clock,
                )
                self.assertEqual(sample["http_status"], 200)
                self.assertEqual(sample["state"], "invalid_response")
                self.assertFalse(sample["authenticated"])
                self.assertFalse(sample["player_model_eligible"])

    def test_dunkapi_unusable_identities_and_statuses_add_no_player_evidence(self):
        rows = [
            {"player_id": True, "status": "Out"},
            {"player_id": ["7"], "status": "Out"},
            {"player_id": " ", "status": "Out"},
            {"player": {"team": {"id": "7"}}, "status": "Out"},
            {"player_id": "7", "status": {"label": "Out"}},
            {"player_id": "7", "status": True},
            {"player_id": "7", "status": " "},
        ]
        report = {"date": "2026-11-18", "count": len(rows), "injuries": rows}
        response = httpx.Response(200, json={"data": report})
        sample = data_capabilities.probe(
            self.root, "dunkapi", fetch=lambda *_: response, clock=self.clock
        )
        self.assertEqual(sample["player_status_rows"], 0)
        self.assertFalse(sample["player_model_eligible"])

    def test_injury_probe_http_uses_documented_provider_requests(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={})

        with (
            httpx.Client(transport=httpx.MockTransport(handle)) as client,
            patch.object(httpx, "get", client.get),
            patch.object(httpx, "post", client.post),
        ):
            for source in ("bigballs", "dunkapi"):
                data_capabilities._probe_http(source, TEST_KEY)

        big, dunk = requests
        self.assertEqual(big.method, "GET")
        self.assertEqual(
            str(big.url.copy_with(query=None)),
            "https://api.bigballsdata.com/v1/injuries",
        )
        self.assertEqual(dict(big.url.params), {"sport": "basketball", "limit": "200"})
        self.assertEqual(dunk.method, "POST")
        self.assertEqual(str(dunk.url), "https://mcp.dunkapi.com/v1/get_injuries")
        self.assertEqual(json.loads(dunk.content), {"league": "nba"})
        self.assertTrue(all(r.headers.get("x-api-key") == TEST_KEY for r in requests))

    def test_conflicting_same_instant_receipts_stay_quarantined_until_new_observation(
        self,
    ):
        row = game(
            status="Final",
            status_state="final",
            period=4,
            home_team_score=101,
            visitor_team_score=100,
        )
        later = dict(row, home_team_score=98)
        self.at += timedelta(days=1)

        def put(db, raw):
            at = data._iso(self.clock())
            data._capture(
                db,
                {"source": "test"},
                data._encoded(raw).encode(),
                [data.normalize_bdl(raw, "regular")],
                200,
                "game_page",
                at,
                self.clock,
            )

        with closing(data._open(self.root)) as db:
            put(db, row)
            self.advance(1)
            # An unchanged page also establishes a receipt timestamp, even without a new game version.
            put(db, row)
            put(db, later)
            put(db, row)
            tied_at = self.clock()
            q = data.games_as_of(self.root, tied_at)[0]
            self.assertEqual(
                (q["status"], q["home_score"], q["away_score"]), ("unknown", None, None)
            )
            self.assertEqual(q["conflict"], "simultaneous_provider_observations")
            self.assertEqual(len(q["conflict_receipts"]), 2)
            for sha in q["conflict_receipts"]:
                raw = db.execute(
                    "SELECT body FROM nba_game_blobs WHERE sha=?", (sha,)
                ).fetchone()[0]
                self.assertEqual(hashlib.sha256(zlib.decompress(raw)).hexdigest(), sha)
            self.advance(1)
            put(db, later)
            self.assertEqual(data.games_as_of(self.root)[0]["home_score"], 98)
            self.assertEqual(
                data.games_as_of(self.root, tied_at)[0]["status"], "unknown"
            )

    def test_eastern_game_date_midnight_and_dst_for_fixtures_and_finals(self):
        for day, tip, valid in (
            ("2021-11-10", "2021-11-09T00:04:00Z", False),
            ("2021-11-07", "2021-11-08T04:59:59Z", True),
            ("2021-11-07", "2021-11-08T05:00:00Z", False),
            ("2021-11-07", "2021-11-07T01:30:00-04:00", True),
            ("2021-11-07", "2021-11-07T01:30:00-05:00", True),
            ("2022-03-13", "2022-03-14T03:59:59Z", True),
            ("2022-03-13", "2022-03-14T04:00:00Z", False),
        ):
            for raw in (
                game(),
                game(
                    status_state="final",
                    status="Final",
                    period=4,
                    home_team_score=102,
                    visitor_team_score=91,
                ),
            ):
                with self.subTest(tip=tip, status=raw["status_state"]):
                    rec = data.normalize_bdl(
                        dict(raw, date=day, datetime=tip), "regular"
                    )
                    if valid:
                        self.assertEqual(rec["status"], raw["status_state"])
                        self.assertIsNotNone(rec["kickoff"])
                    else:
                        self.assertEqual(
                            (rec["status"], rec["kickoff"], rec["home_score"]),
                            ("unknown", None, None),
                        )
                        self.assertEqual(
                            rec["timing_problem"],
                            "kickoff_disagrees_with_eastern_game_date",
                        )
        good = game(
            status_state="final",
            status="Final",
            period=4,
            home_team_score=102,
            visitor_team_score=91,
        )
        self.advance(86400)
        self.sync(page([good]))
        cutoff = self.clock()
        self.sync(page([dict(good, datetime="2021-11-09T00:04:00Z")]))
        self.assertEqual(data.games_as_of(self.root, cutoff)[0]["status"], "final")
        self.assertEqual(data.games_as_of(self.root)[0]["status"], "unknown")

    def test_scan_proof_asof_reports_latest_range_including_partial_failure(self):
        day = "2021-11-10"
        self.assertIsNone(data.schedule_proof(self.root, day, self.at))
        original = self.sync(page([game()]))
        stop = self.clock()
        self.advance(5)
        observed = {}

        def broken(params, key):
            observed.update(data.schedule_proof(self.root, day, self.clock()))
            self.advance(1)
            return httpx.Response(503)

        failed = data.sync_bdl(
            self.root, day, day, fetch=broken, clock=self.clock, sleep=self.advance
        )
        self.assertFalse(failed["complete"])
        self.assertEqual((observed["state"], observed["finished"]), ("running", None))
        self.assertEqual(
            data.schedule_proof(self.root, day, self.clock())["state"], "unavailable"
        )
        self.assertEqual(
            data.schedule_proof(self.root, day, failed["began"])["state"], "running"
        )
        self.assertEqual(
            data.schedule_proof(self.root, day, stop)["scan_id"], original["scan_id"]
        )
        self.assertIsNone(data.schedule_proof(self.root, "2021-11-11", self.clock()))

    def test_mixed_page_preserves_postponement_quarantines_identified_corruption(self):
        self.sync(page([game(1), game(2), game(3)]))
        result = self.sync(
            page([game(1, postponed=True), game(2, home_team=None), game(3)])
        )
        self.assertFalse(result["complete"])
        self.assertEqual(result["state"], "source_quality")
        self.assertEqual(result["quality"]["quarantined_rows"], 1)
        observed = {r["game_id"]: r for r in data.games_as_of(self.root)}
        self.assertEqual(observed["bdl:1"]["status"], "postponed")
        self.assertEqual(
            (observed["bdl:2"]["status"], observed["bdl:2"]["quarantine"]),
            ("unknown", "invalid_game_metadata"),
        )
        failed = self.sync(page([game(1, postponed=False), {"date": "2021-11-10"}]))
        self.assertFalse(failed["complete"])
        self.assertEqual(failed["quality"]["unidentified_rows"], 1)
        proof = data.schedule_proof(self.root, "2021-11-10", self.clock())
        self.assertEqual(
            (proof["state"], proof["quality"]), ("invalid_response", failed["quality"])
        )
        self.assertEqual(
            data.game_versions(self.root, game_id="bdl:3")[-1]["status"], "scheduled"
        )
        self.assertEqual(
            data.game_versions(self.root, game_id="bdl:1")[-1]["status"], "scheduled"
        )

    def test_locked_commit_cannot_backdate_availability(self):
        written = threading.Event()
        row = data.normalize_bdl(game(), "regular")

        def writer():
            with closing(data._open(self.root)) as db:
                db.set_trace_callback(
                    lambda sql: (
                        written.set()
                        if "INSERT INTO nba_game_requests" in sql
                        else None
                    )
                )
                data._capture(
                    db,
                    {},
                    b"source",
                    [row],
                    200,
                    "game_page",
                    data._iso(data._now()),
                    data._now,
                )

        with (
            closing(data._open(self.root)) as reader,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM nba_game_versions").fetchone()
            job = pool.submit(writer)
            try:
                self.assertTrue(written.wait(5))
                cutoff = data._now()
                self.assertEqual(
                    reader.execute("SELECT count(*) FROM nba_game_versions").fetchone()[
                        0
                    ],
                    0,
                )
            finally:
                reader.commit()
            job.result(timeout=5)
        self.assertFalse(data.games_as_of(self.root, cutoff))
        self.assertEqual(len(data.games_as_of(self.root)), 1)

    def test_unpublished_versions_hidden_until_witness_after_recovery(self):
        row = data.normalize_bdl(game(), "regular")
        with closing(data._open(self.root)) as db:
            with patch.object(
                data, "_publish_pending", side_effect=OSError("interrupted witness")
            ):
                with self.assertRaises(OSError):
                    data._capture(
                        db,
                        {},
                        b"source",
                        [row],
                        200,
                        "game_page",
                        data._iso(self.clock()),
                        self.clock,
                    )
            missing_at = self.clock()
            self.assertEqual(data.game_versions(self.root), [])
            self.assertEqual(
                data.capabilities(self.root)["sources"]["balldontlie"][
                    "authenticated_business_receipts"
                ],
                0,
            )
            self.advance(4)
            data._capture(
                db,
                {},
                b"source",
                [row],
                200,
                "game_page",
                data._iso(self.clock()),
                self.clock,
            )
        self.assertEqual(data.game_versions(self.root, as_of=missing_at), [])
        self.assertEqual(
            data.games_as_of(self.root)[0]["available_at"], data._iso(self.clock())
        )

    def test_archive_hash_orientation_and_separate_time(self):
        stream = io.StringIO()
        w = csv.writer(stream)
        w.writerow(
            "game_id year_id date_game team_id opp_id pts opp_pts _iscopy lg_id game_location is_playoffs".split()
        )
        w.writerows(
            [
                ("g", 2015, "12/7/2014", "X", "Y", 8, 7, 0, "NBA", "N", 0),
                ("g", 2015, "12/7/2014", "Y", "X", 7, 8, 1, "NBA", "N", 0),
                ("a", 1975, "12/7/1974", "P", "Q", 8, 7, 0, "ABA", "H", 1),
            ]
        )
        raw = stream.getvalue().encode()
        f = self.root.parent / "fake-test.csv"
        f.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "SHA"):
            data.parse_538(f)
        with patch.object(data, "ARCHIVE_SHA", hashlib.sha256(raw).hexdigest()):
            self.assertEqual(data.import_538(self.root, f)["new_versions"], 1)
            self.assertEqual(data.import_538(self.root, f)["new_versions"], 0)
        self.assertFalse(data.games_as_of(self.root))
        self.assertFalse(data.games_as_of(self.root, self.at, source="538"))
        row = data.games_as_of(self.root, source="538")[0]
        self.assertEqual(
            (
                row["season"],
                row["home"],
                row["away"],
                row["neutral"],
                row["home_designation"],
            ),
            (2014, "X", "Y", True, "synthetic_neutral"),
        )
        self.assertIsNone(row["kickoff"])


if __name__ == "__main__":
    unittest.main()
