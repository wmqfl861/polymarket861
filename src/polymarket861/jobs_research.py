"""Pre-registered paper experiments with evidence-derived comparisons and reversible promotion."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import numpy as np

from . import experiment_statistics as stats
from . import paired_proof
from . import store
from .football_model import ITEM_KEYS, score_probabilities


_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_candidates (
    key TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL, version TEXT NOT NULL,
    baseline TEXT NOT NULL, season TEXT NOT NULL, first INTEGER NOT NULL, span INTEGER NOT NULL,
    hypothesis TEXT NOT NULL, manifest TEXT NOT NULL, digest TEXT NOT NULL,
    registered_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'shadow', assessment TEXT,
    UNIQUE(kind,scope,version,season,first)
);
CREATE TABLE IF NOT EXISTS research_active (
    kind TEXT NOT NULL, scope TEXT NOT NULL, version TEXT NOT NULL, candidate_key TEXT,
    PRIMARY KEY(kind,scope)
);
CREATE TABLE IF NOT EXISTS research_events (
    id INTEGER PRIMARY KEY, key TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL,
    evidence TEXT NOT NULL, UNIQUE(key,action,evidence)
);
CREATE TABLE IF NOT EXISTS research_window_reviews (
    target TEXT NOT NULL, results_digest TEXT NOT NULL, claim TEXT NOT NULL, state TEXT NOT NULL,
    updated_at TEXT NOT NULL, outcome TEXT, PRIMARY KEY(target,results_digest)
);
"""
_BOOTSTRAP_SAMPLES = 4000
_SEED = 861


def _open(data_dir):
    conn = store.connect(data_dir)
    conn.executescript(_SCHEMA)
    return conn


def _event(conn, key, action, evidence):
    conn.execute(
        "INSERT OR IGNORE INTO research_events(key,action,at,evidence) VALUES(?,?,?,?)",
        (key, action, store.now_utc(), store.canonical(evidence)),
    )


def _season(value):
    match = re.fullmatch(r"(\d{4})[-/](\d{2}|\d{4})", str(value))
    if not match or int(match[2]) % 100 != (int(match[1]) + 1) % 100:
        raise ValueError("Use an EPL season such as 2026-27")
    if len(match[2]) == 4 and int(match[2]) != int(match[1]) + 1:
        raise ValueError("Invalid season end year")
    return f"{int(match[1]):04}-{int(match[2]) % 100:02}"


def _window(value):
    try:
        season, start = value.rsplit("/", 1)
        return _season(season), int(start)
    except (AttributeError, ValueError) as exc:
        raise ValueError(
            "Use a window such as 2026-27/12 (season and first official round)"
        ) from exc


def _candidate(row):
    record = dict(row)
    record["manifest"] = json.loads(record["manifest"])
    record["assessment"] = (
        json.loads(record["assessment"]) if record["assessment"] else None
    )
    record["window"] = f"{record['season']}/{record['first']}"
    return record


