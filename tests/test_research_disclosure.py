"""Canaries for the boundary between the private evidence store and the research model."""

import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from polymarket861 import (
    evidence,
    jobs,
    maintenance,
    research_workspace as rw,
    round_history,
    store,
)

CANARY = "WITHHELD_LABEL_73x19_PRIVATE_NEWS"
FEATURES = {"skip_host_skill_discovery", *rw._DISABLED}


def controlled_cli(folder, item, *, probe_ok=True):
    """A real subprocess records its invocation, not a substitute for the actual OS sandbox."""
    script = folder / "fixture-cli"
    script.write_text(
        "#!/usr/bin/env python3\n"
        + "import json,os,pathlib,sys\n"
        + "a=sys.argv[1:]\n"
        + "if a==['--version']: print('codex-cli 0.230.1');sys.exit(0)\n"
        + "if a==['exec','--help']: print('--ignore-user-config --ignore-rules --strict-config --output-schema --ephemeral');sys.exit(0)\n"
        + "if a==['sandbox','--help']: print('--permission-profile --include-managed-config');sys.exit(0)\n"
        + f"if a==['features','list']: print({chr(10).join(sorted(FEATURES))!r});sys.exit(0)\n"
        + "assert ':root' in str(a) and ':minimal' in str(a) and '.network.enabled=false' in str(a)\n"
        + "if a[0]=='sandbox':\n"
        + " assert a[a.index('-P')+1]=='pm861-research' and '--include-managed-config' in a\n"
        + " assert pathlib.Path(a[a.index('-C')+1]).resolve()==pathlib.Path.cwd()\n"
        + " assert 'socket.create_connection' in str(a) and 'runtime.sqlite3' in str(a)\n"
        + f" print('PM861_SCOPE_OK' if {probe_ok!r} else 'leaked');sys.exit(0)\n"
        + "assert a[:3]==['-a','never','exec'] and '--sandbox' not in a\n"
        + "assert all(s in a for s in ['--ignore-user-config','--ignore-rules','--strict-config','--ephemeral','--disable','plugins','computer_use','shell_tool','multi_agent','mcp_servers={}','web_search=\"disabled\"'])\n"
        + "assert pathlib.Path.cwd()==pathlib.Path(a[a.index('--cd')+1])\n"
        + "assert sorted(p.name for p in pathlib.Path.cwd().iterdir())==['marker','proposal-schema.json']\n"
        + "payload=json.loads(sys.stdin.read().splitlines()[-1]);assert isinstance(payload['code_excerpts'],dict) and payload['code_excerpts']\n"
        + f"assert {CANARY!r} not in json.dumps(payload)\n"
        + "assert not os.environ.get('GH_TOKEN'); print(json.dumps({'cwd':os.getcwd(),'topic':payload['task']['topic'],'sample_refs':list(payload['manifest']['sample_refs'])}))\n"
        + f"pathlib.Path(a[a.index('--output-last-message')+1]).write_text({json.dumps(item)!r})\n"
    )
    script.chmod(0o700)
    return script


def note():
    return {
        "kind": "note",
        "hypothesis": "Review this coverage gap",
        "evidence_refs": ["input"],
        "parameters_json": "{}",
    }


class ResearchDisclosureTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.state = self.root / "private"
        self.count = 0

    def prepare(self, topic="discovery", inputs=None):
        self.count += 1
        work = self.root / f"wave-{self.count}"
        work.mkdir()
        original = {
            "task": CANARY,
            "inputs": {
                "topic": topic,
                "free_text": CANARY,
                "nested": {"score": [73, 19]},
                **(inputs or {}),
            },
            "dependencies": {CANARY: {"goals": [73, 19], "private": CANARY}},
        }
        (work / "input.json").write_text(json.dumps(original))
        manifest = rw.prepare(work, self.state, None)
        prompt = rw._prompt(work, manifest)
        self.assertNotIn(CANARY, prompt)
        self.assertNotIn(CANARY, (work / "input.json").read_text())
        self.assertNotIn(CANARY, (work / "reference.json").read_text())
        return json.loads(prompt.splitlines()[-1]), work

    @staticmethod
    def close_report(rnd, complete=True):
        count = 10 if complete else 1
        return {
            "season": "2025-26",
            "round_id": rnd,
            "mode": "formal",
            "horizon": "T-60m",
            "model_version": "B1",
            "protocol": store.PROTOCOL,
            "expected": 10,
            "scheduled": 10,
            "count": count,
            "completed": count,
            "pending": 10 - count,
            "missing": 0,
            "schedule_missing": 0,
            "status": "final" if complete else "provisional",
            "fixtures": [
                {
                    "fixture_id": f"{CANARY}/{i}",
                    "status": "scored" if i < count else "pending",
                    "goals": [73, 19],
                    "result_locator": CANARY,
                }
                for i in range(10)
            ],
            "score": 83.12348,
            "comparisons": {"pairs": {CANARY: {"margin": 987}}},
            "private_text": CANARY,
        }

    def edition(self, body, origin="published", digest=None):
        with closing(store.connect(self.state)) as db, db:
            db.execute(round_history._SCHEMA)
            key = f"2025-26/{body['round_id']}/T-60m/formal/B1"
            edition = db.execute(
                "SELECT coalesce(max(edition),0)+1 FROM round_report_editions WHERE round_key=?",
                (key,),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO round_report_editions VALUES(?,?,?,?,?,?)",
                (
                    key,
                    edition,
                    store.now_utc(),
                    origin,
                    digest or store.digest(body),
                    store.canonical(body),
                ),
            )

    def start_blind(self):
        jobs.register_candidate(
            self.state,
            "no-peeking",
            kind="model",
            version="B0",
            baseline="B1",
            window="2090-91/1",
            hypothesis="Independent probability test",
        )

    def test_partial_superseded_legacy_generic_and_blind_full_editions(self):
        self.edition(self.close_report(1, False))
        self.edition(self.close_report(2))
        self.edition(self.close_report(3), origin="legacy_latest")
        self.edition(self.close_report(4), digest="forged")
        self.edition(self.close_report(5))
        self.edition(self.close_report(5, False))
        for kind in ("round", "source", "incident", "trial-market-study"):
            store.save_report(self.state, kind, CANARY, self.close_report(6))
        visible, _ = self.prepare()
        samples = list(visible["released_samples"].values())
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual((sample["round"], sample["mean_score"]), (2, 83.123))
        self.assertEqual(
            set(sample),
            {
                "kind",
                "season",
                "round",
                "mode",
                "horizon",
                "scored",
                "expected",
                "mean_score",
                "source_ref",
            },
        )
        with closing(store.connect(self.state)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT kind FROM research_disclosure_refs WHERE ref=?",
                    (sample["source_ref"],),
                ).fetchone()[0],
                "round_edition",
            )
        self.start_blind()
        for topic in sorted(rw.TOPICS):
            with self.subTest(topic=topic):
                masked, _ = self.prepare(topic)
                self.assertEqual(masked["task"]["outcome_disclosure"], "withheld")
                self.assertEqual(masked["released_samples"], {})
                self.assertEqual(list(masked["manifest"]["sample_refs"]), ["input"])
                self.assertNotIn("mean_score", json.dumps(masked["task"]))

    def test_queue_laundering_and_generic_alias_share_the_same_profile(self):
        self.start_blind()
        dep = CANARY + "-prerequisite"
        jobs.enqueue_job(
            self.state,
            dep,
            task=CANARY,
            inputs={"required_fields": [CANARY], "records": [{}]},
        )
        jobs.enqueue_job(
            self.state,
            "plain",
            kind="codex",
            task=CANARY,
            inputs={"nested": {"status": CANARY}},
            dependencies=[dep],
        )
        jobs.enqueue_job(self.state, "free-form", kind="codex", task=CANARY)
        for name in ("incident", "source", "trial-market-study"):
            store.save_report(
                self.state,
                name,
                CANARY,
                {"status": "open", "observed_issues": {"weather": {"label": CANARY}}},
            )
        maintenance.tick(self.state)
        with patch.object(
            jobs.shutil, "which", return_value=str(controlled_cli(self.root, note()))
        ):
            report = jobs.run_workers(self.state, allow_codex=True, timeout=10)
        self.assertTrue(
            all(a["outcome"] == "done" for a in report["attempts"]), report["attempts"]
        )
        for job in jobs.jobs_status(self.state)["jobs"]:
            if job["kind"] == "codex":
                result = job["result"]
                path = Path(result["output_dir"])
                self.assertEqual(result["research_isolation"]["os_probe"], "passed")
                self.assertNotIn(CANARY, (path / "input.json").read_text())
                self.assertNotIn(CANARY, (path / "codex.jsonl").read_text())
                self.assertNotEqual(
                    Path(json.loads((path / "codex.jsonl").read_text())["cwd"]), path
                )
                self.assertEqual(rw.verify(self.state, job["key"])["state"], "note")
                if job["key"].startswith("research-"):
                    with closing(jobs._open(self.state)) as db:
                        self.assertNotIn(
                            CANARY, json.dumps(jobs._read_job(db, job["key"])["inputs"])
                        )

    def test_failed_enforcement_never_starts_inference(self):
        jobs.enqueue_job(
            self.state,
            "locked",
            kind="codex",
            task=CANARY,
            inputs={"topic": "source"},
            max_attempts=1,
        )
        with patch.object(
            jobs.shutil,
            "which",
            return_value=str(controlled_cli(self.root, note(), probe_ok=False)),
        ):
            report = jobs.run_workers(self.state, allow_codex=True)
        attempt = report["attempts"][0]
        self.assertEqual(attempt["outcome"], "failed")
        self.assertTrue(attempt["published"])
        self.assertIn("isolation probe failed", attempt["error"])
        self.assertFalse(list(self.state.rglob("proposal.json")))
        self.assertEqual(next(self.state.rglob("codex.jsonl")).read_text(), "")

    def test_operational_signals_keep_fields_grants_faults_without_source_text_or_names(
        self,
    ):
        from test_evidence import starters, time_at

        self.start_blind()
        raw_id = "feed-" + CANARY
        for ident in (raw_id, "gdelt-doc"):
            evidence.register_source(
                self.state,
                {
                    "source_id": ident,
                    "name": CANARY,
                    "free": True,
                    "status": "candidate",
                    "upstream": ident,
                    "terms_locator": "https://example.org/terms",
                    "effective_at": time_at(0),
                    "fields": {"lineup": {"uses": ["trial"], "methods": ["manual"]}},
                },
            )
            evidence.observe_source(
                self.state,
                {
                    "source_id": ident,
                    "field": "lineup",
                    "checked_at": time_at(7),
                    "outcome": "error",
                    "private_label": CANARY,
                },
            )
        stored = evidence.import_evidence(
            self.state,
            [
                {
                    "source_id": raw_id,
                    "fixture_id": CANARY,
                    "team": "Home",
                    "field": "lineup",
                    "method": "manual",
                    "published_at": time_at(9),
                    "received_at": time_at(9, 2),
                    "locator": "https://example.org/team/" + CANARY,
                    "value": {"confirmed": False, "starters": starters("H")},
                }
            ],
        )
        evidence.open_incident(
            self.state,
            {
                "incident_id": CANARY,
                "symptom": CANARY,
                "affected_evidence_ids": stored["evidence_ids"],
                "opened_at": time_at(10),
            },
        )
        maintenance.tick(self.state)
        with closing(jobs._open(self.state)) as db:
            rows = [
                jobs._read_job(db, r[0])
                for r in db.execute("SELECT job_key FROM research_jobs")
            ]
        raw_source_job = next(
            j
            for j in rows
            if j["inputs"]["topic"] == "source" and self._subject(j) == raw_id
        )
        src, _ = self.prepare("source", raw_source_job["inputs"])
        health = src["task"]["collection_health"]["field_health"]
        self.assertEqual(len(health), 1)
        self.assertRegex(health[0]["adapter"], r"^src-[a-f0-9]{40}$")
        self.assertEqual(health[0]["last_collection_status"], "error")
        self.assertEqual(health[0]["registered_uses"], ["trial"])
        self.assertEqual(health[0]["error_or_invalid_checks"], "one")
        discover, _ = self.prepare("discovery")
        whole = discover["task"]["collection_health"]
        self.assertIn("gdelt-doc", {r["adapter"] for r in whole["field_health"]})
        self.assertIn("injury", whole["fields_without_registered_source_in_sample"])
        incident = next(j for j in rows if j["inputs"]["topic"] == "incident")
        redacted, _ = self.prepare("incident", incident["inputs"])
        self.assertEqual(
            redacted["task"]["collection_health"]["quarantined_evidence_by_field"],
            {"lineup": "one"},
        )

    def _subject(self, job):
        with closing(store.connect(self.state)) as db:
            return db.execute(
                "SELECT subject FROM research_maintenance_subjects WHERE ref=?",
                (job["inputs"]["source_ref"],),
            ).fetchone()[0]

    def test_endpoint_opt_in_projects_only_transport_and_never_auth_in_argv(self):
        config = self.root / "cli"
        config.mkdir()
        (config / "config.toml").write_text(
            'model="example"\nmodel_provider="own"\n[model_providers.own]\nname="Own"\n'
            'base_url="https://example.org/v1"\nwire_api="responses"\nenv_key="PM861_TEST_API_KEY"\n'
            '[mcp_servers.unsafe]\ncommand="' + CANARY + '"\n'
        )
        env = {"HOME": str(self.root), "CODEX_HOME": str(config)}
        with patch.dict(os.environ, {"PM861_TEST_API_KEY": CANARY}):
            opts = rw._provider({"use_codex_config": True}, env)
        self.assertIn('model_providers.own.base_url="https://example.org/v1"', opts)
        self.assertNotIn(CANARY, str(opts))
        self.assertNotIn("mcp_servers", str(opts))
        self.assertEqual(env["PM861_TEST_API_KEY"], CANARY)
        self.assertEqual(
            (env["HOME"], env["CODEX_HOME"]), (str(self.root), str(config))
        )

    def test_real_round_publisher_closed_and_revealed_only(self):
        from polymarket861 import strategy

        kickoff = "2025-08-17T12:00Z"
        settled = "2025-08-17T15:00Z"
        for i in range(10):
            fixture = f"round-publish-{i}"
            store.register_fixture(
                self.state,
                dict(
                    fixture_id=fixture,
                    season="2025-26",
                    round_id=7,
                    home=f"H{i}",
                    away=f"A{i}",
                    kickoff=kickoff,
                    known_at="2025-08-01T00:00Z",
                    source="official",
                    locator=CANARY,
                ),
            )
            store.record_result(
                self.state,
                dict(
                    fixture_id=fixture,
                    home_goals=73,
                    away_goals=19,
                    source="official",
                    locator=CANARY,
                    finalized_at=settled,
                    published_at=settled,
                    received_at=settled,
                ),
            )
        report = round_history.publish_round(self.state, "2025-26", 7)
        self.assertEqual((report["completed"], report["pending"]), (10, 0))
        visible, work = self.prepare("round")
        sample = next(iter(visible["released_samples"].values()))
        self.assertIsNone(sample["mean_score"])
        origins = rw._verify_round_origins(self.state, work, visible["manifest"])
        self.assertEqual(
            next(iter(origins.values()))["sha256"], report["report_sha256"]
        )
        with closing(store.connect(self.state)) as db, db:
            db.executescript(strategy.SCHEMA)
            db.execute(
                "INSERT INTO selection_trials(key,start,spec,digest,registered) VALUES('sealed','2090-91/1','{}','proof','now')"
            )
        withheld, _ = self.prepare("round")
        self.assertEqual(withheld["released_samples"], {})
        (work / "input.json").chmod(0o600)
        (work / "input.json").write_text(
            json.dumps({"topic": "round", "replaced": CANARY})
        )
        with self.assertRaisesRegex(ValueError, "changed before inference"):
            rw._prompt(work, visible["manifest"])

    def test_private_home_can_contain_system_temp_and_provider_session_stays_unchanged(
        self,
    ):
        home = self.root / "user"
        tmp = home / "system-temp"
        tmp.mkdir(parents=True)
        cli = controlled_cli(self.root, note())
        jobs.enqueue_job(
            self.state, "nested-temp", kind="codex", task=CANARY, max_attempts=1
        )
        with (
            patch.object(tempfile, "tempdir", str(tmp)),
            patch.object(jobs.shutil, "which", return_value=str(cli)),
            patch.dict(os.environ, {"HOME": str(home)}),
        ):
            run = jobs.run_workers(self.state, allow_codex=True)
            self.assertEqual(os.environ["HOME"], str(home))
        self.assertEqual(run["attempts"][0]["outcome"], "done", run)
        folder = Path(jobs.jobs_status(self.state)["jobs"][0]["output"])
        self.assertTrue(
            Path(
                json.loads((folder / "codex.jsonl").read_text())["cwd"]
            ).is_relative_to(tmp)
        )
