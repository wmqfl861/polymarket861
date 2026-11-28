"""Chronological technical replay on finalized schedules and lagged final scores."""

from collections import defaultdict, deque
from datetime import timedelta
from hashlib import sha256
from itertools import groupby
import math
import json
from pathlib import Path

import numpy as np

from . import model, venue

METRICS = ("brier_win", "crps_total", "crps_home", "crps_away")


def _bounds(day_records, replicates, block_days, seed):
    """Sample contiguous calendar days within a season, preserving all games from each day."""
    indices = sorted(day_records)
    span = (indices[-1] - indices[0]).days + 1
    block = min(block_days, span)
    matrix = np.zeros((span, 1 + 2 * len(METRICS)))
    for when, (n, losses) in day_records.items():
        matrix[(when - indices[0]).days] = np.r_[n, np.diff(losses, axis=0).ravel()]
    cumulative = np.vstack([np.zeros(matrix.shape[1]), np.cumsum(matrix, axis=0)])
    rng = np.random.default_rng(seed)
    result = np.zeros((replicates, matrix.shape[1]))
    for offset in range(0, span, block):
        length = min(span - offset, block)
        starts = rng.integers(0, span - length + 1, size=replicates)
        result += cumulative[starts + length] - cumulative[starts]
    return result


def _summary(records, draws):
    count = sum(n for n, _ in records)
    sums = sum(values for _, values in records)
    averages = sums / count
    usable = draws[draws[:, 0] > 0]
    contrasts = usable[:, 1:] / usable[:, :1]
    lo, hi = np.quantile(contrasts, [0.025, 0.975], axis=0)
    comparisons = {}
    for i, key in enumerate(("N1_minus_N0", "N2_minus_N1")):
        comparisons[key] = {
            metric: {"difference": round(float(averages[i+1, j] - averages[i, j]), 7),
                     "block_interval_95": [round(float(lo[i*len(METRICS)+j]), 7),
                                            round(float(hi[i*len(METRICS)+j]), 7)]}
            for j, metric in enumerate(METRICS)}
    return {"games": count, "losses": {arm: dict(zip(METRICS, np.round(avg, 7).tolist(), strict=True))
                                       for arm, avg in zip(model.MODELS, averages, strict=True)},
            "paired": comparisons}



def _shape(moments, games):
    diagnostics = {}
    for label, averages in zip(("observed",) + model.MODELS, moments / games, strict=True):
        h, a, hh, aa, ha, tie, one, three = averages
        vh, va = max(0., hh - h*h), max(0., aa - a*a)
        cov = ha - h*a
        diagnostics[label] = {
            "p_tied_final": round(float(tie), 7), "p_one_point": round(float(one), 7),
            "p_within_three": round(float(three), 7),
            "home_away_score_correlation": round(float(cov / math.sqrt(vh * va)), 7) if vh * va else None,
            "total_sd": round(math.sqrt(max(0., vh + va + 2*cov)), 7),
            "margin_sd": round(math.sqrt(max(0., vh + va - 2*cov)), 7)}
    return diagnostics

