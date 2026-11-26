"""The approval protects a released model, its inputs and its actual retained installation."""

import importlib.util
import json
import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from polymarket861 import (
    datasets,
    football,
    model_study,
    pipeline,
    season_readiness,
    store,
)


class RecipeScopeTests(unittest.TestCase):
    def test_presentation_other_adapters_and_a_new_news_epoch_leave_b1_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "package"
            shutil.copytree(
                model_study.PACKAGE_ROOT,
                source,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            with patch.object(model_study, "PACKAGE_ROOT", source):
                first = pipeline.register_recipe(root, "B1", "B1")
                news = pipeline._study_recipe(first["recipe"])
                for filename in (
                    "dashboard.py",
                    "assets/dashboard.css",
                    "foreign_league_adapter.py",
                ):
                    path = source / filename
                    with path.open("a") as stream:
                        stream.write(
                            "\n/* colour */\n"
                            if path.suffix == ".css"
                            else "\nNEW_DISPLAY_NAME = 'entry'\n"
                        )
                # A real news model code change creates a separate paired epoch.
                path = source / "news_model.py"
                path.write_text(
                    path.read_text().replace(
                        "def paired_prediction(", "def renamed_original("
                    )
                    + "\ndef paired_prediction(*args, **kwargs): return None\n"
                )
                second = pipeline.register_recipe(root, "B1", "B1")
                self.assertEqual(first["recipe_digest"], second["recipe_digest"])
                self.assertNotEqual(
                    first["release_provenance_sha256"],
                    second["release_provenance_sha256"],
                )
                self.assertNotEqual(news, pipeline._study_recipe(second["recipe"]))
                self.assertEqual(season_readiness.release(root)["state"], "ready")
                with closing(store.connect(root)) as db:
                    for record in (first, second):
                        sha = record["release_provenance_sha256"]
                        doc = db.execute(
                            "SELECT payload FROM reports WHERE kind='release-provenance' AND key=?",
                            (sha,),
                        ).fetchone()
                        self.assertEqual(store.digest(json.loads(doc[0])), sha)
                        self.assertEqual(
                            model_study.inspect_provenance(root, sha)[
                                "release_provenance_sha256"
                            ],
                            sha,
                        )
                for filename, before, after in (
                    (
                        "football_model.py",
                        "DECAY_PER_DAY = 0.002",
                        "DECAY_PER_DAY = 0.003",
                    ),
                    ("pipeline.py", "timedelta(hours=24)", "timedelta(hours=23)"),
                ):
                    path = source / filename
                    original = path.read_text()
                    self.assertIn(before, original)
                    path.write_text(original.replace(before, after, 1))
                    with self.assertRaisesRegex(ValueError, "different recipe"):
                        pipeline.register_recipe(root, "B1", "B1")
                    self.assertFalse(season_readiness.release(root)["ready"])
                    path.write_text(original)

    def test_changed_observation_writes_different_training_score_and_requires_release(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            shutil.copytree(
                model_study.PACKAGE_ROOT,
                package,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            history = root / "history"
            path = history / "2024-25" / "en.1.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "matches": [
                            {
                                "round": "Matchday 1",
                                "date": "2024-08-20",
                                "time": "17:00",
                                "team1": "AFC Home",
                                "team2": "FC Away",
                                "score": {"ft": [2, 1]},
                            }
                        ]
                    }
                )
            )
            inputs = {
                "revision": "c" * 40,
                "expected": {"2024-25": football.blob_sha(path.read_bytes())},
            }

            def observed_score(adapter, data_dir):
                result = adapter.observe(data_dir, history, **inputs)
                rows = football.read_json_rows(Path(result["evidence_path"]))
                return [row["score"] for row in rows if row.get("score") is not None]

            with patch.object(model_study, "PACKAGE_ROOT", package):
                state = root / "state"
                first = pipeline.register_recipe(state, "B1", "B1")
                self.assertEqual(observed_score(datasets, state), [[2, 1]])
                path = package / "datasets.py"
                original = path.read_text()
                before = "score = [fixture.home_goals, fixture.away_goals]"
                self.assertEqual(original.count(before), 1)
                path.write_text(
                    original.replace(
                        before, "score = [fixture.away_goals, fixture.home_goals]"
                    )
                )
                spec = importlib.util.spec_from_file_location(
                    "polymarket861.mutated_observer", path
                )
                mutated = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mutated)
                self.assertEqual(observed_score(mutated, root / "other"), [[1, 2]])
                self.assertNotEqual(
                    first["recipe_digest"],
                    store.digest(
                        pipeline._build_recipe("B1", first["recipe"]["lines"])
                    ),
                )
                with self.assertRaisesRegex(ValueError, "different recipe"):
                    pipeline.register_recipe(state, "B1", "B1")
                self.assertFalse(season_readiness.release(state)["ready"])

    def test_result_writers_and_source_activation_clock_but_not_diagnostics_change_recipe(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            shutil.copytree(
                model_study.PACKAGE_ROOT,
                package,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            with patch.object(model_study, "PACKAGE_ROOT", package):
                primary = pipeline.register_recipe(root, "B1", "B1")["recipe"]
                contextual = pipeline.register_recipe(root, "B2", "B2")["recipe"]
                for name, before, after, affects in (
                    (
                        "datasets.py",
                        "SELECT MIN(observed_at)",
                        "SELECT MAX(observed_at)",
                        True,
                    ),
                    (
                        "store.py",
                        "if not valid_score((item",
                        "if valid_score((item",
                        True,
                    ),
                    (
                        "result_admission.py",
                        "return store.record_result_db(",
                        "return None and store.record_result_db(",
                        True,
                    ),
                    (
                        "market_http.py",
                        "received = store.now_utc()",
                        "received = requested",
                        True,
                    ),
                    (
                        "jobs_research.py",
                        "AND e.at<=? AND e.action",
                        "AND e.at>=? AND e.action",
                        True,
                    ),
                    (
                        "datasets.py",
                        '"experiment_reviews": reviews',
                        '"status_of_reviews": reviews',
                        False,
                    ),
                    (
                        "pipeline.py",
                        "须再核实同一代币双时点价量及账户风险",
                        "测试展示文案",
                        False,
                    ),
                ):
                    path = package / name
                    original = path.read_text()
                    self.assertEqual(original.count(before), 1)
                    path.write_text(original.replace(before, after))
                    try:
                        with self.subTest(path=name, change=before):
                            for recipe in (primary, contextual):
                                rebuilt = pipeline._build_recipe(
                                    recipe["family"], recipe["lines"]
                                )
                                self.assertEqual(recipe != rebuilt, affects)
                                if affects:
                                    with self.assertRaisesRegex(
                                        ValueError, "different recipe"
                                    ):
                                        pipeline.register_recipe(
                                            root, recipe["family"], recipe["family"]
                                        )
                                else:
                                    self.assertEqual(
                                        pipeline.register_recipe(
                                            root, recipe["family"], recipe["family"]
                                        )["recipe"],
                                        recipe,
                                    )
                            self.assertEqual(
                                season_readiness.release(root)["ready"], not affects
                            )
                    finally:
                        path.write_text(original)

    def test_formal_release_uses_the_reference_python(self):
        with patch.object(
            model_study.platform, "python_version_tuple", return_value=("3", "11", "18")
        ):
            with self.assertRaisesRegex(ValueError, "CPython 3.12"):
                model_study.release_report("unused", "unused", benchmark_only=True)
