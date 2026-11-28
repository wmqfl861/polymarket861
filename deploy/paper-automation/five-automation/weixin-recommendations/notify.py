"""Send a Weixin receipt for each new filled position in the shared paper account."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
PAPER = HERE.parents[1] / "paper-10000"
HERMES = "/home/ubuntu/.local/bin/hermes"
BEIJING = ZoneInfo("Asia/Shanghai")
LEAGUES = {"epl": "英超", "lal": "西甲", "sea": "意甲", "fl1": "法甲", "bun": "德甲"}


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("missing timezone")
    return result


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("invalid ledger amount")
    return result


def filled_positions(paper):
    account = json.loads((paper / "account-status.json").read_bytes())
    if (account["state"] != "ok" or account["schema"] != "five-league-paper-risk-v1"
            or account["evidence_mode"] != "local_receipts"):
        raise ValueError("paper ledger unavailable")
    cutoff = instant(account["as_of"])
    filled = {d["id"]: d for d in account["decisions"] if d["outcome"] == "filled"}
    positions = []
    for position in account["positions"]:
        decision = filled.get(position["id"])
        if not decision or position["outcome"] != "filled":
            continue
        at = instant(position["at"])
        cost = number(position["cost_including_cash_fees"])
        net = number(position["fill"]["net_shares"])
        if (at <= cutoff and at == instant(decision["at"])
                and cost == number(decision["cost_including_cash_fees"]) and cost > 0
                and net == number(decision["fill"]["net_shares"]) and net > 0):
            positions.append(position)
    return positions


def database(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, attempted_at TEXT NOT NULL,
            status TEXT NOT NULL, body TEXT NOT NULL, message_id TEXT, error TEXT);
        CREATE INDEX IF NOT EXISTS attempt_key ON attempts(fingerprint, id);
        CREATE TABLE IF NOT EXISTS paper_opening_policy (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            starts_at TEXT NOT NULL, existing_position_ids TEXT NOT NULL);
    """)
    return db


def opening_policy(db, positions, starts_at, *, preview=False):
    saved = db.execute("SELECT starts_at, existing_position_ids FROM paper_opening_policy WHERE singleton=1").fetchone()
    if saved:
        return instant(saved["starts_at"]), set(json.loads(saved["existing_position_ids"]))
    # Seed old position IDs once. A later revaluation of an existing position is never a new opening.
    existing = {p["id"] for p in positions if instant(p["at"]) <= starts_at}
    if not preview:
        with db:
            db.execute("INSERT INTO paper_opening_policy VALUES (1, ?, ?)",
                       (starts_at.isoformat(), json.dumps(sorted(existing))))
    return starts_at, existing


def opening_message(position):
    kickoff = instant(position["kickoff"]).astimezone(BEIJING)
    opened = instant(position["at"]).astimezone(BEIJING)
    league = LEAGUES.get(position["league"], position["league"])
    cost = number(position["cost_including_cash_fees"])
    cent_price = (100 * number(position["weighted_fill_price"])).quantize(Decimal("0.01"))
    return (f"模拟账户新建仓 · {league}\n"
            f"{position['display']}\n"
            f"模拟投入 {cost:,.2f} 美元，成交均价 {format(cent_price.normalize(), 'f')}¢\n"
            f"北京时间 {opened:%m/%d %H:%M} 建仓，{kickoff:%m/%d %H:%M} 开球")


def send(body):
    try:
        proc = subprocess.run([HERMES, "send", "--to", "weixin", "--json", "--file", "-"], input=body,
                              capture_output=True, text=True, timeout=90, check=False)
        reply = json.loads(proc.stdout)
    except (FileNotFoundError, PermissionError) as exc:
        return "failed", None, type(exc).__name__
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return "uncertain", None, type(exc).__name__
    if not isinstance(reply, dict):
        return "uncertain", None, "invalid_response"
    if (proc.returncode == 0 and reply.get("success") is True and reply.get("platform") == "weixin"
            and not reply.get("skipped") and not reply.get("error")):
        if reply.get("message_id"):
            return "accepted", reply["message_id"], None
        return "uncertain", None, "missing message id"
    error = str(reply.get("error") or reply.get("reason") or "rejected")
    codes = re.findall(r"\b(?:ret|errcode)=-?\d+", error)
    if "session" in error.lower() or "prepare failed" in error.lower():
        codes.append("session")
    if "rate limit" in error.lower():
        codes.append("rate_limit")
    return "failed", None, " ".join(codes) or "Hermes rejected delivery"


