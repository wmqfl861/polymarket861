"""Point-in-time research obligations derived from the domestic events actually received."""

import hashlib
import json
import re
from datetime import timedelta
from itertools import groupby

from . import market_provider as provider
from .store import canonical, instant, iso, read_blob


SCHEMA = """
CREATE TABLE IF NOT EXISTS big5_schedule_receipts(
 fixture TEXT NOT NULL, sport TEXT NOT NULL, slug TEXT NOT NULL, kickoff TEXT NOT NULL,
 received TEXT NOT NULL, sha TEXT NOT NULL, delays TEXT NOT NULL, stopped INTEGER NOT NULL, origin TEXT NOT NULL,
 PRIMARY KEY(slug,received,sha));
CREATE INDEX IF NOT EXISTS big5_schedule_by_fixture ON big5_schedule_receipts(fixture,received,slug,sha);
CREATE TABLE IF NOT EXISTS big5_schedule_recovery(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS big5_play_evidence(
 fixture TEXT NOT NULL, received TEXT NOT NULL, slug TEXT NOT NULL, sha TEXT NOT NULL,
 state TEXT NOT NULL, PRIMARY KEY(fixture,state,received,slug,sha));
"""


def delays(event):
    return sorted(
        {
            m["secondsDelay"]
            for m in event.get("markets") or []
            if isinstance(m, dict)
            and type(m.get("secondsDelay")) is int
            and m["secondsDelay"] >= 0
            and m.get("sportsMarketType")
            in ("moneyline", "totals", "soccer_team_totals")
        }
    )


# Gamma can omit the typed flags. A sporting status, cancellation or malformed
# value must not be silently promoted to an explicit or schedule-only pregame.
_STARTED = {
    "live",
    "in progress",
    "inprogress",
    "in play",
    "inplay",
    "playing",
    "started",
    "first half",
    "second half",
    "half time",
    "halftime",
    "ht",
    "1h",
    "2h",
    "extra time",
    "et",
    "penalties",
    "pens",
    "full time",
    "fulltime",
    "ft",
    "aet",
    "ended",
    "final",
    "finished",
    "completed",
    "break",
    "penaltyshootout",
    "penalty shootout",
    "ft ot",
    "ft nr",
    "vft",
}
_STOPPED = {"cancelled", "canceled", "postponed", "abandoned", "suspended", "void", "awarded", "sus", "pst", "canc"}
_PLANNED = {"scheduled", "not started", "ns", "pre", "pregame", "pre game", "upcoming"}
_BLANK = {"", "-", "--", "–", "—", "-:-", "- -", "n/a"}


def _status(value):
    return " ".join(str(value).casefold().replace("-", " ").replace("_", " ").split())


def finished_at(event, received_at):
    """Only an absolute finish time that had elapsed when Gamma reported it is evidence."""
    value = event.get("finishedTimestamp")
    if not isinstance(value, str) or received_at is None:
        return None
    try:
        stamp = instant(value.strip())
        if stamp.year > 1970 and stamp <= instant(received_at):
            return stamp
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def _field(value):
    return "" if value is None else str(value).strip().casefold()


def _scoreboard(event, received_at):
    """A positive sporting signal prevails even when another field is blank or malformed."""
    if finished_at(event, received_at) is not None:
        return "started"
    unknown = _field(event.get("finishedTimestamp")) not in _BLANK
    period = _field(event.get("period"))
    if _status(period) in _STARTED:
        return "started"
    if period not in _BLANK and _status(period) not in _PLANNED | {"0"} | _STOPPED:
        unknown = True
    elapsed = _field(event.get("elapsed"))
    if elapsed not in _BLANK:
        # Soccer minute, added time, or a minute:second clock. Punctuation alone is never play.
        clock = re.fullmatch(r"([0-9]{1,3})(?:\s*\+\s*([0-9]{1,2}))?(?::([0-5][0-9]))?['′’]?", elapsed)
        if clock is None:
            unknown = True
        elif any(int(x) for x in clock.groups() if x):
            return "started"
    score = _field(event.get("score"))
    if score not in _BLANK:
        goals = re.fullmatch(r"([0-9]{1,2})\s*[-–—:]\s*([0-9]{1,2})", score)
        if goals is None:
            unknown = True
        elif any(int(n) for n in goals.groups()):
            return "started"
    return "unclassified" if unknown else "neutral"


