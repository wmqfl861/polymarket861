import math
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from scipy.special import expit, logit
from scipy.stats import t

from polymarket861 import (
    experiment_statistics as stats,
    football as fb,
    football_candidates as candidates,
    football_model as fm,
    jobs_research as research,
    paired_proof,
    store,
)


class StatisticalTests(unittest.TestCase):
    def test_empty_schedule_during_season_cannot_backdate_registration(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(store, "now_utc", return_value="2040-10-01T12:00:00.000000Z"),
        ):
            with self.assertRaisesRegex(ValueError, "full schedule"):
                research.register_candidate(
                    Path(folder),
                    "late",
                    kind="model",
                    version="X",
                    baseline="B1",
                    window="2040-41/1",
                    hypothesis="test fair future horizon",
                )

    def test_small_cluster_interval_weight_and_family(self):
        groups = [[0, 1], [1], [3, 2, 4]]
        bands = stats.clustered_t(groups, 4)
        v = bands["intervals"][0]
        m = 11 / 6
        self.assertAlmostEqual(v["difference"], m)
        se = math.sqrt(3 / 2 * ((1 - 2 * m) ** 2 + (1 - m) ** 2 + (9 - 3 * m) ** 2)) / 6
        self.assertAlmostEqual(v["lower"], m - t.ppf(1 - 0.025 / 4, 2) * se)
        self.assertGreater(
            stats.clustered_t(groups, 1)["intervals"][0]["lower"], v["lower"]
        )
        self.assertIsNone(stats.clustered_t([[3]], 1))

    def test_totals_calibration_preserves_joint_score_and_team_totals(self):
        matrix, _ = fm._goals_matrix(1.85, 1.1, -0.07)
        projected = candidates.calibrate_matrix(matrix, [0.15, 0.7])
        base, totals = fm.probabilities(matrix, [0.5, 1.5, 2.5, 3.5])
        p, lines = fm.probabilities(projected, [0.5, 1.5, 2.5, 3.5])
        self.assertAlmostEqual(projected.sum(), 1)
        self.assertAlmostEqual(
            p["total_over_2_5"], expit(0.15 + 0.7 * logit(base["total_over_2_5"]))
        )
        self.assertAlmostEqual(
            projected[0, 0] / projected[0, 1], matrix[0, 0] / matrix[0, 1]
        )
        self.assertAlmostEqual(
            projected[3, 2] / projected[2, 1], matrix[3, 2] / matrix[2, 1]
        )
        self.assertAlmostEqual(p["home_over_1_5"], projected[2:, :].sum())
        self.assertAlmostEqual(p["away_over_1_5"], projected[:, 2:].sum())
        self.assertGreater(len(lines), 0)

    def test_training_ignores_future_labels_withdrawals_and_outcomes(self):
        raw = []
        stage = datetime(2022, 8, 1, tzinfo=UTC)
        for year in (2020, 2021, 2022):
            for i in range(120 if year == 2021 else 2):
                at = datetime(year, 9, 1, tzinfo=UTC) + timedelta(hours=10 * i)
                k = f"epl/{year}-{(year + 1) % 100:02}/p{i}/q{i}"
                score = (i % 3, (i + 1) % 3)
                available = at + timedelta(hours=24)
                raw.append(
                    fb.Fixture(
                        k,
                        k.split("/")[1],
                        1 + i // 10,
                        at,
                        f"p{i}",
                        f"q{i}",
                        *score,
                        result_versions=(
                            fb.ResultVersion(
                                available, score, "lag_assumption", "fixture"
                            ),
                        ),
                    )
                )

        def forecast(engine, home, away, kick, cut, **kw):
            n = int(home[1:])
            m, _ = fm._goals_matrix(1 + n % 4 / 4, 1.1, -0.01)
            return {
                "model": "B1",
                "matrix": m,
                "fallback_reason": None,
                "probabilities": fm.probabilities(m, ())[0],
                "evidence": {
                    "result_grades": {"lag_assumption": 1},
                    "latest_result_available_at": store.iso(cut - timedelta(days=1)),
                    "input_sha256": str(n),
                },
            }

        late = tuple(
            replace(
                f,
                home_goals=None,
                away_goals=None,
                result_versions=f.result_versions
                + (
                    fb.ResultVersion(
                        stage + timedelta(days=2), None, "lag_assumption", "fixture"
                    ),
                ),
            )
            if f.season == "2021-22"
            else f
            for f in raw
        )
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(fb.FootballEngine, "forecast", new=forecast),
        ):

            def run(fixtures):
                with patch.object(
                    candidates.historical_inputs,
                    "load_for_study",
                    return_value=(
                        fixtures,
                        {"timing_authenticated": False, "reason": None},
                    ),
                ):
                    return candidates.study(
                        Path(temp),
                        None,
                        first="2022-23",
                        last="2022-23",
                        archive_lag_hours=24,
                    )

            before, after = run(raw), run(late)
        self.assertEqual(before["folds"][0], after["folds"][0])
        self.assertEqual(before["folds"][0]["calibration_training_n"], 122)
        self.assertIsNotNone(before["folds"][0]["calibration"])
        self.assertLess(
            store.instant(before["folds"][0]["latest_calibration_result"]), stage
        )

    def test_source_contract_can_require_named_review_and_a_changed_receipt_needs_review(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            research.register_candidate(
                data,
                "manual-feed",
                kind="source",
                scope="lineup",
                version="new",
                baseline="old",
                window="2040-41/1",
                hypothesis="field trial",
                manifest={
                    "rounds": 2,
                    "field": "lineup",
                    "reference_source": "independent",
                    "activation_policy": "named_review",
                },
            )
            verdict = {
                "qualified": True,
                "reason": "qualified",
                "rank": 0.95,
                "complete": True,
                "reference_digest": "before",
            }
            with patch.object(
                research, "_assess", side_effect=lambda *a: dict(verdict)
            ):
                self.assertEqual(
                    research.evaluate_candidate(data, "manual-feed")["state"],
                    "recommended",
                )
                self.assertIsNone(research.active_version(data, "source", "lineup"))
                verdict["reference_digest"] = "after"
                altered = research.approve_candidate(
                    data,
                    "manual-feed",
                    reviewer="source editor",
                    reason="reviewed receipt",
                )
                self.assertEqual(
                    altered["assessment"]["reason"], "evidence_changed_review_again"
                )
                self.assertIsNone(research.active_version(data, "source", "lineup"))
                confirmed = research.approve_candidate(
                    data,
                    "manual-feed",
                    reviewer="source editor",
                    reason="reviewed revised receipt",
                )
                self.assertEqual(confirmed["state"], "promoted")
                self.assertEqual(
                    confirmed["assessment"]["approved_by"]["reviewer"], "source editor"
                )

    def test_legacy_model_window_remains_registered_and_cannot_replace_incumbent(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            manifest = {"comparison_policy": paired_proof.POLICY}
            spec = dict(
                kind="model",
                scope="epl",
                version="legacy",
                baseline="old",
                season="2040-41",
                first=1,
                span=12,
                hypothesis="legacy",
                manifest=manifest,
            )
            identity = store.digest(spec)
            with closing(research._open(data)) as db, db:
                db.execute(
                    "INSERT INTO research_candidates(key,kind,scope,version,baseline,season,first,span,hypothesis,manifest,digest,registered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "before",
                        "model",
                        "epl",
                        "legacy",
                        "old",
                        "2040-41",
                        1,
                        12,
                        "legacy",
                        store.canonical(manifest),
                        identity,
                        store.now_utc(),
                    ),
                )
            same = research.register_candidate(
                data,
                "before",
                kind="model",
                version="legacy",
                baseline="old",
                window="2040-41/1",
                hypothesis="legacy",
                manifest=manifest,
            )
            self.assertEqual(same["digest"], identity)
            with patch.object(
                research,
                "_assess",
                return_value={
                    "qualified": True,
                    "reason": "qualified",
                    "rank": 0.2,
                    "complete": True,
                },
            ):
                self.assertEqual(
                    research.evaluate_candidate(data, "before")["assessment"]["reason"],
                    "frozen_legacy_window_research_only",
                )
                with self.assertRaisesRegex(ValueError, "recommended"):
                    research.approve_candidate(
                        data, "before", reviewer="operator", reason="old pass"
                    )
            self.assertIsNone(research.active_version(data))
