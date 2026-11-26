"""Frozen, research-only five-league evaluation of the initial B1 against local B0."""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from . import football, league_registry
from .experiment_statistics import clustered_t
from .football_model import DECAY_PER_DAY, ITEM_KEYS, MIN_MATCHES, RIDGE


DEVELOPMENT = ("2021-22", "2022-23", "2023-24", "2024-25")
HOLDOUT = "2025-26"
SOURCE_SEASONS = ("2018-19", "2019-20", "2020-21", *DEVELOPMENT, HOLDOUT)
ENDPOINTS = ("composite", *ITEM_KEYS)
FAMILY_SIZE = len(ENDPOINTS) * 2


@dataclass(frozen=True)
class PairedScore:
    fixture_id: str
    season: str
    round_number: int
    match_week: str
    model: float
    baseline: float
    differences: tuple[float, ...]
    fallback: str | None

    @classmethod
    def from_backtest(cls, row):
        model, baseline = row["model_score"], row["baseline_score"]
        # A Tuesday boundary keeps a weekend's Monday matches in its own block.
        tuesday_week = football.utc(row["kickoff"]) - timedelta(days=1)
        year, week, _ = tuesday_week.isocalendar()
        diff = tuple(
            row["model_items"][k] - row["baseline_items"][k] for k in ITEM_KEYS
        )
        return cls(
            row["fixture_id"],
            row["season"],
            row["round"],
            f"{year}-W{week:02d}",
            model,
            baseline,
            (model - baseline, *diff),
            row["fallback_reason"],
        )


def _statistics(rows, *, pooled=False):
    groups = defaultdict(list)
    seen = set()
    for row in rows:
        if row.fixture_id in seen:
            raise ValueError(
                f"duplicate fixture in research population: {row.fixture_id}"
            )
        seen.add(row.fixture_id)
        group = row.match_week if pooled else (row.season, row.round_number)
        groups[group].append(row.differences)
    estimate = clustered_t((groups[k] for k in sorted(groups)), FAMILY_SIZE)
    return {
        "matches": len(rows),
        "cluster_unit": "shared_UTC_week_Tuesday_through_Monday"
        if pooled
        else "season_official_round",
        "clusters": len(groups),
        "mean_b1": football._average([r.model for r in rows]),
        "mean_b0": football._average([r.baseline for r in rows]),
        "intervals": dict(zip(ENDPOINTS, estimate["intervals"], strict=True))
        if estimate
        else None,
    }


def _snapshot(root, destination, codes):
    """Verify all inputs before scoring, and score the exact bytes that were verified."""
    manifest = {}
    for code in codes:
        manifest[code] = {}
        for season in SOURCE_SEASONS:
            filename = league_registry.profile(code).source_file
            relative = Path(season, filename)
            raw = (root / relative).read_bytes()
            expected = league_registry.source_blob(code, season)
            digest = football.blob_sha(raw)
            if digest != expected:
                raise ValueError(f"fixed archive blob mismatch: {relative}")
            path = destination / relative
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_bytes(raw)
            manifest[code][season] = digest
    return manifest


def _league_replay(spec):
    code, snapshot = spec
    fixtures = football.load_fixtures(snapshot, archive_lag_hours=24, league=code)
    scored = []
    summary = football.backtest(
        fixtures,
        DEVELOPMENT[0],
        HOLDOUT,
        horizons=(60,),
        bootstrap=0,
        archive_lag_hours=24,
        details_sink=lambda row: scored.append(PairedScore.from_backtest(row)),
        league=code,
    )
    audit = {
        "omissions_in_graded_fixtures": summary["horizons"]["T-60"]["omissions"],
        "excluded_fixtures": summary["fixtures"]["excluded_fixtures"],
        "missing_score_without_exception": summary["fixtures"]["score_unavailable"],
    }
    return code, scored, audit


