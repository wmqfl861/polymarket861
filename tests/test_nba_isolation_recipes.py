import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket861 import model_study

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("check_recipe_identity", SCRIPTS / "check_recipe_identity.py")
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)


class FootballReleaseIsolation(unittest.TestCase):
    def test_shared_transport_change_fails_pinned_eight_recipe_check(self):
        baseline = json.loads((SCRIPTS / "football-recipe-baseline.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "package"
            shutil.copytree(model_study.PACKAGE_ROOT, package, ignore=shutil.ignore_patterns("__pycache__"))
            path = package / "market_http.py"
            source = path.read_text()
            self.assertEqual(source.count("def read(method, url, headers, content, timeout):"), 1)
            path.write_text(source.replace(
                "def read(method, url, headers, content, timeout):",
                "def read(method, url, headers, content, timeout):\n    timeout = min(timeout, 5)",
            ))
            with patch.object(model_study, "PACKAGE_ROOT", package):
                result = identity.capture()
            with self.assertRaises(ValueError) as failure:
                identity.verify_baseline(result["recipes"], baseline)
            for family in ("B0", "B1", "B2", "B3"):
                for kind in ("primary", "paired"):
                    self.assertIn(f"{family}/{kind}", str(failure.exception))
