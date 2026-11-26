"""Compare the registered news/close slope and distance on labelled synthetic closes.

Run from the repository root with PYTHONPATH=src. The input supplies only the
published home-win news movements. Every closing price and outcome here is
synthetic. Repeat looks are descriptive and never authorize a model change.
"""

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import t as student

from polymarket861 import news_market, news_study_plan


def cluster(moves, gaps, family):
    rounds, size = moves.shape[1:]
    # Inputs are percentage points. The registered tolerance is a probability.
    active = np.abs(moves) > 100 * news_study_plan.PLAN["direction_absolute_tolerance"]
    x = np.where(active, moves, 0.0)
    effective = active.any(axis=2).sum(axis=1)
    xx = np.square(x).sum(axis=(1, 2))
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = (x * gaps).sum(axis=(1, 2)) / xx
        scores = (x * (gaps - slope[:, None, None] * x)).sum(axis=2)
        se = np.where(
            effective > 1,
            np.sqrt(effective / (effective - 1) * np.square(scores).sum(axis=1)) / xx,
            np.nan,
        )
    slope_crit = student.ppf(1 - 0.025 / family, effective - 1)
    gain = abs(gaps) - abs(gaps - moves)
    delta = gain.mean(axis=(1, 2))
    influence = gain.sum(axis=2) - size * delta[:, None]
    distance_se = np.sqrt(rounds / (rounds - 1) * np.square(influence).sum(axis=1)) / (
        rounds * size
    )
    crit = float(student.ppf(1 - 0.025 / family, rounds - 1))
    return slope, se, slope - slope_crit * se, delta, delta - crit * distance_se


def validation(moves_pp, gaps_pp):
    declaration = {
        "study_status": "declared",
        "study_plan_sha256": news_study_plan.PLAN_SHA256,
        "implementation": "controlled_calculation",
    }
    rows = []
    for r, (moves, gaps) in enumerate(zip(moves_pp, gaps_pp), start=1):
        for x, y in zip(moves, gaps):
            row = news_market._measure(0.5, 0.5 + x / 100, 0.5, 0.5 + y / 100)
            rows.append(
                declaration
                | {
                    "season": "controlled",
                    "round_id": r,
                    "scheduled_market_close": {"endpoints": {"home_win": row}},
                }
            )
    return news_market.regression(rows, "scheduled_market_close")


def study(path, draws=2000):
    raw = path.read_bytes()
    docs = json.loads(raw)["technical_early_seals"]
    moves = [
        100
        * (
            d["arms"]["with_news"]["probabilities"]["home_win"]
            - d["arms"]["no_news"]["probabilities"]["home_win"]
        )
        for d in docs
    ]
    family = news_study_plan.PLAN["family_comparisons"]
    rng = np.random.default_rng(861)
    cells = []
    max_diff = 0.0
    for sigma in (3, 5, 8):
        for beta in (0, 0.5, 1):
            for rounds in (27, 38, 76):
                sums = defaultdict(float)
                for start in range(0, draws, 125):
                    n = min(draws - start, 125)
                    shape = (n, rounds, 10)
                    x = rng.choice(moves, size=shape)
                    y = (
                        beta * x
                        + rng.normal(0, sigma * np.sqrt(0.05), (n, rounds, 1))
                        + rng.normal(0, sigma * np.sqrt(0.95), shape)
                    )
                    slopes, se, lo, distance, distance_lo = cluster(x, y, family)
                    if start == 0:
                        actual = validation(x[0], y[0])
                        max_diff = max(
                            max_diff,
                            abs(actual["slope"] - slopes[0]),
                            abs(actual["interval"]["standard_error"] - se[0]),
                            abs(actual["interval"]["lower"] - lo[0]),
                        )
                    for key, data in (
                        ("mean_slope", slopes),
                        ("mean_distance_gain_pp", distance),
                        ("slope_lower_above_zero_fraction", lo > 0),
                        ("distance_lower_above_zero_fraction", distance_lo > 0),
                    ):
                        sums[key] += float(data.sum())
                cells.append(
                    {
                        "assumed_gap_sd_pp": sigma,
                        "assumed_beta": beta,
                        "rounds": rounds,
                        "matches": 10 * rounds,
                        **{k: round(v / draws, 6) for k, v in sums.items()},
                    }
                )
    if max_diff >= 1e-10:
        raise ValueError(
            "independent vectorized estimator disagrees with the released implementation"
        )
    sparse = [[2.0], [-2.0]]
    gaps = [[1.6], [-2.4]]
    sparse_checks = []
    for zero_rounds in (0, 36):
        x = np.asarray(sparse + [[0.0]] * zero_rounds)
        y = np.asarray(gaps + [[1.0]] * zero_rounds)
        production = validation(x, y)
        lower = cluster(x[None, :, :], y[None, :, :], family)[2][0]
        if abs(production["interval"]["lower"] - lower) > 1e-10:
            raise ValueError("informative-round placebo check disagrees")
        sparse_checks.append(
            {
                "observed_rounds": production["rounds"],
                "informative_rounds": production["informative_rounds"],
                "df": production["interval"]["degrees_of_freedom"],
                "slope": production["slope"],
                "lower": production["interval"]["lower"],
                "upper": production["interval"]["upper"],
            }
        )
    return {
        "scope": "Simulation. Native closes, kickoff proof, realized scores, profits and calibration of the actual v3 policy are unobserved here.",
        "plan_sha256": news_study_plan.PLAN_SHA256,
        "source_file": path.name,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_fixtures": len(docs),
        "home_news_moves_pp": [round(v, 6) for v in moves],
        "seed": 861,
        "replications_per_cell": draws,
        "intraround_correlation_assumption": 0.05,
        "family_comparisons": family,
        "empirical_power_quantity": "fraction of simulated lower simultaneous confidence bounds above zero",
        "independent_crosschecks": len(cells),
        "max_absolute_difference_vs_released_slope_se_or_bound": float(max_diff),
        "zero_movement_round_check": sparse_checks,
        "cells": cells,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seals", type=Path)
    parser.add_argument("--draws", type=int, default=2000)
    args = parser.parse_args()
    if args.draws < 1:
        parser.error("--draws requires a positive integer")
    print(json.dumps(study(args.seals, args.draws), indent=2, allow_nan=False))