def _league_report(code, rows, audit):
    seasons = {}
    round_counts = {}
    for season in (*DEVELOPMENT, HOLDOUT):
        current = [r for r in rows if r.season == season]
        counted = Counter(r.round_number for r in current)
        rounds = league_registry.round_count(code, season)
        scheduled = league_registry.fixture_count(code, season)
        if any(n < 1 or n > rounds for n in counted) or len(current) > scheduled:
            raise ValueError(f"{code}/{season}: pairs outside registered competition")
        round_counts[season] = {str(n): counted[n] for n in range(1, rounds + 1)}
        seasons[season] = {
            **_statistics(current),
            "scheduled": scheduled,
            "coverage": len(current) / scheduled,
            "rounds_with_pairs": len(counted),
            "registered_rounds": rounds,
            "fallback_fixtures": [r.fixture_id for r in current if r.fallback],
        }
    return {
        "development": _statistics([r for r in rows if r.season in DEVELOPMENT]),
        "holdout": seasons[HOLDOUT],
        "by_season": seasons,
        "paired_counts_by_official_round": round_counts,
        **audit,
    }


def _initial_research_gate(leagues, pooled):
    local = {}
    for code, report in leagues.items():
        dev, held = report["development"], report["holdout"]
        interval = held["intervals"]
        local[code] = {
            "history_lower_positive": bool(
                dev["intervals"] and dev["intervals"]["composite"]["lower"] > 0
            ),
            "holdout_point_positive": bool(
                interval and interval["composite"]["difference"] > 0
            ),
            "holdout_coverage": held["matches"] * 100 >= held["scheduled"] * 95,
            "holdout_every_round_represented": held["rounds_with_pairs"]
            == held["registered_rounds"],
        }
    lower = pooled["intervals"]
    pooled_positive = bool(lower and lower["composite"]["lower"] > 0)
    return {
        "protocol": "initial-local-B1-research-v1",
        "per_league": local,
        "pooled_holdout_lower_positive": pooled_positive,
        "research_criteria_met": set(local) == set(league_registry.PROFILES)
        and pooled_positive
        and all(all(gates.values()) for gates in local.values()),
        "formal_promotion": False,
    }


def baseline_study(root: Path, *, workers: int = 1):
    codes = list(league_registry.PROFILES)
    if not 1 <= workers <= len(codes):
        raise ValueError(f"workers must be between 1 and {len(codes)}")
    with TemporaryDirectory(prefix="pm861-five-study-") as temp:
        snapshot = Path(temp)
        manifest = _snapshot(Path(root), snapshot, codes)
        specs = [(code, snapshot) for code in codes]
        if workers == 1:
            results = [_league_replay(spec) for spec in specs]
        else:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                results = list(executor.map(_league_replay, specs))
    reports = {code: _league_report(code, rows, audit) for code, rows, audit in results}
    combined = [r for _, rows, _ in results for r in rows]
    pooled = {
        "development": _statistics(
            [r for r in combined if r.season in DEVELOPMENT], pooled=True
        ),
        "holdout": _statistics(
            [r for r in combined if r.season == HOLDOUT], pooled=True
        ),
    }
    return {
        "protocol": "five-league-initial-b1-2026-11-26-v1",
        "scope": "retrospective, research-only; unverified local receipts; no news, executions or ROI",
        "inputs": {
            "openfootball_commit": football.SOURCE_COMMIT,
            "git_blobs": manifest,
        },
        "assumptions": {
            "archive_lag_hours": 24,
            "cutoff_minutes": 60,
            "decay_per_day": DECAY_PER_DAY,
            "ridge": RIDGE,
            "minimum_matches": MIN_MATCHES,
            "development": list(DEVELOPMENT),
            "holdout": HOLDOUT,
            "family_comparisons_per_registered_analysis": FAMILY_SIZE,
            "prior_disclosure": "EPL holdout outcomes and five-league descriptive aggregates were public.",
        },
        "leagues": reports,
        "pooled": pooled,
        "initial_research_gate": _initial_research_gate(reports, pooled["holdout"]),
    }


def _run(args):
    report = baseline_study(args.path, workers=args.workers)
    if args.output:
        football._write_json(args.output, report)
    return report


def register(subcommands):
    command = subcommands.add_parser(
        "baseline-study", help="用固定八季输入复算五联赛 B1/B0 和留出季成绩"
    )
    command.add_argument("path", type=Path, help="固定 OpenFootball 快照根目录")
    command.add_argument("--workers", type=int, default=1)
    command.add_argument("--output", type=Path)
    command.set_defaults(func=_run)
