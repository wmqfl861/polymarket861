#!/usr/bin/env python3
"""球队模型在“部署机实际会喂给它的输入”下表现如何。

随仓的回放报告用 FiveThirtyEight 归档：每场都有主客/中立标志，评测前先学 8 个以上赛季。部署机上
的输入不一样：免费赛程来源的比赛对象里没有 neutral_site（官方 SDK 的 NBAGame 模型没有这个字段），
自动同步只覆盖前 14 天。这个探针用同一份归档、同一套模型函数，把这两处差别分别量出来。

做的事：
  1 fidelity      自己写一遍回放循环，汇总损失应与随仓报告逐位一致（证明下面各变体的循环与原版等价）；
                  顺带算同一批比赛上归档自带的 538 Elo 赛前概率的 Brier，作为外部参照。
  2 no_lookahead  调用仓库自己的 backtest，把某一天及以后的比分全部对调，核对该日和次日的预测不变、
                  之后的预测发生变化（滞后 1 天时，D 日和 D+1 日的预测不该用到 D 日及以后的比分）。
  3 venue_unknown 把每场的 neutral 置为未知（部署机上的情形），其余不变。
  4 cold_start    只给模型最近 14 天 / 28 天 / 本季至今 / 外加 1 个或 3 个往季的历史。
                  “shift15” 是把每队得分统一加 15 分，用来模拟今天每队约 114 分的得分水平（合成，
                  只改水平不改分差；归档时期每队约 99 分，恰好贴着模型的初值 100）。
  5 calibration   已发布配置下 N2 主胜概率的校准斜率，总分与两队得分 10%–90% 区间的实际覆盖率。

用法：
  python -I probe_nba_model_inputs.py --src <src 目录> --archive <nbaallelo.csv> [--report <随仓回放报告>]

不联网，不写任何文件，结果以 JSON 打到标准输出。
"""

import argparse
import csv
import json
import sys
from collections import deque
from datetime import date, timedelta
from itertools import groupby
from pathlib import Path
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("--archive", required=True)
parser.add_argument("--report")
args = parser.parse_args()
sys.path.insert(0, str(Path(args.src).resolve()))

import numpy as np  # noqa: E402

from polymarket861.nba import model, model_replay  # noqa: E402
from polymarket861.nba.data import parse_538  # noqa: E402

FIRST, LAST = 2005, 2014
ARMS = model.MODELS
ROWS = parse_538(Path(args.archive))


def variant_rows(*, venue_unknown=False, shift=0):
    out = []
    for row in ROWS:
        row = dict(row)
        if venue_unknown:
            row["neutral"] = None
        if shift:
            row["home_score"] += shift
            row["away_score"] += shift
        out.append(row)
    return model.prepare(out)


def evaluate(state, game, schedule, bank):
    result = model.forecast(state, game, schedule, bank)
    h, a = game["home_score"], game["away_score"]
    item = {"date": game["game_date"], "season": game["season"], "home_win": int(h > a), "total": h + a,
            "home": h, "away": a}
    for arm in ARMS:
        m = result["models"][arm]
        loss = model.score(m, h, a)
        item[arm] = {"p": m["p_home_win"], **loss, "total_mean": m["total"]["mean"],
                     "q": {k: (m[k]["q10"], m[k]["q90"]) for k in model.FAMILIES}}
    return item


def replay(games, first, last, *, lag_days=1, warmup=8):
    """与 model_replay.backtest 同构的单趟回放，返回逐场结果。"""
    games = [g for g in games if first - warmup <= g["season"] <= last]
    state = model.ScoreState()
    played, schedule, pending = {}, {}, deque()
    out, missed = {}, 0
    for day, rows_today in groupby(games, key=model.day_of):
        today = list(rows_today)
        allowed = day - timedelta(days=lag_days + 1)
        while pending and pending[0][0] <= allowed:
            _, observable = pending.popleft()
            state.learn_day(observable, played)
            model.update_dates(played, observable)
        if today[0]["season"] >= first:
            if state.total_games < model.MIN_HISTORY or (day - state.latest_result).days > 500:
                missed += len(today)
            else:
                bank = state.bank()
                for game in today:
                    out[game["game_id"]] = evaluate(state, game, schedule, bank)
        pending.append((day, today))
        model.update_dates(schedule, today)
    return out, missed


def per_season(games, warmup):
    """每个评测赛季单独起一个新状态，只给它本季和之前 warmup 个赛季。"""
    out, missed = {}, 0
    for season in range(FIRST, LAST + 1):
        part, lost = replay(games, season, season, warmup=warmup)
        out.update(part)
        missed += lost
    return out, missed


