"""systemd ExecCondition for recovering the EPL collector's stalled public HTTP pool."""

import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


SERVICE = "polymarket861-season.service"
WINDOW = timedelta(minutes=15)


def stamp(at):
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def stalled(db, now):
    since = stamp(now - WINDOW)
    if db.execute("SELECT 1 FROM ma_cooldown WHERE not_before > ? LIMIT 1", (stamp(now),)).fetchone():
        return False
    # Cap the cost on a live archive. An incomplete lookback cannot authorize a restart.
    rows = db.execute(
        "SELECT id, operation, received_at, status, detail FROM ma_requests ORDER BY id DESC LIMIT 8192"
    ).fetchall()
    end = stamp(now)
    recent = [row[1:] for row in rows if since <= row[2] <= end]
    if any(row[2] in ("ok", "not_found") or "HTTP Error" in (row[3] or "") for row in recent):
        return False
    failures = [(op, at) for op, at, status, detail in recent
                if status == "error" and detail == "public HTTP total deadline elapsed"]
    ops = {op for op, _ in failures}
    if not (
        ops.intersection(("catalog", "event")) and ops.intersection(("book", "books"))
        and min(at for _, at in failures) <= stamp(now - timedelta(minutes=10))
        and max(at for _, at in failures) >= stamp(now - timedelta(minutes=3))
    ):
        return False
    # Requests can commit out of timestamp order while the archive is busy.
    if len(rows) == 8192 and db.execute(
        "SELECT 1 FROM ma_requests WHERE id < ? AND received_at BETWEEN ? AND ? LIMIT 1",
        (rows[-1][0], since, end),
    ).fetchone():
        return False
    return True


def active_process():
    output = subprocess.check_output(
        ["systemctl", "show", SERVICE, "-p", "ActiveState", "-p", "MainPID",
         "-p", "ActiveEnterTimestampMonotonic"], text=True, timeout=4,
    )
    props = dict(line.split("=", 1) for line in output.splitlines())
    entered = int(props["ActiveEnterTimestampMonotonic"]) / 1_000_000
    if props["ActiveState"] == "active" and entered and time.monotonic() - entered >= 1800:
        return int(props["MainPID"])
    return 0


def upstream_reachable(db):
    # Use the deployed transport, service resolver and public endpoints. No archive writes.
    from polymarket861 import market_provider as provider

    tokens = {row[0] for row in db.execute("SELECT token FROM ma_books ORDER BY rowid DESC LIMIT 8")}
    if not tokens:
        return False
    events = json.loads(provider.get(provider.GAMMA + "/events", {"limit": 1}, timeout=6).body)
    books = json.loads(provider.get(provider.CLOB + "/books", {"token_ids": sorted(tokens)}, timeout=3.5).body)
    return (isinstance(events, list) and bool(events) and isinstance(events[0], dict)
            and isinstance(books, list) and any(
                isinstance(row, dict) and row.get("asset_id") in tokens
                and isinstance(row.get("bids"), list) and isinstance(row.get("asks"), list)
                for row in books))


def main(path):
    pid = active_process()
    if not pid:
        return 1
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        db.execute("PRAGMA query_only=ON")
        if not stalled(db, datetime.now(timezone.utc)):
            return 1
        try:
            if not upstream_reachable(db):
                print("Fresh Gamma or CLOB probe returned unexpected data")
                return 1
        except (OSError, ValueError) as error:
            print(f"Fresh upstream probe failed ({type(error).__name__})")
            return 1
        if active_process() != pid or not stalled(db, datetime.now(timezone.utc)):
            return 1
    print(f"EPL PID {pid}: persistent Gamma and CLOB timeouts with healthy fresh connections", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(Path(sys.argv[1])))
    except (OSError, sqlite3.Error, subprocess.SubprocessError, ValueError) as error:
        print(f"Recovery check unavailable: {error}", file=sys.stderr)
        sys.exit(255)
