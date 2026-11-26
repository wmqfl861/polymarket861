"""Reconstruct timely domestic sampling receipts separately from evidence of match state."""

from __future__ import annotations

import json
from datetime import timedelta

from . import archive_proof, execution_books, market_provider as provider
from . import big5_market_schedule as schedule

STATES = ("explicit_pregame", "schedule_only", "unclassified", "not_pregame")


def combined_state(states):
    """The weakest state used by a completed window or matched pair."""
    return max(states, key=STATES.index)


def rule_profile(market):
    from .ledger_terms import rule_profile as reviewed

    try:
        return reviewed(market)
    except ValueError as exc:
        return {"version": None, "unreviewed": str(exc)}


def _event(db, root, slug, sha, received, league):
    from .big5_market import _identity

    # A simultaneous catalog request on another league can return the same
    # bytes. Authenticate the selector as well as the hash and receipt time.
    try:
        reply, _, _, _ = archive_proof.original_receipt(
            db, root, sha, received, {"league_event"}, expected_selector=slug
        )
    except ValueError:
        reply, _, _, _ = archive_proof.original_receipt(
            db, root, sha, received, {"league_catalog"}, expected_selector=league
        )
        events = json.loads(reply.body)
        if not isinstance(events, list):
            raise ValueError("catalogue original is not an event list")
    else:
        events = [json.loads(reply.body)]
    selected = [e for e in events if isinstance(e, dict) and e.get("slug") == slug]
    if len(selected) != 1:
        raise ValueError("native Gamma receipt does not uniquely identify this event")
    event = selected[0]
    identity = _identity(event, league, history=db, received_at=received)
    state = schedule.pregame_flags(event, received)
    if not provider.fresh_observation(reply) and state != "not_pregame":
        state = "unclassified"
    markets = provider.normalized_markets(event, sport=league, project_teams=True)
    return identity, markets, state


def _terms(market):
    return {
        "condition": market["condition_id"],
        "market_slug": market["slug"],
        "event_slug": market["event_slug"],
        "event_id": market["event_id"],
        "protocol": market["protocol"],
        "rule_sha256": market["rule_sha256"],
        "seconds_delay": market["seconds_delay"],
        "fees_enabled": market["fees_enabled"],
        "fee_schedule": market["fee_schedule"],
    }


def _outcomes(market):
    return {t["outcome"]: t["token_id"] for t in market["tokens"]}


def _native_markets(db, root, fixture, kickoff, groups):
    from .big5_market import _selection

    events = {}
    league = fixture.split("-", 1)[0]
    for group in groups.values():
        slug = group["event_slug"]
        receipt = (group["contract_source_sha256"], group["contract_received"])
        if slug in events:
            if events[slug][0] != receipt:
                raise ValueError("simultaneous contracts use different event receipts")
            continue
        if slug not in (fixture, fixture + "-more-markets"):
            raise ValueError("Gamma receipt belongs to a different fixture")
        identity, markets, state = _event(db, root, slug, *receipt, league)
        events[slug] = receipt, identity, markets, state
    if fixture not in events or any(
        identity[3] != kickoff or identity[4:6] != events[fixture][1][4:6]
        for _, identity, _, _ in events.values()
    ):
        raise ValueError("event receipts disagree on kickoff or participants")
    selected, _ = _selection(
        [m for _, _, items, _ in events.values() for m in items], True
    )
    if set(groups) != selected.keys():
        raise ValueError("run does not cover the original eligible selection")
    return selected, events


def _state_at_receipt(db, root, selected, events, slug, received):
    """A correction received during the CLOB request already applies to its books."""
    from .big5_market import _selection

    (sha, known), identity, _, state = events[slug]
    for row in db.execute(
        "SELECT received,sha FROM big5_schedule_receipts WHERE slug=? AND received>=? AND received<=? "
        "AND NOT (received=? AND sha=?) ORDER BY received,sha",
        (slug, known, received, known, sha),
    ):
        updated, markets, grade = _event(
            db, root, slug, row["sha"], row["received"], identity[2]
        )
        if updated != identity:
            raise ValueError(
                "event identity or kickoff changed before the book arrived"
            )
        other = [
            m for s, (_, _, items, _) in events.items() if s != slug for m in items
        ]
        active, _ = _selection(other + markets, True)
        if active.keys() != selected.keys() or any(
            _terms(m) != _terms(selected[k]) or _outcomes(m) != _outcomes(selected[k])
            for k, m in active.items()
        ):
            raise ValueError("a listing changed before the book arrived")
        state = combined_state((state, grade))
    return state


def _book_batch(db, root, fixture, sha, received, *, require_fresh=True):
    reply, _, _, meta = archive_proof.original_receipt(
        db, root, sha, received, {"books"}, expected_selector=fixture
    )
    requested = meta.get("request_token_ids")
    if not isinstance(requested, list) or (require_fresh and not provider.fresh_observation(reply)):
        raise ValueError("missing native CLOB request or stale HTTP response")
    return reply, execution_books.native_rows(json.loads(reply.body), requested)


