"""Paired probabilities against verified, matched Polymarket decision and closing books."""

from collections import Counter, defaultdict
from datetime import timedelta
from itertools import groupby
from pathlib import Path
from sqlite3 import DatabaseError
from statistics import fmean
import json
import math

from scipy.stats import t as student

from . import (
    archive_proof,
    big5_market_schedule,
    comparisons,
    football,
    kickoff_proof,
    market_provider,
    news_study_plan,
    store,
)

_TOL = news_study_plan.PLAN["direction_absolute_tolerance"]


def source_capability(db):
    """Report acquisition routes under *this* release and current local grant.

    Only resolve() can authenticate a particular existing receipt; it checks the
    grant at the time of capture even if future captures are no longer possible.
    """
    releases = []
    for key in sorted(kickoff_proof.OPEN_RELEASES):
        try:
            kickoff_proof._open_release(key)
        except ValueError:
            continue
        releases.append(key)
    espn = "no_reviewed_free_grant"
    detail = None
    if kickoff_proof.FREE_ESPN_PERMISSIONS:
        root = Path(db.execute("PRAGMA database_list").fetchone()[2]).parent
        try:
            kickoff_proof._permission(root, db, store.now_utc())
        except (ValueError, OSError) as exc:
            espn, detail = "local_permission_unavailable", str(exc)
        else:
            espn = "capture_authorized"
    commands = []
    if releases:
        commands.append(
            "ledger-import-open-kickoff --fixture ID --schedule-version N --event ID --release KEY --timeline FILE --licence FILE"
        )
    if espn == "capture_authorized":
        commands.append(
            "ledger-capture-kickoff --fixture ID --schedule-version N --event ESPN_ID"
        )
    if commands:
        state = "reviewed_route_available"
        action = "For a completed matching fixture, admit the publisher's original first-half event against the schedule version in force at T-60."
    elif espn == "local_permission_unavailable":
        state = "local_permission_required"
        action = "Archive the original release-reviewed ESPN grant and register its active free schedule/auto/trial source permission before capturing a completed event."
    else:
        state = "no_reviewed_free_source"
        action = "A reviewed source release must pin a freely licensed EPL F24 export and its licence, or authenticate a free written ESPN grant for retrieving and retaining first-half event originals."
    return {
        "status": state,
        "reviewed_open_f24_release_keys": releases,
        "reviewed_espn_grants": len(kickoff_proof.FREE_ESPN_PERMISSIONS),
        "current_espn_capture": espn,
        "local_permission_reason": detail,
        "admission_commands": commands,
        "action": action,
    }


def _reference(family, quotes):
    # A 1X2 reference requires all three compatible Yes books in one native
    # batch. Binary totals use the Over midpoint after both sides were verified.
    mids = {key: values[0]["mid"] for key, values in quotes.items()}
    if family == "result":
        total = sum(mids.values())
        return {k: v / total for k, v in mids.items()}
    return mids


def _direction(step, gap):
    if abs(step) <= _TOL:
        return "unchanged"
    if abs(gap) <= _TOL:
        return "at_reference"
    return "toward" if step * gap > 0 else "away"


def _measure(base, news, decision, close):
    if not all(0 <= p <= 1 for p in (base, news, decision, close)):
        raise ValueError("invalid_paired_probability")
    return {
        "no_news": base,
        "with_news": news,
        "decision": decision,
        "close": close,
        "direction": _direction(news - base, close - base),
        "market_drift_agreement": _direction(news - base, close - decision),
        "distance_gain_pp": 100 * (abs(base - close) - abs(news - close)),
    }


def _contracts(group):
    return {
        key: [
            {k: q[k] for k in ("side", "condition", "token", "rule_sha256", "protocol")}
            for q in quotes
        ]
        for key, quotes in group.items()
    }


def _scheduled_revision(db, fixture, cutoff, close):
    versions = []
    for stamp in (cutoff, close - timedelta(microseconds=1)):
        when = store.iso(stamp)
        row = db.execute(
            "SELECT f.*,s.precision FROM fixture_versions f JOIN schedule_receipts s USING(fixture_id,version) "
            "WHERE f.fixture_id=? AND f.known_at<=? AND s.recorded_at<=? ORDER BY f.known_at DESC LIMIT 1",
            (fixture["fixture_id"], when, when),
        ).fetchone()
        if (
            not row
            or row["precision"] not in ("minute", "second")
            or any(row[k] != fixture[k] for k in ("home", "away", "kickoff"))
        ):
            raise ValueError("unverified_scheduled_fixture_identity")
        versions.append(row["version"])
    if versions[0] != versions[1]:
        raise ValueError("scheduled_fixture_revision_changed")
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='epl_schedule_holds'"
    ).fetchone():
        held = db.execute(
            "SELECT reason FROM epl_schedule_holds WHERE fixture=? AND version=? AND at<? ORDER BY at DESC LIMIT 1",
            (fixture["fixture_id"], versions[0], store.iso(close)),
        ).fetchone()
        if held and held[0]:
            raise ValueError("scheduled_fixture_under_review")
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='epl_schedule_reviews'"
    ).fetchone():
        review = db.execute(
            "SELECT action FROM epl_schedule_reviews WHERE fixture=? AND version=? AND at<? ORDER BY at DESC LIMIT 1",
            (fixture["fixture_id"], versions[0], store.iso(close)),
        ).fetchone()
        if review and review[0] == "withdrawn":
            raise ValueError("scheduled_fixture_withdrawn")
    return versions[0]


