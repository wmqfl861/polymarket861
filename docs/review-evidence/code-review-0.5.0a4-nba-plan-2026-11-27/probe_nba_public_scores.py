#!/usr/bin/env python3
"""用 NBA 计划自己引用的公开文件（FiveThirtyEight nba-elo/nbaallelo.csv，CC BY 4.0，比赛信息来自
Basketball-Reference）回答三个问题。文件不随仓库分发，运行前自行下载：
  https://raw.githubusercontent.com/fivethirtyeight/data/master/nba-elo/nbaallelo.csv

一、计划 DATA-SOURCES.md 对这份文件的描述（字节数、行数、日期范围、NBA/ABA 行数、独立比赛数、摘要）对不对。

二、“一个留出赛季”能分辨多大的模型差别。文件里每场比赛带 FiveThirtyEight 的 Elo 胜率（forecast 列），
    所以可以直接量三层的胜方 Brier：
      N0 类：只用训练季的主场胜率（常数）；
      N1 类：Elo 胜率；
      N2 类：Elo 胜率的对数几率上加“主队背靠背”“客队背靠背”两个系数（背靠背 = 前一天也有比赛，
            由文件里的日期直接算出），系数只在留出季之前的 8 个赛季上用逻辑回归拟合。
    对每个留出赛季（2006 到 2015 年结束的 10 个常规赛季）算 N1−N0、N2−N1 的配对 Brier 差，
    并按计划 VALIDATION.md 写的方法（7 个比赛日的移动区块重抽样）给 95% 区间。

三、得分分布的几个量：单队得分、总分、分差的标准差，两队得分的相关系数；以及“把分差当成正态再取整”
    会给第四节打平多大的概率（文件里没有加时标记，这一项只算模型一侧的数，不与实际加时率比较）。

用法：
  python -I probe_nba_public_scores.py <nbaallelo.csv 的路径> [--boot 2000] [--seed 861]

只读这一份文件，不联网，不读写仓库。
"""

import argparse
import csv
import hashlib
import json
import math
import random
import sys
from collections import defaultdict
from datetime import date, timedelta

parser = argparse.ArgumentParser()
parser.add_argument("path")
parser.add_argument("--boot", type=int, default=2000)
parser.add_argument("--seed", type=int, default=861)
args = parser.parse_args()

raw = open(args.path, "rb").read()
rows = list(csv.DictReader(raw.decode("utf-8").splitlines()))


def day(text):
    m, d, y = text.split("/")
    return date(int(y), int(m), int(d))


facts = {
    "bytes": len(raw),
    "sha256": hashlib.sha256(raw).hexdigest(),
    "git_blob_sha1": hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest(),
    "lines_including_header": raw.count(b"\n") + (0 if raw.endswith(b"\n") else 1),
    "data_rows": len(rows),
    "first_date": min(day(r["date_game"]) for r in rows).isoformat(),
    "last_date": max(day(r["date_game"]) for r in rows).isoformat(),
    "nba_team_rows": sum(r["lg_id"] == "NBA" for r in rows),
    "nba_distinct_game_ids": len({r["game_id"] for r in rows if r["lg_id"] == "NBA"}),
    "aba_team_rows": sum(r["lg_id"] == "ABA" for r in rows),
}

# 每场比赛只取主队视角的一行；中立场（N）不进这三层比较。
games = defaultdict(list)       # season -> [(date, home, away, p_home_elo, home_win, pts_home, pts_away)]
played = defaultdict(set)       # team -> {dates}
for r in rows:
    if r["lg_id"] != "NBA":
        continue
    played[(r["year_id"], r["team_id"])].add(day(r["date_game"]))
    if r["is_playoffs"] == "0" and r["game_location"] == "H":
        games[int(r["year_id"])].append(
            (day(r["date_game"]), r["team_id"], r["opp_id"], float(r["forecast"]),
             int(r["game_result"] == "W"), int(r["pts"]), int(r["opp_pts"]))
        )


def b2b(season, team, when):
    return int(when - timedelta(days=1) in played[(str(season), team)])


