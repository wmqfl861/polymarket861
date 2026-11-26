"""Reconcile paper positions with archived exchange payouts and frozen entry rules."""

import json
from collections import Counter
from contextlib import closing
from datetime import timedelta
from decimal import ROUND_DOWN

from . import (
    archive_proof,
    dbscope,
    ledger,
    market_archive,
    market_provider,
    opportunity,
    shadow,
    shadow_market,
    store,
)
from .ledger_rules import (
    UNIT,
    digest,
    fmt,
    instant,
    iso,
    number,
    packed,
    scenario_payout,
    unit,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settlement_queue(
 mode TEXT NOT NULL,account TEXT NOT NULL,intent_id TEXT NOT NULL,fixture TEXT NOT NULL,token TEXT NOT NULL,
 state TEXT NOT NULL,reason TEXT,checked_at TEXT NOT NULL,details TEXT NOT NULL,PRIMARY KEY(mode,account,intent_id));
CREATE INDEX IF NOT EXISTS settlement_queue_checked ON settlement_queue(checked_at);
CREATE INDEX IF NOT EXISTS settlement_queue_account_checked ON settlement_queue(account,checked_at);
"""


def _regulation(root, fixture):
    from .result_admission import verified_regulation

    return verified_regulation(root, fixture)


def _records(root, token):
    with closing(market_archive.database(root)) as db:
        return _records_from_db(db, root, token)


def resolution_history(db, root, token, *, as_of, market=None):
    """Reconstruct every authenticated observation through a single receipt frontier."""

    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='ma_requests'"
    ).fetchone():
        return []
    rows = db.execute(
        "SELECT sha,received_at FROM ma_requests WHERE operation='resolution' AND selector=? AND status='ok' "
        "AND received_at<=? ORDER BY received_at,rowid",
        (token, store.iso(as_of)),
    ).fetchall()
    items = []
    saw_terminal = False
    for r in rows:
        reply, _, selector, _ = archive_proof.original_receipt(
            db,
            root,
            r["sha"],
            r["received_at"],
            {"resolution"},
            expected_selector=token,
        )
        if selector != token:
            raise ValueError("resolution request selector differs from token")
        native = market_provider.resolution_row(json.loads(reply.body), token)
        if market is not None and native is not None:
            _match_record_identity(market, token, native, require_complete=False)
        resolved = native is not None and native["resolved"]
        if saw_terminal and not resolved:
            raise ValueError("venue withdrew an observed terminal token state")
        saw_terminal = saw_terminal or resolved
        amount = unit(native["final_price"]) if resolved else None
        items.append(
            {
                "condition_id": native["condition_id"] if native is not None else None,
                "token_id": token,
                "resolution_id": r["sha"],
                "source": reply.url,
                "source_sha256": r["sha"],
                "resolved_at": reply.received_at,
                "status": "resolved" if resolved else "pending",
                "payout": fmt(amount) if amount is not None else None,
                "row": native,
            }
        )
    # Pending rows can be sparse. Every identity value they do give must
    # stay consistent as data arrives. The closed flag legitimately changes before settlement.
    nonempty = [item["row"] for item in items if item["row"] is not None]
    for field in (
        "condition_id",
        "outcome",
        "outcome_index",
        "opposite_token_id",
        "market_slug",
        "event_slug",
    ):
        values = [row[field] for row in nonempty if row.get(field) is not None]
        if values and any(
            type(v) is not type(values[0]) or v != values[0] for v in values[1:]
        ):
            raise ValueError(f"official token changed its {field} identity")
    terminal = [i for i in items if i["status"] == "resolved"]
    if len({number(v["payout"]) for v in terminal}) > 1:
        raise ValueError("conflicting terminal payouts for official token")
    closed = [
        item["row"]["closed"]
        for item in terminal
        if item["row"].get("closed") is not None
    ]
    if closed and any(type(v) is not bool or v != closed[0] for v in closed):
        raise ValueError("conflicting official token closed state")
    return items


def _records_from_db(db, root, token, *, market=None):
    items = resolution_history(db, root, token, as_of=store.now_utc(), market=market)
    terminal = [i for i in items if i["status"] == "resolved"]
    if terminal:
        identity = (
            "token_id",
            "outcome",
            "outcome_index",
            "opposite_token_id",
            "market_slug",
            "event_slug",
            "closed",
        )
        # Older versions returned a sparse status. A fuller native receipt may
        # complete missing identifiers; a later incomplete response cannot qualify.
        if all(terminal[-1]["row"].get(k) is not None for k in identity):
            return next(
                t
                for t in terminal
                if all(t["row"].get(k) is not None for k in identity)
            )
        return terminal[-1]
    return items[-1] if items else None


def token_status(root, token, *, cached_only=False, recheck_terminal=False):
    token = str(token)
    if not token.isdigit():
        raise ValueError("numeric ERC-1155 token required")
    item = _records(root, token)
    if cached_only:
        return item
    refresh = not item or (
        item["status"] == "pending"
        and instant(store.now_utc()) - instant(item["resolved_at"])
        >= timedelta(minutes=15)
    )
    if recheck_terminal and item and item["status"] == "resolved":
        with closing(market_archive.database(root)) as db:
            latest = db.execute(
                "SELECT MAX(received_at) FROM ma_requests WHERE operation='resolution' AND selector=? "
                "AND status='ok' AND received_at<=?",
                (token, store.now_utc()),
            ).fetchone()[0]
        refresh = not latest or instant(store.now_utc()) - instant(latest) >= timedelta(
            days=1
        )
    if refresh:
        ledger.capture_resolution(root, token)
        item = _records(root, token)
    return item


def _positions(root, account, limit, now):
    with ledger.connection(root) as db:
        dbscope.prepare(db, "research-shadow", shadow.SCHEMA)
        dbscope.prepare(db, "settlement", SCHEMA)
        market_archive.prepare(db)
        sql = """
          WITH balances AS (
            SELECT 'formal' mode,i.account,i.id,i.fixture,i.token,i.condition_id,i.status,i.original plan,i.result,i.fingerprint,i.cutoff
              FROM paper_intents i JOIN paper_accounts a ON a.account=i.account
              WHERE a.mode='formal' AND i.status=:formal_state
            UNION ALL
            SELECT 'research_shadow',account,id,fixture,token,condition_id,status,plan,result,plan_sha,cutoff
              FROM research_shadow_orders WHERE status=:shadow_state
          )
          SELECT b.*, json_extract(q.details,'$.opposite_token_id') AS previously_known_opposite,
            (SELECT coalesce(max(rowid),0) FROM ma_requests) AS resolution_request_cursor
          FROM balances b LEFT JOIN settlement_queue q
            ON q.mode=b.mode AND q.account=b.account AND q.intent_id=b.id
          WHERE (:account IS NULL OR b.account=:account) AND b.cutoff<=:mature
        """
        # A newly observed refund, conflicting token payout, or revised league score
        # immediately reopens the audit. Otherwise posted cash is rechecked once a day.
        audit_due = """
          AND (q.checked_at IS NULL OR q.checked_at<=:daily
            OR json_extract(q.details,'$.resolution_request_cursor') IS NULL OR EXISTS (
            SELECT 1 FROM ma_requests r WHERE r.operation='resolution' AND r.selector IN (b.token,json_extract(q.details,'$.opposite_token_id'))
              AND r.status='ok' AND (r.received_at>q.checked_at
                OR r.rowid>json_extract(q.details,'$.resolution_request_cursor')) AND r.received_at<=:now
          ) OR EXISTS (
            SELECT 1 FROM result_versions r WHERE r.fixture_id=b.fixture
              AND r.recorded_at>q.checked_at AND r.recorded_at<=:now
          ))
        """
        order = " ORDER BY COALESCE(q.checked_at,b.cutoff), b.cutoff,b.mode,b.account,b.id LIMIT :limit"
        args = {
            "account": account,
            "mature": iso(now - timedelta(minutes=150)),
            "limit": limit,
            "daily": iso(now - timedelta(days=1)),
            "now": iso(now),
        }
        opened = [
            dict(r)
            for r in db.execute(
                sql + order, args | {"formal_state": "filled", "shadow_state": "held"}
            )
        ]
        posted = [
            dict(r)
            for r in db.execute(
                sql + audit_due + order,
                args | {"formal_state": "settled", "shadow_state": "settled"},
            )
        ]
        if not opened or not posted:
            return opened or posted
        if limit == 1:
            # A one-position budget cannot reserve two lanes. Alternate when both are due.
            previous = db.execute(
                "SELECT json_extract(details,'$.position_was_settled') FROM settlement_queue "
                "WHERE (? IS NULL OR account=?) ORDER BY checked_at DESC,rowid DESC LIMIT 1",
                (account, account),
            ).fetchone()
            return posted if previous is not None and previous[0] == 0 else opened
        # Reserve one slot for the oldest due audit. Unused capacity is shared both ways.
        slots = min(len(opened), limit - 1)
        return opened[:slots] + posted[: limit - slots]


def _held(position):
    mode = position["mode"]
    plan = json.loads(position["plan"])
    out = json.loads(position["result"])
    if digest(plan) != position["fingerprint"]:
        raise ValueError("held plan hash differs")
    intent = plan["intent"]
    c = intent["contract"]
    if (
        position["token"] != c["token_id"]
        or position["condition_id"] != c["condition_id"]
        or position["fixture"] != intent["match_id"]
    ):
        raise ValueError("position differs from its held token or fixture")
    seal = instant(plan["sealed_at"] if mode == "research_shadow" else out["sealed_at"])
    return intent, seal


def _entry_market(db, root, intent, seal, mode):
    c = intent["contract"]
    market = archive_proof.original_market(db, root, c["condition_id"], seal)
    if mode == "research_shadow":
        derived = shadow_market.terms(
            db, root, c["condition_id"], c["token_id"], seal, intent["match_id"]
        )
        if digest(derived) != digest(c):
            raise ValueError("research contract no longer verifies")
    else:
        ledger._attest_contract(db, root, c, seal, instant(intent["kickoff"]))
    opportunity.verified_fixture(root, market["fixture"], intent["match_id"], seal)
    return market


def _outcome_slot(market, token_id):
    tokens = market["tokens"]
    if len(tokens) != 2 or len({t["token_id"] for t in tokens}) != 2:
        raise ValueError("entry market has no unambiguous pair of outcome tokens")
    matching = [i for i, t in enumerate(tokens) if t["token_id"] == token_id]
    if len(matching) != 1:
        raise ValueError("resolution token absent from original contract")
    return matching[0]


def _match_record_identity(market, token_id, native, *, require_complete):
    tokens = market["tokens"]
    index = _outcome_slot(market, token_id)
    expected = {
        "condition_id": market["condition_id"],
        "outcome": tokens[index]["outcome"],
        "outcome_index": index,
        "opposite_token_id": tokens[1 - index]["token_id"],
        "market_slug": market["slug"],
    }
    if str(native.get("token_id")) != token_id:
        raise ValueError("official token identity mismatch")
    for name, value in expected.items():
        actual = native.get(name)
        if actual is None and not require_complete:
            continue
        if actual != value or type(actual) is not type(value):
            raise ValueError(f"official token {name} differs from frozen market")
    event_slug = native.get("event_slug")
    if (event_slug is not None or require_complete) and event_slug not in (
        market["fixture"],
        market["fixture"] + "-more-markets",
    ):
        raise ValueError("official token event_slug differs from frozen market")
    return tokens[1 - index]["token_id"]


def _verify_token(market, official, token_id):
    native = official["row"]
    if (
        official["status"] != "resolved"
        or native.get("resolved") is not True
        or native.get("closed") is not True
    ):
        raise ValueError("official token lacks a closed, resolved state")
    return _match_record_identity(market, token_id, native, require_complete=True)


def require_reviewed_equal_split(contract):
    """Only a reviewed split clause in this outcome's entry-time contract permits 0.5."""
    if not any(v == "0.5" for v in contract["special"].values()):
        raise ValueError("frozen entry rules have no reviewed equal-split payout clause")


def _split_event(root, db, intent, seal, mode, official, opposite):
    """A witnessed exchange redemption records no inference about the football result."""
    c = intent["contract"]
    market = _entry_market(db, root, intent, seal, mode)
    other_id = _verify_token(market, official, c["token_id"])
    if not opposite or opposite["status"] != "resolved":
        raise ValueError("both terminal token payout receipts are required for a split")
    _verify_token(market, opposite, other_id)
    if any(number(r["payout"]) != number("0.5") for r in (official, opposite)):
        raise ValueError("terminal payout vector is not the supported equal split")
    require_reviewed_equal_split(c)
    if min(instant(r["resolved_at"]) for r in (official, opposite)) < seal:
        raise ValueError("terminal exchange receipt predates the entry seal")
    receipts = {r["token_id"]: r for r in (official, opposite)}
    witness = [
        {
            "token_id": t["token_id"],
            "outcome": t["outcome"],
            "outcome_index": i,
            "payout": receipts[t["token_id"]]["payout"],
            "source": receipts[t["token_id"]]["source"],
            "source_sha256": receipts[t["token_id"]]["source_sha256"],
            "received_at": receipts[t["token_id"]]["resolved_at"],
        }
        for i, t in enumerate(market["tokens"])
    ]
    return {k: v for k, v in official.items() if k != "row"} | {
        "scenario": "exchange_split",
        "score": None,
        "regulation_receipt": None,
        "resolved_at": iso(
            max(instant(r["resolved_at"]) for r in (official, opposite))
        ),
        "exchange_observed_at": official["resolved_at"],
        "payout_vector": witness,
        "opposite_token_id": other_id,
        "rule_sha256": c["rule_sha256"],
        "rule_profile": c["rule_profile"],
        "rule_source_sha256": c["rule_source_sha256"],
        "rule_received_at": c["rule_received_at"],
    }


def _account_positions(db, account, condition, token):
    return db.execute(
        "SELECT i.original plan,i.result,i.fingerprint,i.token,i.condition_id,i.fixture, "
        "'formal' mode FROM paper_intents i WHERE account=? AND condition_id=? AND token=? "
        "AND (status IN ('filled','frozen','settled') OR (status='skipped' "
        "AND json_extract(result,'$.reason')='token resolved before execution'))",
        (account, condition, token),
    ).fetchall()


def _binary_history(db, root, market, token, amount):
    """Compare all available native receipts for the two token IDs frozen at entry."""
    index = _outcome_slot(market, token)
    ids = [token, market["tokens"][1 - index]["token_id"]]
    records = [_records_from_db(db, root, ident, market=market) for ident in ids]
    for side, (ident, record) in enumerate(zip(ids, records, strict=True)):
        if not record:
            continue
        row = record["row"]
        if row is None:
            if side == 0:
                raise ValueError("held token resolution has not arrived")
            continue
        slot = index if side == 0 else 1 - index
        # Sparse older binary receipts still bind token, condition and price. Any
        # additional identity the exchange actually published must also agree.
        expected = {
            "condition_id": market["condition_id"],
            "outcome_index": slot,
            "outcome": market["tokens"][slot]["outcome"],
            "opposite_token_id": ids[1 - side],
            "market_slug": market["slug"],
        }
        if (
            str(row["token_id"]) != ident
            or any(row.get(k) is not None and row[k] != v for k, v in expected.items())
            or (
                row.get("outcome_index") is not None
                and type(row["outcome_index"]) is not int
            )
            or row.get("event_slug")
            not in (None, market["fixture"], market["fixture"] + "-more-markets")
        ):
            raise ValueError(
                "official binary token, outcome or market identity mismatch"
            )
        if record["status"] == "resolved":
            if row.get("closed") is not None and row["closed"] is not True:
                raise ValueError("terminal binary token is not closed")
            if number(record["payout"]) != (amount if side == 0 else 1 - amount):
                raise ValueError("conflicting official binary token payout vector")
        elif side == 0 or (
            records[0]
            and instant(record["resolved_at"]) >= instant(records[0]["resolved_at"])
        ):
            raise ValueError(
                "official token status disagrees with a terminal binary payout"
            )
    return ids[1], records


def attest_ledger_payout_history(db, root, account, event):
    """The direct and automatic cash paths share the same locked contradiction check."""
    token, condition = event["token_id"], event["condition_id"]
    for row in _account_positions(db, account, condition, token):
        intent, seal = _held(row)
        market = archive_proof.original_market(db, root, condition, seal)
        _binary_history(db, root, market, token, unit(event["payout"]))


def attest_ledger_split(db, root, account, event):
    """Repeat both native proofs and entry contracts inside the cash transaction."""
    condition, token = event["condition_id"], event["token_id"]
    rows = _account_positions(db, account, condition, token)
    if not rows:
        raise ValueError("missing held position for split payout")
    held = _records_from_db(db, root, token)
    if not held or held["status"] != "resolved":
        raise ValueError("held token has no terminal receipt")
    other = _records_from_db(db, root, str(held["row"].get("opposite_token_id")))
    expected = None
    for row in rows:
        intent, seal = _held(row)
        derived = _split_event(root, db, intent, seal, "formal", held, other)
        fill_at = json.loads(row["result"]).get("filled_at")
        if fill_at and any(
            instant(t["received_at"]) < instant(fill_at)
            for t in derived["payout_vector"]
        ):
            raise ValueError("terminal token predates the held fill")
        if expected is not None and expected != derived:
            raise ValueError("the positions have different sealed settlement terms")
        expected = derived
        if event["scenario"] == "exchange_split":
            agrees = expected == event
        else:
            # Original operators specify the scenario and supply capture_resolution().
            # Derive the canonical exchange-only event afresh, never infer that the
            # football match was canceled from an operator's label.
            agrees = (
                scenario_payout(
                    intent["contract"], event["scenario"], event.get("score")
                )
                == number("0.5")
                and all(
                    event.get(k) == held[k]
                    for k in (
                        "condition_id",
                        "token_id",
                        "resolution_id",
                        "source",
                        "source_sha256",
                        "status",
                    )
                )
                and number(event["payout"]) == number(held["payout"])
                and instant(event["resolved_at"]) == instant(held["resolved_at"])
                and all(
                    v == expected[k]
                    for k, v in event.items()
                    if k in expected and k not in {"scenario", "payout", "resolved_at"}
                )
            )
        if not agrees:
            raise ValueError(
                "split event differs from original exchange vector or entry rules"
            )
    return expected


def _match(root, position, official, score):
    intent, seal = _held(position)
    c = intent["contract"]
    if (
        score.get("fixture_id") != intent["match_id"]
        or score.get("status") != "final"
        or score.get("period") != "regulation"
        or instant(score["kickoff"]) != instant(intent["kickoff"])
        or instant(score["confirmed_at"]) > instant(store.now_utc())
    ):
        raise ValueError(
            "special match status, changed schedule, or unconfirmed regulation result"
        )
    observed = instant(official["resolved_at"])
    if observed < instant(intent["kickoff"]) + timedelta(minutes=90):
        raise ValueError("terminal token observation predates a normal finished match")
    value = unit(official["payout"])
    goals = [score.get("home_goals"), score.get("away_goals")]
    if (
        any(type(x) != int or x < 0 for x in goals)
        or value not in (0, 1)
        or scenario_payout(c, "normal", goals) != value
    ):
        raise ValueError("special payout or conflict with verified regulation score")
    with closing(store.connect(root)) as db:
        db.execute("BEGIN")
        market = _entry_market(db, root, intent, seal, position["mode"])
        opposite, records = _binary_history(db, root, market, c["token_id"], value)
        if not records[0]:
            raise ValueError("held token resolution receipt is unavailable")
        _verify_token(market, official, c["token_id"])
    return {k: v for k, v in official.items() if k != "row"} | {
        "scenario": "normal",
        "opposite_token_id": opposite,
        "score": goals,
        "resolved_at": iso(max(observed, instant(score["confirmed_at"]))),
        "exchange_observed_at": iso(observed),
        "regulation_receipt": score.get("proof"),
        "regulation_source": score.get("source"),
    }


def _credit_shadow(root, pos, event):
    with shadow.db_for(root) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT status,result,plan,plan_sha AS fingerprint,token,condition_id,fixture, "
            "'research_shadow' AS mode FROM research_shadow_orders WHERE account=? AND id=?",
            (pos["account"], pos["id"]),
        ).fetchone()
        scenario = event.get("scenario", "normal")
        if number(event["payout"]) not in (0, 1) and scenario != "exchange_split":
            raise ValueError("non-binary payout requires both original token receipts")
        if scenario == "exchange_split":
            intent, seal = _held(row)
            held = _records_from_db(db, root, row["token"])
            other = _records_from_db(db, root, event["opposite_token_id"])
            if (
                not held
                or _split_event(root, db, intent, seal, "research_shadow", held, other)
                != event
            ):
                raise ValueError("split evidence changed before the shadow settlement")
        elif scenario == "normal":
            intent, seal = _held(row)
            market = archive_proof.original_market(db, root, row["condition_id"], seal)
            _, records = _binary_history(
                db, root, market, row["token"], unit(event["payout"])
            )
            if event.get("status") == "resolved" and not records[0]:
                raise ValueError("held token resolution receipt is unavailable")
        out = json.loads(row["result"])
        if scenario == "exchange_split" and any(
            instant(t["received_at"]) < instant(out["filled_at"])
            for t in event["payout_vector"]
        ):
            raise ValueError("terminal token predates the held fill")
        if row["status"] == "settled":
            if (
                number(out["payout"]) != number(event["payout"])
                or out["score"] != event["score"]
                or out.get("resolution_scenario", "normal") != scenario
            ):
                raise ValueError(
                    "posted shadow settlement conflicts; preserve booked cash for review"
                )
            return
        if row["status"] != "held":
            raise ValueError("no shadow position held to resolution")
        proceeds = (number(out["net_shares"]) * number(event["payout"])).quantize(
            UNIT, rounding=ROUND_DOWN
        )
        cash = number(
            db.execute(
                "SELECT cash FROM research_shadow_accounts WHERE account=?",
                (pos["account"],),
            ).fetchone()[0]
        )
        out.update(
            status="settled",
            payout=event["payout"],
            proceeds=fmt(proceeds),
            pnl=fmt(proceeds - number(out["spent"])),
            score=event["score"],
            resolved_at=event["resolved_at"],
            resolution_sha256=event["source_sha256"],
            regulation_receipt=event["regulation_receipt"],
        )
        out["resolution_scenario"] = scenario
        if scenario == "exchange_split":
            out["settlement_evidence"] = event
        db.execute(
            "UPDATE research_shadow_accounts SET cash=? WHERE account=?",
            (fmt(cash + proceeds), pos["account"]),
        )
        db.execute(
            "UPDATE research_shadow_orders SET status='settled',result=? WHERE account=? AND id=?",
            (packed(out), pos["account"], pos["id"]),
        )