def _history_states(body, operation, selectors, slugs, received_at):
    events = json.loads(body)
    if operation == "event":
        if not isinstance(events, dict) or {events.get("slug")} != selectors:
            raise ValueError("unexpected Gamma event identity")
        events = [events]
    if not isinstance(events, list):
        raise ValueError("unexpected Gamma catalogue")
    states = {}
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("slug"), str):
            raise ValueError("unknown Gamma event identity")
        key = event["slug"]
        if key not in slugs:
            continue
        state = big5_market_schedule.pregame_flags(event, received_at)
        if key in states or state == "unclassified":
            raise ValueError("ambiguous Gamma event state")
        reason = None
        if state == "not_pregame":
            reason = (
                "exchange_start_or_end_observed"
                if big5_market_schedule.has_started(event, received_at)
                else "exchange_schedule_interrupted"
            )
        # Historical indexes stored only these two booleans. Recheck them
        # separately from sporting statuses recovered from the original bytes.
        states[key] = (event.get("live"), event.get("ended"), reason)
    return states


def _first_disqualifying_state(db, root, slug, close):
    # The collector commits the original and request before writing its two
    # indexes. Enumerate all three independently; read whole catalogue pages
    # because a missing member cannot be discovered from a derived index.
    aux = slug + "-more-markets"
    slugs = {slug, aux}
    rows = db.execute(
        """
        SELECT received_at,sha,'event_index' AS kind,slug AS selector,NULL AS live,NULL AS ended
          FROM ma_events WHERE slug IN (:slug,:aux) AND received_at<:close
        UNION ALL
        SELECT received_at,sha,'state_index',slug,live,ended
          FROM ma_event_states WHERE slug IN (:slug,:aux) AND received_at<:close
        UNION ALL
        SELECT received_at,sha,operation,selector,NULL,NULL FROM ma_requests
          WHERE status='ok' AND received_at<:close
            AND (operation='catalog' OR (operation='event' AND selector IN (:slug,:aux)))
        UNION ALL
        SELECT o.observed_at,o.sha256,'unlogged',json_extract(o.metadata,'$.operation'),NULL,NULL
          FROM observations o WHERE o.source='polymarket.com' AND o.observed_at<:close
            AND json_valid(o.metadata) AND json_extract(o.metadata,'$.operation') IN ('catalog','event')
            AND (json_extract(o.metadata,'$.url') IN (:main_url,:aux_url,:catalog_url)
                 OR json_extract(o.metadata,'$.url') LIKE :catalog_query)
            AND NOT EXISTS (SELECT 1 FROM ma_requests r WHERE r.sha=o.sha256
                 AND r.received_at=o.observed_at AND r.status='ok'
                 AND r.requested_at=json_extract(o.metadata,'$.requested_at')
                 AND r.operation=json_extract(o.metadata,'$.operation')
                 AND (r.operation='catalog' OR json_extract(o.metadata,'$.url')=:direct_url || r.selector))
        ORDER BY received_at,sha
        """,
        {
            "slug": slug,
            "aux": aux,
            "close": store.iso(close),
            "main_url": market_provider.GAMMA + "/events/slug/" + slug,
            "aux_url": market_provider.GAMMA + "/events/slug/" + aux,
            "direct_url": market_provider.GAMMA + "/events/slug/",
            "catalog_url": market_provider.GAMMA + "/events",
            "catalog_query": market_provider.GAMMA + "/events?%",
        },
    )
    for (stamp, sha), group in groupby(
        rows, key=lambda r: (r["received_at"], r["sha"])
    ):
        receipts = list(group)
        indexes = {r["selector"] for r in receipts if r["kind"] == "event_index"}
        states = {
            r["selector"]: (r["live"], r["ended"])
            for r in receipts
            if r["kind"] == "state_index"
        }
        requests = {r["kind"] for r in receipts if r["kind"] in ("event", "catalog")}
        unlogged = {r["selector"] for r in receipts if r["kind"] == "unlogged"}
        unknown = stamp, "unverified_exchange_state_history"
        try:
            if unlogged == {"catalog"} and not (requests or indexes or states):
                # An orphan catalogue containing exclusively different slugs
                # cannot contribute evidence about this fixture.
                if not _history_states(
                    archive_proof._body(root, sha), "catalog", set(), slugs, stamp
                ):
                    continue
            if unlogged or len(requests) != 1:
                return unknown
            reply, operation, selector, _ = archive_proof.original_receipt(
                db, root, sha, stamp, requests
            )
            direct = {r["selector"] for r in receipts if r["kind"] == "event"}
            if operation == "event" and direct != {selector}:
                return unknown
            native = _history_states(reply.body, operation, direct, slugs, stamp)
        except (ValueError, OSError, KeyError, TypeError):
            return unknown
        for _, _, reason in native.values():
            if reason:
                return stamp, reason
        if indexes != native.keys() or states != {k: v[:2] for k, v in native.items()}:
            return unknown
    return None


