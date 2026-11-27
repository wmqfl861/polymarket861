#!/usr/bin/env python3
"""推荐列表（deploy/paper-automation/five-automation/recommendation-feed/discover.py）选出来的方向，
和它自己带的留出季校准表（同目录 calibration.json，2025/26 赛季 1,751 场）对照。

读码得到的规则：
  * 一个方向要入选，模型的保守公允值要比买入成本高至少 0.03，买入价不高于外部赔率去水后的概率；
  * 校准表按“联赛 × 玩法 × 方向 × 模型概率十分位”分格。只有当某一格的残差（实际频率 − 模型概率）
    的校正区间上界 < 0 时才排除这个方向（discover.py: `if history["adjusted_ci"][1] < 0: return None`）。

本探针回答三个问题：
一、这道校准闸门有多大的分辨力：一共多少格、每格多少场、区间多宽、实际排除了多少格。
二、把五个联赛合并起来看，模型在哪些格子里系统性地高估或低估（合并后每格的样本够大）。
三、prepare-slate.py 里写死的六个已建仓方向，各自落在哪几格；这些格子合并后的实际频率和买入限价比是高是低。
    六个仓位的模型概率不在仓库里，这里按规则反推它可能落入的十分位：保守公允值 ≥ 限价 + 0.03，
    又不能比盘口中间价高出 0.08 以上，所以取 [限价+0.03, 限价+0.09] 覆盖到的十分位。

合并用的 z 值是简单二项近似，没有按比赛周聚类，只用来指出方向和量级。

用法：
  python -I probe_feed_calibration_bias.py <calibration.json 的路径>
"""

import json
import math
import sys
from collections import defaultdict
from statistics import median

data = json.load(open(sys.argv[1], encoding="utf-8"))
leagues = data["leagues"]

# ---- 一、闸门的分辨力 ----
bins = [(league, key, row) for league, body in leagues.items() for key, row in body["bins"].items()]
with_ci = [row for _, _, row in bins if row["adjusted_ci"] is not None]
excluded = [(league, key, row) for league, key, row in bins if row["adjusted_ci"] is not None and row["adjusted_ci"][1] < 0]
gate = {
    "matches": sum(body["verified_matches"] for body in leagues.values()),
    "bins": len(bins),
    "bins_with_interval": len(with_ci),
    "median_matches_per_bin": median(row["n"] for _, _, row in bins),
    "bins_under_30_matches": sum(row["n"] < 30 for _, _, row in bins),
    "median_interval_width": round(median(row["adjusted_ci"][1] - row["adjusted_ci"][0] for row in with_ci), 3),
    "bins_excluded_by_rule": len(excluded),
    "excluded_bins_with_abs_residual_under_0.01": sum(abs(row["residual"]) < 0.01 for _, _, row in excluded),
    "excluded_examples": [
        {"league": league, "bin": key, "n": row["n"], "model": round(row["mean_model"], 4), "observed": round(row["observed"], 4)}
        for league, key, row in sorted(excluded, key=lambda item: -item[2]["n"])[:5]
    ],
}

# ---- 二、五个联赛合并 ----
pooled = defaultdict(lambda: [0, 0, 0.0])
for _, key, row in bins:
    cell = pooled[key]
    cell[0] += row["n"]
    cell[1] += row["hits"]
    cell[2] += row["mean_model"] * row["n"]


def describe(n, hits, model_sum):
    model, observed = model_sum / n, hits / n
    se = math.sqrt(model * (1 - model) / n)
    return {
        "n": n,
        "model": round(model, 4),
        "observed": round(observed, 4),
        "residual": round(observed - model, 4),
        "z": round((observed - model) / se, 2) if se else None,
    }


pooled_rows = {key: describe(*cell) for key, cell in pooled.items()}
largest = sorted(
    ((key, row) for key, row in pooled_rows.items() if row["n"] >= 60 and row["z"] is not None),
    key=lambda item: -abs(item[1]["z"]),
)[:12]


def band(prefixes, low, high):
    n = hits = 0
    model_sum = 0.0
    for _, key, row in bins:
        name, decile = key.rsplit(".", 1)
        if name in prefixes and low <= int(decile) < high:
            n += row["n"]
            hits += row["hits"]
            model_sum += row["mean_model"] * row["n"]
    return describe(n, hits, model_sum)


wins = ("moneyline.home.yes", "moneyline.away.yes")
favourite_longshot = {
    "win_probability_0.60_and_above": band(wins, 6, 10),
    "win_probability_0.40_to_0.60": band(wins, 4, 6),
    "win_probability_0.20_to_0.40": band(wins, 2, 4),
    "win_probability_under_0.20": band(wins, 0, 2),
    "not_win_probability_0.20_to_0.40": band(("moneyline.home.no", "moneyline.away.no"), 2, 4),
}

# ---- 三、六个已建仓方向（prepare-slate.py 里写死的市场、方向和限价）----
POSITIONS = [
    ("sea-rom-mon-2026-11-29-rom", "sea", "moneyline.home.no", 0.21, "罗马 vs 蒙扎：罗马不胜"),
    ("sea-int-gen-2026-11-28-int", "sea", "moneyline.home.no", 0.25, "国际米兰 vs 热那亚：国际米兰不胜"),
    ("sea-fro-par-2026-11-28-par", "sea", "moneyline.away.yes", 0.16, "弗罗西诺内 vs 帕尔马：帕尔马胜"),
    ("epl-lee-cov-2026-11-28-cov", "epl", "moneyline.away.yes", 0.14, "利兹联 vs 考文垂：考文垂胜"),
    ("fl1-sbr-pfc-2026-11-27-pfc", "fl1", "moneyline.away.no", 0.58, "布雷斯特 vs 巴黎 FC：巴黎 FC 不胜"),
    ("epl-ful-bou-2026-11-29-total-2pt5", "epl", "total.2.5.under", 0.47, "富勒姆 vs 伯恩茅斯：小 2.5"),
]
positions = []
for slug, league, direction, limit, label in POSITIONS:
    deciles = sorted({min(9, int((limit + step) * 10)) for step in (0.03, 0.06, 0.09)})
    cells = []
    for decile in deciles:
        key = f"{direction}.{decile}"
        own = leagues[league]["bins"].get(key)
        cells.append({
            "bin": key,
            "pooled_five_leagues": pooled_rows.get(key),
            "own_league": None if own is None else {
                "n": own["n"],
                "model": round(own["mean_model"], 4),
                "observed": round(own["observed"], 4),
                "interval": None if own["adjusted_ci"] is None else [round(x, 3) for x in own["adjusted_ci"]],
                "excluded_by_rule": own["adjusted_ci"] is not None and own["adjusted_ci"][1] < 0,
            },
            "pooled_observed_minus_limit": None if key not in pooled_rows else round(pooled_rows[key]["observed"] - limit, 4),
        })
    positions.append({"market": slug, "direction": label, "limit": limit, "candidate_bins": cells})

json.dump(
    {
        "season": data["season"],
        "gate": gate,
        "pooled_largest_miscalibration": [{"bin": key, **row} for key, row in largest],
        "favourite_longshot": favourite_longshot,
        "opened_positions": positions,
    },
    sys.stdout,
    ensure_ascii=False,
    indent=1,
)
print()
