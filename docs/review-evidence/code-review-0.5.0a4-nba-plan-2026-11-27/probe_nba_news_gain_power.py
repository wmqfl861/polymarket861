#!/usr/bin/env python3
"""NBA 计划 VALIDATION.md“新闻究竟有没有增益”一节的判定规则，在一个赛季里能有多大把握。

计划写的规则（第 5 条）：T−60m 上 N4 与 N3 的胜方 Brier 配对平均差 D = 损失(N4) − 损失(N3)，
“主差值 95% 区间上界小于 0 且点估计改善至少 0.002，才对胜方宣布新闻增益”；第 3 条把 0.002 定为
最小关注差，并要求在这个效应量上把握达到 80%。

本探针不读任何文件、不联网，只做算术和一次模拟核对。设定如下，全部写在输出里：

  * 每场的 N3 主胜概率 p：对数几率服从正态 N(0.58, 0.90²)。这两个数是公开文件 nbaallelo.csv 里
    2006–2015 年 12,053 场常规赛 Elo 主胜率的均值和标准差（同目录 probe_nba_public_scores.py 的数据）。
  * 一场比赛以概率 f“有新闻”。有新闻时真实主胜概率变成 q = p ± s·4p(1−p)，正负各半（所以 N3 仍然校准，
    只是不知道这条消息）；N4 完全正确地吸收了消息，直接给出 q。没有新闻时两臂相同，这一场的差值为 0。
    这是对 N4 最有利的设定：现实中的新闻臂还会有误读、过度反应和重复计入。
  * 比赛结果 y 按 q 抽取。每场差值 D = (q−y)² − (p−y)²，期望是 −(q−p)²。
  * 给定“真实平均改善 δ”和 f，反解 s，使全体比赛上 E[D] 正好等于 −δ。
  * 另一种终点：不拿赛果打分，而拿开赛前最后一份合格市场概率 c 打分，c = q + 噪声，噪声标准差 σ_e。
    σ_e 代表“模型与收盘概率之间、与这条新闻无关的差距”（模型本身的误差加上报价噪声），取 0.03、0.05、0.08 三档。
    这时每场差值是 (q−c)² − (p−c)²，期望同样是 −(q−p)²，方差小得多。

判定规则：
  规则甲（计划原文）：平均差的 95% 区间上界 < 0，并且点估计 ≤ −0.002。
  规则乙：只要求 95% 区间上界 < 0。

把握按“一个赛季的平均差近似正态、各场独立”计算，并对三个格子做整季模拟核对。真实数据里同一比赛日
的比赛相关、区块重抽样的区间通常更宽，所以这里的把握应当看作上限。

用法：
  python -I probe_nba_news_gain_power.py [--seasons 4000] [--seed 861]
"""

import argparse
import json
import math
import random
import sys
from statistics import NormalDist

parser = argparse.ArgumentParser()
parser.add_argument("--seasons", type=int, default=4000)
parser.add_argument("--seed", type=int, default=861)
args = parser.parse_args()

NORMAL = NormalDist()
Z975 = NORMAL.inv_cdf(0.975)
Z80 = NORMAL.inv_cdf(0.80)
MCID = 0.002
LOGIT_MEAN, LOGIT_SD = 0.58, 0.90
SEASON = 1230

# N3 概率的离散网格：4,001 个等距标准正态点，按密度加权。
GRID = []
total = 0.0
for k in range(4001):
    z = -5 + 10 * k / 4000
    weight = math.exp(-z * z / 2)
    p = 1 / (1 + math.exp(-(LOGIT_MEAN + LOGIT_SD * z)))
    GRID.append((p, weight))
    total += weight
GRID = [(p, w / total) for p, w in GRID]
MEAN_PQ = sum(w * p * (1 - p) for p, w in GRID)
MEAN_W2 = sum(w * (4 * p * (1 - p)) ** 2 for p, w in GRID)
MEAN_W1 = sum(w * 4 * p * (1 - p) for p, w in GRID)


