"""Probe B (0.4.1) — how many matches would the declared news study need, in the news arm's best case?

news_study_plan.PLAN declares two endpoints and the effect it calls practical:
  score  : with-news minus no-news four-component score, practical effect 0.10 points, planning SD 1.0
  close  : |no-news - close| - |with-news - close| on the home-win probability, 0.25 pp, planning SD 1.5
Both use a round-clustered Student interval at a family of 4 comparisons (experiment_statistics.clustered_t).

Part 1 (exact, no simulation). For the ten published matchweek-12 pairs (reports/news-early-seals-0.4.1.json)
assume the with-news probabilities ARE the truth. The expected score difference is then an exact sum over
scorelines, using the package's own grid and football_model.score_probabilities.

Part 2 (simulation, seed 861). close = base + e + beta * move, where move is the published signed home-win
change of a randomly drawn pair, beta is the share of that change the market ends up agreeing with, and
e is the ordinary model-versus-market gap (SD sigma, intraround correlation 0.05 as in the plan).
sigma is an ASSUMPTION — the repository holds no closing prices yet — so three values are shown.
Two analyses of the same simulated data are compared:
  plan  : mean distance gain > 0 by the plan's clustered interval (family of 4)
  slope : through-origin regression of (close - base) on move, round-clustered CR1, same family of 4

Usage: PYTHONPATH=src python docs/review-evidence/code-review-0.4.1-2026-11-26/probe_power.py reports/news-early-seals-0.4.1.json [--studies 4000]
"""

from __future__ import annotations

import argparse
import json
import math

import numpy as np
from scipy.stats import norm, t as student

from polymarket861 import experiment_statistics, football_model, news_study_plan

PLAN = news_study_plan.PLAN
FAMILY = PLAN["family_comparisons"]
SIZE = 13  # scorelines above 12 goals for one side carry < 1e-9 of the probability here
PER_ROUND = PLAN["planning_assumptions"]["fixtures_per_round"]
ICC = PLAN["planning_assumptions"]["intraround_correlation"]
DEFF = 1 + (PER_ROUND - 1) * ICC
Z_CRIT = norm.ppf(1 - (1 - PLAN["family_confidence"]) / (2 * FAMILY))
Z_80 = norm.ppf(PLAN["target_power"])


def arm(rates, lines):
    grid, _ = football_model._goals_matrix(rates["lambda_home"], rates["lambda_away"], rates["rho"])
    probs, _ = football_model.probabilities(grid, lines)
    pmf = np.zeros((SIZE, SIZE))
    n = min(SIZE, np.asarray(grid).shape[0])
    pmf[:n, :n] = np.asarray(grid)[:n, :n]
    return probs, pmf / pmf.sum()


def score_table(probs):
    return np.array([[football_model.score_probabilities(probs, h, a)["score"] for a in range(SIZE)]
                     for h in range(SIZE)])


def needed(sd, effect):
    return math.ceil(DEFF * (sd * (Z_CRIT + Z_80) / effect) ** 2)


def part_one(rows):
    gains, variances, fixtures = [], [], []
    for row in rows:
        lines = tuple(row["lines"])
        base_probs, _ = arm(row["arms"]["no_news"]["rates"], lines)
        news_probs, news_pmf = arm(row["arms"]["with_news"]["rates"], lines)
        delta = score_table(news_probs) - score_table(base_probs)
        mean = float((delta * news_pmf).sum())
        gains.append(mean)
        variances.append(float((delta ** 2 * news_pmf).sum()) - mean ** 2)
        fixtures.append({"fixture": row["fixture_id"].split("/", 2)[2],
                         "home_win_move_pp": round(100 * (news_probs["home_win"] - base_probs["home_win"]), 3),
                         "expected_score_gain_if_news_is_exactly_right": round(mean, 5)})
    gain = float(np.mean(gains))
    sd = math.sqrt(float(np.mean(variances)) + float(np.var(gains)))
    declared = PLAN["practical_effect"]["score_points"]
    planned = needed(PLAN["planning_assumptions"]["single_fixture_sd"]["score_points"], declared)
    return {
        "truth": "results drawn from the published with-news probabilities (best case for the news arm)",
        "expected_gain_per_match_points": gain,
        "sd_of_paired_difference_per_match_points": sd,
        "declared_practical_effect_points": declared,
        "declared_effect_over_best_case_gain": declared / gain,
        "plan_illustrative_matches_for_declared_effect": planned,
        "matches_for_80pct_power_at_best_case_gain": needed(sd, gain),
        "seasons_for_80pct_power_at_best_case_gain": needed(sd, gain) / 380,
        "power_at_plan_illustrative_matches_for_best_case_gain":
            float(norm.cdf(gain * math.sqrt(planned / DEFF) / sd - Z_CRIT)),
        "fixtures": fixtures,
    }


