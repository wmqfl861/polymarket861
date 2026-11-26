#!/usr/bin/env python3
"""新闻前瞻研究 v2（epl-news-home-and-full-over-v2）的把握，用另一组随机数独立重算。

开发方随包发布了 108 格把握表（scripts/reproduce_news_endpoint_power.py，种子 861，向量化的估计函数）。
这里不用那份脚本：逐次调用产品登记后真正使用的估计函数 news_market.price_gap_slope
（按“模式 × 概率版本”组内去均值、按轮聚类、十四项校正），换一个种子，只算说明文档表里的六行。
两边的差应当落在抽样误差之内（每格 2,000 次，标准误不超过 1.1 个百分点）。

模拟的做法与上一份审核的 probe_endpoint_study_power.py 相同：
  - 新闻调整量从随仓发布的 10 场第 12 轮提前封存里重抽（主胜、全场大 2.5 各一组）；
  - 收盘相对 B1 的差 = 真实斜率 × 调整量 + 噪声，噪声方差的 5% 是整轮共有的，95% 是单场的。
全部是合成的收盘，只说明样本量够不够，不说明新闻有没有用。

用法：
  python -I probe_endpoint_v2_power.py --src <仓库>/src <仓库>/reports/news-early-seals-0.4.2.json \
      [--published <仓库>/reports/five-leagues-0.5.0a2/news-endpoint-power-v2.json] [--draws 2000]
"""

import argparse
import json
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("seals")
parser.add_argument("--published")
parser.add_argument("--draws", type=int, default=2000)
args = parser.parse_args()
sys.path.insert(0, args.src)

import numpy as np  # noqa: E402

import polymarket861  # noqa: E402
from polymarket861 import news_endpoint_study, news_endpoint_v2, news_market  # noqa: E402

FAMILY = news_endpoint_v2.FAMILY
docs = json.load(open(args.seals, encoding="utf-8"))["technical_early_seals"]
MOVES = {
    endpoint: [
        d["arms"]["with_news"]["probabilities"][native] - d["arms"]["no_news"]["probabilities"][native]
        for d in docs
    ]
    for endpoint, (native, _) in news_endpoint_study.ENDPOINTS.items()
}
LAYOUTS = {
    "one_mode_era": ["technical/era_1"],
    "four_mode_era_groups": ["technical/era_1", "technical/era_2", "formal/era_2", "formal/era_3"],
}


def strata(rounds, names):
    """Consecutive, nearly equal blocks of rounds, as the design document describes."""
    edges = np.linspace(0, rounds, len(names) + 1).round().astype(int)
    return [names[int(np.searchsorted(edges, r, side="right")) - 1] for r in range(rounds)]


def power(endpoint, rounds, per_round, sigma_pp, beta, names, rng):
    moves = np.asarray(MOVES[endpoint])
    sigma = sigma_pp / 100
    era = strata(rounds, names)
    hits = defined = 0
    for _ in range(args.draws):
        x = rng.choice(moves, size=(rounds, per_round))
        y = (
            beta * x
            + rng.normal(0, sigma * np.sqrt(0.05), (rounds, 1))
            + rng.normal(0, sigma * np.sqrt(0.95), (rounds, per_round))
        )
        points = [
            (("sim", r), era[r], float(x[r, i]), float(y[r, i]))
            for r in range(rounds)
            for i in range(per_round)
        ]
        fit = news_market.price_gap_slope(points, fixed_effects=True, family=FAMILY, no_pairs_status="none")
        if fit["interval"]:
            defined += 1
            hits += fit["interval"]["lower"] > 0
    return hits / args.draws, defined / args.draws


PUBLISHED = {}
if args.published:
    for cell in json.load(open(args.published, encoding="utf-8"))["cells"]:
        key = (cell["endpoint"], cell["layout"], cell["rounds"], cell["matches"], float(cell["assumed_gap_sd_pp"]), float(cell["assumed_slope"]))
        PUBLISHED[key] = cell["lower_bound_above_zero"]


rng = np.random.default_rng(20261126)
rows = []
for rounds, per_round, sigma, beta in ((65, 10, 3, 0.5), (65, 10, 5, 0.5), (65, 10, 5, 1.0), (65, 10, 8, 0.5), (65, 5, 5, 0.5), (27, 10, 5, 0.5)):
    for layout, names in LAYOUTS.items():
        for endpoint in MOVES:
            share, defined = power(endpoint, rounds, per_round, sigma, beta, names, rng)
            row = {
                "endpoint": endpoint,
                "layout": layout,
                "rounds": rounds,
                "fixtures_per_round": per_round,
                "assumed_gap_sd_pp": sigma,
                "assumed_slope": beta,
                "lower_bound_above_zero": round(share, 4),
                "interval_defined": round(defined, 4),
            }
            theirs = PUBLISHED.get((endpoint, layout, rounds, rounds * per_round, float(sigma), float(beta)))
            if theirs is not None:
                # Two independent estimates of the same proportion, 2,000 draws each.
                se = float(np.sqrt(share * (1 - share) / args.draws + theirs * (1 - theirs) / 2000))
                row["published_lower_bound_above_zero"] = theirs
                row["difference_pp"] = round(100 * (share - theirs), 2)
                row["difference_in_standard_errors"] = round((share - theirs) / se, 2) if se else None
            rows.append(row)

json.dump(
    {
        "package_version": polymarket861.__version__,
        "study": news_endpoint_v2.proposal()["plan"]["version"],
        "family_comparisons": FAMILY,
        "draws_per_cell": args.draws,
        "seed": 20261126,
        "estimator": "news_market.price_gap_slope(fixed_effects=True)，逐次调用",
        "movement_rms_pp": {k: round(100 * float(np.sqrt(np.mean(np.square(v)))), 4) for k, v in MOVES.items()},
        "cells": rows,
    },
    sys.stdout,
    ensure_ascii=False,
    indent=1,
)
print()
