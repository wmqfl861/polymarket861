#!/usr/bin/env python3
"""Per-league walk-forward baselines with the unmodified 0.4.2 score engine.

What it measures, for each of the five leagues, from the pinned OpenFootball files:
  1. the score of the existing B1 recipe against the league's own B0 (same T-60 cutoff, same
     "result usable 24 h after kickoff" assumption, same three-prior-seasons training window);
  2. how far the plan's pre-registered 3x3 grid (decay 0.001/0.002/0.004 x ridge 4/8/16) moves it;
  3. what happens when club spellings that changed between seasons are not joined;
  4. the cluster standard errors needed to judge what the proposed promotion gate can detect;
  5. the plan's forward gate (composite lower bound > 0, every component lower bound >= -0.5) replayed
     on each scored season with B1 as the challenger and B0 as the incumbent, per league and pooled.

It imports fit_team_model, _goals_matrix, probabilities and score_probabilities from the installed
package and does not modify them; DECAY_PER_DAY and RIDGE are overridden in memory for grid cells only.
The training window, B0 definition and unseen-team fallback mirror football.FootballEngine. The EPL
five-season number (75.447813, published with 0.4.2) is the fidelity check of this re-implementation.

The four new leagues are scored on 2021-22..2024-25 only. 2025-26 is the plan's frozen hold-out and is
not even loaded for them, so nothing here spends it.

Usage (about 90 s on four cores; keep BLAS single-threaded, as the repository's CI does, or the workers fight):
  OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 python -I probe_league_baselines.py --src <repo>/src <openfootball dir> <output.json>
(-I keeps the downloaded data directory out of the import path, so the package location is passed with --src.)
"""
from __future__ import annotations

import argparse
import bisect
import collections
import json
import math
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ZONES = {"en": "Europe/London", "es": "Europe/Madrid", "de": "Europe/Berlin", "it": "Europe/Rome", "fr": "Europe/Paris"}
EVALUATED = {
    "en": ["2021-22", "2022-23", "2023-24", "2024-25", "2025-26"],
    "es": ["2021-22", "2022-23", "2023-24", "2024-25"],
    "de": ["2021-22", "2022-23", "2023-24", "2024-25"],
    "it": ["2021-22", "2022-23", "2023-24", "2024-25"],
    "fr": ["2021-22", "2022-23", "2023-24", "2024-25"],
}
# Older spelling -> current spelling of the same club in the pinned files (seasons 2010-11..2026-27).
# football.team_key already joins spellings that differ only by "FC"/"AFC"; these are the others.
ALIASES = {
    "es": {
        "Atlético Madrid": "Club Atlético de Madrid", "CD Alavés": "Deportivo Alavés",
        "Espanyol Barcelona": "RCD Espanyol de Barcelona", "Rayo Vallecano": "Rayo Vallecano de Madrid",
        "RC Celta": "RC Celta de Vigo", "Real Betis": "Real Betis Balompié", "Real Madrid": "Real Madrid CF",
        "Real Sociedad": "Real Sociedad de Fútbol", "Real Valladolid": "Real Valladolid CF",
        "Deportivo La Coruña": "RC Deportivo La Coruña",
    },
    "de": {
        "1899 Hoffenheim": "TSG 1899 Hoffenheim", "Bayer Leverkusen": "Bayer 04 Leverkusen",
        "Bor. Mönchengladbach": "Borussia Mönchengladbach", "Werder Bremen": "SV Werder Bremen",
        "FC St. Pauli": "FC St. Pauli 1910", "SpVgg Greuther Fürth": "SpVgg Greuther Fürth 1903",
    },
    "it": {
        "Atalanta": "Atalanta BC", "Bologna FC": "Bologna FC 1909", "Inter": "FC Internazionale Milano",
        "Lazio Roma": "SS Lazio", "Sampdoria": "UC Sampdoria", "Sassuolo Calcio": "US Sassuolo Calcio",
    },
    "fr": {
        "Olympique Marseille": "Olympique de Marseille", "RC Lens": "Racing Club de Lens",
        "RC Strasbourg": "RC Strasbourg Alsace", "Stade Rennais": "Stade Rennais FC 1901",
        "ESTAC Troyes": "ES Troyes AC",
    },
    "en": {},
}
# Results decided off the pitch. Source-labelled ones are caught by status; this one is not labelled.
UNLABELLED_AWARDS = {("de", "2021-22", "2022-03-18", "VfL Bochum 1848")}
GRID = [(d, r) for d in (0.001, 0.002, 0.004) for r in (4.0, 8.0, 16.0)]
DEFAULT = (0.002, 8.0)
LAG = timedelta(hours=24)
HORIZON = timedelta(minutes=60)
ITEMS = ("result", "total", "home", "away")