def has_started(event, received_at=None):
    return (
        event.get("live") is True
        or event.get("ended") is True
        or any(_status(event.get(k)) in _STARTED for k in ("gameStatus", "status"))
        or _scoreboard(event, received_at) == "started"
    )


def stopped(event, received_at=None):
    return (
        has_started(event, received_at)
        or event.get("cancelled") is True
        or event.get("canceled") is True
        or any(_status(event.get(k)) in _STOPPED for k in ("gameStatus", "status", "period"))
    )


def pregame_flags(event, received_at=None):
    """Interpret original Gamma state at its own receipt time."""
    if stopped(event, received_at):
        return "not_pregame"
    if _scoreboard(event, received_at) == "unclassified":
        return "unclassified"
    if any(
        event.get(k) is not None and type(event[k]) is not bool
        for k in (
            "live",
            "ended",
            "cancelled",
            "canceled",
            "closed",
            "archived",
            "active",
        )
    ):
        return "unclassified"
    if (
        event.get("closed") is True
        or event.get("archived") is True
        or event.get("active") is False
    ):
        # The whole listing may be unavailable despite open child contracts.
        # Its venue state does not establish the time of a sporting kickoff.
        return "unclassified"
    if any(
        event.get(k) not in (None, "") and _status(event[k]) not in _PLANNED
        for k in ("gameStatus", "status")
    ):
        return "unclassified"
    if event.get("live") is False and event.get("ended") is False:
        return "explicit_pregame"
    return "schedule_only"


def _record_play(db, fixture, slug, received, sha, event, *, indexed_started=False):
    if indexed_started or event is not None and has_started(event, received):
        state = "started"
    elif event is None:
        state = "unverified"
    elif pregame_flags(event, received) == "unclassified":
        state = "unclassified_at_receipt"
    else:
        return
    db.execute(
        "INSERT OR IGNORE INTO big5_play_evidence VALUES(?,?,?,?,?)",
        (fixture, iso(received), slug, sha, state),
    )


def play_history(db, fixture, first, through):
    for state, grade in (("started", "not_pregame"), ("unverified", "unclassified")):
        if db.execute(
            "SELECT 1 FROM big5_play_evidence WHERE fixture=? AND state=? AND received<=? LIMIT 1",
            (fixture, state, through),
        ).fetchone():
            return grade
    # An inactive parent listing can reopen. Use its latest snapshot at the
    # first instant and any intervening receipt, never a superseded older status.
    if db.execute(
        "SELECT 1 FROM big5_play_evidence p WHERE fixture=? AND state='unclassified_at_receipt' "
        "AND received<=? AND received>=COALESCE((SELECT MAX(h.received) FROM big5_schedule_receipts h "
        "WHERE h.slug=p.slug AND h.received<=?),?) LIMIT 1",
        (fixture, through, first, first),
    ).fetchone():
        return "unclassified"
    return None


def consistent_during(db, fixture, kickoff, first, last):
    # Each linked event contributes its most recent receipt at the first book
    # request and any corrections received through the last book response.
    conflict = db.execute(
        "SELECT 1 FROM big5_schedule_receipts s WHERE fixture=? AND received<=? AND "
        "(kickoff<>? OR stopped) AND received>=COALESCE(("
        "SELECT MAX(h.received) FROM big5_schedule_receipts h WHERE h.slug=s.slug AND h.received<=?"
        "),?) LIMIT 1",
        (fixture, last, kickoff, first, first),
    ).fetchone()
    return conflict is None


def record(db, fields, received, sha, waits, stop, origin, *, event):
    slug, fixture, sport, kickoff = fields[:4]
    db.execute(
        "INSERT OR IGNORE INTO big5_schedule_receipts VALUES(?,?,?,?,?,?,?,?,?)",
        (
            fixture,
            sport,
            slug,
            kickoff,
            iso(received),
            sha,
            canonical(waits),
            int(stop),
            origin,
        ),
    )
    _record_play(db, fixture, slug, received, sha, event)


def _original_events(root, sha):
    data = read_blob(root, sha)
    if hashlib.sha256(data).hexdigest() != sha:
        raise ValueError("schedule receipt hash mismatch")
    data = json.loads(data)
    result = {}
    for event in data if isinstance(data, list) else [data]:
        if isinstance(event, dict) and isinstance(slug := event.get("slug"), str):
            result[slug] = None if slug in result else event
    return result