def backtest(rows, *, first_season=None, last_season=None, lag_days=1, replicates=2000, seed=861):
    """Predict before seeing any same-day labels. Schedule dates are final archive proxies."""
    if lag_days not in (1, 2) or not 100 <= replicates <= 10000:
        raise ValueError("use 1 or 2 days of post-day-end lag and 100–10000 bootstrap replicates")
    games = model.prepare(rows)
    if not games:
        raise ValueError("no regular season NBA finals")
    available = {g["season"] for g in games}
    last = max(available) if last_season is None else last_season
    first = last - 4 if first_season is None else first_season
    if first > last or any(s not in available for s in range(first, last + 1)):
        raise ValueError("requested NBA seasons do not have continuous coverage")
    # Same bounded warmup on every rerun; the latest eight prior seasons are enough
    # to populate the 1200-game bank and let the team recency updates stabilize.
    games = [g for g in games if first-8 <= g["season"] <= last]
    state = model.ScoreState()
    played_at_training = {}
    known_schedule = {}
    pending = deque()
    records = defaultdict(dict)
    missed = defaultdict(int)
    shapes = defaultdict(lambda: np.zeros((1 + len(model.MODELS), len(model.MOMENTS))))
    for day, rows_today in groupby(games, key=model.day_of):
        today = list(rows_today)
        allowed = day - timedelta(days=lag_days + 1)
        while pending and pending[0][0] <= allowed:
            _, observable = pending.popleft()
            state.learn_day(observable, played_at_training)
            model.update_dates(played_at_training, observable)
        season = today[0]["season"]
        if season >= first:
            if state.total_games < model.MIN_HISTORY or (day - state.latest_result).days > 500:
                missed[season] += len(today)
            else:
                bank = state.bank()
                sums = np.zeros((len(model.MODELS), len(METRICS)))
                for game in today:
                    result = model.forecast(state, game, known_schedule, bank)
                    h, a = game["home_score"], game["away_score"]
                    shapes[season][0] += model.pair_moments(h, a)
                    for i, arm in enumerate(model.MODELS):
                        moments = result["models"][arm]["pair_moments"]
                        shapes[season][i+1] += [moments[key] for key in model.MOMENTS]
                        loss = model.score(result["models"][arm], game["home_score"], game["away_score"])
                        sums[i] += [loss[key] for key in METRICS]
                records[season][day] = len(today), sums
        pending.append((day, today))
        model.update_dates(known_schedule, today)
    if set(records) != set(range(first, last+1)):
        raise ValueError("insufficient pregame history in requested NBA seasons")
    summaries = {}
    seasonal_draws = []
    all_records = []
    for season in sorted(records):
        days = records[season]
        draws = _bounds(days, replicates, 7, seed + season)
        observed = list(days.values())
        summary = _summary(observed, draws)
        summary["final_score_calibration"] = _shape(shapes[season], summary["games"])
        summary["insufficient_history"] = missed[season]
        summary["game_days"] = len(days)
        summaries[str(season)] = summary
        seasonal_draws.append(draws)
        all_records.extend(observed)
    pooled = _summary(all_records, np.sum(seasonal_draws, axis=0))
    pooled["final_score_calibration"] = _shape(sum(shapes.values()), pooled["games"])
    return {"recipe": model.RECIPE, "scope": model.SCOPE, "sample_unit": "one NBA regular season game",
            "venue_policy": venue.POLICY, "data_grade": "technical_final_archive", "schedule_basis": "final archive game date; no receipt-time schedule",
            "lag": {"days_after_game_date_end": lag_days, "excludes_all_same_date_scores": True},
            "season_ids": "starting year", "evaluated_seasons": [first, last], "eligible_archive_rows": len(games),
            "interval_method": {"unit": "7-calendar-day moving blocks, stratified by season",
                                "replicates": replicates, "seed": seed, "confidence": 0.95,
                                "interpretation": "marginal exploratory intervals"},
            "parameters": {"paired_score_bank": model.BANK_SIZE, "minimum_training_games": model.MIN_HISTORY,
                           "team_online_rate": model.TEAM_RATE, "offseason_strength_carry": model.SEASON_CARRY,
                           "b2b_margin_points": model.B2B_MARGIN_POINTS, "warmup_prior_seasons": 8,
                           "actual_prior_seasons": sorted({g["season"] for g in games if g["season"] < first})},
            "per_season": summaries, "pooled": pooled,
            "prediction_scope": "final scores including overtime; no regulations-only or player/news claims",
            "margin_use": "diagnostic only; one-point and three-point probabilities require independent validation before spread use",
            "market_receipts": 0, "paper_eligible": False}


def register(actions):
    p = actions.add_parser("backtest", help="Replay a pinned FiveThirtyEight NBA archive or stored scores")
    p.add_argument("--archive", type=Path, help="verified original nbaallelo.csv")
    p.add_argument("--source", choices=("538", "balldontlie"), default="538")
    p.add_argument("--first-season", type=int, help="starting year, inclusive")
    p.add_argument("--last-season", type=int, help="starting year, inclusive")
    p.add_argument("--lag-days", type=int, choices=(1, 2), default=1)
    p.add_argument("--replicates", type=int, default=2000)
    p.set_defaults(func=_run)


def _run(args):
    if args.archive is not None:
        from .data import parse_538

        if args.source != "538":
            raise ValueError("--archive accepts only the pinned FiveThirtyEight archive")
        rows = parse_538(args.archive)
        checksum = sha256(args.archive.read_bytes()).hexdigest()
    else:
        from .data import games_as_of

        rows = games_as_of(args.data_dir, source=args.source)
        checksum = None
    result = backtest(rows, first_season=args.first_season, last_season=args.last_season,
                      lag_days=args.lag_days, replicates=args.replicates)
    result["input"] = {"source": args.source, "csv_sha256": checksum, "normalized_records_sha256": sha256(
        json.dumps(sorted(rows, key=lambda g: g["game_id"]), sort_keys=True, separators=(",", ":"),
                   default=str).encode()).hexdigest()}
    if args.source == "538":
        result["input"].update(
            credit="FiveThirtyEight NBA Elo dataset, game logs sourced from Basketball-Reference; CC BY 4.0",
            archive_blob="6d6ccd07151aed322e1a77c57ea83b8ea58c5523",
            archive_commit="4c1ff5e3aef1816ae04af63218015066e186c147",
            source_url="https://github.com/fivethirtyeight/data/tree/4c1ff5e3aef1816ae04af63218015066e186c147/nba-elo",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            transformations="paired team records; final scores only; source Elo forecasts excluded")
    else:
        result["coverage_validation"] = "unverified exploratory slice; nba model modern-study verifies complete seasons"
    return result