def season_year(season: str) -> int:
    return int(season[:4])


def load(root: Path, league: str, team_key, use_aliases: bool):
    """Regular-season fixtures of every season needed for the evaluated ones."""
    wanted = {f"{y}-{(y + 1) % 100:02}" for s in EVALUATED[league] for y in range(season_year(s) - 3, season_year(s) + 1)}
    zone = ZoneInfo(ZONES[league])
    aliases = ALIASES[league] if use_aliases else {}
    fixtures, names = [], collections.defaultdict(set)
    for season in sorted(wanted):
        path = root / season / f"{league}.1.json"
        rows = json.loads(path.read_text(encoding="utf-8"))["matches"]
        for row in rows:
            if not re.fullmatch(r"Matchday ([1-9][0-9]?)", row["round"]):
                continue
            day = date.fromisoformat(row["date"])
            if league == "en" and season == "2019-20" and day.year == 2019 and day.month == 7:
                day = day.replace(year=2020)  # the documented repair in football._parse_match
            clock = row.get("time")
            kickoff = datetime.combine(day, time.fromisoformat(clock) if clock else time(23, 59, 59), zone).astimezone(UTC)
            score = row.get("score")
            if isinstance(score, dict):
                score = score.get("ft")
            valid = isinstance(score, list) and len(score) == 2 and all(type(v) is int and 0 <= v <= 50 for v in score)
            status = str(row.get("status", "")).casefold()
            awarded = status == "awarded" or (league, season, row["date"], row["team1"]) in UNLABELLED_AWARDS
            home, away = (team_key(aliases.get(row[k], row[k])) for k in ("team1", "team2"))
            names[home].add(row["team1"])
            names[away].add(row["team2"])
            fixtures.append({
                "season": season, "round": int(row["round"].split()[1]), "key": f"{season}/{home}/{away}",
                "home": home, "away": away, "kickoff": kickoff, "minute": bool(clock),
                "score": tuple(score) if valid and not awarded else None, "awarded": awarded, "has_raw_score": valid,
            })
    return fixtures, names


