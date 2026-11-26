"""Connection ownership and transaction-local evidence reuse for a single paper fixture."""

import hashlib
import os
import sqlite3
from collections import OrderedDict
from contextlib import closing, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path
from threading import Lock


@dataclass
class FixtureWrite:
    root: Path
    db: sqlite3.Connection
    dirty: set = field(default_factory=set)
    checked: dict = field(default_factory=dict)
    recovered_until: dict = field(default_factory=dict)
    originals: dict = field(default_factory=dict)
    unavailable_originals: dict = field(default_factory=dict)
    deadline: datetime | None = None


_active = ContextVar("pm861_fixture_writer", default=None)
_gzip_hashes = OrderedDict()
_gzip_hash_lock = Lock()


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


def _read_representation(root, path, sha):
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        _track_unavailable(root, path, None)
        raise
    with handle as file:
        identity = _identity(os.fstat(file.fileno()))
        raw = file.read()
        if identity != _identity(os.fstat(file.fileno())):
            raise EvidenceChanged(f"archived original {sha} changed while reading")
    return raw, identity


def _track_original(root, path, sha, identity):
    active = current(root)
    if active is not None:
        active.originals[path] = (sha, identity)


def _track_unavailable(root, path, identity):
    active = current(root)
    if active is not None:
        active.unavailable_originals[path] = identity


def verified_bytes(root, path, sha, error):
    from .blob_storage import decoded

    raw, identity = _read_representation(root, path, sha)
    try:
        payload = decoded(path, raw)
        if hashlib.sha256(payload).hexdigest() != sha:
            raise ValueError(error)
    except ValueError:
        _track_unavailable(root, path, identity)
        raise
    _track_original(root, path, sha, identity)
    return payload


def verify_content(root, path, sha, error):
    """Verify the current bytes when a caller only needs proof of their identity."""
    from .blob_storage import decoded

    raw, identity = _read_representation(root, path, sha)
    encoded_sha = hashlib.sha256(raw).hexdigest()
    if path.suffix == ".gz":
        # An identical gzip byte stream decodes to the same original. Retain
        # only hash pairs established by decoding and hashing that exact stream.
        with _gzip_hash_lock:
            known = _gzip_hashes.get(sha) == encoded_sha
            if known:
                _gzip_hashes.move_to_end(sha)
        if not known:
            try:
                if hashlib.sha256(decoded(path, raw)).hexdigest() != sha:
                    raise ValueError(error)
            except ValueError:
                _track_unavailable(root, path, identity)
                raise
            with _gzip_hash_lock:
                _gzip_hashes[sha] = encoded_sha
                _gzip_hashes.move_to_end(sha)
                if len(_gzip_hashes) > 4096:
                    _gzip_hashes.popitem(last=False)
    elif encoded_sha != sha:
        _track_unavailable(root, path, identity)
        raise ValueError(error)
    _track_original(root, path, sha, identity)


def recheck_originals(batch):
    from .blob_storage import decoded

    # A restored or newly preferred encoding can also change an earlier negative result.
    for path, identity in getattr(batch, "unavailable_originals", {}).items():
        raw_path = path.with_suffix("") if path.suffix == ".gz" else path
        packed = raw_path.with_suffix(".gz")
        if identity is None:
            if not raw_path.is_file() and not packed.is_file():
                continue
        else:
            try:
                if (path == raw_path or not raw_path.is_file()) and _identity(path.stat()) == identity:
                    continue
            except OSError:
                pass
        raise EvidenceChanged(f"previously unavailable archive {path.name} changed during verification")

    # A writer lock protects the SQLite receipt rows, not content-addressed files.
    for path, (sha, identity) in batch.originals.items():
        try:
            preferred = path.with_suffix("") if path.suffix == ".gz" and path.with_suffix("").is_file() else path
            if preferred == path and _identity(path.stat()) == identity:
                continue
            if hashlib.sha256(decoded(preferred, preferred.read_bytes())).hexdigest() == sha:
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
