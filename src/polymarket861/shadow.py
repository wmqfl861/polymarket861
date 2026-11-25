"""Isolated research cash accounts. No formal order, A-grade replay or promotion reads these tables."""

import json
from collections import Counter
from contextlib import closing, contextmanager
from datetime import timedelta

from . import market_archive, opportunity, shadow_market, store
from .ledger_rules import (
    MAX_GAP,
    MAX_MATCH,
    MAX_ORDER,
    ONE,
    ZERO,
    check_book,
    check_contract,
    digest,
    fair_values,
    fit,
    fmt,
    instant,
    iso,
    number,
    packed,
    pair_depth,
)

INITIAL = number("10000")
SCHEMA = """
CREATE TABLE IF NOT EXISTS research_shadow_accounts(account TEXT PRIMARY KEY,initial TEXT NOT NULL,cash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_shadow_orders(
 account TEXT NOT NULL REFERENCES research_shadow_accounts, id TEXT NOT NULL, fixture TEXT NOT NULL,slug TEXT NOT NULL,
 cutoff TEXT NOT NULL,token TEXT NOT NULL,condition_id TEXT NOT NULL,status TEXT NOT NULL,plan_sha TEXT NOT NULL,plan TEXT NOT NULL,result TEXT NOT NULL,
 PRIMARY KEY(account,id),UNIQUE(account,fixture,cutoff,condition_id,token));
CREATE INDEX IF NOT EXISTS research_shadow_pending ON research_shadow_orders(account,slug,status);
"""


@contextmanager
def db_for(root):
    with closing(store.connect(root)) as db:
        db.executescript(SCHEMA)
        with db:
            yield db


def _budget(db, account, fixture):
    cash = number(
        db.execute(
            "SELECT cash FROM research_shadow_accounts WHERE account=?", (account,)
        ).fetchone()[0]
    )
    reserved = used = ZERO
    for r in db.execute(
        "SELECT fixture,status,result FROM research_shadow_orders WHERE account=? AND status IN ('frozen','held','settled')",
        (account,),
    ):
        v = json.loads(r["result"])
        if r["status"] == "frozen":
            reserved += number(v["budget"])
        if r["fixture"] == fixture:
            used += number(v["budget"] if r["status"] == "frozen" else v["spent"])
    return max(ZERO, min(MAX_ORDER, cash - reserved, MAX_MATCH - used))


def _freeze(root, db, account, fixture, seal, contract, book, clock):
    c = contract
    cutoff = instant(fixture["kickoff"]) - timedelta(hours=1)
    ident = digest(
        [
            account,
            fixture["fixture_id"],
            fixture["kickoff"],
            c["condition_id"],
            c["token_id"],
        ]
    )
    existing = db.execute(
        "SELECT status FROM research_shadow_orders WHERE account=? AND id=?",
        (account, ident),
    ).fetchone()
    if existing:
        return {
            "id": ident,
            "token_id": c["token_id"],
            "state": existing[0],
            "repeated": True,
        }
    when = instant(store.now_utc())
    if not cutoff - timedelta(seconds=30) <= when <= cutoff:
        raise ValueError("missed research decision deadline")
    if when != clock:
        c = shadow_market.terms(
            db, root, c["condition_id"], c["token_id"], when, fixture["fixture_id"]
        )
        book = shadow_market.book(
            db, root, c, cutoff, instant(fixture["kickoff"]), "first", seen_by=when
        )
        if book is None:
            raise ValueError("no admissible first book at the actual research decision")
    intent = opportunity._intent(fixture, c, seal, book, cutoff)
    check_contract(c, intent, cutoff)
    fair, *_ = fair_values(intent["normal_fv"], c)
    quote = check_book(book, c, cutoff, instant(fixture["kickoff"]), "first")
    budget = _budget(db, account, fixture["fixture_id"])
    depth = pair_depth(quote["asks"], quote["asks"], number(intent["limit_price"]))
    candidate = fit(depth, number(intent["quantity"]), c["fee_schedule"], budget, fair)
    reason = None
    if abs(fair - quote["mid"]) > MAX_GAP:
        reason = "model_market_gap"
    elif candidate["gross"] < quote["minimum"] or candidate["net"] <= 0:
        reason = "insufficient_edge_depth_cash_or_fixture_budget"
    status = "skipped" if reason else "frozen"
    plan = {
        "intent": intent,
        "first_book": book,
        "sealed_at": iso(when),
        "book_observed_by": iso(when),
        "forecast_mode": seal["mode"],
        "lineups": seal["lineup_stratum"],
        "fixture": {
            "home": fixture["home"],
            "away": fixture["away"],
            "source_sha256": fixture["source_sha256"],
        },
        "fee_basis": "research_estimate"
        if c["fee_schedule"].get("research_assumption")
        else "verified",
        "pregame_basis": book["research_pregame_basis"],
        "fair_low": fmt(fair),
        "quantity": fmt(candidate["gross"]),
        "budget": fmt(budget),
    }
    result = {"status": status, "reason": reason, "budget": fmt(budget)}
    db.execute(
        "INSERT INTO research_shadow_orders VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            account,
            ident,
            fixture["fixture_id"],
            fixture["market_slug"],
            iso(cutoff),
            c["token_id"],
            c["condition_id"],
            status,
            digest(plan),
            packed(plan),
            packed(result),
        ),
    )
    return {"id": ident, "token_id": c["token_id"], "state": status, "reason": reason}


