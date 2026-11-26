"""Dual-schedule price references for the prospective EPL endpoints protocol v2."""

from sqlite3 import DatabaseError

from . import comparisons, news_market, news_study_plan, store


def observe(db, root, fixture, entry, pair, *, as_of):
    """Match both native books for each endpoint on the schedule-only research clock."""
    scope = {
        "basis": "two_source_schedule",
        "research_only": True,
        "eligible_for_formal_clv": False,
        "eligible_for_promotion": False,
    }
    if entry["mode"] not in {"technical", "formal"}:
        return scope | {"reason": "requires_prospective_seal"}
    if not news_study_plan.timely_coverage(pair):
        return scope | {"reason": "news_coverage_incomplete_or_stale"}
    scheduled = store.instant(fixture["kickoff"])
    cutoff = store.instant(pair["input_cutoff"])
    sealed = store.instant(entry["sealed_at"])
    if not cutoff <= sealed < scheduled:
        return scope | {"reason": "seal_outside_pregame_window"}
    if store.instant(as_of) <= scheduled:
        return scope | {"reason": "awaiting_postmatch_clock_check"}
    try:
        after = news_market._schedule_only_frame(
            db, root, fixture, cutoff, scheduled, as_of=as_of
        )

        def confirm_decision(slug, received_at):
            original_slug, sources = news_market._schedule_pair_at_book(
                db,
                root,
                fixture,
                after["schedule_version"],
                received_at,
                scheduled,
                decision=True,
            )
            if original_slug != slug:
                raise ValueError("different_fixture_in_schedule_and_book")
            return sources

        before = comparisons.frame(
            db,
            root,
            fixture,
            cutoff,
            cutoff,
            scheduled,
            prospective=True,
            verify_decision_schedule=confirm_decision,
        )
    except (
        DatabaseError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
    ) as exc:
        return scope | {"reason": str(exc)}

    result = scope | {
        "scheduled_kickoff": fixture["kickoff"],
        "schedule_version": after["schedule_version"],
        "postmatch_check": after["postmatch_check"],
        "input_cutoff": pair["input_cutoff"],
        "endpoints": {},
        "receipts": {},
        "missing": {},
    }
    for family, key in (("result", "home_win"), ("total", "total_over_2_5")):
        missing = {}
        if family not in before["items"]:
            missing["decision"] = before["missing"][family]
        if family not in after["family_receipts"]:
            missing["close"] = after["missing"][family]
        if missing:
            result["missing"][family] = missing
            continue
        opening = before["items"][family]
        contracts = news_market._contracts(opening)
        closing = after["family_receipts"][family]
        if contracts != closing["contracts"]:
            result["missing"][family] = {"pair": "different_contract_tokens_or_rules"}
            continue
        if not (
            store.instant(before["received_at"])
            <= cutoff
            <= sealed
            < store.instant(closing["received_at"])
            < scheduled
            and before["batch"] != closing["batch"]
        ):
            result["missing"][family] = {"pair": "unmatched_decision_close_clocks"}
            continue
        result["receipts"][family] = {
            "contracts": contracts,
            "decision": {
                "batch": before["batch"],
                "received_at": before["received_at"],
                "schedule_sources": before["schedule_sources"],
            },
            "close": closing,
        }
        result["endpoints"][key] = news_market._measure(
            pair["no_news"]["probabilities"][key],
            pair["with_news"]["probabilities"][key],
            news_market._reference(family, opening)[key],
            after["prices"][family][key],
        )
    return result
