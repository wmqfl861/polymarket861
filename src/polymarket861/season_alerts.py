"""Durable delivery for optional operator commands and the independent heartbeat check."""

import shlex
import sys
import subprocess
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from . import store

TIMEOUT = 3


def emit(db, key, fixture, role, reason):
    now = store.now_utc()
    inserted = db.execute(
        "INSERT OR IGNORE INTO season_alerts VALUES(?,?,?,?,?)",
        (key, fixture, role, now, reason),
    ).rowcount
    if inserted:
        db.execute(
            "INSERT INTO season_alert_delivery VALUES(?,?,0,NULL,NULL)", (key, now)
        )
        print(
            store.canonical(
                {"season_alert": role, "fixture": fixture, "reason": reason, "at": now}
            ),
            file=sys.stderr,
            flush=True,
        )


def report_cause(db, fixture, role, cause, reason, stamp):
    # A new slot, retry or varying HTTP message must not page the same cause repeatedly.
    scope = store.digest([fixture, role, cause])
    previous = db.execute(
        "SELECT key,occurred FROM season_alert_causes WHERE cause=?", (scope,)
    ).fetchone()
    if previous and store.instant(stamp) - store.instant(
        previous["occurred"]
    ) < timedelta(hours=1):
        return previous["key"]
    key = store.digest([scope, stamp])
    emit(db, key, fixture, role, reason)
    db.execute(
        "INSERT INTO season_alert_causes VALUES(?,?,?) ON CONFLICT(cause) DO UPDATE "
        "SET key=excluded.key,occurred=excluded.occurred",
        (scope, key, stamp),
    )
    return key


def command_args(command):
    if command is None:
        return None
    argv = shlex.split(command)
    if not argv:
        raise ValueError("alert command must name an executable")
    return argv


def deliver(root, argv, *, key=None):
    from .season import _db

    now = store.instant(store.now_utc())
    stamp = store.iso(now)
    with closing(_db(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        # New installations do not replay an entire season of old local diagnostics.
        row = db.execute(
            "SELECT d.key,d.attempts,a.occurred,a.role,a.fixture,a.reason FROM season_alert_delivery d "
            "JOIN season_alerts a USING(key) WHERE d.delivered IS NULL AND d.available BETWEEN ? AND ? "
            "AND a.occurred>=? AND (? IS NULL OR d.key=?) ORDER BY d.available,d.key LIMIT 1",
            (
                store.iso(now - timedelta(hours=1)),
                stamp,
                store.iso(now - timedelta(hours=1)),
                key,
                key,
            ),
        ).fetchone()
        if not row:
            return
        attempt = row["attempts"] + 1
        db.execute(
            "UPDATE season_alert_delivery SET attempts=?,available=? WHERE key=?",
            (attempt, store.iso(now + timedelta(seconds=15)), row["key"]),
        )
    event = {k: row[k] for k in ("key", "occurred", "role", "fixture", "reason")}
    event["data_dir"] = str(Path(root).expanduser().resolve())
    try:
        result = subprocess.run(
            argv,
            input=store.canonical(event) + "\n",
            text=True,
            encoding="utf-8",
            timeout=TIMEOUT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
        )
        error = (
            None
            if result.returncode == 0
            else f"command exited with status {result.returncode}"
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        error = type(exc).__name__ + (
            f": {exc.strerror}" if isinstance(exc, OSError) else ""
        )
    end = store.instant(store.now_utc())
    with closing(_db(root)) as db, db:
        db.execute(
            "UPDATE season_alert_delivery SET available=?,delivered=?,error=? "
            "WHERE key=? AND delivered IS NULL AND attempts=?",
            (
                store.iso(end + timedelta(seconds=min(900, 30 * 2 ** min(attempt, 5)))),
                None if error else store.iso(end),
                error,
                row["key"],
                attempt,
            ),
        )
    return {"key": row["key"], "attempt": attempt, "error": error}


def heartbeat(root, *, alert_command=None):
    from .season import _db

    argv = command_args(alert_command)
    now = store.instant(store.now_utc())
    stamp = store.iso(now)
    key = None
    with closing(_db(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT updated FROM season_services WHERE service='heartbeat'"
        ).fetchone()
        last = row[0] if row else None
        stale = not last or (now - store.instant(last)).total_seconds() > 90
        if stale:
            reason = (
                "no coordinator heartbeat"
                if not last
                else "last coordinator heartbeat " + last
            )
            key = report_cause(db, None, "watchdog", "heartbeat_stale", reason, stamp)
    delivery = deliver(root, argv, key=key) if argv else None
    if argv and key and delivery is None:
        delivery = deliver(root, argv)
    return {
        "data_dir": str(Path(root).expanduser().resolve()),
        "observed_at": stamp,
        "heartbeat": last,
        "heartbeat_stale": stale,
        "delivery": delivery,
    }
