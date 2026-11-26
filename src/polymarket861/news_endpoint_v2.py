"""Registered, prospective news slopes for three distinct closing references."""

from collections import Counter, defaultdict
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import re
import sqlite3
from statistics import fmean

from . import (
    news_accumulation,
    news_endpoint_originals,
    news_endpoint_study as v1,
    news_evaluation,
    news_model,
    news_study_plan,
    store,
)


MODES = ("technical", "formal")
CLOCKS = (*v1.CLOCKS, "dual_schedule_close")
FAMILY = len(v1.HORIZONS) * (len(CLOCKS) * len(v1.ENDPOINTS) + 1)
MODE = "technical+formal"
BINDINGS = """CREATE TABLE IF NOT EXISTS news_endpoint_v2_bindings(
 capture_order INTEGER PRIMARY KEY, seal TEXT NOT NULL UNIQUE REFERENCES forecast_seals(digest),
 declaration TEXT NOT NULL REFERENCES news_endpoint_declarations(digest), bound_at TEXT NOT NULL,
 probability_era TEXT NOT NULL)"""
TRIGGER_NAME = "news_endpoint_v2_bind_on_insert"
TRIGGER = v1.capture_trigger(
    TRIGGER_NAME,
    "news_endpoint_v2_bindings",
    v1.V2_VERSION,
    "d.mode='technical+formal' AND NEW.mode IN ('technical','formal')",
)


def proposal():
    from . import news_endpoint_power

    plan = {
        "version": v1.V2_VERSION,
        "eligible_seasons": list(v1.SEASONS),
        "ends_at_exclusive": v1.END,
        "modes": list(MODES),
        "horizons": list(v1.HORIZONS),
        "endpoints": list(v1.ENDPOINTS),
        "prices": "Use the normalized Yes midpoints of all three verified 1X2 contracts for home win. Use the Over midpoint of the verified full-match 2.5 contract with both Over and Under books. At each clock require matching event, tokens, market rules, depth, spread and native decision/closing receipts.",
        "closing_clocks": list(CLOCKS),
        "native_pair_declaration_sha256": news_study_plan.PLAN_SHA256,
        "population": "Prospectively bound native EPL main seals with contemporaneous registered news originals for both clubs. At each fixture, official kickoff and horizon choose the first bound seal across modes in local insertion order, before checking its facts, outcomes or books. No replacement or retrospective binding.",
        "comparability": "Enroll the installed news policy ID and SHA for paired B1 versus with-news probabilities on the local receipt clock. Admit every individually reverified implementation era satisfying that contract. Report incompatible eras and their counts separately. A first seal never determines a cohort. This estimates the registered process across implementations, without certifying identical model probabilities.",
        "estimand": "Common within-stratum slope of closing probability minus B1 on with-news probability minus B1. Subtract both sample means separately within each (technical/formal mode, probability era). Weight by within-stratum squared movement. The paired outcome guardrail is the fixture-weighted mean four-component score difference.",
        "references": {
            "market_close": "Independently proved first whistle and a fresh same-contract pre-whistle receipt.",
            "scheduled_market_close": "Frozen unchanged kickoff and explicitly pregame exchange state at the receipt.",
            "dual_schedule_close": "OpenLigaDB and native Gamma must independently agree on the fixture and unchanged kickoff at both books. At decision use schedule receipts seen within 24 hours of the book. At close use schedule receipts seen in the 24 hours before kickoff and a native book 120 to 10 seconds before kickoff. Screen contrary evidence of actual start through the fixed analysis cutoff. This is a separate research reference.",
        },
        "interval": "Two-sided simultaneous Bonferroni family. Cluster by season and official round across strata; slope uses CR1 and Student G-1. Singleton or invariant strata supply no slope leverage. Zero movement and missing books stay visible in coverage. The score guardrail uses paired scores from verified regulation finals.",
        "family_comparisons": FAMILY,
        "family_confidence": 0.95,
        "score_guardrail": "Per horizon use the match-weighted four-component score difference (with news minus B1) with the same simultaneous round-cluster interval. Its upper bound below zero flags deterioration.",
        "sensitivity_at_end": "For each estimable slope report the minimum slope magnitude for 80% two-sided rejection at the observed CR1 standard error and G-1 degrees of freedom using the noncentral Student distribution. This conditional approximation requires positive estimable cluster variance. It is separate from the registered design simulations.",
        "zero_move_tolerance": 1e-12,
        "reporting": "Before the fixed end publish only coverage, missingness and the advance design. Reveal all fourteen registered estimates together using evidence received strictly before the end. No automated promotion, trading or statement of profitability.",
        "planning_power": news_endpoint_power.planning(),
    }
    return {"plan": plan, "plan_sha256": store.digest(plan)}


def _policy():
    return {"protocol": news_model.PROTOCOL, "sha256": news_model.POLICY_SHA256}


