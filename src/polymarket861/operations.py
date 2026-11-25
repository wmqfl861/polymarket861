"""Back up the private runtime and restore it to a new directory."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from .store import blob_path, canonical, connect, now_utc

PORTABLE_FOLDERS = ("datasets", "sources", "openfootball", "job-work", "manifests")
NESTED_STORE = "season-catalog"


def checksum(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _staging(destination: Path) -> Path:
    if destination.exists():
        raise ValueError("destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".pm861-stage-", dir=destination.parent))


def _entry(root, name, sha):
    path = PurePosixPath(name)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != name
        or "\\" in name
        or ":" in name
        or len(sha) != 64
        or any(c not in "0123456789abcdef" for c in sha)
    ):
        raise ValueError("unsafe backup path or fingerprint")
    parts = path.parts[1:] if path.parts[0] == NESTED_STORE else path.parts
    if not parts:
        raise ValueError("missing backup path")
    if parts[0] == "archive":
        valid = PurePosixPath(*parts).as_posix() == blob_path(Path("."), sha).as_posix()
    else:
        valid = (
            parts == ("runtime.sqlite3",)
            or len(parts) > 1
            and parts[0] in PORTABLE_FOLDERS
        )
    dest = root.joinpath(*path.parts)
    if not valid or not dest.resolve().is_relative_to(root) or dest.is_symlink():
        raise ValueError("unrecognized backup path: " + name)
    return dest


def verify_backup(backup: Path) -> dict:
    root = backup.resolve()
    info = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if info.get("format") not in (1, 2) or not isinstance(info.get("files"), dict):
        raise ValueError("unrecognized backup manifest")
    files = info["files"]
    if "runtime.sqlite3" not in files:
        raise ValueError("missing database in manifest")
    for name, sha in files.items():
        p = _entry(root, name, sha)
        if not p.is_file() or checksum(p) != sha:
            raise ValueError(f"backup integrity failure: {name}")
    for prefix in (Path("."), Path(NESTED_STORE)):
        rel = (prefix / "runtime.sqlite3").as_posix()
        if rel not in files:
            continue
        uri = (root / rel).as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed")
            required = {
                (prefix / blob_path(Path("."), r[0])).as_posix()
                for r in db.execute("SELECT DISTINCT sha256 FROM observations")
            }
            if required - set(files):
                raise ValueError("a referenced evidence file is missing")
    return {"ok": True, "files": len(files), "created_at": info["created_at"]}


def _snapshot(root, stage, prefix, files):
    destination = stage / prefix
    destination.mkdir(parents=True, exist_ok=True)
    with (
        closing(connect(root)) as source,
        closing(sqlite3.connect(destination / "runtime.sqlite3")) as copy,
    ):
        source.backup(copy)
        copy.execute("PRAGMA journal_mode=DELETE")
        hashes = [
            r[0] for r in copy.execute("SELECT DISTINCT sha256 FROM observations")
        ]
    files[(prefix / "runtime.sqlite3").as_posix()] = checksum(
        destination / "runtime.sqlite3"
    )
    for sha in hashes:
        name = blob_path(Path("."), sha)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
        files[(prefix / name).as_posix()] = sha
    for folder in PORTABLE_FOLDERS:
        for path in sorted((root / folder).rglob("*")):
            if (
                path.is_symlink()
                or not path.is_file()
                or not path.resolve().is_relative_to(root)
            ):
                continue
            name = path.relative_to(root)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            files[(prefix / name).as_posix()] = checksum(target)


def backup_state(data_dir, destination) -> dict:
    root, dest = Path(data_dir).resolve(), Path(destination).resolve()
    if not (root / "runtime.sqlite3").is_file():
        raise ValueError("data directory has no runtime database")
    if dest.is_relative_to(root):
        raise ValueError("choose a backup destination outside the data directory")
    stage = _staging(dest)
    try:
        files = {}
        _snapshot(root, stage, Path("."), files)
        if (root / NESTED_STORE / "runtime.sqlite3").is_file() and not (
            root / NESTED_STORE
        ).is_symlink():
            _snapshot(root / NESTED_STORE, stage, Path(NESTED_STORE), files)
        info = {"format": 2, "created_at": now_utc(), "files": files}
        (stage / "manifest.json").write_text(canonical(info), encoding="utf-8")
        check = verify_backup(stage)
        os.rename(stage, dest)
        return check | {"destination": str(dest)}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def restore_state(backup, destination) -> dict:
    root, dest = Path(backup).resolve(), Path(destination).resolve()
    verification = verify_backup(root)
    if dest.is_relative_to(root):
        raise ValueError("choose a restore destination outside the backup")
    stage = _staging(dest)
    try:
        for name in json.loads((root / "manifest.json").read_text(encoding="utf-8"))[
            "files"
        ]:
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, target)
        shutil.copyfile(root / "manifest.json", stage / "manifest.json")
        verify_backup(stage)
        (stage / "manifest.json").unlink()
        os.rename(stage, dest)
        return verification | {"restored": str(dest)}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def register(subparsers):
    ops = subparsers.add_parser(
        "backup", help="备份及校验运行库、赛程文件和证据"
    ).add_subparsers(required=True)
    create = ops.add_parser("create")
    create.add_argument("destination", type=Path)
    create.set_defaults(func=lambda a: backup_state(a.data_dir, a.destination))
    verify = ops.add_parser("verify")
    verify.add_argument("backup", type=Path)
    verify.set_defaults(func=lambda a: verify_backup(a.backup))
    restore = ops.add_parser("restore")
    restore.add_argument("backup", type=Path)
    restore.add_argument("destination", type=Path)
    restore.set_defaults(func=lambda a: restore_state(a.backup, a.destination))