def logit(p):
    return math.log(p / (1 - p))


def fit(train):
    """逻辑回归：logit(p) = a + b*logit(elo) + c*主队背靠背 + d*客队背靠背，牛顿法。"""
    beta = [0.0, 1.0, 0.0, 0.0]
    for _ in range(40):
        grad = [0.0] * 4
        hess = [[0.0] * 4 for _ in range(4)]
        for x, y in train:
            z = sum(b * v for b, v in zip(beta, x))
            p = 1 / (1 + math.exp(-z))
            for i in range(4):
                grad[i] += (y - p) * x[i]
                for j in range(4):
                    hess[i][j] += p * (1 - p) * x[i] * x[j]
        # 解 4x4 线性方程组（高斯消元）
        a = [row[:] + [g] for row, g in zip(hess, grad)]
        for i in range(4):
            piv = max(range(i, 4), key=lambda k: abs(a[k][i]))
            a[i], a[piv] = a[piv], a[i]
            for k in range(i + 1, 4):
                f = a[k][i] / a[i][i]
                for j in range(i, 5):
                    a[k][j] -= f * a[i][j]
        step = [0.0] * 4
        for i in reversed(range(4)):
            step[i] = (a[i][4] - sum(a[i][j] * step[j] for j in range(i + 1, 4))) / a[i][i]
        beta = [b + s for b, s in zip(beta, step)]
        if max(map(abs, step)) < 1e-10:
            break
    return beta


def block_interval(by_day, rng):
    """计划写的方法：连续比赛日做 7 日移动区块重抽样，同时重抽每日损失差之和与场数，再求比值。"""
    days = sorted(by_day)
    first, last = days[0], days[-1]
    span = (last - first).days + 1
    calendar = [first + timedelta(days=i) for i in range(span)]      # 保留空比赛日
    sums = [sum(by_day.get(d, ())) for d in calendar]
    counts = [len(by_day.get(d, ())) for d in calendar]
    blocks = math.ceil(span / 7)
    stats = []
    for _ in range(args.boot):
        s = n = 0
        for _ in range(blocks):
            start = rng.randrange(span - 6)
            s += sum(sums[start:start + 7])
            n += sum(counts[start:start + 7])
        stats.append(s / n)
    stats.sort()
    return stats[int(0.025 * args.boot)], stats[int(0.975 * args.boot) - 1]


rng = random.Random(args.seed)
seasons = []
for holdout in range(2006, 2016):
    train_years = range(holdout - 8, holdout)
    base = sum(g[4] for y in train_years for g in games[y]) / sum(len(games[y]) for y in train_years)
    train = [((1.0, logit(g[3]), b2b(y, g[1], g[0]), b2b(y, g[2], g[0])), g[4])
             for y in train_years for g in games[y]]
    beta = fit(train)
    d10, d21 = defaultdict(list), defaultdict(list)
    brier = [0.0, 0.0, 0.0]
    for g in games[holdout]:
        x = (1.0, logit(g[3]), b2b(holdout, g[1], g[0]), b2b(holdout, g[2], g[0]))
        p2 = 1 / (1 + math.exp(-sum(b * v for b, v in zip(beta, x))))
        losses = [(base - g[4]) ** 2, (g[3] - g[4]) ** 2, (p2 - g[4]) ** 2]
        for i in range(3):
            brier[i] += losses[i]
        d10[g[0]].append(losses[1] - losses[0])
        d21[g[0]].append(losses[2] - losses[1])
    n = len(games[holdout])
    lo10, hi10 = block_interval(d10, rng)
    lo21, hi21 = block_interval(d21, rng)
    seasons.append({
        "holdout_season_ending": holdout, "games": n,
        "home_back_to_back_share": round(sum(b2b(holdout, g[1], g[0]) for g in games[holdout]) / n, 3),
        "away_back_to_back_share": round(sum(b2b(holdout, g[2], g[0]) for g in games[holdout]) / n, 3),
        "fitted_coefficients_intercept_elo_homeb2b_awayb2b": [round(b, 3) for b in beta],
        "brier_N0_home_rate": round(brier[0] / n, 5), "brier_N1_elo": round(brier[1] / n, 5),
        "brier_N2_elo_plus_back_to_back": round(brier[2] / n, 5),
        "N1_minus_N0": round((brier[1] - brier[0]) / n, 5), "N1_minus_N0_block_95": [round(lo10, 5), round(hi10, 5)],
        "N2_minus_N1": round((brier[2] - brier[1]) / n, 5), "N2_minus_N1_block_95": [round(lo21, 5), round(hi21, 5)],
    })

