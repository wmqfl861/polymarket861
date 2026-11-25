"""Exercise production activation, rollback, clock and recipe gates independently of score estimation."""

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

import test_jobs
from polymarket861 import jobs, jobs_research, pipeline, store
from test_recipe_upgrade import old_build


class ReleaseApproval(unittest.TestCase):
    def setUp(self):
        self.case = test_jobs.ResearchTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root
        self.registered = self.case.registered
        self.review = self.case.judged
        self.later = "2031-05-26T03:00:00.000000Z"
        self.approve = lambda: jobs.approve_candidate(
            self.root,
            "candidate",
            reviewer="analyst",
            reason="Reviewed forward and historical comparisons",
        )
        self.qualified = {
            "qualified": True,
            "complete": True,
            "rank": 1.0,
            "reason": "qualified",
        }

    def recommend(self):
        self.case.register_model()
        with (
            patch.object(store, "now_utc", return_value=self.review),
            patch.object(jobs_research, "_assess", return_value=dict(self.qualified)),
        ):
            self.assertEqual(
                jobs.evaluate_candidate(self.root, "candidate")["state"], "recommended"
            )

    def test_upgrade_between_evidence_calculation_and_approval_is_observed_under_write_lock(
        self,
    ):
        with old_build():
            self.recommend()
        entered, release = threading.Event(), threading.Event()

        def assess(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return dict(self.qualified)

        with (
            patch.object(store, "now_utc", return_value=self.later),
            patch.object(jobs_research, "_assess", side_effect=assess),
            ThreadPoolExecutor(1) as pool,
        ):
            pending = pool.submit(self.approve)
            try:
                self.assertTrue(entered.wait(5))
                observed = pipeline.prepare_builtin(self.root)
            finally:
                release.set()
            blocked = pending.result(timeout=5)
        self.assertEqual(
            (
                blocked["state"],
                blocked["assessment"]["reason"],
                blocked["assessment"]["active_version"],
            ),
            ("shadow", "active_baseline_changed", observed),
        )
        self.assertEqual(
            jobs.active_version(self.root, default="B1", at=self.later), observed
        )
        self.assertFalse(
            any(
                e["action"] == "promoted"
                for e in jobs.experiment_status(self.root)["events"]
            )
        )

    def test_named_recipe_bound_to_old_install_cannot_be_approved_on_unchanged_baseline(
        self,
    ):
        original = pipeline._build_recipe

        def different_b2(family, lines):
            recipe = original(family, lines)
            if family == "B2":
                recipe["algorithm_source"]["football_model.py"] = "a" * 64
            return recipe

        with patch.object(pipeline, "_build_recipe", side_effect=different_b2):
            self.recommend()
        self.assertIsNone(jobs_research._bound_recipe(self.root, "B2-trial"))
        with (
            patch.object(jobs_research, "_assess", return_value=dict(self.qualified)),
            patch.object(store, "now_utc", return_value=self.later),
        ):
            blocked = self.approve()
        self.assertEqual(
            (
                blocked["state"],
                blocked["assessment"]["reason"],
                blocked["assessment"]["unavailable_version"],
            ),
            ("shadow", "registered_recipe_not_executable", "B2-trial"),
        )
        self.assertIsNone(jobs.active_candidate(self.root))

    def test_old_promotion_rolls_back_to_effective_build_and_legacy_static_rows_follow_releases(
        self,
    ):
        with (
            old_build(),
            patch.object(jobs_research, "_assess", return_value=dict(self.qualified)),
        ):
            self.recommend()
            with patch.object(store, "now_utc", return_value=self.review):
                promoted = self.approve()
        self.assertEqual(promoted["state"], "promoted")
        with closing(store.connect(self.root)) as db, db:
            old = promoted["assessment"].copy()
            old.pop("previous_version")
            db.execute(
                "UPDATE research_candidates SET assessment=? WHERE key=?",
                (store.canonical(old), "candidate"),
            )
        with patch.object(store, "now_utc", return_value=self.later):
            start = threading.Barrier(3)

            def rollback():
                start.wait(5)
                return jobs.rollback_candidate(
                    self.root,
                    "candidate",
                    reviewer="duty analyst",
                    reason="Rollback independently reviewed",
                )

            def upgrade():
                start.wait(5)
                return pipeline.prepare_builtin(self.root)

            with ThreadPoolExecutor(3) as pool:
                one, two, build = (
                    pool.submit(rollback),
                    pool.submit(rollback),
                    pool.submit(upgrade),
                )
                self.assertEqual(one.result(timeout=5)["state"], "rolled_back")
                self.assertEqual(two.result(timeout=5)["state"], "rolled_back")
                self.assertTrue(build.result(timeout=5).startswith("B1@"))
            self.assertEqual(
                sum(
                    e["action"] == "rolled_back"
                    for e in jobs.experiment_status(self.root)["events"]
                ),
                1,
            )
            active = jobs.active_version(self.root, default="B1")
            self.assertTrue(active.startswith("B1@"))
            event = next(
                e
                for e in jobs.experiment_status(self.root)["events"]
                if e["action"] == "rolled_back"
            )
            self.assertEqual(
                (
                    event["evidence"]["restored_version"],
                    event["evidence"]["previous_version_at_promotion"],
                ),
                (active, "B1"),
            )
            self.assertEqual(
                jobs.active_version(self.root, default="B1", at=self.review), "B2-trial"
            )
            with closing(store.connect(self.root)) as db, db:
                # Previous releases wrote a permanent-looking B1 row without ownership by any candidate.
                db.execute(
                    "UPDATE research_active SET version='B1' WHERE kind='model' AND scope='epl' AND candidate_key IS NULL"
                )
                ev = event["evidence"].copy()
                ev["restored_version"] = "B1"
                ev.pop("previous_version_at_promotion")
                db.execute(
                    "UPDATE research_events SET evidence=? WHERE action='rolled_back'",
                    (json.dumps(ev),),
                )
            self.assertEqual(
                jobs.active_version(self.root, at=self.later, default="B1"), active
            )
            self.assertEqual(
                jobs.experiment_status(self.root)["active"][0]["version"], active
            )
