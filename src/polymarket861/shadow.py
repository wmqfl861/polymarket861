"""Isolated research cash accounts. No formal order, A-grade replay or promotion reads these tables."""

import json
from collections import Counter
from contextlib import closing, contextmanager
from datetime import timedelta

from . import (
    dbscope,
    jobs_research,
    ledger,
    market_archive,
    opportunity,
    shadow_market,
    store,
)
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
LEGACY_PROTOCOL = "research-shadow-0.2.0-budget"
ORDER_PROTOCOL = "research-shadow-shared-risk-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS research_shadow_accounts(account TEXT PRIMARY KEY,initial TEXT NOT NULL,cash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_shadow_orders(
 account TEXT NOT NULL REFERENCES research_shadow_accounts, id TEXT NOT NULL, fixture TEXT NOT NULL,slug TEXT NOT NULL,
 cutoff TEXT NOT NULL,token TEXT NOT NULL,condition_id TEXT NOT NULL,status TEXT NOT NULL,plan_sha TEXT NOT NULL,plan TEXT NOT NULL,result TEXT NOT NULL,
 PRIMARY KEY(account,id),UNIQUE(account,fixture,cutoff,condition_id,token));
CREATE INDEX IF NOT EXISTS research_shadow_pending ON research_shadow_orders(account,slug,status);
CREATE INDEX IF NOT EXISTS research_shadow_fixture_risk ON research_shadow_orders(account,fixture,status);
CREATE INDEX IF NOT EXISTS research_shadow_frozen ON research_shadow_orders(account,cutoff) WHERE status='frozen';
CREATE TABLE IF NOT EXISTS research_shadow_depth(
 account TEXT NOT NULL,condition_id TEXT NOT NULL,token TEXT NOT NULL,snapshot TEXT NOT NULL,
 price TEXT NOT NULL,consumed TEXT NOT NULL,PRIMARY KEY(account,condition_id,token,snapshot,price));
