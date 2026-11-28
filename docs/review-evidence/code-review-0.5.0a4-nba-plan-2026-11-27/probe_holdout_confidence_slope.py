#!/usr/bin/env python3
"""用产品自己的滚动回测，把 2025/26 留出季五个联赛 B1 模型的逐场概率重算出来，回答：
模型给出的胜平负概率是不是系统性地“不够自信”，以及这对“模型比价格高 3 个百分点就入选”的推荐规则意味着什么。

做法与 deploy/paper-automation/five-automation/recommendation-feed/build_calibration.py 相同
（T−60 分钟、档案滞后 24 小时、football.backtest 的 details_sink），区别是保留逐场数据，所以可以：

一、核对：逐联赛的场数和 B1 平均分是否等于已发布的基线报告（reports/five-leagues-0.5.0a2/baseline-study.json）；
    按同样的分格方法重建校准表，逐格与随仓的 calibration.json 比较场数和命中数。
二、校准斜率：对“主胜、平、客胜”三种结果各自拟合 logit(实际) = a + b·logit(模型概率)。b > 1 表示模型不够自信
    （概率大的事件实际发生得更多，概率小的事件实际发生得更少）。区间用“联赛 × 官方轮次”为单位的重抽样。
三、按模型概率分段看“某队取胜”和“小 2.5”的实际频率（取胜是主胜和客胜合在一起，每场两行），同样按轮次重抽样
    给区间；并列出随仓校准表里对应三格在五个联赛各自有没有被闸门排除。
四、已建仓方向所在的概率区间：主队不胜（模型 0.24–0.33）、客队取胜（模型 0.17–0.24）、小 2.5（模型 0.50–0.55）、
    客队不胜（模型 0.61–0.66）的实际频率。区间按“限价 + 0.03 到 限价 + 0.08”取，六个仓位的模型概率本身不在仓库里。
五、机制演示：如果一份合约的价格正好等于真实概率 π，这个模型会给出多少概率，也就是凭空多出多少“优势”。
    用第二步拟合的 a、b 把真实概率换算成模型概率：logit(模型) = (logit(π) − a) / b。
    先用三种结果合并的拟合算一遍；再按合约的种类各用对应结果的拟合算一遍（主队不胜用“主胜”的拟合取补，
    客队取胜和客队不胜用“客胜”的拟合，小 2.5 用“大 2.5”的拟合取补），区间取自第二步同一批重抽样。
六、对六个已建仓方向做同样的换算，方向反过来：模型概率取“限价 + 0.03、+ 0.055、+ 0.08”三个值，
    按对应的拟合收回去之后是多少，比限价高还是低。这就是审核稿建议的“连续校正”在这六笔上的效果。

用法（OpenFootball 固定提交 e6744429 的五联赛文件自行下载到 <raw>/<赛季>/<文件名>，
可用 ../code-review-0.4.2-2026-11-26/fetch_openfootball.py）：
  python -I probe_holdout_confidence_slope.py --src <src 目录> --raw <openfootball 目录> \
      --report <baseline-study.json> --calibration <calibration.json> <空的临时目录> [--boot 2000]

不联网，只写命令行给出的临时目录。
"""

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("--raw", required=True)
parser.add_argument("--report", required=True)
parser.add_argument("--calibration", required=True)
parser.add_argument("scratch")
parser.add_argument("--boot", type=int, default=2000)
parser.add_argument("--seed", type=int, default=861)
args = parser.parse_args()
sys.path.insert(0, args.src)

LINES = (0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5)


def replay(league):
    from polymarket861 import football, league_study

    report = json.loads(Path(args.report).read_bytes())
    season = report["assumptions"]["holdout"]
    root = Path(args.scratch) / league
    root.mkdir(parents=True)
    manifest = league_study._snapshot(Path(args.raw), root, [league])
    if manifest[league] != report["inputs"]["git_blobs"][league]:
        raise ValueError("pinned source inputs changed")
    rows = []
    scores = []

    def record(row):
        scores.append(row["model_score"])
        if row["model"] != "B1":
            return
        home, away = row["score"]
        p = row["model_probabilities"]
        rows.append({
            "league": league,
            "round": row["round"],
            "fixture": row["fixture_id"],
            "score": [home, away],
            "p_home": p["home_win"],
            "p_draw": p["draw"],
            "p_away": p["away_win"],
            "totals": {str(float(line)): v["over"] for line, v in row["totals"]["total"].items()},
        })

    history = football.load_fixtures(root, archive_lag_hours=24, league=league)
    football.backtest(
        history, season, season, horizons=(60,), bootstrap=0, archive_lag_hours=24,
        details_sink=record, league=league, lines=LINES,
    )
    return league, (rows, scores)


