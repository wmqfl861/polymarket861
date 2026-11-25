"""Back up the private runtime and restore it to a new directory."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zlib
from contextlib import closing
from pathlib import Path, PurePosixPath

from .blob_storage import blob_name, blob_size, open_blob
from .store import canonical, connect, digest, iso, now_utc

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
        valid = PurePosixPath(*parts).as_posix() == blob_name(sha).as_posix()
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


def _archived_blob(name):
    parts = PurePosixPath(name).parts
    return parts[0] == "archive" or parts[:2] == (NESTED_STORE, "archive")


def _content(root, name, sha, version):
    path = _entry(root, name, sha)
    if version == 3 and _archived_blob(name):
        packed = path.with_name(path.name + ".gz")
        if packed.is_symlink() or not packed.resolve().is_relative_to(root):
            raise ValueError("invalid compressed backup path")
        return gzip.open(packed, "rb")
    return path.open("rb")


def verify_backup(backup: Path) -> dict:
    root = backup.resolve()
    info = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if info.get("format") not in (1, 2, 3) or not isinstance(info.get("files"), dict):
        raise ValueError("unrecognized backup manifest")
    files = info["files"]
    if "runtime.sqlite3" not in files:
        raise ValueError("missing database in manifest")
    for name, sha in files.items():
        try:
            with _content(root, name, sha, info["format"]) as data:
                actual = hashlib.file_digest(data, "sha256").hexdigest()
        except (OSError, EOFError, zlib.error) as exc:
            raise ValueError(f"unreadable backup content: {name}") from exc
        if actual != sha:
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
                (prefix / blob_name(r[0])).as_posix()
                for r in db.execute("SELECT DISTINCT sha256 FROM observations")
            }
            if required - set(files):
                raise ValueError("a referenced evidence file is missing")
    return {"ok": True, "files": len(files), "created_at": info["created_at"]}


def _snapshot(root, stage, prefix, files, compress):
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
        name = blob_name(sha)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open_blob(root, sha) as data:
                if compress:
                    with target.with_suffix(".gz").open("wb") as buf, gzip.GzipFile(
                        filename="", fileobj=buf, mode="wb", mtime=0, compresslevel=6
                    ) as gz:
                        shutil.copyfileobj(data, gz)
                else:
                    with target.open("wb") as out:
                        shutil.copyfileobj(data, out)
        except (EOFError, gzip.BadGzipFile, zlib.error) as exc:
            raise ValueError(f"unreadable archived original: {sha}") from exc
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


def backup_state(data_dir, destination, *, compress=False) -> dict:
    root, dest = Path(data_dir).resolve(), Path(destination).resolve()
    if not (root / "runtime.sqlite3").is_file():
        raise ValueError("data directory has no runtime database")
    if dest.is_relative_to(root):
        raise ValueError("choose a backup destination outside the data directory")
    stage = _staging(dest)
    try:
        files = {}
        _snapshot(root, stage, Path("."), files, compress)
        if (root / NESTED_STORE / "runtime.sqlite3").is_file() and not (
            root / NESTED_STORE
        ).is_symlink():
            _snapshot(root / NESTED_STORE, stage, Path(NESTED_STORE), files, compress)
        info = {"format": 3 if compress else 2, "created_at": now_utc(), "files": files}
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
        info = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        for name, sha in info["files"].items():
            target = _entry(stage, name, sha)
            target.parent.mkdir(parents=True, exist_ok=True)
            with (
                _content(root, name, sha, info["format"]) as data,
                target.open("xb") as restored,
            ):
                shutil.copyfileobj(data, restored)
        (stage / "manifest.json").write_text(
            canonical(dict(info, format=2)), encoding="utf-8"
        )
        verify_backup(stage)
        (stage / "manifest.json").unlink()
        os.rename(stage, dest)
        return verification | {"restored": str(dest)}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def witness_manifest(data_dir, start, end):
    """Commit to sealed prediction digests in a half-open UTC interval."""
    start, end = iso(start), iso(end)
    if start >= end:
        raise ValueError("witness interval must have a positive duration")
    with closing(connect(data_dir)) as db:
        commitments = [
            row[0]
            for row in db.execute(
                "SELECT digest FROM forecast_seals WHERE sealed_at >= ? AND sealed_at < ? ORDER BY digest",
                (start, end),
            )
        ]
    body = {
        "format": "pm861-forecast-commitments-v1",
        "since": start,
        "until": end,
        "digests": commitments,
    }
    return {
        **body,
        "count": len(commitments),
        "manifest_sha256": digest(body),
        "time_witness": "local_only",
    }


def storage_status(data_dir):
    """Measure actual SQLite files and the referenced evidence footprint in each runtime."""
    root = Path(data_dir).resolve()
    if not (root / "runtime.sqlite3").is_file():
        raise ValueError("data directory has no runtime database")
    result = {"measured_at": now_utc(), "stores": {}}
    for label, directory in (("runtime", root), (NESTED_STORE, root / NESTED_STORE)):
        database = directory / "runtime.sqlite3"
        if not database.is_file():
            continue
        by_day = {}
        missing = 0
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            for sha, size, first in db.execute(
                "SELECT sha256,MAX(size),MIN(observed_at) FROM observations GROUP BY sha256"
            ):
                day = by_day.setdefault(first[:10], {"originals": 0, "logical_bytes": 0, "stored_bytes": 0})
                day["originals"] += 1
                day["logical_bytes"] += size
                try:
                    day["stored_bytes"] += blob_size(directory, sha)[1]
                except FileNotFoundError:
                    missing += 1
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            measurements = {
                "first_received_utc_day": dict(sorted(by_day.items())),
                "missing_originals": missing,
                "sqlite_reusable_bytes": page_size * db.execute("PRAGMA freelist_count").fetchone()[0],
            }
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='ma_contract_receipts'").fetchone():
                measurements["contract_receipts"] = db.execute("SELECT count(*) FROM ma_contract_receipts").fetchone()[0]
                measurements["contract_bodies"] = db.execute("SELECT count(*) FROM ma_contract_payloads").fetchone()[0]
        for suffix, key in (("", "database_bytes"), ("-wal", "wal_bytes"), ("-shm", "shm_bytes")):
            try:
                measurements[key] = Path(str(database) + suffix).stat().st_size
            except FileNotFoundError:
                measurements[key] = 0
        result["stores"][label] = measurements
    return result


def register(subparsers):
    storage = subparsers.add_parser("storage", help="实测数据库和归档原件占用").add_subparsers(required=True)
    storage.add_parser("status").set_defaults(func=lambda a: storage_status(a.data_dir))
    witness = subparsers.add_parser("witness", help="导出封存摘要清单")
    manifest = witness.add_subparsers(dest="witness_action", required=True).add_parser(
        "manifest"
    )
    manifest.add_argument("--since", required=True)
    manifest.add_argument("--until", required=True)
    manifest.set_defaults(func=lambda a: witness_manifest(a.data_dir, a.since, a.until))
    ops = subparsers.add_parser(
        "backup", help="备份及校验运行库、赛程文件和证据"
    ).add_subparsers(required=True)
    create = ops.add_parser("create")
    create.add_argument("destination", type=Path)
    create.add_argument(
        "--compress",
        action="store_true",
        help="压缩备份中的行情及来源原件，按未压缩内容的 SHA-256 验证",
    )
    create.set_defaults(
        func=lambda a: backup_state(a.data_dir, a.destination, compress=a.compress)
    )
    verify = ops.add_parser("verify")
    verify.add_argument("backup", type=Path)
    verify.set_defaults(func=lambda a: verify_backup(a.backup))
    restore = ops.add_parser("restore")
    restore.add_argument("backup", type=Path)
    restore.add_argument("destination", type=Path)
    restore.set_defaults(func=lambda a: restore_state(a.backup, a.destination))