def _get(conn, key):
    row = conn.execute(
        "SELECT * FROM research_candidates WHERE key=?", (key,)
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown experiment: {key}")
    return _candidate(row)


def get_candidate(data_dir, key):
    with closing(_open(data_dir)) as conn:
        return _get(conn, key)


def _status(conn):
    candidates = [
        _candidate(r)
        for r in conn.execute(
            "SELECT * FROM research_candidates ORDER BY registered_at,key"
        )
    ]
    active = [
        dict(r)
        for r in conn.execute("SELECT * FROM research_active ORDER BY kind,scope")
    ]
    events = [
        dict(r) | {"evidence": json.loads(r["evidence"])}
        for r in conn.execute("SELECT * FROM research_events ORDER BY id")
    ]
    return {
        "candidates": candidates,
        "active": active,
        "events": events,
        "registered_trials": len(candidates),
    }


def experiment_status(data_dir):
    with closing(_open(data_dir)) as conn:
        return _status(conn)


def _pointer(conn, kind, scope, at):
    if at is None:
        row = conn.execute(
            "SELECT version,candidate_key FROM research_active WHERE kind=? AND scope=?",
            (kind, scope),
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)
    row = conn.execute(
        "SELECT e.action,e.evidence,c.key,c.version FROM research_events e "
        "JOIN research_candidates c ON e.key=c.key WHERE c.kind=? AND c.scope=? "
        "AND e.at<=? AND e.action IN ('promoted','rolled_back') ORDER BY e.at DESC,e.id DESC LIMIT 1",
        (kind, scope, store.iso(at)),
    ).fetchone()
    if not row:
        return None, None
    if row["action"] == "promoted":
        return row["version"], row["key"]
    undo = json.loads(row["evidence"])
    return undo["restored_version"], undo.get("restored_candidate")


def active_version(data_dir, kind="model", scope="epl", default=None, *, at=None):
    with closing(_open(data_dir)) as conn:
        version, _ = _pointer(conn, kind, scope, at)
        return version if version is not None else default


def active_candidate(data_dir, kind="model", scope="epl", *, at=None):
    with closing(_open(data_dir)) as conn:
        _, key = _pointer(conn, kind, scope, at)
        return _get(conn, key) | {"state": "promoted"} if key else None


def _window_fixtures(schedule, season, first, span):
    return [
        f
        for f in schedule
        if _season(f["season"]) == season and first <= int(f["round_id"]) < first + span
    ]


def _cutoff(fixture):
    return store.instant(fixture["kickoff"]) - timedelta(hours=1)


def _bound_recipe(data_dir, version):
    with closing(store.connect(data_dir)) as conn:
        row = conn.execute(
            "SELECT recipe FROM model_recipes WHERE version=?", (version,)
        ).fetchone()
    if row:
        return store.digest(json.loads(row[0]))
    if version in {"B0", "B1", "B2"}:
        from .pipeline import register_recipe

        return register_recipe(data_dir, version, version)["recipe_digest"]
    return None


def register_candidate(
    data_dir,
    key,
    *,
    kind,
    version,
    baseline,
    window,
    hypothesis,
    manifest=None,
    scope=None,
):
    manifest = {} if manifest is None else manifest
    if kind not in {"model", "source"} or not isinstance(manifest, dict):
        raise ValueError("Paper experiments support model and source manifests")
    scope = scope or ("epl" if kind == "model" else manifest.get("field"))
    if (
        not all(
            isinstance(s, str) and s.strip()
            for s in (key, scope, version, baseline, hypothesis)
        )
        or version == baseline
    ):
        raise ValueError(
            "Supply key, scope, distinct candidate and baseline versions, and a hypothesis"
        )
    season, first = _window(window)
    # A repeated old registration uses its original protocol and digest, including its 12-round window.
    with closing(_open(data_dir)) as conn:
        old_row = conn.execute(
            "SELECT * FROM research_candidates WHERE key=?", (key,)
        ).fetchone()
    prior = _candidate(old_row) if old_row else None
    manifest = dict(manifest)
    if not prior or "activation_policy" in prior["manifest"]:
        manifest.setdefault(
            "activation_policy",
            "named_review" if kind == "model" else "auto_independent_reference",
        )
        allowed = (
            {"named_review"}
            if kind == "model"
            else {"named_review", "auto_independent_reference"}
        )
        if manifest["activation_policy"] not in allowed:
            raise ValueError("invalid activation policy for this type of experiment")
    if kind == "model" and (not prior or "inference" in prior["manifest"]):
        manifest.setdefault("inference", stats.PROTOCOL)
        if manifest["inference"] != stats.PROTOCOL:
            raise ValueError(
                "new model trials require the registered round-cluster t protocol"
            )
    span = manifest.get(
        "rounds", prior["span"] if prior else (38 if kind == "model" else 12)
    )
    if (
        type(span) is not int
        or span < 2
        or not 1 <= first <= 39 - span
        or (kind == "model" and span != (prior["span"] if prior else 38))
    ):
        raise ValueError(
            "new model windows cover all 38 official rounds from round one; overlapping or shifted windows cannot be registered"
        )
    if kind == "source" and (
        manifest.get("field") != scope
        or not manifest.get("reference_source")
        or manifest["reference_source"] in {version, baseline}
    ):
        raise ValueError(
            "A source experiment requires a field scope and a separate reference_source"
        )
    if "recipe_digest" in manifest and not re.fullmatch(
        r"[0-9a-f]{64}", str(manifest["recipe_digest"])
    ):
        raise ValueError("recipe_digest must be a SHA-256 fingerprint")
    if kind == "model":
        manifest = dict(manifest)
        manifest.setdefault("comparison_policy", paired_proof.POLICY)
        if manifest["comparison_policy"] != paired_proof.POLICY:
            raise ValueError("unregistered input-comparison policy")
        pinned = manifest.get("historical_snapshots", {})
        if not isinstance(pinned, dict) or any(
            not re.fullmatch(r"\d{4}-\d{2}", str(k))
            or not re.fullmatch(r"[0-9a-f]{64}", str(v))
            for k, v in pinned.items()
        ):
            raise ValueError(
                "historical_snapshots must bind season names to registered snapshot hashes"
            )
        for tag, ident in (
            ("recipe_digest", version),
            ("baseline_recipe_digest", baseline),
        ):
            actual = _bound_recipe(data_dir, ident)
            if actual and manifest.get(tag, actual) != actual:
                raise ValueError(
                    f"{tag} does not match the executable registered recipe"
                )
            if actual:
                manifest[tag] = actual
    spec = {
        "kind": kind,
        "scope": scope,
        "version": version,
        "baseline": baseline,
        "season": season,
        "first": first,
        "span": span,
        "hypothesis": hypothesis,
        "manifest": manifest,
    }
    if kind == "model":
        _history_seasons(spec)
        if scope != "epl":
            raise ValueError("This model registry scores EPL fixtures")
    if kind == "source" and manifest.get("unit", "team") not in {"team", "fixture"}:
        raise ValueError("A source observation unit must be team or fixture")
    if kind == "source" and manifest.get("reference_time", "result") not in {
        "decision",
        "kickoff",
        "result",
        "reveal",
    }:
        raise ValueError(
            "Choose a fixed source adjudication time: decision, kickoff, result, or reveal"
        )
    fingerprint = store.digest(spec)
    fixtures = _window_fixtures(store.eligible_fixtures(data_dir), season, first, span)
    with closing(_open(data_dir)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM research_candidates WHERE key=?", (key,)
        ).fetchone():
            prior = _get(conn, key)
            if prior["digest"] != fingerprint:
                raise ValueError("A registered experiment cannot be edited")
            return prior
        now = store.now_utc()
        if kind == "model":
            stats.require_unopened_season(season, fixtures, now)
        if fixtures and min(map(_cutoff, fixtures)) <= store.instant(now):
            raise ValueError("The first decision deadline for this window has passed")
        active = conn.execute(
            "SELECT version FROM research_active WHERE kind=? AND scope=?",
            (kind, scope),
        ).fetchone()
        if active and active[0] != baseline:
            raise ValueError("Baseline must equal the active local version")
        if kind == "model" and scope == "epl" and not active and baseline != "B1":
            raise ValueError("The initial EPL champion is B1")
        peers = conn.execute(
            "SELECT kind,scope,first,span,baseline FROM research_candidates WHERE season=?",
            (season,),
        ).fetchall()
        cohort = [p for p in peers if p["first"] == first and p["span"] == span]
        if len(cohort) >= 3:
            raise ValueError("This window already has three registered candidates")
        for p in peers:
            if p["kind"] != kind or p["scope"] != scope:
                continue
            if first < p["first"] + p["span"] and p["first"] < first + span:
                if (p["first"], p["span"], p["baseline"]) != (first, span, baseline):
                    raise ValueError(
                        "Experiments on the same target require identical or nonoverlapping windows and a common baseline"
                    )
        conn.execute(
            "INSERT INTO research_candidates(key,kind,scope,version,baseline,season,first,span,hypothesis,manifest,digest,registered_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                kind,
                scope,
                version,
                baseline,
                season,
                first,
                span,
                hypothesis,
                store.canonical(manifest),
                fingerprint,
                now,
            ),
        )
        _event(
            conn,
            key,
            "registered",
            {"digest": fingerprint, "protocol": store.PROTOCOL, "mode": "paper"},
        )
        store.put_report(conn, "experiment", "latest", _status(conn))
        return _get(conn, key)


