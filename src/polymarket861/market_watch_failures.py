"""Persist failed collector work separately from its market and settlement evidence."""

import errno
import json
import logging
import sqlite3
from concurrent.futures import BrokenExecutor
from contextlib import closing
from pathlib import Path
from urllib.error import URLError

from .store import canonical


def recoverable(exc):
    if isinstance(exc, URLError):
        return True
    if isinstance(exc, sqlite3.Error):
        code = getattr(exc, "sqlite_errorcode", None)
        if code is not None:
            return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        return isinstance(exc, sqlite3.OperationalError) and str(exc).lower() in (
            "database is locked",
            "database table is locked",
        )
    if isinstance(exc, OSError):
        # An inaccessible or exhausted local store needs an operator and a process failure.
        if exc.filename is not None and not isinstance(exc, FileNotFoundError):
            return False
        return errno.errorcode.get(exc.errno) not in {
            "EFBIG",
            "EIO",
            "EISDIR",
            "ELOOP",
            "ENAMETOOLONG",
            "ENODEV",
            "ENOSPC",
            "ENOTDIR",
            "ENXIO",
            "EROFS",
            "ESTALE",
            "EDQUOT",
            "EACCES",
            "EPERM",
            "ENOMEM",
            "EMFILE",
            "ENFILE",
        }
    return not isinstance(
        exc, (MemoryError, SystemError, RecursionError, BrokenExecutor)
    )


def record(root, observed, operation, context, details):
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    report = {"observed_at": observed, "operation": operation, **context, **details}
    payload = canonical(report)
    logging.getLogger(__name__).warning("Collector task failed: %s", payload)
    # A transient lock on the evidence archive must not also prevent its diagnosis.
    with closing(sqlite3.connect(root / "watch-health.sqlite3", timeout=30)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS task_failures "
            "(id INTEGER PRIMARY KEY, observed TEXT NOT NULL, report TEXT NOT NULL)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS task_failure_time ON task_failures(observed)"
        )
        db.execute(
            "INSERT INTO task_failures(observed,report) VALUES(?,?)",
            (observed, payload),
        )


def in_window(root, start, end):
    path = (Path(root) / "watch-health.sqlite3").resolve()
    empty = {"count": 0, "latest": []}
    if not path.is_file():
        return empty
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_failures'"
        ).fetchone():
            return empty
        times = (start, end)
        count = db.execute(
            "SELECT count(*) FROM task_failures WHERE observed>=? AND observed<?", times
        ).fetchone()[0]
        rows = db.execute(
            "SELECT report FROM task_failures WHERE observed>=? AND observed<? "
            "ORDER BY observed DESC,id DESC LIMIT 100",
            times,
        )
        return {"count": count, "latest": [json.loads(row[0]) for row in rows]}
