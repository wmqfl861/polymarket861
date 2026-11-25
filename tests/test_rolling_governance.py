import json
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket861 import forward_window as window
from polymarket861 import jobs_research as research
from polymarket861 import ledger, model_study, pipeline, round_history, store, strategy
from release_fixture import compared
from test_recipe_upgrade import old_build


class RollingGovernance(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.stamp = "2031-05-01T12:00Z"

    def round(self, season, number, kickoff, *, at=None):
        with patch.object(store, "now_utc", return_value=store.iso(at or self.stamp)):
            for i in range(10):
                store.register_fixture(
                    self.root,
                    {
                        "fixture_id": f"{season}-{number}-{i}",
                        "home": f"H{i}",
                        "away": f"A{i}",
                        "season": season,
                        "round_id": number,
                        "kickoff": kickoff,
                        "source": "league",
                        "locator": f"{season}-{number}",
                        "known_at": self.stamp,
                    },
                )

    def register(self, *, track=window.RESEARCH, key="challenger", at=None):
        with patch.object(store, "now_utc", return_value=store.iso(at or self.stamp)):
            pipeline.register_recipe(self.root, key, "B2")
            return research.register_candidate(
                self.root,
                key,
                kind="model",
                version=key,
                baseline="B1",
                window="2030-31/38",
                hypothesis="preregistered",
                manifest={"evidence_track": track},
            )

    def test_cross_season_registration_new_schedule_receipts_and_fixed_denominator(
        self,
    ):
        self.round("2030-31", 38, "2031-05-19T15:00Z")
        c = self.register()
        plan = c["manifest"]["forward_window"]
        self.assertEqual(
            (plan["rounds"][0], plan["rounds"][-1], plan["expected_fixtures"]),
            (["2030-31", 38], ["2031-32", 37], 380),
        )
        self.assertEqual(c["digest"], self.register()["digest"])
        self.assertEqual(len(plan["scheduled_at_registration"]), 10)
        with self.assertRaisesRegex(ValueError, "deadline"):
            self.register(key="late", at="2031-05-19T14:05Z")
        with patch.object(
            store, "now_utc", return_value=store.iso("2031-06-01T12:00Z")
        ):
            with self.assertRaisesRegex(ValueError, "overlapping"):
                research.register_candidate(
                    self.root,
                    "peer",
                    kind="model",
                    version="B0",
                    baseline="B1",
                    window="2031-32/1",
                    hypothesis="overlapping",
                )
        self.round("2031-32", 1, "2031-08-12T15:00Z", at="2031-06-01T12:00Z")
        games = store.eligible_fixtures(self.root)
        self.assertIsNone(window.audit(self.root, plan, games))
        self.assertEqual(len(games), 20)
        self.assertEqual(
            research._closed(
                games, 38, 38, store.instant("2032-05-30T00:00Z"), "2030-31"
            ),
            "incomplete_official_schedule",
        )
        self.assertEqual(
            window.audit(self.root, plan, games[1:]),
            "previously_registered_fixture_removed_or_reassigned",
        )
        self.round("2031-32", 2, "2031-08-19T15:00Z", at="2031-08-20T12:00Z")
        self.assertEqual(
            window.audit(self.root, plan, store.eligible_fixtures(self.root)),
            "missing_pregame_schedule_receipt",
        )
        with self.assertRaisesRegex(ValueError, "bind"):
            ledger._registered_candidate(self.root, c["version"], c["key"])
        self.assertFalse(window.complete(games[:10] + games[:1], {("2030-31", 38)}))

    def test_research_signoff_does_not_change_financial_or_model_pointer(self):
        self.round("2030-31", 38, "2031-05-19T15:00Z")
        c = self.register()
        summary = {
            "paired_count": 380,
            "denominator": 380,
            "rounds": 38,
            "baseline_count": 380,
            "candidate_count": 380,
            "comparison_rejections": {},
            "seasons": research._history_seasons(c),
            "intervals": {
                k: {"lower": 1.0, "difference": 2.0}
                for k in ("overall", *research.ITEM_KEYS)
            },
        }
        with (
            patch.object(model_study, "trial_history", return_value=summary),
            patch.object(research, "_paired_evidence", return_value=summary) as forward,
            patch.object(
                store,
                "require_formal",
                side_effect=AssertionError("research cannot use financial evidence"),
            ),
            patch.object(research, "_paper_audit", side_effect=AssertionError),
        ):
            outcome = research._model_assessment(
                self.root,
                c,
                store.eligible_fixtures(self.root),
                [],
                1,
                store.instant("2032-06-01T00:00Z"),
            )
            self.assertTrue(outcome["qualified"])
            self.assertFalse(outcome["formal_activation_allowed"])
            self.assertEqual(forward.call_args.args[2], "prospective")
        with patch.object(research, "_assess", return_value=outcome):
            self.assertEqual(
                research.evaluate_candidate(self.root, c["key"])["state"], "recommended"
            )
            call = lambda: research.approve_candidate(
                self.root,
                c["key"],
                reviewer="analyst",
                reason="looked at the retained study",
            )
            self.assertEqual(call()["state"], "research_reviewed")
            self.assertEqual(call()["state"], "research_reviewed")
            self.assertEqual(research.active_version(self.root, default="B1"), "B1")
        strict = dict(c, manifest=dict(c["manifest"], evidence_track=window.FORMAL))
        with self.assertRaisesRegex(ValueError, "separately registered"):
            model_study.trial_history(self.root, strict)

    def test_legacy_release_hashes_research_evidence_and_effective_approval(self):
        with old_build():
            pipeline.register_recipe(self.root, "B1", "B1")
        source = pipeline._build_recipe("B1", (0.5, 1.5, 2.5, 3.5))["algorithm_source"]
        self.assertTrue(
            {"pipeline.py", "football.py", "evidence.py", "input_manifest.py"}.issubset(
                source
            )
        )
        with compared(self.root) as report:
            self.assertEqual(pipeline.prepare_builtin(self.root), "B1")
            with self.assertRaisesRegex(ValueError, "named reviewer"):
                model_study.approve_release(
                    self.root, report["report_sha256"], "", "reviewed"
                )
            with self.assertRaisesRegex(ValueError, "verified local"):
                model_study.approve_release(self.root, "0" * 64, "operator", "reviewed")
            approved = model_study.approve_release(
                self.root, report["report_sha256"], "operator", "reviewed"
            )
            self.assertEqual(pipeline.prepare_builtin(self.root), approved["version"])
            with closing(store.connect(self.root)) as db:
                self.assertEqual(
                    store.builtin_model_at(
                        db,
                        store.instant(approved["effective_at"])
                        - timedelta(microseconds=1),
                    ),
                    "B1",
                )
            self.assertEqual(
                model_study.approve_release(
                    self.root, report["report_sha256"], "operator", "reviewed"
                )["version"],
                approved["version"],
            )

    def test_round_marks_the_first_actual_main_forecast_after_release(self):
        with old_build():
            pipeline.register_recipe(self.root, "B1", "B1")
        with compared(self.root) as report:
            approved = model_study.approve_release(
                self.root, report["report_sha256"], "match analyst", "verified"
            )
        stamp = store.instant(approved["effective_at"])
        fixtures = []
        with closing(store.connect(self.root)) as db, db:
            for i, version in enumerate(("B1", approved["version"])):
                cut = store.iso(stamp + timedelta(minutes=i * 2 - 1))
                kick = store.iso(store.instant(cut) + timedelta(hours=1))
                fid = f"fixture-{i}"
                fixtures.append({"fixture_id": fid, "kickoff": kick})
                db.execute(
                    "INSERT INTO forecast_seals(fixture_id,kickoff,horizon,model_version,protocol,mode,cutoff,sealed_at,data_json,digest) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        fid,
                        kick,
                        "T-60m",
                        version,
                        store.PROTOCOL,
                        "technical",
                        cut,
                        cut,
                        store.canonical({"purpose": "main"}),
                        f"sample-{i}",
                    ),
                )
            rollout = round_history._rollout(db, fixtures, "T-60m", "technical")
        self.assertEqual(
            [r["version"] for r in rollout["versions"]], ["B1", approved["version"]]
        )
        switch = rollout["switches"][0]
        self.assertEqual(
            (switch["first_fixture"], switch["release_decision"]["comparison"]),
            ("fixture-1", report["report_sha256"]),
        )

    def test_selector_detects_changed_implementation_without_rewriting_registration(
        self,
    ):
        spec = {"implementation": strategy.implementation_digest()}
        with strategy.database(self.root, True) as db:
            db.execute(
                "INSERT INTO selection_trials(key,start,spec,digest,registered) VALUES(?,?,?,?,?)",
                (
                    "example",
                    "2030-31/38",
                    store.canonical(spec),
                    store.digest(spec),
                    self.stamp,
                ),
            )
            with patch.object(
                strategy, "implementation_digest", return_value="changed"
            ):
                with self.assertRaisesRegex(ValueError, "implementation changed"):
                    strategy._trial(db, "example")
            self.assertEqual(
                json.loads(
                    db.execute("SELECT spec FROM selection_trials").fetchone()[0]
                ),
                spec,
            )