def _complete_rounds(fixtures, first, span):
    rounds = Counter(int(f["round_id"]) for f in fixtures)
    if len({f["fixture_id"] for f in fixtures}) != len(fixtures):
        return False
    clubs = defaultdict(set)
    for f in fixtures:
        for side in ("home", "away"):
            if not f.get(side):
                return False
            clubs[int(f["round_id"])].add(f[side].strip().casefold())
    return all(
        rounds[n] == 10 and len(clubs[n]) == 20 for n in range(first, first + span)
    )


def _closed(fixtures, first, span, as_of):
    if not _complete_rounds(fixtures, first, span):
        return "incomplete_official_schedule"
    for f in fixtures:
        if (
            f.get("home_goals") is None
            or f.get("away_goals") is None
            or not f.get("received_at")
            or store.instant(f["received_at"]) > as_of
            or store.instant(f["kickoff"]) >= as_of
        ):
            return "awaiting_official_results"
    return None


def _rows(data_dir, version, mode, expected, recipe_digest=None):
    rows = {}
    for row in store.evaluation_rows(
        data_dir, version, mode=mode, horizon="T-60m", protocol=store.PROTOCOL
    ):
        fid = row["fixture_id"]
        if fid not in expected:
            continue
        fixture = expected[fid]
        if (
            row["mode"] != mode
            or row["model_version"] != version
            or not row.get("digest")
            or store.instant(row["cutoff"]) != _cutoff(fixture)
            or store.instant(row["kickoff"]) != store.instant(fixture["kickoff"])
            or (row["home_goals"], row["away_goals"])
            != (fixture["home_goals"], fixture["away_goals"])
            or (
                mode == "formal"
                and store.instant(row["sealed_at"]) > store.instant(row["cutoff"])
            )
            or (recipe_digest and row.get("recipe_digest") != recipe_digest)
        ):
            continue
        if fid in rows:
            raise ValueError("Non-unique prediction evidence at one decision time")
        rows[fid] = row
    return rows


