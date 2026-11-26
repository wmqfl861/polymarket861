#!/usr/bin/env python3
"""新登记的新闻前瞻研究（epl-news-home-and-full-over-v1）在最好情况下有多大把握。

研究对十项比较做 Bonferroni 校正，到 2028-07-01 结束，英超还剩大约 27 + 38 = 65 轮。
这里按开发方自己的盘口研究模拟（scripts/reproduce_news_market_study.py）同样的做法：
  - 新闻调整量从随仓发布的 10 场第 12 轮提前封存里重抽（主胜、全场大 2.5 各一组）；
  - 收盘相对 B1 的差 = 真实斜率 × 调整量 + 噪声，噪声方差的 5% 是整轮共有的，95% 是单场的；
  - 估计量直接调用产品的 news_market.price_gap_slope（过原点、按轮聚类、十项校正）。
“有把握”指校正后的区间下界大于零。全部是合成的收盘，只说明样本量够不够，不说明新闻有没有用。

用法：
  python -I probe_endpoint_study_power.py --src <仓库>/src <仓库>/reports/news-early-seals-0.4.2.json [--draws 2000]
"""

import argparse
import json
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("seals")
parser.add_argument("--draws", type=int, default=2000)
args = parser.parse_args()
sys.path.insert(0, args.src)

import numpy as np  # noqa: E402

import polymarket861  # noqa: E402
from polymarket861 import news_endpoint_study, news_market  # noqa: E402

FAMILY = news_endpoint_study.FAMILY
docs = json.load(open(args.seals, encoding="utf-8"))["technical_early_seals"]
MOVES = {
    endpoint: [
        d["arms"]["with_news"]["probabilities"][native] - d["arms"]["no_news"]["probabilities"][native]
        for d in docs
    ]
    for endpoint, (native, _) in news_endpoint_study.ENDPOINTS.items()
}


def power(endpoint, rounds, per_round, sigma_pp, beta, rng):
    moves = np.asarray(MOVES[endpoint])
    sigma = sigma_pp / 100
    hits = defined = 0
    for _ in range(args.draws):
        x = rng.choice(moves, size=(rounds, per_round))
        y = (
            beta * x
            + rng.normal(0, sigma * np.sqrt(0.05), (rounds, 1))
            + rng.normal(0, sigma * np.sqrt(0.95), (rounds, per_round))
        )
        points = [
            (("sim", r), "era", float(x[r, i]), float(y[r, i]))
            for r in range(rounds)
            for i in range(per_round)
        ]
        fit = news_market.price_gap_slope(points, family=FAMILY, no_pairs_status="none")
        if fit["interval"]:
            defined += 1
            hits += fit["interval"]["lower"] > 0
    return hits / args.draws, defined / args.draws


rng = np.random.default_rng(861)
cells = []
for endpoint in MOVES:
    for rounds, per_round, label in (
        (27, 10, "本赛季余下 27 轮，每轮 10 场全部合格"),
        (65, 10, "到 2028 年 6 月共 65 轮，每轮 10 场全部合格（上限）"),
        (65, 5, "65 轮，每轮只有 5 场合格"),
    ):
        for sigma in (3, 5, 8):
            for beta in (0.0, 0.5, 1.0):
                share, defined = power(endpoint, rounds, per_round, sigma, beta, rng)
                cells.append(
                    {
                        "endpoint": endpoint,
                        "sample": label,
                        "rounds": rounds,
                        "matches": rounds * per_round,
                        "assumed_gap_sd_pp": sigma,
                        "assumed_slope": beta,
                        "lower_bound_above_zero": round(share, 4),
                        "interval_defined": round(defined, 4),
                    }
                )

json.dump(
    {
        "package_version": polymarket861.__version__,
        "study": news_endpoint_study.PLAN["version"],
        "family_comparisons": FAMILY,
        "draws_per_cell": args.draws,
        "seed": 861,
        "movement_source": "reports/news-early-seals-0.4.2.json，10 场第 12 轮提前封存",
        "movement_rms_pp": {k: round(100 * float(np.sqrt(np.mean(np.square(v)))), 4) for k, v in MOVES.items()},
        "movement_pp": {k: [round(100 * m, 3) for m in v] for k, v in MOVES.items()},
        "cells": cells,
    },
    sys.stdout,
    ensure_ascii=False,
    indent=1,
)
print()