def mirrored(body, attempted_at):
    # Hermes writes the target DM's transcript only after its adapter has accepted a message.
    path = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "state.db"
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=4)
        try:
            return bool(db.execute("""
                SELECT 1 FROM messages m JOIN sessions s ON s.id=m.session_id
                WHERE s.source='weixin' AND m.role='assistant' AND m.content=?
                  AND m.timestamp>=? LIMIT 1
            """, (body, instant(attempted_at).timestamp() - 2)).fetchone())
        finally:
            db.close()
    except sqlite3.Error:
        return None


def notify_opening(db, position, now, preview, sender):
    # The ledger ID is stable across price, fixture-status and account-report updates.
    key = hashlib.sha256(("paper-position-opened\0" + position["id"]).encode()).hexdigest()
    prior = db.execute("SELECT * FROM attempts WHERE fingerprint=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
    if prior and prior["status"] in ("accepted", "expired_undelivered"):
        return True
    ambiguous = prior and prior["status"] in ("sending", "uncertain", "expired_unconfirmed")
    if ambiguous:
        receipt = mirrored(prior["body"], prior["attempted_at"])
        if receipt:
            if not preview:
                with db:
                    db.execute("UPDATE attempts SET status='accepted', error=NULL WHERE id=?", (prior["id"],))
            return True
    if instant(position["kickoff"]) <= now or position.get("redemption_cash") is not None:
        if preview or (prior and prior["status"] == "expired_unconfirmed"):
            return True
        # Keep the original attempt timestamp for future read-only receipt reconciliation.
        with db:
            if ambiguous:
                db.execute("UPDATE attempts SET status='expired_unconfirmed' WHERE id=?", (prior["id"],))
            else:
                db.execute("INSERT INTO attempts(fingerprint,attempted_at,status,body,error) VALUES (?,?,?,?,?)",
                           (key, now.isoformat(), "expired_undelivered", opening_message(position), "delivery window ended"))
        print(f"opening delivery window ended for {position['id']}", file=sys.stderr)
        return True
    if ambiguous:
        # Allow time for the transcript to catch up before retrying an ambiguous send.
        if receipt is None or now < instant(prior["attempted_at"]) + timedelta(minutes=30):
            print(f"awaiting Hermes receipt for opening {position['id']}", file=sys.stderr)
            return False
    if prior and now < instant(prior["attempted_at"]) + timedelta(minutes=5):
        return False
    body = opening_message(position)
    if preview:
        print(body)
        return True
    with db:
        attempt = db.execute(
            "INSERT INTO attempts(fingerprint,attempted_at,status,body) VALUES (?,?,'sending',?)",
            (key, now.isoformat(), body),
        ).lastrowid
    status, message_id, error = sender(body)
    if status == "uncertain" and mirrored(body, now.isoformat()):
        status, error = "accepted", None
    with db:
        db.execute("UPDATE attempts SET status=?, message_id=?, error=? WHERE id=?",
                   (status, message_id, error, attempt))
    print(f"{status} · paper opening {position['id']}")
    return status == "accepted"


def run(db, now, preview=False, sender=send, paper=PAPER):
    positions = filled_positions(paper)
    start, existing = opening_policy(db, positions, now, preview=preview)
    success = True
    for position in sorted(positions, key=lambda p: (instant(p["at"]), p["id"])):
        if position["id"] in existing or not start < instant(position["at"]) <= now:
            continue
        if not notify_opening(db, position, now, preview, sender):
            success = False
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    with (HERE / "notify.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with closing(database(HERE / "notifications.sqlite3")) as db:
            try:
                return run(db, datetime.now(timezone.utc), preview=args.preview)
            except (OSError, ValueError, KeyError, TypeError, ArithmeticError, sqlite3.Error) as exc:
                print(f"paper opening check failed: {exc}", file=sys.stderr)
                return 1


if __name__ == "__main__":
    sys.exit(main())
