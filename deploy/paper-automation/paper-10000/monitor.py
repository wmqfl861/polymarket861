"""Update the shared paper account from archived public token status receipts."""

import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
from functools import partial
from http.client import HTTPException
from pathlib import Path
from zoneinfo import ZoneInfo

from account_engine import FUND, replay
from polymarket861 import budget_archive_proof as proof
from polymarket861 import ledger_rules as rules
from polymarket861 import market_archive, settlement, store

ROOT = Path(__file__).resolve().parent
BEIJING = ZoneInfo("Asia/Shanghai")
PROOF_ERRORS = (ValueError, TypeError, KeyError, OSError, sqlite3.Error)
REQUEST_ERRORS = (*PROOF_ERRORS, HTTPException)
QUOTE_AGE = timedelta(seconds=30)
STATES = {
    "awaiting_kickoff": "待开球",
    "awaiting_official_result": "待官方双边结算",
    "settled": "已结算",
    "disputed": "结算证据有冲突",
    "proof_unavailable": "结算证据暂不可用",
}


def load(path, default=None):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return default


def atomic_write(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as f:
        temporary = Path(f.name)
        try:
            f.write(value.encode())
            f.flush()
            os.fsync(f.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def watch_quotes():
    path = ROOT / "watch-status.json"
    started = store.now_utc()
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "watch-picks.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if result.returncode:
            return {"state": "error", "reason": result.stderr.strip()[-1200:]}, {}
        seen = load(path, {})
        health = {"checked_at": seen.get("checked_at"), "counts": seen.get("counts", {})}
        if health["checked_at"] and rules.instant(health["checked_at"]) >= rules.instant(started):
            return {**health, "state": "ok"}, {pick["id"]: pick for pick in seen["picks"]}
        return {**health, "state": "no_new_report"}, {}
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
        return {"state": "error", "reason": str(exc)}, {}


def receipt(record):
    return {
        "token_id": record["token_id"],
        "received_at": record["resolved_at"],
        "original_sha256": record["source_sha256"],
        "source": record["source"],
        "status": record["status"],
        "payout": record["payout"],
    }


def terminal(source, market, token, kickoff, cutoff):
    archive, db = source
    history = settlement.resolution_history(db, archive, token, as_of=cutoff, market=market)
    first = None
    for item in history:
        if item["status"] != "resolved":
            continue
        settlement._verify_token(market, item, token)
        if rules.instant(item["resolved_at"]) < kickoff:
            raise ValueError("terminal original predates verified fixture")
        if first is None:
            first = item
    return first, history[-1] if history else None


def newest_originals(source, tokens, cutoff):
    if not tokens:
        return []
    _, db = source
    checks = []
    for token in tokens:
        rows = db.execute(
            """SELECT received_at, sha AS original_sha256
               FROM ma_requests
               WHERE operation='resolution' AND selector=? AND status='ok' AND received_at<=?
               ORDER BY received_at DESC, rowid DESC LIMIT 2""",
            (token, rules.iso(cutoff)),
        )
        checks.extend({"token_id": token, **dict(row)} for row in rows)
    return checks


def settlement_for(source, market, intent, tokens, cutoff):
    kickoff = rules.instant(intent["kickoff"])
    pair = [terminal(source, market, token, kickoff, cutoff) for token in tokens]
    observed = [receipt(latest) for _, latest in pair if latest is not None]
    if any(first is None for first, _ in pair):
        return {"state": "awaiting_official_result", "receipts": observed}
    held, opposite = [first for first, _ in pair]
    event = {
        "id": f"redemption-{intent['id']}",
        "fixture_id": intent["fixture_id"],
        "condition_id": intent["condition_id"],
        "token_id": intent["token_id"],
        "at": max(held["resolved_at"], opposite["resolved_at"]),
        "original_sha256": held["source_sha256"],
    }
    payout, gap = proof.redemption(source, market, event, kickoff)
    if gap:
        raise ValueError(gap)
    proof.audit_redemption(source, market, event, kickoff, cutoff, payout)
    return {
        "state": "settled",
        "event": event,
        "payout": rules.fmt(payout),
        "receipts": [receipt(held), receipt(opposite)],
    }


def settlement_update(document, fills, history, cutoff):
    books = {book["id"]: book for book in document["books"]}
    intents = {intent["id"]: intent for intent in document["intents"]}
    current = {}
    verified = {}
    with proof.read_archive(ROOT) as source, proof.football_archive(source) as football:
        archive = source[0] if source else None
        for fill in fills:
            position_id = fill["id"]
            intent = intents[position_id]
            if cutoff < rules.instant(intent["kickoff"]):
                current[position_id] = {"state": "awaiting_kickoff"}
                continue
            try:
                pair = tuple(books[intent[key]] for key in ("decision_book", "execution_book"))
                market = proof.entry(
                    source, intent, pair, scheduled=True, as_of=cutoff, football=football
                )
                token = intent["token_id"]
                other = settlement._outcome_slot(market, token)
                tokens = (token, market["tokens"][1 - other]["token_id"])
                verified[position_id] = (market, tokens)
            except PROOF_ERRORS as exc:
                current[position_id] = {"state": "proof_unavailable", "reason": str(exc)}

    request_errors = {}
    fetch = partial(market_archive._fetch, attempts=1, timeout=6)
    for token in dict.fromkeys(token for _, tokens in verified.values() for token in tokens):
        try:
            settlement.token_status(archive, token, recheck_terminal=True, fetch=fetch)
        except REQUEST_ERRORS as exc:
            request_errors[token] = f"{type(exc).__name__}: {exc}"

    cutoff = rules.instant(store.now_utc())
    with proof.read_archive(ROOT) as source:
        for position_id, (market, tokens) in verified.items():
            try:
                result = settlement_for(source, market, intents[position_id], tokens, cutoff)
            except (ValueError, TypeError, KeyError) as exc:
                result = {
                    "state": "disputed",
                    "reason": str(exc),
                    "originals_for_review": newest_originals(source, tokens, cutoff),
                }
            except (OSError, sqlite3.Error) as exc:
                result = {"state": "proof_unavailable", "reason": str(exc)}
            previous = history.get(position_id, {})
            if result["state"] == "settled":
                history[position_id] = {**previous, "accepted": result}
            elif result["state"] == "disputed":
                dispute = {**result, "first_observed_at": rules.iso(cutoff)}
                prior = previous.get("disputes", [])
                if not any({k: v for k, v in item.items() if k != "first_observed_at"} == result for item in prior):
                    history[position_id] = {**previous, "disputes": [*prior, dispute]}
                if previous.get("accepted"):
                    result["previously_accepted"] = previous["accepted"]
            errors = {token: request_errors[token] for token in tokens if token in request_errors}
            current[position_id] = {**result, **({"request_errors": errors} if errors else {})}
    return current, cutoff


def render_position(intent, decision, pick, state):
    cost = Decimal(decision["cost_including_cash_fees"])
    fill = decision["fill"]
    net = Decimal(fill["net_shares"])
    weighted_price = sum(Decimal(level["price"]) * Decimal(level["size"]) for level in fill["levels"])
    sizing = intent["sizing"]
    p = (rules.unit(sizing["model_fair"]) + rules.unit(sizing["external_fair"])) / 2
    position = {
        **decision,
        "display": pick.get("display", decision["id"]),
        "fixture": pick.get("fixture"),
        "condition_id": intent["condition_id"],
        "token_id": intent["token_id"],
        "kickoff": intent["kickoff"],
        "weighted_fill_price": rules.fmt(weighted_price / Decimal(fill["gross_shares"])),
        "sizing_probability": rules.fmt(p),
        "sizing": sizing,
        "profit_if_full_payout": rules.fmt(net - cost),
        "loss_if_zero_payout": rules.fmt(-cost),
        "settlement": state,
        "redemption_cash": None,
        "settled_paper_pnl": None,
    }
    if state["state"] == "settled":
        proceeds = (net * Decimal(state["payout"])).quantize(rules.UNIT, rounding=ROUND_DOWN)
        position["redemption_cash"] = rules.fmt(proceeds)
        position["settled_paper_pnl"] = rules.fmt(proceeds - cost)
    return position


def position_mark(position, observation, as_of):
    if position["redemption_cash"] is not None:
        return {"state": "settled"}
    if position["settlement"]["state"] == "disputed":
        return {"state": "unavailable", "reason": "settlement_disputed"}
    quotes = observation.get("selected_quotes", [])
    market = observation.get("market", {})
    if (len(quotes) != 2 or observation.get("state") == "closed"
        or observation.get("token_id") != position["token_id"]
        or market.get("condition_id") != position["condition_id"]):
        return {"state": "unavailable", "reason": "verified_pair_unavailable"}

    net = Decimal(position["fill"]["net_shares"])
    latest = quotes[1]
    observations = []
    try:
        first_receipt, last_receipt = [rules.instant(q["received_at"]) for q in quotes]
        delay = timedelta(seconds=max(5, market["seconds_delay"]))
        if (rules.instant(latest["requested_at"]) < first_receipt + delay
            or last_receipt >= rules.instant(position["kickoff"])):
            return {"state": "unavailable", "reason": "quote_timing_invalid"}
        for quote in quotes:
            bid = rules.unit(quote["bid"])
            ask = rules.unit(quote["ask"])
            received_at = rules.instant(quote["received_at"])
            exchange_at = rules.instant(quote["exchange_at"])
            requested_at = rules.instant(quote["requested_at"])
            if not (rules.ZERO < bid < ask < rules.ONE
                    and timedelta(0) <= received_at - requested_at <= QUOTE_AGE
                    and exchange_at <= received_at):
                return {"state": "unavailable", "reason": "invalid_book"}
            if not timedelta(0) <= as_of - received_at <= QUOTE_AGE:
                return {"state": "unavailable", "reason": "stale_quote"}
            if rules.number(quote["bid_size"]) < net:
                return {"state": "unavailable", "reason": "insufficient_best_bid_depth"}
            observations.append({
                **quote,
                "receipt_age_seconds": (as_of - received_at).total_seconds(),
                "book_last_change_age_seconds": (received_at - exchange_at).total_seconds(),
            })
    except (TypeError, ValueError, KeyError) as exc:
        return {"state": "unavailable", "reason": str(exc)}

    bid = rules.unit(latest["bid"])
    value = (net * bid).quantize(rules.UNIT, rounding=ROUND_DOWN)
    return {
        "state": "quoted",
        "bid": rules.fmt(bid),
        "value": rules.fmt(value),
        "unrealized_paper_pnl": rules.fmt(value - Decimal(position["cost_including_cash_fees"])),
        "quotes": observations,
        "all_book_changes_within_30_seconds": all(
            q["book_last_change_age_seconds"] <= QUOTE_AGE.total_seconds()
            for q in observations
        ),
    }


def portfolio_mark(account, positions):
    open_positions = [p for p in positions if p["redemption_cash"] is None]
    valued = [p["mark_to_market"] for p in open_positions if p["mark_to_market"]["state"] == "quoted"]
    strict = [mark for mark in valued if mark["all_book_changes_within_30_seconds"]]
    full_coverage = len(open_positions) == len(valued)
    marked_value = sum((Decimal(mark["value"]) for mark in valued), rules.ZERO)
    marked_pnl = sum((Decimal(mark["unrealized_paper_pnl"]) for mark in valued), rules.ZERO)
    cash = Decimal(account["total_available_cash"])
    return {
        "mark_to_market_value": rules.fmt(marked_value) if full_coverage else None,
        "mark_to_market_equity": rules.fmt(cash + marked_value) if full_coverage else None,
        "total_unrealized_paper_pnl": rules.fmt(marked_pnl) if full_coverage else None,
        "valuation": {
            "basis": "latest_best_bid_after_two_fresh_observations_with_full_position_size",
            "http_receipt_max_age_seconds": int(QUOTE_AGE.total_seconds()),
            "includes_exit_fees": False,
            "open_positions": len(open_positions),
            "valued_positions": len(valued),
            "positions_with_both_book_changes_within_30_seconds": len(strict),
            "value_of_valued_positions": rules.fmt(marked_value),
            "unrealized_pnl_of_valued_positions": rules.fmt(marked_pnl),
        },
    }


def report_text(result):
    if result["state"] == "awaiting_account_input":
        return "# 10,000 模拟美元账户\n\n等待入场记录。\n"

    def amount(value, signed=False):
        return "—" if value is None else format(Decimal(value), "+,.2f" if signed else ",.2f")

    def beijing(value):
        return rules.instant(value).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")

    lines = [
        "# 10,000 模拟美元账户",
        "",
        f"更新于北京时间 {beijing(result['as_of'])}",
        "",
        "| 初始资金 | 可用现金 | 未结成本 | 已实现盈亏 | 持仓估值 | 浮动盈亏 | 估算账户权益 |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        "| " + " | ".join(
            amount(result[key], key in ("total_settled_paper_pnl", "total_unrealized_paper_pnl"))
            for key in ("opening_cash", "total_available_cash", "total_unredeemed_cost",
                        "total_settled_paper_pnl", "mark_to_market_value",
                        "total_unrealized_paper_pnl", "mark_to_market_equity")
        ) + " |",
        "",
        "| 持仓 | 开球（北京时间） | 平均成交价（每净份含费成本） | 凯利计算概率 | 模拟成本 | 净份数 | 全额兑付净赚 | 零兑付亏损 | 状态 | 兑付现金 | 已实现盈亏 | 买一估值 | 浮动盈亏 | 盘口变动距取样（秒） |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for position in result["positions"]:
        state = position["settlement"]
        status = STATES[state["state"]]
        if state["state"] == "settled":
            status += " " + beijing(state["event"]["at"])
        price = Decimal(position["weighted_fill_price"])
        net_cost = Decimal(position["fill"]["cost_per_net_share"])
        probability = Decimal(position["sizing_probability"])
        mark = position["mark_to_market"]
        lines.append("| " + " | ".join((
            position["display"].replace("|", "\\|"),
            beijing(position["kickoff"]),
            f"{price:.4f}（{net_cost:.4f}）",
            f"{probability:.2%}",
            amount(position["cost_including_cash_fees"]),
            amount(position["fill"]["net_shares"]),
            amount(position["profit_if_full_payout"], True),
            amount(position["loss_if_zero_payout"], True),
            status,
            amount(position["redemption_cash"]),
            amount(position["settled_paper_pnl"], True),
            amount(mark.get("value")),
            amount(mark.get("unrealized_paper_pnl"), True),
            " / ".join(f"{q['book_last_change_age_seconds']:.1f}" for q in mark.get("quotes", [])) or "—",
        )) + " |")
    valuation = result["valuation"]
    if valuation["open_positions"]:
        total = valuation["open_positions"]
        valued = valuation["valued_positions"]
        recent = valuation["positions_with_both_book_changes_within_30_seconds"]
        lines.extend(("", f"估值覆盖 {valued}/{total} 笔未结持仓，两次取样时距盘口上次变动均在 30 秒内的覆盖为 {recent}/{total}。"))
        if 0 < valued < total:
            lines.append(f"已覆盖持仓的浮动盈亏为 {amount(valuation['unrealized_pnl_of_valued_positions'], True)} 模拟美元。")
    lines.extend(("", "估值用最近一次买一价乘以费后净份数，两次公开报价的独立回执均经新鲜度核验，买一深度均须覆盖整笔持仓。估值以卖出前金额计。"))
    quote = result["quote_monitor"]
    quote_label = {"ok": "已更新", "no_new_report": "本轮未生成新报告", "error": "本轮检查异常"}
    lines.extend(("", f"盘口监控{quote_label[quote['state']]}。", ""))
    return "\n".join(lines)


def update():
    try:
        content = (ROOT / "account-input.json").read_bytes()
    except FileNotFoundError:
        return {"state": "awaiting_account_input", "as_of": store.now_utc(), "opening_cash": rules.fmt(FUND)}
    document = json.loads(content)
    if document.get("redemptions"):
        raise ValueError("account-input.json contains redemptions instead of the monitor-owned journal")
    picks = load(ROOT / "picks.json", [])
    by_pick = {pick["id"]: pick for pick in picks}
    quote_monitor, observations = watch_quotes() if picks else ({"state": "no_new_report"}, {})
    cutoff = rules.instant(store.now_utc())
    preview = replay(document, as_of=cutoff, data_dir=ROOT)
    intents = {intent["id"]: intent for intent in document["intents"]}
    fills = [item for item in preview["decisions"] if item["outcome"] == "filled"]
    journal = load(ROOT / "redemptions.json", {"positions": {}})
    current, cutoff = settlement_update(document, fills, journal["positions"], cutoff)
    payments = [event["event"] for event in current.values() if event["state"] == "settled"]
    account = replay({**document, "redemptions": payments}, as_of=cutoff, data_dir=ROOT)
    rows = [
        render_position(intents[fill["id"]], fill, by_pick.get(fill["id"], {}), current[fill["id"]])
        for fill in account["decisions"] if fill["outcome"] == "filled"
    ]
    for position in rows:
        position["mark_to_market"] = position_mark(position, observations.get(position["id"], {}), cutoff)
    expected_open_cost = sum(Decimal(p["cost_including_cash_fees"]) for p in rows if p["redemption_cash"] is None)
    expected_realized = sum(Decimal(p["settled_paper_pnl"]) for p in rows if p["settled_paper_pnl"] is not None)
    if (account["redemption_events"].get("positions_redeemed", 0) != len(payments)
        or expected_open_cost != Decimal(account["total_unredeemed_cost"])
        or expected_realized != Decimal(account["total_settled_paper_pnl"])):
        raise ValueError("position settlement and account replay disagree")
    write_json(ROOT / "redemptions.json", {**journal, "as_of": account["as_of"], "active_events": payments})
    return {
        **account,
        **portfolio_mark(account, rows),
        "state": "ok",
        "account_input_sha256": hashlib.sha256(content).hexdigest(),
        "positions": rows,
        "settlement_counts": dict(Counter(p["settlement"]["state"] for p in rows)),
        "quote_monitor": quote_monitor,
    }


def main():
    with (ROOT / ".monitor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        result = update()
        write_json(ROOT / "account-status.json", result)
        atomic_write(ROOT / "account.md", report_text(result))
        keys = ("as_of", "state", "total_available_cash", "total_unredeemed_cost",
                "total_settled_paper_pnl", "settlement_counts", "quote_monitor")
        print(json.dumps({key: result.get(key) for key in keys}, ensure_ascii=False))


if __name__ == "__main__":
    main()