def _state_at_book(db, root, fixture, slug, received):
    event, row = comparisons._event(db, root, slug, received)
    if not comparisons._participants(event, fixture, slug):
        raise ValueError("exchange_fixture_revision_changed")
    age = (store.instant(received) - store.instant(row["received_at"])).total_seconds()
    reply, _, _, _ = archive_proof.original_receipt(
        db, root, row["sha"], row["received_at"], {"event", "catalog"}
    )
    if not market_provider.fresh_observation(reply) or (
        age + float(reply.headers.get("age", 0))
        > news_study_plan.PLAN["scheduled_state_max_age_seconds"]
    ):
        raise ValueError("stale_exchange_pregame_state")
    if (
        big5_market_schedule.pregame_flags(event, row["received_at"]) != "explicit_pregame"
        or event.get("closed") is True
        or event.get("archived") is True
    ):
        raise ValueError("no_explicit_exchange_pregame_state")
    return {"event_slug": slug, "sha256": row["sha"], "received_at": row["received_at"]}


def _closing_state_check(db, root, fixture, close, *, explicit):
    history = {}

    def check(slug, received):
        if slug not in history:
            history[slug] = _first_disqualifying_state(db, root, slug, close)
        problem = history[slug]
        if problem and problem[0] <= received:
            raise ValueError(problem[1])
        if explicit:
            return _state_at_book(db, root, fixture, slug, received)

    return check


def observe(db, root, fixture, entry, pair, *, scheduled_proxy=False, as_of=None):
    """Use the two predeclared clock tiers independently, with shared book eligibility."""
    if entry["mode"] == "replay":
        return {"reason": "requires_prospective_seal"}
    if not news_study_plan.timely_coverage(pair):
        return {"reason": "news_coverage_incomplete_or_stale"}
    scheduled = store.instant(fixture["kickoff"])
    cut = store.instant(pair["input_cutoff"])
    if scheduled_proxy:
        if store.instant(as_of or store.now_utc()) < scheduled:
            return {"reason": "awaiting_scheduled_kickoff"}
        try:
            version = _scheduled_revision(db, fixture, cut, scheduled)
        except (DatabaseError, ValueError) as exc:
            return {"reason": str(exc)}
        close = scheduled
        witness = {
            "scheduled_kickoff": store.iso(scheduled),
            "schedule_version": version,
        }
    else:
        try:
            proof = kickoff_proof.resolve(
                root,
                fixture["fixture_id"],
                scheduled,
                scheduled - timedelta(hours=1),
                db=db,
                prospective=True,
                received_through=as_of,
            )
        except kickoff_proof.MissingKickoffProof:
            return {"reason": "missing_verified_actual_kickoff"}
        except (KeyError, TypeError, OSError, ValueError, OverflowError):
            return {"reason": "invalid_actual_kickoff_evidence"}
        close = store.instant(proof["actual_kickoff"])
        witness = {
            "actual_kickoff": proof["actual_kickoff"],
            "kickoff_proof": proof["proof_id"],
        }
    if as_of is not None and close > store.instant(as_of):
        return {"reason": "closing_clock_after_analysis_cutoff"}
    if not cut <= store.instant(entry["sealed_at"]) < min(scheduled, close):
        return witness | {"reason": "seal_outside_pregame_window"}
    try:
        before = comparisons.frame(
            db, root, fixture, cut, cut, min(scheduled, close), prospective=True
        )
        after = comparisons._closing_frame(
            db,
            root,
            fixture,
            cut,
            close,
            not scheduled_proxy,
            True,
            validate_state=_closing_state_check(
                db, root, fixture, close, explicit=scheduled_proxy
            ),
        )
    except (DatabaseError, OSError, TypeError, KeyError, ValueError):
        return witness | {"reason": "unavailable_or_invalid_market_archive"}
    result = witness | {
        "input_cutoff": pair["input_cutoff"],
        "endpoints": {},
        "receipts": {},
        "missing": {},
    }
    for family, keys in comparisons.ITEMS.items():
        missing = {}
        if family not in before["items"]:
            missing["decision"] = before["missing"][family]
        if family not in after["items"]:
            missing["close"] = after["missing"][family]
        if missing:
            result["missing"][family] = missing
            continue
        opening = before["items"][family]
        closing = after["items"][family]
        contracts = _contracts(opening)
        close_receipt = after["family_receipts"][family]
        if contracts != _contracts(closing):
            result["missing"][family] = {"pair": "different_contract_tokens_or_rules"}
            continue
        # frame already checks native exchange and local receive clocks, depth,
        # spread and contemporaneous rules. Require two distinct observations.
        if not (
            before["received_at"]
            <= pair["input_cutoff"]
            <= entry["sealed_at"]
            < close_receipt["received_at"]
            < store.iso(close)
            and (scheduled_proxy or before["batch"] != close_receipt["batch"])
        ):
            result["missing"][family] = {"pair": "unmatched_decision_close_clocks"}
            continue
        opening_mids = _reference(family, opening)
        closing_mids = _reference(family, closing)
        result["receipts"][family] = {
            "contracts": contracts,
            "decision": {
                "batch": before["batch"],
                "received_at": before["received_at"],
            },
            "close": close_receipt,
        }
        for key in keys:
            result["endpoints"][key] = _measure(
                pair["no_news"]["probabilities"][key],
                pair["with_news"]["probabilities"][key],
                opening_mids[key],
                closing_mids[key],
            )
    return result