def _paired_evidence(data_dir, candidate, mode, fixtures, count):
    index = {f["fixture_id"]: f for f in fixtures}
    c_rows = _rows(
        data_dir,
        candidate["version"],
        mode,
        index,
        candidate["manifest"].get("recipe_digest"),
    )
    b_rows = _rows(
        data_dir,
        candidate["baseline"],
        mode,
        index,
        candidate["manifest"].get("baseline_recipe_digest"),
    )
    validator = paired_proof.PairValidator(data_dir)
    policy = candidate["manifest"].get("comparison_policy")
    pinned = candidate["manifest"].get("historical_snapshots", {})
    paired = []
    rejected = {}
    batches = set()
    for fid in sorted(c_rows.keys() & b_rows.keys()):
        c, b = c_rows[fid], b_rows[fid]
        reason = (
            "missing_registered_comparison_policy"
            if policy != paired_proof.POLICY
            else validator.check(c, b, index[fid], mode)
        )
        if not reason and mode == "replay":
            snapshot = c["comparison_proof"]["snapshot"]
            if pinned and pinned.get(index[fid]["season"]) != snapshot:
                reason = "snapshot_differs_from_registration"
            else:
                batches.add(snapshot)
        if reason:
            rejected[fid] = reason
        else:
            paired.append(fid)
    if mode == "replay" and not pinned and len(batches) > 1:
        rejected.update({fid: "mixed_unregistered_history_batches" for fid in paired})
        paired = []
    grouped = defaultdict(list)
    seals = []
    for fid in paired:
        c, b = c_rows[fid], b_rows[fid]
        goals = index[fid]["home_goals"], index[fid]["away_goals"]
        cg, bg = (
            score_probabilities(c["probabilities"], *goals),
            score_probabilities(b["probabilities"], *goals),
        )
        diffs = [cg["score"] - bg["score"]] + [
            cg["item_scores"][k] - bg["item_scores"][k] for k in ITEM_KEYS
        ]
        grouped[(_season(index[fid]["season"]), int(index[fid]["round_id"]))].append(
            diffs
        )
        seals.append(
            [fid, c["digest"], b["digest"], goals, index[fid].get("result_version")]
        )
    result = {
        "denominator": len(index),
        "candidate_count": len(c_rows),
        "baseline_count": len(b_rows),
        "paired_count": len(paired),
        "comparison_rejections": rejected,
        "comparison_policy": policy,
        "candidate_missing": sorted(index.keys() - c_rows.keys()),
        "baseline_missing": sorted(index.keys() - b_rows.keys()),
        "rounds": len(grouped),
        "seasons": sorted({k[0] for k in grouped}),
        "paired_evidence_digest": store.digest(seals),
    }
    if len(grouped) < 2:
        return result
    if candidate["manifest"].get("inference") == stats.PROTOCOL:
        names = ("overall", *ITEM_KEYS)
        calculated = stats.clustered_t(
            [v for _, v in sorted(grouped.items())], count * 2 * len(names)
        )
        result["intervals"] = dict(zip(names, calculated.pop("intervals")))
        result["inference"] = calculated | {
            "candidates": count,
            "phases": 2,
            "endpoints": list(names),
        }
        return result
    # Retain the originally registered interval for old, research-only short windows.
    # Sum/count weighting preserves equal fixture weights when a sampled round has missing pairs.
    sums = np.array([np.sum(v, axis=0) for _, v in sorted(grouped.items())])
    sizes = np.array([len(v) for _, v in sorted(grouped.items())])
    rng = np.random.default_rng(_SEED)
    draws = rng.integers(0, len(sizes), size=(_BOOTSTRAP_SAMPLES, len(sizes)))
    means = sums[draws].sum(axis=1) / sizes[draws].sum(axis=1)[:, None]
    tail = 0.025 / count
    lower, upper = np.quantile(means, [tail, 1 - tail], axis=0)
    observed = sums.sum(axis=0) / len(paired)
    result["intervals"] = {
        name: {
            "difference": float(observed[i]),
            "lower": float(lower[i]),
            "upper": float(upper[i]),
        }
        for i, name in enumerate(("overall", *ITEM_KEYS))
    }
    result["bootstrap"] = {
        "unit": "official_round",
        "resamples": _BOOTSTRAP_SAMPLES,
        "seed": _SEED,
        "candidates": count,
        "family_confidence": 0.95,
        "individual_confidence": 1 - 2 * tail,
    }
    return result


def _history_seasons(c):
    year = int(c["season"][:4])
    default = [f"{start:04}-{(start + 1) % 100:02}" for start in (year - 2, year - 1)]
    declared = c["manifest"].get("history_seasons", default)
    if not isinstance(declared, list):
        raise ValueError("history_seasons must be a list")
    seasons = sorted({_season(s) for s in declared})
    if len(seasons) < 2 or any(s >= c["season"] for s in seasons):
        raise ValueError("Pre-register at least two preceding seasons for replay")
    return seasons


def _paper_audit(data_dir, c, start, end, fixtures):
    from .ledger import audit_risk

    return audit_risk(
        data_dir,
        c["version"],
        account=c["manifest"].get("paper_account", c["version"]),
        window_start=store.iso(start),
        window_end=store.iso(end),
        protocol=store.PROTOCOL,
        fixture_ids=fixtures,
        mode="formal",
    )