def shift_for(delta, share):
    """有新闻的比赛里，五五开的比赛概率移动多少（s），才能让全体平均改善等于 delta。"""
    return math.sqrt(delta / (share * MEAN_W2))


def moments(delta, share, closing_sd=None):
    """每场差值 D 的均值和标准差。closing_sd 为 None 时用赛果打分，否则用带噪声的收盘概率打分。"""
    s = shift_for(delta, share)
    first = second = 0.0
    for p, w in GRID:
        for sign in (1, -1):
            q = p + sign * s * 4 * p * (1 - p)
            gap = q - p
            if closing_sd is None:
                # y=1 的差值为 gap·(q+p−2)，y=0 的差值为 gap·(q+p)。
                e1 = gap * (q * (q + p - 2) + (1 - q) * (q + p))
                e2 = gap * gap * (q * (q + p - 2) ** 2 + (1 - q) * (q + p) ** 2)
            else:
                e1 = -gap * gap
                e2 = gap ** 4 + 4 * gap * gap * closing_sd ** 2
            first += share * 0.5 * w * e1
            second += share * 0.5 * w * e2
    return first, math.sqrt(second - first * first)


def power(mean, sd, games, rule):
    se = sd / math.sqrt(games)
    if rule == "plan":
        threshold = max(MCID, Z975 * se)
    else:
        threshold = Z975 * se
    return NORMAL.cdf((-mean - threshold) / se)


def games_for_80(mean, sd, rule):
    interval_only = ((Z975 + Z80) * sd / -mean) ** 2
    if rule != "plan":
        return math.ceil(interval_only)
    if -mean <= MCID:
        return None  # 点估计过 0.002 这一条的概率不超过一半，多少场都到不了 80%。
    return math.ceil(max(interval_only, (Z80 * sd / (-mean - MCID)) ** 2))


def effect_for_80(games, share, rule, closing_sd=None):
    low, high = 1e-6, 0.05
    for _ in range(60):
        mid = (low + high) / 2
        mean, sd = moments(mid, share, closing_sd)
        if power(mean, sd, games, rule) >= 0.80:
            high = mid
        else:
            low = mid
    return high


def simulate(delta, share, games, seasons, rng, closing_sd=None):
    """整季模拟核对：逐场抽样，用样本标准差做正态区间，数两条规则各通过多少次。"""
    s = shift_for(delta, share)
    plan = interval = 0
    for _ in range(seasons):
        total = squares = 0.0
        for _ in range(games):
            if rng.random() >= share:
                continue
            p = 1 / (1 + math.exp(-(LOGIT_MEAN + LOGIT_SD * rng.gauss(0, 1))))
            q = p + (1 if rng.random() < 0.5 else -1) * s * 4 * p * (1 - p)
            if closing_sd is None:
                y = 1.0 if rng.random() < q else 0.0
                d = (q - y) ** 2 - (p - y) ** 2
            else:
                c = q + rng.gauss(0, closing_sd)
                d = (q - c) ** 2 - (p - c) ** 2
            total += d
            squares += d * d
        mean = total / games
        se = math.sqrt(max(squares / games - mean * mean, 0.0) / games)
        upper = mean + Z975 * se
        interval += upper < 0
        plan += upper < 0 and mean <= -MCID
    return {"seasons": seasons, "plan_rule": round(plan / seasons, 3), "interval_only": round(interval / seasons, 3)}


out = {
    "assumptions": {
        "base_home_win_logit_mean_sd": [LOGIT_MEAN, LOGIT_SD],
        "mean_p_times_1_minus_p": round(MEAN_PQ, 4),
        "news_arm": "perfect: N4 equals the true probability; N3 calibrated but uninformed",
        "games_independent": True,
        "mcid": MCID,
        "one_season_games": SEASON,
    },
    "what_0.002_means": [],
    "outcome_target": [],
    "closing_target": [],
    "share_sensitivity_at_0.002": [],
    "effect_needed_for_80_percent": {},
    "simulation_check": {},
}

