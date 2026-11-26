"""Compare the current research duration coefficient with B1 on Claude's fixed synthetic schedule."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from polymarket861 import football_model, news_model


def main():
    original = (Path(__file__).resolve().parents[1] / "docs" / "review-evidence"
                / "code-review-0.4.1-2026-11-26" / "probe_absorption.py")
    spec = importlib.util.spec_from_file_location("review_absorption_schedule", original)
    sample = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sample)
    rng = np.random.default_rng(861)
    attack = dict(zip(sample.TEAMS, rng.normal(0, 0.25, 20), strict=True))
    defence = dict(zip(sample.TEAMS, rng.normal(0, 0.25, 20), strict=True))
    matches = sample.schedule()
    cut = datetime(2026, 11, 25, tzinfo=timezone.utc)
    kickoffs = [(cut + timedelta(days=d)).timestamp() for h, a, d in matches if sample.TARGET in (h, a)]
    base = football_model.fit_team_model(sample.records(matches, attack, defence, 0))
    initial = float(base.attack[base.positions[sample.TARGET]])
    rows = []
    for days in (7, 14, 28, 42, 63, 91, 182, 364):
        fitted = football_model.fit_team_model(sample.records(matches, attack, defence, days))
        change = float(fitted.attack[fitted.positions[sample.TARGET]]) - initial
        estimate = news_model.duration_evidence(kickoffs, cut - timedelta(days=days))
        rows.append({"absence_days": days, "matches_in_episode": estimate["matches_in_episode"],
                     "unabsorbed_fraction_from_refit": round(1 - change / sample.DROP, 6),
                     "research_duration_multiplier": round(estimate["duration_multiplier"], 6)})
    report = {"scope": "Controlled expected-goal experiment using Claude's seed and schedule. Coefficients approximate B1 training exposure; this does not measure player value or predictive gain on real matches.",
              "seed": 861, "training_matches": len(matches), "team_matches": len(kickoffs),
              "decay_per_day": football_model.DECAY_PER_DAY, "news_policy": news_model.POLICY_SHA256, "rows": rows}
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
