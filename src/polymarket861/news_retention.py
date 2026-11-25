"""Lossless cold storage of superseded, completed-fixture availability evidence."""

import gzip
import hashlib
import os
from datetime import timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import store
from .blob_storage import blob_name


_COLD = """WITH cold AS (
SELECT a.seq,a.fixture_id,a.original_sha256 FROM news_articles a
WHERE a.source='touchline-availability' AND a.recorded_at<=?
AND EXISTS (SELECT 1 FROM news_articles n WHERE n.season=a.season AND n.team_key=a.team_key
    AND n.source=a.source AND n.external_id=a.external_id AND n.recorded_at<=?
    AND (n.updated_at>a.updated_at OR (n.updated_at=a.updated_at AND n.seq>a.seq)))
AND EXISTS (SELECT 1 FROM result_versions r JOIN result_schedule_refs s USING(fixture_id,version)
    WHERE r.fixture_id=a.fixture_id AND r.finalized_at<=? AND r.recorded_at<=?
    AND s.kickoff=(SELECT f.kickoff FROM fixture_versions f WHERE f.fixture_id=a.fixture_id
        ORDER BY f.version DESC LIMIT 1)
    AND coalesce((SELECT d.held FROM result_dispositions d WHERE d.fixture_id=r.fixture_id
        AND d.kickoff=s.kickoff ORDER BY d.at DESC LIMIT 1),0)=0))
"""


def _sync_directory(directory):
    # Persist the new directory entry before dropping the old encoding.
    if os.name == "posix":
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _compact(root, sha):
    raw = Path(root) / blob_name(sha)
    packed = raw.with_suffix(".gz")
    if raw.is_symlink() or packed.is_symlink():
        raise ValueError("unexpected symlink in the news archive")
    payload = store.read_blob(root, sha)
    if not raw.is_file():
        return 0
    encoded = gzip.compress(payload, compresslevel=6, mtime=0)
    if len(encoded) >= len(payload):
        return 0
    if not packed.exists():
        with NamedTemporaryFile(dir=raw.parent, delete=False) as temp:
            staging = Path(temp.name)
            try:
                temp.write(encoded)
                temp.flush()
                os.fsync(temp.fileno())
            except BaseException:
                staging.unlink(missing_ok=True)
                raise
        try:
            os.replace(staging, packed)
        finally:
            staging.unlink(missing_ok=True)
    # Both representations exist at this point. The packed bytes must reopen,
    # decompress and reproduce the content address before the raw path is retired.
    with gzip.open(packed, "rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
            raise ValueError("cold news original failed SHA-256 verification")
    stored_size = packed.stat().st_size
    if stored_size >= len(payload):
        return 0
    _sync_directory(raw.parent)
    try:
        raw.unlink()
    except FileNotFoundError:  # Another compactor completed this digest.
        return 0
    _sync_directory(raw.parent)
    return len(payload) - stored_size


def retention(data_dir, *, days=90, backup_to=None, compact=False, limit=500):
    from .news import _db

    if type(days) is not int or not 1 <= days <= 36500 or type(limit) is not int or not 1 <= limit <= 50000:
        raise ValueError("news retention requires 1–36500 days and a limit of 1–50000 originals")
    before = store.iso(store.instant(store.now_utc()) - timedelta(days=days))
    with _db(data_dir) as db:
        events = db.execute("SELECT 1 FROM sqlite_master WHERE name='news_event_updates'").fetchone()
        event_ref = "OR EXISTS (SELECT 1 FROM news_event_updates u WHERE u.fixture_id=c.fixture_id)" if events else ""
        row = db.execute(_COLD + """
            SELECT count(*) superseded_versions,count(DISTINCT c.fixture_id) completed_fixtures,
            coalesce(sum((SELECT max(o.size) FROM observations o WHERE o.source='epl-team-news'
              AND o.sha256=c.original_sha256)),0) original_bytes,
            coalesce(sum(EXISTS (SELECT 1 FROM forecast_seals s WHERE s.fixture_id=c.fixture_id) """
            + event_ref + """),0) versions_on_sealed_or_event_fixtures FROM cold c""", (before,) * 4).fetchone()
        candidates = []
        if compact:
            candidates = [r[0] for r in db.execute(_COLD + """
                SELECT c.original_sha256 FROM cold c WHERE NOT EXISTS (SELECT 1 FROM news_articles a
                    WHERE a.original_sha256=c.original_sha256 AND a.seq NOT IN (SELECT seq FROM cold))
                EXCEPT SELECT sha256 FROM observations WHERE source<>'epl-team-news'
                EXCEPT SELECT original_sha256 FROM news_wire_compactions ORDER BY 1 LIMIT ?""",
                ((before,) * 4) + (limit,))]
    result = {"mode": "compact" if compact else "inventory", "superseded_before": before, **dict(row),
              "deleted_versions": 0, "compacted_originals": 0, "encoded_bytes_saved": 0}
    if backup_to:
        from .operations import backup_state

        result["backup"] = backup_state(data_dir, backup_to, compress=True)
    for sha in candidates:
        savings = _compact(data_dir, sha)
        with _db(data_dir, write=True) as db:
            db.execute("INSERT OR IGNORE INTO news_wire_compactions VALUES (?,?)", (sha, store.now_utc()))
        result["compacted_originals"] += bool(savings)
        result["encoded_bytes_saved"] += savings
    return result