def recover(root, db, validate):
    """Recover old versions only with their own original. A latest-only index cannot backdate knowledge."""
    if db.execute("SELECT 1 FROM big5_schedule_recovery WHERE version=1").fetchone():
        return
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute(
            "SELECT 1 FROM big5_schedule_recovery WHERE version=1"
        ).fetchone():
            return
        old = db.execute(
            "SELECT e.*,b.sport,b.home_id,b.away_id FROM ma_events e JOIN big5_events b "
            "ON e.slug=b.slug AND e.event_id=b.event_id ORDER BY e.sha,e.received_at,e.slug"
        )
        for sha, rows in groupby(old, key=lambda r: r["sha"]):
            originals = {}
            try:
                originals = _original_events(root, sha)
            except (FileNotFoundError, TypeError, ValueError):
                pass
            for r in rows:
                event = originals.get(r["slug"])
                try:
                    fields = validate(event, r["sport"])
                    if fields[4:7] != (
                        r["home_id"],
                        r["away_id"],
                        r["event_id"],
                    ) or fields[3] != iso(provider.moment(r["kickoff"])):
                        raise ValueError("original differs from legacy identity")
                except (KeyError, TypeError, ValueError):
                    record(
                        db,
                        (
                            r["slug"],
                            r["fixture"],
                            r["sport"],
                            r["kickoff"] or r["received_at"],
                        ),
                        r["received_at"],
                        sha,
                        [],
                        True,
                        "legacy_unverified",
                        event=None,
                    )
                    continue
                record(
                    db,
                    fields,
                    r["received_at"],
                    sha,
                    delays(event),
                    stopped(event, r["received_at"]),
                    "recovered_original",
                    event=event,
                )
        # An incomplete old installation may have the current index only. State before the
        # latest observed receipt, or a receipt without original evidence, is unknown.
        for r in db.execute(
            "SELECT * FROM big5_events b WHERE NOT EXISTS (SELECT 1 FROM big5_schedule_receipts v "
            "WHERE v.slug=b.slug AND v.received=b.receipt AND v.sha=b.sha)"
        ):
            record(
                db,
                (r["slug"], r["fixture"], r["sport"], r["kickoff"]),
                r["receipt"],
                r["sha"],
                [],
                True,
                "legacy_unverified",
                event=None,
            )
        db.execute("INSERT INTO big5_schedule_recovery VALUES(1)")


def recover_play(root, db, validate):
    """Upgrade earlier flag-only indexes from each original's own receipt time."""
    complete = "SELECT COUNT(*)=2 FROM big5_schedule_recovery WHERE version IN (2,3)"
    if db.execute(complete).fetchone()[0]:
        return
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute(complete).fetchone()[0]:
            return
        known = db.execute(
            "SELECT h.*,s.live,s.ended FROM big5_schedule_receipts h LEFT JOIN ma_event_states s "
            "ON (s.slug,s.received_at,s.sha)=(h.slug,h.received,h.sha) ORDER BY h.sha"
        )
        for sha, rows in groupby(known, key=lambda r: r["sha"]):
            try:
                originals = _original_events(root, sha)
            except (FileNotFoundError, TypeError, ValueError):
                originals = {}
            for r in rows:
                native = originals.get(r["slug"])
                _record_play(
                    db,
                    r["fixture"],
                    r["slug"],
                    r["received"],
                    sha,
                    None if r["origin"] == "legacy_unverified" else native,
                    indexed_started=r["live"] or r["ended"],
                )
        # Older direct refreshes archived the auxiliary but rejected its divergent
        # clock before indexing it. Recover that interruption at its own receipt time.
        from .archive_proof import original_receipt

        skipped = db.execute(
            "SELECT r.selector,r.sha,r.received_at,b.sport,b.fixture,b.home_id,b.away_id,b.event_id FROM ma_requests r "
            "JOIN big5_events b ON b.slug=r.selector WHERE r.operation='league_event' AND r.status='ok' "
            "AND b.slug<>b.fixture AND NOT EXISTS (SELECT 1 FROM big5_schedule_receipts h "
            "WHERE h.slug=r.selector AND h.received=r.received_at AND h.sha=r.sha)"
        )
        for r in skipped:
            try:
                reply, _, _, _ = original_receipt(
                    db,
                    root,
                    r["sha"],
                    r["received_at"],
                    {"league_event"},
                    expected_selector=r["selector"],
                )
                event = json.loads(reply.body)
                fields = validate(event, r["sport"], history=db, received_at=r["received_at"])
                if (fields[0], fields[1], *fields[4:7]) != tuple(
                    r[k]
                    for k in ("selector", "fixture", "home_id", "away_id", "event_id")
                ):
                    continue
                record(
                    db,
                    fields,
                    r["received_at"],
                    r["sha"],
                    delays(event),
                    True,
                    "conflicting_aux_schedule",
                    event=event,
                )
            except (FileNotFoundError, KeyError, TypeError, ValueError):
                continue
        db.execute("INSERT OR IGNORE INTO big5_schedule_recovery VALUES(2),(3)")