def regression(rows, field="market_close", *, fixed_effects=False):
    """Home-win price-gap slope, with one observation per supplied prospective fixture.

    Single-era estimates use the prespecified zero intercept. After a validated
    equivalence comparison, the optional pooled estimator de-means both axes in
    each era and retains a common slope. Influence is clustered by season/round.
    """
    points = []
    for row in rows:
        if (
            row.get("study_status") != "declared"
            or row.get("study_plan_sha256") != news_study_plan.PLAN_SHA256
        ):
            continue
        home = (row.get(field) or {}).get("endpoints", {}).get("home_win")
        if home:
            base = home["no_news"]
            points.append(
                (
                    (row["season"], row["round_id"]),
                    row["implementation"],
                    home["with_news"] - base,
                    home["close"] - base,
                )
            )
    return price_gap_slope(points, fixed_effects=fixed_effects,
                           family=news_study_plan.PLAN["family_comparisons"],
                           no_pairs_status="no_matched_home_win_pairs")


def price_gap_slope(points, *, fixed_effects=False, family, no_pairs_status):
    """Round-clustered price-gap inference over points admitted by the caller's declaration."""
    if type(family) is not int or family < 1:
        raise ValueError("the declared family must have at least one comparison")
    rounds = {r for r, _, _, _ in points}
    method = "within_era_fixed_effects" if fixed_effects else "through_origin"
    result = {
        "method": method,
        "predictor": "with_news_minus_B1",
        "response": "close_minus_B1",
        "pairs": len(points),
        "informative_pairs": 0,
        "rounds": len(rounds),
        "informative_rounds": 0,
        "slope": None,
        "interval": None,
    }
    if not points:
        return result | {"status": no_pairs_status}
    eras = {era for _, era, _, _ in points}
    if len(eras) > 1 and not fixed_effects:
        return result | {"status": "multiple_probability_eras"}
    if fixed_effects:
        axes = defaultdict(list)
        for _, era, x, y in points:
            axes[era].append((x, y))
        means = {
            era: (fmean(x for x, _ in vals), fmean(y for _, y in vals))
            for era, vals in axes.items()
        }
        points = [
            (rnd, era, x - means[era][0], y - means[era][1])
            for rnd, era, x, y in points
        ]
    informative = [p for p in points if abs(p[2]) > _TOL]
    result.update(
        informative_pairs=len(informative),
        informative_rounds=len({p[0] for p in informative}),
    )
    if not informative:
        return result | {"status": "no_informative_news_movements"}
    # A common one-direction shift cannot distinguish the news adjustment from
    # a constant level difference, even when the through-origin fit is finite.
    moves = [p[2] for p in informative]
    if len(moves) > 1 and max(moves) - min(moves) <= _TOL:
        return result | {"status": "no_variation_in_news_movements"}
    # Rows at zero (including after within-era centering) have no slope
    # leverage. Empty-information rounds must not tighten a critical value.
    xx = math.fsum(x * x for _, _, x, _ in informative)
    slope = math.fsum(x * y for _, _, x, y in informative) / xx
    result["slope"] = slope
    g, n = result["informative_rounds"], len(informative)
    p = 1 + len({era for _, era, _, _ in informative}) if fixed_effects else 1
    if g < 2 or n <= p:
        return result | {"status": "insufficient_independent_rounds"}
    scores = defaultdict(list)
    for rnd, _, x, y in informative:
        scores[rnd].append(x * (y - slope * x))
    meat = math.fsum(math.fsum(v) ** 2 for v in scores.values())
    se = math.sqrt(g / (g - 1) * (n - 1) / (n - p) * meat) / xx
    if se <= 1e-15 * max(1.0, abs(slope)):
        return result | {"status": "no_estimable_cluster_variance"}
    critical = float(student.ppf(1 - 0.025 / family, g - 1))
    return result | {
        "status": "estimated",
        "interval": {
            "lower": slope - critical * se,
            "upper": slope + critical * se,
            "standard_error": se,
            "degrees_of_freedom": g - 1,
            "family_comparisons": family,
            "critical_t": critical,
            "family_confidence": news_study_plan.PLAN["family_confidence"],
            "cluster": "season_and_official_round",
        },
    }


