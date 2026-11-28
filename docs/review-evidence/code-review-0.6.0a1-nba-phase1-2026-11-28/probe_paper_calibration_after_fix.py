#!/usr/bin/env python3
"""上轮 W1-2 的复验：足球模拟账户的推荐脚本改用“连续校正”之后，原来高估的几个方向还高估多少。

0.5.0a4 的推荐列表用的是分格闸门；本版（deploy/paper-automation/five-automation/recommendation-feed/
paper_calibration.py）改成四条合并五联赛的逻辑校正曲线，胜平负三条各自校正后再归一。曲线的系数就是上轮
审核稿在 2025/26 留出季上拟合出来的那四组，所以这里是“样本内”复验：同一批 1,745 场，用产品自己的滚动
回测重算逐场 B1 概率（与上轮 probe_holdout_confidence_slope.py 相同的做法），再直接调用部署脚本里的
calibrated() 得到校正后的概率，与实际频率比较。

回答三个问题：
  一、校正之后，各方向的概率与实际频率差多少（整体，和上轮点名的几个概率区间）；
  二、胜平负“各自校正再归一”与“只用该方向自己那条曲线”差多少；
  三、对校正后的概率再拟合一次 logit(实际) = a + b·logit(校正后)，b 是否回到 1 附近。

区间用“联赛 × 官方轮次”为单位的重抽样。

用法（OpenFootball 固定提交的五联赛文件自行下载，见上一个证据目录的 README）：
  python -I probe_paper_calibration_after_fix.py --src <src 目录> --raw <openfootball 目录> \
      --report <reports/five-leagues-0.5.0a2/baseline-study.json> --feed <recommendation-feed 目录> <空的临时目录>

不联网，只写命令行给出的临时目录。
"""

import argparse
import importlib.util
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
parser.add_argument("--feed", required=True)
parser.add_argument("scratch")
parser.add_argument("--boot", type=int, default=2000)
parser.add_argument("--seed", type=int, default=861)
args = parser.parse_args()
sys.path.insert(0, str(Path(args.src).resolve()))

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

    def record(row):
        if row["model"] != "B1":
            return
        p = row["model_probabilities"]
        rows.append({"league": league, "round": row["round"], "score": list(row["score"]),
                     "home": p["home_win"], "draw": p["draw"], "away": p["away_win"],
                     "over": row["totals"]["total"][2.5]["over"] if 2.5 in row["totals"]["total"]
                     else row["totals"]["total"]["2.5"]["over"]})

    history = football.load_fixtures(root, archive_lag_hours=24, league=league)
    football.backtest(history, season, season, horizons=(60,), bootstrap=0, archive_lag_hours=24,
                      details_sink=record, league=league, lines=LINES)
    return league, rows


def logit(p):
    return math.log(p / (1 - p))