def _model_assessment(data_dir, c, fixtures, schedule, count, as_of):
    start = min(map(_cutoff, fixtures))
    for tag, ident in (
        ("recipe_digest", c["version"]),
        ("baseline_recipe_digest", c["baseline"]),
    ):
        if not c["manifest"].get(tag) or c["manifest"][tag] != _bound_recipe(
            data_dir, ident
        ):
            return {
                "reason": "pre_registered_recipe_unverified",
                "model_version": ident,
            }
    try:
        store.require_formal(data_dir, at=store.iso(start))
    except ValueError:
        return {"reason": "window_outside_formal_paper_trial"}
    history = []
    for season in _history_seasons(c):
        season_games = _window_fixtures(schedule, season, 1, 38)
        if _closed(season_games, 1, 38, as_of):
            return {
                "reason": "incomplete_multiseason_replay_schedule",
                "season": season,
                "fixtures": len(season_games),
            }
        history.extend(season_games)
    replay = _paired_evidence(data_dir, c, "replay", history, count)
    forward = _paired_evidence(data_dir, c, "formal", fixtures, count)
    evidence = {"history": replay, "forward": forward}
    if (
        forward["paired_count"]
        < (int(0.95 * forward["denominator"]) if c["span"] == 38 else 100)
        or (c["span"] == 38 and forward["rounds"] != 38)
        or bool(forward["comparison_rejections"])
        or bool(replay["comparison_rejections"])
        or replay["paired_count"] != replay["denominator"]
        or set(replay["seasons"]) != set(_history_seasons(c))
        or not replay.get("intervals")
        or not forward.get("intervals")
    ):
        return evidence | {"reason": "insufficient_paired_evidence"}
    for name, phase in (("history", replay), ("forward", forward)):
        if phase["candidate_count"] < phase["baseline_count"]:
            return evidence | {"reason": f"{name}_coverage_declined", "complete": True}
        if phase["intervals"]["overall"]["lower"] <= 0:
            return evidence | {
                "reason": f"{name}_superiority_unproven",
                "complete": True,
            }
    if any(forward["intervals"][k]["lower"] < -0.5 for k in ITEM_KEYS):
        return evidence | {"reason": "forward_item_degradation", "complete": True}
    scored_fixtures = (
        {f["fixture_id"] for f in fixtures}
        - set(forward["candidate_missing"])
        - set(forward["baseline_missing"])
    )
    risk = _paper_audit(
        data_dir,
        c,
        start,
        max(store.instant(f["received_at"]) for f in fixtures),
        scored_fixtures,
    )
    evidence["risk"] = risk
    account = c["manifest"].get("paper_account", c["version"])
    verified_accounts = risk.get("accounts") or []
    if (
        not risk.get("passed")
        or not risk.get("checked_intents")
        or not risk.get("checked_fills")
        or not verified_accounts
        or any(
            a.get("account") != account
            or a.get("mode") != "formal"
            or a.get("role") != "candidate"
            or a.get("candidate_key") != c["key"]
            or a.get("candidate_version") != c["version"]
            or a.get("registration_digest") != c["digest"]
            for a in verified_accounts
        )
    ):
        return evidence | {"reason": "paper_risk_unverified"}
    return evidence | {
        "reason": "qualified",
        "qualified": True,
        "complete": True,
        "rank": forward["intervals"]["overall"]["difference"],
    }


def _wilson(correct, n):
    z = 1.959963984540054
    p = correct / n
    mid = p + z * z / (2 * n)
    delta = z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5
    divisor = 1 + z * z / n
    return [max(0.0, (mid - delta) / divisor), min(1.0, (mid + delta) / divisor)]


