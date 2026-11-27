"""Reproduce published held-out B1 scores and summarize buying directions by probability decile."""

import hashlib
import json
import math
import os
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from scipy.stats import t
from polymarket861 import football, league_study, store

HERE = Path(__file__).resolve().parent
REPORT = HERE.parents[2] / "reports/five-leagues-0.5.0a2/baseline-study.json"
RAW = HERE.parent / "openfootball"
TARGET = "calibration.json"
LINES = (0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5)


def summarize(rows):
    n = len(rows)
    mean = math.fsum(x[0] for x in rows) / n
    hits = sum(x[1] for x in rows)
    diff = hits / n - mean
    cluster = defaultdict(float)
    for probability, observed, matchweek in rows:
        cluster[matchweek] += observed - probability - diff
    k = len(cluster)
    out = {
        "n": n,
        "hits": hits,
        "mean_model": mean,
        "observed": hits / n,
        "residual": diff,
        "clusters": k,
        "adjusted_ci": None,
    }
    if k >= 2:
        se = math.sqrt(k / (k - 1) * math.fsum(x * x for x in cluster.values())) / n
        delta = float(t.ppf(1 - 0.05 / (2 * 6), k - 1)) * se
        out["adjusted_ci"] = [diff - delta, diff + delta]
    return out


def process(league):
    report = json.loads(REPORT.read_bytes())
    season = report["assumptions"]["holdout"]
    bins = defaultdict(list)
    scores = []
    groups = set()

    def record(row):
        if row["fixture_id"] in groups:
            raise ValueError("duplicate holdout fixture")
        groups.add(row["fixture_id"])
        scores.append(row["model_score"])
        h, a = row["score"]
        p = row["model_probabilities"]
        values = []
        for k, res in (("home", h > a), ("draw", h == a), ("away", a > h)):
            prob = p["draw" if k == "draw" else k + "_win"]
            values.extend(
                [
                    (f"moneyline.{k}.yes", prob, res),
                    (f"moneyline.{k}.no", 1 - prob, not res),
                ]
            )
        for line, v in row["totals"]["total"].items():
            for side in ("under", "over"):
                truth = h + a < float(line) if side == "under" else h + a > float(line)
                values.append((f"total.{float(line):g}.{side}", v[side], truth))
        for key, prob, truth in values:
            if row["model"] == "B1":
                bins[f"{key}.{min(9, int(prob * 10))}"].append(
                    (prob, int(truth), row["round"])
                )

    with TemporaryDirectory(prefix=".calibration-", dir=HERE) as tmp:
        root = Path(tmp)
        manifest = league_study._snapshot(RAW, root, [league])
        require = report["inputs"]["git_blobs"][league]
        if manifest[league] != require:
            raise ValueError("pinned source inputs changed")
        history = football.load_fixtures(root, archive_lag_hours=24, league=league)
        football.backtest(
            history,
            season,
            season,
            horizons=(60,),
            bootstrap=0,
            archive_lag_hours=24,
            details_sink=record,
            league=league,
            lines=LINES,
        )
    frozen = report["leagues"][league]["holdout"]
    mean = math.fsum(scores) / len(scores)
    if len(scores) != frozen["matches"] or abs(mean - frozen["mean_b1"]) > 1e-8:
        raise ValueError("holdout denominator or published mean score differs")
    print(league, len(scores), flush=True)
    return league, {
        "verified_matches": len(scores),
        "verified_mean_b1": mean,
        "input_blobs": manifest[league],
        "bins": {k: summarize(rows) for k, rows in sorted(bins.items())},
    }


def main():
    report = json.loads(REPORT.read_bytes())
    leagues = list(report["leagues"])
    with ProcessPoolExecutor(max_workers=min(3, len(leagues))) as workers:
        data = dict(workers.map(process, leagues))
    result = {
        "research_protocol": report["protocol"],
        "source_report_sha256": hashlib.sha256(REPORT.read_bytes()).hexdigest(),
        "source_commit": report["inputs"]["openfootball_commit"],
        "season": report["assumptions"]["holdout"],
        "horizon_minutes": 60,
        "archive_lag_hours": 24,
        "generated_at": store.now_utc(),
        "leagues": data,
    }
    path = HERE / (TARGET + ".working")
    path.write_text(
        json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    )
    os.replace(path, HERE / TARGET)


if __name__ == "__main__":
    main()