# 一、0.002 的平均改善，换算成“多少比例的比赛、概率移动多少”。
for share in (0.05, 0.10, 0.20, 0.50, 1.00):
    s = shift_for(MCID, share)
    out["what_0.002_means"].append({
        "share_of_games_with_news": share,
        "shift_in_an_even_game": round(s, 4),
        "mean_absolute_shift_in_news_games": round(s * MEAN_W1, 4),
    })

# 二、用赛果打分：不同真实效应、不同场数下两条规则的把握。
for delta in (0.0002, 0.0005, 0.001, 0.002, 0.003, 0.004, 0.006):
    mean, sd = moments(delta, 0.20)
    row = {"true_mean_improvement": delta, "per_game_sd": round(sd, 5)}
    for games in (1230, 1730, 2460, 3690):
        row[f"n{games}"] = {
            "plan_rule": round(power(mean, sd, games, "plan"), 3),
            "interval_only": round(power(mean, sd, games, "interval"), 3),
        }
    row["games_for_80_plan_rule"] = games_for_80(mean, sd, "plan")
    row["games_for_80_interval_only"] = games_for_80(mean, sd, "interval")
    out["outcome_target"].append(row)

# 三、用收盘概率打分。
for closing_sd in (0.03, 0.05, 0.08):
    for delta in (0.0001, 0.0002, 0.0005, 0.001, 0.002):
        mean, sd = moments(delta, 0.20, closing_sd)
        out["closing_target"].append({
            "closing_noise_sd": closing_sd,
            "true_mean_improvement": delta,
            "per_game_sd": round(sd, 5),
            "n1230_interval_only": round(power(mean, sd, 1230, "interval"), 3),
            "games_for_80_interval_only": games_for_80(mean, sd, "interval"),
        })

# 四、同样的 0.002，新闻集中在少数比赛还是分散在多数比赛，把握差多少。
for share in (0.05, 0.10, 0.20, 0.50):
    mean, sd = moments(MCID, share)
    out["share_sensitivity_at_0.002"].append({
        "share_of_games_with_news": share,
        "per_game_sd": round(sd, 5),
        "n1230_plan_rule": round(power(mean, sd, 1230, "plan"), 3),
        "n1230_interval_only": round(power(mean, sd, 1230, "interval"), 3),
    })

# 五、一个赛季要有 80% 的把握，真实效应得有多大。
out["effect_needed_for_80_percent"] = {
    "n1230_plan_rule": round(effect_for_80(1230, 0.20, "plan"), 5),
    "n1230_interval_only": round(effect_for_80(1230, 0.20, "interval"), 5),
    "n1730_plan_rule": round(effect_for_80(1730, 0.20, "plan"), 5),
    "n1730_interval_only": round(effect_for_80(1730, 0.20, "interval"), 5),
    "n1230_closing_sd_0.05_interval_only": round(effect_for_80(1230, 0.20, "interval", 0.05), 6),
    "n1230_closing_sd_0.08_interval_only": round(effect_for_80(1230, 0.20, "interval", 0.08), 6),
}

# 六、整季模拟核对三个格子。
rng = random.Random(args.seed)
for label, delta, closing_sd in (
    ("outcome_0.002_n1230", 0.002, None),
    ("outcome_0.004_n1230", 0.004, None),
    ("closing_sd_0.05_0.0005_n1230", 0.0005, 0.05),
):
    mean, sd = moments(delta, 0.20, closing_sd)
    out["simulation_check"][label] = {
        "formula": {
            "plan_rule": round(power(mean, sd, SEASON, "plan"), 3),
            "interval_only": round(power(mean, sd, SEASON, "interval"), 3),
        },
        "simulated": simulate(delta, 0.20, SEASON, args.seasons, rng, closing_sd),
    }

json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
print()