def coverage(rows, capability=None, mode=None, *, field="market_close"):
    """Per-fixture denominators; an archived receipt counts only after successful reverification."""
    observed = [r.get(field) or {} for r in rows]
    eligible = sum(
        bool(
            r.get("sealed")
            and r.get("timely_news_coverage")
            and obs.get("reason")
            not in {"requires_prospective_seal", "requires_scheduled_proxy_declaration"}
        )
        for r, obs in zip(rows, observed)
    )
    verified = sum(bool(obs.get("kickoff_proof")) for obs in observed)
    paired = sum(bool(obs.get("endpoints")) for obs in observed)
    missing = Counter(
        obs.get("reason") or r.get("reason") or "no_validated_pair"
        for r, obs in zip(rows, observed)
        if obs.get("reason") or not obs
    )
    if field == "scheduled_market_close":
        available = sum(bool(obs.get("scheduled_kickoff")) for obs in observed)
        if mode == "replay":
            state = "requires_prospective_seal"
        elif paired:
            state = "descriptive_scheduled_proxy"
        elif not eligible:
            state = "no_eligible_news_seals"
        elif not available and missing["awaiting_scheduled_kickoff"] == eligible:
            state = "awaiting_scheduled_kickoff"
        else:
            state = "no_matched_scheduled_quotes"
        return {
            "status": state,
            "clock_basis": "scheduled_kickoff_with_native_pregame_event",
            "scheduled_clock_eligible": True,
            "eligible_seals": eligible,
            "paired_fixtures": paired,
            "home_win_pairs": sum(
                "home_win" in obs.get("endpoints", {}) for obs in observed
            ),
            "exclusions": dict(sorted(missing.items())),
        }
    if mode == "replay":
        state = "requires_prospective_seal"
    elif paired:
        state = "descriptive_only"
    elif verified and missing["seal_outside_pregame_window"] == verified:
        state = "seal_outside_pregame_window"
    elif verified and missing["unavailable_or_invalid_market_archive"] == verified:
        state = "unavailable_or_invalid_market_archive"
    elif verified:
        state = "no_matched_market_quotes"
    elif missing["invalid_actual_kickoff_evidence"]:
        state = "invalid_actual_kickoff_evidence"
    elif capability and capability["status"] != "reviewed_route_available":
        state = "actual_kickoff_source_unavailable"
    elif not eligible:
        state = "no_eligible_news_seals"
    else:
        state = "awaiting_verified_actual_kickoff"
    actions = {
        "actual_kickoff_source_unavailable": (capability or {}).get("action"),
        "awaiting_verified_actual_kickoff": "Admit a matching first-half event original for the completed fixture through an available reviewed route.",
        "invalid_actual_kickoff_evidence": "Check the archived issuer evidence, its permission at collection, fixture and schedule revision, and any conflicting event originals.",
        "no_eligible_news_seals": "Collect a prospective primary news pair with timely local observations for both teams.",
        "seal_outside_pregame_window": "Check the schedule revision and sealing time against the verified first-half event.",
        "unavailable_or_invalid_market_archive": "Check the original native market receipts and their indexes.",
        "no_matched_market_quotes": "Inspect the decision and actual-close book gaps by family. A qualifying pair requires both original batches for the same contracts and rules.",
        "requires_prospective_seal": "Use a technical or formal primary pair sealed before its deadline.",
    }
    return {
        "status": state,
        "clock_basis": "independently_verified_first_half_wallclock",
        **({"action": actions[state]} if state in actions else {}),
        "scheduled_clock_eligible": False,
        "eligible_seals": eligible,
        "verified_actual_kickoffs": verified,
        "paired_fixtures": paired,
        "home_win_pairs": sum(
            "home_win" in obs.get("endpoints", {}) for obs in observed
        ),
        "exclusions": dict(sorted(missing.items())),
        **({"source_capability": capability} if capability is not None else {}),
    }


