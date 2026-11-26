#!/usr/bin/env python3
"""Operating characteristics of the acceptance thresholds proposed in docs/five-leagues-0.5.

Pure arithmetic (SciPy only); no project data is read. Each block names the plan section it checks.

Usage: python check_gate_statistics.py > check-gate-statistics.json
"""
from __future__ import annotations

import json
import math

from scipy.stats import binom, norm, t


def wilson(k: int, n: int, z: float = 1.959963984540054):
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [centre - half, centre + half]


out = {}

# NEWS-AND-OPERATIONS.md, "确认缺阵": precision >= 0.95 on >= 40 predicted confirmations per language.
news = {"wilson_38_of_40": wilson(38, 40)}
# With n = 40 the rule passes with 0, 1 or 2 false confirmations (38/40 = 0.95).
news["pass_probability_n40_at_most_2_errors"] = {str(p): float(binom.cdf(2, 40, 1 - p)) for p in (0.80, 0.85, 0.90, 0.95, 0.98)}
# Smallest n whose Wilson lower bound reaches 0.90 for 0, 1, 2, 3 errors, and the pass probabilities of those rules.
need = {}
for errors in range(0, 6):
    n = next(n for n in range(errors + 1, 2000) if wilson(n - errors, n)[0] >= 0.90)
    need[str(errors)] = {
        "n": n, "wilson_lower": wilson(n - errors, n)[0], "point": (n - errors) / n,
        "pass_probability": {str(p): float(binom.cdf(errors, n, 1 - p)) for p in (0.85, 0.90, 0.95, 0.98)},
    }
news["rule_wilson_lower_at_least_0_90"] = need
# Five languages must each pass: probability that all five pass when every language truly has the same precision.
news["all_five_languages_pass_n40"] = {str(p): float(binom.cdf(2, 40, 1 - p)) ** 5 for p in (0.90, 0.95, 0.98)}
out["news_confirmed_absence_precision"] = news

# DEVELOPMENT-PLAN.md "来源更替" and MODEL-AND-VALIDATION.md "数据字段成为主源": >= 20 samples, timely-valid rate >= 95 %.
source = {"wilson_19_of_20": wilson(19, 20), "wilson_20_of_20": wilson(20, 20)}
source["pass_probability_n20_at_least_19"] = {str(p): float(binom.sf(18, 20, p)) for p in (0.80, 0.85, 0.90, 0.95, 0.98)}
source["pass_probability_n60_at_least_57"] = {str(p): float(binom.sf(56, 60, p)) for p in (0.80, 0.85, 0.90, 0.95, 0.98)}
source["wilson_57_of_60"] = wilson(57, 60)
source["pass_probability_n100_at_least_95"] = {str(p): float(binom.sf(94, 100, p)) for p in (0.80, 0.85, 0.90, 0.95, 0.98)}
# Alternative rules: the same question asked of more samples, with a threshold that separates 0.95 from 0.85.
levels = (0.80, 0.85, 0.90, 0.95, 0.98)
source["alternative_rules_pass_probability"] = {
    f"n{n}_at_least_{k}": {str(p): float(binom.sf(k - 1, n, p)) for p in levels}
    for n, k in ((20, 18), (40, 36), (60, 54), (100, 91), (100, 92))
}
out["source_promotion_20_samples_95pct"] = source

# MODEL-AND-VALIDATION.md "统计区间": candidates x 2 stages x 5 endpoints; one forward season has 38 or 34 round clusters.
gate = {}
for rounds in (34, 38):
    for candidates in (1, 2, 3):
        family = candidates * 2 * 5
        critical = float(t.ppf(1 - 0.025 / family, rounds - 1))
        gate[f"rounds_{rounds}_candidates_{candidates}"] = {
            "family": family, "critical_t": critical,
            "t_needed_for_80pct_power": critical + float(norm.ppf(0.8)),
            # both stages must clear their bound; with equal independent power the joint power is the square
            "joint_power_if_each_stage_has_80pct": 0.8 ** 2,
        }
gate["five_leagues_each_must_pass_both_stages_if_each_stage_has_80pct"] = 0.8 ** 10
gate["five_leagues_each_must_pass_both_stages_if_each_stage_has_95pct"] = 0.95 ** 10
out["promotion_gate"] = gate

# Coverage floors quoted in the plan.
out["coverage_floor"] = {str(n): math.ceil(0.95 * n) for n in (380, 306)}
out["coverage_floor"]["290_of_306"] = 290 / 306

# DEVELOPMENT-PLAN.md "资金竞争": 10,000 per strategy, worst net loss per fixture <= 150, per intent <= 50.
fixtures_per_weekend = 48
out["bankroll"] = {
    "fixtures_in_sampled_weekend": fixtures_per_weekend,
    "worst_case_at_risk_if_every_fixture_reaches_cap": fixtures_per_weekend * 150,
    "share_of_10000": fixtures_per_weekend * 150 / 10000,
    "weekends_of_full_exposure_before_cash_is_exhausted_without_settlement": 10000 / (fixtures_per_weekend * 150),
    "fixtures_until_cash_is_exhausted_at_one_50_intent_each": 10000 / 50,
}

# Capacity: target tokens in one weekend and book observations with the 0.4.2 cadence in market_archive.next_capture.
contracts_per_fixture = 24  # 3 moneyline + 9 totals + 6 + 6 team totals observed in the 2026 window
tokens = contracts_per_fixture * 2
captures_last_three_hours = (3 * 3600 - 120) // 60 + (120 + 600) // 12  # every 60 s to T-120 s, then every 12 s to T+600 s
captures_day_before = (24 * 3600 - 3 * 3600) // 900  # every 900 s from T-24 h to T-3 h
out["capacity"] = {
    "contracts_per_fixture": contracts_per_fixture, "tokens_per_fixture": tokens,
    "tokens_in_a_48_fixture_weekend": 48 * tokens,
    "captures_per_fixture_last_three_hours": captures_last_three_hours,
    "captures_per_fixture_from_T_minus_24h": captures_day_before + captures_last_three_hours,
    "token_books_per_fixture_from_T_minus_24h": (captures_day_before + captures_last_three_hours) * tokens,
    "token_books_in_a_48_fixture_weekend_from_T_minus_24h": 48 * (captures_day_before + captures_last_three_hours) * tokens,
    "token_books_per_season_1752_fixtures_from_T_minus_24h": 1752 * (captures_day_before + captures_last_three_hours) * tokens,
    "batch_requests_per_fixture_from_T_minus_24h": captures_day_before + captures_last_three_hours,
    "batch_requests_in_a_48_fixture_weekend": 48 * (captures_day_before + captures_last_three_hours),
    "batch_requests_per_season_1752_fixtures": 1752 * (captures_day_before + captures_last_three_hours),
    "note": "one batch request per fixture per capture in market_archive._capture; token-book counts are observations, not HTTP requests",
}

# Receipts that cannot be backfilled: fixtures of the four new leagues played per round while collection has not started.
per_round = {"lal": 10, "bun": 9, "sea": 10, "fl1": 9}
out["uncollected_new_league_fixtures"] = {
    "fixtures_per_round_of_all_four": sum(per_round.values()),
    "contracts_per_fixture": contracts_per_fixture,
    "after_weeks": {str(w): {"fixtures": w * sum(per_round.values()), "contracts": w * sum(per_round.values()) * contracts_per_fixture} for w in (1, 4, 7)},
    "note": "one round of each league per week is an approximation; midweek rounds and breaks change the count",
}

print(json.dumps(out, indent=1))
