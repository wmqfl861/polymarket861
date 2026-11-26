"""Read-only local news-source diagnostics for the published EPL v2 experiment."""

from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3

from . import (
    football, news, news_endpoint_originals as originals, news_endpoint_study as v1,
    news_endpoint_v2 as v2, news_evaluation, news_model, news_scope, news_sources,
    news_study_plan, pipeline, store,
)

EVIDENCE_ERRORS = (ValueError, KeyError, IndexError, TypeError, AttributeError, OSError, sqlite3.Error)


def _description(config):
    return {
        "source": config["id"], "kind": config["kind"], "license": config["license"],
        "license_url": config["license_url"], "rights": list(config["rights"]),
        "raw_response_policy": "retainable" if originals.retainable(config) else "not_retainable",
    }


def _local_source_rows(db, tables, as_of):
    streams, local = {}, []
    if "news_streams" in tables:
        for row in db.execute(
            """SELECT * FROM news_streams WHERE season IN (?,?) AND registered_at<=?
               ORDER BY season,team_key,source""", (*v1.SEASONS, as_of)
        ):
            key = (row["season"], row["team_key"], row["source"])
            try:
                config = news_sources.validate_source(json.loads(row["config"]))
                if (config["id"], football.team_key(config["team"])) != (key[2], key[1]):
                    raise ValueError("subscription identity differs")
                if config.get("season", key[0]) != key[0]:
                    raise ValueError("subscription season differs")
                info = _description(config)
            except EVIDENCE_ERRORS:
                info = {"source": key[2], "raw_response_policy": "unknown"}
            streams[key] = info, row["registered_at"]
            local.append(info | {"season": key[0], "team": row["team"], "type": "recorded_subscription",
                                 "registered_at": row["registered_at"]})
    if "news_feed_config" in tables:
        for row in db.execute(
            """SELECT * FROM news_feed_config WHERE registered_at<=? AND
               (disabled_at IS NULL OR disabled_at>?) ORDER BY id""", (as_of, as_of)
        ):
            try:
                config = news_sources.validate_source(json.loads(row["config"]))
                if config["id"] != row["id"] or football.team_key(config["team"]) != row["team_key"]:
                    raise ValueError("configured feed identity differs")
                info = _description(config)
            except EVIDENCE_ERRORS:
                info = {"source": row["id"], "raw_response_policy": "unknown"}
            local.append(info | {"team_key": row["team_key"], "type": "enabled_custom_feed"})
    return streams, local


def _retention(streams, season, team, source, cutoff):
    item = streams.get((season, football.team_key(team), source))
    return item[0]["raw_response_policy"] if item and item[1] <= cutoff else "unknown"


def _original(bound, func, *args):
    if not bound:
        return "no_v2_binding"
    try:
        func(*args)
        return "verified"
    except EVIDENCE_ERRORS:
        return "missing_or_invalid"


def _fact(db, root, fixture, ident, cutoff, streams, bound, cache, rosters):
    result = {"id": ident, "source": None, "raw_response_policy": "unknown"}
    try:
        row = db.execute(
            """SELECT c.payload,a.source,a.team_key,a.season,p.receipt
            FROM news_claims c JOIN news_articles a ON a.seq=c.article
            JOIN news_article_proofs p ON p.article=c.article WHERE c.id=? AND a.fixture_id=?""",
            (ident, fixture["fixture_id"]),
        ).fetchone()
        if row:
            claim = json.loads(row["payload"])
            if (store.digest(claim) == ident and claim["source"] == row["source"]
                and football.team_key(claim["team"]) == row["team_key"]
                and row["season"] == fixture["season"] and claim["known_at"] <= cutoff
                and claim["source_receipt"] == row["receipt"]):
                result.update(
                    source=row["source"],
                    raw_response_policy=_retention(streams, row["season"], claim["team"], row["source"], cutoff),
                )
    except EVIDENCE_ERRORS:
        pass
    result["v2_original"] = _original(bound, originals._fact, db, root, ident, fixture, cutoff, cache, rosters)
    return result