"""


@contextmanager
def db_for(root):
    with closing(store.connect(root)) as db:
        dbscope.prepare(db, "research-shadow", SCHEMA)
        with db:
            yield db


def _cash(db, account):
    return number(
        db.execute(
            "SELECT cash FROM research_shadow_accounts WHERE account=?", (account,)
        ).fetchone()[0]
    )


def _legacy_available(db, account, fixture, cash):
    reserved = sum(
        (
            number(r[0])
            for r in db.execute(
                "SELECT json_extract(result,'$.budget') FROM research_shadow_orders WHERE account=? AND status='frozen'",
                (account,),
            )
        ),
        ZERO,
    )
    used = sum(
        (
            number(r[0])
            for r in db.execute(
                "SELECT CASE status WHEN 'frozen' THEN json_extract(result,'$.budget') ELSE json_extract(result,'$.spent') END "
                "FROM research_shadow_orders WHERE account=? AND fixture=? AND status IN ('held','settled','frozen')",
                (account, fixture),
            )
        ),
        ZERO,
    )
    return max(ZERO, min(cash - reserved, MAX_MATCH - used))


def _freeze(db, account, fixture, seal, c, book, clock, budget, protocol):
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
    intent = opportunity._intent(fixture, c, seal, book, cutoff)
    check_contract(c, intent, cutoff)
    fair, *_ = fair_values(intent["normal_fv"], c)
    quote = check_book(book, c, cutoff, instant(fixture["kickoff"]), "first")
    depth = pair_depth(quote["asks"], quote["asks"], number(intent["limit_price"]))
    candidate = fit(depth, number(intent["quantity"]), c["fee_schedule"], budget, fair)
    reason = None
    if abs(fair - quote["mid"]) > MAX_GAP:
        reason = "model_market_gap"
    elif candidate["gross"] < quote["minimum"] or candidate["net"] <= 0:
        reason = (
            "insufficient_edge_depth_cash_or_fixture_budget"
            if protocol == LEGACY_PROTOCOL
            else "insufficient_edge_depth_cash_or_minimum"
        )
    status = "skipped" if reason else "frozen"
    plan = {
        "intent": intent,
        "first_book": book,
        "order_protocol": protocol,
        "season": seal["season"],
        "round_id": seal["round_id"],
        "sealed_at": iso(clock),
        "book_observed_by": iso(clock),
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
    return {
        "id": ident,
        "fixture_id": fixture["fixture_id"],
        "token_id": c["token_id"],
        "state": status,
        "reason": reason,
        "order_protocol": protocol,
    }


def _advance(root, db, row, clock, reserved=None):
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
            legacy = plan.get("order_protocol", LEGACY_PROTOCOL) == LEGACY_PROTOCOL
            cash = _cash(db, row["account"])
            budget = min(cash, number(plan["budget"]))
            if legacy:
                first_depth, second_depth = q1["asks"], q2["asks"]
                worst = None
            else:
                if q1["key"] == q2["key"] and (
                    q1["asks"] != q2["asks"] or q1["mid"] != q2["mid"]
                ):
                    raise ValueError("identical book identifier has conflicting depth")
                keys = (db, row["account"], row["condition_id"], row["token"])
                first_depth = ledger._remaining(*keys, q1, research=True)
                second_depth = ledger._remaining(*keys, q2, research=True)
                budget = min(budget, reserved)
                worst = ledger._risk_scenarios(
                    db, row["account"], row["fixture"], c, research=True
                )
            depth = pair_depth(first_depth, second_depth, number(intent["limit_price"]))
            fill = fit(
                depth,
                number(plan["quantity"]),
                c["fee_schedule"],
                budget,
                number(plan["fair_low"]),
                risk_ok=(lambda f: worst(f) <= MAX_MATCH) if worst else None,
            )
            if fill["gross"] < max(q1["minimum"], q2["minimum"]) or fill["net"] <= 0:
                raise ValueError(
                    "no eligible delayed fill"
                    if legacy
                    else "remaining_depth_price_cash_or_fixture_risk_insufficient"
                )
            if worst:
                res["worst_match_loss_after"] = fmt(worst(fill))
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
                cash_after_fill=fmt(cash - fill["spent"]),
            )
        except dbscope.EvidenceChanged:
            raise
        except (ValueError, TypeError, KeyError, OSError) as exc:
            res.update(status="skipped", reason=str(exc))
        else:
            # Publish depth, debit and status under the same transaction. Legacy fills
            # keep their original sizing, but future fills can see newly consumed levels.
            ledger._consume(
                db,
                row["account"],
                row["condition_id"],
                row["token"],
                q1,
                q2,
                fill["used"],
                research=True,
            )
            db.execute(
                "UPDATE research_shadow_accounts SET cash=? WHERE account=?",
                (res["cash_after_fill"], row["account"]),
            )
    if (
        status in ("held", "settled")
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
        "fixture_id": row["fixture"],
        "order_protocol": plan.get("order_protocol", LEGACY_PROTOCOL),
        "state": res["status"],
        "reason": res.get("reason"),
        "close_status": res.get("close_status"),
    }


def _decide(root, db, account, fixture, cut, clock, actions, gaps):
    seal = shadow_market.forecast(root, fixture, cut, clock)
    markets = market_archive.contracts(root, fixture["market_slug"], as_of=iso(clock))
    groups = {}
    for market in markets:
        if not market["mapping_error"]:
            for token in market["tokens"]:
                groups.setdefault(
                    (market["condition_id"], token["token_id"]), []
                ).append(market)
    existing = {
        (r["condition_id"], r["token"]): r
        for r in db.execute(
            "SELECT id,fixture,condition_id,token,status,json_extract(result,'$.reason') AS reason, "
            "COALESCE(json_extract(plan,'$.order_protocol'),?) AS protocol "
            "FROM research_shadow_orders WHERE account=? AND fixture=? AND cutoff=?",
            (LEGACY_PROTOCOL, account, fixture["fixture_id"], iso(cut)),
        )
    }
    protocols = {r["protocol"] for r in existing.values()}
    if len(protocols) > 1 or protocols - {LEGACY_PROTOCOL, ORDER_PROTOCOL}:
        raise ValueError(
            "conflicting or unsupported research protocols at one decision cutoff"
        )
    protocol = next(iter(protocols), ORDER_PROTOCOL)
    db.execute(
        "INSERT OR IGNORE INTO research_shadow_accounts VALUES(?,?,?)",
        (account, fmt(INITIAL), fmt(INITIAL)),
    )
    available = _cash(db, account)
    if protocol == LEGACY_PROTOCOL:
        available = _legacy_available(db, account, fixture["fixture_id"], available)
    for (cond, token), listings in sorted(groups.items()):
        if (previous := existing.get((cond, token))) is not None:
            actions.append(
                {
                    "id": previous["id"],
                    "fixture_id": previous["fixture"],
                    "token_id": token,
                    "state": previous["status"],
                    "reason": previous["reason"],
                    "order_protocol": protocol,
                    "repeated": True,
                }
            )
            continue
        try:
            opportunity._unique_market(listings)
            for market in listings:
                opportunity._check_child(root, fixture, market, clock)
            contract = shadow_market.terms(
                db, root, cond, token, clock, fixture["fixture_id"]
            )
            first = shadow_market.book(
                db,
                root,
                contract,
                cut,
                instant(fixture["kickoff"]),
                "first",
                seen_by=clock,
            )
            if not first:
                raise ValueError("missing original research first book")
            budget = min(MAX_ORDER, available)
            action = _freeze(
                db, account, fixture, seal, contract, first, clock, budget, protocol
            )
            actions.append(action)
            if protocol == LEGACY_PROTOCOL and action["state"] == "frozen":
                available -= budget
        except dbscope.EvidenceChanged:
            raise
        except (ValueError, TypeError, KeyError, OSError) as exc:
            gaps.append({"token": token, "reason": str(exc)})


def _advance_orders(root, db, account, slug, clock, actions, recovered, gaps):
    def advance(row, reserved=None):
        # A failed mark or original must never leave a debit without its fill.
        db.execute("SAVEPOINT shadow_action")
        try:
            item = _advance(root, db, row, clock, reserved)
        except dbscope.EvidenceChanged:
            raise
        except (ValueError, TypeError, KeyError, OSError) as exc:
            db.execute("ROLLBACK TO shadow_action")
            gaps.append(
                {"fixture": row["slug"], "token": row["token"], "reason": str(exc)}
            )
            return False
        finally:
            db.execute("RELEASE shadow_action")
        if item:
            (actions if row["slug"] == slug else recovered).append(item)
        return bool(item)

    if db.execute(
        "SELECT 1 FROM research_shadow_orders WHERE account=? AND slug=? AND status='frozen' LIMIT 1",
        (account, slug),
    ).fetchone():
        pending = []
        for row in db.execute(
            "SELECT *,json_extract(plan,'$.intent.priority') AS priority FROM research_shadow_orders WHERE account=? AND status='frozen'",
            (account,),
        ):
            plan = json.loads(row["plan"])
            protocol = plan.get("order_protocol", LEGACY_PROTOCOL)
            if protocol not in (ORDER_PROTOCOL, LEGACY_PROTOCOL):
                raise ValueError("unknown frozen research order protocol")
            delay = max(5, int(plan["intent"]["contract"]["seconds_delay"]))
            target = iso(instant(row["cutoff"]) + timedelta(seconds=delay))
            pending.append(
                dict(row)
                | {
                    "priority": row["priority"] or 0,
                    "protocol": protocol,
                    "target": target,
                    "budget": number(plan["budget"]),
                }
            )
        # Old, already sealed positions execute in their original cutoff/ID order.
        # Every new position shares the formal reservation and execution priorities.
        legacy_rows = sorted(
            (r for r in pending if r["protocol"] == LEGACY_PROTOCOL),
            key=lambda r: (r["cutoff"], r["id"]),
        )
        current_rows = sorted(
            (r for r in pending if r["protocol"] == ORDER_PROTOCOL),
            key=lambda r: (r["target"], *ledger.reservation_order(r)),
        )
        remaining = {r["id"] for r in pending}
        for row in legacy_rows:
            if (
                row["slug"] == slug
                or instant(row["target"]) + timedelta(seconds=5) < clock
            ) and advance(row):
                remaining.remove(row["id"])
        reservation_rows = sorted(pending, key=ledger.reservation_order)
        requested = [i for i, r in enumerate(current_rows) if r["slug"] == slug]
        for row in current_rows[: max(requested, default=-1) + 1]:
            if clock < instant(row["target"]):
                break
            funds = ledger.reserve_budgets(
                _cash(db, account),
                (
                    (r["id"], r["budget"])
                    for r in reservation_rows
                    if r["id"] in remaining
                ),
            )
            if not advance(row, funds[row["id"]]):
                break
            remaining.remove(row["id"])
    for row in db.execute(
        "SELECT * FROM research_shadow_orders WHERE account=? AND slug=? AND status IN ('held','settled') "
        "AND cutoff<=? AND json_extract(result,'$.scheduled_close_sha256') IS NULL ORDER BY cutoff,id",
        (account, slug, iso(clock - timedelta(hours=1))),
    ).fetchall():
        advance(row)


def run(data_dir, slug, *, account="research-shadow", fixture_id=None):
    if not isinstance(account, str) or not account.strip():
        raise ValueError("research account name required")
    started = instant(store.now_utc())
    actions, recovered, gaps = [], [], []
    clock = started
    with db_for(data_dir) as db:
        dbscope.prepare(db, "market-archive", market_archive._SCHEMA)
        dbscope.prepare(db, "research", jobs_research._SCHEMA)
        try:
            with db, dbscope.bind(data_dir, db) as batch:
                db.execute("BEGIN IMMEDIATE")
                clock = instant(store.now_utc())
                try:
                    fixture = opportunity._locate(data_dir, slug, fixture_id, clock)
                    cut = instant(fixture["kickoff"]) - timedelta(hours=1)
                    if cut - timedelta(seconds=30) <= clock <= cut:
                        batch.deadline = cut
                        _decide(
                            data_dir, db, account, fixture, cut, clock, actions, gaps
                        )
                    elif started <= cut < clock:
                        raise ledger.DecisionWindowClosed(
                            "missed research decision deadline while waiting for the writer"
                        )
                except (dbscope.EvidenceChanged, ledger.DecisionWindowClosed):
                    raise
                except (ValueError, TypeError, KeyError, OSError) as exc:
                    gaps.append({"reason": str(exc)})
                if batch.deadline is None:
                    _advance_orders(
                        data_dir, db, account, slug, clock, actions, recovered, gaps
                    )
                dbscope.recheck_originals(batch)
                if batch.deadline and instant(store.now_utc()) > batch.deadline:
                    raise ledger.DecisionWindowClosed(
                        "research fixture missed the decision deadline; no new intents committed"
                    )
        except (dbscope.EvidenceChanged, ledger.DecisionWindowClosed) as exc:
            actions.clear()
            recovered.clear()
            gaps = [{"reason": str(exc)}]
    return {
        "account": account,
        "mode": "research_shadow",
        "fixture": slug,
        "observed_at": iso(clock),
        "actions": actions,
        "recovered": recovered,
        "gaps": gaps,
        "eligible_for_formal": False,
        "eligible_for_A": False,
    }


def _round_interval(orders, measure):
    # The mean of these fixture-level linearized values is the portfolio ratio.
    # Treat all positions in the same official league round as one dependent cluster.
    from .experiment_statistics import clustered_t

    matches = {}
    unassigned = 0
    for row in orders:
        if measure == "settled_roi":
            if row["status"] != "settled":
                continue
            cost = number(row["spent"])
            numerator, denominator = number(row["proceeds"]) - cost, cost
        else:
            if row.get("scheduled_close_cost_difference") is None:
                continue
            numerator, denominator = number(row["scheduled_close_cost_difference"]), ONE
        key = (row["season"], row["round_id"])
        if not key[0] or type(key[1]) != int or not 1 <= key[1] <= 38:
            unassigned += 1
            continue
        totals = matches.setdefault((key, row["fixture_id"]), [ZERO, ZERO])
        totals[0] += numerator
        totals[1] += denominator
    denominator = sum((den for _, den in matches.values()), ZERO)
    groups = {}
    estimate = None
    if denominator:
        estimate = sum((num for num, _ in matches.values()), ZERO) / denominator
        avg_weight = denominator / len(matches)
        for (key, _), (num, den) in matches.items():
            groups.setdefault(key, []).append(
                float(estimate + (num - estimate * den) / avg_weight)
            )
    result = {
        "rounds": len(groups),
        "matches": len(matches),
        "unassigned_orders": unassigned,
        "estimate": fmt(estimate) if estimate is not None and not unassigned else None,
        "interval": None,
    }
    if unassigned:
        result["reason"] = "missing_official_round"
    elif len(groups) < 2:
        result["reason"] = "fewer_than_two_official_rounds"
    else:
        stats = clustered_t(groups.values(), family_comparisons=2)
        interval = stats["intervals"][0]
        result.update(
            method=stats["method"],
            degrees_of_freedom=stats["degrees_of_freedom"],
            family_confidence=stats["family_confidence"],
            family_comparisons=stats["family_comparisons"],
            interval={k: interval[k] for k in ("lower", "upper", "standard_error")},
        )
    return result


def _performance(orders):
    settled = [o for o in orders if o["status"] == "settled"]
    spent = sum((number(o["spent"]) for o in settled), ZERO)
    proceeds = sum((number(o["proceeds"]) for o in settled), ZERO)
    return {
        "orders": len(orders),
        "counts": dict(Counter(o["status"] for o in orders)),
        "settled_spend": fmt(spent),
        "settled_proceeds": fmt(proceeds),
        "realized_pnl": fmt(proceeds - spent) if settled else None,
        "settled_roi": fmt((proceeds - spent) / spent) if spent else None,
        "round_clustered": {
            metric: _round_interval(orders, metric)
            for metric in ("settled_roi", "mean_scheduled_close_difference")
        },
    }


def report(data_dir, account="research-shadow"):
    with db_for(data_dir) as db:
        acc = db.execute(
            "SELECT * FROM research_shadow_accounts WHERE account=?", (account,)
        ).fetchone()
        if not acc and account != "research-shadow":
            raise ValueError("unknown research shadow account")
        orders = []
        classes = Counter()
        legacy_rounds = {}
        for r in db.execute(
            "SELECT * FROM research_shadow_orders WHERE account=? ORDER BY cutoff,id",
            (account,),
        ):
            p, out = json.loads(r["plan"]), json.loads(r["result"])
            i = p["intent"]
            c = i["contract"]
            if digest(p) != r["plan_sha"]:
                raise ValueError("altered research decision")
            protocol = p.get("order_protocol", LEGACY_PROTOCOL)
            season, round_id = p.get("season"), p.get("round_id")
            if protocol == LEGACY_PROTOCOL and round_id is None:
                forecast = i["forecast_digest"]
                if forecast not in legacy_rounds:
                    row = db.execute(
                        "SELECT data_json FROM forecast_seals WHERE digest=? AND fixture_id=?",
                        (forecast, r["fixture"]),
                    ).fetchone()
                    sealed = json.loads(row[0]) if row else {}
                    legacy_rounds[forecast] = (
                        (sealed.get("season"), sealed.get("round_id"))
                        if store.digest(sealed) == forecast
                        else (None, None)
                    )
                season, round_id = legacy_rounds[forecast]
            classes[r["status"]] += 1
            orders.append(
                {
                    "id": r["id"],
                    "order_protocol": protocol,
                    "season": season,
                    "round_id": round_id,
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
        cohorts = {}
        for i in orders:
            cohorts.setdefault(i["order_protocol"], []).append(i)
            category = (
                i["lineups"]["home"],
                i["lineups"]["away"],
                i["pregame_basis"],
                i["fee_basis"],
                i["order_protocol"],
            )
            g = groups.setdefault(
                category,
                {
                    "lineup_home_confirmed": category[0],
                    "lineup_away_confirmed": category[1],
                    "pregame_basis": category[2],
                    "fee_basis": category[3],
                    "order_protocol": category[4],
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
            "account_state": "initialized" if acc else "unstarted",
            "initial_cash": acc["initial"] if acc else None,
            "cash": acc["cash"] if acc else None,
            "counts": dict(classes),
            "orders": orders,
            "strata": list(groups.values()),
            "by_order_protocol": {k: _performance(v) for k, v in cohorts.items()},
            "performance_scope": "per_order_protocol"
            if len(cohorts) > 1
            else "single_order_protocol",
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
            if closed and len(cohorts) == 1
            else None,
            "settled_spend": fmt(spent),
            "realized_pnl": fmt(pnl) if settled else None,
            "settled_roi": fmt(pnl / spent) if spent and len(cohorts) == 1 else None,
        }
    if acc:
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
