"""Shared persistent evidence, paper trial state and identities."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from . import dbscope
from .blob_storage import blob_path, read_blob, representation

PROTOCOL = "epl-v1.1"


def instant(value: str | datetime) -> datetime:
    stamp = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
        raise ValueError("a timezone-aware timestamp is required")
    return stamp.astimezone(timezone.utc)


def iso(value: str | datetime) -> str:
    return instant(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def now_utc() -> str:
    return iso(datetime.now(timezone.utc))


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    kind TEXT NOT NULL, key TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS observations (
    source TEXT NOT NULL, observed_at TEXT NOT NULL, sha256 TEXT NOT NULL,
    media_type TEXT NOT NULL, size INTEGER NOT NULL, metadata TEXT NOT NULL,
    UNIQUE(source, observed_at, sha256, metadata)
);
CREATE TABLE IF NOT EXISTS fixture_versions (
    fixture_id TEXT NOT NULL, season TEXT NOT NULL, round_id INTEGER NOT NULL,
    home TEXT NOT NULL, away TEXT NOT NULL, kickoff TEXT NOT NULL, known_at TEXT NOT NULL,
    source TEXT NOT NULL, locator TEXT NOT NULL, version INTEGER NOT NULL,
    PRIMARY KEY(fixture_id, version), UNIQUE(fixture_id, known_at)
);
CREATE TABLE IF NOT EXISTS result_versions (
    fixture_id TEXT NOT NULL, version INTEGER NOT NULL, home_goals INTEGER NOT NULL,
    away_goals INTEGER NOT NULL, finalized_at TEXT NOT NULL, published_at TEXT NOT NULL,
    received_at TEXT NOT NULL, recorded_at TEXT NOT NULL, source TEXT NOT NULL, locator TEXT NOT NULL,
    PRIMARY KEY(fixture_id, version)
);
CREATE INDEX IF NOT EXISTS fixture_at_kickoff ON fixture_versions(kickoff,known_at,fixture_id);
CREATE INDEX IF NOT EXISTS observations_receipt ON observations(source,sha256,observed_at);
CREATE TABLE IF NOT EXISTS core_metadata (name TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedule_receipts (
    fixture_id TEXT NOT NULL,version INTEGER NOT NULL,recorded_at TEXT NOT NULL,precision TEXT NOT NULL,
    PRIMARY KEY(fixture_id,version)
);
CREATE TABLE IF NOT EXISTS result_schedule_refs (
    fixture_id TEXT NOT NULL,version INTEGER NOT NULL,kickoff TEXT NOT NULL,PRIMARY KEY(fixture_id,version)
);
CREATE TABLE IF NOT EXISTS result_dispositions (
    fixture_id TEXT NOT NULL, kickoff TEXT NOT NULL, at TEXT NOT NULL,
    held INTEGER NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(fixture_id,kickoff,at)
);
CREATE TABLE IF NOT EXISTS result_time_grades (
    fixture_id TEXT NOT NULL,version INTEGER NOT NULL,grade TEXT NOT NULL,PRIMARY KEY(fixture_id,version)
);
CREATE TABLE IF NOT EXISTS model_recipes (
    version TEXT PRIMARY KEY, recipe TEXT NOT NULL, registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS builtin_model_releases (
    id INTEGER PRIMARY KEY, version TEXT NOT NULL REFERENCES model_recipes(version), effective_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forecast_seals (
    fixture_id TEXT NOT NULL, kickoff TEXT NOT NULL, cutoff TEXT NOT NULL, horizon TEXT NOT NULL,
    model_version TEXT NOT NULL, protocol TEXT NOT NULL, mode TEXT NOT NULL, sealed_at TEXT NOT NULL,
    digest TEXT NOT NULL UNIQUE, data_json TEXT NOT NULL,
    PRIMARY KEY(fixture_id, kickoff, horizon, model_version, protocol, mode)
);
CREATE TABLE IF NOT EXISTS trial_activations (
    protocol TEXT NOT NULL, starts_at TEXT NOT NULL, recorded_at TEXT NOT NULL, manifest TEXT NOT NULL,
    PRIMARY KEY(protocol, starts_at)
);
"""


