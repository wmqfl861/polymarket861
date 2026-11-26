import runpy
import tempfile
import unittest
from pathlib import Path


class LeagueBoundaryTests(unittest.TestCase):
    def test_new_or_duplicate_season_assumption_requires_review(self):
        script = (
            Path(__file__).resolve().parents[1] / "scripts/check_league_boundaries.py"
        )
        tools = runpy.run_path(str(script))
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            module = package / "schedule.py"
            module.write_text("matches = 380\n")
            approved = tools["scan"](package)
            module.write_text("matches = 380\nmatches = 380\n")
            (package / "second.py").write_text("tz = 'Europe/Berlin'\n")
            self.assertCountEqual(
                tools["additions"](tools["scan"](package), approved),
                [
                    ("schedule.py", "matches = 380", 1),
                    ("second.py", "tz = 'Europe/Berlin'", 1),
                ],
            )
