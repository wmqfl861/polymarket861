"""Physical release provenance, immutable legacy comparisons and transitive execution scope."""

from contextlib import closing
import gzip
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from polymarket861 import model_study as study
from polymarket861 import pipeline, store, strategy

OLD = {
    "family": "B1",
    "lines": [0.5, 1.5, 2.5, 3.5],
    "algorithm_source": {
        "football.py": "a7283a3c9b4ebb2752590eb6388a973f328ea672c50e7c3b5531154154e2908b",
        "football_model.py": "093e90294315132d2c60826734bb7a12fc329264ca8d4863bb3ad69bd5b2b829",
    },
}


def comparison(root, historical_format):
    with closing(study._db(root)) as db, db:
        db.execute(
            "INSERT INTO model_recipes VALUES(?,?,?)",
            ("B1", store.canonical(OLD), store.now_utc()),
        )
        db.commit()
        new = store.digest(pipeline._build_recipe("B1", OLD["lines"]))
        # Use the genuine 1,900-row packaged predecessor. The controlled candidate has identical predictions.
        doc = study._read(root, db, study.LEGACY_RECIPE, study.BENCHMARK) | {
            "recipe": new
        }
        sha = store.archive_bytes(
            root, "test-study", gzip.compress(store.canonical(doc).encode(), mtime=0)
        )["sha256"]
        db.execute(
            "INSERT INTO probability_studies VALUES(?,?,?)",
            (new, store.digest(study.BENCHMARK), sha),
        )
    with patch.object(study, "measure"):
        current = study.release_report(root, None)
    if historical_format == "current":
        return current
    original = {k: v for k, v in current.items() if k != "report_sha256"}
    if historical_format == "audit-format":
        original["baseline_identity"] = {
            "source": "reproduced_tagged_predecessor",
            "reference_commit": study.LEGACY_BENCHMARK_CHECKOUT,
            "reference_pipeline_sha256": current["baseline_identity"][
                "reference_pipeline_sha256"
            ],
            "legacy_recipe_files": ["football.py", "football_model.py"],
            "local_previous_pipeline_independently_known": False,
        }
    else:
        del original["baseline_identity"]
    sha = store.digest(original)
    with closing(study._db(root)) as db, db:
        db.execute(
            "INSERT INTO model_release_comparisons VALUES(?,?)",
            (sha, store.canonical(original)),
        )
    return original | {"report_sha256": sha}


class LegacyPhysicalIdentity(unittest.TestCase):
    def test_both_reviewed_commits_sign_only_the_identity_supplied(self):
        self.assertEqual(store.digest(OLD), study.LEGACY_RECIPE)
        for fmt, commit in (
            ("current", study.LEGACY_RELEASE),
            ("audit-format", study.LEGACY_BENCHMARK_CHECKOUT),
            ("before-identity", study.LEGACY_RELEASE),
        ):
            with self.subTest(format=fmt), tempfile.TemporaryDirectory() as root:
                report = comparison(root, fmt)
                sha = report["report_sha256"]
                if fmt == "current":
                    identity = report["baseline_identity"]
                    self.assertEqual(
                        (identity["reference_commit"], identity["benchmark_checkout"]),
                        (study.LEGACY_RELEASE, study.LEGACY_BENCHMARK_CHECKOUT),
                    )
                    self.assertEqual(
                        set(identity["identical_execution_git_objects"]),
                        {"src", "pyproject.toml", "constraints", "deploy"},
                    )
                before = study.inspect_release(root, sha)
                self.assertIsNone(before["decision"])
                self.assertEqual(store.digest(before["report"]), sha)

                def sign(ref=None):
                    return study.approve_release(
                        root,
                        sha,
                        "operator",
                        "checked the actual installed package",
                        legacy_reference=ref,
                    )

                with self.assertRaisesRegex(ValueError, "confirm the actual"):
                    sign()
                for bad in ("0" * 40, commit[:7]):
                    with self.assertRaisesRegex(ValueError, "unverified"):
                        sign(bad)
                self.assertEqual(pipeline.prepare_builtin(root), "B1")
                self.assertEqual(sign(commit)["legacy_reference"], commit)
                self.assertEqual(sign()["legacy_reference"], commit)
                with self.assertRaisesRegex(ValueError, "different installed commit"):
                    sign(next(x for x in study.LEGACY_INSTALLATIONS if x != commit))
                with self.assertRaisesRegex(ValueError, "unverified"):
                    sign("f" * 40)
                retrieved = study.inspect_release(root, sha)
                self.assertEqual(retrieved["report"], before["report"])
                self.assertEqual(retrieved["decision"]["legacy_reference"], commit)
                # A DB predating the new separate receipt has no signed SHA to recreate.
                if fmt == "audit-format":
                    with closing(study._db(root)) as db, db:
                        db.execute("DROP TABLE model_release_legacy_confirmations")
                    self.assertIsNone(
                        study.inspect_release(root, sha)["decision"]["legacy_reference"]
                    )
                    self.assertIsNone(sign(commit)["legacy_reference"])

    def test_model_and_selector_follow_every_executable_module(self):
        essential = {
            "datasets.py",
            "runner.py",
            "opportunity.py",
            "market_provider.py",
            "forward_window.py",
            "experiment_statistics.py",
            "result_admission.py",
            "context_feeds.py",
            "strategy_scope.py",
            "ledger.py",
            "football_model.py",
        }
        actual = pipeline._build_recipe("B1", OLD["lines"])
        self.assertLessEqual(essential, actual["algorithm_source"].keys())
        self.assertFalse(
            {"operations.py", "dashboard.py"} & actual["algorithm_source"].keys()
        )
        complete = study.decision_implementation()
        self.assertEqual(strategy.implementation_digest(), store.digest(complete))
        self.assertEqual(actual["algorithm_source"], complete["algorithm_source"])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(study, "PACKAGE_ROOT", Path(tmp)),
        ):
            root = Path(tmp)
            for name in essential:
                (root / name).write_text("first\n")
            model = lambda: store.digest(pipeline._build_recipe("B1", OLD["lines"]))
            for name in sorted(essential | {"future_provider.py"}):
                prior = (model(), strategy.implementation_digest())
                (root / name).write_text("updated collection or decision\n")
                self.assertNotEqual(model(), prior[0], name)
                self.assertNotEqual(strategy.implementation_digest(), prior[1], name)
            unchanged = model(), strategy.implementation_digest()
            for cosmetic in ("dashboard.py", "operations.py"):
                (root / cosmetic).write_text("updated presentation or backup code")
                self.assertEqual((model(), strategy.implementation_digest()), unchanged)
            self.assertIn(
                "future_provider.py",
                study.decision_implementation()["algorithm_source"],
            )
