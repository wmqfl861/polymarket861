import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from polymarket861 import league_study as study


class LeagueStudyTests(unittest.TestCase):
    def pair(self, fixture_id, instant, delta=1.0):
        return study.PairedScore.from_backtest(
            {
                "fixture_id": fixture_id,
                "season": "2025-26",
                "round": 2,
                "kickoff": instant,
                "model_score": 70 + delta,
                "baseline_score": 70,
                "model_items": dict.fromkeys(study.ITEM_KEYS, 70 + delta),
                "baseline_items": dict.fromkeys(study.ITEM_KEYS, 70),
                "fallback_reason": None,
            }
        )

    def test_shared_week_local_round_and_bonferroni(self):
        monday = datetime(2026, 11, 23, 20, tzinfo=timezone.utc)
        tuesday = datetime(2026, 11, 24, 0, tzinfo=timezone.utc)
        rows = [
            self.pair("lal-a", monday),
            self.pair("bun-a", monday),
            self.pair("sea-b", tuesday, -0.5),
        ]
        self.assertEqual(study._statistics(rows)["clusters"], 1)
        pooled = study._statistics(rows, pooled=True)
        self.assertEqual(pooled["clusters"], 2)
        self.assertEqual(pooled["matches"], 3)
        self.assertEqual(pooled["intervals"]["composite"]["difference"], 0.5)
        self.assertEqual(study.FAMILY_SIZE, 10)
        with self.assertRaisesRegex(ValueError, "duplicate fixture"):
            study._statistics(rows + rows[:1], pooled=True)

    def test_country_draws_down_coverage_and_effect_gate_independently(self):
        common = {"intervals": {"composite": {"lower": 0.3, "difference": 0.8}}}
        one = {
            "development": common,
            "holdout": {
                **common,
                "matches": 291,
                "scheduled": 306,
                "rounds_with_pairs": 34,
                "registered_rounds": 34,
            },
        }
        reports = dict.fromkeys(study.league_registry.PROFILES, one)
        approved = study._initial_research_gate(reports, common)
        self.assertTrue(approved["research_criteria_met"])
        self.assertFalse(approved["formal_promotion"])
        self.assertFalse(
            study._initial_research_gate({"bun": one}, common)["research_criteria_met"]
        )
        for override in (
            {"matches": 290},
            {"rounds_with_pairs": 33},
            {"intervals": {"composite": {"lower": -1, "difference": -0.1}}},
        ):
            reports["bun"] = {**one, "holdout": {**one["holdout"], **override}}
            self.assertFalse(
                study._initial_research_gate(reports, common)["research_criteria_met"]
            )

    def test_hash_mismatch_stops_before_scoring(self):
        with tempfile.TemporaryDirectory() as path:
            base = Path(path)
            season = study.SOURCE_SEASONS[0]
            (base / season).mkdir()
            (
                base / season / study.league_registry.profile("bun").source_file
            ).write_bytes(b"changed")
            with patch.object(
                study,
                "_league_replay",
                side_effect=AssertionError("scored before check"),
            ):
                with self.assertRaisesRegex(ValueError, "blob mismatch"):
                    study._snapshot(base, base / "snapshot", ["bun"])
