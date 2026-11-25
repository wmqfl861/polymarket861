"""Walk-forward EPL research on coherent totals calibration and a first-appearance prior."""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, logit

from . import experiment_statistics, football, football_model, historical_inputs, store

SPEC = {
    "version": "football-candidates-1",
    "baseline": "B1 including its original B0 fallback",
    "horizon_minutes": 60,
    "fold": "freeze both candidates each August 1 at 00:00 UTC; score the following EPL season",
    "calibration": {
        "prior_prediction_seasons": 3,
        "minimum_oof_pairs": 100,
        "penalty": 8,
        "target": "full-match over 2.5",
        "matrix": "rescale mass above/below 2.5, preserving conditional scoreline distribution within each set",
        "applies": "B1 predictions only",
    },
    "newcomer": {
        "previous_complete_seasons": 5,
        "prior_games": 8,
        "minimum_team_seasons": 3,
        "applies": "original unseen_team fallback only; the previous complete season must identify non-incumbents",
        "estimator": "attack and defensive strength of previously promoted team-seasons relative to EPL goals per team game",
    },
    "inference": "match-weighted difference in four-item score, clustered by season and official round; two candidate primary contrasts, simultaneous 95% t intervals; item contrasts descriptive",
    "activation": "research_only; prospective registration and new approved production recipe required",
}
NAMES = ("totals_calibration", "newcomer_prior")


def _available(f, cut):
    versions = [v for v in f.result_versions if v.available <= cut]
    return versions[-1] if versions and versions[-1].score is not None else None


def calibrator(pairs):
    if len(pairs) < SPEC["calibration"]["minimum_oof_pairs"]:
        return None
    x = np.array([logit(np.clip(p, 1e-6, 1 - 1e-6)) for p, y in pairs])
    y = np.array([y for p, y in pairs])
    ridge = SPEC["calibration"]["penalty"]

    def loss(params):
        a, b = params
        z = a + b * x
        err = expit(z) - y
        return float(
            (np.logaddexp(0, z) - y * z).sum() + 0.5 * ridge * (a * a + (b - 1) ** 2)
        ), np.array([err.sum() + ridge * a, np.dot(err, x) + ridge * (b - 1)])

    opt = minimize(
        loss,
        np.array([0.0, 1.0]),
        jac=True,
        method="L-BFGS-B",
        bounds=((-5, 5), (0, 4)),
    )
    return [float(x) for x in opt.x] if opt.success else None


def calibrate_matrix(matrix, params):
    mat = np.asarray(matrix, dtype=float)
    if params is None:
        return mat
    h, a = np.indices(mat.shape)
    over = h + a >= 3
    p = float(mat[over].sum())
    if not 1e-12 < p < 1 - 1e-12:
        return mat
    q = float(expit(params[0] + params[1] * logit(p)))
    out = mat * np.where(over, q / p, (1 - q) / (1 - p))
    return out / out.sum()


def complete_season(fixtures, cutoff):
    if len(fixtures) != 380:
        return None
    table = defaultdict(lambda: [0, 0, 0])
    latest = None
    for f in fixtures:
        v = _available(f, cutoff)
        if not v:
            return None
        latest = max(v.available, latest) if latest else v.available
        for team, gf, ga in ((f.home, *v.score), (f.away, *reversed(v.score))):
            k = football.team_key(team)
            table[k][0] += gf
            table[k][1] += ga
            table[k][2] += 1
    if len(table) != 20 or any(r[2] != 38 for r in table.values()):
        return None
    return table, latest


def newcomer_prior(by_year, year, cutoff):
    full = {
        y: complete_season(by_year.get(y, ()), cutoff) for y in range(year - 6, year)
    }
    previous = full.get(year - 1)
    offsets = []
    clocks = []
    if previous is None:
        return None, set(), 0, None
    clocks.append(previous[1])
    prior_games = SPEC["newcomer"]["prior_games"]
    for y in range(year - 5, year):
        before, current = full[y - 1], full[y]
        if not before or not current:
            continue
        table = current[0]
        total = sum(val[0] for val in table.values())
        avg = total / (380 * 2)
        if avg <= 0:
            continue
        clocks.extend([before[1], current[1]])
        for club in sorted(table.keys() - before[0].keys()):
            gf, ga, n = table[club]
            denom = (n + prior_games) * avg
            offsets.append(
                (
                    math.log((gf + prior_games * avg) / denom),
                    -math.log((ga + prior_games * avg) / denom),
                )
            )
    prior = (
        np.mean(offsets, axis=0).tolist()
        if len(offsets) >= SPEC["newcomer"]["minimum_team_seasons"]
        else None
    )
    return prior, set(previous[0]), len(offsets), max(clocks)