def _advance(root, db, row, clock):
    plan, res = json.loads(row["plan"]), json.loads(row["result"])
    if digest(plan) != row["plan_sha"]:
        raise ValueError("research decision hash differs")
    intent = plan["intent"]
    c = intent["contract"]
    kick = instant(intent["kickoff"])
    cutoff = instant(row["cutoff"])
    status = row["status"]
    if status == "frozen":
        target = cutoff + timedelta(seconds=max(5, int(c["seconds_delay"])))
        if clock < target:
            return None
        second = shadow_market.book(db, root, c, target, kick, "second", seen_by=clock)
        if not second and clock < target + timedelta(seconds=5):
            return None
        try:
            if second is None:
                raise ValueError("no original delayed two-sided book")
            fx = opportunity.verified_fixture(
                root, row["slug"], row["fixture"], second["received_at"]
            )
            if fx["kickoff"] != iso(kick) or any(
                fx[s] != plan["fixture"][s] for s in ("home", "away")
            ):
                raise ValueError("fixture changed before execution")
            first = shadow_market.book(
                db, root, c, cutoff, kick, "first", seen_by=plan["book_observed_by"]
            )
            if digest(first) != digest(plan["first_book"]):
                raise ValueError("original research first book no longer verifies")
            q1 = check_book(first, c, cutoff, kick, "first")
            q2 = check_book(second, c, target, kick, "second")
            depth = pair_depth(q1["asks"], q2["asks"], number(intent["limit_price"]))
            cash = number(
                db.execute(
                    "SELECT cash FROM research_shadow_accounts WHERE account=?",
                    (row["account"],),
                ).fetchone()[0]
            )
            fill = fit(
                depth,
                number(plan["quantity"]),
                c["fee_schedule"],
                min(cash, number(plan["budget"])),
                number(plan["fair_low"]),
            )
            if fill["gross"] < max(q1["minimum"], q2["minimum"]) or fill["net"] <= 0:
                raise ValueError("no eligible delayed fill")
            db.execute(
                "UPDATE research_shadow_accounts SET cash=? WHERE account=?",
                (fmt(cash - fill["spent"]), row["account"]),
            )
            res.update(
                status="held",
                spent=fmt(fill["spent"]),
                net_shares=fmt(fill["net"]),
                gross_shares=fmt(fill["gross"]),
                entry_cost_per_net_share=fmt(fill["cost"]),
                decimal_odds=fmt(ONE / fill["cost"]),
                fee_cash_equivalent=fmt(fill["fees"]),
                filled_at=second["received_at"],
                second_book=second["source_sha256"],
                execution_pregame_basis=second["research_pregame_basis"],
                levels=fill["levels"],
            )
        except (ValueError, TypeError, KeyError, OSError) as exc:
            res.update(status="skipped", reason=str(exc))
    if (
        res["status"] in ("held", "settled")
        and clock >= kick
        and not res.get("scheduled_close_sha256")
    ):
        mark = shadow_market.book(db, root, c, kick, kick, "close", seen_by=kick)
        if mark is None:
            if res.get("close_status") == "missing" and status == res["status"]:
                return None
            res["close_status"] = "missing"
        else:
            mid = check_book(mark, c, kick, kick, "close")["mid"]
            res.update(
                close_status="scheduled_kickoff",
                scheduled_close_sha256=mark["source_sha256"],
                scheduled_close_at=mark["received_at"],
                scheduled_close_mid=fmt(mid),
                scheduled_close_cost_difference=fmt(
                    mid - number(res["entry_cost_per_net_share"])
                ),
                close_pregame_basis=mark["research_pregame_basis"],
            )
    if res == json.loads(row["result"]):
        return None
    db.execute(
        "UPDATE research_shadow_orders SET status=?,result=? WHERE account=? AND id=?",
        (res["status"], packed(res), row["account"], row["id"]),
    )
    return {
        "id": row["id"],
        "token_id": row["token"],
        "state": res["status"],
        "reason": res.get("reason"),
        "close_status": res.get("close_status"),
    }


