"""Dispatch every version from the best timely lineup without moving the prediction deadline."""

import json
import unittest
from contextlib import closing
from unittest.mock import patch

import test_runner as scenarios

from polymarket861 import evidence, evidence_sources, store


class LateLineups(unittest.TestCase):
    def setUp(self):
        self.case = scenarios.RunnerTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.data = self.case.data
        self.game = self.case.fixtures()[0]
        self.versions = ("B1", "B2")
        ts = store.iso(self.case.known)
        with (
            scenarios.clock(ts),
            patch.object(evidence_sources, "now_utc", return_value=ts),
        ):
            evidence.register_source(
                self.data,
                {
                    "source_id": "desk",
                    "status": "active",
                    "free": True,
                    "upstream": "own",
                    "terms_locator": "test://terms",
                    "fields": {
                        "lineup": {
                            "methods": ["manual"],
                            "uses": ["prediction", "trial"],
                        }
                    },
                },
            )

    def lineup(self, side, at, *, confirmed=True, revision=1):
        ts = store.iso(at)
        team = self.game[side]
        with (
            scenarios.clock(ts),
            patch.object(evidence, "now_utc", return_value=ts),
            patch.object(evidence_sources, "now_utc", return_value=ts),
        ):
            evidence.import_evidence(
                self.data,
                [
                    {
                        "source_id": "desk",
                        "field": "lineup",
                        "fixture_id": self.game["fixture_id"],
                        "team": team,
                        "revision": revision,
                        "method": "manual",
                        "published_at": ts,
                        "received_at": ts,
                        "locator": "test://lineups/" + side,
                        "value": {
                            "confirmed": confirmed,
                            "starters": [team + str(n) for n in range(11)],
                        },
                    }
                ],
            )

    def run_at(self, time):
        with scenarios.clock(time):
            return self.case.dispatch(versions=self.versions, workers=2)

    def seals(self):
        with closing(store.connect(self.data)) as conn:
            return {
                r["model_version"]: json.loads(r["data_json"])
                for r in conn.execute(
                    "SELECT * FROM forecast_seals WHERE horizon='T-60m'"
                )
            }

    def test_wait_until_both_really_confirmed_all_versions_share_actual_frame(self):
        self.lineup("home", "2030-08-20T13:54:25Z")
        self.lineup("away", "2030-08-20T13:55:15Z", confirmed=False)
        waiting = self.run_at("2030-08-20T13:56:00Z")
        self.assertEqual(waiting["counts"]["waiting_lineups"], 2)
        self.assertEqual(
            waiting["next_window_opens"], store.iso("2030-08-20T13:58:30Z")
        )
        self.assertFalse(self.seals())
        self.lineup("away", "2030-08-20T13:57:20Z", revision=2)
        done = self.run_at("2030-08-20T13:57:40Z")
        self.assertEqual(done["counts"]["done"], 2)
        bodies = self.seals()
        self.assertEqual(set(bodies), set(self.versions))
        for body in bodies.values():
            self.assertTrue(all(team["confirmed"] for team in body["lineups"].values()))
        self.assertEqual(
            len({s["comparison_proof"]["frame"] for s in bodies.values()}), 1
        )
        self.assertEqual(
            len(
                {
                    s["baseline_comparator"]["input_proof_sha256"]
                    for s in bodies.values()
                }
            ),
            1,
        )
        self.assertEqual(
            self.run_at("2030-08-20T13:59:10Z")["counts"]["already_done"], 2
        )

    def test_backup_window_publishes_missing_evidence_once_and_expired_is_missed(self):
        self.lineup("home", "2030-08-20T13:54:25Z")
        self.assertEqual(
            self.run_at("2030-08-20T13:58:29Z")["counts"]["waiting_lineups"], 2
        )
        due = self.run_at("2030-08-20T13:58:30Z")
        self.assertEqual(due["counts"]["done"], 2)
        before = self.seals()
        for b in before.values():
            self.assertFalse(b["lineups"]["away"]["confirmed"])
            self.assertEqual(b["decision"]["state"], "no_paper_order")
        self.lineup("away", "2030-08-20T13:59:25Z")
        self.assertEqual(self.run_at("2030-08-20T14:01Z")["counts"]["already_done"], 2)
        self.assertEqual(self.seals(), before)
        with scenarios.clock("2030-08-20T14:01Z"):
            other = self.case.dispatch(versions=["B0"])
        self.assertTrue(all(r["state"] == "missed" for r in other["jobs"]))
