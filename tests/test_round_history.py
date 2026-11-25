"""Official-round editions, evidence links and ordering of concurrent corrections."""

import contextlib
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from polymarket861 import cli, pipeline, round_history as history, store

SEASON = "2025-26"
OLD = "2025-08-16T14:00Z"
NEW = "2025-08-23T14:00Z"
PROBS = dict(
    home_win=0.5,
    draw=0.3,
    away_win=0.2,
    total_over_2_5=0.4,
    home_over_1_5=0.6,
    away_over_1_5=0.25,
)


class RoundEditions(unittest.TestCase):
    def test_review_requires_a_complete_prospective_t60_report(self):
        report = {"mode": "technical", "horizon": "T-60m", "expected": 10, "completed": 10}
        for mode in ("formal", "technical"):
            self.assertTrue(history.ready_for_experiment_review(report | {"mode": mode}))
        for change in (
            {"mode": "replay"},
            {"horizon": "T-24h"},
            {"horizon": "early"},
            {"completed": 9},
            {"expected": None, "completed": None},
            {"expected": 0, "completed": 0},
            {"expected": True, "completed": True},
        ):
            with self.subTest(change=change):
                self.assertFalse(history.ready_for_experiment_review(report | change))

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def schedule(self, kick=OLD, seen="2025-08-01T10:00Z"):
        return store.register_fixture(
            self.root,
            dict(
                fixture_id="fixture",
                season=SEASON,
                round_id=1,
                home="A",
                away="B",
                kickoff=kick,
                known_at=seen,
                source="league",
                locator="official fixture bulletin",
            ),
        )

    def score(self, goals, stamp="2025-08-24T18:00Z"):
        return store.record_result(
            self.root,
            dict(
                fixture_id="fixture",
                home_goals=goals[0],
                away_goals=goals[1],
                source="official scorer",
                locator="score revision " + stamp,
                finalized_at=stamp,
                published_at=stamp,
                received_at=stamp,
            ),
        )

    def seal(self, kick, cut, prob=PROBS):
        body = {
            "kickoff": store.iso(kick),
            "probabilities": prob,
            "recipe_digest": "test recipe",
        }
        checksum = store.digest(body)
        with contextlib.closing(store.connect(self.root)) as db, db:
            db.execute(
                "INSERT INTO forecast_seals VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "fixture",
                    store.iso(kick),
                    store.iso(cut),
                    "T-60m",
                    "B1",
                    store.PROTOCOL,
                    "replay",
                    store.now_utc(),
                    checksum,
                    store.canonical(body),
                ),
            )
        return checksum

    def publish(self):
        return pipeline.publish_round(self.root, SEASON, 1, mode="replay")

    def editions(self, n=None):
        return history.round_history(self.root, SEASON, 1, mode="replay", edition=n)

    def test_postponement_missing_prediction_and_result_correction(self):
        self.schedule()
        old = self.seal(OLD, "2025-08-16T13:00Z")
        self.assertEqual(self.publish()["report_version"], 1)
        self.schedule(NEW, "2025-08-15T12:00Z")
        self.assertEqual(self.publish()["report_version"], 2)
        self.score((2, 0))
        missing = self.publish()
        self.assertEqual(missing["fixtures"][0]["status"], "missing_forecast")
        self.assertEqual(missing["fixtures"][0]["result_version"], 1)
        fresh = self.seal(NEW, "2025-08-23T13:00Z")
        graded = self.publish()
        self.assertEqual(graded["report_version"], 4)
        self.assertAlmostEqual(graded["score"], 85.6875)
        self.assertEqual(graded["fixtures"][0]["digest"], fresh)
        self.assertEqual(graded["calibration"]["n"], 1)
        self.assertEqual(graded["hits"]["result"]["correct"], 1)
        self.assertEqual(
            sum(b["n"] for b in graded["calibration"]["outcomes"]["draw"]), 1
        )
        self.assertEqual(self.publish(), graded)
        self.score((0, 2), "2025-08-26T18:00Z")
        revised = self.publish()
        self.assertLess(revised["score"], graded["score"])
        listing = self.editions()
        self.assertEqual(listing["count"], 5)
        change = listing["editions"][-1]["fixture_changes"][0]
        self.assertEqual(
            (change["before"]["result_version"], change["after"]["result_version"]),
            (1, 2),
        )
        self.assertEqual(
            (change["before"]["digest"], change["after"]["digest"]), (fresh, fresh)
        )
        self.assertEqual(self.editions(4)["report"]["fixtures"][0]["goals"], [2, 0])
        self.assertEqual(self.editions(5)["report"]["fixtures"][0]["goals"], [0, 2])
        self.assertEqual(
            store.digest(self.editions(4)["report"]), graded["report_sha256"]
        )
        with contextlib.closing(store.connect(self.root)) as db:
            seals = {r[0] for r in db.execute("SELECT digest FROM forecast_seals")}
        self.assertEqual(seals, {old, fresh})
        result = io.StringIO()
        with contextlib.redirect_stdout(result):
            self.assertEqual(
                cli.main(
                    [
                        "--data-dir",
                        str(self.root),
                        "forecast",
                        "round-history",
                        SEASON,
                        "1",
                        "--mode",
                        "replay",
                        "--edition",
                        "4",
                    ]
                ),
                0,
            )
        self.assertEqual(json.loads(result.getvalue()), self.editions(4))

    def test_existing_current_view_migrates_once_with_original_timestamp(self):
        key = f"{SEASON}/1/T-60m/replay/B1"
        legacy = {
            "round_id": 1,
            "status": "provisional",
            "score": 61.25,
            "fixtures": [{"fixture_id": "fixture", "status": "pending"}],
        }
        store.save_report(self.root, "round", key, legacy)
        timestamp = store.list_reports(self.root, "round")[0]["updated_at"]
        self.assertEqual(self.editions(1)["report"], legacy)
        self.assertEqual(self.editions(1)["report_origin"], "legacy_latest")
        self.assertEqual(self.editions(1)["report_published_at"], timestamp)
        self.assertEqual(self.editions()["count"], 1)
        self.schedule()
        self.assertEqual(self.publish()["report_version"], 2)
        self.assertEqual(self.editions(1)["report"], legacy)
        self.assertEqual(self.editions()["count"], 2)
        with self.assertRaisesRegex(ValueError, "not found"):
            self.editions(3)

    def test_old_calculation_does_not_overwrite_new_score(self):
        self.schedule()
        self.score((2, 0))
        self.seal(OLD, "2025-08-16T13:00Z")
        entered, release, attempt = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        original = history._grade

        def pause(*args):
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(5))
            return original(*args)

        def correct_and_grade():
            attempt.set()
            self.score((0, 2), "2025-08-26T20:00Z")
            return self.publish()

        with (
            patch.object(history, "_grade", side_effect=pause),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(self.publish)
            self.assertTrue(entered.wait(5))
            latest = pool.submit(correct_and_grade)
            self.assertTrue(attempt.wait(5))
            release.set()
            previous, updated = first.result(timeout=10), latest.result(timeout=10)
        self.assertEqual(
            (previous["report_version"], updated["report_version"]), (1, 2)
        )
        self.assertEqual(
            store.list_reports(self.root, "round")[0]["payload"]["report_sha256"],
            updated["report_sha256"],
        )
        self.assertEqual(self.editions(1)["report"]["fixtures"][0]["goals"], [2, 0])
        self.assertEqual(self.editions(2)["report"]["fixtures"][0]["goals"], [0, 2])
        with ThreadPoolExecutor(max_workers=4) as pool:
            repeats = list(pool.map(lambda _: self.publish(), range(8)))
        self.assertEqual({r["report_version"] for r in repeats}, {2})
        self.assertEqual(self.editions()["count"], 2)

    def test_tied_directions_have_an_explicit_denominator(self):
        self.schedule()
        self.score((2, 0))
        probs = dict(PROBS, home_win=0.4, draw=0.4, away_win=0.2, total_over_2_5=0.5)
        self.seal(OLD, "2025-08-16T13:00Z", probs)
        r = self.publish()
        self.assertEqual(
            r["hits"]["result"], {"correct": 0, "eligible": 0, "ties": 1, "rate": None}
        )
        self.assertEqual(r["hits"]["total"], r["hits"]["result"])
        self.assertEqual(r["calibration"]["n"], 1)
        self.assertEqual(r["hits"]["home"]["correct"], 1)