def run(data_dir, slug, *, account="research-shadow", fixture_id=None):
    if not isinstance(account, str) or not account.strip():
        raise ValueError("research account name required")
    clock = instant(store.now_utc())
    entries = []
    gaps = []
    try:
        fixture = opportunity._locate(data_dir, slug, fixture_id, clock)
        cut = instant(fixture["kickoff"]) - timedelta(hours=1)
        if cut - timedelta(seconds=30) <= clock <= cut:
            seal = shadow_market.forecast(data_dir, fixture, cut, clock)
            markets = market_archive.contracts(data_dir, slug, as_of=iso(clock))
            groups = {}
            for m in markets:
                if not m["mapping_error"]:
                    for token in m["tokens"]:
                        groups.setdefault(
                            (m["condition_id"], token["token_id"]), []
                        ).append(m)
            with closing(market_archive.database(data_dir)) as reader:
                for (cond, tok), values in sorted(groups.items()):
                    try:
                        opportunity._unique_market(values)
                        for m in values:
                            opportunity._check_child(data_dir, fixture, m, clock)
                        c = shadow_market.terms(
                            reader, data_dir, cond, tok, clock, fixture["fixture_id"]
                        )
                        book = shadow_market.book(
                            reader,
                            data_dir,
                            c,
                            cut,
                            instant(fixture["kickoff"]),
                            "first",
                            seen_by=clock,
                        )
                        if not book:
                            raise ValueError("missing original research first book")
                        entries.append((fixture, seal, c, book))
                    except (ValueError, TypeError, KeyError, OSError) as exc:
                        gaps.append({"token": tok, "reason": str(exc)})
    except (ValueError, TypeError, KeyError, OSError) as exc:
        gaps.append({"reason": str(exc)})
    actions = []
    with db_for(data_dir) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO research_shadow_accounts VALUES(?,?,?)",
            (account, fmt(INITIAL), fmt(INITIAL)),
        )
        for f, s, c, b in entries:
            try:
                actions.append(_freeze(data_dir, db, account, f, s, c, b, clock))
            except (ValueError, TypeError, KeyError, OSError) as exc:
                gaps.append({"token": c["token_id"], "reason": str(exc)})
        for row in db.execute(
            "SELECT * FROM research_shadow_orders WHERE account=? AND slug=? AND status IN ('frozen','held','settled') ORDER BY cutoff,id",
            (account, slug),
        ).fetchall():
            try:
                item = _advance(data_dir, db, row, clock)
                if item:
                    actions.append(item)
            except (ValueError, TypeError, KeyError, OSError) as exc:
                gaps.append({"token": row["token"], "reason": str(exc)})
    return {
        "account": account,
        "mode": "research_shadow",
        "fixture": slug,
        "actions": actions,
        "gaps": gaps,
        "eligible_for_formal": False,
        "eligible_for_A": False,
    }