def newcomer_matrix(f, cut, engine, prior, incumbents):
    if not prior:
        return None
    model = engine.fit(f.season, cut, f.fixture_id).b1
    if model is None:
        return None
    teams = (football.team_key(f.home), football.team_key(f.away))
    positions = model.positions
    if not any(t not in positions for t in teams) or any(
        t in incumbents and t not in positions for t in teams
    ):
        return None

    def strengths(team):
        idx = positions.get(team)
        return (
            (float(model.attack[idx]), float(model.defence[idx]))
            if idx is not None
            else prior
        )

    ha, hd = strengths(teams[0])
    aa, ad = strengths(teams[1])
    lam = math.exp(model.intercept + model.home_advantage + ha - ad)
    mu = math.exp(model.intercept + aa - hd)
    try:
        return football_model._goals_matrix(lam, mu, model.rho)[0]
    except ValueError:
        return None


def _summary(records):
    groups = defaultdict(list)
    for season, rnd, base, changed, active in records:
        groups[(season, rnd)].append(changed["score"] - base["score"])
    numbers = experiment_statistics.clustered_t(groups.values(), len(NAMES))
    interval = (numbers.pop("intervals")[0] | numbers) if numbers else None

    def mean(values):
        return float(np.mean(values)) if values else None

    return {
        "n": len(records),
        "changed_forecasts": sum(int(r[4]) for r in records),
        "changed_rounds": len({(r[0], r[1]) for r in records if r[4]}),
        "changed_only_difference_descriptive": mean(
            [r[3]["score"] - r[2]["score"] for r in records if r[4]]
        ),
        "base_score": mean([r[2]["score"] for r in records]),
        "candidate_score": mean([r[3]["score"] for r in records]),
        "interval": interval,
        "item_score_differences": {
            k: mean([r[3]["item_scores"][k] - r[2]["item_scores"][k] for r in records])
            for k in football_model.ITEM_KEYS
        },
        "over_2_5_brier": {
            k: mean([r[i]["losses"]["total"] for r in records])
            for k, i in [("baseline", 2), ("candidate", 3)]
        },
    }


