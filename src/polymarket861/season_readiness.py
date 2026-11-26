"""Local, prospective checks for a season handoff before the first decision window."""

import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from . import (
    football,
    input_manifest,
    model_study,
    pipeline,
    result_freshness,
    schedule_consensus,
    store,
)
from .result_admission import scope


def release(root, at=None):
    """Compare executable and effective recipe without binding or approving either one."""
    try:
        return _release(root, at)
    except (ValueError, OSError, KeyError, TypeError, sqlite3.DatabaseError) as exc:
        return {
            "state": "unavailable",
            "ready": False,
            "active_version": None,
            "reason": f"Cannot verify the built-in release in this installation: {exc}. "
            "Check the installed files and the retained local recipe before sealing.",
        }


def _release(root, at):
    at = store.iso(at or store.now_utc())
    execution = model_study.release_provenance()
    with model_study.build_sources(execution):
        installed = store.digest(pipeline._build_recipe("B1", (0.5, 1.5, 2.5, 3.5)))
    with closing(model_study._db(root)) as db:
        db.execute("BEGIN")
        active = store.builtin_model_at(db, at)
        row = db.execute(
            "SELECT recipe FROM model_recipes WHERE version=?", (active,)
        ).fetchone()
        active_digest = store.digest(json.loads(row[0])) if row else None
        signed = db.execute(
            "SELECT r.effective_at,d.comparison,d.reviewer,d.reason FROM builtin_model_releases r "
            "JOIN model_release_decisions d ON d.release_id=r.id WHERE r.version=? AND r.effective_at<=? "
            "ORDER BY r.effective_at DESC,r.id DESC LIMIT 1",
            (active, at),
        ).fetchone()
        result = {
            "active_version": active,
            "active_recipe_sha256": active_digest,
            "installed_recipe_sha256": installed,
            "installed_release_provenance_sha256": store.digest(execution),
            "reference_python": model_study.REFERENCE_PYTHON,
            "running_python": execution["python"],
            "approval": dict(signed) if signed else None,
        }
        if active_digest == installed:
            return result | {"state": "ready", "ready": True}
        if (
            not row
            and active == "B1"
            and not db.execute(
                "SELECT 1 FROM forecast_seals WHERE model_version='B1' LIMIT 1"
            ).fetchone()
        ):
            return result | {"state": "first_use", "ready": True}
        baseline = db.execute(
            "SELECT sha256 FROM probability_studies WHERE recipe=? AND sample=?",
            (active_digest, store.digest(model_study.BENCHMARK)),
        ).fetchone()
        comparison = db.execute(
            "SELECT digest,body FROM model_release_comparisons WHERE "
            "json_extract(body,'$.from_version')=? AND json_extract(body,'$.to_version')=? "
            "ORDER BY rowid DESC LIMIT 1",
            (active, "B1@" + installed),
        ).fetchone()
    report_sha = (
        comparison[0]
        if comparison and store.digest(json.loads(comparison[1])) == comparison[0]
        else None
    )
    actions = []
    if not baseline and active_digest != model_study.LEGACY_RECIPE:
        actions.append(
            "Use the incumbent installation against this data directory to run forecast release-baseline HISTORY."
        )
    if not report_sha:
        actions.append("Run forecast release-compare HISTORY with the installed build.")
    approval = f"forecast release-approve {report_sha or 'REPORT_SHA256'} --reviewer NAME --reason REASON"
    if not baseline and active_digest == model_study.LEGACY_RECIPE:
        approval += " --legacy-reference VERIFIED_PREDECESSOR_COMMIT"
    actions.append(f"Review the comparison and run {approval}.")
    return result | {
        "state": "approval_required",
        "ready": False,
        "baseline_sha256": baseline[0] if baseline else None,
        "comparison_sha256": report_sha,
        "reason": "New built-in seals require a named release approval before the first window. "
        + " ".join(actions),
        "actions": actions,
    }


