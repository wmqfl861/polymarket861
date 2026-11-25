import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack, closing
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from test_result_feed import match

from polymarket861 import (
    datasets,
    football,
    input_manifest,
    pipeline,
    store,
)
from polymarket861 import (
    result_admission as admission,
)
from polymarket861 import result_feed as feed

YEAR = 2026
SEASON = "2026-27"
START = "2026-08-01T12:00:00Z"
AFTER = store.instant("2026-08-23T12:00:00Z")


class VerifiedResults(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.time = store.iso(START)
        stack = ExitStack()
        self.addCleanup(stack.close)
        for module in (store, feed, datasets, input_manifest, pipeline):
            stack.enter_context(
                patch.object(module, "now_utc", side_effect=lambda: self.time)
            )
        teams = [("Arsenal FC", "Brentford FC"), ("Chelsea FC", "Fulham FC")]
        self.rows = [
            match(i + 10, a, b, s)
            for i, ((a, b), s) in enumerate(zip(teams, ([2, 1], [1, 0])))
        ]
        self.fids = [football.fixture_id(SEASON, a, b) for a, b in teams]
        source = self.root / "input" / SEASON / "en.1.json"
        source.parent.mkdir(parents=True)
        source.write_text(
            json.dumps(
                {
                    "matches": [
                        {
                            "round": "Matchday 1",
                            "date": "2026-08-21",
                            "time": "20:00",
                            "team1": a,
                            "team2": b,
                        }
                        for a, b in teams
                    ]
                }
            )
        )
        self.root_files = source.parent.parent
        hist = self.root_files / "2025-26" / "en.1.json"
        hist.parent.mkdir()
        hist.write_text(
            json.dumps(
                {
                    "matches": [
                        {
                            "round": "Matchday 1",
                            "date": "2025-08-20",
                            "time": "17:00",
                            "team1": a,
                            "team2": b,
                            "score": {"ft": [2, 1]},
                        }
                        for a, b in teams
                    ]
                }
            )
        )
        self.view = datasets.observe(
            self.root,
            self.root_files,
            revision="d" * 40,
            expected={
                SEASON: football.blob_sha(source.read_bytes()),
                "2025-26": football.blob_sha(hist.read_bytes()),
            },
            register_season=SEASON,
        )
        with closing(store.connect(self.root)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM fixture_versions").fetchone()[0], 2
            )
        p = pipeline.make_forecast(
            self.root, self.fids[0], None, mode="technical", horizon="early"
        )
        self.seal = p["digest"]

    def set_time(self, minutes):
        self.time = store.iso(AFTER + timedelta(minutes=minutes))

    def collect(self, rows, *, season=False):
        raw = json.dumps(rows).encode()

        def get(url):
            return (
                b'"example remote change marker"'
                if "/getlastchangedate/" in url
                else raw
            )

        with patch.object(feed, "_get", side_effect=get):
            return feed.sync(
                self.root, YEAR, round_id=None if season else 1, confirm=True
            )

    def reconcile(self):
        return feed.sync_season(self.root, YEAR, offline=True)

    def state(self, fid):
        return next(
            f
            for f in store.eligible_fixtures(self.root, season=SEASON)
            if f["fixture_id"] == fid
        )

    def test_two_receipts_restore_correct_remove_at_actual_admission(self):
        fid = self.fids[0]
        self.set_time(0)
        self.collect(self.rows, season=True)
        self.assertEqual(self.reconcile()["available_results"], 0)
        self.set_time(12)
        self.collect(self.rows)
        self.assertEqual(self.reconcile()["available_results"], 0)
        self.set_time(43)
        self.collect(self.rows)
        result = self.reconcile()
        self.assertEqual(result["available_results"], 2)
        self.assertEqual(result["rounds"][0]["reports"][0]["completed"], 2)
        confirmed = self.time
        self.assertEqual(admission.verified_regulation(self.root, fid)["home_goals"], 2)
        self.assertIsNone(
            admission.verified_regulation(self.root, fid, at=store.iso(AFTER))
        )
        self.assertTrue(result["manifest"])
        changed = deepcopy(self.rows)
        changed[0]["matchResults"][-1]["pointsTeam1"] = 3
        self.set_time(75)
        self.collect(changed)
        hold = self.time
        self.assertEqual(self.reconcile()["available_results"], 1)
        self.assertIsNone(self.state(fid)["home_goals"])
        self.assertIsNone(admission.verified_regulation(self.root, fid))
        self.set_time(107)
        self.collect(changed)
        self.assertEqual(self.reconcile()["available_results"], 2)
        self.assertEqual(admission.verified_regulation(self.root, fid)["home_goals"], 3)
        new = input_manifest.verify_observed_inputs(self.root)
        f = next(
            x
            for x in football.load_fixtures(
                new["root"], evidence_path=new["evidence_path"]
            )
            if x.fixture_id == fid
        )
        self.assertIsNone(football._result_at(f, AFTER))
        self.assertEqual(football._result_at(f, store.instant(confirmed)), (2, 1))
        self.assertIsNone(football._result_at(f, store.instant(hold)))
        self.assertEqual(football._result_at(f, store.instant(self.time)), (3, 1))
        with closing(store.connect(self.root)) as db:
            old = db.execute(
                "SELECT digest,data_json FROM forecast_seals WHERE fixture_id=?", (fid,)
            ).fetchone()
            self.assertEqual(self.seal, store.digest(json.loads(old["data_json"])))
        self.assertEqual(old["digest"], self.seal)
        self.assertEqual(self.reconcile()["rounds"], [])

    def test_different_teams_bad_schedule_or_changed_bytes_do_not_verify(self):
        self.set_time(0)
        self.collect(self.rows, season=True)
        altered = deepcopy(self.rows)
        altered[0]["matchDateTimeUTC"] = "2026-08-21T20:00:00Z"
        altered[1]["team1"]["teamName"], altered[1]["team2"]["teamName"] = (
            "Fulham FC",
            "Chelsea FC",
        )
        self.set_time(31)
        self.collect(altered)
        self.assertEqual(self.reconcile()["available_results"], 0)
        self.set_time(62)
        capture = self.collect(self.rows)
        self.set_time(93)
        self.collect(self.rows)
        self.assertEqual(self.reconcile()["available_results"], 2)
        store.blob_path(self.root, capture["sha256"]).write_bytes(
            b"altered archived source"
        )
        self.set_time(94)
        r = self.reconcile()
        self.assertEqual(r["available_results"], 0)
        self.assertTrue(r["receipt_errors"])
        self.assertIsNone(admission.verified_regulation(self.root, self.fids[0]))

    def test_complete_manual_batch_with_evidence_rollback_and_conflict(self):
        self.set_time(0)
        evidence = self.root / "matchsheet.txt"
        evidence.write_text("Independent club match reports: 2-1 and 1-0.")
        copies = [
            {
                "home": r["team1"]["teamName"],
                "away": r["team2"]["teamName"],
                "kickoff": r["matchDateTimeUTC"],
                "home_goals": r["matchResults"][-1]["pointsTeam1"],
                "away_goals": r["matchResults"][-1]["pointsTeam2"],
                "status": "final",
                "period": "regulation",
                "locator": f"page {i + 1}",
            }
            for i, r in enumerate(self.rows)
        ]
        path = self.root / "round.json"
        base = {
            "season": SEASON,
            "round": 1,
            "rights": "permission",
            "source": "club match sheets",
            "evidence_file": evidence.name,
            "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            "matches": copies,
        }
        path.write_text(json.dumps(base | {"matches": copies[:1]}))
        with self.assertRaisesRegex(ValueError, "whole"):
            feed.import_round(self.root, YEAR, 1, path)
        self.assertIsNone(self.state(self.fids[0])["home_goals"])
        path.write_text(json.dumps(base))
        r = feed.import_round(self.root, YEAR, 1, path)
        self.assertEqual(self.state(self.fids[0])["home_goals"], 2)
        self.assertIsNone(admission.verified_regulation(self.root, self.fids[0]))
        self.assertEqual(
            feed.import_round(self.root, YEAR, 1, path)["batch_id"], r["batch_id"]
        )
        self.set_time(10)
        undone = feed.rollback_batch(self.root, r["batch_id"], "incorrect source file")
        self.assertEqual(undone["count"], 2)
        self.assertIsNone(self.state(self.fids[0])["home_goals"])
        self.assertEqual(
            feed.rollback_batch(self.root, r["batch_id"], "retry")["at"], undone["at"]
        )
        with closing(store.connect(self.root)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM result_versions").fetchone()[0], 2
            )
        self.assertEqual(len(self.reconcile()["overdue"]), 2)

    def test_fallback_revision_triggers_quarantine_and_regrade_without_a_season_tick(
        self,
    ):
        self.set_time(0)
        self.collect(self.rows, season=True)
        self.set_time(31)
        self.collect(self.rows)
        self.reconcile()
        document = self.root_files / SEASON / "en.1.json"
        original = json.loads(document.read_text())
        original["matches"][0]["score"] = {"ft": [0, 0]}
        self.set_time(62)
        for rev, score, held in (("e", [0, 0], True), ("f", [2, 1], False)):
            original["matches"][0]["score"] = {"ft": score}
            document.write_text(json.dumps(original))
            datasets.observe(
                self.root,
                self.root_files,
                revision=rev * 40,
                expected={SEASON: football.blob_sha(document.read_bytes())},
                register_season=SEASON,
                current_head=True,
            )
            f = self.state(self.fids[0])
            self.assertEqual(f["home_goals"] is None, held)
            self.assertEqual(
                admission.verified_regulation(self.root, self.fids[0]) is None, held
            )
            self.set_time(93)

    def test_later_complete_round_omits_an_admitted_fixture(self):
        self.set_time(0)
        self.collect(self.rows, season=True)
        self.set_time(31)
        self.collect(self.rows)
        self.assertEqual(self.reconcile()["available_results"], 2)
        self.set_time(64)
        self.collect(self.rows[1:])
        r = self.reconcile()
        self.assertEqual(r["available_results"], 1)
        self.assertIsNone(admission.verified_regulation(self.root, self.fids[0]))
        self.assertEqual(
            admission.verified_regulation(self.root, self.fids[1])["home_goals"], 1
        )

    def test_github_api_quota_still_pins_head_from_git_advertisement(self):
        sha = b"3" * 40
        line = sha + b" HEAD\0multi_ack thin-pack\n"
        data = (
            b"001e# service=git-upload-pack\n0000"
            + f"{len(line) + 4:04x}".encode()
            + line
            + b"0000"
        )
        with patch.object(datasets, "_get", side_effect=[OSError("quota"), data]):
            self.assertEqual(datasets._provider_head(), sha.decode())