def _registration(db):
    row = db.execute(
        "SELECT * FROM news_endpoint_declarations WHERE version=?", (v1.V2_VERSION,)
    ).fetchone()
    if not row:
        return None
    manifest = json.loads(row["manifest"])
    # The scientific design is immutable. Its artifact is stored in the
    # registration, so an operator can still inspect the original assumptions.
    proposed = proposal()
    registered_policy = manifest.get("news_policy") or {}
    if (
        store.digest(manifest) != row["digest"]
        or manifest.get("version") != v1.V2_VERSION
        or manifest.get("plan_sha256") != proposed["plan_sha256"]
        or store.digest(manifest.get("plan")) != proposed["plan_sha256"]
        or manifest.get("ends_at") != v1.END
        or any(
            manifest.get(key) != row[key] for key in ("mode", "starts_at", "ends_at")
        )
        or manifest["mode"] != MODE
        or not manifest["registered_at"] <= manifest["starts_at"] < v1.END
        or not isinstance(registered_policy.get("protocol"), str)
        or not registered_policy["protocol"]
        or not isinstance(registered_policy.get("sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", registered_policy["sha256"])
    ):
        raise ValueError("Invalid stored v2 endpoint study declaration")
    trigger = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (TRIGGER_NAME,)
    ).fetchone()
    if not trigger or trigger[0] != TRIGGER:
        raise ValueError("v2 endpoint study capture trigger missing or changed")
    return manifest | {"registration_sha256": row["digest"]}


