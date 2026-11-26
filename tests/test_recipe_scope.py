"""The approval protects a released model, its inputs and its actual retained installation."""

import json
import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from polymarket861 import model_study, pipeline, season_readiness, store


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

    def test_formal_release_uses_the_reference_python(self):
        with patch.object(
            model_study.platform, "python_version_tuple", return_value=("3", "11", "18")
        ):
            with self.assertRaisesRegex(ValueError, "CPython 3.12"):
                model_study.release_report("unused", "unused", benchmark_only=True)