def _source_assessment(data_dir, c, fixtures, as_of):
    from .evidence import source_comparison, source_reference, source_report

    field, new, old = c["scope"], c["version"], c["baseline"]
    ref = c["manifest"]["reference_source"]
    try:
        registry = {ident: source_report(data_dir, ident) for ident in (new, old, ref)}
    except ValueError as exc:
        return {"reason": "missing_source_registry", "detail": str(exc)}
    primary = registry[new]
    uses = primary.get("fields", {}).get(field, {}).get("uses", [])
    if (
        primary.get("status") == "disabled"
        or primary.get("free") is not True
        or "prediction" not in uses
    ):
        return {"reason": "field_prediction_permission_unverified"}
    reference_origin = registry[ref].get("upstream")
    observed_origins = {
        new,
        old,
        registry[new].get("upstream"),
        registry[old].get("upstream"),
    }
    if not reference_origin or reference_origin in observed_origins:
        return {"reason": "reference_shares_upstream"}
    unit = c["manifest"].get("unit", "team")
    observations = []
    references = []
    for fixture in fixtures:
        teams = (fixture["home"], fixture["away"]) if unit == "team" else ("",)
        reference_times = {
            "result": fixture["received_at"],
            "kickoff": fixture["kickoff"],
            "decision": store.iso(_cutoff(fixture)),
            "reveal": store.iso(as_of),
        }
        reference_at = reference_times[c["manifest"].get("reference_time", "result")]
        for team in teams:
            obs = {
                "fixture_id": fixture["fixture_id"],
                "team": team,
                "cutoff": store.iso(_cutoff(fixture)),
                "round": fixture["round_id"],
                "subject": c["manifest"].get("subject"),
                "prospective": True,
            }
            witness = source_reference(
                data_dir,
                ref,
                fixture["fixture_id"],
                team,
                field,
                cutoff=reference_at,
                subject=obs["subject"],
                purpose="trial",
                prospective=True,
            )
            if (
                witness
                and witness.get("upstream") not in observed_origins
                and witness.get("origin_id") not in observed_origins
            ):
                obs.update(
                    expected_value=witness["value"],
                    reference_origin=witness["origin_id"],
                    reference_locator=witness["locator"],
                )
                references.append(witness["evidence_id"])
            observations.append(obs)
    summary = source_comparison(data_dir, [new, old], field, observations)
    test, base = summary["by_source"][new], summary["by_source"][old]
    n = summary["denominator"]
    info = {
        "acceptance_method": "field_reference_quality_contract",
        "comparison": summary,
        "reference_evidence_ids": references,
        "correct_rate_wilson95": _wilson(test["correct"], n) if n else None,
    }
    if (
        not summary["denominator_provided"]
        or summary["fixture_count"] < 20
        or len(summary["rounds"]) < 2
        or not n
    ):
        return info | {"reason": "insufficient_source_denominator"}
    # Every obtained candidate observation must pass independent identity, value and timing checks.
    if (
        test["conflicts"]
        or test["correct"] != test["on_time"]
        or test["verified_correct_rate"] < 0.95
    ):
        return info | {"reason": "source_quality_or_verification_shortfall"}
    if test["on_time"] < base["on_time"] or test["correct"] < base["correct"]:
        return info | {"reason": "source_coverage_declined", "complete": True}
    return info | {
        "reason": "qualified",
        "qualified": True,
        "complete": True,
        "rank": test["verified_correct_rate"],
    }


def _assess(data_dir, c, schedule, count, as_of):
    fixtures = _window_fixtures(schedule, c["season"], c["first"], c["span"])
    blocked = _closed(fixtures, c["first"], c["span"], as_of)
    if blocked:
        return {
            "reason": blocked,
            "scheduled": len(fixtures),
            "final_results": sum(f.get("home_goals") is not None for f in fixtures),
        }
    if store.instant(c["registered_at"]) >= min(map(_cutoff, fixtures)):
        return {"reason": "registration_after_first_cutoff", "complete": True}
    if c["kind"] == "source":
        return _source_assessment(data_dir, c, fixtures, as_of)
    return _model_assessment(data_dir, c, fixtures, schedule, count, as_of)


def evaluate_candidate(data_dir, key):
    return _judge(data_dir, key)


def approve_candidate(data_dir, key, *, reviewer, reason):
    signature = stats.signoff(reviewer, reason)
    previous = get_candidate(data_dir, key)
    if previous["state"] == "promoted":
        return previous
    if previous["state"] != "recommended":
        raise ValueError(
            "an independently qualified, recommended experiment must be evaluated before approval"
        )
    return _judge(data_dir, key, approval=signature)