def _one(db, root, row, registration, streams, binding, recipe_cache):
    bound = binding is not None
    info = {"fixture_id": row["fixture_id"], "kickoff": row["kickoff"], "horizon": row["horizon"],
            "sealed_at": row["sealed_at"], "seal": row["digest"], "v2_binding": bound,
            "applied_facts": [], "fact_dependencies": [], "coverage": {}, "known_blockers": []}
    if not row["is_object"]:
        raise ValueError("sealed forecast is not a JSON object")
    seal = pipeline._verified_seal(row)
    if (row["protocol"] != store.PROTOCOL or row["mode"] not in v2.MODES
            or not (seal.get("purpose") == "main" or
                    (seal.get("purpose") is None and row["model_version"] == "B1"))):
        raise ValueError("seal outside the native main forecast protocol")
    pair = seal["news_pair"]
    snapshot = seal["context"]["player_news"]
    cut = store.iso(seal["input_cutoff"])
    if (seal["season"] not in v1.SEASONS or not row["fixture_id"].startswith(f"{v1.COMPETITION}/{seal['season']}/")
        or cut != pair["input_cutoff"] or cut != snapshot["cutoff"] or snapshot["fixture_id"] != row["fixture_id"]
        or pair["facts_sha256"] != store.digest(snapshot["facts"]) or pair["coverage"] != snapshot["coverage"]
        or pair["availability_basis"] != news_model.LOCAL):
        raise ValueError("inconsistent sealed local news inputs")
    if news.snapshot(root, row["fixture_id"], cut) != snapshot:
        raise ValueError("frozen snapshot differs from reopened local news")
    policy = {"protocol": pair["protocol"], "sha256": pair["policy_sha256"]}
    info["sealed_news_policy"] = policy
    blockers = info["known_blockers"]
    if policy != (registration["news_policy"] if registration else v2._policy()):
        blockers.append("incompatible_news_policy")
    deadline = store.instant(row["kickoff"]) - v1.HORIZONS[row["horizon"]]
    if (store.iso(deadline) != row["cutoff"]
        or not store.iso(deadline - timedelta(minutes=5)) <= cut <= row["sealed_at"] <= row["cutoff"]):
        blockers.append("invalid_fixed_window")
    era, declaration = news_evaluation._build(db, seal, recipe_cache)
    if declaration != "declared" or (seal.get("news_study") or {}).get("plan_sha256") != news_study_plan.PLAN_SHA256:
        blockers.append("missing_or_incompatible_native_declaration")
    if binding and (binding["probability_era"] != era
                    or not registration["starts_at"] <= cut <= binding["bound_at"] <= row["cutoff"]):
        blockers.append("invalid_v2_binding")
    info["timely_two_team_coverage"] = news_study_plan.timely_coverage(pair)
    if not info["timely_two_team_coverage"]:
        blockers.append("missing_timely_two_team_coverage")
    fixture = {"fixture_id": row["fixture_id"], "season": seal["season"]}
    cache, rosters, dependencies = {}, set(), []
    for label in ("home", "away"):
        side = snapshot["coverage"][label]
        team = side["team"]
        sources = []
        for src in side["sources"]:
            if news_study_plan._study_source(src):
                sources.append({
                    "source": src["id"],
                    "raw_response_policy": _retention(streams, seal["season"], team, src["id"], cut),
                    "v2_original": _original(bound, originals._coverage_poll, db, root, fixture, side, src, cut, cache),
                })
        side_info = {"team": team, "sources": sources}
        dependencies.extend(sources)
        roster = side.get("roster") or {}
        if sources and roster.get("state") == "available":
            state = _original(bound, originals._roster, db, root, team, seal["season"], roster)
            side_info["roster_original"] = state
            if state == "verified":
                rosters.add((team, roster["digest"], roster["seen_at"]))
            if state == "missing_or_invalid":
                blockers.append("missing_or_invalid_v2_roster")
        info["coverage"][label] = side_info
    fact_ids = {fact["id"]: sorted({fact["id"], *fact.get("evidence_ids", ())}) for fact in snapshot["facts"]}
    for item in pair["applied"]:
        support = set(fact_ids[item["id"]])
        if item.get("duration_evidence_id"):
            support.update(fact_ids[item["duration_evidence_id"]])
        info["applied_facts"].append({"id": item["id"], "evidence_ids": sorted(support)})
    for ident in sorted({i for ids in fact_ids.values() for i in ids}):
        info["fact_dependencies"].append(_fact(db, root, fixture, ident, cut, streams, bound, cache, rosters))
    dependencies.extend(info["fact_dependencies"])
    if any(item["raw_response_policy"] == "not_retainable" for item in dependencies):
        blockers.append("required_source_does_not_retain_originals")
    if any(item["v2_original"] == "missing_or_invalid" for item in dependencies):
        blockers.append("missing_or_invalid_v2_original")
    if blockers:
        state = "expected_ineligible"
    elif bound and dependencies and all(d["v2_original"] == "verified" for d in dependencies):
        state = "bound_news_originals_verified"
    else:
        state = "unknown"
    return info | {"news_evidence": state}