def report(data_dir, account="research-shadow"):
    with db_for(data_dir) as db:
        acc = db.execute(
            "SELECT * FROM research_shadow_accounts WHERE account=?", (account,)
        ).fetchone()
        if not acc:
            raise ValueError("unknown research shadow account")
        orders = []
        classes = Counter()
        for r in db.execute(
            "SELECT * FROM research_shadow_orders WHERE account=? ORDER BY cutoff,id",
            (account,),
        ):
            p, out = json.loads(r["plan"]), json.loads(r["result"])
            i = p["intent"]
            c = i["contract"]
            if digest(p) != r["plan_sha"]:
                raise ValueError("altered research decision")
            classes[r["status"]] += 1
            orders.append(
                {
                    "id": r["id"],
                    "fixture_id": r["fixture"],
                    "condition_id": r["condition_id"],
                    "token_id": r["token"],
                    "family": c["family"],
                    "team": c.get("team"),
                    "selection": c.get("selection"),
                    "side": c["side"],
                    "line": c.get("line"),
                    "lineups": p["lineups"],
                    "forecast": i["forecast_digest"],
                    "forecast_mode": p["forecast_mode"],
                    "fee_basis": p["fee_basis"],
                    "fee_schedule": c["fee_schedule"],
                    "pregame_basis": p["pregame_basis"],
                    "normal_fair_value": i["normal_fv"],
                    "plan_sha256": r["plan_sha"],
                    **out,
                }
            )
        closed = [
            i for i in orders if i.get("scheduled_close_cost_difference") is not None
        ]
        settled = [i for i in orders if i["status"] == "settled"]
        spent = sum((number(i["spent"]) for i in settled), ZERO)
        pnl = sum((number(i["proceeds"]) - number(i["spent"]) for i in settled), ZERO)
        groups = {}
        for i in orders:
            category = (
                i["lineups"]["home"],
                i["lineups"]["away"],
                i["pregame_basis"],
                i["fee_basis"],
            )
            g = groups.setdefault(
                category,
                {
                    "lineup_home_confirmed": category[0],
                    "lineup_away_confirmed": category[1],
                    "pregame_basis": category[2],
                    "fee_basis": category[3],
                    "orders": 0,
                    "filled": 0,
                    "settled": 0,
                    "scheduled_marks": 0,
                },
            )
            g["orders"] += 1
            g["filled"] += int(i["status"] in ("held", "settled"))
            g["settled"] += int(i["status"] == "settled")
            g["scheduled_marks"] += int(
                i.get("scheduled_close_cost_difference") is not None
            )
        output = {
            "account": account,
            "mode": "research_shadow",
            "eligible_for_formal": False,
            "eligible_for_A": False,
            "initial_cash": acc["initial"],
            "cash": acc["cash"],
            "counts": dict(classes),
            "orders": orders,
            "strata": list(groups.values()),
            "close_basis": "scheduled kickoff; last eligible held-token bid/ask midpoint minus all-in cost per net share; positive is favorable",
            "unit": "collateral units per net held token share",
            "scheduled_close_coverage": [
                len(closed),
                sum(i["status"] in ("held", "settled") for i in orders),
            ],
            "mean_scheduled_close_difference": fmt(
                sum(
                    (number(i["scheduled_close_cost_difference"]) for i in closed), ZERO
                )
                / len(closed)
            )
            if closed
            else None,
            "settled_spend": fmt(spent),
            "realized_pnl": fmt(pnl) if settled else None,
            "settled_roi": fmt(pnl / spent) if spent else None,
        }
    store.save_report(data_dir, "shadow", account, output)
    return output


def register(subparsers):
    sub = subparsers.add_parser(
        "shadow", help="独立研究影子账户、计划开球收盘和官方兑付"
    ).add_subparsers(required=True)
    r = sub.add_parser("run")
    r.add_argument("slug")
    r.add_argument("--fixture-id")
    r.add_argument("--account", default="research-shadow")
    r.set_defaults(
        func=lambda a: run(
            a.data_dir, a.slug, account=a.account, fixture_id=a.fixture_id
        )
    )
    r = sub.add_parser("report")
    r.add_argument("--account", default="research-shadow")
    r.set_defaults(func=lambda a: report(a.data_dir, a.account))
