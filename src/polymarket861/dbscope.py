"""Connection ownership and transaction-local evidence reuse for a single paper fixture."""

import hashlib
import os
import sqlite3
from contextlib import closing, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path


@dataclass
class FixtureWrite:
    root: Path
    db: sqlite3.Connection
    dirty: set = field(default_factory=set)
    checked: dict = field(default_factory=dict)
    recovered_until: dict = field(default_factory=dict)
    originals: dict = field(default_factory=dict)
    deadline: datetime | None = None


_active = ContextVar("pm861_fixture_writer", default=None)


def current(root):
    active = _active.get()
    return active if active is not None and active.root == Path(root).absolute() else None


def for_connection(db):
    active = _active.get()
    return active if active is not None and active.db is db else None


@contextmanager
def bind(root, db):
    state = FixtureWrite(Path(root).absolute(), db)
    token = _active.set(state)
    try:
        yield state
    finally:
        _active.reset(token)


@contextmanager
def reading(root, opener=None):
    active = current(root)
    if active is not None:
        yield active.db
        return
    if opener is None:
        from .store import connect

        opener = connect
    with closing(opener(root)) as db:
        yield db


def checked(root, key, read):
    """Cache immutable evidence within the locked snapshot; never hand a caller the cached object."""
    active = current(root)
    if active is None:
        return read()
    if key not in active.checked:
        active.checked[key] = read()
    return deepcopy(active.checked[key])


class EvidenceChanged(ValueError):
    """An original used by the in-flight paper tick changed before it could commit."""


def _identity(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def verified_bytes(root, path, sha, error):
    from .blob_storage import decoded

    with path.open("rb") as file:
        identity = _identity(os.fstat(file.fileno()))
        payload = decoded(path, file.read())
        if identity != _identity(os.fstat(file.fileno())):
            raise EvidenceChanged(f"archived original {sha} changed while reading")
    if hashlib.sha256(payload).hexdigest() != sha:
        raise ValueError(error)
    active = current(root)
    if active is not None:
        active.originals[path] = (sha, identity)
    return payload


def recheck_originals(batch):
    from .blob_storage import decoded

    # A writer lock protects the SQLite receipt rows, not content-addressed files.
    for path, (sha, identity) in batch.originals.items():
        try:
            if _identity(path.stat()) == identity:
                continue
            if hashlib.sha256(decoded(path, path.read_bytes())).hexdigest() == sha:
                continue
        except (OSError, ValueError):
            pass
        raise EvidenceChanged(f"archived original {sha} changed during the fixture tick")


@cache
def _schema_digest(sql):
    return hashlib.sha256(sql.encode()).hexdigest()


def prepare(db, namespace, sql):
    """Apply transactional, idempotent DDL once per component version in each physical database."""
    sha = _schema_digest(sql)

    def installed():
        try:
            row = db.execute(
                "SELECT digest FROM runtime_schema_versions WHERE component=?", (namespace,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: runtime_schema_versions" not in str(exc):
                raise
            return False
        return row is not None and row[0] == sha

    if installed():
        return
    own = not db.in_transaction
    if own:
        db.execute("BEGIN IMMEDIATE")
    try:
        if own and installed():  # A concurrent opener may have upgraded while we waited.
            db.commit()
            return
        db.execute(
            "CREATE TABLE IF NOT EXISTS runtime_schema_versions("
            "component TEXT PRIMARY KEY,digest TEXT NOT NULL)"
        )
        # executescript commits already-open transactions. All component scripts consist of
        # CREATE statements, which SQLite can execute atomically via individual statements.
        pending = ""
        for line in sql.splitlines(keepends=True):
            pending += line
            if sqlite3.complete_statement(pending):
                db.execute(pending)
                pending = ""
        if pending.strip():
            raise ValueError("incomplete database schema statement")
        db.execute(
            "INSERT INTO runtime_schema_versions VALUES(?,?) "
            "ON CONFLICT(component) DO UPDATE SET digest=excluded.digest", (namespace, sha)
        )
        if own:
            db.commit()
    except BaseException:
        if own:
            db.rollback()
        raise
