#!/usr/bin/env python3
"""Independent recount of docs/five-leagues-0.5/evidence/market-season-2025-26.tsv.

Reads only the table and the pinned OpenFootball 2025-26 files. It cannot reach Polymarket, so it
checks what is checkable offline: that every regular-season fixture of the five leagues appears
exactly once, that the per-league coverage counts match the plan, that identifiers are unique, and
where in the season the gaps fall.

Usage: python -I check_market_evidence.py <openfootball dir> <market-season-2025-26.tsv> [polymarket.json]
"""
from __future__ import annotations

import collections
import csv
import json
import sys
from pathlib import Path

LEAGUES = {"epl": "en", "lal": "es", "bun": "de", "sea": "it", "fl1": "fr"}


def main() -> None:
    root, table = Path(sys.argv[1]), Path(sys.argv[2])
    rows = list(csv.DictReader(table.open(encoding="utf-8", newline=""), delimiter="\t"))
    out: dict = {"rows": len(rows), "columns": list(rows[0].keys())}

    by_league = collections.defaultdict(list)
    for row in rows:
        by_league[row["league"]].append(row)
    out["league_codes"] = sorted(by_league)

    fixtures = {}
    for code, prefix in LEAGUES.items():
        data = json.loads((root / "2025-26" / f"{prefix}.1.json").read_text(encoding="utf-8"))
        fixtures[code] = {(m["date"], m["round"], m["team1"], m["team2"]) for m in data["matches"]}

    coverage = {}
    mismatched = []
    for code in LEAGUES:
        league_rows = by_league.get(code, [])
        keys = [(r["date"], r["round"], r["home"], r["away"]) for r in league_rows]
        missing = sorted(fixtures[code] - set(keys))
        extra = sorted(set(keys) - fixtures[code])
        pairs_source = {(k[2], k[3]) for k in fixtures[code]}
        pairs_table = {(k[2], k[3]) for k in keys}
        coverage[code] = {
            "source_fixtures": len(fixtures[code]),
            "table_rows": len(league_rows),
            "duplicate_rows": len(keys) - len(set(keys)),
            "rows_not_in_source_by_date_round_pair": len(extra),
            "source_fixtures_not_in_table": len(missing),
            "ordered_pairs_match_source": pairs_source == pairs_table,
            "result_1x2": sum(r["result_1x2"] == "1" for r in league_rows),
            "total": sum(r["total"] == "1" for r in league_rows),
            "home_total": sum(r["home_total"] == "1" for r in league_rows),
            "away_total": sum(r["away_total"] == "1" for r in league_rows),
            "status": dict(collections.Counter(r["status"] for r in league_rows)),
            "total_line": dict(collections.Counter(r["total_line"] for r in league_rows)),
            "main_offset": dict(collections.Counter(r["main_offset"] for r in league_rows)),
        }
        mismatched.extend({"league": code, "row": e} for e in extra[:20])
    out["coverage"] = coverage
    out["all"] = {k: sum(c[k] for c in coverage.values()) for k in ("table_rows", "result_1x2", "total", "home_total", "away_total")}
    out["date_or_round_differences_vs_source_sample"] = mismatched[:40]

    # identifier consistency
    def nonempty(field):
        return [r[field] for r in rows if r[field]]

    ident = {}
    for field in ("gamma_main_slug", "home_gamma_market_id", "draw_gamma_market_id", "away_gamma_market_id", "draw_condition_id", "total_gamma_market_id", "total_condition_id"):
        values = nonempty(field)
        ident[field] = {"present": len(values), "distinct": len(set(values))}
    out["identifiers"] = ident
    out["flag_vs_identifier"] = {
        "result_1x2_flag_without_three_market_ids": sum(r["result_1x2"] == "1" and not (r["home_gamma_market_id"] and r["draw_gamma_market_id"] and r["away_gamma_market_id"]) for r in rows),
        "three_market_ids_without_flag": sum(r["result_1x2"] != "1" and bool(r["home_gamma_market_id"]) for r in rows),
        "total_flag_without_market_id": sum(r["total"] == "1" and not r["total_gamma_market_id"] for r in rows),
        "total_market_id_without_flag": sum(r["total"] != "1" and bool(r["total_gamma_market_id"]) for r in rows),
        "total_flag_without_line": sum(r["total"] == "1" and not r["total_line"] for r in rows),
        "slug_date_differs_from_fixture_date": sum(bool(r["gamma_main_slug"]) and not r["gamma_main_slug"].endswith(r["date"]) for r in rows),
        "rows_with_other_main_candidates": sum(bool(r["other_main_candidates"]) for r in rows),
    }

    # where the gaps are in time
    gaps = {}
    for code in LEAGUES:
        league_rows = sorted(by_league.get(code, []), key=lambda r: r["date"])
        no_main = [r for r in league_rows if r["result_1x2"] != "1"]
        no_total = [r for r in league_rows if r["total"] != "1"]
        with_total = [r for r in league_rows if r["total"] == "1"]
        first_total = with_total[0]["date"] if with_total else None
        after_first = [r for r in league_rows if first_total and r["date"] >= first_total]
        gaps[code] = {
            "fixtures_without_1x2": [(r["date"], r["round"], r["home"], r["away"], r["status"]) for r in no_main],
            "first_fixture_date_with_total": first_total,
            "last_fixture_date_without_total": max((r["date"] for r in no_total), default=None),
            "fixtures_on_or_after_first_total": len(after_first),
            "of_which_with_total": sum(r["total"] == "1" for r in after_first),
            "without_total_by_month": dict(sorted(collections.Counter(r["date"][:7] for r in no_total).items())),
            "with_total_by_month": dict(sorted(collections.Counter(r["date"][:7] for r in with_total).items())),
        }
    out["gaps"] = gaps

    if len(sys.argv) > 3:
        evidence = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
        out["polymarket_json_top_level_keys"] = list(evidence.keys())
    json.dump(out, sys.stdout, indent=1, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
