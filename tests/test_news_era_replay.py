"""Finite, bit-exact era certificates assembled through prospective forecast seals."""

import importlib.util
import json
import math
import shutil
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from polymarket861 import (
    football,
    model_study,
    news_era_replay as replay,
    news_evaluation,
    pipeline,
    store,
)
from test_news_accumulation import coverage
from test_news_matchday import HOME, AWAY, KICKOFF, SEASON, seed_fixture
from test_news_model import claim
from test_paired_proof import at


class EraReplayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root, self.history = Path(tmp.name) / "app", Path(tmp.name) / "history"
        self.current = pipeline._build_recipe("B1", (0.5, 1.5, 2.5, 3.5))
        self.previous = deepcopy(self.current)
        # Represent an incidental numerical packaging change. Both executions
        # actually run the real team model, news model, and all configured lines.
        self.previous["numeric_runtime"]["scipy"] += "-prior-build"
        self.teams = [(HOME, AWAY), ("Club 01", "Club 18"), ("Club 02", "Club 17")]
        with at("2026-08-01T10:00Z"):
            seed_fixture(self.root, self.history)
            for home, away in self.teams[1:]:
                store.register_fixture(
                    self.root,
                    {
                        "fixture_id": football.fixture_id(SEASON, home, away),
                        "season": SEASON,
                        "home": home,
                        "away": away,
                        "round_id": 1,
                        "kickoff": KICKOFF,
                        "known_at": store.now_utc(),
                        "source": "league",
                        "locator": "test://league",
                    },
                )
        self.clock = "2026-08-12T12:58Z"

    def seal(self, index, recipe, extractor="c" * 64):
        home, away = self.teams[index]
        fixture = football.fixture_id(SEASON, home, away)
        snapshot = {
            "fixture_id": fixture,
            "cutoff": store.iso(self.clock),
            "facts": [
                claim(
                    team=home,
                    extractor_config_sha256=extractor,
                    published_at="2026-08-12T10:10Z",
                    received_at="2026-08-12T10:11Z",
                    known_at="2026-08-12T10:12Z",
                )
            ],
            "coverage": coverage(home, away, self.clock, extractor),
        }
        with (
            at(self.clock),
            patch.object(pipeline, "_build_recipe", return_value=recipe),
        ):
            name = "B1@" + store.digest(recipe)
            pipeline.register_recipe(self.root, name, "B1")
            with closing(store.connect(self.root)) as db, db:
                db.execute(
                    "INSERT INTO builtin_model_releases(version,effective_at) VALUES (?,?)",
                    (name, store.now_utc()),
                )
            with patch("polymarket861.news.snapshot", return_value=snapshot):
                seal = pipeline.make_forecast(self.root, fixture, self.history)
        self.assertEqual(seal["purpose"], "main")
        self.assertTrue(seal["news_pair"]["changed"])
        return seal

    def compare(self, source, candidate=None):
        with patch.object(
            pipeline, "_build_recipe", return_value=candidate or self.current
        ):
            return replay.compare(self.root, source, SEASON)

    def test_all_original_arms_pool_only_while_complete_inventory_still_matches(self):
        old = self.seal(0, self.previous)
        target = self.seal(1, self.current)
        source_era = old["news_study"]["era_sha256"]
        report = self.compare(source_era)
        self.assertEqual(report["status"], "bit_identical", report)
        self.assertEqual(
            self.compare(source_era)["report_sha256"], report["report_sha256"]
        )
        self.assertEqual(report["target_era"], target["news_study"]["era_sha256"])
        self.assertEqual(len(report["records"]), 2)
        self.assertEqual({r["probability_values"] for r in report["records"]}, {60})
        self.assertEqual(
            {r["seal"] for r in report["records"]}, {old["digest"], target["digest"]}
        )
        for r in report["records"]:
            self.assertEqual(r["sealed_binary64_sha256"], r["replayed_binary64_sha256"])

        def accumulated():
            return news_evaluation.evaluate_accumulation(self.root, SEASON)

        results = accumulated()
        self.assertEqual(len(results["eras"]), 2)
        self.assertEqual(
            results["era_equivalence"]["comparisons"][0]["pooled"]["eligible_seals"], 2
        )
        self.seal(2, self.current)
        stale = accumulated()["era_equivalence"]["comparisons"][0]
        self.assertEqual(
            (stale["status"], stale["pooled"]),
            ("inventory_changed_replay_required", None),
        )
        updated = self.compare(source_era)
        self.assertEqual(updated["status"], "bit_identical")
        self.assertNotEqual(updated["report_sha256"], report["report_sha256"])
        self.assertEqual(
            accumulated()["era_equivalence"]["comparisons"][0]["pooled"][
                "eligible_seals"
            ],
            3,
        )
        self.assertEqual(
            news_evaluation.evaluate_accumulation(self.root, SEASON, horizon="T-24h")[
                "era_equivalence"
            ]["comparisons"],
            [],
        )

    def test_reduced_source_manifests_still_register_but_cannot_certify_either_side(
        self,
    ):
        from polymarket861 import model_study

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(model_study, "PACKAGE_ROOT", Path(tmp)),
        ):
            (Path(tmp) / "runner.py").write_text("a = 1\n")
            reduced = pipeline._build_recipe("B1", self.current["lines"])
        self.assertIn("runner.py", reduced["algorithm_source"])
        self.assertIsNone(reduced["news_frozen_feature_source"])
        original = self.seal(0, self.previous)
        source_era = original["news_study"]["era_sha256"]
        self.assertEqual(
            self.compare(source_era, reduced)["status"], "incomplete_feature_manifest"
        )
        partial = self.seal(1, reduced)
        partial_era = partial["news_study"]["era_sha256"]
        result = self.compare(partial_era)
        self.assertEqual(
            (result["status"], result["eligible_for_pooling"]),
            ("incomplete_feature_manifest", False),
        )
        self.assertFalse(replay._same_features(reduced, reduced))

    def test_one_ulp_on_one_away_under_direction_is_a_real_difference(self):
        old = self.seal(0, self.previous)
        source = old["news_study"]["era_sha256"]
        self.assertEqual(self.compare(source)["status"], "bit_identical")
        forecast = pipeline._forecast_arms

        def changed(*args, **kwargs):
            primary, arms = forecast(*args, **kwargs)
            one = arms["with_news"]["totals"]["away"]["3.5"]
            one["under"] = math.nextafter(one["under"], 0)
            return primary, arms

        with patch.object(pipeline, "_forecast_arms", side_effect=changed):
            mismatch = self.compare(source)
        self.assertFalse(mismatch["eligible_for_pooling"])
        self.assertEqual(mismatch["records"][0]["status"], "different_probabilities")
        self.assertEqual(
            replay.compare(self.root, source, SEASON, mode="formal")["status"],
            "no_sealed_source_pairs",
        )

    def test_real_fixture_and_score_selectors_change_player_weight_and_block_the_bridge(
        self,
    ):
        from polymarket861 import news_minutes, opportunity, result_pairings
        from test_news_importance import UsageHistoryTests, usage_rows

        old = self.seal(0, self.current)
        era = old["news_study"]["era_sha256"]
        usage = UsageHistoryTests()
        usage.setUp()
        self.addCleanup(usage.doCleanups)
        usage.capture(usage_rows())
        self.assertEqual(
            usage.history()["enwiki:100"]["basis"], "epl_season_minutes_and_starts"
        )
        mutations = (
            (
                "result_pairings",
                "counts[key(f)] == 1",
                "counts[key(f)] == 2",
                result_pairings,
                "unique_fixtures",
            ),
            (
                "opportunity",
                'return "-".join("united"',
                'return "" and "-".join("united"',
                opportunity,
                "_club",
            ),
            (
                "score_bounds",
                "0 <= goals <= 50",
                "0 <= goals <= 1",
                news_minutes,
                "valid_score",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "polymarket861"
            shutil.copytree(
                model_study.PACKAGE_ROOT,
                source,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            with patch.object(model_study, "PACKAGE_ROOT", source):
                for name, before, after, consumer, symbol in mutations:
                    with self.subTest(module=name):
                        path = source / f"{name}.py"
                        original = path.read_text()
                        self.assertEqual(original.count(before), 1)
                        path.write_text(original.replace(before, after))
                        try:
                            candidate = pipeline._build_recipe(
                                "B1", self.current["lines"]
                            )
                            spec = importlib.util.spec_from_file_location(
                                f"polymarket861._changed_{name}", path
                            )
                            module = importlib.util.module_from_spec(spec)
                            spec.loader.exec_module(module)
                            with patch.object(
                                consumer, symbol, getattr(module, symbol)
                            ):
                                self.assertEqual(
                                    usage.history()["enwiki:100"]["basis"], "neutral"
                                )
                            for key in (
                                "news_probability_source",
                                "news_frozen_feature_source",
                            ):
                                self.assertNotEqual(
                                    self.current[key][path.name],
                                    candidate[key][path.name],
                                )
                            result = self.compare(era, candidate)
                            self.assertNotEqual(era, result["target_era"])
                            self.assertEqual(result["status"], "not_equivalent")
                            self.assertEqual(
                                result["records"][0]["reason"],
                                "different_frozen_feature_semantics",
                            )
                            self.assertFalse(result["eligible_for_pooling"])
                        finally:
                            path.write_text(original)

    def test_changed_policy_or_upstream_selector_and_damaged_archive_cannot_bridge(
        self,
    ):
        old = self.seal(0, self.previous)
        source = old["news_study"]["era_sha256"]
        for field in ("news_probability_policy", "news_frozen_feature_source"):
            candidate = deepcopy(self.current)
            if field == "news_probability_policy":
                candidate[field][-1] = "b" * 64
                self.assertEqual(
                    self.compare(source, candidate)["status"], "different_news_policy"
                )
            else:
                candidate[field]["news.py"] = "b" * 64
                fail = self.compare(source, candidate)
                self.assertFalse(fail["eligible_for_pooling"])
                self.assertEqual(
                    fail["records"][0]["reason"], "different_frozen_feature_semantics"
                )
        target = self.seal(1, self.current)["news_study"]["era_sha256"]
        self.assertEqual(self.compare(source)["status"], "bit_identical")
        with closing(store.connect(self.root)) as db:
            manifest = json.loads(
                db.execute(
                    "SELECT manifest FROM football_input_manifests WHERE digest=?",
                    (old["input_manifest_sha256"],),
                ).fetchone()[0]
            )
        store.blob_path(self.root, manifest["evidence_sha256"]).write_bytes(b"broken")
        comparisons = news_evaluation.evaluate_accumulation(self.root, SEASON)[
            "era_equivalence"
        ]["comparisons"]
        (matching,) = [c for c in comparisons if c["target_era"] == target]
        self.assertEqual(matching["status"], "unverifiable_inventory")
        damaged = self.compare(source)
        self.assertEqual(damaged["status"], "not_equivalent")
        self.assertFalse(damaged["eligible_for_pooling"])

    def test_original_declaration_cannot_silently_remove_a_sealed_pair(self):
        old = self.seal(0, self.previous)
        source = old["news_study"]["era_sha256"]
        target = self.seal(1, self.current)
        self.assertEqual(self.compare(source)["status"], "bit_identical")
        with closing(store.connect(self.root)) as db, db:
            raw = json.loads(
                db.execute(
                    "SELECT data_json FROM forecast_seals WHERE digest=?",
                    (target["digest"],),
                ).fetchone()[0]
            )
            del raw["news_study"]
            db.execute(
                "UPDATE forecast_seals SET data_json=?,digest=? WHERE digest=?",
                (store.canonical(raw), store.digest(raw), target["digest"]),
            )
        changed = self.compare(source)
        self.assertEqual(changed["status"], "unverifiable_inventory")
        self.assertIn("declaration", changed["reason"])
        current = news_evaluation.evaluate_accumulation(self.root, SEASON)[
            "era_equivalence"
        ]["comparisons"][0]
        self.assertEqual(
            (current["status"], current["pooled"]), ("unverifiable_inventory", None)
        )

    def test_different_extractor_stays_separate_and_a_restored_archive_can_be_recognised(
        self,
    ):
        old = self.seal(0, self.previous)
        source = old["news_study"]["era_sha256"]
        other = self.seal(1, self.current, extractor="9" * 64)
        only_old = self.compare(source)
        self.assertNotEqual(only_old["target_era"], other["news_study"]["era_sha256"])
        self.assertEqual(only_old["inventory_count_by_era"], {source: 1})
        same = self.seal(2, self.current)
        genuine = self.compare(source)
        with closing(store.connect(self.root)) as db:
            ref = json.loads(
                db.execute(
                    "SELECT manifest FROM football_input_manifests WHERE digest=?",
                    (old["input_manifest_sha256"],),
                ).fetchone()[0]
            )["evidence_sha256"]
        path = store.blob_path(self.root, ref)
        original = path.read_bytes()
        path.write_bytes(b"damaged")
        self.assertFalse(self.compare(source)["eligible_for_pooling"])
        path.write_bytes(original)
        self.assertEqual(
            self.compare(source)["report_sha256"], genuine["report_sha256"]
        )
        study = news_evaluation.evaluate_accumulation(self.root, SEASON)
        self.assertEqual(len(study["eras"]), 3)
        pooled = study["era_equivalence"]["comparisons"][0]
        self.assertEqual(pooled["target_era"], same["news_study"]["era_sha256"])
        self.assertEqual(pooled["pooled"]["eligible_seals"], 2)

    def test_new_seal_mid_replay_cannot_install_a_partial_certificate(self):
        source = self.seal(0, self.previous)["news_study"]["era_sha256"]
        original = replay._replay_one

        def new_receipt(*args, **kwargs):
            row = original(*args, **kwargs)
            self.seal(1, self.current)
            return row

        with patch.object(replay, "_replay_one", side_effect=new_receipt):
            report = self.compare(source)
        self.assertEqual(report["status"], "inventory_changed_during_replay")
        self.assertFalse(report["eligible_for_pooling"])
        self.assertFalse(store.list_reports(self.root, replay.KIND))
