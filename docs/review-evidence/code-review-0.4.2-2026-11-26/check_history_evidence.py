"""Recount the five-league history evidence of the 0.5 plan from the pinned OpenFootball files.

Independent of the project package and of the plan's own tooling. Reads:
  - a directory with the pinned files as <dir>/<YYYY-YY>/<league>.1.json
    (commit e6744429ee395bc86f247348c6184bb08d4eb361 of openfootball/football.json)
  - docs/five-leagues-0.5/evidence/history.json
and compares every count it can recompute.

Usage: python -I check_history_evidence.py <openfootball dir> <history.json>
"""

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

LEAGUES = ("en", "es", "de", "it", "fr")
SEASONS = ("2021-22", "2022-23", "2023-24", "2024-25", "2025-26")
ROUND = re.compile(r"^Matchday ([1-9][0-9]?)$")
CLOCK = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
# Known award that the archive does not label (DFB sports court, Bochum v Gladbach, 2022-03-18).
UNLABELLED_AWARDS = {("de", "2021-22", "2022-03-18", "VfL Bochum")}


def rounds_for(league, season):
    if league == "de":
        return 34
    if league == "fr":
        return 38 if season in ("2021-22", "2022-23") else 34
    return 38


def score_of(match):
    raw = match.get("score")
    shape = None
    if isinstance(raw, list):
        shape, raw = "list", raw
    elif isinstance(raw, dict) and isinstance(raw.get("ft"), list):
        shape, raw = "object", raw["ft"]
    else:
        return None, None
    if len(raw) == 2 and all(type(v) is int and 0 <= v <= 50 for v in raw):
        return tuple(raw), shape
    return None, None


