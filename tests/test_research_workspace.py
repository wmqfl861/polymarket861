import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket861 import jobs, research_workspace as rw


class ResearchWorkspaceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        tracked = self.project / "src/polymarket861/football.py"
        tracked.parent.mkdir(parents=True)
        tracked.write_text("origin = 1\n")
        for args in (
            ["init", "-q"],
            ["add", "."],
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "base",
            ],
        ):
            subprocess.run(["git", "-C", str(self.project), *args], check=True)
        (self.project / "private.txt").write_text("not a research input")
        tracked.write_text("origin = 999\n")
        self.state = self.base / "research"

    def run_one(self, kind, params, refs=None):
        ident = f"proposal-{kind}"
        item = {
            "kind": kind,
            "hypothesis": "A feasible change to check",
            "parameters_json": json.dumps(params),
            "evidence_refs": ["input"] if refs is None else refs,
        }
        binary = self.base / "fixture-cli"
        binary.write_text(
            "#!/usr/bin/env python3\nimport pathlib,sys,json\n"
            "p=pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1])\n"
            "assert sys.argv[sys.argv.index('--sandbox')+1]=='read-only'\n"
            "assert pathlib.Path('reference/src/polymarket861/football.py').read_text()=='origin = 1\\n'\n"
            "assert not pathlib.Path('reference/private.txt').exists()\n"
            f"p.write_text({json.dumps(item)!r})\n"
        )
        binary.chmod(0o700)
        jobs.enqueue_job(
            self.state,
            ident,
            task="research",
            kind="codex",
            inputs={"topic": "round"},
            max_attempts=1,
        )
        with patch.object(jobs.shutil, "which", return_value=str(binary)):
            run = jobs.run_workers(self.state, allow_codex=True, project=self.project)
        return ident, run

    def test_immutable_source_and_winning_attempt_are_bound_to_note(self):
        key, run = self.run_one("note", {})
        self.assertEqual(run["proposals"], [{"job": key, "state": "note"}])
        row = rw.verify(self.state, key)
        self.assertTrue(row["code_revision"])
        job = jobs.jobs_status(self.state)["jobs"][0]
        folder = Path(job["output"])
        (folder / "proposal.json").write_text(
            (folder / "proposal.json").read_text().replace("feasible", "different")
        )
        with self.assertRaisesRegex(ValueError, "changed"):
            rw.verify(self.state, key)

    def test_only_known_evidence_and_reviewable_patch(self):
        diff = "diff --git a/src/polymarket861/football.py b/src/polymarket861/football.py\n--- a/src/polymarket861/football.py\n+++ b/src/polymarket861/football.py\n@@ -1 +1 @@\n-origin = 1\n+origin = 2\n"
        key, run = self.run_one("patch", {"diff": diff})
        self.assertEqual(run["proposals"][0]["state"], "awaiting_code_review")
        self.assertTrue(rw.verify(self.state, key)["applies_to_source"])
        self.assertEqual(
            (self.project / "src/polymarket861/football.py").read_text(),
            "origin = 999\n",
        )
        _, bad = self.run_one("note", {}, refs=["imaginary.json"])
        self.assertIn("reference supplied", str(bad["attempts"][0]["error"]))

    def test_source_proposals_do_not_self_grant_free_data_rights(self):
        params = dict(
            source_id="club-new",
            field="lineup",
            terms_url="https://example.org/terms",
            source_url="https://example.org/teams",
            baseline="old",
            reference_source="verified",
        )
        key, run = self.run_one("source", params)
        self.assertEqual(run["proposals"][0]["state"], "awaiting_source_evidence")
        self.assertEqual(
            rw.verify(self.state, key)["missing_source_or_trial_grant"], "club-new"
        )
        self.assertEqual(
            jobs.active_version(self.state, "source", "lineup", default="old"), "old"
        )

    def test_model_proposal_waits_for_fixed_holdout_before_registration(self):
        key, run = self.run_one("model", {"family": "B0"})
        self.assertEqual(run["proposals"][0]["state"], "awaiting_history")
        malformed = self.base / "bad/2025-26/en.1.json"
        malformed.parent.mkdir(parents=True)
        malformed.write_text('{"matches":[]}')
        with self.assertRaisesRegex(ValueError, "fixed OpenFootball"):
            rw.verify(self.state, key, history=malformed.parents[1], season="2025-26")
        self.assertEqual(
            jobs.active_version(self.state, "model", "epl", default="B1"), "B1"
        )

    def test_verified_field_sample_registers_a_shadow_candidate(self):
        from polymarket861 import evidence
        from test_evidence import starters, time_at

        params = dict(
            source_id="club-new",
            field="lineup",
            terms_url="https://example.org/terms",
            source_url="https://example.org/teams",
            baseline="old",
            reference_source="verified",
        )
        key, _ = self.run_one("source", params)
        for ident in ("club-new", "old", "verified"):
            evidence.register_source(
                self.state,
                {
                    "source_id": ident,
                    "name": ident,
                    "free": True,
                    "status": "candidate",
                    "upstream": ident,
                    "terms_locator": params["terms_url"],
                    "effective_at": time_at(0),
                    "fields": {"lineup": {"uses": ["trial"], "methods": ["manual"]}},
                },
            )
        evidence.import_evidence(
            self.state,
            [
                {
                    "source_id": "club-new",
                    "fixture_id": "F",
                    "team": "Home",
                    "field": "lineup",
                    "method": "manual",
                    "published_at": time_at(9),
                    "received_at": time_at(9, 2),
                    "locator": "https://example.org/teams/home",
                    "value": {"confirmed": False, "starters": starters("H")},
                }
            ],
        )
        candidate = rw.verify(self.state, key)
        self.assertEqual(candidate["state"], "registered_for_trial")
        self.assertEqual(
            rw.verify(self.state, key)["spec_digest"], candidate["spec_digest"]
        )
        self.assertEqual(
            jobs.active_version(self.state, "source", "lineup", default="old"), "old"
        )