def connect(data_dir: Path | str) -> sqlite3.Connection:
    root = Path(data_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(root / "runtime.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")
        # Setting WAL on a new file can return SQLITE_BUSY immediately even with
        # a busy handler when another process is opening the same database.
        deadline = time.monotonic() + 30
        while True:
            try:
                if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                    db.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                if (
                    code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(0.025)
        dbscope.prepare(db, "core", _SCHEMA)
        if not db.execute(
            "SELECT 1 FROM core_metadata WHERE name='schedule_receipts'"
        ).fetchone():
            with db:
                db.execute("BEGIN IMMEDIATE")
                # Old local schedules only gain a provable local receipt at migration.
                db.execute(
                    "INSERT OR IGNORE INTO schedule_receipts SELECT fixture_id,version,?,"
                    "'minute' FROM fixture_versions",
                    (now_utc(),),
                )
                db.execute("""INSERT OR IGNORE INTO result_schedule_refs
                    SELECT r.fixture_id,r.version,
                    (SELECT f.kickoff FROM fixture_versions f WHERE f.fixture_id=r.fixture_id
                     AND f.known_at<=r.recorded_at AND f.kickoff<r.finalized_at
                     ORDER BY f.known_at DESC LIMIT 1) AS event_kickoff
                    FROM result_versions r WHERE event_kickoff IS NOT NULL""")
                db.execute(
                    "INSERT OR IGNORE INTO core_metadata VALUES ('schedule_receipts','1')"
                )
    except BaseException:
        db.close()
        raise
    return db


def put_report(db, kind: str, key: str, payload: Any) -> None:
    db.execute(
        "INSERT INTO reports VALUES (?,?,?,?) ON CONFLICT(kind,key) DO UPDATE SET "
        "updated_at=excluded.updated_at,payload=excluded.payload",
        (kind, key, now_utc(), canonical(payload)),
    )


def save_report(data_dir, kind: str, key: str, payload: Any) -> None:
    with closing(connect(data_dir)) as db, db:
        put_report(db, kind, key, payload)


def list_reports(data_dir, kind: str | None = None) -> list[dict]:
    sql = "SELECT * FROM reports" + (" WHERE kind = ?" if kind is not None else "")
    with closing(connect(data_dir)) as db:
        rows = db.execute(
            sql + " ORDER BY updated_at DESC, kind, key",
            (kind,) if kind is not None else (),
        )
        return [dict(row) | {"payload": json.loads(row["payload"])} for row in rows]


def builtin_model_at(db, at=None):
    row = db.execute(
        "SELECT version FROM builtin_model_releases WHERE effective_at<=? ORDER BY effective_at DESC,id DESC LIMIT 1",
        (iso(at or now_utc()),),
    ).fetchone()
    return row[0] if row else "B1"


def archive_bytes(
    data_dir,
    source,
    payload: bytes,
    *,
    observed_at=None,
    media_type="application/json",
    metadata=None,
) -> dict:
    if not source or not isinstance(payload, bytes):
        raise ValueError("archive requires a source and bytes")
    observed = iso(observed_at or now_utc())
    sha = hashlib.sha256(payload).hexdigest()
    path = blob_path(data_dir, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path, stored = representation(path, payload)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            try:
                stream.write(stored)
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                stream.close()
        try:
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    if hashlib.sha256(read_blob(data_dir, sha)).hexdigest() != sha:
        raise ValueError(f"stored evidence failed hash verification: {sha}")
    with closing(connect(data_dir)) as db, db:
        db.execute(
            "INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?)",
            (
                source,
                observed,
                sha,
                media_type,
                len(payload),
                canonical(metadata or {}),
            ),
        )
    return {
        "sha256": sha,
        "source": source,
        "observed_at": observed,
        "path": str(path),
        "size": len(payload),
        "stored_size": path.stat().st_size,
        "encoding": "gzip" if path.suffix == ".gz" else "identity",
    }


def register_fixture(data_dir, row: dict) -> dict:
    """Keep every schedule revision. The stable identity survives a postponement."""
    if row.get("source") == "openligadb+gamma-schedule":
        raise ValueError("this schedule source requires native OpenLigaDB and Gamma receipts")
    with closing(connect(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        return register_fixture_db(db, row, recorded_at=now_utc())


def register_fixture_db(db, row, *, recorded_at):
    """Append a schedule and its original receipt in an existing write transaction."""
    values = {
        k: str(row[k]).strip()
        for k in ("fixture_id", "season", "home", "away", "source", "locator")
    }
    if not all(values.values()) or values["home"] == values["away"]:
        raise ValueError("fixture requires distinct teams, identity and evidence")
    precision = row.get("precision", "minute")
    if precision not in ("date", "minute", "second"):
        raise ValueError("unknown kickoff precision")
    rnd = row["round_id"]
    if isinstance(rnd, bool) or not isinstance(rnd, int) or not 1 <= rnd <= 38:
        raise ValueError("EPL round_id must be an integer from 1 to 38")
    values.update(
        round_id=rnd, kickoff=iso(row["kickoff"]), known_at=iso(row["known_at"])
    )
    if values["known_at"] > recorded_at:
        raise ValueError("future schedule publication")
    old = db.execute(
        "SELECT * FROM fixture_versions WHERE fixture_id=? ORDER BY version DESC LIMIT 1",
        (values["fixture_id"],),
    ).fetchone()
    if old:
        if any(old[k] != values[k] for k in ("season", "home", "away")):
            raise ValueError("a fixture identity cannot change teams or season")
        same = db.execute(
            "SELECT * FROM fixture_versions WHERE fixture_id=? AND known_at=?",
            (values["fixture_id"], values["known_at"]),
        ).fetchone()
        if same:
            if all(same[k] == v for k, v in values.items()):
                return dict(same)
            raise ValueError(
                "conflicting schedule values for the same publication time"
            )
    values["version"] = old["version"] + 1 if old else 1
    db.execute(
        "INSERT INTO fixture_versions VALUES "
        "(:fixture_id,:season,:round_id,:home,:away,:kickoff,:known_at,:source,:locator,:version)",
        values,
    )
    recorded = recorded_at
    db.execute(
        "INSERT INTO schedule_receipts VALUES (?,?,?,?)",
        (values["fixture_id"], values["version"], recorded, precision),
    )
    return values | {"recorded_at": recorded, "precision": precision}


def fixture_at(data_dir, fixture_id, at=None, *, prospective=False) -> dict:
    with dbscope.reading(data_dir) as db:
        cutoff = iso(at or now_utc())
        query = (
            "SELECT f.*,s.recorded_at,s.precision FROM fixture_versions f "
            "JOIN schedule_receipts s USING(fixture_id,version) WHERE f.fixture_id=? AND f.known_at<=? "
        )
        args = (fixture_id, cutoff)
        if prospective:
            query += "AND s.recorded_at<=? "
            args += (cutoff,)
        row = db.execute(query + "ORDER BY f.known_at DESC LIMIT 1", args).fetchone()
        if prospective and row and row["source"] == "openligadb+gamma-schedule":
            from . import schedule_consensus
            schedule_consensus.attest(data_dir,db,row,cutoff)
    if not row:
        raise ValueError("no known schedule for this fixture at that time")
    return dict(row)


def record_result(data_dir, outcome: dict) -> dict:
    """Record the independent scorer's evidence, including corrections as new versions."""
    if outcome.get("source") in {"openligadb", "manual-regulation"}:
        raise ValueError(
            "reserved result source requires the verified result-feed or licensed manual batch"
        )
    with closing(connect(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        return record_result_db(db, outcome, recorded_at=now_utc())


def record_result_db(db, outcome, *, recorded_at):
    """Append one version in the caller's existing write transaction."""
    item = {
        k: outcome[k]
        for k in ("fixture_id", "home_goals", "away_goals", "source", "locator")
    }
    if not item["source"] or not item["locator"]:
        raise ValueError("a result needs a source and a locator")
    if any(
        type(item[k]) is not int or item[k] < 0 for k in ("home_goals", "away_goals")
    ):
        raise ValueError("goals must be non-negative integers")
    grade = "observed"
    if "archive_at" in outcome:
        grade = "archived_upper_bound"
        item.update(
            finalized_at=iso(outcome["finalized_by"]),
            published_at=iso(outcome["published_by"]),
            received_at=iso(outcome["archive_at"]),
        )
    else:
        for k in ("finalized_at", "published_at", "received_at"):
            item[k] = iso(outcome[k])
    fixed = db.execute(
        "SELECT * FROM fixture_versions WHERE fixture_id=? ORDER BY known_at DESC LIMIT 1",
        (item["fixture_id"],),
    ).fetchone()
    if (
        not fixed
        or not fixed["kickoff"]
        < item["finalized_at"]
        <= item["published_at"]
        <= item["received_at"]
        <= recorded_at
    ):
        raise ValueError(
            "result time sequence must be kickoff < final <= published <= received <= now"
        )
    if outcome.get("kickoff") and iso(outcome["kickoff"]) != fixed["kickoff"]:
        raise ValueError("result is for a different schedule revision")
    if fixed["known_at"] > item["received_at"] and fixed["version"] > 1:
        raise ValueError("result predates the latest schedule revision")
    row = db.execute(
        "SELECT * FROM result_versions WHERE fixture_id=? ORDER BY version DESC LIMIT 1",
        (item["fixture_id"],),
    ).fetchone()
    if row:
        if all(item[k] == row[k] for k in item):
            return dict(row)
        if (item["published_at"], item["received_at"]) <= (
            row["published_at"],
            row["received_at"],
        ):
            raise ValueError("an older result cannot supersede a later correction")
    item.update(version=row["version"] + 1 if row else 1, recorded_at=recorded_at)
    db.execute(
        "INSERT INTO result_versions VALUES (:fixture_id,:version,:home_goals,:away_goals,"
        ":finalized_at,:published_at,:received_at,:recorded_at,:source,:locator)",
        item,
    )
    db.execute(
        "INSERT INTO result_schedule_refs VALUES (?,?,?)",
        (item["fixture_id"], item["version"], fixed["kickoff"]),
    )
    db.execute(
        "INSERT INTO result_time_grades VALUES (?,?,?)",
        (item["fixture_id"], item["version"], grade),
    )
    return item | {"time_grade": grade}


def eligible_fixtures(
    data_dir,
    *,
    season: str | None = None,
    horizon="T-60m",
    protocol=PROTOCOL,
    mode=None,
) -> list[dict]:
    with closing(connect(data_dir)) as db:
        return eligible_fixture_rows(db, season=season)


def eligible_fixture_rows(db, *, season=None):
    # The denominator is the schedule, independent of a candidate's prediction coverage.
    query = """WITH f AS (
        SELECT *,row_number() OVER(PARTITION BY fixture_id ORDER BY known_at DESC) AS n FROM fixture_versions
    ), r AS (
        SELECT r.*,link.kickoff,row_number() OVER(PARTITION BY r.fixture_id,link.kickoff ORDER BY r.version DESC) AS n
        FROM result_versions r JOIN result_schedule_refs link USING(fixture_id,version)
    ) , disposition AS (
        SELECT *,row_number() OVER(PARTITION BY fixture_id,kickoff ORDER BY at DESC) AS n FROM result_dispositions
    ) SELECT f.fixture_id,f.season,f.round_id,f.home,f.away,f.kickoff,f.known_at,
        CASE WHEN coalesce(d.held,0)=0 THEN r.home_goals END AS home_goals,
        CASE WHEN coalesce(d.held,0)=0 THEN r.away_goals END AS away_goals,
        d.reason AS result_disposition, r.finalized_at,r.received_at,r.version AS result_version,s.precision,s.recorded_at,
        f.version AS schedule_version,r.source AS result_source,r.locator AS result_locator
        FROM f JOIN schedule_receipts s ON f.fixture_id=s.fixture_id AND f.version=s.version
        LEFT JOIN r ON f.fixture_id=r.fixture_id AND f.kickoff=r.kickoff AND r.n=1
        LEFT JOIN disposition d ON f.fixture_id=d.fixture_id AND f.kickoff=d.kickoff AND d.n=1 WHERE f.n=1"""
    args = ()
    if season is not None:
        query += " AND f.season=?"
        args = (season,)
    return [
        dict(r) for r in db.execute(query + " ORDER BY f.kickoff,f.fixture_id", args)
    ]


def evaluation_rows(
    data_dir, version, *, horizon="T-60m", mode="formal", protocol=PROTOCOL
):
    with closing(connect(data_dir)) as db:
        return scored_fixture_rows(
            db, eligible_fixture_rows(db), version, horizon, mode, protocol
        )


def scored_fixture_rows(
    db, fixtures, version, horizon, mode, protocol=PROTOCOL, *, one_round=False
):
    from .paired_proof import from_seal

    eligible = {row["fixture_id"]: row for row in fixtures}
    if not eligible:
        return []
    sql = "SELECT * FROM forecast_seals WHERE model_version=? AND horizon=? AND mode=? AND protocol=?"
    args = [version, horizon, mode, protocol]
    if one_round:
        sql += " AND fixture_id IN (" + ",".join("?" for _ in eligible) + ")"
        args.extend(eligible)
    sealed = db.execute(sql + " ORDER BY kickoff,fixture_id", args)
    result = []
    for entry in sealed:
        fixture = eligible.get(entry["fixture_id"])
        if (
            not fixture
            or fixture["home_goals"] is None
            or fixture["kickoff"] != entry["kickoff"]
        ):
            continue
        payload = json.loads(entry["data_json"])
        if digest(payload) != entry["digest"]:
            raise ValueError("forecast evidence hash mismatch")
        if mode != "replay" and entry["sealed_at"] > entry["cutoff"]:
            continue
        result.append(
            fixture
            | {
                k: entry[k]
                for k in ("cutoff", "sealed_at", "digest", "model_version", "mode")
            }
            | {
                "probabilities": payload["probabilities"],
                "recipe_digest": payload["recipe_digest"],
                "comparison_proof": from_seal(payload),
            }
        )
    return result


def check_formal_forecast(data_dir, forecast_digest, fixture_id, kickoff, decision_at):
    key = ("sealed_forecast", forecast_digest, fixture_id, iso(kickoff), iso(decision_at))
    return dbscope.checked(data_dir, key, lambda: _checked_forecast(
        data_dir, forecast_digest, fixture_id, kickoff, decision_at
    ))


def _checked_forecast(data_dir, forecast_digest, fixture_id, kickoff, decision_at):
    with dbscope.reading(data_dir) as db:
        row = db.execute(
            "SELECT * FROM forecast_seals WHERE digest=? AND fixture_id=? AND kickoff=? "
            "AND cutoff=? AND protocol=? AND horizon='T-60m' AND mode='formal'",
            (forecast_digest, fixture_id, iso(kickoff), iso(decision_at), PROTOCOL),
        ).fetchone()
    if not row or row["sealed_at"] > row["cutoff"]:
        raise ValueError("missing timely formal forecast")
    payload = json.loads(row["data_json"])
    if digest(payload) != forecast_digest:
        raise ValueError("invalid sealed forecast digest")
    manifest_id = payload.get("input_manifest_sha256")
    with dbscope.reading(data_dir) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='football_input_manifests'"
        ).fetchone()
        manifest = (
            db.execute(
                "SELECT manifest,observed_at FROM football_input_manifests WHERE digest=?",
                (manifest_id,),
            ).fetchone()
            if exists and manifest_id
            else None
        )
    if (
        not manifest
        or digest(json.loads(manifest["manifest"])) != manifest_id
        or instant(manifest["observed_at"]) > instant(row["cutoff"])
    ):
        raise ValueError("the sealed forecast has no verified timely input manifest")
    if payload.get("source_health", {}).get("critical_stale"):
        raise ValueError("the sealed forecast identified a critical source gap")
    for side in ("home", "away"):
        lineup = payload["lineups"][side]
        if (
            not lineup["confirmed"]
            or not lineup["received_at"]
            or instant(lineup["received_at"]) > instant(row["cutoff"])
        ):
            raise ValueError("two timely confirmed lineups are required")
    return payload


def require_formal(data_dir, *, at=None) -> dict:
    when = iso(at or now_utc())
    batch = dbscope.current(data_dir)
    key = ("trial_activation", PROTOCOL)
    if batch is not None and key in batch.checked:
        start, end, manifest = batch.checked[key]
        if start <= when and (end is None or when < end):
            return json.loads(manifest)
    with dbscope.reading(data_dir) as db:
        row = db.execute(
            "SELECT starts_at,manifest,(SELECT min(starts_at) FROM trial_activations "
            "WHERE protocol=? AND starts_at>?) AS next_start FROM trial_activations "
            "WHERE protocol=? AND starts_at<=? ORDER BY starts_at DESC LIMIT 1",
            (PROTOCOL, when, PROTOCOL, when),
        ).fetchone()
    if not row:
        raise ValueError("formal paper trial has no eligible activation for this time")
    if batch is not None:
        batch.checked[key] = (row["starts_at"], row["next_start"], row["manifest"])
    return json.loads(row["manifest"])
