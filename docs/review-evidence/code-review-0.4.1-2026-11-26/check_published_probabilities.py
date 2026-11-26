"""Independent recomputation of every published 0.4.1 news probability.

Reads only the two public JSON reports. Does not import the project package.
For each arm it rebuilds a Dixon-Coles corrected independent-Poisson score grid from the
published lambda_home, lambda_away and rho, sums 1X2 and every over/under direction, and
compares with the stored numbers. It then summarises how far the with-news arm moved.

Usage: python docs/review-evidence/code-review-0.4.1-2026-11-26/check_published_probabilities.py reports/news-early-seals-0.4.1.json reports/news-event-probabilities-0.4.1.json
"""

import json
import math
import sys
from statistics import fmean

MAX_GOALS = 40  # far beyond the publisher's adaptive grid; tail mass < 1e-30 at these rates


def grid(lh, la, rho):
    ph = [math.exp(-lh) * lh**k / math.factorial(k) for k in range(MAX_GOALS + 1)]
    pa = [math.exp(-la) * la**k / math.factorial(k) for k in range(MAX_GOALS + 1)]
    g = [[ph[i] * pa[j] for j in range(MAX_GOALS + 1)] for i in range(MAX_GOALS + 1)]
    g[0][0] *= 1 - lh * la * rho
    g[0][1] *= 1 + lh * rho
    g[1][0] *= 1 + la * rho
    g[1][1] *= 1 - rho
    total = sum(map(sum, g))
    return [[v / total for v in row] for row in g]


def recompute(arm, lines):
    r = arm["rates"]
    g = grid(r["lambda_home"], r["lambda_away"], r["rho"])
    n = range(MAX_GOALS + 1)
    out = {
        "home_win": sum(g[i][j] for i in n for j in n if i > j),
        "draw": sum(g[i][i] for i in n),
        "away_win": sum(g[i][j] for i in n for j in n if i < j),
    }
    home = [sum(g[i]) for i in n]
    away = [sum(g[i][j] for i in n) for j in n]
    tot = [0.0] * (2 * MAX_GOALS + 1)
    for i in n:
        for j in n:
            tot[i + j] += g[i][j]
    totals = {}
    for name, dist in (("total", tot), ("home", home), ("away", away)):
        totals[name] = {}
        for line in lines:
            over = sum(p for k, p in enumerate(dist) if k > line)
            totals[name][str(line)] = {"over": over, "under": 1 - over}
    out["total_over_2_5"] = totals["total"]["2.5"]["over"]
    out["home_over_1_5"] = totals["home"]["1.5"]["over"]
    out["away_over_1_5"] = totals["away"]["1.5"]["over"]
    return out, totals


def main(paths):
    report = {}
    for path in paths:
        doc = json.load(open(path))
        rows = doc.get("technical_early_seals") or doc.get("events")
        worst, values, moves = 0.0, 0, []
        for row in rows:
            for name, arm in row["arms"].items():
                probs, totals = recompute(arm, row["lines"])
                for key, stored in arm["probabilities"].items():
                    worst = max(worst, abs(stored - probs[key]))
                    values += 1
                for fam, by_line in arm["totals"].items():
                    for line, sides in by_line.items():
                        for side, stored in sides.items():
                            worst = max(worst, abs(stored - totals[fam][line][side]))
                            values += 1
            a, b = row["arms"]["no_news"], row["arms"]["with_news"]
            moves.append({
                "fixture": row["fixture_id"].split("/", 2)[2],
                "kickoff": row["kickoff"],
                "applied_claims": row["applied_claims"],
                "home_win_no_news_pct": round(100 * a["probabilities"]["home_win"], 3),
                "home_win_with_news_pct": round(100 * b["probabilities"]["home_win"], 3),
                "home_win_move_pp": round(100 * (b["probabilities"]["home_win"] - a["probabilities"]["home_win"]), 3),
                "over25_move_pp": round(100 * (b["probabilities"]["total_over_2_5"] - a["probabilities"]["total_over_2_5"]), 3),
                "home_log_shift": round(math.log(b["rates"]["lambda_home"] / a["rates"]["lambda_home"]), 6),
                "away_log_shift": round(math.log(b["rates"]["lambda_away"] / a["rates"]["lambda_away"]), 6),
                "rho_equal": a["rates"]["rho"] == b["rates"]["rho"],
            })
        report[path.rsplit("/", 1)[-1]] = {
            "records": len(rows),
            "stored_values_compared": values,
            "max_abs_difference": worst,
            "mean_abs_home_win_move_pp": round(fmean(abs(m["home_win_move_pp"]) for m in moves), 3),
            "max_abs_home_win_move_pp": max(abs(m["home_win_move_pp"]) for m in moves),
            "mean_abs_over25_move_pp": round(fmean(abs(m["over25_move_pp"]) for m in moves), 3),
            "applied_claims_total": sum(m["applied_claims"] for m in moves),
            "policy_sha256": sorted({r["policy_sha256"] for r in rows}),
            "recipe_sha256": sorted({r["recipe_sha256"] for r in rows}),
            "fixtures": moves,
        }
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