def fit(pairs):
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
        da, db = (h11 * g0 - h01 * g1) / det, (h00 * g1 - h01 * g0) / det
        a, b = a + da, b + db
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
    leagues = list(report["leagues"])
    with ProcessPoolExecutor(max_workers=min(4, len(leagues))) as pool:
        data = dict(pool.map(replay, leagues))

    spec = importlib.util.spec_from_file_location("paper_calibration", Path(args.feed) / "paper_calibration.py")
    feed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(feed)

    def direct(p, key):
        a, b = map(float, feed.FITS[key])
        return 1 / (1 + math.exp(-(a + b * logit(p))))

    clusters = defaultdict(list)
    for league, rows in data.items():
        for r in rows:
            arm = {"probabilities": {"home_win": repr(r["home"]), "draw": repr(r["draw"]), "away_win": repr(r["away"])},
                   "totals": {"total": {"2.5": {"over": repr(r["over"])}}}}
            home, away = r["score"]
            happened = {"home": home > away, "draw": home == away, "away": away > home}
            r["directions"] = {}
            for selection in ("home", "draw", "away"):
                for side in ("yes", "no"):
                    terms = {"family": "result", "selection": selection, "side": side}
                    raw = r[selection] if side == "yes" else 1 - r[selection]
                    own = direct(r[selection], selection)
                    r["directions"][f"{selection}_{side}"] = {
                        "raw": raw, "deployed": float(feed.calibrated(arm, terms)),
                        "own_curve_only": own if side == "yes" else 1 - own,
                        "hit": int(happened[selection] == (side == "yes"))}
            for side in ("over", "under"):
                terms = {"family": "total", "line": "2.5", "side": side}
                raw = r["over"] if side == "over" else 1 - r["over"]
                value = float(feed.calibrated(arm, terms))
                r["directions"][f"{side}_2.5"] = {"raw": raw, "deployed": value, "own_curve_only": value,
                                                  "hit": int((home + away > 2.5) == (side == "over"))}
            clusters[(league, r["round"])].append(r)
    keys = sorted(clusters)
    everything = [row for key in keys for row in clusters[key]]
    rng = random.Random(args.seed)
    samples = [[row for _ in keys for row in clusters[keys[rng.randrange(len(keys))]]] for _ in range(args.boot)]

    def region(direction, low, high):
        def stats(rows):
            chosen = [r["directions"][direction] for r in rows if low <= r["directions"][direction]["raw"] < high]
            if not chosen:
                return None
            n = len(chosen)
            return (n, sum(c["raw"] for c in chosen) / n, sum(c["deployed"] for c in chosen) / n,
                    sum(c["own_curve_only"] for c in chosen) / n, sum(c["hit"] for c in chosen) / n)
        n, raw, deployed, own, observed = stats(everything)
        gaps = [s[4] - s[2] for s in map(stats, samples) if s]
        return {"rows": n, "model_before_calibration": round(raw, 4), "deployed_calibration": round(deployed, 4),
                "own_curve_without_renormalising": round(own, 4), "observed": round(observed, 4),
                "observed_minus_before": round(observed - raw, 4),
                "observed_minus_deployed": round(observed - deployed, 4),
                "observed_minus_deployed_95": [round(percentile(gaps, q), 4) for q in (0.025, 0.975)]}

    out = {"holdout": report["assumptions"]["holdout"], "matches": len(everything), "clusters": len(keys),
           "fits_in_deployed_script": feed.FITS, "bootstrap": args.boot}
    out["whole_range"] = {d: region(d, 0.0, 1.01) for d in everything[0]["directions"]}
    out["regions_named_in_previous_review"] = {
        "home_not_win_model_0.24_to_0.33": region("home_no", 0.24, 0.33),
        "home_not_win_model_0.20_to_0.40": region("home_no", 0.20, 0.40),
        "away_win_model_0.17_to_0.24": region("away_yes", 0.17, 0.24),
        "away_win_model_0.10_to_0.25": region("away_yes", 0.10, 0.25),
        "away_not_win_model_0.61_to_0.66": region("away_no", 0.61, 0.66),
        "away_not_win_model_0.60_to_0.70": region("away_no", 0.60, 0.70),
        "under_2.5_model_0.50_to_0.55": region("under_2.5", 0.50, 0.55),
        "under_2.5_model_0.60_to_0.70": region("under_2.5", 0.60, 0.70),
        "home_win_model_0.60_and_above": region("home_yes", 0.60, 1.01),
    }
    refit = {}
    for direction in ("home_yes", "draw_yes", "away_yes", "over_2.5"):
        def pairs(rows, which):
            return [(logit(r["directions"][direction][which]), r["directions"][direction]["hit"]) for r in rows]
        cell = {}
        for which in ("raw", "deployed", "own_curve_only"):
            a, b = fit(pairs(everything, which))
            boots = [fit(pairs(s, which))[1] for s in samples[:400]]
            cell[which] = {"intercept": round(a, 4), "slope": round(b, 4),
                           "slope_95": [round(percentile(boots, q), 4) for q in (0.025, 0.975)]}
        refit[direction] = cell
    out["recalibration_slope_of_each_version"] = refit
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