def summary(rows, capability=None, mode=None, *, field="market_close"):
    family_gaps = Counter()
    compared = {k: [] for k in football.PROB_KEYS}
    for r in rows:
        obs = r.get(field) or {}
        if obs and not obs.get("reason"):
            for family, reasons in obs["missing"].items():
                for stage, error in reasons.items():
                    family_gaps[f"{family}/{stage}/{error}"] += 1
            for key, item in obs["endpoints"].items():
                compared[key].append(item)
    metrics = {}
    for key, values in compared.items():
        dirs = Counter(v["direction"] for v in values)
        drift = Counter(v["market_drift_agreement"] for v in values)
        informative = dirs["toward"] + dirs["away"]
        metrics[key] = {
            "pairs": len(values),
            **{d: dirs[d] for d in ("toward", "away", "unchanged", "at_reference")},
            "toward_fraction": dirs["toward"] / informative if informative else None,
            "market_drift_agreement": {
                "agree": drift["toward"],
                "oppose": drift["away"],
                "unchanged_news": drift["unchanged"],
                "flat_market": drift["at_reference"],
            },
            "mean_abs_news_move_pp": fmean(
                100 * abs(v["with_news"] - v["no_news"]) for v in values
            )
            if values
            else None,
            "mean_abs_base_close_gap_pp": fmean(
                100 * abs(v["no_news"] - v["close"]) for v in values
            )
            if values
            else None,
            "mean_distance_gain_pp": fmean(v["distance_gain_pp"] for v in values)
            if values
            else None,
        }
    report = {
        **coverage(rows, capability, mode, field=field),
        "home_win_regression": regression(rows, field),
        "unit": "matched fixture per endpoint",
        "primary_endpoint": "home_win",
        "endpoints": metrics,
        "missing_family_reasons": dict(sorted(family_gaps.items())),
    }
    eras = sorted(
        {r["implementation"] for r in rows if (r.get(field) or {}).get("endpoints")}
    )
    if len(eras) > 1:
        report.update(
            status="multiple_probability_eras",
            endpoints={},
            by_era={
                key: summary(
                    [r for r in rows if r.get("implementation") == key],
                    capability,
                    mode,
                    field=field,
                )
                for key in eras
            },
        )
    return report


def _schedule_pair_at_book(
    db, root, fixture, version, received, scheduled, *, decision=False
):
    from . import opportunity, schedule_consensus

    admitted = schedule_consensus._registration(db, fixture["fixture_id"], version)
    proof = schedule_consensus.attest(root, db, admitted, received)
    schedule_consensus._check_latest(root, db, admitted, proof, received)
    clubs = tuple(opportunity._club(fixture[k]) for k in ("home", "away"))
    ol = schedule_consensus._round(
        root, db, int(admitted["season"][:4]), admitted["round_id"], received
    ).get(clubs)
    slug = proof["gamma"]["slug"]
    _, row = comparisons._event(db, root, slug, received)
    gamma_clubs, gamma = schedule_consensus._gamma(root, db, row, received)
    if (
        not ol
        or gamma_clubs != clubs
        or ol.get("match_id") != proof["openligadb"]["match_id"]
    ):
        raise ValueError("schedule_sources_disagree_on_fixture")
    receipts = {}
    # A T−24h opening book may arrive a few seconds before the decision.
    # Its independent schedules must be fresh at that receipt. The near-start
    # closing reference retains its original kickoff-relative 24-hour rule.
    fresh_at = store.instant(received) if decision else scheduled
    for source, info in (("openligadb", ol), ("gamma", gamma)):
        if info["kickoff"] != fixture["kickoff"] or not fresh_at - timedelta(
            hours=24
        ) <= store.instant(info["received_at"]) <= store.instant(received):
            raise ValueError("schedule_sources_outside_24h_or_disagree")
        receipts[source] = {
            "sha256": info["sha256"],
            "received_at": info["received_at"],
        }
    return slug, receipts


