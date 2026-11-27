"""Observe the current OpenFootball season without rewriting earlier local observations."""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from . import datasets, league_registry, market_provider, store

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
 league TEXT, season TEXT, owner TEXT, next_at TEXT, status TEXT, detail TEXT,
 PRIMARY KEY(league,season));
CREATE TABLE IF NOT EXISTS revisions (
 league TEXT, season TEXT, received TEXT, revision TEXT, blob TEXT,
 PRIMARY KEY(league,season,received));
"""


def _base(root):
    return Path(root) / "sources" / "domestic-openfootball"


def _path(root, blob, season, name):
    return _base(root) / "objects" / blob / season / name


def _download(league, season):
    name = league_registry.profile(league).source_file
    scope = f"{season}/{name}"
    args = {"path": scope, "per_page": 1}
    commits = json.loads(market_provider.get(datasets.BASE + "/commits", args, timeout=12).body)
    if not isinstance(commits, list) or not commits:
        raise ValueError("OpenFootball has no published revision for " + scope)
    revision = commits[0].get("sha")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("invalid OpenFootball commit")
    url = datasets.BASE + "/contents/" + scope + "?" + urlencode({"ref": revision})
    obj = json.loads(market_provider.get(url, timeout=12).body)
    if obj.get("path") != scope or obj.get("encoding") != "base64" or obj.get("type") != "file":
        raise ValueError("unexpected OpenFootball contents response")
    raw = base64.b64decode("".join(obj["content"].split()), validate=True)
    blob = league_registry.git_blob(raw)
    if blob != obj.get("sha") or len(raw) != obj.get("size") or len(raw) > 2_000_000:
        raise ValueError("OpenFootball contents do not match the published Git blob")
    season_file = json.loads(raw)
    rows = season_file["matches"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("empty OpenFootball season")
    for row in rows:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ("team1", "team2")):
            raise ValueError("unexpected OpenFootball match")
    league_registry.validate_members(league, season, {row[k] for row in rows for k in ("team1", "team2")},
                                     require_complete=True)
    return revision, blob, raw


def refresh(root, league, season):
    """Share an hourly poll and retry a failed request after five minutes."""
    name = league_registry.profile(league).source_file
    league_registry.season_start_year(season)
    folder = _base(root)
    folder.mkdir(parents=True, exist_ok=True)
    owner = uuid4().hex
    with closing(sqlite3.connect(folder / "receipts.sqlite3", timeout=10)) as db:
        db.executescript(SCHEMA)
        with db:
            db.execute("BEGIN IMMEDIATE")
            now = store.instant(store.now_utc())
            row = db.execute("SELECT next_at,status,detail FROM polls WHERE league=? AND season=?",
                             (league, season)).fetchone()
            if row and row[0] > store.iso(now):
                return {"state": row[1], "next_at": row[0], **json.loads(row[2])}
            lease = store.iso(now + timedelta(minutes=2))
            db.execute("INSERT OR REPLACE INTO polls VALUES(?,?,?,?,?,?)",
                       (league, season, owner, lease, "refreshing", "{}"))
        try:
            revision, blob, raw = _download(league, season)
            path = _path(root, blob, season, name)
            if not path.is_file() or league_registry.git_blob(path.read_bytes()) != blob:
                datasets._atomic(path, raw)
            state, delay = "ok", timedelta(hours=1)
            detail = {"commit": revision, "blob": blob}
        except (OSError, ValueError, TypeError, IndexError, KeyError) as exc:
            state, delay = "error", timedelta(minutes=5)
            detail = {"error": str(exc)[:300]}
        with db:
            db.execute("BEGIN IMMEDIATE")
            received = store.now_utc()
            next_at = store.iso(store.instant(received) + delay)
            detail["checked_at"] = received
            changed = db.execute("UPDATE polls SET status=?,detail=?,next_at=?,owner='' "
                                 "WHERE league=? AND season=? AND owner=?",
                                 (state, store.canonical(detail), next_at, league, season, owner)).rowcount
            if not changed:
                return {"state": "superseded"}
            if state == "ok":
                previous = db.execute("SELECT revision,blob FROM revisions WHERE league=? AND season=? "
                                      "ORDER BY received DESC LIMIT 1", (league, season)).fetchone()
                if previous != (revision, blob):
                    db.execute("INSERT INTO revisions VALUES(?,?,?,?,?)", (league, season, received, revision, blob))
        return {"state": state, "next_at": next_at, **detail}


def snapshots(root, league, season, as_of):
    """Read verified files in local arrival order, including withdrawals and reversions within a file."""
    name = league_registry.profile(league).source_file
    league_registry.season_start_year(season)
    index = _base(root) / "receipts.sqlite3"
    if not index.is_file():
        return
    with closing(sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        rows = db.execute("SELECT received,revision,blob FROM revisions WHERE league=? AND season=? "
                          "AND received<=? ORDER BY received", (league, season, store.iso(as_of))).fetchall()
    for received, commit, blob in rows:
        path = _path(root, blob, season, name)
        if league_registry.git_blob(path.read_bytes()) != blob:
            raise ValueError(f"archived OpenFootball file failed verification: {league}/{season}/{blob}")
        yield received, commit, blob, path


def prepare_history(folder):
    """Install the registry's pinned domestic training files for a fresh deployment."""
    from .football import fetch_history

    folder = Path(folder)
    leagues = {}
    for league in league_registry.PROFILES:
        pins = sorted(league_registry.source_blobs(league))[-9:]
        leagues[league] = fetch_history(folder, pins[0], pins[-1], league=league)["seasons"]
    return {"source": league_registry.SOURCE_ROOT, "directory": str(folder), "verified_seasons": leagues}
