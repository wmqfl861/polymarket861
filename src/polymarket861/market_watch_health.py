"""Heartbeat of a continuously running domestic market collector, separate from its evidence."""

import json
import logging
import os
import socket
import sqlite3
import time
from contextlib import closing, contextmanager
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


def _scope(leagues, start, end, discover):
    return {
        "leagues": sorted(leagues),
        "start": start,
        "end": end,
        "discover": discover,
    }


@contextmanager
def monitor(root, leagues, start, end, *, continuous, discover):
    """Only a long-running scheduler advancing real ticks can keep its scope healthy."""
    if not continuous:
        yield None
        return
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "watch-health.sqlite3")) as db:
        run = uuid4().hex
        scope = store.canonical(_scope(leagues, start, end, discover))
        started = store.now_utc()
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS watches (id TEXT PRIMARY KEY, scope TEXT NOT NULL, "
                "started TEXT NOT NULL, updated TEXT NOT NULL, state TEXT NOT NULL, ticks INTEGER NOT NULL, "
                "catalog TEXT)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS watches_scope ON watches(scope, updated)"
            )
            db.execute(
                "DELETE FROM watches WHERE updated < ?",
                (store.iso(store.instant(started) - timedelta(days=7)),),
            )
            db.execute(
                "INSERT INTO watches VALUES(?,?,?,?,?,0,NULL)",
                (run, scope, started, started, "starting"),
            )
        next_write = None

        def tick(count, discovery):
            nonlocal next_write
            monotonic = time.monotonic()
            if next_write is not None and monotonic < next_write:
                return
            coverage = (
                None
                if discovery is None
                else {
                    k: bool(v["page_walk_complete"])
                    for k, v in discovery["leagues"].items()
                }
            )
            with db:
                db.execute(
                    "UPDATE watches SET updated=?,state='running',ticks=?,catalog=? WHERE id=?",
                    (store.now_utc(), count, store.canonical(coverage), run),
                )
            _notify("READY=1\nWATCHDOG=1" if next_write is None else "WATCHDOG=1")
            next_write = monotonic + 15

        try:
            yield tick
        finally:
            with db:
                db.execute(
                    "UPDATE watches SET state='stopped',updated=? WHERE id=?",
                    (store.now_utc(), run),
                )
            _notify("STOPPING=1")


def status(root, leagues, start, end, *, discover=True, max_age=180):
    if max_age <= 0:
        raise ValueError("heartbeat maximum age must be positive")
    path = (Path(root) / "watch-health.sqlite3").resolve()
    now = store.now_utc()
    scope = _scope(leagues, start, end, discover)
    result = {
        "scope": scope,
        "observed_at": now,
        "max_age_seconds": max_age,
        "healthy": False,
        "state": "missing",
        "run_id": None,
        "started_at": None,
        "last_heartbeat": None,
        "ticks": 0,
        "catalog_complete_by_league": None,
    }
    if not path.is_file():
        return result
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='watches'"
        ).fetchone():
            return result
        rows = db.execute(
            "SELECT * FROM watches WHERE scope=? ORDER BY updated DESC,rowid DESC",
            (store.canonical(scope),),
        )
        for row in rows:
            age = (store.instant(now) - store.instant(row["updated"])).total_seconds()
            state = row["state"]
            if age < 0:
                state = "clock_conflict"
            elif state in ("starting", "running") and age > max_age:
                state = "stale"
            # When two collectors share a scope, a stopped peer must not hide a live one.
            if result["last_heartbeat"] is None or state == "running":
                result.update(
                    healthy=state == "running",
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
            if result["healthy"]:
                break
    return result