def clustered_mean(values):
    """Vectorised experiment_statistics.clustered_t for equal rounds. values: studies x rounds x fixtures."""
    studies, rounds, per = values.shape
    n = rounds * per
    sums = values.sum(axis=2)
    mean = sums.sum(axis=1) / n
    influence = sums - per * mean[:, None]
    se = np.sqrt(rounds / (rounds - 1) * np.square(influence).sum(axis=1)) / n
    critical = student.ppf(1 - 0.025 / FAMILY, rounds - 1)
    return mean, se, critical


def clustered_slope(move, gap):
    """Through-origin slope of gap on move with CR1 round clusters. Arrays: studies x rounds x fixtures."""
    studies, rounds, per = move.shape
    sxx = np.square(move).sum(axis=(1, 2))
    slope = (move * gap).sum(axis=(1, 2)) / sxx
    scores = (move * (gap - slope[:, None, None] * move)).sum(axis=2)
    se = np.sqrt(rounds / (rounds - 1) * np.square(scores).sum(axis=1)) / sxx
    critical = student.ppf(1 - 0.025 / FAMILY, rounds - 1)
    return slope, se, critical


def part_two(moves_pp, studies, rng):
    moves_pp = np.asarray(moves_pp)
    out = {"published_home_win_moves_pp": [round(float(v), 3) for v in moves_pp],
           "mean_abs_move_pp": float(np.abs(moves_pp).mean()),
           "root_mean_square_move_pp": float(np.sqrt(np.square(moves_pp).mean())),
           "declared_practical_effect_pp": PLAN["practical_effect"]["home_close_distance_pp"],
           "plan_illustrative_matches_for_declared_effect":
               needed(PLAN["planning_assumptions"]["single_fixture_sd"]["home_close_distance_pp"],
                      PLAN["practical_effect"]["home_close_distance_pp"]),
           "intraround_correlation": ICC, "family_comparisons": FAMILY, "studies_per_cell": studies,
           "cells": []}
    for sigma in (3.0, 5.0, 8.0):
        for beta in (1.0, 0.5, 0.0):
            for rounds in (10, 27, 38, 76, 152):
                shape = (studies, rounds, PER_ROUND)
                move = rng.choice(moves_pp, size=shape)
                shared = rng.normal(0, sigma * math.sqrt(ICC), size=(studies, rounds, 1))
                own = rng.normal(0, sigma * math.sqrt(1 - ICC), size=shape)
                gap = shared + own + beta * move          # close - base
                gain = np.abs(gap) - np.abs(gap - move)   # |base-close| - |news-close|
                mean, se, critical = clustered_mean(gain)
                slope, slope_se, _ = clustered_slope(move, gap)
                out["cells"].append({
                    "sigma_gap_pp": sigma, "beta": beta, "rounds": rounds, "matches": rounds * PER_ROUND,
                    "mean_distance_gain_pp": round(float(mean.mean()), 4),
                    "sd_of_single_match_gain_pp": round(float(gain.std()), 4),
                    "plan_test_detects_gain": round(float((mean - critical * se > 0).mean()), 4),
                    "slope_test_detects_positive_slope": round(float((slope - critical * slope_se > 0).mean()), 4),
                    "mean_slope": round(float(slope.mean()), 4),
                    "sd_of_slope": round(float(slope.std()), 4),
                })
    return out


def cross_check(rng):
    """The vectorised interval must equal the package's own function."""
    values = rng.normal(0.2, 1.5, size=(3, 12, PER_ROUND))
    mean, se, critical = clustered_mean(values)
    worst = 0.0
    for i in range(3):
        ours = experiment_statistics.clustered_t([[[v] for v in row] for row in values[i]], FAMILY)
        interval = ours["intervals"][0]
        worst = max(worst, abs(interval["difference"] - mean[i]), abs(interval["standard_error"] - se[i]),
                    abs(ours["critical_t"] - critical))
    return worst


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--studies", type=int, default=4000)
    args = parser.parse_args()
    rows = json.load(open(args.report))["technical_early_seals"]
    rng = np.random.default_rng(861)
    moves = [100 * (r["arms"]["with_news"]["probabilities"]["home_win"] -
                    r["arms"]["no_news"]["probabilities"]["home_win"]) for r in rows]
    out = {
        "plan_sha256": news_study_plan.PLAN_SHA256,
        "design_effect": DEFF, "normal_critical_value_family_of_4": float(Z_CRIT),
        "vectorised_interval_max_abs_difference_from_package": cross_check(rng),
        "score_endpoint_best_case": part_one(rows),
        "closing_endpoint": part_two(moves, args.studies, rng),
    }
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