def rolling(games, window_days, *, lag_days=1):
    """每个比赛日起一个新状态，只给它之前 window_days 天内、滞后 lag_days 天可见的终场。"""
    by_day = {day: list(rows) for day, rows in groupby(games, key=model.day_of)}
    out, missed = {}, 0
    for day, today in by_day.items():
        if not FIRST <= today[0]["season"] <= LAST:
            continue
        state, played, schedule = model.ScoreState(), {}, {}
        for back in range(window_days, 0, -1):
            earlier = by_day.get(day - timedelta(days=back))
            if not earlier or earlier[0]["season"] != today[0]["season"]:
                continue
            if back >= lag_days + 1:
                state.learn_day(earlier, played)
                model.update_dates(played, earlier)
            model.update_dates(schedule, earlier)
        if state.total_games < model.MIN_HISTORY:
            missed += len(today)
            continue
        bank = state.bank()
        for game in today:
            out[game["game_id"]] = evaluate(state, game, schedule, bank)
    return out, missed


def summary(games, missed, baseline=None):
    keys = sorted(games)
    result = {"games": len(keys), "no_forecast_insufficient_history": missed}
    for arm in ARMS:
        cell = {metric: round(float(np.mean([games[k][arm][metric] for k in keys])), 5)
                for metric in ("brier_win", "crps_total", "crps_home", "crps_away")}
        bias = [games[k][arm]["total_mean"] - games[k]["total"] for k in keys]
        cell["total_mean_minus_actual"] = round(float(np.mean(bias)), 2)
        cell["mean_p_home_win"] = round(float(np.mean([games[k][arm]["p"] for k in keys])), 4)
        if baseline is not None:
            cell["paired_vs_published_config_same_games"] = {
                metric: round(float(np.mean([games[k][arm][metric] - baseline[k][arm][metric] for k in keys])), 5)
                for metric in ("brier_win", "crps_total")}
        result[arm] = cell
    result["actual_home_win_rate"] = round(float(np.mean([games[k]["home_win"] for k in keys])), 4)
    return result


def first_weeks(games, weeks=(2, 4, 8)):
    """部署后头几周的样子：按赛季内的日历周分段看 N2 的总分偏差。"""
    out = {}
    for season in sorted({g["season"] for g in games.values()}):
        dates = sorted({g["date"] for g in games.values() if g["season"] == season})
        start = date.fromisoformat(dates[0])
        for w in weeks:
            cut = (start + timedelta(days=7 * w)).isoformat()
            chosen = [g for g in games.values() if g["season"] == season and g["date"] < cut]
            out.setdefault(f"first_{w}_weeks_with_forecasts", []).extend(chosen)
    return {k: {"games": len(v),
                "N2_total_mean_minus_actual": round(float(np.mean([g["N2"]["total_mean"] - g["total"] for g in v])), 2),
                "N2_crps_total": round(float(np.mean([g["N2"]["crps_total"] for g in v])), 4),
                "N2_brier_win": round(float(np.mean([g["N2"]["brier_win"] for g in v])), 5)}
            for k, v in out.items()}


def logistic_slope(p, y):
    x = np.log(p / (1 - p))
    a, b = 0.0, 1.0
    for _ in range(50):
        z = a + b * x
        q = 1 / (1 + np.exp(-z))
        w = q * (1 - q)
        grad = np.array([np.sum(y - q), np.sum((y - q) * x)])
        hess = np.array([[np.sum(w), np.sum(w * x)], [np.sum(w * x), np.sum(w * x * x)]])
        step = np.linalg.solve(hess, grad)
        a, b = a + step[0], b + step[1]
        if abs(step).max() < 1e-10:
            break
    return round(float(a), 4), round(float(b), 4)


out = {"archive_rows": len(ROWS), "evaluated_seasons": [FIRST, LAST], "lag_days": 1}

# 1 ── 与随仓报告对齐
published = variant_rows()
base, base_missed = replay(published, FIRST, LAST)
out["fidelity"] = {"own_loop": summary(base, base_missed)}
if args.report:
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    theirs = report["pooled"]["losses"]
    mine = {arm: {m: round(float(np.mean([g[arm][m] for g in base.values()])), 7)
                  for m in model_replay.METRICS} for arm in ARMS}
    out["fidelity"]["shipped_report_pooled_losses"] = theirs
    out["fidelity"]["own_loop_pooled_losses_7dp"] = mine
    out["fidelity"]["identical_to_shipped_report"] = mine == theirs and len(base) == report["pooled"]["games"]

elo = {}
with open(args.archive, encoding="utf-8", newline="") as handle:
    for row in csv.DictReader(handle):
        if row["lg_id"] == "NBA" and row["_iscopy"] == "0":
            elo["538:" + row["game_id"]] = float(row["forecast"])
out["fidelity"]["reference_538_elo_forecast_brier_same_games"] = round(float(np.mean(
    [(elo[k] - g["home_win"]) ** 2 for k, g in base.items()])), 5)
out["fidelity"]["reference_constant_home_rate_brier"] = round(float(np.mean(
    [(np.mean([g["home_win"] for g in base.values()]) - g["home_win"]) ** 2 for g in base.values()])), 5)