def enroll(data_dir, *, starts_at=None):
    """Create the local opt-in and capture trigger in one transaction."""
    start = store.iso(starts_at) if starts_at is not None else None
    with closing(store.connect(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(v1._DECLARATIONS)
        existing = _registration(db)
        if existing:
            if start is None or existing["starts_at"] == start:
                return existing
            raise ValueError("v2 already has an immutable local start")
        registered = store.now_utc()
        start = start or registered
        if not registered <= start < v1.END:
            raise ValueError(
                "Start must be at or after registration and before 2028-07-01 UTC"
            )
        manifest = proposal() | {
            "version": v1.V2_VERSION,
            "registered_at": registered,
            "starts_at": start,
            "ends_at": v1.END,
            "mode": MODE,
            "news_policy": _policy(),
        }
        digest = store.digest(manifest)
        db.execute(BINDINGS)
        news_endpoint_originals.ensure_source_table(
            db, news_endpoint_originals.V2_ORIGINALS
        )
        db.execute(TRIGGER)
        db.execute(
            "INSERT INTO news_endpoint_declarations VALUES(?,?,?,?,?,?)",
            (v1.V2_VERSION, digest, store.canonical(manifest), start, v1.END, MODE),
        )
        return manifest | {"registration_sha256": digest}


def _slope_cell(rows, field, endpoint, reveal):
    cell = v1._cell(rows, field, endpoint, reveal, family_size=FAMILY, stratified=True)
    native_key = v1.ENDPOINTS[endpoint][0]
    groups = defaultdict(list)
    for row in rows:
        groups[row["mode"], row["implementation"]].append(row)
    coverage = []
    for (mode, era), cohort in sorted(groups.items()):
        moves = []
        for row in cohort:
            quote = (row.get(field) or {}).get("endpoints", {}).get(native_key)
            if quote:
                moves.append(
                    (
                        (row["season"], row["round_id"]),
                        quote["with_news"] - quote["no_news"],
                    )
                )
        detail = {
            "mode": mode,
            "era": era,
            "eligible_news_pairs": len(cohort),
            "matched_books": len(moves),
        }
        if reveal:
            center = fmean(x for _, x in moves) if moves else 0.0
            rounds = [rnd for rnd, x in moves if abs(x - center) > 1e-12]
            detail.update(
                informative_pairs=len(rounds),
                informative_rounds=len(set(rounds)),
                slope_information=(
                    "within_stratum_movement"
                    if rounds
                    else "no_within_stratum_variation"
                    if moves
                    else "no_matched_books"
                ),
            )
        coverage.append(detail)
    if reveal:
        interval = cell["interval"]
        detectable = None
        if interval:
            detectable = interval[
                "standard_error"
            ] * news_accumulation._detectable_shift(
                interval["critical_t"], interval["degrees_of_freedom"]
            )
        cell["minimum_detectable_slope_at_80pct"] = detectable
    return cell | {"strata": coverage}


def _summary(
    registration,
    rows,
    due,
    exclusions,
    strata,
    *,
    reveal=False,
    bound=0,
    duplicates=None,
):
    estimates = {}
    duplicates = duplicates or {}
    for horizon in v1.HORIZONS:
        cohort = rows.get(horizon, [])
        by_stratum = [
            {
                "mode": mode,
                "era": era,
                "selected": counts["selected"],
                "qualified": counts["qualified"],
                "excluded": counts["selected"] - counts["qualified"],
                "exclusions": {
                    k: n
                    for k, n in counts.items()
                    if k not in ("selected", "qualified")
                },
            }
            for (mode, era), counts in sorted(strata.get(horizon, {}).items())
        ]
        estimates[horizon] = {
            "past_scheduled_windows": due.get(horizon, 0),
            "qualified_news_pairs": len(cohort),
            "duplicate_bindings_ignored": duplicates.get(horizon, 0),
            "exclusions": dict(exclusions.get(horizon, {})),
            "strata": by_stratum,
            "clocks": {
                field: {
                    ep: _slope_cell(cohort, field, ep, reveal) for ep in v1.ENDPOINTS
                }
                for field in CLOCKS
            },
            "score_guardrail": v1._guardrail(cohort, reveal, family_size=FAMILY),
        }
    return {
        "version": v1.V2_VERSION,
        "plan_sha256": registration["plan_sha256"]
        if registration
        else proposal()["plan_sha256"],
        "registration": registration,
        "state": "not_registered"
        if registration is None
        else "window_ended"
        if reveal
        else "prospective_collection",
        "bound_seals": bound,
        "family_comparisons": FAMILY,
        "horizons": estimates,
        "automatic_promotion": False,
    }


def _mismatch(row, binding, registration):
    if row.get("sealed") is not True or row.get("seal") != binding["seal"]:
        return row.get("reason") or "native_seal_failed_reverification"
    if row.get("study_status") != "declared":
        return row.get("study_status") or "native_seal_failed_reverification"
    if row.get("study_plan_sha256") != news_study_plan.PLAN_SHA256:
        return "incompatible_native_pair_declaration"
    if binding["probability_era"] != row["implementation"]:
        return "invalid_bound_probability_era"
    if {
        "protocol": row["news_protocol"],
        "sha256": row["policy_sha256"],
    } != registration["news_policy"]:
        return "incompatible_registered_news_policy"
    if not row["timely_news_coverage"]:
        return "missing_timely_qualified_two_team_news"
    if (
        not registration["starts_at"]
        <= row["input_cutoff"]
        <= binding["bound_at"]
        <= binding["cutoff"]
        < row["kickoff"]
    ):
        return "invalid_binding_time"
    return None


def report(data_dir):
    path = Path(data_dir).expanduser() / "runtime.sqlite3"
    empty = lambda: _summary(None, {}, {}, {}, {})
    if not path.is_file():
        return empty()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='news_endpoint_declarations'"
        ).fetchone():
            return empty()
        registration = _registration(db)
        if registration is None:
            return empty()
        now = store.now_utc()
        as_of = min(now, store.iso(store.instant(v1.END) - timedelta(microseconds=1)))
        v1._scope_fixture_history(db, as_of)
        news_endpoint_originals.scope_originals(
            db,
            news_endpoint_originals.V2_ORIGINALS,
            registration["registration_sha256"],
        )
        return _cohort(db, registration, as_of, reveal=now >= v1.END)


def _cohort(db, registration, as_of, *, reveal):
    bindings = db.execute(
        """SELECT b.seal,b.bound_at,b.probability_era,s.fixture_id,s.horizon,s.kickoff,s.cutoff,s.mode
        FROM news_endpoint_v2_bindings b JOIN forecast_seals s ON s.digest=b.seal
        WHERE b.declaration=? AND b.bound_at<=? ORDER BY b.capture_order""".strip(),
        (registration["registration_sha256"], as_of),
    ).fetchall()
    first = {}
    duplicates = Counter()
    for row in bindings:
        key = (row["fixture_id"], row["horizon"], row["kickoff"])
        if key in first:
            duplicates[row["horizon"]] += 1
        else:
            first[key] = row
    due, accepted = Counter(), defaultdict(list)
    reasons, strata = defaultdict(Counter), defaultdict(lambda: defaultdict(Counter))
    for season in v1.SEASONS:
        fixtures = [
            f
            for f in store.eligible_fixture_rows(db, season=season)
            if f["fixture_id"].startswith(f"epl/{season}/")
        ]
        for horizon, lead in v1.HORIZONS.items():
            scheduled = [
                f
                for f in fixtures
                if registration["starts_at"]
                <= store.iso(store.instant(f["kickoff"]) - lead)
                <= as_of
                and f["kickoff"] < v1.END
            ]
            due[horizon] += len(scheduled)
            by_mode, selected = defaultdict(list), {}
            for f in scheduled:
                chosen = first.get((f["fixture_id"], horizon, f["kickoff"]))
                if chosen is None:
                    reasons[horizon]["no_prospective_binding"] += 1
                else:
                    selected[f["fixture_id"]] = chosen
                    by_mode[chosen["mode"]].append(f)
            ids = {b["seal"] for b in selected.values()}
            for mode, candidates in by_mode.items():
                for row in news_evaluation._records(
                    db,
                    candidates,
                    horizon,
                    mode,
                    as_of=as_of,
                    reopen_news=True,
                    eligible_seals=ids,
                    include_schedule_reference=True,
                ):
                    binding = selected[row["fixture_id"]]
                    counts = strata[horizon][mode, binding["probability_era"]]
                    counts["selected"] += 1
                    reason = _mismatch(row, binding, registration)
                    if reason:
                        counts[reason] += 1
                        reasons[horizon][reason] += 1
                        continue
                    counts["qualified"] += 1
                    admitted = v1._receipts_before_end(db, row) if reveal else row
                    accepted[horizon].append(admitted | {"mode": mode})
    return _summary(
        registration,
        accepted,
        due,
        reasons,
        strata,
        reveal=reveal,
        bound=len(bindings),
        duplicates=duplicates,
    )
