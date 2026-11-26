"""Point-in-time research obligations derived from the domestic events actually received."""

import hashlib
import json
from datetime import timedelta
from itertools import groupby

from . import market_provider as provider
from .store import canonical, iso, read_blob


SCHEMA = """
CREATE TABLE IF NOT EXISTS big5_schedule_receipts(
 fixture TEXT NOT NULL, sport TEXT NOT NULL, slug TEXT NOT NULL, kickoff TEXT NOT NULL,
 received TEXT NOT NULL, sha TEXT NOT NULL, delays TEXT NOT NULL, stopped INTEGER NOT NULL, origin TEXT NOT NULL,
 PRIMARY KEY(slug,received,sha));
CREATE INDEX IF NOT EXISTS big5_schedule_by_fixture ON big5_schedule_receipts(fixture,received,slug,sha);
CREATE TABLE IF NOT EXISTS big5_schedule_recovery(version INTEGER PRIMARY KEY);
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


def stopped(event):
    status = (
        str(event.get("gameStatus") or event.get("status") or "").casefold().strip()
    )
    return (
        event.get("live") is True
        or event.get("ended") is True
        or event.get("cancelled") is True
        or event.get("canceled") is True
        or status
        in {"cancelled", "canceled", "postponed", "abandoned", "suspended", "void"}
    )


def record(db, fields, received, sha, waits, stop, origin):
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
                body = read_blob(root, sha)
                if hashlib.sha256(body).hexdigest() == sha:
                    data = json.loads(body)
                    originals = {
                        r.get("slug"): r
                        for r in (data if isinstance(data, list) else [data])
                        if isinstance(r, dict)
                    }
            except (OSError, TypeError, ValueError):
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
                    )
                    continue
                record(
                    db,
                    fields,
                    r["received_at"],
                    sha,
                    delays(event),
                    stopped(event),
                    "recovered_original",
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
            )
        db.execute("INSERT INTO big5_schedule_recovery VALUES(1)")


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
        for row in items:
            latest[row["slug"]] = row
        if main not in latest:
            continue
        primary = latest[main]
        kickoff = primary["kickoff"]
        siblings = [r for r in latest.values() if r["kickoff"] == kickoff]
        active = all(not r["stopped"] for r in siblings)
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


def count_windows(db, counts, start, end, now):
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
                decision_key = f"t{hours}_decision@" + iso(
                    instant - timedelta(seconds=25)
                )
                has_decision = known(decision_end)
                decision = (
                    (_slot_receipt(slots, decision_key, epoch).get("bindings") or {})
                    if has_decision
                    else {}
                )
                if has_decision and due(decision_end):
                    c = counts[league]
                    c["fixed_windows_due"] += 1
                    c["fixed_windows_complete"] += bool(
                        _slot_receipt(slots, decision_key, epoch)
                    )
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
                        key = f"t{hours}_{role}@" + iso(opens)
                        executions[delay] = (
                            _slot_receipt(slots, key, epoch).get("bindings") or {}
                        )
                        latest_expiry = max(latest_expiry or expires, expires)
                        if due(expires):
                            c = counts[league]
                            c["fixed_windows_due"] += 1
                            c["fixed_windows_complete"] += bool(
                                _slot_receipt(slots, key, epoch)
                            )
                if has_decision and latest_expiry and due(latest_expiry):
                    c = counts[league]
                    c[f"t{hours}_pairs_due"] += 1
                    c[f"t{hours}_pairs_complete"] += _pair_matches(decision, executions)
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
