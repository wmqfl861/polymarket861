"""Probe C — can one season of paired forecasts settle whether the news arm helps?

The season verdict in news_evaluation needs the Bonferroni-adjusted (family of 10), round-clustered
t interval of the mean paired score difference to exclude zero over 38 rounds (and at least 361
covered pairs). This probe asks the most favourable version of that question:

  * every match changes (as in the ten published event predictions);
  * the size of each change is resampled from those ten published pairs;
  * the with-news probabilities are assumed to be EXACTLY the truth (results are drawn from them).

Base rates/shifts are recovered from reports/news-live-0.4.0.json by fitting each published arm to a
Dixon-Coles grid (same recovery as check_live_events.py). Scoring and the interval use the package's
own football_model.score_probabilities and experiment_statistics.clustered_t.

Usage: PYTHONPATH=src python probe_power.py reports/news-live-0.4.0.json [--seasons 4000]
"""

from __future__ import annotations

import argparse
import json
import math

import numpy as np
from scipy.optimize import least_squares
from scipy.stats import norm, poisson, t as student

from polymarket861 import experiment_statistics, football_model

KEYS = ("home_win", "draw", "away_win", "total_over_2_5", "home_over_1_5", "away_over_1_5")
LINES = (0.5, 1.5, 2.5, 3.5)
SIZE = 13  # outcomes above 12 goals for one side carry < 1e-9 of the probability here


def six(lam, mu, rho, n=60):
    k = np.arange(n)
    m = np.outer(poisson.pmf(k, lam), poisson.pmf(k, mu))
    m[0, 0] *= 1 - rho * lam * mu
    m[0, 1] *= 1 + rho * lam
    m[1, 0] *= 1 + rho * mu
    m[1, 1] *= 1 - rho
    m /= m.sum()
    h, a = np.indices(m.shape)
    return np.array([m[h > a].sum(), m[h == a].sum(), m[h < a].sum(),
                     m[h + a > 2.5].sum(), m[h > 1.5].sum(), m[a > 1.5].sum()])


def solve(target):
    fit = least_squares(lambda x: six(*x) - target, (1.4, 1.2, -0.05), xtol=1e-15, ftol=1e-15, gtol=1e-15)
    return fit.x


def arm(lam, mu, rho):
    grid, _ = football_model._goals_matrix(lam, mu, rho)
    probs, _ = football_model.probabilities(grid, LINES)
    pmf = np.zeros((SIZE, SIZE))
    n = min(SIZE, grid.shape[0])
    pmf[:n, :n] = np.asarray(grid)[:n, :n]
    return probs, pmf / pmf.sum()


def score_table(probs):
    return np.array([[football_model.score_probabilities(probs, h, a)["score"] for a in range(SIZE)]
                     for h in range(SIZE)])


def simulate(types, truth, seasons, rounds, rng, family=10):
    """types: list of (delta_table, pmf_no_news, pmf_with_news). truth: 'with_news' or 'no_news'."""
    wins = losses = 0
    means = []
    for _ in range(seasons):
        clusters = []
        for _ in range(rounds):
            picks = rng.integers(0, len(types), 10)
            values = []
            for i in picks:
                delta, base, news = types[i]
                pmf = news if truth == "with_news" else base
                cell = rng.choice(SIZE * SIZE, p=pmf.ravel())
                values.append([delta.ravel()[cell]])
            clusters.append(values)
        interval = experiment_statistics.clustered_t(clusters, family)["intervals"][0]
        wins += interval["lower"] > 0
        losses += interval["upper"] < 0
        means.append(interval["difference"])
    return {"p_higher_score_verdict": wins / seasons, "p_lower_score_verdict": losses / seasons,
            "mean_observed_difference": float(np.mean(means)), "sd_of_season_difference": float(np.std(means))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--seasons", type=int, default=4000)
    args = parser.parse_args()
    report = json.load(open(args.report))
    rng = np.random.default_rng(861)

    def build(scale):
        types, gains, variances = [], [], []
        for event in report["event_predictions"]:
            probs = {p["market"]: p for p in event["probabilities"]}
            l0, m0, rho = solve(np.array([probs[k]["without_news"] for k in KEYS]))
            l1, m1, _ = solve(np.array([probs[k]["with_news"] for k in KEYS]))
            dh, da = scale * math.log(l1 / l0), scale * math.log(m1 / m0)
            base_probs, base_pmf = arm(l0, m0, rho)
            news_probs, news_pmf = arm(l0 * math.exp(dh), m0 * math.exp(da), rho)
            delta = score_table(news_probs) - score_table(base_probs)
            types.append((delta, base_pmf, news_pmf))
            mean = float((delta * news_pmf).sum())
            gains.append(mean)
            variances.append(float((delta ** 2 * news_pmf).sum()) - mean ** 2)
        gain = float(np.mean(gains))
        # total variance of a random match = mean within-type variance + variance of type means
        sd = math.sqrt(float(np.mean(variances)) + float(np.var(gains)))
        return types, gain, sd

    out = {"assumptions": {
        "matches_per_round": 10, "family_comparisons": 10,
        "critical_t_38_rounds": float(student.ppf(1 - 0.025 / 10, 37)),
        "truth": "results drawn from the with-news probabilities (best case for the news arm)",
        "all_matches_changed": True}}
    for label, scale in (("published_effect_sizes", 1.0), ("effects_twice_as_large", 2.0), ("effects_four_times_as_large", 4.0)):
        types, gain, sd = build(scale)
        z = norm.ppf(1 - 0.025 / 10)
        need = {f"matches_for_{int(p * 100)}pct_power": math.ceil(((z + norm.ppf(p)) * sd / gain) ** 2) for p in (0.5, 0.8)}
        block = {
            "expected_gain_per_match_points": gain,
            "sd_of_paired_difference_per_match_points": sd,
            "expected_t_statistic_one_season": gain * math.sqrt(380) / sd,
            **need,
            "seasons_for_80pct_power": need["matches_for_80pct_power"] / 380,
            "one_season_38_rounds": simulate(types, "with_news", args.seasons, 38, rng),
        }
        if scale == 1.0:
            block["one_season_if_news_has_no_real_effect"] = simulate(types, "no_news", args.seasons, 38, rng)
        out[label] = block
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
