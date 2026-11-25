"""Locally received, season-to-date EPL appearances from the public player directory."""

from __future__ import annotations

import uuid
from contextlib import closing
from datetime import timedelta
from urllib.parse import urlencode

from . import dbscope, football, news_sources, store

SOURCE = "touchline-epl-player-usage"
ATTRIBUTION = "Tactical Data by Touchline Wire (https://touchlinewire.com). Normalized factual fields."
LIMIT = 65
_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_usage_checks (
 season TEXT NOT NULL, club TEXT NOT NULL, next_at TEXT NOT NULL, owner TEXT,
 PRIMARY KEY(season,club));
CREATE TABLE IF NOT EXISTS news_usage_history (
 season TEXT NOT NULL, club TEXT NOT NULL, known_at TEXT NOT NULL, received_at TEXT NOT NULL,
 provider_as_of TEXT NOT NULL, usage_sha256 TEXT NOT NULL, original_sha256 TEXT NOT NULL,
 PRIMARY KEY(season,club,known_at));
"""


def _connection(root):
    db = store.connect(root)
    dbscope.prepare(db, "news-player-usage", _SCHEMA)
    return db


def source_url(team):
    club = football.team_key(team)
    slug = news_sources.WIRE_CLUBS.get(club, (team, club))[1]
    return news_sources.WIRE_HOST + "/api/v1/players?" + urlencode({"club": slug, "limit": LIMIT})


def parse(raw, team, season, receipt):
    doc, stamp = news_sources._wire_header(raw, receipt, {})
    published = store.instant(stamp)
    if published.year - (published.month < 7) != football.season_year(season):
        raise ValueError("player usage is for a different season")
    rows = doc.get("data")
    if (not isinstance(rows, list) or not 11 <= len(rows) < LIMIT
            or type(doc["meta"].get("count")) is not int or len(rows) != doc["meta"]["count"]):
        raise ValueError("incomplete EPL player directory")
    club = football.team_key(team)
    wire_club = news_sources.WIRE_CLUBS.get(club, (team, club))[0]
    players, seen = [], set()
    for row in rows:
        if (not isinstance(row, dict) or row.get("league") != "epl" or
                not news_sources._wire_club({"team": team, "wire_club": wire_club}, row.get("club"))):
            raise ValueError("player directory league or club differs from request")
        ident, starts, minutes = (row.get(key) for key in ("id", "starts", "minutes"))
        names = sorted({v.strip() for v in (row.get("display_name"), row.get("name"))
                        if isinstance(v, str) and v.strip() and len(v) <= 150})
        if (type(ident) is not int or ident <= 0 or ident in seen or not names or
                type(starts) is not int or not 0 <= starts <= 38 or
                type(minutes) is not int or not 0 <= minutes <= 38 * 90 or
                row.get("position") not in {"GK", "DEF", "MID", "FWD"}):
            raise ValueError("invalid or duplicate season appearance record")
        seen.add(ident)
        players.append({"id": ident, "names": names, "position": row["position"],
                        "starts": starts, "minutes": minutes})
    return stamp, sorted(players, key=lambda p: p["id"])


def refresh(root, team, season, *, getter=None):
    """Capture actual starts/minutes. A preview, roster prediction or injury count supplies none."""
    clock = store.instant(store.now_utc())
    at, club = store.iso(clock), football.team_key(team)
    owner = uuid.uuid4().hex
    with closing(_connection(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT next_at FROM news_usage_checks WHERE season=? AND club=?",
                         (season, club)).fetchone()
        if old and old[0] > at:
            return {"state": "cached"}
        db.execute("INSERT OR REPLACE INTO news_usage_checks VALUES(?,?,?,?)",
                   (season, club, store.iso(clock + timedelta(minutes=5)), owner))
    next_check = timedelta(hours=12)
    try:
        url = source_url(team)
        reply = (getter or news_sources._get)(url)
        receipt = store.iso(reply.received_at)
        if (reply.url != url or not isinstance(reply.body, bytes) or len(reply.body) > news_sources.MAX_BYTES
                or not at <= store.iso(reply.requested_at) <= receipt <= store.iso(store.now_utc())):
            raise ValueError("invalid local player-directory receipt")
        stamp, players = parse(reply.body, team, season, receipt)
        digest = store.digest(players)
        with closing(_connection(root)) as db:
            previous = latest(db, season, club, receipt)
        if previous and stamp < previous["provider_as_of"]:
            raise ValueError("player directory snapshot moved backwards")
        if previous and stamp == previous["provider_as_of"] and previous["usage_sha256"] != digest:
            raise ValueError("contradictory player directory at one publisher time")
        if (previous and previous["usage_sha256"] == digest and
                store.instant(stamp) - store.instant(previous["provider_as_of"]) < timedelta(days=6)):
            return {"state": "unchanged", "original_sha256": previous["original_sha256"]}
        original = store.archive_bytes(root, SOURCE, reply.body, observed_at=receipt, metadata={
            "url": url, "attribution": ATTRIBUTION, "license_url": news_sources.WIRE_HOST + "/docs#attribution",
        })["sha256"]
        with closing(_connection(root)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM news_usage_checks WHERE season=? AND club=? AND owner=?",
                              (season, club, owner)).fetchone():
                return {"state": "lease_lost"}
            db.execute("INSERT INTO news_usage_history VALUES(?,?,?,?,?,?,?)",
                       (season, club, store.iso(store.now_utc()), receipt, stamp, digest, original))
        return {"state": "captured", "players": len(players), "original_sha256": original}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        next_check = timedelta(minutes=30)
        return {"state": "unavailable", "reason": str(exc)[:200]}
    finally:
        with closing(_connection(root)) as db, db:
            db.execute("UPDATE news_usage_checks SET next_at=?,owner=NULL WHERE season=? AND club=? AND owner=?",
                       (store.iso(store.instant(store.now_utc()) + next_check), season, club, owner))


def latest(db, season, club, cutoff):
    return db.execute(
        "SELECT * FROM news_usage_history WHERE season=? AND club=? AND known_at<=? "
        "AND received_at<=? AND provider_as_of<=? ORDER BY known_at DESC LIMIT 1",
        (season, club, cutoff, cutoff, cutoff),
    ).fetchone()
