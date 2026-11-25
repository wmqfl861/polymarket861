"""News diagnostic denominators, original market witnesses and prospective study eras."""

import json
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import test_opportunity as market
import test_round_comparisons as native
from kickoff_fixture import capture_example
from polymarket861 import (
    kickoff_proof,
    news_accumulation,
    news_evaluation,
    news_market,
    news_model,
    news_study_plan as plan,
    pipeline,
    store,
)
from test_news_matchday import AWAY, FID, HOME, KICKOFF, SEASON, seed_fixture
from test_news_outcome_admission import admit_final
from test_paired_proof import at


def coverage(home, away, stamp, config="a" * 64):
    return {
        "both_teams_observed": True,
        "observed_teams": 2,
        **{
            side: {
                "team": team,
                "observed": True,
                "sources": [
                    {
                        "status": "ok",
                        "checked_at": store.iso(stamp),
                        "availability_observed": True,
                        "source_observed_at": store.iso(stamp),
                        "extractor_config_sha256": config,
                    }
                ],
            }
            for side, team in (("home", home), ("away", away))
        },
    }


class NativeCloseTests(unittest.TestCase):
    def setUp(self):
        self.case = native.RoundComparisons()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root
        self.game = store.fixture_at(self.root, market.FID)
        self.cut = market.CUT - timedelta(minutes=1)
        self.entry = {"sealed_at": store.iso(self.cut), "mode": "formal"}
        p = self.case.forecast["probabilities"] | {
            "home_win": 0.40,
            "draw": 0.35,
            "away_win": 0.25,
        }
        self.pair = {
            "coverage": coverage(market.HOME, market.AWAY, self.cut),
            "input_cutoff": store.iso(self.cut),
            "no_news": {"probabilities": p},
            "with_news": {"probabilities": p | {"home_win": 0.50, "draw": 0.25}},
        }

    def read(self):
        with closing(store.connect(self.root)) as db:
            return news_market.observe(db, self.root, self.game, self.entry, self.pair)

    def test_matches_frozen_clock_and_original_condition_family_at_actual_close(self):
        self.case.batch(
            market.CUT - timedelta(seconds=2)
        )  # Too late for the sealed inputs.
        self.case.batch(market.KICK - timedelta(seconds=5))
        self.assertEqual(self.read()["reason"], "missing_verified_actual_kickoff")
        actual = market.KICK + timedelta(minutes=1)
        self.case.batch(actual - timedelta(seconds=15), shift=2)
        self.case.batch(actual - timedelta(seconds=5), omit={"21", "42"}, shift=3)
        self.case.batch(actual + timedelta(seconds=1), shift=4)
        with (
            patch.dict(kickoff_proof.OPEN_RELEASES, {}),
            market.at(actual + timedelta(hours=3)),
        ):
            receipt = capture_example(self.root, self.game, 77332111, actual)
            missing = self.read()
            self.assertFalse(missing["endpoints"])
            self.assertIn("decision", missing["missing"]["result"])
            self.case.batch(self.cut - timedelta(seconds=4))
            # All tokens within a family must come from one native observation.
            joined = self.read()
            self.assertEqual(joined["kickoff_proof"], receipt["proof_id"])
            self.assertEqual(
                set(joined["endpoints"]), set(self.pair["no_news"]["probabilities"])
            )
            self.assertEqual(
                joined["receipts"]["result"]["close"]["received_at"],
                store.iso(actual - timedelta(seconds=15)),
            )
            self.assertEqual(
                joined["receipts"]["home"]["close"]["received_at"],
                store.iso(actual - timedelta(seconds=5)),
            )
            self.assertEqual(len(joined["receipts"]["result"]["contracts"]), 3)
            h = joined["endpoints"]["home_win"]
            self.assertAlmostEqual(h["decision"], 0.50 / 1.02)
            self.assertAlmostEqual(h["close"], 0.52 / 1.08)
            self.assertEqual(
                (h["direction"], h["market_drift_agreement"]), ("toward", "away")
            )
            self.assertAlmostEqual(
                h["distance_gain_pp"],
                100 * (abs(0.40 - h["close"]) - abs(0.50 - h["close"])),
            )
            self.assertEqual(
                joined["endpoints"]["total_over_2_5"]["direction"], "unchanged"
            )
            # A changed indexed observation cannot replace its archived original.
            with closing(store.connect(self.root)) as db, db:
                db.execute(
                    "UPDATE ma_books SET payload='{}' WHERE sha=?",
                    (joined["receipts"]["result"]["decision"]["batch"],),
                )
            self.assertFalse(self.read()["endpoints"])

    def test_stale_coverage_and_replay_are_separate_from_market_misses(self):
        self.entry["mode"] = "replay"
        self.assertEqual(self.read()["reason"], "requires_prospective_seal")
        self.entry["mode"] = "formal"
        self.pair["coverage"]["away"]["sources"][0]["source_observed_at"] = store.iso(
            self.cut - timedelta(hours=7)
        )
        self.assertEqual(self.read()["reason"], "news_coverage_incomplete_or_stale")
        metrics = news_market.summary([])["endpoints"]["home_win"]
        self.assertEqual(metrics["pairs"], 0)
        self.assertIsNone(metrics["toward_fraction"])
        self.assertIsNone(metrics["mean_distance_gain_pp"])
        # A step may point toward the close and still overshoot it.
        overshoot = news_market._measure(0.40, 0.75, 0.30, 0.50)
        self.assertEqual(overshoot["direction"], "toward")
        self.assertLess(overshoot["distance_gain_pp"], 0)
        self.assertEqual(
            news_market._measure(0.50, 0.45, 0.60, 0.50)["direction"], "at_reference"
        )


