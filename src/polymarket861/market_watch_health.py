"""Scheduler heartbeats and successful league-wide catalog receipts for a watch scope."""

import json
import logging
import os
import signal
import socket
import sqlite3
import threading
import time
from contextlib import ExitStack, closing, contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from . import store


def _notify(message):
    address = os.environ.get("NOTIFY_SOCKET")
    if address and hasattr(socket, "AF_UNIX"):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.settimeout(1)
                sock.sendto(
                    message.encode(),
                    "\0" + address[1:] if address.startswith("@") else address,
                )
        except OSError as exc:
            logging.getLogger(__name__).warning("systemd notification failed: %s", exc)


def _scope(leagues, start, end, discover, rolling=False):
    return {
        "leagues": sorted(leagues),
        "start": "rolling" if rolling else start,
        "end": "rolling" if rolling else end,
        "discover": discover,
    }


def _pid_domain():
    """Linux PID numbers and process start ticks are local to a namespace and boot."""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        namespace = Path("/proc/self/ns/pid").stat().st_ino
    except OSError:
        return None
    return f"{boot}/{namespace}" if boot else None


def _process_start(pid):
    """Return Linux start ticks, -1 for a known exit, or None if unverifiable."""
    try:
        # The process name in field 2 can contain spaces and parentheses.
        fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()
        return -1 if fields[0] in (b"Z", b"X", b"x") else int(fields[19])
    except (FileNotFoundError, ProcessLookupError):
        return -1
    except (OSError, ValueError, IndexError):
        return None


@contextmanager
def stop_signals(continuous):
    """A foreground or background CLI collector can drain and record a manual stop."""
    with ExitStack() as cleanup:
        if continuous and threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                old_handler = signal.signal(signum, signal.default_int_handler)
                cleanup.callback(signal.signal, signum, old_handler)
        yield


def _last_page_receipt(report):
    # Only the terminal page of a successful, gap-free walk establishes catalog age.
    pages = report.get("pages")
    if (report.get("page_walk_complete") is True and report.get("gaps") == [] and pages
        and all(isinstance(page, dict) and page.get("http_fresh") is True for page in pages)):
        try:
            return store.iso(pages[-1]["received_at"])
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            pass
    return None


@contextmanager
def monitor(root, leagues, start, end, *, continuous, discover, rolling=False):
    """Only a continuously running collector advancing real ticks emits heartbeats."""
    if not continuous:
        yield None
        return
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "watch-health.sqlite3")) as db:
        run = uuid4().hex
        selected = _scope(leagues, start, end, discover, rolling)
        scope = store.canonical(selected)
        started = store.iso(store.now_utc())
        db.execute("PRAGMA foreign_keys=ON")
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS watches (id TEXT PRIMARY KEY, scope TEXT NOT NULL, "
                "started TEXT NOT NULL, updated TEXT NOT NULL, state TEXT NOT NULL, ticks INTEGER NOT NULL, "
                "catalog TEXT)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS watches_scope ON watches(scope, updated)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS watch_catalog_success (scope TEXT NOT NULL, "
                "league TEXT NOT NULL, received_at TEXT NOT NULL, PRIMARY KEY (scope, league))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS watch_owners (run TEXT PRIMARY KEY REFERENCES watches(id) "
                "ON DELETE CASCADE, pid_domain TEXT NOT NULL, pid INTEGER NOT NULL, start_ticks INTEGER NOT NULL)"
            )
            db.execute(
                "DELETE FROM watches WHERE updated < ?",
                (store.iso(store.instant(started) - timedelta(days=7)),),
            )
            db.execute(
                "INSERT INTO watches VALUES(?,?,?,?,?,0,NULL)",
                (run, scope, started, started, "starting"),
            )
            domain = _pid_domain()
            ticks = _process_start(os.getpid()) if domain else None
            if ticks is not None and ticks >= 0:
                db.execute(
                    "INSERT INTO watch_owners VALUES(?,?,?,?)",
                    (run, domain, os.getpid(), ticks),
                )
        next_heartbeat = None
        last_discovery = None
        coverage = None

        def tick(count, discovery):
            nonlocal next_heartbeat, last_discovery, coverage
            monotonic = time.monotonic()
            heartbeat_due = next_heartbeat is None or monotonic >= next_heartbeat
            # The scheduler reuses the same report until another traversal finishes.
            new_walk = (
                discover and discovery is not None and discovery is not last_discovery
            )
            if not heartbeat_due and not new_walk:
                return
            receipts = {}
            if new_walk:
                reports = discovery["leagues"]
                coverage = {
                    league: reports[league].get("page_walk_complete") is True
                    for league in selected["leagues"]
                    if league in reports
                }
                receipts = {
                    league: stamp
                    for league in coverage
                    if (stamp := _last_page_receipt(reports[league])) is not None
                }
            with db:
                db.execute("BEGIN IMMEDIATE")
                now = store.iso(store.now_utc())
                db.execute(
                    "UPDATE watches SET updated=?,state='running',ticks=?,catalog=? WHERE id=?",
                    (now, count, store.canonical(coverage), run),
                )
                for league, stamp in receipts.items():
                    if stamp <= now:
                        db.execute(
                            "INSERT INTO watch_catalog_success VALUES(?, ?, ?) "
                            "ON CONFLICT(scope,league) DO UPDATE SET received_at=excluded.received_at "
                            "WHERE received_at < excluded.received_at OR received_at > ?",
                            (scope, league, stamp, now),
                        )
            if new_walk:
                last_discovery = discovery
            if heartbeat_due:
                _notify(
                    "READY=1\nWATCHDOG=1" if next_heartbeat is None else "WATCHDOG=1"
                )
                next_heartbeat = monotonic + 15

        try:
            yield tick
        finally:
            with db:
                db.execute(
                    "UPDATE watches SET state='stopped',updated=? WHERE id=?",
                    (store.iso(store.now_utc()), run),
                )
            _notify("STOPPING=1")