def _retrospective_clock(db, root, fixture, scheduled, *, as_of=None):
    from . import result_admission

    now = store.iso(as_of) if as_of is not None else store.now_utc()
    if store.instant(now) <= scheduled:
        raise ValueError("awaiting_postmatch_clock_check")
    proof = None
    try:
        proof = kickoff_proof.resolve(
            root,
            fixture["fixture_id"],
            scheduled,
            scheduled - timedelta(hours=1),
            db=db,
            prospective=True,
            received_through=as_of,
        )
    except kickoff_proof.MissingKickoffProof:
        pass
    if proof and store.instant(proof["actual_kickoff"]) < scheduled:
        raise ValueError("early_actual_kickoff_observed")
    # A version number counts local inserts. At each receipt, only the latest
    # publication visible then can change the schedule. A genuine change still
    # disqualifies the weak clock if another publication later restores it.
    effective = fixture["known_at"]
    revisions = db.execute(
        "SELECT f.known_at,f.kickoff,f.home,f.away FROM fixture_versions f "
        "JOIN schedule_receipts s USING(fixture_id,version) WHERE f.fixture_id=? "
        "AND f.known_at>? AND f.known_at<=? AND s.recorded_at<=? "
        "ORDER BY max(f.known_at,s.recorded_at), f.known_at DESC",
        (fixture["fixture_id"], effective, now, now),
    )
    for revision in revisions:
        if revision["known_at"] <= effective:
            continue
        effective = revision["known_at"]
        if any(revision[key] != fixture[key] for key in ("kickoff", "home", "away")):
            raise ValueError("retrospective_fixture_revision")
    for table, field, blocked in (
        ("epl_schedule_holds", "reason", lambda reason: bool(reason)),
        ("epl_schedule_reviews", "action", lambda action: action == "withdrawn"),
    ):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            latest = db.execute(
                f"SELECT {field} FROM {table} WHERE fixture=? AND version=? AND at<=? ORDER BY at DESC LIMIT 1",
                (fixture["fixture_id"], fixture["version"], now),
            ).fetchone()
            if latest and blocked(latest[0]):
                raise ValueError("retrospective_schedule_under_review")
    # A passed wall clock alone does not establish that the match took place.
    finals = result_admission.verified_season_regulation(
        root, db, fixture["season"], [fixture["fixture_id"]], at=now
    )
    final = finals.get(fixture["fixture_id"])
    if final and store.instant(final["kickoff"]) < scheduled:
        raise ValueError("result_source_reports_early_kickoff")
    if (
        not final
        or final.get("status") != "final"
        or final.get("period") != "regulation"
        or final.get("registry_kickoff", fixture["kickoff"]) != fixture["kickoff"]
        or store.instant(final["confirmed_at"]) < scheduled
        or store.instant(final["confirmed_at"]) > store.instant(now)
    ):
        raise ValueError("awaiting_verified_postmatch_result")
    return {
        "checked_at": now,
        "result_confirmed_at": final["confirmed_at"],
        "result_proof": final["proof"],
        "actual_kickoff_proof": proof["proof_id"] if proof else None,
    }


def _schedule_only_frame(db, root, fixture, cutoff, scheduled, *, as_of=None):
    from . import schedule_consensus

    version = _scheduled_revision(db, fixture, cutoff, scheduled)
    # Bind the proof to the schedule actually held at both pregame clocks.
    # A later publication may reaffirm the same kickoff under another version.
    registered = schedule_consensus._registration(db, fixture["fixture_id"], version)
    retrospective = _retrospective_clock(db, root, registered, scheduled, as_of=as_of)
    slug, _ = comparisons._catalog(db, root, fixture, store.iso(cutoff), True)
    # A correction published after kickoff can still invalidate a weak clock.
    rows = db.execute(
        "SELECT slug,sha,received_at,kickoff FROM ma_events WHERE slug IN (?,?) "
        "AND received_at BETWEEN ? AND ? ORDER BY received_at",
        (slug, slug + "-more-markets", store.iso(cutoff), retrospective["checked_at"]),
    )
    for row in rows:
        original = archive_proof._event_capture(
            db, root, row["sha"], row["received_at"], row["slug"]
        )
        native_start = store.instant(original["startTime"])
        if native_start != store.instant(row["kickoff"]) or native_start != scheduled:
            raise ValueError("retrospective_exchange_schedule_revision")
    # Reject even a known early start after the chosen book, and corrupt state history.
    state = _first_disqualifying_state(db, root, slug, scheduled)
    if state:
        raise ValueError(state[1])
    stamps = db.execute(
        "SELECT DISTINCT received_at FROM ma_requests WHERE operation='books' AND selector=? "
        "AND status='ok' AND received_at BETWEEN ? AND ? ORDER BY received_at DESC",
        (
            slug,
            store.iso(scheduled - timedelta(seconds=120)),
            store.iso(scheduled - timedelta(seconds=10)),
        ),
    ).fetchall()
    missing = dict.fromkeys(comparisons.ITEMS, "no_book_in_120_to_10_second_window")
    used, prices = {}, {}
    for (stamp,) in stamps:
        try:
            source_slug, confirmations = _schedule_pair_at_book(
                db, root, fixture, version, stamp, scheduled
            )
            if source_slug != slug:
                raise ValueError("different_fixture_in_schedule_and_book")
            data = comparisons.frame(
                db,
                root,
                fixture,
                cutoff,
                store.instant(stamp),
                scheduled,
                prospective=True,
                as_of_book=True,
                schedule_reference=stamp,
            )
            if data["received_at"] != stamp:
                raise ValueError("book_receipt_time_differs_from_research_reference")
        except (DatabaseError, OSError, KeyError, TypeError, ValueError) as exc:
            for family in missing:
                missing[family] = str(exc)
            continue
        for family in tuple(missing):
            if family not in data["items"]:
                missing[family] = data["missing"].get(
                    family, "unverified_market_family"
                )
                continue
            group = data["items"][family]
            prices[family] = _reference(family, group)
            used[family] = {
                "received_at": stamp,
                "batch": data["batch"],
                "schedule_sources": confirmations,
                "contracts": _contracts(group),
            }
            del missing[family]
        if not missing:
            break
    return {
        "scheduled_kickoff": fixture["kickoff"],
        "schedule_version": version,
        "postmatch_check": retrospective,
        "family_receipts": used,
        "prices": prices,
        "missing": missing,
    }