# 2 ── 仓库自己的回放有没有用到未来比分
def recorded_backtest(rows, lag):
    seen = {}
    original = model.forecast

    def spy(state, fixture, prior_dates, bank, *a, **k):
        result = original(state, fixture, prior_dates, bank, *a, **k)
        seen[fixture["game_id"]] = (fixture["game_date"],
                                    tuple(result["models"][arm]["p_home_win"] for arm in ARMS),
                                    tuple(result["models"][arm]["total"]["mean"] for arm in ARMS))
        return result

    with patch.object(model, "forecast", spy):
        model_replay.backtest(rows, first_season=2013, last_season=2014, lag_days=lag, replicates=100)
    return seen


pivot = date(2014, 1, 15)
altered = []
for row in ROWS:
    row = dict(row)
    if date.fromisoformat(row["game_date"]) >= pivot:
        row["home_score"], row["away_score"] = row["away_score"], row["home_score"]
    altered.append(row)
out["no_lookahead"] = {"pivot_date": pivot.isoformat(), "alteration": "home and away scores swapped on and after the pivot"}
for lag in (1, 2):
    before, after = recorded_backtest(ROWS, lag), recorded_backtest(altered, lag)
    safe_until = (pivot + timedelta(days=lag)).isoformat()
    early = [k for k, v in before.items() if v[0] <= safe_until]
    late = [k for k, v in before.items() if v[0] > safe_until]
    out["no_lookahead"][f"lag_{lag}"] = {
        "predictions_recorded": len(before),
        "dated_on_or_before_pivot_plus_lag": len(early),
        "changed_among_those": sum(before[k] != after[k] for k in early),
        "dated_after_pivot_plus_lag": len(late),
        "changed_among_the_later_ones": sum(before[k] != after[k] for k in late),
    }

# 3 ── 赛地标志整体缺失
unknown = variant_rows(venue_unknown=True)
games, missed = replay(unknown, FIRST, LAST)
out["venue_unknown_all_rows"] = summary(games, missed, base)

# 4 ── 历史长度与得分水平
shifted = variant_rows(shift=15)
shifted_base, shifted_missed = replay(shifted, FIRST, LAST)
out["cold_start"] = {"shift15_full_history": summary(shifted_base, shifted_missed)}
cold = out["cold_start"]
for label, source, reference in (("archive_level", published, base), ("shift15", shifted, shifted_base)):
    for window in (14, 28):
        games, missed = rolling(source, window)
        cold[f"{label}_last_{window}_days_only"] = summary(games, missed, reference)
    for warmup in (0, 1, 3):
        games, missed = per_season(source, warmup)
        name = "season_to_date_only" if warmup == 0 else f"season_to_date_plus_{warmup}_prior_seasons"
        cold[f"{label}_{name}"] = summary(games, missed, reference)
        if warmup == 0:
            cold[f"{label}_{name}"]["by_weeks_since_first_forecast"] = first_weeks(games)

# 部署文档原样执行、不做回填时的样子：14 天历史、赛地未知、今天的得分水平
both = variant_rows(venue_unknown=True, shift=15)
games, missed = rolling(both, 14)
cold["shift15_venue_unknown_last_14_days_only"] = summary(games, missed, shifted_base)
games, missed = per_season(both, 0)
cold["shift15_venue_unknown_season_to_date_only"] = summary(games, missed, shifted_base)
games, missed = replay(both, FIRST, LAST)
cold["shift15_venue_unknown_full_history"] = summary(games, missed, shifted_base)

# 5 ── 已发布配置的校准
keys = sorted(base)
y = np.array([base[k]["home_win"] for k in keys], dtype=float)
calibration = {}
for arm in ("N1", "N2"):
    p = np.clip(np.array([base[k][arm]["p"] for k in keys]), 1e-6, 1 - 1e-6)
    intercept, slope = logistic_slope(p, y)
    edges = np.quantile(p, np.linspace(0, 1, 11))
    bins = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (p >= lo) & (p <= hi)
        bins.append([round(float(p[mask].mean()), 3), round(float(y[mask].mean()), 3), int(mask.sum())])
    cover = {}
    for i, family in enumerate(model.FAMILIES):
        actual = np.array([base[k][("total", "home", "away")[i]] for k in keys])
        lo = np.array([base[k][arm]["q"][family][0] for k in keys])
        hi = np.array([base[k][arm]["q"][family][1] for k in keys])
        cover[family] = {"inside_q10_q90": round(float(np.mean((actual >= lo) & (actual <= hi))), 4),
                         "below_q10": round(float(np.mean(actual < lo)), 4),
                         "above_q90": round(float(np.mean(actual > hi)), 4)}
    calibration[arm] = {"logistic_recalibration_intercept": intercept, "logistic_recalibration_slope": slope,
                        "deciles_mean_p_then_observed_then_n": bins, "interval_coverage": cover}
out["calibration_published_config"] = calibration

json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
print()