def logit(p):
    return math.log(p / (1 - p))


def fit(pairs):
    """两参数逻辑回归 y ~ sigmoid(a + b·x)，牛顿法。pairs 是 (x, y) 列表。"""
    a, b = 0.0, 1.0
    for _ in range(50):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y in pairs:
            mu = 1 / (1 + math.exp(-(a + b * x)))
            w = mu * (1 - mu)
            g0 += y - mu
            g1 += (y - mu) * x
            h00 += w
            h01 += w * x
            h11 += w * x * x
        det = h00 * h11 - h01 * h01
        da = (h11 * g0 - h01 * g1) / det
        db = (h00 * g1 - h01 * g0) / det
        a += da
        b += db
        if abs(da) + abs(db) < 1e-10:
            break
    return a, b


def percentile(values, q):
    values = sorted(values)
    at = q * (len(values) - 1)
    low = int(math.floor(at))
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (at - low)


def main():
    report = json.loads(Path(args.report).read_bytes())
    published = json.loads(Path(args.calibration).read_bytes())
    leagues = list(report["leagues"])
    with ProcessPoolExecutor(max_workers=min(4, len(leagues))) as pool:
        replayed = dict(pool.map(replay, leagues))
    data = {league: rows for league, (rows, _) in replayed.items()}

    # ---- 一、核对 ----
    checks = {}
    rebuilt = {league: defaultdict(lambda: [0, 0]) for league in leagues}
    for league, rows in data.items():
        frozen = report["leagues"][league]["holdout"]
        scores = replayed[league][1]
        mean = math.fsum(scores) / len(scores)
        checks[league] = {
            "matches": len(scores),
            "published_matches": frozen["matches"],
            "matches_scored_by_b1": len(rows),
            "mean_score": round(mean, 6),
            "published_mean_b1": round(frozen["mean_b1"], 6),
        }
        for r in rows:
            home, away = r["score"]
            for side, prob, truth in (
                ("home", r["p_home"], home > away), ("draw", r["p_draw"], home == away), ("away", r["p_away"], away > home),
            ):
                for outcome, value, hit in (("yes", prob, truth), ("no", 1 - prob, not truth)):
                    cell = rebuilt[league][f"moneyline.{side}.{outcome}.{min(9, int(value * 10))}"]
                    cell[0] += 1
                    cell[1] += int(hit)
            for line, over in r["totals"].items():
                for side, value, hit in (
                    ("under", 1 - over, home + away < float(line)), ("over", over, home + away > float(line)),
                ):
                    cell = rebuilt[league][f"total.{float(line):g}.{side}.{min(9, int(value * 10))}"]
                    cell[0] += 1
                    cell[1] += int(hit)
    compared = same = 0
    for league in leagues:
        bins = published["leagues"][league]["bins"]
        keys = set(bins) | set(rebuilt[league])
        for key in keys:
            compared += 1
            mine = rebuilt[league].get(key)
            theirs = bins.get(key)
            same += bool(mine and theirs and mine[0] == theirs["n"] and mine[1] == theirs["hits"])
    checks["calibration_bins_compared"] = compared
    checks["calibration_bins_identical_n_and_hits"] = same

    # ---- 逐场数据整理成“簇”（联赛 × 轮次）----
    clusters = defaultdict(list)
    for league, rows in data.items():
        for r in rows:
            clusters[(league, r["round"])].append(r)
    keys = sorted(clusters)
    rng = random.Random(args.seed)

    def resamples():
        for _ in range(args.boot):
            yield [row for _ in keys for row in clusters[keys[rng.randrange(len(keys))]]]

    everything = [row for key in keys for row in clusters[key]]

    def outcome_pairs(rows, which):
        out = []
        for r in rows:
            home, away = r["score"]
            if which == "over_2.5":
                out.append((logit(r["totals"]["2.5"]), int(home + away > 2.5)))
                continue
            if which in ("home", "all"):
                out.append((logit(r["p_home"]), int(home > away)))
            if which in ("draw", "all"):
                out.append((logit(r["p_draw"]), int(home == away)))
            if which in ("away", "all"):
                out.append((logit(r["p_away"]), int(away > home)))
        return out

    # ---- 二、校准斜率 ----
    slopes = {}
    boots = {name: [] for name in ("home", "draw", "away", "all", "over_2.5")}
    for sample in resamples():
        for name in boots:
            boots[name].append(fit(outcome_pairs(sample, name)))
    for name in boots:
        a, b = fit(outcome_pairs(everything, name))
        slopes[name] = {
            "rows": len(outcome_pairs(everything, name)),
            "intercept": round(a, 4),
            "slope": round(b, 4),
            "slope_95": [round(percentile([x[1] for x in boots[name]], q), 4) for q in (0.025, 0.975)],
            "share_of_resamples_with_slope_above_1": round(sum(x[1] > 1 for x in boots[name]) / args.boot, 4),
        }

    # ---- 三、四：按模型概率分段 ----
    def frequency(rows, select):
        n = hits = 0
        total = 0.0
        for r in rows:
            for prob, hit in select(r):
                n += 1
                hits += hit
                total += prob
        return n, hits, total

    def segment(select):
        n, hits, total = frequency(everything, select)
        gaps = []
        for sample in resamples():
            bn, bh, bt = frequency(sample, select)
            if bn:
                gaps.append(bh / bn - bt / bn)
        return {
            "rows": n,
            "model": round(total / n, 4),
            "observed": round(hits / n, 4),
            "observed_minus_model": round(hits / n - total / n, 4),
            "difference_95": [round(percentile(gaps, q), 4) for q in (0.025, 0.975)],
        }

    def wins_between(low, high):
        def select(r):
            home, away = r["score"]
            out = []
            if low <= r["p_home"] < high:
                out.append((r["p_home"], int(home > away)))
            if low <= r["p_away"] < high:
                out.append((r["p_away"], int(away > home)))
            return out
        return select

    def home_not_win_between(low, high):
        def select(r):
            home, away = r["score"]
            value = 1 - r["p_home"]
            return [(value, int(home <= away))] if low <= value < high else []
        return select

    def away_win_between(low, high):
        def select(r):
            home, away = r["score"]
            return [(r["p_away"], int(away > home))] if low <= r["p_away"] < high else []
        return select

    def under_between(low, high):
        def select(r):
            home, away = r["score"]
            value = 1 - r["totals"]["2.5"]
            return [(value, int(home + away < 2.5))] if low <= value < high else []
        return select

    bands = {
        "team_win_under_0.20": segment(wins_between(0.0, 0.20)),
        "team_win_0.20_to_0.40": segment(wins_between(0.20, 0.40)),
        "team_win_0.40_to_0.60": segment(wins_between(0.40, 0.60)),
        "team_win_0.60_and_above": segment(wins_between(0.60, 1.01)),
    }
    position_regions = {
        "home_not_win_model_0.24_to_0.33": segment(home_not_win_between(0.24, 0.33)),
        "home_not_win_model_0.20_to_0.40": segment(home_not_win_between(0.20, 0.40)),
        "away_win_model_0.17_to_0.24": segment(away_win_between(0.17, 0.24)),
        "away_win_model_0.10_to_0.25": segment(away_win_between(0.10, 0.25)),
        "under_2.5_model_0.50_to_0.55": segment(under_between(0.50, 0.55)),
    }
    totals = {
        "under_2.5_model_0.60_to_0.70": segment(under_between(0.60, 0.70)),
        "under_2.5_model_0.50_to_0.60": segment(under_between(0.50, 0.60)),
        "under_2.5_model_0.40_to_0.50": segment(under_between(0.40, 0.50)),
        "under_2.5_model_0.30_to_0.40": segment(under_between(0.30, 0.40)),
    }

    # 第六个已建仓方向（客队不胜，限价 0.58）。放在最后计算，前面各段用到的随机数序列不变。
    def away_not_win_between(low, high):
        def select(r):
            home, away = r["score"]
            value = 1 - r["p_away"]
            return [(value, int(away <= home))] if low <= value < high else []
        return select

    position_regions["away_not_win_model_0.61_to_0.66"] = segment(away_not_win_between(0.61, 0.66))
    position_regions["away_not_win_model_0.60_to_0.70"] = segment(away_not_win_between(0.60, 0.70))

    gate = {}
    for key in ("total.2.5.under.6", "moneyline.home.no.3", "moneyline.home.yes.6"):
        gate[key] = {}
        for league in leagues:
            cell = published["leagues"][league]["bins"].get(key)
            gate[key][league] = None if cell is None else {
                "n": cell["n"],
                "observed_minus_model": round(cell["residual"], 4),
                "interval": None if cell["adjusted_ci"] is None else [round(x, 3) for x in cell["adjusted_ci"]],
                "excluded_by_rule": cell["adjusted_ci"] is not None and cell["adjusted_ci"][1] < 0,
            }

    # ---- 五、机制演示 ----
    a, b = slopes["all"]["intercept"], slopes["all"]["slope"]
    illusion = []
    for true in (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80):
        model = 1 / (1 + math.exp(-((logit(true) - a) / b)))
        illusion.append({"fair_price": true, "model_probability": round(model, 4), "apparent_edge": round(model - true, 4)})

    # ---- 五（续）：按合约种类，用对应结果的拟合；区间取自第二步已经存下的重抽样，不再抽样 ----
    fits = {name: fit(outcome_pairs(everything, name)) for name in boots}

    def sigmoid(x):
        return 1 / (1 + math.exp(-x))

    def phantom(name, complement, prices):
        out = []
        for price in prices:
            def edge(pair, price=price):
                a, b = pair
                if complement:
                    return (1 - sigmoid((logit(1 - price) - a) / b)) - price
                return sigmoid((logit(price) - a) / b) - price
            draws = [edge(pair) for pair in boots[name]]
            out.append({
                "fair_price": price,
                "model_probability": round(edge(fits[name]) + price, 4),
                "apparent_edge": round(edge(fits[name]), 4),
                "apparent_edge_95": [round(percentile(draws, q), 4) for q in (0.025, 0.975)],
                "share_of_resamples_with_apparent_edge_at_least_0.03": round(sum(d >= 0.03 for d in draws) / len(draws), 4),
            })
        return out

    by_contract = {
        "home_side_does_not_win": phantom("home", True, (0.15, 0.20, 0.25, 0.30)),
        "away_side_wins": phantom("away", False, (0.10, 0.15, 0.20, 0.25)),
        "away_side_does_not_win": phantom("away", True, (0.55, 0.60, 0.65)),
        "under_2.5": phantom("over_2.5", True, (0.45, 0.50, 0.55, 0.58, 0.60, 0.65)),
        "over_2.5": phantom("over_2.5", False, (0.35, 0.40, 0.45, 0.50, 0.55)),
    }

    # ---- 六、六个已建仓方向按拟合收回去 ----
    def corrected(name, complement, model_value):
        def truth(pair):
            a, b = pair
            value = sigmoid(a + b * logit(1 - model_value if complement else model_value))
            return 1 - value if complement else value
        draws = [truth(pair) for pair in boots[name]]
        return truth(fits[name]), [round(percentile(draws, q), 4) for q in (0.025, 0.975)]

    opened = []
    for label, name, complement, limit in (
        ("罗马 vs 蒙扎：罗马不胜", "home", True, 0.21),
        ("国际米兰 vs 热那亚：国际米兰不胜", "home", True, 0.25),
        ("弗罗西诺内 vs 帕尔马：帕尔马胜", "away", False, 0.16),
        ("利兹联 vs 考文垂：考文垂胜", "away", False, 0.14),
        ("布雷斯特 vs 巴黎 FC：巴黎 FC 不胜", "away", True, 0.58),
        ("富勒姆 vs 伯恩茅斯：小 2.5", "over_2.5", True, 0.47),
    ):
        entry = {"position": label, "limit": limit, "fit_used": name, "by_model_probability": []}
        for step in (0.03, 0.055, 0.08):
            point, interval = corrected(name, complement, limit + step)
            entry["by_model_probability"].append({
                "model_probability": round(limit + step, 3),
                "calibrated_probability": round(point, 4),
                "calibrated_95": interval,
                "calibrated_minus_limit": round(point - limit, 4),
            })
        opened.append(entry)

    json.dump(
        {
            "holdout": report["assumptions"]["holdout"],
            "checks": checks,
            "clusters": len(keys),
            "calibration_slope": slopes,
            "team_win_bands": bands,
            "regions_of_opened_positions": position_regions,
            "total_2.5_bands": totals,
            "per_league_gate_on_three_bins": gate,
            "apparent_edge_on_a_fairly_priced_contract": illusion,
            "apparent_edge_by_contract_type": by_contract,
            "opened_positions_after_calibration": opened,
        },
        sys.stdout,
        ensure_ascii=False,
        indent=1,
    )
    print()


if __name__ == "__main__":
    main()
