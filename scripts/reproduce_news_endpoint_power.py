"""Reproduce the v2 EPL registration's planning probabilities from the ten prior seals."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.stats import t as student

from polymarket861 import news_market, news_study_plan


FAMILY = 14
SEED = 861
PILOT_SHA = "46a998f5c8f33449b364d37d19e82e7fed45fd12a806408422b0b89e6c24e9f2"
ENDPOINTS = {"home_win": "home_win", "full_over_2_5": "total_over_2_5"}
LAYOUTS = {
    "one_mode_era": ["technical/era_1"],
    "four_mode_era_groups": [
        "technical/era_1",
        "technical/era_2",
        "formal/era_2",
        "formal/era_3",
    ],
}


def centered_lower_bound(x, y, labels):
    """Vectorized within-stratum slope with one cluster per official round."""
    blocks = np.unique(labels)
    xc, yc = x.copy(), y.copy()
    p = np.ones(x.shape[0], dtype=int)
    tolerance = news_study_plan.PLAN["direction_absolute_tolerance"]
    for group in blocks:
        idx = labels == group
        xc[:, idx, :] -= x[:, idx, :].mean(axis=(1, 2), keepdims=True)
        yc[:, idx, :] -= y[:, idx, :].mean(axis=(1, 2), keepdims=True)
        p += (np.abs(xc[:, idx, :]) > tolerance).any(axis=(1, 2))
    active = np.abs(xc) > tolerance
    xc = np.where(active, xc, 0.0)
    n = active.sum(axis=(1, 2))
    groups = active.any(axis=2).sum(axis=1)
    xx = np.square(xc).sum(axis=(1, 2))
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = (xc * yc).sum(axis=(1, 2)) / xx
        scores = (xc * (yc - slope[:, None, None] * xc)).sum(axis=2)
        se = (
            np.sqrt(
                groups
                / (groups - 1)
                * (n - 1)
                / (n - p)
                * np.square(scores).sum(axis=1)
            )
            / xx
        )
        critical = student.ppf(1 - 0.025 / FAMILY, groups - 1)
    spread = np.where(active, xc, -np.inf).max(axis=(1, 2)) - np.where(
        active, xc, np.inf
    ).min(axis=(1, 2))
    estimable = (
        (groups > 1)
        & (n > p)
        & (spread > tolerance)
        & (se > 1e-15 * np.maximum(1.0, abs(slope)))
    )
    return np.where(estimable, slope - critical * se, np.nan)


def verify_fit(x, y, labels, lower):
    points = [
        (("simulated_season", rnd), int(labels[rnd]), float(step), float(gap))
        for rnd, (moves, gaps) in enumerate(zip(x, y))
        for step, gap in zip(moves, gaps)
    ]
    estimate = news_market.price_gap_slope(
        points, fixed_effects=True, family=FAMILY, no_pairs_status="no_pairs"
    )
    native = estimate["interval"]
    if native is None:
        if np.isfinite(lower):
            raise ValueError(
                "simulated evidence admitted when the registered estimator is undefined"
            )
    elif not np.isclose(native["lower"], lower, rtol=0, atol=1e-10):
        raise ValueError("simulated bound differs from the registered estimator")


def cell(rng, moves, groups, rounds, per_round, sd, beta, draws):
    # Each modeled upgrade or mode transition happens at an official round boundary.
    labels = np.empty(rounds, dtype=int)
    for group, indices in enumerate(np.array_split(np.arange(rounds), len(groups))):
        labels[indices] = group
    positives = defined = 0
    for start in range(0, draws, 80):
        size = (min(80, draws - start), rounds, per_round)
        x = rng.choice(moves, size=size)
        y = (
            beta * x
            + rng.normal(0.0, sd * np.sqrt(0.05), (size[0], rounds, 1))
            + rng.normal(0.0, sd * np.sqrt(0.95), size)
        )
        lower = centered_lower_bound(x, y, labels)
        if start == 0:
            verify_fit(x[0], y[0], labels, lower[0])
        defined += int(np.isfinite(lower).sum())
        positives += int((lower > 0).sum())
    return {
        "rounds": rounds,
        "matches": rounds * per_round,
        "assumed_gap_sd_pp": sd * 100,
        "assumed_slope": beta,
        "interval_defined": defined / draws,
        "lower_bound_above_zero": positives / draws,
    }


def study(source, draws=2000):
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PILOT_SHA:
        raise ValueError(
            "The registered pilot requires the original 0.4.2 ten-fixture input"
        )
    if draws < 1:
        raise ValueError("positive replication count required")
    seals = json.loads(raw)["technical_early_seals"]
    rng = np.random.default_rng(SEED)
    movements, cells = {}, []
    for endpoint, key in ENDPOINTS.items():
        moves = np.asarray(
            [
                s["arms"]["with_news"]["probabilities"][key]
                - s["arms"]["no_news"]["probabilities"][key]
                for s in seals
            ]
        )
        movements[endpoint] = {
            "rms_pp": round(100 * float(np.sqrt(np.mean(moves**2))), 6),
            "moves_pp": [round(float(step) * 100, 6) for step in moves],
        }
        for layout, groups in LAYOUTS.items():
            for rounds, per_round in ((27, 10), (65, 10), (65, 5)):
                for sd in (0.03, 0.05, 0.08):
                    for beta in (0.0, 0.5, 1.0):
                        cells.append(
                            {"endpoint": endpoint, "layout": layout}
                            | cell(
                                rng, moves, groups, rounds, per_round, sd, beta, draws
                            )
                        )
    return {
        "design": "epl-news-home-and-full-over-v2",
        "scope": "Prospective design sensitivity. All closes and their noise are simulated. "
        "The reported probabilities describe the ability to detect a stipulated slope.",
        "source": {
            "file": source.name,
            "sha256": PILOT_SHA,
            "round": 12,
            "fixtures": len(seals),
            "basis": "2026/27 matchweek 12 unscheduled, pre-release technical early seals",
        },
        "family_comparisons": FAMILY,
        "family_confidence": 0.95,
        "simulated_outcomes": "The fourteen comparisons include twelve price slopes and two paired score guardrails. The table estimates marginal power for price slopes under assumed eligible coverage. Score guardrail power would also require a model of regulation results and all score components.",
        "replications_per_cell": draws,
        "seed": SEED,
        "estimator": "Common within-(mode,probability-era) slope, both axes demeaned even for one stratum. "
        "Season/official-round CR1 clusters and Student intervals with G-1 degrees of freedom.",
        "estimand": "Fraction of replications with a positive lower simultaneous bound, "
        "including undefined intervals in the denominator.",
        "clock_and_horizon_assumption": "The same empirical movement distribution is used as a design input for "
        "each of three research clocks and two horizons. Receipt, admission, "
        "coverage and agreement between clocks remain empirical questions.",
        "simulation": "With-replacement movement draws from ten prior fixtures. The close gap equals beta times "
        "the movement plus Gaussian noise with 5% of variance shared by each round. Strata are "
        "consecutive approximately equal blocks of rounds with a shared slope. The 650-fixture "
        "case assumes every future book and eligibility proof is present.",
        "layouts": LAYOUTS,
        "movements": movements,
        "maximum_monte_carlo_standard_error_pp": round(50 / np.sqrt(draws), 4),
        "production_check": "The first replicate of each cell is checked against news_market.price_gap_slope.",
        "reproduce": "PYTHONPATH=src python scripts/reproduce_news_endpoint_power.py reports/news-early-seals-0.4.2.json",
        "cells": cells,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seals", type=Path)
    parser.add_argument("--draws", type=int, default=2000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--package-asset", type=Path)
    args = parser.parse_args()
    if args.package_asset and args.draws != 2000:
        parser.error("the registered asset uses 2,000 replications per cell")
    report = study(args.seals, args.draws)
    content = (
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode()
    if args.output:
        args.output.write_bytes(content)
    else:
        print(content.decode(), end="")
    if args.package_asset:
        with args.package_asset.open("wb") as out:
            with gzip.GzipFile(
                filename="", fileobj=out, mode="wb", mtime=0
            ) as compressed:
                compressed.write(content)