def _record(root, p, state, why, detail):
    checked = store.now_utc()
    cursor = p["resolution_request_cursor"]
    detail = {"resolution_request_cursor": cursor, **detail}
    if p["previously_known_opposite"]:
        detail.setdefault("opposite_token_id", p["previously_known_opposite"])
    with closing(store.connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        # Older booked rows have no stored counter-token. Recover the watch even
        # when this audit stopped at a missing result or contradictory held-token receipt.
        if p["status"] == "settled" and not detail.get("opposite_token_id"):
            try:
                _, seal = _held(p)
                market = archive_proof.original_market(
                    db, root, p["condition_id"], seal
                )
                slot = _outcome_slot(market, p["token"])
                detail["opposite_token_id"] = market["tokens"][1 - slot]["token_id"]
            except (OSError, KeyError, TypeError, ValueError) as exc:
                state, why = "manual_review", str(exc)
        other = detail.get("opposite_token_id")
        if (
            state == "settled"
            and other
            and "payout" in detail
            and db.execute(
                "SELECT 1 FROM ma_requests WHERE rowid>? AND operation='resolution' AND selector IN (?,?) "
                "AND status='ok' AND received_at<=? LIMIT 1",
                (cursor, p["token"], other, checked),
            ).fetchone()
        ):
            try:
                intent, seal = _held(p)
                market = archive_proof.original_market(
                    db, root, p["condition_id"], seal
                )
                _binary_history(db, root, market, p["token"], unit(detail["payout"]))
            except (OSError, ValueError, KeyError, TypeError) as ex:
                state, why = "manual_review", str(ex)
            detail["resolution_request_cursor"] = db.execute(
                "SELECT coalesce(max(rowid),0) FROM ma_requests"
            ).fetchone()[0]
        db.execute(
            "INSERT INTO settlement_queue VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(mode,account,intent_id) DO UPDATE SET "
            "state=excluded.state,reason=excluded.reason,checked_at=excluded.checked_at,details=excluded.details "
            "WHERE coalesce(json_extract(settlement_queue.details,'$.resolution_request_cursor'),-1) "
            "<=json_extract(excluded.details,'$.resolution_request_cursor')",
            (
                p["mode"],
                p["account"],
                p["id"],
                p["fixture"],
                p["token"],
                state,
                why,
                checked,
                packed(detail),
            ),
        )
        saved = db.execute(
            "SELECT state,reason,details FROM settlement_queue WHERE mode=? AND account=? AND intent_id=?",
            (p["mode"], p["account"], p["id"]),
        ).fetchone()
        state, why, detail = (
            saved["state"],
            saved["reason"],
            json.loads(saved["details"]),
        )
    return {
        "mode": p["mode"],
        "account": p["account"],
        "fixture": p["fixture"],
        "token": p["token"],
        "intent": p["id"],
        "state": state,
        "reason": why,
        "detail": detail,
    }


def sync(data_dir, *, account=None, limit=50, cached_only=False):
    if type(limit) != int or not 1 <= limit <= 1000:
        raise ValueError("limit must be 1..1000 positions")
    rows = _positions(data_dir, account, limit, instant(store.now_utc()))
    updates = []
    audit_tokens = {p["token"] for p in rows if p["status"] == "settled"}
    cache = {}
    scores = {}

    def lookup(token, audit=False):
        key = (token, audit or token in audit_tokens)
        if key not in cache:
            try:
                cache[key] = token_status(
                    data_dir, token, cached_only=cached_only, recheck_terminal=key[1]
                )
            except (OSError, ValueError, KeyError, TypeError) as exc:
                cache[key] = exc
        item = cache[key]
        if isinstance(item, Exception):
            raise item
        return item

    for p in rows:
        state, why = "pending", None
        detail = {}
        try:
            fixture = p["fixture"]
            official = lookup(p["token"])
            event = None
            already = p["status"] == "settled"
            if (
                official
                and official["row"] is not None
                and official["condition_id"] != p["condition_id"]
            ):
                raise ValueError("official token condition differs from held market")
            if not official or official["status"] != "resolved":
                why = "waiting for official token payout"
            elif number(official["payout"]) not in (0, 1):
                if number(official["payout"]) != number("0.5"):
                    raise ValueError("unsupported non-binary exchange payout")
                intent, seal = _held(p)
                with closing(store.connect(data_dir)) as db:
                    market = _entry_market(db, data_dir, intent, seal, p["mode"])
                    other = _verify_token(market, official, p["token"])
                detail.update(
                    opposite_token_id=other, resolution_sha256=official["source_sha256"]
                )
                opposite = lookup(other, audit=already)
                with closing(store.connect(data_dir)) as db:
                    event = _split_event(
                        data_dir, db, intent, seal, p["mode"], official, opposite
                    )
            else:
                if fixture not in scores:
                    scores[fixture] = _regulation(data_dir, fixture)
                if scores[fixture]:
                    event = _match(data_dir, p, official, scores[fixture])
                else:
                    state = "manual_review"
                    why = "terminal token has no independently verified normal regulation score"
            if event:
                detail["opposite_token_id"] = event["opposite_token_id"]
                if p["mode"] == "research_shadow":
                    _credit_shadow(data_dir, p, event)
                elif already:
                    old = json.loads(p["result"])
                    with closing(store.connect(data_dir)) as db:
                        posted = db.execute(
                            "SELECT payload FROM paper_resolutions WHERE account=? AND condition_id=? AND token=? AND terminal=1",
                            (p["account"], p["condition_id"], p["token"]),
                        ).fetchone()
                    if (
                        not posted
                        or old.get("resolution_scenario") != event["scenario"]
                        or json.loads(posted[0]).get("score") != event["score"]
                        or number(old["payout"]) != number(event["payout"])
                    ):
                        raise ValueError(
                            "posted official settlement differs from confirmed score, scenario, or payout"
                        )
                else:
                    ledger.settle(data_dir, p["account"], event)
                state = "settled"
                detail.update(
                    payout=event["payout"],
                    score=event["score"],
                    scenario=event["scenario"],
                    resolution_sha256=event["source_sha256"],
                    regulation_proof=event["regulation_receipt"],
                    already_posted=already,
                )
                if event["scenario"] == "exchange_split":
                    detail.update(
                        payout_vector=event["payout_vector"],
                        rule_sha256=event["rule_sha256"],
                        rule_profile=event["rule_profile"],
                    )
        except (OSError, TimeoutError) as exc:
            state = "pending"
            why = str(exc)
        except (KeyError, TypeError, ValueError) as exc:
            state = "manual_review"
            why = str(exc)
        detail["position_was_settled"] = p["status"] == "settled"
        updates.append(_record(data_dir, p, state, why, detail))
    result = {
        "paper_only": True,
        "examined": len(rows),
        "states": dict(Counter(i["state"] for i in updates)),
        "positions": updates,
    }
    store.save_report(data_dir, "settlement", account or "all", result)
    return result


def queue(data_dir, *, account=None):
    with closing(store.connect(data_dir)) as db:
        dbscope.prepare(db, "settlement", SCHEMA)
        return {
            "rows": [
                dict(r) | {"details": json.loads(r["details"])}
                for r in db.execute(
                    "SELECT * FROM settlement_queue "
                    "WHERE (? IS NULL OR account=?) AND state!='settled' ORDER BY checked_at DESC",
                    (account, account),
                )
            ]
        }


def register(subparsers):
    sub = subparsers.add_parser(
        "settlement", help="核对正式和研究持仓的交易所兑付与90分钟赛果"
    ).add_subparsers(required=True)
    x = sub.add_parser("sync")
    x.add_argument("--account")
    x.add_argument("--limit", type=int, default=50)
    x.add_argument("--cached-only", action="store_true")
    x.set_defaults(
        func=lambda a: sync(
            a.data_dir, account=a.account, limit=a.limit, cached_only=a.cached_only
        )
    )
    x = sub.add_parser("queue")
    x.add_argument("--account")
    x.set_defaults(func=lambda a: queue(a.data_dir, account=a.account))
    x = sub.add_parser("token")
    x.add_argument("token")
    x.add_argument("--cached-only", action="store_true")
    x.set_defaults(
        func=lambda a: token_status(a.data_dir, a.token, cached_only=a.cached_only)
    )