def _judge(data_dir, key, *, approval=None):
    """A reader names the experiment. Only the independent stores supply scores and labels."""
    with closing(_open(data_dir)) as conn:
        requested = _get(conn, key)
        if requested["state"] in ("promoted", "rolled_back") or (
            (requested["assessment"] or {}).get("revealed") and approval is None
        ):
            return requested
        cohort = [
            _candidate(r)
            for r in conn.execute(
                "SELECT * FROM research_candidates WHERE season=? AND first=? AND span=? ORDER BY key",
                (requested["season"], requested["first"], requested["span"]),
            )
        ]
    peers = [
        c
        for c in cohort
        if (c["kind"], c["scope"]) == (requested["kind"], requested["scope"])
    ]
    schedule = store.eligible_fixtures(data_dir)
    now = store.instant(store.now_utc())
    decisions = {
        c["key"]: _assess(data_dir, c, schedule, len(cohort), now) for c in peers
    }
    qualified = [c for c in peers if decisions[c["key"]].get("qualified")]
    # The declared tie break makes the active pointer independent of CLI call order.
    winner = (
        sorted(qualified, key=lambda c: (-decisions[c["key"]]["rank"], c["key"]))[0]
        if qualified
        else None
    )
    reveal = bool(winner) or all(d.get("complete") for d in decisions.values())
    with closing(_open(data_dir)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _get(conn, key)
        if current["state"] in ("promoted", "rolled_back") or (
            (current["assessment"] or {}).get("revealed") and approval is None
        ):
            return current
        kind, scope = requested["kind"], requested["scope"]
        active = conn.execute(
            "SELECT * FROM research_active WHERE kind=? AND scope=?", (kind, scope)
        ).fetchone()
        if winner and active and active["version"] != winner["baseline"]:
            decisions[winner["key"]].update(
                reason="active_baseline_changed", qualified=False
            )
            winner = None
        for c in peers:
            if c["state"] in ("promoted", "rolled_back"):
                continue
            decision = decisions[c["key"]] | {
                "revealed": reveal,
                "registered_candidates": len(cohort),
                "protocol": store.PROTOCOL,
            }
            selected = winner is not None and c["key"] == winner["key"]
            switch = c["manifest"].get(
                "activation_policy",
                "auto_independent_reference" if kind == "source" else "named_review",
            )
            decision["activation_policy"] = switch
            legacy = kind == "model" and (
                c["span"] != 38 or c["manifest"].get("inference") != stats.PROTOCOL
            )
            if legacy:
                decision["activation_policy"] = "frozen_legacy_research"
            decision_id = store.digest(decision)
            prior_decision = (c["assessment"] or {}).get("recommendation_digest")
            confirmed = (
                selected
                and approval is not None
                and c["key"] == key
                and prior_decision == decision_id
            )
            promoted = (
                selected
                and not legacy
                and (switch == "auto_independent_reference" or confirmed)
            )
            if promoted:
                decision["previous_candidate"] = (
                    active["candidate_key"] if active else None
                )
                if confirmed:
                    decision["approved_by"] = approval
                conn.execute(
                    "INSERT INTO research_active VALUES(?,?,?,?) ON CONFLICT(kind,scope) DO UPDATE SET "
                    "version=excluded.version,candidate_key=excluded.candidate_key",
                    (kind, scope, c["version"], c["key"]),
                )
            recommended = selected and not legacy and not promoted
            state = (
                "promoted" if promoted else "recommended" if recommended else "shadow"
            )
            if selected and legacy:
                decision["reason"] = "frozen_legacy_window_research_only"
            elif recommended:
                decision["recommendation_digest"] = decision_id
                decision["reason"] = (
                    "evidence_changed_review_again"
                    if approval
                    else "named_approval_required"
                )
            elif not selected and decision.get("qualified"):
                decision["reason"] = "another_registered_candidate_ranked_higher"
            conn.execute(
                "UPDATE research_candidates SET state=?,assessment=? WHERE key=?",
                (state, store.canonical(decision), c["key"]),
            )
            _event(
                conn,
                c["key"],
                "promoted"
                if promoted
                else "recommended"
                if recommended
                else ("revealed" if reveal else "observed"),
                {
                    "reason": decision["reason"],
                    "assessment_digest": store.digest(decision),
                    **({"approval": approval} if confirmed and promoted else {}),
                },
            )
            store.put_report(conn, "experiment", c["key"], _get(conn, c["key"]))
        store.put_report(conn, "experiment", "latest", _status(conn))
        return _get(conn, key)


def rollback_candidate(data_dir, key, *, reason):
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Record the reason for rollback")
    with closing(_open(data_dir)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        c = _get(conn, key)
        active = conn.execute(
            "SELECT * FROM research_active WHERE kind=? AND scope=?",
            (c["kind"], c["scope"]),
        ).fetchone()
        if c["state"] == "rolled_back":
            return c
        if c["state"] != "promoted" or not active or active["candidate_key"] != key:
            raise ValueError("Only the currently active candidate may be rolled back")
        previous = c["assessment"]["previous_candidate"]
        conn.execute(
            "UPDATE research_active SET version=?,candidate_key=? WHERE kind=? AND scope=?",
            (c["baseline"], previous, c["kind"], c["scope"]),
        )
        conn.execute(
            "UPDATE research_candidates SET state='rolled_back' WHERE key=?", (key,)
        )
        _event(
            conn,
            key,
            "rolled_back",
            {
                "restored_version": c["baseline"],
                "restored_candidate": previous,
                "reason": reason,
            },
        )
        store.put_report(conn, "experiment", key, _get(conn, key))
        store.put_report(conn, "experiment", "latest", _status(conn))
        return _get(conn, key)


def review_due_experiments(data_dir, season, round_id):
    """Review a newly closed registered window once, including its last outstanding postponement."""
    import uuid

    season = _season(season)
    if type(round_id) is not int or not 1 <= round_id <= 38:
        raise ValueError("round_id must be an official EPL round")
    with closing(_open(data_dir)) as conn:
        candidates = [
            _candidate(r)
            for r in conn.execute(
                "SELECT * FROM research_candidates WHERE season=? AND first<=? AND first+span>? ORDER BY key",
                (season, round_id, round_id),
            )
        ]
    groups = defaultdict(list)
    for c in candidates:
        groups[(c["kind"], c["scope"], c["first"], c["span"])].append(c)
    response = {"season": season, "round_id": round_id, "windows": []}
    if not groups:
        return response
    schedule = store.eligible_fixtures(data_dir)
    now = store.instant(store.now_utc())
    for target, peers in groups.items():
        keys = [c["key"] for c in peers]
        window = {
            "candidates": keys,
            "first_round": target[2],
            "last_round": target[2] + target[3] - 1,
        }
        if all((c["assessment"] or {}).get("revealed") for c in peers):
            response["windows"].append(window | {"state": "already_revealed"})
            continue
        fixtures = _window_fixtures(schedule, season, target[2], target[3])
        pending = _closed(fixtures, target[2], target[3], now)
        if pending:
            response["windows"].append(
                window | {"state": "awaiting_window", "reason": pending}
            )
            continue
        fingerprint = store.digest(
            {
                "candidates": [c["digest"] for c in peers],
                "official_results": sorted(
                    [
                        f["fixture_id"],
                        f["kickoff"],
                        f["home_goals"],
                        f["away_goals"],
                        f.get("result_version"),
                        f["received_at"],
                    ]
                    for f in fixtures
                ),
            }
        )
        ident, claim = store.digest([season, *target]), uuid.uuid4().hex
        with closing(_open(data_dir)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT state,updated_at FROM research_window_reviews WHERE target=? AND results_digest=?",
                (ident, fingerprint),
            ).fetchone()
            if previous and (
                previous["state"] == "checked"
                or (
                    previous["state"] == "running"
                    and store.instant(previous["updated_at"]) + timedelta(minutes=5)
                    > now
                )
            ):
                response["windows"].append(
                    window
                    | {
                        "state": "already_checked"
                        if previous["state"] == "checked"
                        else "in_progress"
                    }
                )
                continue
            conn.execute(
                "INSERT INTO research_window_reviews VALUES(?,?,?,'running',?,NULL) ON CONFLICT(target,results_digest) "
                "DO UPDATE SET claim=excluded.claim,state='running',updated_at=excluded.updated_at",
                (ident, fingerprint, claim, store.iso(now)),
            )
        try:
            evaluate_candidate(data_dir, keys[0])
            outcomes = []
            for key in keys:
                c = get_candidate(data_dir, key)
                outcomes.append(
                    {
                        "key": key,
                        "state": c["state"],
                        "reason": (c["assessment"] or {}).get("reason"),
                    }
                )
            result = window | {"state": "checked", "outcomes": outcomes}
        except Exception as exc:
            result = window | {
                "state": "error",
                "reason": f"{type(exc).__name__}: {exc}"[:800],
            }
        with closing(_open(data_dir)) as conn, conn:
            conn.execute(
                "UPDATE research_window_reviews SET state=?,outcome=?,updated_at=? WHERE target=? AND results_digest=? AND claim=?",
                (
                    result["state"],
                    store.canonical(result),
                    store.now_utc(),
                    ident,
                    fingerprint,
                    claim,
                ),
            )
        response["windows"].append(result)
    return response


def register_cli(subparsers):
    actions = subparsers.add_parser(
        "experiment", help="Registered paper model and field-source trials"
    ).add_subparsers(dest="experiment_action", required=True)
    add = actions.add_parser("register")
    add.add_argument("key")
    add.add_argument("--kind", choices=("model", "source"), required=True)
    for field in ("version", "baseline", "window", "hypothesis"):
        add.add_argument(f"--{field}", required=True)
    add.add_argument("--scope")
    add.add_argument("--manifest", type=Path)

    def create(a):
        manifest = (
            json.loads(a.manifest.read_text(encoding="utf-8")) if a.manifest else {}
        )
        return register_candidate(
            a.data_dir,
            a.key,
            kind=a.kind,
            scope=a.scope,
            version=a.version,
            baseline=a.baseline,
            window=a.window,
            hypothesis=a.hypothesis,
            manifest=manifest,
        )

    add.set_defaults(func=create, handler=create)
    from . import football_candidates

    football_candidates.register(actions)
    approve = actions.add_parser(
        "approve", help="recheck evidence and approve a recommended paper experiment"
    )
    approve.add_argument("key")
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--reason", required=True)
    approve.set_defaults(
        func=lambda a: approve_candidate(
            a.data_dir, a.key, reviewer=a.reviewer, reason=a.reason
        )
    )
    show = actions.add_parser("status")
    show.set_defaults(
        func=lambda a: experiment_status(a.data_dir),
        handler=lambda a: experiment_status(a.data_dir),
    )
    evaluate = actions.add_parser("evaluate")
    evaluate.add_argument("key")
    evaluate.set_defaults(
        func=lambda a: evaluate_candidate(a.data_dir, a.key),
        handler=lambda a: evaluate_candidate(a.data_dir, a.key),
    )
    due = actions.add_parser(
        "review-due",
        help="Review newly closed registered windows, including postponements",
    )
    due.add_argument("season")
    due.add_argument("round", type=int)
    due.set_defaults(
        func=lambda a: review_due_experiments(a.data_dir, a.season, a.round),
        handler=lambda a: review_due_experiments(a.data_dir, a.season, a.round),
    )
    undo = actions.add_parser("rollback")
    undo.add_argument("key")
    undo.add_argument("--reason", required=True)
    undo.set_defaults(
        func=lambda a: rollback_candidate(a.data_dir, a.key, reason=a.reason),
        handler=lambda a: rollback_candidate(a.data_dir, a.key, reason=a.reason),
    )