def walk(league: str, fixtures, decay: float, ridge: float):
    """Mirror of FootballEngine.fit/forecast at T-60 for one league and one hyper-parameter cell."""
    import numpy as np
    from polymarket861 import football_model as fm

    fm.DECAY_PER_DAY, fm.RIDGE = decay, ridge
    by_season = collections.defaultdict(list)
    for f in fixtures:
        by_season[f["season"]].append(f)
    rows, fits = [], 0
    for season in EVALUATED[league]:
        y = season_year(season)
        window = [f for f in fixtures if y - 3 <= season_year(f["season"]) <= y and f["score"] is not None]
        window.sort(key=lambda f: (f["kickoff"] + LAG, f["key"]))
        times = [f["kickoff"] + LAG for f in window]
        planned = {s: len(by_season[s]) for s in by_season}
        excluded = collections.Counter(f["season"] for f in fixtures if f["awarded"])
        cache: dict[int, tuple] = {}
        targets = sorted((f for f in by_season[season] if f["score"] is not None and f["minute"]), key=lambda f: (f["kickoff"], f["key"]))
        for target in targets:
            cutoff = target["kickoff"] - HORIZON
            n = bisect.bisect_right(times, cutoff)
            if not n:
                continue
            if n not in cache:
                eligible = sorted(window[:n], key=lambda f: f["key"])
                count = collections.Counter(f["season"] for f in eligible)
                complete = {s for s, c in count.items() if s < season and c + excluded[s] == planned[s]}
                reference = [f for f in eligible if f["season"] in complete] if complete else eligible
                size = max(max(f["score"]) for f in reference) + 1
                grid = np.zeros((size, size))
                for f in reference:
                    grid[f["score"]] += 1
                grid /= len(reference)
                records = [(f["home"], f["away"], *f["score"], f["kickoff"].timestamp()) for f in eligible]
                seen = collections.Counter(team for r in records for team in r[:2])
                cache[n] = (grid, fm.fit_team_model(records), len(eligible), len(reference), bool(complete), seen)
                fits += 1
            baseline, model, trained, b0_n, b0_complete, seen = cache[n]
            fallback = None
            if model is None:
                fallback = "insufficient_training_or_fit_failed"
            elif target["home"] not in model.positions or target["away"] not in model.positions:
                fallback = "unseen_team"
            matrix, rates = baseline, None
            if fallback is None:
                try:
                    rates = model.rates(target["home"], target["away"])
                    matrix, _ = fm._goals_matrix(*rates)
                except ValueError:
                    fallback, rates = "invalid_dc_probability", None
            p1, _ = fm.probabilities(matrix, ())
            p0, _ = fm.probabilities(baseline, ())
            s1 = fm.score_probabilities(p1, *target["score"])
            s0 = fm.score_probabilities(p0, *target["score"])
            rows.append({
                "season": season, "round": target["round"], "key": target["key"], "fallback": fallback,
                "b1": s1["score"], "b0": s0["score"],
                "b1_items": [s1["item_scores"][k] for k in ITEMS], "b0_items": [s0["item_scores"][k] for k in ITEMS],
                "trained": trained, "goals": list(target["score"]),
                "rates": [rates[0], rates[1]] if rates else None,
                # matches of each side already inside the training sample (all four seasons of the window)
                "prior": [seen[target["home"]], seen[target["away"]]],
                # match week running Tuesday..Monday, shared by all leagues
                "week": "%d-W%02d" % (target["kickoff"] - timedelta(days=1)).isocalendar()[:2],
            })
    return rows, fits


def task(args):
    src, root, league, decay, ridge, use_aliases = args
    sys.path.insert(0, src)
    from polymarket861.football import team_key

    fixtures, names = load(Path(root), league, team_key, use_aliases)
    rows, fits = walk(league, fixtures, decay, ridge)
    split = {k: sorted(v) for k, v in names.items() if len(v) > 1}
    return league, decay, ridge, use_aliases, rows, fits, len(fixtures), split


def cluster(rows, value, family):
    from polymarket861.experiment_statistics import clustered_t

    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["season"], r["round"])].append(value(r))
    return clustered_t([groups[k] for k in sorted(groups)], family)


def summary(rows):
    return {
        "matches": len(rows), "rounds": len({(r["season"], r["round"]) for r in rows}),
        "b1": math.fsum(r["b1"] for r in rows) / len(rows), "b0": math.fsum(r["b0"] for r in rows) / len(rows),
        "b1_items": [math.fsum(r["b1_items"][i] for r in rows) / len(rows) for i in range(4)],
        "b0_items": [math.fsum(r["b0_items"][i] for r in rows) / len(rows) for i in range(4)],
        "fallbacks": dict(collections.Counter(r["fallback"] for r in rows if r["fallback"])),
        "fallback_fixtures": [r["key"] for r in rows if r["fallback"]],
    }


