"""A reviewed cross-file exception is limited to its caller and primary family."""

import runpy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket861 import football, model_study, pipeline, recipe_identity, store


CHECK = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/check_recipe_references.py")
)


class RecipeReferences(unittest.TestCase):
    def test_same_caller_and_callee_still_detect_changed_reviewed_implementations(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            recipe_identity, "_PRIMARY", {"entry.py": ("decide",)}
        ):
            root = Path(temp)
            (root / "entry.py").write_text("from . import schedule\ndef decide(): return schedule.refresh()\n")
            schedule = root / "schedule.py"
            source = "def refresh(): return prepare()\ndef prepare(): return 1\n"
            schedule.write_text(source)
            original = CHECK["Package"](root)
            pins = {name: original.binding_digest(name) for name in ("schedule.refresh", "schedule.prepare")}
            policy = {
                "schedule.refresh": {
                    "callers": ["entry.decide"],
                    "reason": "Reopens the reviewed schedule snapshot.",
                    "reviewed_bindings": pins,
                }
            }
            self.assertEqual(CHECK["audit"](original, policy)["changed_reviewed_bindings"], {})

            for name, change in (
                ("schedule.refresh", source.replace("return prepare()", "return 1")),
                ("schedule.prepare", source.replace("return 1", "return 0")),
                ("schedule.prepare", source.split("def prepare")[0]),
            ):
                with self.subTest(name=name, change=change):
                    schedule.write_text(change)
                    result = CHECK["audit"](CHECK["Package"](root), policy)
                    self.assertEqual(result["unreviewed"], [])
                    self.assertEqual(result["obsolete"], [])
                    self.assertEqual(list(result["changed_reviewed_bindings"]), [name])
                    self.assertEqual(result["changed_reviewed_bindings"][name]["expected"], pins[name])

    def test_new_caller_alias_reexport_and_context_are_checked(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(
                recipe_identity,
                "_PRIMARY",
                {
                    "entry.py": ("decide",),
                    "bridge.py": ("weight",),
                    "football_model.py": ("signed",),
                },
            ),
        ):
            root = Path(temp)
            (root / "football_model.py").write_text(
                "def signed(): return 1\ndef apply_context(): return 2\n"
            )
            (root / "bridge.py").write_text(
                "from .football_model import apply_context as weight\n"
            )
            entry = root / "entry.py"
            entry.write_text(
                "from .bridge import weight as replay\n"
                "def decide():\n    from polymarket861 import football_model as fm\n"
                "    return fm.signed() + replay()\n"
            )
            exception = {
                "football_model.apply_context": {
                    "reason": "Only the contextual primary family uses this arm.",
                    "families": ["B0", "B1", "B3"],
                    "callers": ["entry.decide"],
                }
            }

            def audited():
                return CHECK["audit"](CHECK["Package"](root), exception)

            self.assertEqual(audited()["unreviewed"], [])
            self.assertEqual(audited()["obsolete"], [])
            entry.write_text(
                entry.read_text().replace(
                    "return fm.signed()", "return other() + fm.signed()"
                )
                + "def other():\n    import polymarket861.football_model as engine\n"
                "    return getattr(engine, 'apply_context')()\n"
            )
            actual = audited()["unreviewed"]
            self.assertEqual(
                actual,
                [
                    (f, "entry.other", "football_model.apply_context")
                    for f in ("B0", "B1", "B3")
                ],
            )
            entry.write_text(entry.read_text().replace("'apply_context'", "field_name"))
            self.assertIn(
                ("B1", "entry.other", "football_model.<dynamic>"),
                audited()["unreviewed"],
            )

    def test_input_clock_pairing_and_source_pin_are_in_active_recipe(self):
        edits = (
            (
                "ledger_rules.py",
                "        return dt.astimezone(UTC)\n",
                "        return dt\n",
            ),
            (
                "jobs.py",
                "    active_version,\n",
                "    model_for_decision as active_version,\n",
            ),
            ("result_pairings.py", '        proof["actual_kickoff"],\n', ""),
            (
                "football.py",
                f'SOURCE_COMMIT = "{football.SOURCE_COMMIT}"',
                'SOURCE_COMMIT = "test-publisher-pin"',
            ),
            (
                "store.py",
                "builtin_model_releases WHERE effective_at<=?",
                "builtin_model_releases WHERE effective_at>=?",
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "polymarket861"
            shutil.copytree(
                model_study.PACKAGE_ROOT,
                package,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            with patch.object(model_study, "PACKAGE_ROOT", package):

                def primary_id():
                    return store.digest(
                        pipeline._build_recipe("B1", football.DEFAULT_LINES)
                    )

                before = primary_id()
                for filename, old, new in edits:
                    with self.subTest(filename=filename):
                        path = package / filename
                        source = path.read_text()
                        self.assertEqual(source.count(old), 1)
                        path.write_text(source.replace(old, new))
                        self.assertNotEqual(primary_id(), before)
                        path.write_text(source)
                        self.assertEqual(primary_id(), before)

    def test_nested_and_conditional_imports_cannot_hide_a_package_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("early", "late", "nested", "default"):
                (root / f"{name}.py").write_text("def helper(): return 1\n")
            (root / "entry.py").write_text(
                "from . import default as settings\n"
                "def decide(flag, settings=settings.helper()):\n"
                "    if flag:\n        from . import early as source\n"
                "    else:\n        from . import late as source\n"
                "    def child():\n        from . import nested as source\n"
                "        return source.helper()\n"
                "    return source.helper() + child()\n"
            )
            package = CHECK["Package"](root)
            definitions, _ = package.module("entry")
            self.assertEqual(
                package.references("entry", definitions["decide"]),
                {"early.helper", "late.helper", "nested.helper", "default.helper"},
            )

    def test_indirect_module_loading_and_unsigned_top_level_import_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "source.py").write_text("def feature(): return 1\n")
            (root / "entry.py").write_text(
                "from importlib import import_module as load\n"
                "if optional:\n    from .source import feature as feature_at\n"
                "def decide(): return load('.source', __package__).feature() + feature_at()\n"
            )
            package = CHECK["Package"](root)
            definitions, _ = package.module("entry")
            self.assertEqual(
                package.references("entry", definitions["decide"]),
                {"polymarket861.<dynamic>", "entry.<dynamic>"},
            )