class EraTests(unittest.TestCase):
    def test_wire_mapping_quarantine_and_claimant_change_era_while_transport_does_not(
        self,
    ):
        import hashlib
        from polymarket861 import news_sources

        original = Path(news_sources.__file__).read_text()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "news_sources.py"

            def fingerprint(extra=""):
                raw = (original + "\n" + extra).encode()
                path.write_bytes(raw)
                return plan.probability_sources(
                    root, {path.name: hashlib.sha256(raw).hexdigest()}
                )

            base = fingerprint()
            for name in (
                "wire_claim",
                "wire_fingerprint",
                "wire_labels",
                "_wire_rejection",
                "_wire_row",
            ):
                with self.subTest(name=name):
                    self.assertNotEqual(
                        base, fingerprint(f"def {name}(*args): return None")
                    )
            for name in ("WIRE_MODEL", "WIRE_FIELDS"):
                self.assertNotEqual(base, fingerprint(f"{name} = 'different'"))
            self.assertEqual(base, fingerprint("def _get(url): return None"))

    def test_narrow_probability_source_and_runtime_while_physical_gate_stays_whole(
        self,
    ):
        recipe = pipeline._build_recipe("B1", (1.5, 2.5))
        pair = {
            "protocol": news_model.PROTOCOL,
            "availability_basis": news_model.LOCAL,
            "policy_sha256": news_model.POLICY_SHA256,
        }
        expected = plan.era(recipe, pair, "a" * 64)
        self.assertIn("ledger.py", recipe["algorithm_source"])
        self.assertNotIn("ledger.py", recipe["news_probability_source"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "news_minutes.py"

            def identity(text):
                path.write_text(text)
                import hashlib

                return plan.probability_sources(
                    root, {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}
                )

            definition = (
                "RATE=2\ndef parse(x): return x*RATE\ndef latest(x): return parse(x)\n"
            )
            self.assertEqual(
                identity(definition), identity(definition + "def poll(): return 2\n")
            )
            self.assertNotEqual(
                identity(definition), identity(definition.replace("RATE=2", "RATE=3"))
            )
        changed = deepcopy(recipe)
        changed["algorithm_source"]["ledger.py"] = "b" * 64
        self.assertNotEqual(store.digest(changed), store.digest(recipe))
        self.assertEqual(plan.era(changed, pair, "a" * 64), expected)
        for new_recipe, candidate, extractor in (
            (recipe | {"lines": [2.5]}, pair, "a" * 64),
            (recipe | {"numeric_runtime": {"scipy": "other"}}, pair, "a" * 64),
            (recipe, pair | {"policy_sha256": "changed"}, "a" * 64),
            (recipe, pair, "b" * 64),
        ):
            self.assertNotEqual(plan.era(new_recipe, candidate, extractor), expected)

    def test_only_original_sealed_declaration_counts_and_scores_require_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, history = Path(tmp) / "app", Path(tmp) / "football"
            with at("2026-08-01T10:00Z"):
                seed_fixture(root, history)
            stamp = "2026-08-12T12:59Z"
            snap = {
                "fixture_id": FID,
                "cutoff": store.iso(stamp),
                "facts": [],
                "coverage": coverage(HOME, AWAY, stamp),
            }
            with patch("polymarket861.news.snapshot", return_value=snap), at(stamp):
                seal = pipeline.make_forecast(root, FID, history)
            self.assertEqual(seal["news_study"]["plan_sha256"], plan.PLAN_SHA256)
            era = seal["news_study"]["era_sha256"]
            study = news_evaluation.evaluate_accumulation(root, "2027-28")
            self.assertEqual(study["scheduled_by_season"], {"2026-27": 1, "2027-28": 0})
            early = study["eras"][era]
            self.assertEqual(
                (
                    early["eligible_seals"],
                    early["score"]["pairs"],
                    early["home_close"]["pairs"],
                ),
                (1, 0, 0),
            )
            self.assertIsNone(early["home_close"]["mean"])
            self.assertIsNone(early["score"]["power"]["mde_at_target_power"])
            self.assertGreater(
                study["planning"]["score_points"][
                    "illustrative_matches_at_assumed_variance"
                ],
                380,
            )
            self.assertEqual(
                news_evaluation.evaluate_accumulation(root, SEASON, mode="formal")[
                    "eras"
                ],
                {},
            )
            self.assertEqual(
                news_evaluation.evaluate_accumulation(root, SEASON, horizon="T-24h")[
                    "eras"
                ],
                {},
            )
            with self.assertRaises(ValueError):
                news_evaluation.evaluate_accumulation(root, SEASON, mode="replay")
            admit_final(root, HOME, AWAY, KICKOFF, [0, 1], "2026-08-13T10:00Z")
            later = news_evaluation.evaluate_accumulation(root, SEASON)["eras"][era]
            self.assertEqual(
                (later["score"]["pairs"], later["score"]["mean"]), (1, 0.0)
            )
            self.assertIsNone(later["score"]["interval"])
            # A valid earlier pair which never declared the new design still
            # grades under its original policy without joining the new cohort.
            with closing(store.connect(root)) as db, db:
                raw = json.loads(
                    db.execute(
                        "SELECT data_json FROM forecast_seals WHERE digest=?",
                        (seal["digest"],),
                    ).fetchone()[0]
                )
                del raw["news_study"]
                raw["news_pair"]["protocol"], raw["news_pair"]["policy_sha256"] = (
                    plan.LEGACY_POLICY
                )
                recipe = json.loads(
                    db.execute(
                        "SELECT recipe FROM model_recipes WHERE version=?",
                        (raw["model_version"],),
                    ).fetchone()[0]
                )
                del recipe["news_probability_policy"], recipe["news_probability_source"]
                raw["recipe_digest"] = store.digest(recipe)
                db.execute(
                    "UPDATE model_recipes SET recipe=? WHERE version=?",
                    (store.canonical(recipe), raw["model_version"]),
                )
                db.execute(
                    "UPDATE forecast_seals SET digest=?,data_json=? WHERE digest=?",
                    (store.digest(raw), store.canonical(raw), seal["digest"]),
                )
            after = news_evaluation.evaluate_accumulation(root, SEASON)
            self.assertEqual(
                after["exclusions"], {"no_prospective_study_declaration": 1}
            )
            self.assertEqual(after["eras"], {})
            old = news_evaluation.evaluate_season(root, SEASON, policy="v1")
            self.assertEqual(
                (old["policy_sha256"], old["scored"]), (plan.LEGACY_POLICY[1], 1)
            )

    def test_round_clusters_across_seasons_include_zeros_and_separate_eras(self):
        rows = []
        for season, rnd, score, era in [
            ("2026-27", 12, 1, "first"),
            ("2026-27", 12, 0, "first"),
            ("2027-28", 12, -2, "first"),
            ("2027-28", 13, 4, "second"),
        ]:
            movement = news_market._measure(0.4, 0.4 + score / 100, 0.4, 0.5)
            rows.append(
                {
                    "season": season,
                    "round_id": rnd,
                    "sealed": True,
                    "scored": True,
                    "timely_news_coverage": True,
                    "observed_both": True,
                    "study_status": "declared",
                    "implementation": era,
                    "policy_sha256": era,
                    "news_protocol": "test",
                    "extractor_config_sha256": era,
                    "recipe": "physical",
                    "scores": {
                        "no_news": {"score": 50},
                        "with_news": {"score": 50 + score},
                    },
                    "market_close": {
                        "endpoints": {"home_win": movement},
                        "missing": {},
                    },
                }
            )
        rows.append(
            rows[0] | {"study_status": "unknown_or_mixed_extractor_configuration"}
        )
        out = news_accumulation.summarize(
            rows, ["2026-27", "2027-28"], "T-60m", "technical"
        )
        self.assertEqual(out["eligible_seals"], 4)
        self.assertEqual(out["eras"]["second"]["score"]["pairs"], 1)
        first = out["eras"]["first"]
        self.assertEqual(first["seasons"], {"2026-27": 2, "2027-28": 1})
        for key in ("score", "home_close"):
            one = first[key]
            self.assertEqual(
                (one["pairs"], one["rounds"], one["interval"]["family_comparisons"]),
                (3, 2, 4),
            )
            self.assertAlmostEqual(one["mean"], -1 / 3)
            self.assertGreater(one["power"]["mde_at_target_power"], 0)
            self.assertTrue(0 <= one["power"]["power_at_practical_effect"] <= 1)
        primary = first["market_close"]["endpoints"]["home_win"]
        self.assertEqual(
            (primary["toward"], primary["away"], primary["unchanged"]), (1, 1, 1)
        )
        self.assertEqual(primary["toward_fraction"], 0.5)