def bindings(contracts):
    """A research sampling intention identifies both original outcome tokens and the effective rule and fee."""
    result = {}
    for side, c in contracts.items():
        tokens = c.get("tokens", {})
        if (
            len(tokens) != 2
            or any("error" in v or not v.get("id") for v in tokens.values())
            or not c.get("condition")
            or not c.get("rule_sha256")
            or type(c.get("seconds_delay")) is not int
        ):
            return {}
        profile = c.get("rule_profile") or {}
        identity = [
            c["condition"],
            c.get("market_slug"),
            c.get("event_slug"),
            c.get("event_id"),
            c.get("protocol"),
            side,
            c["rule_sha256"],
            profile.get("version"),
            profile.get("template_sha256"),
            sorted((k, v["id"]) for k, v in tokens.items()),
            c["seconds_delay"],
            c.get("fees_enabled"),
            c.get("fee_schedule"),
        ]
        result[side] = [
            max(5, c["seconds_delay"]),
            hashlib.sha256(canonical(identity).encode()).hexdigest(),
        ]
    return result


def _epochs(receipts):
    """Merge independently received main/aux states. Repeating a snapshot never changes the start of knowledge."""
    main = receipts[0]["fixture"]
    latest = {}
    out = []
    previous = None
    for timestamp, items in groupby(receipts, key=lambda r: r["received"]):
        for slug, values in groupby(items, key=lambda r: r["slug"]):
            rows = list(values)
            # No hash ordering can establish which differing response was
            # received last when both observations have the exact same clock.
            latest[slug] = (
                rows[0]
                if len({(r["kickoff"], r["stopped"], r["delays"]) for r in rows}) == 1
                else None
            )
        if main not in latest:
            continue
        primary = latest[main]
        kickoff = primary["kickoff"] if primary is not None else None
        siblings = [r for r in latest.values() if r is not None]
        active = len(siblings) == len(latest) and all(
            r["kickoff"] == kickoff and not r["stopped"] for r in siblings
        )
        current = kickoff if active else None
        waits = frozenset(
            max(5, n) for r in siblings for n in json.loads(r["delays"])
        ) or frozenset({5})
        if previous != current:
            if previous is not None:
                out[-1]["until"] = timestamp
            if current is not None:
                out.append(
                    {
                        "kick": kickoff,
                        "since": timestamp,
                        "until": None,
                        "waits": [(timestamp, waits)],
                    }
                )
        elif current is not None and out[-1]["waits"][-1][1] != waits:
            out[-1]["waits"].append((timestamp, waits))
        previous = current
    return out


def _held_at_expiry(since, until, expiry):
    # The previous schedule owns the half-open window ending exactly at a revision receipt.
    return since < expiry and (until is None or expiry <= until)


def _slot_receipt(slots, key, epoch):
    row = slots.get(key)
    if not row or not row["success"]:
        return {}
    item = json.loads(row["report"] or "{}")
    first, last = item.get("first_receipt"), item.get("finished")
    if (
        not first
        or not last
        or first < epoch["since"]
        or (epoch["until"] and last >= epoch["until"])
    ):
        return {}
    return item


def _pair_matches(decision, executions):
    if not decision:
        return False
    checked = {}
    for delay, witness in executions.items():
        if not witness:
            return False
        for side, candidate in witness.items():
            if candidate[0] == delay:
                checked[side] = candidate
    return checked == decision


