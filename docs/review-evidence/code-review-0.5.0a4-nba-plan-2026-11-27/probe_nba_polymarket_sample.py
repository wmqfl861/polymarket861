#!/usr/bin/env python3
"""核对 NBA 计划随附的 Polymarket 样本（docs/nba-plan/POLYMARKET-SAMPLE.csv，计划分支 0c3534a）。

回答两件事：
一、POLYMARKET.md 那张表里的数（11 场、55 个合约、110 个令牌；各玩法“有卖盘 / 两边有报价 / 卖档累计够
    50 美元”的令牌数；文件摘要）和这份 CSV 对不对得上；互补令牌的档位数是否互为镜像。
    另外看抽样规则（同类多条 consensus 时取市场编号最小的一条）选到的线离五五开多远。
二、按样本里的最优卖价和计划写的费率（份数 × 0.05 × 价格 × (1−价格)，只对吃单方收），买入一份要比
    “两侧最优卖价归一后的概率”多付多少。这个数就是纸面策略在每种玩法上要先跨过的门槛。

只读命令行给出的这一份 CSV，不联网。样本是 2026-11-27 13:25 UTC 的一个时点，不代表 T−60m 的盘口。

用法：
  python -I probe_nba_polymarket_sample.py <POLYMARKET-SAMPLE.csv 的路径>
"""

import csv
import hashlib
import json
import sys
from collections import defaultdict
from decimal import Decimal
from statistics import median

FEE_RATE = Decimal("0.05")
path = sys.argv[1]
raw = open(path, "rb").read()
rows = list(csv.DictReader(raw.decode("utf-8").splitlines()))

by_condition = defaultdict(list)
for row in rows:
    by_condition[row["condition_id"]].append(row)

structure = {
    "sha256": hashlib.sha256(raw).hexdigest(),
    "data_rows": len(rows),
    "events": len({r["event_slug"] for r in rows}),
    "conditions": len(by_condition),
    "tokens": len({r["token_id"] for r in rows}),
    "conditions_with_exactly_two_tokens": sum(len(v) == 2 for v in by_condition.values()),
    "events_with_all_five_families": sum(
        len({r["family"] for r in rows if r["event_slug"] == slug}) == 5
        for slug in {r["event_slug"] for r in rows}
    ),
    "lines_not_on_half_point": sorted({r["line"] for r in rows if r["line"] and not r["line"].endswith(".5")}),
    "accepting_orders_but_empty_book_tokens": sum(
        r["accepting_orders"] == "1" and r["bid_levels"] == "0" and r["ask_levels"] == "0" for r in rows
    ),
    "mirrored_level_counts": sum(
        len(v) == 2 and v[0]["bid_levels"] == v[1]["ask_levels"] and v[0]["ask_levels"] == v[1]["bid_levels"]
        for v in by_condition.values()
    ),
    "crossed_or_locked_books": sum(
        bool(r["best_bid"]) and bool(r["best_ask"]) and Decimal(r["best_bid"]) >= Decimal(r["best_ask"]) for r in rows
    ),
    "book_timestamp_minutes": sorted({r["book_timestamp_utc"][11:16] for r in rows}),
}

families = {}
for family in ("win", "full.total", "full.team_total.home", "full.team_total.away", "full.spread"):
    part = [r for r in rows if r["family"] == family]
    conditions = defaultdict(list)
    for r in part:
        conditions[r["condition_id"]].append(r)
    ask_sums, spreads, costs, fees, top_usd, off_centre = [], [], [], [], [], []
    for pair in conditions.values():
        first = pair[0]
        if first["best_bid"] and first["best_ask"]:
            mid = (Decimal(first["best_bid"]) + Decimal(first["best_ask"])) / 2
            off_centre.append((abs(mid - Decimal("0.5")), first["event_slug"], first["line"], float(mid)))
        if all(r["best_ask"] for r in pair):
            asks = [Decimal(r["best_ask"]) for r in pair]
            total = sum(asks)
            ask_sums.append(total)
            for ask in asks:
                fee = FEE_RATE * ask * (1 - ask)
                fees.append(fee)
                # 归一概率 = 该侧卖价 / 两侧卖价之和；多付 = 卖价 + 费用 − 归一概率。
                costs.append(ask + fee - ask / total)
        for r in pair:
            if r["best_bid"] and r["best_ask"]:
                spreads.append(Decimal(r["best_ask"]) - Decimal(r["best_bid"]))
            if r["best_ask"]:
                top_usd.append(Decimal(r["best_ask"]) * Decimal(r["best_ask_shares"]))
    families[family] = {
        "tokens": len(part),
        "tokens_with_asks": sum(r["ask_levels"] != "0" for r in part),
        "tokens_two_sided": sum(r["ask_levels"] != "0" and r["bid_levels"] != "0" for r in part),
        "tokens_all_asks_usd_ge_50": sum(r["all_asks_usd_ge_50"] == "1" for r in part),
        "empty_book_tokens": sum(r["ask_levels"] == "0" and r["bid_levels"] == "0" for r in part),
        "conditions_with_both_asks": len(ask_sums),
        "median_ask_levels_when_quoted": (
            median(int(r["ask_levels"]) for r in part if r["ask_levels"] != "0") if top_usd else None
        ),
        "median_best_ask_usd": float(round(median(top_usd), 2)) if top_usd else None,
        "ask_sum_min_median_max": [float(min(ask_sums)), float(median(ask_sums)), float(max(ask_sums))] if ask_sums else None,
        "median_bid_ask_spread": float(median(spreads)) if spreads else None,
        "median_fee_per_share": float(round(median(fees), 5)) if fees else None,
        "median_cost_over_normalised_probability": float(round(median(costs), 5)) if costs else None,
        "max_cost_over_normalised_probability": float(round(max(costs), 5)) if costs else None,
        # 抽样规则取的是“同类 consensus 里市场编号最小的一条”。这条线离五五开有多远：
        "sampled_lines_with_mid_at_least_0.08_from_even": None if family == "win" else [
            {"event": slug, "line": line, "first_outcome_mid": mid}
            for gap, slug, line, mid in sorted(off_centre, reverse=True) if gap >= Decimal("0.08")
        ],
    }

# 结果的排列次序：胜负盘第一行是哪一队，让分盘第一行又是哪一队（按 CSV 行序）。
order = {"spread_first_outcome_is_moneyline_first_team": [], "spread_first_outcome_is_moneyline_second_team": []}
for slug in sorted({r["event_slug"] for r in rows}):
    win = [r["outcome"] for r in rows if r["event_slug"] == slug and r["family"] == "win"]
    spread = [r for r in rows if r["event_slug"] == slug and r["family"] == "full.spread"]
    key = "spread_first_outcome_is_moneyline_first_team" if spread[0]["outcome"] == win[0] else "spread_first_outcome_is_moneyline_second_team"
    order[key].append(f"{slug}: {spread[0]['outcome']} {spread[0]['line']}")
structure["outcome_order"] = {
    "moneyline_first_team_matches_first_slug_code_note": "胜负盘第一行与赛事 slug 的第一个队码都是客队（计划 POLYMARKET.md 的说明）",
    **order,
}

json.dump({"structure": structure, "families": families}, sys.stdout, ensure_ascii=False, indent=1)
print()