def late_book_receipts(db, root, fixture, report, window):
    """Attribute missed deadlines to actual book replies, including in older archives."""
    if report.get("fixture") != fixture or report.get("capture_window") not in (window, list(window)):
        return []
    deadline = window[1]
    candidates = {}
    for group in report.get("contracts", {}).values():
        for token in group.get("tokens", {}).values():
            received, sha = token.get("received_at"), token.get("source_sha256")
            if received and received >= deadline and sha and token.get("id"):
                candidates.setdefault((received, sha), {})[token["id"]] = group.get("condition")
    late = []
    for (received, sha), conditions in sorted(candidates.items()):
        try:
            # The HTTP arrival time is observable even if the depth itself was stale.
            reply, native = _book_batch(db, root, fixture, sha, received, require_fresh=False)
            if not max(window[0], report["started"]) <= reply.requested_at <= received:
                continue
            count = sum(
                token in native and native[token].get("market") == condition
                for token, condition in conditions.items()
            )
            micros = (provider.moment(received) - provider.moment(deadline)) // timedelta(microseconds=1)
            if count:
                late.append({
                    "received_tokens": count, "requested_at": reply.requested_at, "received_at": received,
                    "cutoff_exclusive": deadline, "late_by_microseconds": micros, "source_sha256": sha,
                    "reason": f"{count} native CLOB token books arrived late at {received} "
                              f"(exclusive cutoff {deadline})",
                })
        except (KeyError, TypeError, ValueError, FileNotFoundError):
            continue
    return late


def witness(db, root, report, fixture, kickoff, window):
    """Use the same immutable Gamma and native CLOB receipts for old and new runs.

    A successful HTTP receipt says nothing on its own about tradability. Each
    original token must belong to this slot and arrive inside its half-open window.
    """
    from .big5_market import CORE

    if not isinstance(report, dict):
        return {}
    groups = report.get("contracts")
    if (
        report.get("capture_window") not in (tuple(window), list(window))
        or report.get("fixture") != fixture
        or report.get("core_missing") != []
        or not isinstance(groups, dict)
        or not set(CORE) <= groups.keys()
        or report.get("requested_tokens") != 2 * len(groups)
        or report.get("received_tokens") != report["requested_tokens"]
    ):
        return {}
    try:
        markets, events = _native_markets(db, root, fixture, kickoff, groups)
        batches, states, histories = {}, {}, {}
        requests, receipts, grades = [], [], {}
        for side, group in groups.items():
            market = markets[side]
            if market["game_times"] != [kickoff]:
                return {}
            if any(group[k] != value for k, value in _terms(market).items()):
                return {}
            profile = rule_profile(market)
            if any(
                group["rule_profile"].get(k) != profile.get(k)
                for k in ("version", "template_sha256")
            ):
                return {}
            tokens = group["tokens"]
            if {label: t["id"] for label, t in tokens.items()} != _outcomes(market):
                return {}
            market.update(
                recorded_at=group["contract_received"],
                source_sha256=group["contract_source_sha256"],
            )
            received_last = max(t["received_at"] for t in tokens.values())
            state_key = (market["event_slug"], received_last)
            if state_key not in states:
                states[state_key] = _state_at_receipt(
                    db, root, markets, events, *state_key
                )
            grade = states[state_key]
            interval = (min(t["requested_at"] for t in tokens.values()), received_last)
            if interval not in histories:
                histories[interval] = schedule.play_history(db, fixture, *interval)
            if histories[interval]:
                grade = combined_state((grade, histories[interval]))
            for token in tokens.values():
                sha, received = token["source_sha256"], token["received_at"]
                key = (sha, received)
                if key not in batches:
                    batches[key] = _book_batch(db, root, fixture, sha, received)
                reply, native = batches[key]
                requested = reply.requested_at
                if not window[0] <= requested <= received < window[1]:
                    return {}
                age = provider.moment(requested) - provider.moment(
                    market["recorded_at"]
                )
                if not timedelta(0) <= age <= timedelta(minutes=10):
                    return {}
                ident = token["id"]
                book = provider.normalize_book(native[ident], reply, market, ident, sha)
                if (
                    not book["exchange_at"]
                    or book["exchange_at"] > received
                    or token.get("on_time") is not True
                    or any(
                        book[k] != token.get(k)
                        for k in ("requested_at", "observed_at", "exchange_at")
                    )
                ):
                    return {}
                row = db.execute(
                    "SELECT payload FROM ma_books WHERE token=? AND received_at=? AND sha=?",
                    (ident, received, sha),
                ).fetchone()
                if not row or json.loads(row[0]) != book:
                    return {}
                requests.append(requested)
                receipts.append(received)
                if received >= kickoff and grade != "not_pregame":
                    grade = "unclassified"
            grades[side] = grade
        first_request, finished = min(requests), max(receipts)
        if not schedule.consistent_during(
            db, fixture, kickoff, first_request, finished
        ):
            return {}
        return {
            "first_request": first_request,
            "first_receipt": min(receipts),
            "finished": finished,
            "bindings": schedule.bindings(groups),
            "contract_exchange_states": grades,
            "exchange_state": combined_state(grades.values()),
        }
    except (
        KeyError,
        IndexError,
        AttributeError,
        TypeError,
        ValueError,
        ArithmeticError,
        FileNotFoundError,
    ):
        return {}