def newcomers(rows, limit=19):
    """Sides with fewer than `limit` matches in the whole training window: clubs new to the window, early in the season.

    For each such side: goals the model expected it to score and concede against what happened. A positive
    "expected minus actual" goal difference means the model rated the newcomer too highly.
    """
    sides = []
    for r in rows:
        if r["rates"] is None:
            continue
        for index in (0, 1):
            if r["prior"][index] < limit and r["prior"][1 - index] >= limit:
                expected_for, expected_against = r["rates"][index], r["rates"][1 - index]
                actual_for, actual_against = r["goals"][index], r["goals"][1 - index]
                sides.append(((r["season"], r["round"]), expected_for - expected_against, actual_for - actual_against,
                              expected_for, actual_for, expected_against, actual_against, r["b1"], r["b0"]))
    if len(sides) < 2:
        return {"matches": len(sides)}
    from polymarket861.experiment_statistics import clustered_t

    groups = collections.defaultdict(list)
    for s in sides:
        groups[s[0]].append([s[1] - s[2], s[7] - s[8]])
    e = clustered_t([groups[k] for k in sorted(groups)], 1)
    n = len(sides)
    return {
        "rule": f"exactly one side has fewer than {limit} matches in the training window; first-match fallbacks excluded",
        "matches": n, "rounds": e["rounds"],
        "expected_goals_for": math.fsum(s[3] for s in sides) / n, "actual_goals_for": math.fsum(s[4] for s in sides) / n,
        "expected_goals_against": math.fsum(s[5] for s in sides) / n, "actual_goals_against": math.fsum(s[6] for s in sides) / n,
        "expected_minus_actual_goal_difference": e["intervals"][0],
        "b1_score": math.fsum(s[7] for s in sides) / n, "b0_score": math.fsum(s[8] for s in sides) / n,
        "b1_minus_b0": e["intervals"][1],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True)
    parser.add_argument("root")
    parser.add_argument("output")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--leagues", default="en,es,de,it,fr")
    opts = parser.parse_args()
    sys.path.insert(0, opts.src)
    import numpy as np
    import scipy
    from scipy.stats import norm, t

    leagues = opts.leagues.split(",")
    jobs = [(opts.src, opts.root, lg, d, r, True) for lg in leagues for d, r in GRID]
    jobs += [(opts.src, opts.root, lg, *DEFAULT, False) for lg in leagues if lg != "en"]
    results = {}
    with ProcessPoolExecutor(opts.workers) as pool:
        for league, decay, ridge, aliased, rows, fits, n_fixtures, split in pool.map(task, jobs):
            results[(league, decay, ridge, aliased)] = (rows, fits, n_fixtures, split)
            print(league, decay, ridge, "aliases" if aliased else "as-is", len(rows), "scored,", fits, "fits", file=sys.stderr, flush=True)

    out = {
        "what": "walk-forward B0/B1 at T-60 with results assumed usable 24 h after kickoff; unmodified 0.4.2 fit and scoring functions",
        "environment": {"python": sys.version.split()[0], "numpy": np.__version__, "scipy": scipy.__version__},
        "evaluated_seasons": {lg: EVALUATED[lg] for lg in leagues},
        "holdout_note": "2025-26 is not loaded for es/de/it/fr; the EPL five-season figure repeats a number already published with 0.4.2",
        "leagues": {},
    }
    from polymarket861.football import team_key

    for lg in leagues:
        base_rows, fits, n_fixtures, _ = results[(lg, *DEFAULT, True)]
        index = {r["key"]: r for r in base_rows}
        loaded, _ = load(Path(opts.root), lg, team_key, True)
        inventory = {}
        for f in loaded:
            row = inventory.setdefault(f["season"], {"regular_rows": 0, "usable_scores": 0, "awarded_excluded": 0, "no_valid_score": 0, "no_clock": 0})
            row["regular_rows"] += 1
            row["usable_scores"] += f["score"] is not None
            row["awarded_excluded"] += f["awarded"]
            row["no_valid_score"] += not f["has_raw_score"]
            row["no_clock"] += not f["minute"]
        block = {"fixtures_loaded": n_fixtures, "files_used_for_training_or_scoring": inventory, "fits": fits, "default": summary(base_rows)}
        block["default"]["by_season"] = {s: summary([r for r in base_rows if r["season"] == s]) for s in EVALUATED[lg]}
        # B1 - B0 with the plan's basic family: 1 candidate x 2 stages x 5 endpoints.
        est = cluster(base_rows, lambda r: [r["b1"] - r["b0"], *(a - b for a, b in zip(r["b1_items"], r["b0_items"]))], 10)
        block["b1_minus_b0_family10"] = est
        per_season = {}
        for s in EVALUATED[lg]:
            e = cluster([r for r in base_rows if r["season"] == s], lambda r: [r["b1"] - r["b0"]], 10)
            per_season[s] = {k: e[k] for k in ("rounds", "matches", "critical_t")} | e["intervals"][0]
        block["b1_minus_b0_single_season_family10"] = per_season
        # The plan's forward gate replayed on each outer season as if it were the single forward season:
        # composite lower family bound > 0 and every component lower bound >= -0.5 (MODEL-AND-VALIDATION.md,
        # "变更正式概率模型指针"), with B1 as the challenger and B0 as the incumbent.
        replay = {}
        for s in EVALUATED[lg]:
            e = cluster([r for r in base_rows if r["season"] == s],
                        lambda r: [r["b1"] - r["b0"], *(a - b for a, b in zip(r["b1_items"], r["b0_items"]))], 10)
            lows = [i["lower"] for i in e["intervals"]]
            replay[s] = {
                "rounds": e["rounds"], "matches": e["matches"],
                "composite": e["intervals"][0], "components": dict(zip(ITEMS, e["intervals"][1:])),
                "composite_lower_above_zero": lows[0] > 0,
                "components_with_lower_bound_below_minus_half": [n for n, low in zip(ITEMS, lows[1:]) if low < -0.5],
                "passes_both_conditions": lows[0] > 0 and min(lows[1:]) >= -0.5,
            }
        block["forward_gate_replay_b1_vs_b0"] = replay
        # Correlation of the four per-round component differences (B1 - B0).
        groups = collections.defaultdict(list)
        for r in base_rows:
            groups[(r["season"], r["round"])].append([a - b for a, b in zip(r["b1_items"], r["b0_items"])])
        means = np.array([np.mean(v, axis=0) for v in groups.values()])
        block["component_round_correlation_b1_minus_b0"] = {"order": list(ITEMS), "matrix": np.corrcoef(means.T).round(4).tolist()}
        # Grid cells against the default, paired by fixture. Family 3 candidates x 2 stages x 5 endpoints = 30.
        cells = []
        for decay, ridge in GRID:
            rows = results[(lg, decay, ridge, True)][0]
            assert [r["key"] for r in rows] == [r["key"] for r in base_rows]
            cell = {"decay_per_day": decay, "ridge": ridge, "score": math.fsum(r["b1"] for r in rows) / len(rows),
                    "by_season": {s: math.fsum(r["b1"] for r in rows if r["season"] == s) / sum(r["season"] == s for r in rows) for s in EVALUATED[lg]}}
            if (decay, ridge) != DEFAULT:
                e = cluster(rows, lambda r: [r["b1"] - index[r["key"]]["b1"]], 30)
                cell["minus_default_family30"] = e["intervals"][0] | {"critical_t": e["critical_t"], "rounds": e["rounds"]}
                d, se = e["intervals"][0]["difference"], e["intervals"][0]["standard_error"]
                sd_round = se * math.sqrt(e["rounds"])
                cell["t_statistic_all_outer_seasons"] = d / se if se else None
                # rounds needed for 80 % power that a family-30 interval excludes zero, if the true effect equalled this estimate
                need = None
                if d > 0:
                    for g in range(10, 20001):
                        if d / (sd_round / math.sqrt(g)) >= t.ppf(1 - 0.025 / 30, g - 1) + norm.ppf(0.8):
                            need = g
                            break
                cell["rounds_needed_for_80pct_power_if_true"] = need
                # What one forward season can resolve for a change of this kind (single candidate, family 10).
                g1 = 38 if lg in ("en", "es", "it") else 34
                crit1 = float(t.ppf(1 - 0.025 / 10, g1 - 1))
                cell["one_forward_season_family10"] = {
                    "rounds": g1, "standard_error": sd_round / math.sqrt(g1),
                    "half_width": crit1 * sd_round / math.sqrt(g1),
                    "difference_needed_for_80pct_power": (crit1 + float(norm.ppf(0.8))) * sd_round / math.sqrt(g1),
                }
            cells.append(cell)
        block["grid"] = cells
        # Nested choice exactly as the plan describes it: pick the cell with the best mean over earlier outer seasons.
        nested = []
        for i, s in enumerate(EVALUATED[lg]):
            if i == 0:
                continue
            earlier = EVALUATED[lg][:i]
            best = max(cells, key=lambda c: (sum(c["by_season"][e] for e in earlier) / len(earlier)))
            default_cell = next(c for c in cells if (c["decay_per_day"], c["ridge"]) == DEFAULT)
            nested.append({"season": s, "chosen": [best["decay_per_day"], best["ridge"]], "chosen_score": best["by_season"][s],
                           "default_score": default_cell["by_season"][s], "difference": best["by_season"][s] - default_cell["by_season"][s]})
        block["nested_grid_choice"] = nested
        block["nested_grid_choice_mean_difference"] = sum(n["difference"] for n in nested) / len(nested)
        # Detectable difference in one forward season under the plan's gate (lower family bound > 0, 80 % power).
        sd_round = est["intervals"][0]["standard_error"] * math.sqrt(est["rounds"])
        g = 38 if lg in ("en", "es", "it") else 34
        block["one_forward_season_gate"] = {
            "rounds": g, "round_sd_of_b1_minus_b0": sd_round,
            "standard_error_one_season": sd_round / math.sqrt(g),
            "half_width_family10": float(t.ppf(1 - 0.025 / 10, g - 1)) * sd_round / math.sqrt(g),
            "difference_needed_for_80pct_power_family10": (float(t.ppf(1 - 0.025 / 10, g - 1)) + float(norm.ppf(0.8))) * sd_round / math.sqrt(g),
            "power_for_observed_b1_minus_b0": float(norm.cdf(est["intervals"][0]["difference"] / (sd_round / math.sqrt(g)) - float(t.ppf(1 - 0.025 / 10, g - 1)))),
        }
        block["newcomers"] = newcomers(base_rows)
        if lg != "en":
            raw_rows, _, _, split = results[(lg, *DEFAULT, False)]
            aliased_names = results[(lg, *DEFAULT, True)][3]
            by = {s: summary([r for r in raw_rows if r["season"] == s]) for s in EVALUATED[lg]}
            only_alias = {k: v for k, v in aliased_names.items() if k not in split}
            block["without_alias_table"] = {
                "spellings_joined_by_team_key_alone": split,
                "spellings_joined_only_by_the_alias_table": only_alias,
                "clubs_split_in_two_without_the_table": len(only_alias),
                "score": summary(raw_rows)["b1"], "fallbacks": summary(raw_rows)["fallbacks"],
                "by_season": {s: {"b1": by[s]["b1"], "fallbacks": by[s]["fallbacks"], "fallback_fixtures": by[s]["fallback_fixtures"],
                                  "fallback_fixtures_with_table": block["default"]["by_season"][s]["fallback_fixtures"],
                                  "difference_vs_aliased": by[s]["b1"] - block["default"]["by_season"][s]["b1"]} for s in EVALUATED[lg]},
                "difference_vs_aliased": summary(raw_rows)["b1"] - block["default"]["b1"],
            }
        out["leagues"][lg] = block
    # All leagues together on the seasons every league has, clustered by shared match week instead of league round.
    common = ["2021-22", "2022-23", "2023-24", "2024-25"]
    if len(leagues) == 5:
        from polymarket861.experiment_statistics import clustered_t

        pooled = [r for lg in leagues for r in results[(lg, *DEFAULT, True)][0] if r["season"] in common]
        weeks = collections.defaultdict(list)
        for r in pooled:
            weeks[r["week"]].append([r["b1"] - r["b0"], *(a - b for a, b in zip(r["b1_items"], r["b0_items"]))])
        e = clustered_t([weeks[k] for k in sorted(weeks)], 10)
        sd_week = e["intervals"][0]["standard_error"] * math.sqrt(e["rounds"])
        per_season = e["rounds"] / len(common)
        out["pooled_five_leagues"] = {
            "seasons": common, "matches": e["matches"], "match_weeks": e["rounds"], "match_weeks_per_season": per_season,
            "b1_minus_b0_family10": e["intervals"][0],
            "b1_minus_b0_components_family10": dict(zip(ITEMS, e["intervals"][1:])),
            "one_forward_season": {
                "standard_error": sd_week / math.sqrt(per_season),
                "half_width_family10": float(t.ppf(1 - 0.025 / 10, per_season - 1)) * sd_week / math.sqrt(per_season),
                "difference_needed_for_80pct_power_family10": (float(t.ppf(1 - 0.025 / 10, per_season - 1)) + float(norm.ppf(0.8))) * sd_week / math.sqrt(per_season),
            },
            "note": "standard errors of a candidate that changes predictions less than B1 does against B0 are smaller in proportion; the ratio between pooled and per-league widths is what carries over",
        }
        # The same forward-gate replay with all five leagues pooled by match week, season by season.
        pooled_replay = {}
        for s in common:
            wk = collections.defaultdict(list)
            for r in pooled:
                if r["season"] == s:
                    wk[r["week"]].append([r["b1"] - r["b0"], *(a - b for a, b in zip(r["b1_items"], r["b0_items"]))])
            es = clustered_t([wk[k] for k in sorted(wk)], 10)
            lows = [i["lower"] for i in es["intervals"]]
            pooled_replay[s] = {
                "match_weeks": es["rounds"], "matches": es["matches"], "composite": es["intervals"][0],
                "components": dict(zip(ITEMS, es["intervals"][1:])),
                "composite_lower_above_zero": lows[0] > 0,
                "components_with_lower_bound_below_minus_half": [n for n, low in zip(ITEMS, lows[1:]) if low < -0.5],
                "passes_both_conditions": lows[0] > 0 and min(lows[1:]) >= -0.5,
            }
        out["pooled_five_leagues"]["forward_gate_replay_b1_vs_b0"] = pooled_replay
        out["pooled_five_leagues"]["newcomers"] = newcomers(pooled)
        rest = [r for r in pooled if r["rates"] is not None and min(r["prior"]) >= 19]
        out["pooled_five_leagues"]["established_pairs"] = {
            "matches": len(rest), "b1_score": math.fsum(r["b1"] for r in rest) / len(rest), "b0_score": math.fsum(r["b0"] for r in rest) / len(rest),
        }
        out["nested_grid_choice_all_leagues"] = {
            "league_seasons": sum(len(out["leagues"][lg]["nested_grid_choice"]) for lg in leagues),
            "mean_difference_vs_default": sum(n["difference"] for lg in leagues for n in out["leagues"][lg]["nested_grid_choice"]) / sum(len(out["leagues"][lg]["nested_grid_choice"]) for lg in leagues),
            "better": sum(n["difference"] > 0 for lg in leagues for n in out["leagues"][lg]["nested_grid_choice"]),
            "same": sum(n["difference"] == 0 for lg in leagues for n in out["leagues"][lg]["nested_grid_choice"]),
            "worse": sum(n["difference"] < 0 for lg in leagues for n in out["leagues"][lg]["nested_grid_choice"]),
        }
    seasons_replayed = [(lg, s, v) for lg in leagues for s, v in out["leagues"][lg]["forward_gate_replay_b1_vs_b0"].items()]
    out["forward_gate_replay_b1_vs_b0_summary"] = {
        "league_seasons": len(seasons_replayed),
        "composite_lower_above_zero": sum(v["composite_lower_above_zero"] for _, _, v in seasons_replayed),
        "all_component_lower_bounds_at_least_minus_half": sum(not v["components_with_lower_bound_below_minus_half"] for _, _, v in seasons_replayed),
        "passes_both_conditions": sum(v["passes_both_conditions"] for _, _, v in seasons_replayed),
        "failing": [{"league": lg, "season": s, "composite_lower": v["composite"]["lower"],
                     "components_below_minus_half": {n: v["components"][n]["lower"] for n in v["components_with_lower_bound_below_minus_half"]}}
                    for lg, s, v in seasons_replayed if not v["passes_both_conditions"]],
        "note": "EPL 2025-26 is included because its per-match scores were already published with 0.4.2",
    }
    if "en" in out["leagues"]:
        en = out["leagues"]["en"]
        two = [r for r in results[("en", *DEFAULT, True)][0] if r["season"] in ("2024-25", "2025-26")]
        out["fidelity_check"] = {
            "epl_five_seasons_b1": en["default"]["b1"], "published_0_4_2": 75.447813,
            "abs_difference": abs(en["default"]["b1"] - 75.447813), "matches": en["default"]["matches"], "rounds": en["default"]["rounds"],
            "epl_2024_25_and_2025_26": {"b0": summary(two)["b0"], "b1": summary(two)["b1"], "published_b0": 73.474232, "published_b1": 74.791925},
        }
    Path(opts.output).write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