def git_blob_sha1(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def profile(scores):
    n = len(scores)
    home = sum(h for h, _ in scores)
    away = sum(a for _, a in scores)
    counts = {
        "home_win": sum(h > a for h, a in scores),
        "draw": sum(h == a for h, a in scores),
        "away_win": sum(h < a for h, a in scores),
        "full_over_2_5": sum(h + a >= 3 for h, a in scores),
        "home_over_1_5": sum(h >= 2 for h, _ in scores),
        "away_over_1_5": sum(a >= 2 for _, a in scores),
    }
    return {"n": n, "goals_home": home, "goals_away": away, "counts": counts,
            "mean_goals_home": round(home / n, 4), "mean_goals_away": round(away / n, 4),
            "pct": {k: round(100 * v / n, 2) for k, v in counts.items()}}


def main(root, evidence_path):
    root = Path(root)
    evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    problems = []

    def expect(label, mine, theirs):
        if mine != theirs:
            problems.append({"item": label, "recounted": mine, "evidence": theirs})

    # 1. Inventory of 85 target files and their git blob identities.
    present = 0
    for league in LEAGUES:
        listed = evidence["inventory_2010_11_to_2026_27"][league]
        mine = {}
        for year in range(2010, 2027):
            season = f"{year}-{(year + 1) % 100:02d}"
            path = root / season / f"{league}.1.json"
            if path.exists():
                mine[season] = git_blob_sha1(path.read_bytes())
        present += len(mine)
        expect(f"inventory.{league}.blobs", mine, listed["files_git_blob_sha1"])
        expect(f"inventory.{league}.missing", sorted(set(f"{y}-{(y + 1) % 100:02d}" for y in range(2010, 2027)) - mine.keys()),
               listed["missing_seasons"])
    expect("inventory.present_files", present, evidence["all_25_files"]["available_files_in_17_year_query"])

    # 2. The 25 completed league seasons.
    by_file = {(row["league"], row["season"]): row for row in evidence["five_seasons"]}
    league_scores = defaultdict(list)
    totals = Counter()
    zero_zero_shapes = Counter()
    for league in LEAGUES:
        for season in SEASONS:
            raw = (root / season / f"{league}.1.json").read_bytes()
            matches = json.loads(raw)["matches"]
            planned = rounds_for(league, season)
            regular = [m for m in matches if (g := ROUND.match(m.get("round", ""))) and 1 <= int(g[1]) <= planned]
            teams = sorted({m["team1"] for m in regular} | {m["team2"] for m in regular})
            per_round = defaultdict(list)
            for m in regular:
                per_round[m["round"]] += [m["team1"], m["team2"]]
            pairs = Counter((m["team1"], m["team2"]) for m in regular)
            valid = awarded = unlabelled = clocks = 0
            shapes = Counter()
            for m in regular:
                clocks += bool(isinstance(m.get("time"), str) and CLOCK.match(m["time"]))
                score, shape = score_of(m)
                if score is None:
                    continue
                valid += 1
                shapes[shape] += 1
                if score == (0, 0):
                    zero_zero_shapes[shape] += 1
                if m.get("status") == "awarded":
                    awarded += 1
                    continue
                if (league, season, m.get("date"), m["team1"].split(" 1848")[0]) in UNLABELLED_AWARDS:
                    unlabelled += 1
                    continue
                league_scores[league].append(score)
            mine = {
                "file_sha256": hashlib.sha256(raw).hexdigest(),
                "git_blob_sha1": git_blob_sha1(raw),
                "team_count": len(teams),
                "round_count": len(per_round),
                "expected_regular_pairs": len(teams) * (len(teams) - 1),
                "source_regular_rows": len(regular),
                "valid_raw_scores": valid,
                "missing_or_invalid_score": len(regular) - valid,
                "valid_score_source_awarded": awarded,
                "additional_known_award_without_source_status": unlabelled,
                "descriptive_n": valid - awarded - unlabelled,
                "raw_clock_HH_MM": clocks,
                "raw_total_rows": len(matches),
                "list_form_valid": shapes["list"],
                "object_form_valid": shapes["object"],
            }
            theirs = by_file[league, season]
            for key, value in mine.items():
                expect(f"{season}/{league}.{key}", value, theirs.get(key))
            checks = {
                "all_planned_rounds": len(per_round) == planned,
                "each_team_once_per_round": all(sorted(v) == teams for v in per_round.values()),
                "every_ordered_pair_once": len(pairs) == len(teams) * (len(teams) - 1) and set(pairs.values()) == {1},
            }
            if not all(checks.values()):
                problems.append({"item": f"{season}/{league}.schedule", "recounted": checks})
            for key in ("expected_regular_pairs", "source_regular_rows", "valid_raw_scores", "missing_or_invalid_score",
                        "valid_score_source_awarded", "additional_known_award_without_source_status",
                        "descriptive_n", "raw_clock_HH_MM"):
                totals[key] += mine[key]
    for key, value in totals.items():
        expect(f"all_25_files.{key}", value, evidence["all_25_files"][key])

    # 3. Descriptive profile per league.
    profiles = {}
    for league in LEAGUES:
        mine = profile(league_scores[league])
        theirs = evidence["league_totals"][league]["descriptive_profile"]
        for key in ("n", "goals_home", "goals_away", "mean_goals_home", "mean_goals_away"):
            expect(f"profile.{league}.{key}", mine[key], theirs[key])
        for key, value in mine["counts"].items():
            expect(f"profile.{league}.counts.{key}", value, theirs["counts"][key])
        profiles[league] = mine

    # 4. The in-season 2026/27 snapshot.
    running = {}
    for league in LEAGUES:
        matches = json.loads((root / "2026-27" / f"{league}.1.json").read_bytes())["matches"]
        regular = [m for m in matches if ROUND.match(m.get("round", ""))]
        mine = {
            "raw_regular_rows": len(regular),
            "supplied_HH_MM": sum(bool(isinstance(m.get("time"), str) and CLOCK.match(m["time"])) for m in regular),
            "valid_score_rows_at_frozen_commit": sum(score_of(m)[0] is not None for m in regular),
        }
        theirs = evidence["running_2026_27_at_source_commit"][league]
        for key, value in mine.items():
            expect(f"2026-27/{league}.{key}", value, theirs[key])
        running[league] = mine

    print(json.dumps({
        "files_present_of_85": present,
        "five_season_totals": dict(totals),
        "profiles": profiles,
        "zero_zero_results_by_score_shape": dict(zero_zero_shapes),
        "running_2026_27": running,
        "differences_from_evidence": problems,
    }, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
