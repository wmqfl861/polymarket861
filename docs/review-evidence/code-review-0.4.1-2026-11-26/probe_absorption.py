"""Probe E (0.4.1) — how fast does B1 itself absorb a lasting absence, compared with the 42-day news decay?

news_model.POLICY["duration_steps_days"] removes an absence from the with-news arm in steps: full weight in
the first week, 0.75 from day 7, 0.5 from day 14, 0.25 from day 28 and nothing from day 42. The stated idea is
that a long absence is already contained in the team's results. B1 weights results by exp(-0.002 * days)
(football_model.DECAY_PER_DAY), i.e. a half-life of about 347 days.

This probe measures B1's own response with the package's fit (football_model.fit_team_model):
  * 20 teams, three full double round-robin seasons plus the first 11 rounds of a fourth (1,250 matches,
    the size of the real training set), one round a week with a 12-week summer break;
  * goals are the EXPECTED goals of a known model, so the fit has no sampling noise;
  * one team's scoring rate falls by 4% (the largest forward effect in the news policy) for its last
    d days of matches; the same team's fitted attack is compared with the fit without that fall.
"absorbed" = share of the 4% that B1's fitted attack has taken on by the time of the next match.

Usage: PYTHONPATH=src python docs/review-evidence/code-review-0.4.1-2026-11-26/probe_absorption.py
"""

from __future__ import annotations

import json
import math

import numpy as np

from polymarket861 import football_model, news_model

DAY = 86400
TEAMS = [f"Club {i:02d}" for i in range(20)]
TARGET = TEAMS[7]
DROP = -0.04


def schedule():
    """Circle-method double round robin, one round a week, 12-week summer break; the last season stops after 11 rounds."""
    rounds = []
    order = list(range(20))
    for r in range(19):
        pairs = [(order[i], order[19 - i]) for i in range(10)]
        rounds.append([(a, b) if (r + i) % 2 else (b, a) for i, (a, b) in enumerate(pairs)])
        order = [order[0], order[-1], *order[1:-1]]
    double = rounds + [[(b, a) for a, b in rnd] for rnd in rounds]
    matches, day = [], 0
    for season in range(4):
        for index, rnd in enumerate(double):
            if season == 3 and index == 11:
                break
            matches.extend((TEAMS[a], TEAMS[b], day) for a, b in rnd)
            day += 7
        day += 12 * 7
    last = max(m[2] for m in matches)
    return [(h, a, d - last) for h, a, d in matches]  # days relative to the latest round (<= 0)


def records(matches, attack, defence, absent_days):
    """Expected goals; TARGET scores exp(DROP) times as many in matches inside its last `absent_days` days."""
    rows = []
    for home, away, day in matches:
        lam = math.exp(0.15 + 0.20 + attack[home] - defence[away])
        mu = math.exp(0.15 + attack[away] - defence[home])
        if day > -absent_days:
            if home == TARGET:
                lam *= math.exp(DROP)
            if away == TARGET:
                mu *= math.exp(DROP)
        rows.append((home, away, lam, mu, day * DAY))
    return rows


def main():
    rng = np.random.default_rng(861)
    attack = dict(zip(TEAMS, rng.normal(0, 0.25, 20)))
    defence = dict(zip(TEAMS, rng.normal(0, 0.25, 20)))
    matches = schedule()
    base = football_model.fit_team_model(records(matches, attack, defence, 0))
    base_attack = float(base.attack[base.positions[TARGET]])
    steps = news_model.POLICY["duration_steps_days"]
    rows = []
    for days in (7, 14, 28, 42, 63, 91, 182, 364):
        fit = football_model.fit_team_model(records(matches, attack, defence, days))
        moved = float(fit.attack[fit.positions[TARGET]]) - base_attack
        affected = sum(1 for h, a, d in matches if d > -days and TARGET in (h, a))
        policy = next(weight for start, weight in reversed(steps) if start <= days)
        rows.append({
            "absent_for_days": days, "matches_already_played_without_him": affected,
            "b1_has_absorbed_share": round(moved / DROP, 4),
            "share_b1_has_not_absorbed": round(1 - moved / DROP, 4),
            "news_policy_duration_multiplier": policy,
            "simple_exponential_rule_exp(-0.002*days)": round(math.exp(-football_model.DECAY_PER_DAY * days), 4),
        })
    print(json.dumps({
        "training_matches": len(matches), "decay_per_day": football_model.DECAY_PER_DAY,
        "half_life_days": round(math.log(2) / football_model.DECAY_PER_DAY, 1),
        "ridge": football_model.RIDGE, "duration_steps_days": steps, "rows": rows,
    }, indent=1))


if __name__ == "__main__":
    main()