def _fixed_slot(root, db, slots, fixture, epoch, phase, first, expires):
    from . import big5_market_receipts as receipts

    if phase + "@" + first not in slots:
        return {}
    # The legacy slot row only saved a summary for fully flagged books. Reopen
    # the actual run from its own window, including an earlier complete attempt
    # followed by an unsuccessful retry. Never synthesize a leg from two runs.
    for row in db.execute(
        "SELECT report FROM big5_runs WHERE fixture=? AND phase=? AND started>=? AND started<? "
        "ORDER BY started,id",
        (fixture, phase, first, expires),
    ):
        item = receipts.witness(
            db,
            root,
            json.loads(row["report"]),
            fixture,
            epoch["kick"],
            (first, expires),
        )
        if (
            item
            and epoch["since"] <= item["first_request"]
            and (not epoch["until"] or item["finished"] < epoch["until"])
        ):
            return item
    return {}


def _count_state(counter, prefix, state):
    from .big5_market_receipts import STATES

    counter[prefix + "_complete"] += state is not None
    for name in STATES:
        counter[prefix + "_" + name] += state == name


def _pair_state(db, fixture, decision, executions):
    from .big5_market_receipts import combined_state

    if not _pair_matches(
        decision.get("bindings", {}),
        {delay: item.get("bindings", {}) for delay, item in executions.items()},
    ):
        return None
    grades = [decision["exchange_state"]]
    last = max(item["finished"] for item in executions.values())
    history = play_history(db, fixture, decision["first_request"], last)
    if history:
        grades.append(history)
    for side, (delay, _) in decision["bindings"].items():
        grades.append(executions[delay]["contract_exchange_states"][side])
    return combined_state(grades)


def _fixed_gap(root, db, slots, league, fixture, epoch, phase, first, end, required):
    from .big5_market_receipts import late_book_receipts

    slot = slots.get(phase + "@" + first)
    attempts, reasons, refresh_triggers = [], {}, {}

    def add_reasons(report):
        for key, destination in (("refresh_trigger", refresh_triggers), ("event_gaps", reasons)):
            for gap in report.get(key, []):
                if gap.get("error"):
                    reason = gap["error"]
                    if gap.get("slug"):
                        reason = gap["slug"] + ": " + reason
                    destination[reason] = None
        for key in ("batch_error", "error"):
            if report.get(key):
                reasons[str(report[key])] = None
        for group in report.get("contracts", {}).values():
            for token in group.get("tokens", {}).values():
                if token.get("error"):
                    reasons[str(token["error"])] = None

    for row in db.execute(
        "SELECT started,finished,report FROM big5_runs WHERE fixture=? AND phase=? "
        "AND started>=? AND started<? ORDER BY started,id",
        (fixture, phase, max(first, epoch["since"]), min(end, epoch["until"] or end)),
    ):
        report = json.loads(row["report"])
        if report.get("claimed_kickoff", epoch["kick"]) != epoch["kick"]:
            continue
        add_reasons(report)
        if report.get("core_missing") and report.get("requested_tokens"):
            reasons["missing core contracts: " + ", ".join(report["core_missing"])] = None
        attempt = {
            "started": row["started"], "finished": row["finished"],
            "requested_tokens": report.get("requested_tokens"),
            "received_tokens": report.get("received_tokens"),
        }
        late = late_book_receipts(db, root, fixture, report, (first, end))
        if late:
            attempt["late_book_receipts"] = late
            for item in late:
                reasons[item["reason"]] = None
        attempts.append(attempt)
    if slot and slot["report"]:
        summary = json.loads(slot["report"])
        if summary.get("late_book_receipts") and summary.get("error") == "; ".join(
            item["reason"] for item in summary["late_book_receipts"]
        ):
            # Re-derive deadline diagnoses above from the native response when reopening a slot.
            summary.pop("error")
        add_reasons(summary)
    if not reasons:
        reason = "no capture attempt recorded"
        if attempts:
            reason = "native receipts did not verify a complete fixed window"
        elif slot:
            reason = "claimed window has no finished capture record"
        reasons[reason] = None
    return {
        "league": league, "fixture": fixture, "kickoff": epoch["kick"], "phase": phase,
        "window": [first, end], "required_core_tokens": required,
        "attempts": attempts, "reasons": list(reasons), "refresh_triggers": list(refresh_triggers),
    }