def close_reference(root, fixture_id):
    """Measure available clock evidence by family; no ledger/trial/CLV path consumes this report."""
    from contextlib import closing

    def tier(name, read):
        try:
            data = read()
        except (DatabaseError, OSError, KeyError, TypeError, ValueError) as exc:
            data = {
                "family_receipts": {},
                "missing": dict.fromkeys(comparisons.ITEMS, str(exc)),
            }
        return {
            "basis": name,
            "qualified_families": len(data["family_receipts"]),
            **data,
        }

    with closing(store.connect(root)) as db:
        db.execute("BEGIN")
        now = store.now_utc()
        f = db.execute(
            "SELECT f.*,s.recorded_at,s.precision FROM fixture_versions f JOIN schedule_receipts s "
            "USING(fixture_id,version) WHERE f.fixture_id=? AND f.known_at<=? AND s.recorded_at<=? "
            "ORDER BY f.known_at DESC LIMIT 1",
            (fixture_id, now, now),
        ).fetchone()
        if f:
            scheduled = store.instant(f["kickoff"])
            cut = scheduled - timedelta(hours=1)

        def evaluate_tier(which):
            if not f:
                raise ValueError("missing_verified_fixture_registration")
            if store.instant(now) < scheduled:
                raise ValueError("awaiting_scheduled_kickoff")
            if which == "two_source_schedule":
                return _schedule_only_frame(db, root, f, cut, scheduled)
            if which == "actual_first_whistle":
                proof = kickoff_proof.resolve(
                    root, fixture_id, scheduled, cut, db=db, prospective=True
                )
                end = store.instant(proof["actual_kickoff"])
                frame = comparisons._closing_frame(
                    db, root, f, cut, end, True, True,
                    validate_state=_closing_state_check(db, root, f, end, explicit=False),
                )
                witness = {
                    "kickoff_proof": proof["proof_id"],
                    "actual_kickoff": proof["actual_kickoff"],
                }
            else:
                version = _scheduled_revision(db, f, cut, scheduled)

                frame = comparisons._closing_frame(
                    db,
                    root,
                    f,
                    cut,
                    scheduled,
                    False,
                    True,
                    validate_state=_closing_state_check(db, root, f, scheduled, explicit=True),
                )
                witness = {
                    "schedule_version": version,
                    "scheduled_kickoff": f["kickoff"],
                }
            return witness | {
                "family_receipts": frame["family_receipts"],
                "missing": frame["missing"],
                "prices": {
                    fam: _reference(fam, rows) for fam, rows in frame["items"].items()
                },
            }

        return {
            "fixture_id": fixture_id,
            "as_of": now,
            "metric": "closing_price_evidence_by_family",
            "research_only": True,
            "eligible_for_formal_clv": False,
            "eligible_for_promotion": False,
            "tiers": {
                key: tier(key, lambda: evaluate_tier(key))
                for key in (
                    "actual_first_whistle",
                    "explicit_exchange_pregame",
                    "two_source_schedule",
                )
            },
        }


def register(subparsers):
    cmd = subparsers.add_parser(
        "close-reference", help="按玩法分列首哨、交易所赛前状态和双源赛程研究参照"
    )
    cmd.add_argument("--fixture", required=True, help="本地登记的比赛 ID")
    cmd.set_defaults(func=lambda args: close_reference(args.data_dir, args.fixture))