def study(data_dir, root, *, first, last, evidence=None, archive_lag_hours=None):
    fixtures, provenance = historical_inputs.load_for_study(
        data_dir, root, evidence, archive_lag_hours
    )
    lo, hi = football.season_year(first), football.season_year(last)
    if hi < lo:
        raise ValueError("invalid test seasons")
    by_year = defaultdict(list)
    for f in fixtures:
        by_year[football.season_year(f.season)].append(f)
    if set(range(lo, hi + 1)) - by_year.keys():
        raise ValueError("every requested test season must be present")
    confirmed = provenance["timing_authenticated"]
    strict = archive_lag_hours is None
    acceptable = not provenance["reason"] and (not strict or confirmed)
    if not strict and archive_lag_hours != 24:
        raise ValueError("this preregistered sensitivity uses 24 hours")
    engine = football.FootballEngine(fixtures)
    previous_predictions = defaultdict(list)
    outputs = {n: [] for n in NAMES}
    folds = []
    joined = Counter()
    expected = Counter()
    omissions = Counter()
    trace = hashlib.sha256()
    for year in range(lo - 3, hi + 1):
        fold_trace = hashlib.sha256()
        fold_start = datetime(year, 8, 1, tzinfo=timezone.utc)
        games = sorted(by_year[year], key=lambda f: (football.study_kickoff(f), f.fixture_id))
        is_test = year >= lo
        pairs = []
        training_times = []
        for y in range(year - 3, year):
            for f, p in previous_predictions[y]:
                seen = _available(f, fold_start)
                if seen:
                    pairs.append((p, sum(seen.score) >= 3))
                    training_times.append(seen.available)
        params = calibrator(pairs) if is_test else None
        prior, incumbents, n_club, last_prior = (
            newcomer_prior(by_year, year, fold_start)
            if is_test and acceptable
            else (None, set(), 0, None)
        )
        if is_test:
            folds.append(
                {
                    "season": f"{year}-{(year + 1) % 100:02d}",
                    "frozen_at": store.iso(fold_start),
                    "calibration_training_n": len(pairs),
                    "latest_calibration_result": store.iso(max(training_times))
                    if training_times
                    else None,
                    "calibration": params,
                    "newcomer_team_seasons": n_club,
                    "latest_prior_result": store.iso(last_prior)
                    if last_prior
                    else None,
                    "newcomer_log_attack_defence": prior,
                }
            )
        for f in games:
            settled_label = f.home_goals is not None and f.away_goals is not None
            if is_test and settled_label:
                expected[f.season] += 1
            if not acceptable or f.kickoff_precision != "minute":
                if is_test and settled_label:
                    reason = provenance.get("reason") or (
                        "unverified_historical_timing" if not acceptable else "kickoff_time_unknown"
                    )
                    omissions[reason] += 1
                continue
            kickoff = football.study_kickoff(f)
            cut = kickoff - timedelta(minutes=60)
            if cut < fold_start:
                continue
            try:
                pred = engine.forecast(
                    f.home, f.away, kickoff, cut, season=f.season, lines=()
                )
            except ValueError as exc:
                if str(exc) not in football.FORECAST_OMISSIONS:
                    raise
                if is_test and settled_label:
                    omissions[str(exc)] += 1
                continue
            ev = pred["evidence"]
            if strict and (
                ev["schedule_grade"] != "archived_schedule_upper_bound"
                or set(ev["result_grades"]) != {"archived_upper_bound"}
            ):
                continue
            if store.instant(ev["latest_result_available_at"]) > cut:
                raise ValueError("forecast consumed future results")
            if pred["model"] == "B1":
                previous_predictions[year].append(
                    (f, pred["probabilities"]["total_over_2_5"])
                )
            # A later withdrawal may remove an evaluation label. It cannot erase this earlier forecast from a training fold.
            if not is_test or not settled_label:
                continue
            joined[f.season] += 1
            outcome = (f.home_goals, f.away_goals)
            base = football_model.score_probabilities(pred["probabilities"], *outcome)
            adjusted = (
                calibrate_matrix(pred["matrix"], params)
                if params and pred["model"] == "B1"
                else None
            )
            newcomers = (
                newcomer_matrix(f, cut, engine, prior, incumbents)
                if pred["fallback_reason"] == "unseen_team"
                else None
            )
            candidates = []
            for name, mat in zip(NAMES, (adjusted, newcomers)):
                probabilities = (
                    football_model.probabilities(mat, ())[0]
                    if mat is not None
                    else pred["probabilities"]
                )
                score = football_model.score_probabilities(probabilities, *outcome)
                outputs[name].append((f.season, f.round, base, score, mat is not None))
                candidates.append(probabilities)
            event = (
                store.canonical(
                    [
                        f.fixture_id,
                        store.iso(cut),
                        ev["input_sha256"],
                        pred["probabilities"],
                        candidates,
                    ]
                )
                + "\n"
            ).encode()
            trace.update(event)
            fold_trace.update(event)
        if is_test:
            folds[-1]["predictions_sha256"] = fold_trace.hexdigest()
    results = {
        name: {
            "all": _summary(rows),
            "by_season": {
                season: _summary([r for r in rows if r[0] == season])
                for season in expected
            },
        }
        for name, rows in outputs.items()
    }
    for record in results.values():
        interval = record["all"]["interval"]
        record["conclusion"] = (
            "insufficient_paired_rounds"
            if interval is None
            else "no_established_improvement"
            if interval["lower"] <= 0
            else "positive_in_this_historical_protocol"
        )
    result = {
        "reproduce": f"pm861 --data-dir RUNTIME experiment football-study INPUT --from-season {first} --to-season {last}"
        + (" --archive-lag-hours 24" if not strict else "")
        + (" --evidence EVIDENCE" if evidence else ""),
        "protocol": SPEC,
        "spec_sha256": store.digest(SPEC),
        "implementation_sha256": {
            name: hashlib.sha256(
                Path(__file__).with_name(name).read_bytes()
            ).hexdigest()
            for name in (
                "football_candidates.py",
                "football.py",
                "football_model.py",
                "experiment_statistics.py",
            )
        },
        "input_provenance": provenance,
        "test_seasons": [first, last],
        "paired_n": sum(joined.values()),
        "denominator": sum(expected.values()),
        "forecast_omissions": dict(omissions),
        "coverage_by_season": {s: [joined[s], expected[s]] for s in expected},
        "folds": folds,
        "results": results,
        "paired_predictions_sha256": trace.hexdigest(),
        "evidence_grade": "verified_historical"
        if strict and acceptable
        else "strict_without_verified_coverage"
        if strict
        else "24h_sensitivity"
        if acceptable
        else "unverified_inputs",
        "operational_status": "research_only",
        "limitations": [
            "Historical seasons were reviewed before this study. This is a rolling-origin historical comparison. The timing of a sensitivity run is assumed.",
            "Primary intervals compare two candidate recipes against the same incumbent. Item and per-season summaries are descriptive. No fees, market edge or returns are evaluated.",
        ],
    }
    store.save_report(data_dir, "candidate-study", store.digest(result), result)
    return result


def register(subcommands):
    command = subcommands.add_parser(
        "football-study",
        help="walk-forward totals calibration and newcomer-prior evidence; research only",
    )
    command.add_argument("root", type=Path)
    command.add_argument("--from-season", default="2021-22")
    command.add_argument("--to-season", default="2025-26")
    command.add_argument("--evidence", type=Path)
    command.add_argument("--archive-lag-hours", type=float)
    command.add_argument("--output", type=Path)

    def run(a):
        result = study(
            a.data_dir,
            a.root,
            first=a.from_season,
            last=a.to_season,
            evidence=a.evidence,
            archive_lag_hours=a.archive_lag_hours,
        )
        if a.output:
            football._write_json(a.output, result)
        return result

    command.set_defaults(func=run)
