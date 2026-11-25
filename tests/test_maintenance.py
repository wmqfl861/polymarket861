import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from polymarket861 import jobs, maintenance, store


class MaintenanceTests(unittest.TestCase):
    def test_repeated_parallel_ticks_and_corrected_round(self):
        with tempfile.TemporaryDirectory() as root:
            rnd = {
                "mode": "technical",
                "horizon": "T-60m",
                "expected": 10,
                "completed": 10,
                "count": 10,
                "score": 70,
                "season": "2025-26",
                "round_id": 1,
            }
            store.save_report(root, "round", "1", rnd)
            store.save_report(root, "round", "history", dict(rnd, mode="replay"))
            store.save_report(
                root,
                "incident",
                "bad-field",
                {"status": "quarantined", "items": ["bad-score"]},
            )
            with ThreadPoolExecutor(3) as pool:
                list(pool.map(lambda _: maintenance.tick(root), range(3)))
            pending = jobs.jobs_status(root)["jobs"]
            self.assertEqual(
                sorted(j["key"].split("-")[1] for j in pending),
                ["discovery", "incident", "round"],
            )
            self.assertEqual(maintenance.tick(root)["new_jobs"], [])
            store.save_report(root, "round", "1", dict(rnd, score=71))
            self.assertEqual(len(maintenance.tick(root)["new_jobs"]), 1)
            self.assertEqual(
                {j["state"] for j in jobs.jobs_status(root)["jobs"]}, {"pending"}
            )