mean21 = sum(s["N2_minus_N1"] for s in seasons) / len(seasons)
half21 = sum((s["N2_minus_N1_block_95"][1] - s["N2_minus_N1_block_95"][0]) / 2 for s in seasons) / len(seasons)
mean10 = sum(s["N1_minus_N0"] for s in seasons) / len(seasons)
half10 = sum((s["N1_minus_N0_block_95"][1] - s["N1_minus_N0_block_95"][0]) / 2 for s in seasons) / len(seasons)

# 得分分布：2006 到 2015 年结束的常规赛，主队视角。
recent = [g for y in range(2006, 2016) for g in games[y]]
hp = [g[5] for g in recent]
ap = [g[6] for g in recent]


def sd(v):
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


margin = [h - a for h, a in zip(hp, ap)]
total = [h + a for h, a in zip(hp, ap)]
mh, ma = sum(hp) / len(hp), sum(ap) / len(ap)
corr = sum((h - mh) * (a - ma) for h, a in zip(hp, ap)) / (len(hp) - 1) / sd(hp) / sd(ap)
mm, sm = sum(margin) / len(margin), sd(margin)


def phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


normal_tie = phi((0.5 - mm) / sm) - phi((-0.5 - mm) / sm)
within = {k: round(sum(abs(m) <= k for m in margin) / len(margin), 4) for k in (1, 2, 3)}
normal_within = {k: round(phi((k + 0.5 - mm) / sm) - phi((-k - 0.5 - mm) / sm) - normal_tie, 4) for k in (1, 2, 3)}
distribution = {
    "games": len(recent), "home_points_mean_sd": [round(mh, 2), round(sd(hp), 2)],
    "away_points_mean_sd": [round(ma, 2), round(sd(ap), 2)],
    "total_mean_sd": [round(sum(total) / len(total), 2), round(sd(total), 2)],
    "final_margin_mean_sd": [round(mm, 2), round(sm, 2)],
    "home_away_points_correlation": round(corr, 3),
    "final_margin_exactly_zero": sum(m == 0 for m in margin),
    "share_of_finals_decided_by_at_most_k_points": within,
    "same_share_if_final_margin_were_a_rounded_normal_excluding_zero": normal_within,
    "rounded_normal_mass_at_margin_zero": round(normal_tie, 4),
    "note": "The file has no overtime flag. The rounded-normal figure is what a smooth margin model puts on a "
            "tie; real regulation ties are redistributed into the final margins by overtime.",
}

print(json.dumps({
    "file_facts": facts,
    "holdout_seasons": seasons,
    "summary": {
        "mean_N1_minus_N0": round(mean10, 5), "mean_half_width_of_one_season_95_interval_N1_minus_N0": round(half10, 5),
        "mean_N2_minus_N1": round(mean21, 5), "mean_half_width_of_one_season_95_interval_N2_minus_N1": round(half21, 5),
        "seasons_where_N2_minus_N1_interval_excludes_zero": sum(
            s["N2_minus_N1_block_95"][1] < 0 or s["N2_minus_N1_block_95"][0] > 0 for s in seasons),
        "seasons_where_N1_minus_N0_interval_excludes_zero": sum(
            s["N1_minus_N0_block_95"][1] < 0 or s["N1_minus_N0_block_95"][0] > 0 for s in seasons),
    },
    "score_distribution_2006_to_2015_regular_season": distribution,
    "bootstrap_replicates": args.boot, "seed": args.seed, "python": sys.version.split()[0],
}, indent=1, ensure_ascii=False))
