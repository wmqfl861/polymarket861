"""Recompute every applied player weight and both teams' goal-rate shifts from the published 0.4.2 early seals.

Reads only the report JSON. It does not import the project package.

For each applied claim it rebuilds, from the published counts alone:
  importance = 0.25 + 0.75 * mean(starts / matches, minutes / (90 * matches))   (directory basis)
             = neutral 0.55                                                      (fallback)
  absence    = status weight for "out" * (1 - chance_of_playing / 100)
  duration   = weight_before / (weight_before + weight_during)
  combined   = absence * duration * importance
  own/opponent log shifts = role effect * combined
and then the per-team log shift and the with-news goal rates.

Usage: python check_player_weights.py reports/news-early-seals-0.4.2.json
"""

import json
import math
import sys
from collections import Counter, defaultdict


def main(path):
    doc = json.load(open(path, encoding="utf-8"))
    policies = doc["weight_policies"]
    worst = defaultdict(float)
    bases, reasons, by_team = Counter(), Counter(), defaultdict(Counter)
    multipliers, durations, fixtures = [], [], []
    claims = 0
    for seal in doc["technical_early_seals"]:
        policy = policies[seal["policy_sha256"]]
        imp = policy["importance"]
        effects = policy["role_log_rate_effects"]
        cap = policy["max_abs_log_adjustment_per_team"]
        shift = {"home": 0.0, "away": 0.0}
        for item in seal["applied_player_weights"]:
            claims += 1
            history = item["importance_evidence"]
            basis = history["basis"]
            bases[basis] += 1
            by_team[item["team"]][basis] += 1
            if basis == "neutral":
                reasons[history.get("reason")] += 1
                importance = imp["neutral_multiplier"]
            elif basis == "epl_season_minutes_and_starts":
                m = history["matches"]
                share = (history["starts"] / m + history["minutes"] / (90 * m)) / 2
                importance = imp["minimum_multiplier"] + (imp["maximum_multiplier"] - imp["minimum_multiplier"]) * share
                # The directory itself must satisfy the published reconciliation rule.
                assert history["team_starts"] <= history["expected_team_starts"] == 11 * m
                assert history["team_starts"] >= imp["minimum_team_start_coverage"] * history["expected_team_starts"]
            else:
                share = history["starts"] / history["matches"]
                importance = imp["minimum_multiplier"] + (imp["maximum_multiplier"] - imp["minimum_multiplier"]) * share
            worst["importance"] = max(worst["importance"], abs(importance - item["importance_multiplier"]))
            multipliers.append(importance)

            status = policy["absence_weight"][item["evidence_status"]]
            chance = item["source_chance_of_playing_percent"]
            absence = status["out"] * (1 - chance / 100) if chance is not None else status[item["availability"]]
            worst["absence"] = max(worst["absence"], abs(absence - item["absence_weight"]))

            ev = item["duration_evidence"]
            if item["duration_basis"] == "b1_weighted_training_matches":
                total = ev["weight_before_episode"] + ev["weight_during_episode"]
                duration = ev["weight_before_episode"] / total
            else:
                duration = 1.0
            worst["duration"] = max(worst["duration"], abs(duration - item["duration_multiplier"]))
            durations.append(duration)

            combined = absence * duration * importance
            worst["combined"] = max(worst["combined"], abs(combined - item["combined_weight"]))
            own, opp = effects[item["role"]]
            worst["own_shift"] = max(worst["own_shift"], abs(own * combined - item["own_log_shift"]))
            worst["opponent_shift"] = max(worst["opponent_shift"], abs(opp * combined - item["opponent_log_shift"]))
            other = "away" if item["side"] == "home" else "home"
            shift[item["side"]] += own * combined
            shift[other] += opp * combined
        rates = {}
        for side in ("home", "away"):
            capped = max(-cap, min(cap, shift[side]))
            worst["team_shift"] = max(worst["team_shift"], abs(capped - seal["log_goal_shifts"][side]))
            base = seal["arms"]["no_news"]["rates"]["lambda_" + side]
            news = seal["arms"]["with_news"]["rates"]["lambda_" + side]
            worst["rate"] = max(worst["rate"], abs(base * math.exp(capped) - news))
            rates[side] = round(capped, 6)
        fixtures.append({"fixture": seal["fixture_id"].split("/", 2)[2], "claims": len(seal["applied_player_weights"]),
                         "log_shift": rates})
    multipliers.sort()
    print(json.dumps({
        "report": path.rsplit("/", 1)[-1],
        "fixtures": len(fixtures),
        "applied_claims": claims,
        "by_basis": dict(bases),
        "fallback_reasons": dict(reasons),
        "fallback_share": round(bases["neutral"] / claims, 4),
        "teams_with_fallback": {t: dict(c) for t, c in sorted(by_team.items()) if c["neutral"]},
        "importance_multiplier": {
            "min": round(multipliers[0], 4), "median": round(multipliers[len(multipliers) // 2], 4),
            "mean": round(sum(multipliers) / len(multipliers), 4), "max": round(multipliers[-1], 4),
            "at_floor_0.25": sum(abs(x - 0.25) < 1e-12 for x in multipliers),
        },
        "duration_multiplier": {"min": min(durations), "max": max(durations)},
        "max_abs_difference_from_published": {k: float(f"{v:.3g}") for k, v in sorted(worst.items())},
        "per_fixture": fixtures,
    }, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
