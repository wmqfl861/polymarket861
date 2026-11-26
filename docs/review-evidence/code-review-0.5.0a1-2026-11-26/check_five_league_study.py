#!/usr/bin/env python3
"""Independent fixture-by-fixture check of the published five-league study (0.5.0a1).

Two walk-forward implementations are run on the same pinned OpenFootball files:

  * "review": load() and walk() from the 0.4.2 review probe (probe_league_baselines.py). It was written
    against 0.4.2, before any league-aware code existed, and only uses fit_team_model, _goals_matrix,
    probabilities and score_probabilities from the package.
  * "product": polymarket861.league_study._league_replay, the code behind `pm861 leagues baseline-study`.

For every league the script reports
  - fixtures scored by both, by one only, and why the developer's register excludes the others;
  - the largest per-fixture difference in the B1 and B0 scores on the common fixtures;
  - the published aggregates recomputed from the review rows on the common fixtures;
  - the published numbers themselves (reports/five-leagues-0.5/baseline-study.json) for comparison.

The review probe deliberately did not load 2025-26 for the four new leagues. The developer has since opened
that hold-out and published it, so this script does load it.

Usage (about 40 s on four cores, BLAS single-threaded):
  OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 python -I check_five_league_study.py --src <repo>/src \
      --probe <repo>/docs/review-evidence/code-review-0.4.2-2026-11-26/probe_league_baselines.py \
      --published <repo>/reports/five-leagues-0.5/baseline-study.json <openfootball dir> <output.json>
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

CODES = {"epl": "en", "lal": "es", "bun": "de", "sea": "it", "fl1": "fr"}
DEVELOPMENT = ("2021-22", "2022-23", "2023-24", "2024-25")
HOLDOUT = "2025-26"
ENDPOINTS = ("composite", "result", "total", "home", "away")


def _probe(path):
    spec = importlib.util.spec_from_file_location("review_probe", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["review_probe"] = module
    spec.loader.exec_module(module)
    for league in module.EVALUATED:
        module.EVALUATED[league] = [*DEVELOPMENT, HOLDOUT]
    return module


def review_rows(args):
    src, probe_path, root, code = args
    sys.path.insert(0, src)
    probe = _probe(probe_path)
    from polymarket861.football import team_key

    fixtures, _ = probe.load(Path(root), CODES[code], team_key, True)
    rows, _ = probe.walk(CODES[code], fixtures, *probe.DEFAULT)
    return code, rows


def product_rows(args):
    src, root, code = args
    sys.path.insert(0, src)
    from polymarket861 import league_study

    _, rows, audit = league_study._league_replay((code, Path(root)))
    return code, [
        {"key": r.fixture_id, "season": r.season, "round": r.round_number, "week": r.match_week,
         "b1": r.model, "b0": r.baseline, "diff": list(r.differences), "fallback": r.fallback}
        for r in rows
    ], audit


def interval(rows, family, pooled):
    from polymarket861.experiment_statistics import clustered_t

    groups = collections.defaultdict(list)
    for r in rows:
        groups[r["week"] if pooled else (r["season"], r["round"])].append(r["diff"])
    e = clustered_t([groups[k] for k in sorted(groups)], family)
    return {"matches": len(rows), "clusters": len(groups),
            "mean_b1": math.fsum(r["b1"] for r in rows) / len(rows),
            "mean_b0": math.fsum(r["b0"] for r in rows) / len(rows),
            "intervals": dict(zip(ENDPOINTS, e["intervals"]))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True)
    parser.add_argument("--probe", required=True)
    parser.add_argument("--published", required=True)
    parser.add_argument("root")
    parser.add_argument("output")
    parser.add_argument("--workers", type=int, default=4)
    opts = parser.parse_args()
    sys.path.insert(0, opts.src)
    import numpy
    import scipy
    from polymarket861 import league_registry

    with ProcessPoolExecutor(opts.workers) as pool:
        review = dict(pool.map(review_rows, [(opts.src, opts.probe, opts.root, c) for c in CODES]))
        product = {c: (rows, audit) for c, rows, audit in pool.map(product_rows, [(opts.src, opts.root, c) for c in CODES])}
    published = json.loads(Path(opts.published).read_text(encoding="utf-8"))

    out = {
        "what": "review walk-forward (0.4.2 probe, no league-aware code) against the 0.5.0a1 product study, fixture by fixture",
        "environment": {"python": sys.version.split()[0], "numpy": numpy.__version__, "scipy": scipy.__version__},
        "leagues": {},
    }
    pooled_review, pooled_product = [], []
    worst = 0.0
    for code in CODES:
        mine = {}
        for r in review[code]:
            key = f"{code}/{r['key']}"
            mine[key] = {"key": key, "season": r["season"], "round": r["round"], "week": r["week"], "b1": r["b1"], "b0": r["b0"],
                         "diff": [r["b1"] - r["b0"], *(a - b for a, b in zip(r["b1_items"], r["b0_items"]))], "fallback": r["fallback"]}
        theirs = {r["key"]: r for r in product[code][0]}
        common = sorted(mine.keys() & theirs.keys())
        only_review = sorted(mine.keys() - theirs.keys())
        only_product = sorted(theirs.keys() - mine.keys())
        reasons = {}
        exceptions = {}
        for (lg, season, home, away), exc in league_registry._exceptions().items():
            if lg == code:
                from polymarket861 import football
                exceptions[football.fixture_id(season, home, away, league=code)] = exc.kind
        for key in only_review:
            reasons[key] = exceptions.get(key, "not in the developer's exception register")
        gap_b1 = max((abs(mine[k]["b1"] - theirs[k]["b1"]) for k in common), default=0.0)
        gap_b0 = max((abs(mine[k]["b0"] - theirs[k]["b0"]) for k in common), default=0.0)
        gap_items = max((abs(a - b) for k in common for a, b in zip(mine[k]["diff"], theirs[k]["diff"])), default=0.0)
        week_mismatch = [k for k in common if mine[k]["week"] != theirs[k]["week"]]
        round_mismatch = [k for k in common if mine[k]["round"] != theirs[k]["round"]]
        fallback_mismatch = [k for k in common if bool(mine[k]["fallback"]) != bool(theirs[k]["fallback"])]
        worst = max(worst, gap_b1, gap_b0, gap_items)
        block = {
            "scored_by_both": len(common), "scored_only_by_review": only_review, "why_product_excludes_them": reasons,
            "scored_only_by_product": only_product,
            "max_abs_per_fixture_difference": {"b1": gap_b1, "b0": gap_b0, "any_endpoint_b1_minus_b0": gap_items},
            "match_week_label_mismatches": week_mismatch, "round_mismatches": round_mismatch,
            "fallback_flag_mismatches": fallback_mismatch,
            "product_audit": product[code][1],
        }
        for name, seasons in (("development", DEVELOPMENT), ("holdout", (HOLDOUT,))):
            rows_review_all = [mine[k] for k in sorted(mine) if mine[k]["season"] in seasons]
            rows_product = [theirs[k] for k in sorted(theirs) if theirs[k]["season"] in seasons]
            rows_review_common = [mine[k] for k in common if mine[k]["season"] in seasons]
            pub = published["leagues"][code][name]
            block[name] = {
                "published": {"matches": pub["matches"], "clusters": pub["clusters"], "mean_b1": pub["mean_b1"],
                              "mean_b0": pub["mean_b0"], "composite": pub["intervals"]["composite"]},
                "product_recomputed_here": interval(rows_product, 10, False),
                "review_on_common_fixtures": interval(rows_review_common, 10, False),
                "review_on_its_own_fixtures": interval(rows_review_all, 10, False),
            }
        out["leagues"][code] = block
        pooled_review += [mine[k] for k in common]
        pooled_product += [theirs[k] for k in sorted(theirs)]
    out["pooled"] = {}
    for name, seasons in (("development", DEVELOPMENT), ("holdout", (HOLDOUT,))):
        pub = published["pooled"][name]
        out["pooled"][name] = {
            "published": {"matches": pub["matches"], "clusters": pub["clusters"], "intervals": pub["intervals"]},
            "product_recomputed_here": interval([r for r in pooled_product if r["season"] in seasons], 10, True),
            "review_on_common_fixtures": interval([r for r in pooled_review if r["season"] in seasons], 10, True),
        }
    out["largest_per_fixture_difference_any_league"] = worst
    out["published_gate"] = published["initial_research_gate"]
    Path(opts.output).write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    for code, block in out["leagues"].items():
        print(code, "both", block["scored_by_both"], "review-only", len(block["scored_only_by_review"]),
              "product-only", len(block["scored_only_by_product"]), "max diff", block["max_abs_per_fixture_difference"])
    print("largest per-fixture difference", worst)


if __name__ == "__main__":
    main()