def training(root, season, at):
    result = {
        "as_of": at,
        "season": season,
        "manifest_sha256": None,
        "training_matches": None,
    }
    try:
        manifest = input_manifest.verify_observed_inputs(root)
        result["manifest_sha256"] = manifest["digest"]
        fixtures = football.load_fixtures(
            manifest["root"], evidence_path=manifest["evidence_path"]
        )
        cutoff = store.instant(at)
        engine = football.FootballEngine(fixtures)
        if season not in engine.seasons:
            raise ValueError(
                "requested season is absent from the observed football inputs"
            )
        first = scope(football.season_year(season) - 3)
        eligible = [
            f
            for f in fixtures
            if first <= f.season <= season
            and football._result_at(f, cutoff) is not None
        ]
        if not eligible:
            return result | {
                "state": "unavailable",
                "training_matches": 0,
                "reason": "no eligible result in the verified training window",
            }
        evidence = engine.fit(season, cutoff, None).evidence
        return (
            result
            | evidence
            | {
                "state": "available",
                "matches_by_season": dict(
                    sorted(Counter(f.season for f in eligible).items())
                ),
                "season_matches_by_round": dict(
                    sorted(
                        Counter(f.round for f in eligible if f.season == season).items()
                    )
                ),
            }
        )
    except (ValueError, OSError, TypeError, KeyError, sqlite3.DatabaseError) as exc:
        return result | {"state": "unavailable", "reason": str(exc)}


def _coordinator(root, at):
    with closing(store.connect(root)) as db:
        installed = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='season_services'"
        ).fetchone()
        heartbeat = (
            db.execute(
                "SELECT updated FROM season_services WHERE service='heartbeat'"
            ).fetchone()
            if installed
            else None
        )
    stamp = heartbeat[0] if heartbeat else None
    age = (store.instant(at) - store.instant(stamp)).total_seconds() if stamp else None
    if age is None:
        state = "unobserved"
    elif age < 0:
        state = "future_timestamp"
    else:
        state = "current" if age <= 90 else "stale"
    return {"state": state, "last_local_heartbeat": stamp, "age_seconds": age, "fresh_limit_seconds": 90}


def report(root, year, round_id=None):
    if not 2000 <= year <= 2100 or round_id is not None and not 1 <= round_id <= 38:
        raise ValueError("select an EPL season year and a matchweek from 1 through 38")
    season = scope(year)
    schedules = schedule_consensus.readiness(root, season=season, round_id=round_id)
    at = schedules["as_of"]
    results = result_freshness.status(root, year, at, round_id=round_id)
    model = release(root, at)
    sample = training(root, season, at)
    first = None
    if schedules["fixtures"]:
        f = schedules["fixtures"][0]
        cutoff = store.instant(f["kickoff"]) - pipeline.HORIZONS["T-24h"]
        first = {
            "fixture_id": f["fixture_id"],
            "horizon": "T-24h",
            "opens": store.iso(cutoff - timedelta(minutes=5)),
            "cutoff": store.iso(cutoff),
        }
    windows = []
    for f in schedules["fixtures"]:
        for horizon, offset in pipeline.HORIZONS.items():
            cutoff = store.instant(f["kickoff"]) - offset
            if store.iso(cutoff) >= at:
                opens = store.iso(cutoff - timedelta(minutes=5))
                windows.append({
                    "fixture_id": f["fixture_id"], "horizon": horizon, "opens": opens,
                    "cutoff": store.iso(cutoff), "window_open": opens <= at,
                    "fixed_b1_block_reason": f["fixed_b1_block_reason"],
                })
    next_window = min(windows, key=lambda w: (w["cutoff"], w["fixture_id"]), default=None)
    last = results["latest_round"]
    checks = {
        "reported_round_schedule_confirmed": schedules["state"] == "ready",
        "release_executable": model["ready"],
        "training_inputs_available": sample["state"] == "available",
        "latest_due_results_received": results["state"] != "unavailable"
        and last["received"] == last["due"],
        "next_round_teams_fresh": results["next_round"] is not None
        and not results["next_round_blocked_fixtures"],
    }
    if "requested_round" in results:
        projected = results["requested_round"]
        checks["reported_round_teams_fresh"] = (
            bool(projected["teams"]) and not projected["blocked_fixtures"]
        )
    return {
        "schema": "epl-season-readiness-v1",
        "as_of": at,
        "season": season,
        "data_dir": str(Path(root).expanduser().resolve()),
        "checks": checks,
        "state": "ready" if all(checks.values()) else "attention",
        "first_registered_window": first,
        "next_registered_window": next_window,
        "coordinator": _coordinator(root, at),
        "schedule": schedules,
        "release": model,
        "results": results,
        "training": sample,
    }
