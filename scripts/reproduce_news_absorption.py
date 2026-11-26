"""Compare the current research duration coefficient with B1 on Claude's fixed synthetic schedule."""

import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np

from polymarket861 import football_model, news_model


TEAMS = [f"Club {i:02d}" for i in range(20)]
TARGET = TEAMS[7]
DROP = -0.04


def schedule():
    """Double round robin, twelve-week summer breaks, eleven rounds in season four."""
    rounds = []
    order = list(range(len(TEAMS)))
    for round_index in range(19):
        pairs = [(order[i], order[19 - i]) for i in range(10)]
        rounds.append(
            [
                (a, b) if (round_index + i) % 2 else (b, a)
                for i, (a, b) in enumerate(pairs)
            ]
        )
        order = [order[0], order[-1], *order[1:-1]]
    season_rounds = rounds + [[(b, a) for a, b in games] for games in rounds]
    matches = []
    day = 0
    for count in (38, 38, 38, 11):
        for games in season_rounds[:count]:
            matches.extend((TEAMS[a], TEAMS[b], day) for a, b in games)
            day += 7
        day += 12 * 7
    last = max(day for _, _, day in matches)
    return [(home, away, day - last) for home, away, day in matches]


def records(matches, attack, defence, absent_days):
    """Generate expected goals with the fixed attack reduction during the target's absence."""
    rows = []
    for home, away, day in matches:
        home_rate = math.exp(0.15 + 0.20 + attack[home] - defence[away])
        away_rate = math.exp(0.15 + attack[away] - defence[home])
        if day > -absent_days:
            if home == TARGET:
                home_rate *= math.exp(DROP)
            if away == TARGET:
                away_rate *= math.exp(DROP)
        rows.append((home, away, home_rate, away_rate, day * 86400))
    return rows


def main():
    rng = np.random.default_rng(861)
    attack = dict(zip(TEAMS, rng.normal(0, 0.25, 20), strict=True))
    defence = dict(zip(TEAMS, rng.normal(0, 0.25, 20), strict=True))
    matches = schedule()
    cut = datetime(2026, 11, 25, tzinfo=timezone.utc)
    kickoffs = [
        (cut + timedelta(days=d)).timestamp() for h, a, d in matches if TARGET in (h, a)
    ]
    base = football_model.fit_team_model(records(matches, attack, defence, 0))
    initial = float(base.attack[base.positions[TARGET]])
    rows = []
    for days in (7, 14, 28, 42, 63, 91, 182, 364):
        fitted = football_model.fit_team_model(records(matches, attack, defence, days))
        change = float(fitted.attack[fitted.positions[TARGET]]) - initial
        estimate = news_model.duration_evidence(kickoffs, cut - timedelta(days=days))
        rows.append(
            {
                "absence_days": days,
                "matches_in_episode": estimate["matches_in_episode"],
                "unabsorbed_fraction_from_refit": round(1 - change / DROP, 6),
                "research_duration_multiplier": round(
                    estimate["duration_multiplier"], 6
                ),
            }
        )
    report = {
        "scope": "Controlled expected-goal experiment using Claude's seed and schedule. Coefficients approximate B1 training exposure; this does not measure player value or predictive gain on real matches.",
        "seed": 861,
        "training_matches": len(matches),
        "team_matches": len(kickoffs),
        "decay_per_day": football_model.DECAY_PER_DAY,
        "news_policy": news_model.POLICY_SHA256,
        "rows": rows,
    }
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
