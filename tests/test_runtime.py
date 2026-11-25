"""Regression tests for the integrated forecast clock, storage, scoring and backup."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from polymarket861 import (
    datasets,
    football,
    input_manifest,
    operations,
    pipeline,
    qualification,
    store,
)

UTC = timezone.utc


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "state"
        self.history = self.base / "history"
        self.history.mkdir()

    def register(
        self,
        key="a",
        home="Home",
        away="Away",
        kickoff="2025-08-16T12:00:00Z",
        known="2025-08-01T09:00:00Z",
        round_id=1,
    ):
        return store.register_fixture(
            self.root,
            {
                "fixture_id": key,
                "season": "2025-26",
                "round_id": round_id,
                "home": home,
                "away": away,
                "kickoff": kickoff,
                "known_at": known,
                "source": "fixture-test",
                "locator": "test://schedule",
            },
        )

    def test_immutable_content_and_verifiable_restore(self):
        with ThreadPoolExecutor(max_workers=3) as ex:
            seen = list(
                ex.map(
                    lambda d: store.archive_bytes(
                        self.root, "test", b"public payload", observed_at=d
                    ),
                    [
                        "2024-01-01T00:00:00Z",
                        "2024-01-01T00:00:01Z",
                        "2024-01-01T00:00:00Z",
                    ],
                )
            )
        self.assertEqual(len({v["sha256"] for v in seen}), 1)
        with closing(store.connect(self.root)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 2
            )
        store.save_report(self.root, "round", "test", {"score": 72})
        dest = self.base / "backup"
        operations.backup_state(self.root, dest)
        target = self.base / "restore"
        operations.restore_state(dest, target)
        self.assertEqual(store.list_reports(target, "round")[0]["payload"]["score"], 72)
        with self.assertRaises(ValueError):
            operations.restore_state(dest, target)
        store.blob_path(dest, seen[0]["sha256"]).write_bytes(b"wrong")
        with self.assertRaisesRegex(ValueError, "integrity"):
            operations.verify_backup(dest)

    def test_conflicting_schedule_and_out_of_order_result(self):
        self.register()
        self.register(kickoff="2025-08-17T12:00:00Z", known="2025-08-04T09:00:00Z")
        self.assertTrue(
            store.fixture_at(self.root, "a", "2025-08-02T00:00:00Z")[
                "kickoff"
            ].startswith("2025-08-16")
        )
        self.assertTrue(
            store.fixture_at(self.root, "a")["kickoff"].startswith("2025-08-17")
        )
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.register(kickoff="2025-08-19T12:00:00Z", known="2025-08-04T09:00:00Z")
        one = {
            "fixture_id": "a",
            "home_goals": 1,
            "away_goals": 0,
            "source": "official",
            "locator": "test://result",
            "published_at": "2025-08-17T15:01:00Z",
            "finalized_at": "2025-08-17T15:00:00Z",
            "received_at": "2025-08-17T15:02:00Z",
        }
        self.assertEqual(store.record_result(self.root, one)["version"], 1)
        self.assertEqual(store.record_result(self.root, one)["version"], 1)
        two = one | {
            "home_goals": 2,
            "published_at": "2025-08-17T16:00:00Z",
            "received_at": "2025-08-17T16:01:00Z",
        }
        self.assertEqual(store.record_result(self.root, two)["version"], 2)
        with self.assertRaisesRegex(ValueError, "older"):
            store.record_result(self.root, one)
        self.assertEqual(store.eligible_fixtures(self.root)[0]["home_goals"], 2)

    def test_no_backdated_forward_seals_and_future_replay(self):
        self.register()
        with self.assertRaisesRegex(ValueError, "window"):
            pipeline.make_forecast(self.root, "a", self.history, mode="technical")
        with self.assertRaisesRegex(ValueError, "restricted"):
            pipeline.make_forecast(
                self.root, "a", self.history, mode="technical", archive_lag_hours=24
            )
        with self.assertRaisesRegex(ValueError, "activation"):
            pipeline.make_forecast(self.root, "a", self.history, mode="formal")
        future = datetime.now(UTC) + timedelta(days=2)
        self.register(key="future", kickoff=store.iso(future), known=store.now_utc())
        with self.assertRaisesRegex(ValueError, "future"):
            pipeline.make_forecast(self.root, "future", self.history, mode="replay")

    def test_late_schedule_receipt_and_result_are_bound_to_their_kickoff(self):
        self.register()
        then = "2025-08-08T12:00:00Z"
        self.assertEqual(store.fixture_at(self.root, "a", then)["version"], 1)
        with self.assertRaisesRegex(ValueError, "no known schedule"):
            store.fixture_at(self.root, "a", then, prospective=True)
        store.record_result(
            self.root,
            {
                "fixture_id": "a",
                "home_goals": 1,
                "away_goals": 0,
                "source": "test",
                "locator": "test://original-final",
                "finalized_at": "2025-08-16T14:00:00Z",
                "published_at": "2025-08-16T14:02:00Z",
                "received_at": "2025-08-16T14:03:00Z",
            },
        )
        self.register(kickoff="2025-09-13T12:00:00Z", known="2025-08-18T09:00:00Z")
        self.assertIsNone(store.eligible_fixtures(self.root)[0]["home_goals"])

    def test_trial_needs_all_ten_matches_at_both_cutoffs_in_both_rounds(self):
        season = "2025-26"
        batch = []
        for rnd in (1, 2):
            for i in range(10):
                batch.append(
                    {
                        "round": f"Matchday {rnd}",
                        "date": f"2025-08-{16 + 7 * (rnd - 1)}",
                        "time": "13:00",
                        "team1": f"Home {i}",
                        "team2": f"Away {(i + rnd - 1) % 10}",
                    }
                )
        path = self.history / season / "en.1.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"matches": batch}), encoding="utf-8")
        with (
            patch.object(
                input_manifest, "now_utc", return_value="2025-08-01T08:00:00.000000Z"
            ),
            patch.object(
                datasets, "now_utc", return_value="2025-08-01T08:00:00.000000Z"
            ),
        ):
            manifest = datasets.observe(
                self.root,
                self.history,
                expected={season: football.blob_sha(path.read_bytes())},
            )
        for rnd in (1, 2):
            for number in range(10):
                game = batch[(rnd - 1) * 10 + number]
                home, away = game["team1"], game["team2"]
                fid = football.fixture_id(season, home, away)
                day = 16 + 7 * (rnd - 1)
                fixture = self.register(
                    key=fid,
                    home=home,
                    away=away,
                    round_id=rnd,
                    kickoff=f"2025-08-{day}T12:00:00Z",
                )
                store.record_result(
                    self.root,
                    {
                        "fixture_id": fid,
                        "home_goals": 1,
                        "away_goals": 1,
                        "source": "test",
                        "locator": "test://final",
                        "finalized_at": f"2025-08-{day}T14:00:00Z",
                        "published_at": f"2025-08-{day}T14:01:00Z",
                        "received_at": f"2025-08-{day}T14:02:00Z",
                    },
                )
                for horizon, cutoff in (
                    ("T-24h", f"2025-08-{day - 1}T12:00:00Z"),
                    ("T-60m", f"2025-08-{day}T11:00:00Z"),
                ):
                    payload = {
                        "fixture_id": fid,
                        "horizon": horizon,
                        "round_id": rnd,
                        "season": season,
                        "home": home,
                        "away": away,
                        "kickoff": fixture["kickoff"],
                        "mode": "technical",
                        "input_manifest_sha256": manifest["manifest_digest"],
                    }
                    seal = (
                        fid,
                        fixture["kickoff"],
                        cutoff,
                        horizon,
                        "B1",
                        store.PROTOCOL,
                        "technical",
                        cutoff,
                        store.digest(payload),
                        store.canonical(payload),
                    )
                    if rnd == 2 and number == 9 and horizon == "T-60m":
                        missing = seal
                        continue
                    with closing(store.connect(self.root)) as db, db:
                        db.execute(
                            "INSERT INTO forecast_seals VALUES (?,?,?,?,?,?,?,?,?,?)",
                            seal,
                        )
        # Manually entered final scores alone do not prove these EPL matches have finished.
        self.assertFalse(
            qualification.qualification(self.root)["checks"][
                "two_consecutive_observed_technical_rounds"
            ]
        )
        path.write_text(
            json.dumps({"matches": [dict(row, score={"ft": [1, 1]}) for row in batch]}),
            encoding="utf-8",
        )
        datasets.observe(
            self.root,
            self.history,
            revision="b" * 40,
            expected={season: football.blob_sha(path.read_bytes())},
        )
        partial = qualification.qualification(self.root)
        self.assertEqual(
            partial["observed_matchdays"][-1]["sealed"], {"T-24h": 10, "T-60m": 9}
        )
        self.assertFalse(partial["checks"]["two_consecutive_observed_technical_rounds"])
        with closing(store.connect(self.root)) as db, db:
            db.execute(
                "INSERT INTO forecast_seals VALUES (?,?,?,?,?,?,?,?,?,?)", missing
            )
        self.assertTrue(
            qualification.qualification(self.root)["checks"][
                "two_consecutive_observed_technical_rounds"
            ]
        )

    def test_missing_forecast_counts_and_score_corrections(self):
        probs = {
            "home_win": 0.5,
            "draw": 0.3,
            "away_win": 0.2,
            "total_over_2_5": 0.4,
            "home_over_1_5": 0.6,
            "away_over_1_5": 0.25,
        }
        for i in range(10):
            f = self.register(key=f"m{i}", home=f"h{i}", away=f"a{i}")
            store.record_result(
                self.root,
                {
                    "fixture_id": f["fixture_id"],
                    "home_goals": 2,
                    "away_goals": 0,
                    "source": "open-test",
                    "locator": "test://result",
                    "archive_at": "2025-08-20T12:00:00Z",
                    "published_by": "2025-08-20T12:00:00Z",
                    "finalized_by": "2025-08-20T12:00:00Z",
                },
            )
            if i == 9:
                continue
            payload = {
                "fixture_id": f["fixture_id"],
                "probabilities": probs,
                "recipe_digest": "test-digest",
            }
            at = store.iso("2025-08-16T11:00:00Z")
            with closing(store.connect(self.root)) as db, db:
                db.execute(
                    "INSERT INTO forecast_seals VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        f["fixture_id"],
                        f["kickoff"],
                        at,
                        "T-60m",
                        "B1",
                        store.PROTOCOL,
                        "replay",
                        store.now_utc(),
                        store.digest(payload),
                        store.canonical(payload),
                    ),
                )
        result = pipeline.publish_round(self.root, "2025-26", 1, mode="replay")
        self.assertEqual(
            (result["count"], result["missing"], result["expected"], result["status"]),
            (9, 1, 10, "incomplete"),
        )
        self.assertAlmostEqual(result["score"], 85.6875)
        self.assertAlmostEqual(
            result["full_round_range"][1] - result["full_round_range"][0], 10
        )
        changed = pipeline.import_result_and_score(
            self.root,
            {
                "fixture_id": "m0",
                "home_goals": 0,
                "away_goals": 2,
                "source": "official",
                "locator": "test://changed",
                "published_at": "2025-08-21T15:01:00Z",
                "finalized_at": "2025-08-16T15:00:00Z",
                "received_at": "2025-08-21T15:02:00Z",
            },
        )
        self.assertEqual(changed["result"]["version"], 2)
        self.assertLess(changed["round_reports"][0]["score"], result["score"])

    def test_shadow_or_replay_cannot_authorize_formal_orders(self):
        with self.assertRaisesRegex(ValueError, "timely formal"):
            store.check_formal_forecast(
                self.root, "none", "a", "2025-08-16T12:00:00Z", "2025-08-16T11:00:00Z"
            )
        status = qualification.qualification(self.root)
        self.assertTrue(status["checks"]["paper_ledger_idempotency"])
        self.assertFalse(status["ready"])
        with self.assertRaisesRegex(ValueError, "not qualified"):
            qualification.activate(
                self.root, store.iso(datetime.now(UTC) + timedelta(days=1))
            )


if __name__ == "__main__":
    unittest.main()
