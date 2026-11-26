"""Interpret the two paper accounts and source reports at the time the coordinator receives them."""

from collections import Counter
from datetime import timedelta

from . import store
from .season_alerts import report_cause


_STRATEGY_SKIPS = {
    "model_market_gap",
    "insufficient_edge_depth_cash_or_fixture_budget",
    "insufficient_edge_depth_cash_or_minimum",
    "remaining_depth_price_cash_or_fixture_risk_insufficient",
    "market disagreement exceeds 0.08",
    "insufficient edge, depth, budget or minimum size",
    "remaining depth, price, cash or fixture risk insufficient",
    "insufficient cash",
    "no eligible delayed fill",
}


def account_summary(account, report, action_key, started):
    actions = report[action_key]
    states = Counter(a["state"] for a in actions)
    gap_reasons = Counter(g["reason"] for g in report["gaps"])
    skip_reasons = Counter(
        a.get("reason") or "unspecified skip"
        for a in actions
        if a["state"] == "skipped"
    )
    problems = gap_reasons.copy()
    if actions:
        # Missing market families are reported as coverage even when other listed
        # tokens were evaluated correctly. A fixture with no listing still alerts.
        problems.pop("no verified listing at decision", None)
    problems.update({k: n for k, n in skip_reasons.items() if k not in _STRATEGY_SKIPS})
    for a in actions:
        if a.get("close_status") == "missing":
            problems["missing scheduled closing book"] += 1
        if a["state"] in ("filled", "held", "settled") and a.get("reason"):
            problems[a["reason"]] += 1
    held = states["filled"] + states["held"]
    positive = states["frozen"] + held + states["settled"]
    if problems:
        state = "partial" if positive else "gap"
    elif positive:
        state = "active"
    elif states["skipped"]:
        state = "no_value"
    else:
        state = "idle"
    return {
        "account": account,
        "as_of": report.get("observed_at") or started,
        "state": state,
        "decisions": len(actions),
        "frozen": states["frozen"],
        "held": held,
        "skipped": states["skipped"],
        "settled": states["settled"],
        "repeated": sum(a.get("repeated") is True for a in actions),
        "gaps": sum(gap_reasons.values()),
        "gap_reasons": dict(gap_reasons),
        "skip_reasons": dict(skip_reasons),
        "operational_reasons": dict(problems),
    }


def service_problems(name, result):
    if name == "history":
        for err in result.get("catalog_errors", []):
            yield "catalog", str(err.get("error") or err)
    elif name == "news":
        weights = result.get("player_weights") or {}
        if weights.get("alert"):
            yield "player_weight_fallback", (
                f"player weights use fallback for {weights['fallback_claims']}/{weights['weights_with_basis']} "
                f"applied claims ({weights['fallback_share']:.1%}); see news status for source reasons"
            )
        for stream in result.get("streams", []):
            if stream.get("state") in ("error", "no_roster", "pending", "lease_lost", "degraded"):
                cause = f"{stream.get('source')}/{stream.get('team')}"
                detail = ", ".join(map(str, stream.get("issues", []))) or stream["state"]
                yield cause, f"{cause} — {detail}"
        updates = result.get("prediction_updates") or {}
        if updates.get("state") == "error":
            yield "news-prediction", updates.get("reason") or "event-driven forecast failed"
        for item in updates.get("items", []):
            if item.get("status") in ("deferred", "error"):
                yield "news-prediction/" + str(item.get("fixture_id")), str(item.get("reason") or item["status"])
    elif name == "results":
        source_failures = {}
        for attempt in result.get("fetches", []):
            status = str(attempt.get("status", ""))
            if status.startswith(
                ("unavailable", "stale_cache", "backoff", "stale cache")
            ):
                source_failures.setdefault("OpenLigaDB", []).append(
                    f"{attempt.get('scope')}: {attempt.get('error') or status}"
                )
        for field in ("fallback", "secondary"):
            source = result.get(field) or {}
            if (
                source.get("status") in ("unavailable", "error", "degraded")
                or source.get("previous") == "unavailable"
            ):
                source_failures.setdefault(source.get("source") or field, []).append(
                    str(
                        source.get("error")
                        or source.get("previous")
                        or source.get("status")
                    )
                )
        for err in result.get("receipt_errors", []):
            source_failures.setdefault(err.get("source") or "receipt", []).append(
                str(err.get("error") or err)
            )
        for source, reasons in source_failures.items():
            yield "source:" + source, source + " — " + ", ".join(dict.fromkeys(reasons))
        overdue = result.get("overdue") or []
        if overdue:
            fixtures = [str(f["fixture_id"]) for f in overdue]
            yield (
                "overdue",
                f"{len(fixtures)} overdue EPL results: " + ", ".join(fixtures[:12]),
            )
        if result.get("quarantined"):
            yield "quarantined", f"{len(result['quarantined'])} quarantined EPL results"
        if result.get("status") == "degraded" and not (
            overdue or source_failures or result.get("quarantined")
        ):
            yield "degraded", "EPL result reconciliation is degraded"


def paper_state(db, result, row, stamp):
    states = []
    for name in ("formal", "shadow"):
        outcome = result.get(name) or {}
        state = "error" if outcome.get("error") else outcome.get("state")
        if state not in ("error", "gap", "partial", "active", "no_value", "idle"):
            state = "error"
            outcome = result[name] = {
                "error": "paper account returned no valid summary",
                "as_of": stamp,
            }
        outcome["state"] = state
        if (
            row["role"] in ("decision", "after_decision_book")
            and state == "idle"
            and (
                store.instant(outcome["as_of"])
                > store.instant(row["kickoff"]) - timedelta(hours=1)
            )
        ):
            state = outcome["state"] = "gap"
            outcome["operational_reasons"] = {
                "decision window elapsed before account ran": 1
            }
        if state == "error":
            report_cause(
                db,
                row["fixture"],
                "paper_" + name,
                outcome.get("error_type", "error"),
                outcome["error"],
                stamp,
            )
        elif state in ("gap", "partial"):
            for cause, count in outcome["operational_reasons"].items():
                report_cause(
                    db,
                    row["fixture"],
                    "paper_" + name,
                    cause,
                    f"{cause} ({count})",
                    stamp,
                )
        states.append(state)
    if all(s in ("error", "gap") for s in states):
        return "error" if "error" in states else "gap"
    if any(s in ("error", "gap", "partial") for s in states):
        return "partial"
    return "done"
