"""Paired probabilities against verified, matched Polymarket decision and closing books."""

from collections import Counter
from datetime import timedelta
from pathlib import Path
from sqlite3 import DatabaseError
from statistics import fmean

from . import comparisons, football, kickoff_proof, news_study_plan, store

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


def observe(db, root, fixture, entry, pair):
    """No final score or execution is used as a proxy for a price or kickoff."""
    if entry["mode"] == "replay":
        return {"reason": "requires_prospective_seal"}
    if not news_study_plan.timely_coverage(pair):
        return {"reason": "news_coverage_incomplete_or_stale"}
    scheduled = store.instant(fixture["kickoff"])
    cut = store.instant(pair["input_cutoff"])
    try:
        proof = kickoff_proof.resolve(
            root,
            fixture["fixture_id"],
            scheduled,
            scheduled - timedelta(hours=1),
            db=db,
            prospective=True,
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
    if not cut <= store.instant(entry["sealed_at"]) < min(scheduled, close):
        return witness | {"reason": "seal_outside_pregame_window"}
    try:
        before = comparisons.frame(
            db, root, fixture, cut, cut, min(scheduled, close), prospective=True
        )
        after = comparisons._closing_frame(db, root, fixture, cut, close, True, True)
    except (DatabaseError, OSError, ValueError):
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
            < proof["actual_kickoff"]
            and before["batch"] != close_receipt["batch"]
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


def coverage(rows, capability=None, mode=None):
    """Per-fixture denominators; an archived receipt counts only after successful reverification."""
    observed = [r.get("market_close") or {} for r in rows]
    eligible = sum(
        bool(
            r.get("sealed")
            and r.get("timely_news_coverage")
            and obs.get("reason") != "requires_prospective_seal"
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


def summary(rows, capability=None, mode=None):
    family_gaps = Counter()
    compared = {k: [] for k in football.PROB_KEYS}
    for r in rows:
        obs = r.get("market_close") or {}
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
        **coverage(rows, capability, mode),
        "unit": "matched fixture per endpoint",
        "primary_endpoint": "home_win",
        "endpoints": metrics,
        "missing_family_reasons": dict(sorted(family_gaps.items())),
    }
    eras = sorted(
        {
            r["implementation"]
            for r in rows
            if (r.get("market_close") or {}).get("endpoints")
        }
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
                )
                for key in eras
            },
        )
    return report