def _catalog_status(leagues, receipts, now, max_age):
    details = {}
    for league in leagues:
        stamp = receipts.get(league)
        age = (now - store.instant(stamp)).total_seconds() if stamp else None
        if age is None:
            state = "missing"
        elif age < 0:
            state = "clock_conflict"
        elif age > max_age:
            state = "stale"
        else:
            state = "fresh"
        details[league] = {"last_success_at": stamp, "age_seconds": age, "state": state}
    states = {row["state"] for row in details.values()}
    overall = next(
        (s for s in ("clock_conflict", "missing", "stale") if s in states), "fresh"
    )
    return overall, details


def status(
    root, leagues, start, end, *, discover=True, max_age=180, max_catalog_age=600,
    rolling=False
):
    if max_age <= 0 or max_catalog_age <= 0:
        raise ValueError("heartbeat and catalog maximum ages must be positive")
    path = (Path(root) / "watch-health.sqlite3").resolve()
    scope = _scope(leagues, start, end, discover, rolling)
    key = store.canonical(scope)
    watches = []
    receipts = {}
    if path.is_file():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "watches" in tables:
                owner_fields, join = (
                    "NULL AS pid_domain, NULL AS pid, NULL AS start_ticks",
                    "",
                )
                if "watch_owners" in tables:
                    owner_fields = "o.pid_domain, o.pid, o.start_ticks"
                    join = "LEFT JOIN watch_owners o ON o.run=w.id"
                watches = db.execute(
                    f"SELECT w.*, {owner_fields} FROM watches w {join} "
                    "WHERE w.scope=? ORDER BY w.updated DESC,w.rowid DESC",
                    (key,),
                ).fetchall()
            # Older health databases only attest to scheduler liveness.
            if discover and "watch_catalog_success" in tables:
                receipts = dict(
                    db.execute(
                        "SELECT league,received_at FROM watch_catalog_success WHERE scope=?",
                        (key,),
                    )
                )
    now = store.now_utc()
    when = store.instant(now)
    catalog_state, catalog_by_league = (
        _catalog_status(scope["leagues"], receipts, when, max_catalog_age)
        if discover
        else ("local_only", None)
    )
    result = {
        "scope": scope,
        "rolling": rolling,
        "observed_at": now,
        "max_age_seconds": max_age,
        "max_catalog_age_seconds": max_catalog_age if discover else None,
        "healthy": False,
        "scheduler_alive": False,
        "catalog_healthy": catalog_state == "fresh" if discover else None,
        "catalog_state": catalog_state,
        "catalog_by_league": catalog_by_league,
        "state": "missing",
        "run_id": None,
        "started_at": None,
        "last_heartbeat": None,
        "ticks": 0,
        "catalog_complete_by_league": None,
    }
    domain = _pid_domain() if watches else None
    for row in watches:
        age = (when - store.instant(row["updated"])).total_seconds()
        state = row["state"]
        if age < 0:
            state = "clock_conflict"
        elif state in ("starting", "running") and age > max_age:
            state = "stale"
        elif (
            state in ("starting", "running") and domain and row["pid_domain"] == domain
        ):
            ticks = _process_start(row["pid"])
            if ticks is not None and ticks != row["start_ticks"]:
                state = "exited"
        # A stopped or stale peer must not hide a live collector sharing this scope.
        if result["last_heartbeat"] is None or state == "running":
            result.update(
                scheduler_alive=state == "running",
                state=state,
                run_id=row["id"],
                started_at=row["started"],
                last_heartbeat=row["updated"],
                age_seconds=age,
                ticks=row["ticks"],
                catalog_complete_by_league=json.loads(row["catalog"])
                if row["catalog"]
                else None,
            )
        if result["scheduler_alive"]:
            break
    result["healthy"] = result["scheduler_alive"] and (
        not discover or result["catalog_healthy"]
    )
    return result
