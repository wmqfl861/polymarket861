"""Reconcile a held token against native on-chain status and an independent regulation result."""

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
        rows = db.execute(
            "SELECT sha,received_at FROM ma_requests WHERE operation='resolution' AND selector=? AND status='ok' "
            "AND received_at<=? ORDER BY received_at",
            (token, store.now_utc()),
        ).fetchall()
        items = []
        for r in rows:
            reply, _, _, _ = archive_proof.original_receipt(
                db, root, r["sha"], r["received_at"], {"resolution"}
            )
            body = json.loads(reply.body)
            res = [v for v in body.get("data", []) if str(v.get("token_id")) == token]
            if not res:
                continue
            if (
                len(res) != 1
                or type(res[0].get("resolved")) != bool
                or (res[0]["resolved"] != (res[0].get("final_price") is not None))
            ):
                raise ValueError("unrecognized official token resolution")
            native = res[0]
            amount = unit(native["final_price"]) if native["resolved"] else None
            items.append(
                {
                    "condition_id": native["condition_id"],
                    "token_id": token,
                    "resolution_id": r["sha"],
                    "source": reply.url,
                    "source_sha256": r["sha"],
                    "resolved_at": reply.received_at,
                    "status": "resolved" if native["resolved"] else "pending",
                    "payout": fmt(amount) if amount is not None else None,
                    "row": native,
                }
            )
    terminal = [i for i in items if i["status"] == "resolved"]
    if terminal:
        if (
            items[-1]["status"] != "resolved"
            or len({(v["condition_id"], number(v["payout"])) for v in terminal}) != 1
        ):
            raise ValueError("conflicting official token payouts")
        return terminal[0]
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
          SELECT b.* FROM balances b LEFT JOIN settlement_queue q
            ON q.mode=b.mode AND q.account=b.account AND q.intent_id=b.id
          WHERE (:account IS NULL OR b.account=:account) AND b.cutoff<=:mature
        """
        # A newly observed refund, conflicting token payout, or revised league score
        # immediately reopens the audit. Otherwise posted cash is rechecked once a day.
        audit_due = """
          AND (q.checked_at IS NULL OR q.checked_at<=:daily OR EXISTS (
            SELECT 1 FROM ma_requests r WHERE r.operation='resolution' AND r.selector=b.token
              AND r.status='ok' AND r.received_at>q.checked_at AND r.received_at<=:now
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


def _match(root, position, official, score):
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
    seal = instant(plan["sealed_at"] if mode == "research_shadow" else out["sealed_at"])
    with closing(store.connect(root)) as db:
        market = archive_proof.original_market(db, root, c["condition_id"], seal)
        if mode == "research_shadow":
            if digest(
                shadow_market.terms(
                    db, root, c["condition_id"], c["token_id"], seal, intent["match_id"]
                )
            ) != digest(c):
                raise ValueError("research contract no longer verifies")
        else:
            ledger._attest_contract(db, root, c, seal, instant(intent["kickoff"]))
    native = official["row"]
    tok = [
        (i, x) for i, x in enumerate(market["tokens"]) if x["token_id"] == c["token_id"]
    ]
    if (
        len(tok) != 1
        or native.get("condition_id") != c["condition_id"]
        or native.get("market_slug") != market["slug"]
        or native.get("event_slug")
        not in (market["fixture"], market["fixture"] + "-more-markets")
        or native.get("closed") is not True
        or native.get("outcome") != tok[0][1]["outcome"]
        or native.get("outcome_index") != tok[0][0]
        or native.get("opposite_token_id")
        != market["tokens"][1 - tok[0][0]]["token_id"]
    ):
        raise ValueError("official token, outcome, fixture or market identity mismatch")
    opportunity.verified_fixture(root, market["fixture"], intent["match_id"], seal)
    return {k: v for k, v in official.items() if k != "row"} | {
        "scenario": "normal",
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
            "SELECT status,result FROM research_shadow_orders WHERE account=? AND id=?",
            (pos["account"], pos["id"]),
        ).fetchone()
        out = json.loads(row["result"])
        if row["status"] == "settled":
            if (
                number(out["payout"]) != number(event["payout"])
                or out["score"] != event["score"]
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
    with closing(store.connect(root)) as db, db:
        db.execute(
            "INSERT INTO settlement_queue VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(mode,account,intent_id) DO UPDATE SET "
            "state=excluded.state,reason=excluded.reason,checked_at=excluded.checked_at,details=excluded.details",
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
    for p in rows:
        state = "pending"
        why = None
        detail = {}
        try:
            token = p["token"]
            fixture = p["fixture"]
            if token not in cache:
                try:
                    cache[token] = token_status(
                        data_dir,
                        token,
                        cached_only=cached_only,
                        recheck_terminal=token in audit_tokens,
                    )
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    cache[token] = exc
            official = cache[token]
            if isinstance(official, Exception):
                raise official
            if not official or official["status"] != "resolved":
                why = "waiting for official token payout"
            elif number(official["payout"]) not in (0, 1):
                state = "manual_review"
                why = "non-binary payout requires special-contract review"
                detail = {"resolution_sha256": official["source_sha256"]}
            else:
                if fixture not in scores:
                    scores[fixture] = _regulation(data_dir, fixture)
                score = scores[fixture]
                if not score:
                    state = "manual_review"
                    why = "terminal token has no independently verified normal regulation score"
                else:
                    event = _match(data_dir, p, official, score)
                    already = p["status"] == "settled"
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
                            or old.get("resolution_scenario") != "normal"
                            or json.loads(posted[0]).get("score") != event["score"]
                            or number(old["payout"]) != number(event["payout"])
                        ):
                            raise ValueError(
                                "posted official settlement differs from confirmed score, scenario, or payout"
                            )
                    else:
                        ledger.settle(data_dir, p["account"], event)
                    state = "settled"
                    detail = {
                        "payout": event["payout"],
                        "score": event["score"],
                        "resolution_sha256": event["source_sha256"],
                        "regulation_proof": event["regulation_receipt"],
                        "already_posted": already,
                    }
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
