"""Claim a dedicated NBA data root and refuse cross-sport database access."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from pathlib import Path

MARKER = ".pm861-domain"
DATABASE = "nba.sqlite3"
_FOOTBALL_DATABASE = "runtime.sqlite3"
APPLICATION_ID = 0x4E424131  # NBA1
_CLAIM_PREFIX = ".pm861-domain-claim-"


def _owner(root: Path) -> str | None:
    marker = root / MARKER
    try:
        entry = marker.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(entry.st_mode) or entry.st_size > 16:
        raise ValueError(f"invalid domain marker in {root}")
    try:
        domain = marker.read_bytes()
    except FileNotFoundError:
        # A competing legacy football initializer won the database pathname.
        return None
    if domain == b"nba\n":
        return "nba"
    if domain == b"football\n":
        return "football"
    raise ValueError(f"unrecognized domain marker in {root}")


def _root(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def check_football_root(path: Path | str) -> None:
    """Read-only guard, including unmarked NBA databases and subdirectories."""
    root = _root(path)
    paths = [root]
    runtime = root / "runtime.sqlite3"
    if runtime.is_symlink():
        target = runtime.resolve()
        if target.name == DATABASE:
            raise ValueError("football cannot open a linked NBA database")
        paths.append(target.parent)
    for path_root in paths:
        for parent in (path_root, *path_root.parents):
            if _owner(parent) == "nba" or (parent / DATABASE).is_file():
                raise ValueError(f"football cannot use NBA data directory {parent}")
    # A copied or hard-linked NBA database may have lost both its marker and
    # its original name. Inspect the SQLite header before opening it for write.
    if runtime.is_file():
        with runtime.open("rb") as file:
            file.seek(68)  # SQLite file header application_id, big endian
            if file.read(4) == APPLICATION_ID.to_bytes(4, "big"):
                raise ValueError("football cannot open an NBA database")


def _reserved_for_nba(path: Path) -> bool:
    """Only a real, empty directory in an NBA-owned root is a reservation."""
    return (stat.S_ISDIR(path.lstat().st_mode)
            and _owner(path.parent) == "nba"
            and next(path.iterdir(), None) is None)


def check_nba_root(path: Path | str) -> None:
    """Only an empty root or an existing NBA root may receive NBA state."""
    root = _root(path)
    for parent in (root, *root.parents):
        if _owner(parent) == "football":
            raise ValueError(f"NBA cannot use football data directory {parent}")
        if parent.name == _FOOTBALL_DATABASE and _owner(parent.parent) == "nba":
            raise ValueError(f"NBA cannot write inside reserved football database path {parent}")
        runtime = parent / _FOOTBALL_DATABASE
        try:
            reserved = _reserved_for_nba(runtime)
        except FileNotFoundError:
            continue
        if not reserved:
            raise ValueError(f"NBA cannot use football data directory {parent}")
    if not root.exists():
        return
    if _owner(root) != "nba":
        unexpected = next((p for p in root.iterdir() if not p.name.startswith(_CLAIM_PREFIX)), None)
        # The other initializer may have atomically published its marker during the scan.
        if unexpected is not None and _owner(root) != "nba":
            raise ValueError(f"NBA requires a dedicated empty directory: {root}")
    for name in (DATABASE, DATABASE + "-wal", DATABASE + "-shm", DATABASE + "-journal"):
        f = root / name
        try:
            entry = f.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink > 1:
            raise ValueError(f"invalid linked NBA database file: {f}")


def _claim(root: Path, domain: str) -> bool:
    """Return whether this call atomically published the ownership marker."""
    published = False
    owner = _owner(root)
    if owner is None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=root, prefix=_CLAIM_PREFIX, delete=False) as stream:
            tmp = Path(stream.name)
            stream.write((domain + "\n").encode("ascii"))
        try:
            try:
                os.link(tmp, root / MARKER)
                published = True
            except FileExistsError:
                pass
        finally:
            tmp.unlink(missing_ok=True)
        owner = _owner(root)
    if owner != domain:
        raise ValueError(f"{domain} cannot use {owner} data directory {root}")
    return published


def _claim_nba(root: Path) -> None:
    published = _claim(root, "nba")
    # The released football wheel opens this exact pathname directly with
    # SQLite. mkdir claims it atomically; a competing regular file always wins
    # intact, and legacy SQLite cannot write through an established directory.
    runtime = root / _FOOTBALL_DATABASE
    try:
        runtime.mkdir(mode=0o500)
    except FileExistsError:
        if not _reserved_for_nba(runtime):
            if published:
                (root / MARKER).unlink()
            raise ValueError(f"NBA cannot use football data directory {root}") from None


def prepare_football_root(path: Path | str) -> None:
    root = _root(path)
    check_football_root(root)
    # A pre-existing football database already establishes ownership. Its normal
    # opening and backup behavior does not need a migration to a marked root.
    runtime = root / _FOOTBALL_DATABASE
    if runtime.is_dir():
        raise ValueError(f"football cannot use reserved database directory {runtime}")
    if not runtime.exists():
        _claim(root, "football")


def open_existing(path: Path | str) -> sqlite3.Connection | None:
    root = _root(path)
    check_nba_root(root)
    dbfile = root / DATABASE
    if not dbfile.exists():
        return None
    db = sqlite3.connect(dbfile.as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("not an initialized NBA database")
        db.row_factory = sqlite3.Row
        return db
    except BaseException:
        db.close()
        raise


def connect(path: Path | str) -> sqlite3.Connection:
    root = _root(path)
    check_nba_root(root)
    _claim_nba(root)
    db = sqlite3.connect(root / DATABASE, timeout=30)
    try:
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            with db:
                db.execute("BEGIN IMMEDIATE")
                identity = db.execute("PRAGMA application_id").fetchone()[0]
                if identity != APPLICATION_ID:
                    if identity != 0 or db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                        raise ValueError("unrecognized NBA database contents")
                    db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = sqlite3.Row
        return db
    except BaseException:
        db.close()
        raise