def count_windows(root, db, counts, start, end, now, required_core_tokens):
    gaps = []
    scope = db.execute(
        "SELECT * FROM big5_schedule_receipts WHERE received<? "
        "ORDER BY fixture,received,slug,sha",
        (min(end, now),),
    )
    for fixture, items in groupby(scope, key=lambda r: r["fixture"]):
        receipts = list(items)
        league = receipts[0]["sport"]
        for epoch in _epochs(receipts):
            kick = provider.moment(epoch["kick"])
            # Older receipts without a provable original sampling window cannot establish a slot or pair.
            slots = {
                r["slot"]: r
                for r in db.execute(
                    "SELECT slot,success,report FROM big5_slots WHERE fixture=? AND kickoff=?",
                    (fixture, epoch["kick"]),
                )
            }

            def due(t):
                return start <= t < end and t <= now

            def known(t):
                return _held_at_expiry(epoch["since"], epoch["until"], t)

            for hours in (24, 1):
                instant = kick - timedelta(hours=hours)
                decision_end = iso(instant)
                if decision_end >= end or decision_end > now:
                    continue
                max_wait = max(max(waits) for _, waits in epoch["waits"])
                if iso(instant + timedelta(seconds=max_wait + 5)) < start:
                    continue
                has_decision = known(decision_end)
                decision = (
                    _fixed_slot(
                        root,
                        db,
                        slots,
                        fixture,
                        epoch,
                        f"t{hours}_decision",
                        iso(instant - timedelta(seconds=25)),
                        decision_end,
                    )
                    if has_decision
                    else {}
                )
                if has_decision and due(decision_end):
                    c = counts[league]
                    c["fixed_windows_due"] += 1
                    _count_state(c, "fixed_windows", decision.get("exchange_state"))
                    if not decision:
                        gaps.append(_fixed_gap(
                            root, db, slots, league, fixture, epoch, f"t{hours}_decision",
                            iso(instant - timedelta(seconds=25)), decision_end, required_core_tokens,
                        ))
                executions = {}
                latest_expiry = None
                for i, (begin, waits) in enumerate(epoch["waits"]):
                    until = (
                        epoch["waits"][i + 1][0]
                        if i + 1 < len(epoch["waits"])
                        else epoch["until"]
                    )
                    for delay in sorted(waits):
                        opens = instant + timedelta(seconds=delay)
                        expires = iso(opens + timedelta(seconds=5))
                        if not _held_at_expiry(begin, until, expires):
                            continue
                        role = "execution" if delay == 5 else f"execution_{delay}s"
                        item = (
                            _fixed_slot(
                                root,
                                db,
                                slots,
                                fixture,
                                epoch,
                                f"t{hours}_{role}",
                                iso(opens),
                                expires,
                            )
                            if expires < end and expires <= now
                            else {}
                        )
                        executions[delay] = item
                        latest_expiry = max(latest_expiry or expires, expires)
                        if due(expires):
                            c = counts[league]
                            c["fixed_windows_due"] += 1
                            _count_state(c, "fixed_windows", item.get("exchange_state"))
                            if not item:
                                gaps.append(_fixed_gap(
                                    root, db, slots, league, fixture, epoch, f"t{hours}_{role}",
                                    iso(opens), expires, required_core_tokens,
                                ))
                if has_decision and latest_expiry and due(latest_expiry):
                    c = counts[league]
                    c[f"t{hours}_pairs_due"] += 1
                    _count_state(
                        c,
                        f"t{hours}_pairs",
                        _pair_state(db, fixture, decision, executions),
                    )
            if (
                iso(kick - timedelta(seconds=120)) >= end
                or iso(kick + timedelta(seconds=600)) < start
            ):
                continue
            for offset in range(-120, 600, 12):
                first = kick + timedelta(seconds=offset)
                expires = iso(first + timedelta(seconds=12))
                if due(expires) and known(expires):
                    c = counts[league]
                    c["closing_slots_due"] += 1
                    c["closing_slots_complete"] += bool(
                        _slot_receipt(slots, "closing@" + iso(first), epoch)
                    )
    return gaps