# Count the complete population. The detail limit bounds original re-parses,
# never the denominator. A registered window uses its first actual binding.
# Else use the first native main seal as a descriptive sample across both modes.
_WINDOWS = """
WITH candidates AS (
 SELECT s.rowid AS seq,s.*,{binding_fields},
        CASE WHEN json_valid(s.data_json) THEN json_type(s.data_json)='object' ELSE 0 END AS is_object
 FROM forecast_seals s {binding_join}
 WHERE s.horizon IN (?,?) AND (s.fixture_id GLOB ? OR s.fixture_id GLOB ?) AND s.kickoff<?
), windows AS (
 SELECT seq,fixture_id,kickoff,horizon,sealed_at,bound_at,probability_era,is_object,
        row_number() OVER (PARTITION BY fixture_id,kickoff,horizon
                           ORDER BY capture_order IS NULL,capture_order,seq) AS ord
 FROM candidates
 WHERE capture_order IS NOT NULL OR (
    protocol=? AND mode IN (?,?) AND sealed_at<=? AND
    CASE WHEN is_object THEN coalesce(json_extract(data_json,'$.purpose')='main',model_version='B1') ELSE 1 END
 )
)
SELECT s.*,w.bound_at,w.probability_era,w.is_object,count(*) OVER () AS total_windows
FROM windows w JOIN forecast_seals s ON s.rowid=w.seq
WHERE w.ord=1 ORDER BY w.sealed_at DESC,w.seq DESC LIMIT ?
"""


def preflight(data_dir, *, limit=20):
    if not 1 <= limit <= 200:
        raise ValueError("news endpoint preflight limit must be between 1 and 200")
    root = Path(data_dir).expanduser()
    path = root / "runtime.sqlite3"
    as_of = store.now_utc()
    result = {
        "version": v1.V2_VERSION, "plan_sha256": v2.PUBLISHED_PLAN_SHA256, "as_of": as_of,
        "until_exclusive": v1.END, "installed_news_policy": v2._policy(),
        "registration": {"state": "not_registered"}, "local_sources": [],
        # A representative member supplies the built-in descriptors; these are not observations.
        "default_source_policies": [_description(s) for s in news_sources.default_sources(
            news_scope.WIRE_MEMBERSHIP["opening_round"][0][0], v1.SEASONS[0])],
        "fixed_windows_total": 0, "fixed_windows_checked": 0, "limit": limit, "windows": [],
        "news_evidence_counts": {"expected_ineligible": 0, "unknown": 0, "bound_news_originals_verified": 0},
        "future_eligible_windows": None, "decision": "insufficient_local_evidence",
    }
    if not path.is_file():
        v2.published_proposal("unregistered")
        return result
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        registration = v2._registration(db) if "news_endpoint_declarations" in tables else None
        if registration is None:
            v2.published_proposal("unregistered")
        else:
            result["registration"] = {
                "state": "registered", **{k: registration[k] for k in
                    ("registration_sha256", "starts_at", "ends_at", "news_policy")},
                "matches_installed_news_policy": registration["news_policy"] == v2._policy(),
            }
            originals.scope_originals(db, originals.V2_ORIGINALS, registration["registration_sha256"])
        streams, result["local_sources"] = _local_source_rows(db, tables, as_of)
        recipe_cache = {}
        rows = ()
        if "forecast_seals" in tables:
            clock = min(as_of, store.iso(store.instant(v1.END) - timedelta(microseconds=1)))
            fields = "NULL AS capture_order,NULL AS bound_at,NULL AS probability_era"
            join = ""
            params = []
            if registration:
                fields = "b.capture_order,b.bound_at,b.probability_era"
                join = "LEFT JOIN news_endpoint_v2_bindings b ON s.digest=b.seal AND b.declaration=? AND b.bound_at<=?"
                params.extend([registration["registration_sha256"], clock])
            patterns = [f"{v1.COMPETITION}/{season}/*" for season in v1.SEASONS]
            rows = db.execute(
                _WINDOWS.format(binding_fields=fields, binding_join=join),
                (*params, *v1.HORIZONS, *patterns, v1.END, store.PROTOCOL, *v2.MODES, clock, limit),
            )
        for row in rows:
            result["fixed_windows_total"] = row["total_windows"]
            binding = row if row["bound_at"] is not None else None
            try:
                window = _one(db, root, row, registration, streams, binding, recipe_cache)
            except EVIDENCE_ERRORS as exc:
                window = {"fixture_id": row["fixture_id"], "kickoff": row["kickoff"], "horizon": row["horizon"],
                          "seal": row["digest"], "v2_binding": binding is not None, "news_evidence": "unknown",
                          "reason": f"cannot_verify_local_seal: {exc}"}
            result["windows"].append(window)
            result["news_evidence_counts"][window["news_evidence"]] += 1
    result["fixed_windows_checked"] = len(result["windows"])
    counts = result["news_evidence_counts"]
    if registration:
        result["decision"] = "existing_registration"
    elif counts["expected_ineligible"]:
        result["decision"] = "review_observed_blockers_before_registration"
    elif result["windows"]:
        result["decision"] = "review_local_evidence_and_policy_freeze"
    return result
