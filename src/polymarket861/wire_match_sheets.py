"""Point-in-time EPL research observations from Touchline's match-specific team sheets."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from . import dbscope, football, news, news_scope, news_sources, store

INDEX = news_sources.WIRE_HOST + "/api/v1/fixtures"
MATCH = news_sources.WIRE_HOST + "/api/v1/matches/"
INTERVAL = timedelta(minutes=3)
WINDOW = timedelta(minutes=75)
SCHEMA = """
CREATE TABLE IF NOT EXISTS wire_sheet_tasks(
 fixture TEXT NOT NULL, kickoff TEXT NOT NULL, version INTEGER NOT NULL, allowed_after TEXT NOT NULL,
 PRIMARY KEY(fixture,kickoff,version));
CREATE TABLE IF NOT EXISTS wire_sheet_observations(
 fixture TEXT NOT NULL, kickoff TEXT NOT NULL, version INTEGER NOT NULL, received TEXT NOT NULL,
 recorded TEXT NOT NULL, digest TEXT NOT NULL, document TEXT NOT NULL,
 PRIMARY KEY(fixture,kickoff,version,received,digest));
CREATE TABLE IF NOT EXISTS wire_sheet_backoff(
 provider TEXT PRIMARY KEY, allowed_after TEXT NOT NULL);
"""


def _same_team(local, remote):
    expected = news_sources.WIRE_CLUBS.get(football.team_key(local), (local,))[0]
    return news_sources._wire_club({"team": local, "wire_club": expected}, remote)


def _match_id(game, rows):
    found = []
    for row in rows:
        if (not isinstance(row, dict) or row.get("competition") != "epl" or not all(
                isinstance(row.get(s + "_team"), dict) and _same_team(game[s], row[s + "_team"].get("name"))
                for s in ("home", "away"))):
            continue
        if row.get("kickoff_iso") and store.iso(row["kickoff_iso"]) == store.iso(game["kickoff"]):
            found.append(row)
    if len(found) != 1:
        raise ValueError("no unique scheduled Touchline EPL match with the same clubs and kickoff")
    entry = found[0]
    ident = entry.get("api_id")
    if (type(ident) is not int or ident < 1 or sum(isinstance(r, dict) and r.get("api_id") == ident for r in rows) != 1
            or entry.get("status_short") != "NS" or entry.get("is_live") is not False or entry.get("is_finished") is not False
            or any(entry.get(k) is not None for k in ("elapsed_minutes", "score", "penalty_h_score", "penalty_a_score",
                                                      "home_penalties", "away_penalties", "penalty_winner", "penalty_shootout"))
            or str(entry.get("status", "scheduled")).casefold() not in {"not started", "scheduled"}):
        raise ValueError("Touchline fixture index has conflicting identity or state")
    return ident


def _unplayed(row):
    phase = row["status"]
    if (phase.get("short") != "NS" or str(phase.get("long", "not started")).casefold() not in {"not started", "scheduled"}
            or any(phase.get(k) is not None for k in ("elapsed", "elapsed_minutes", "extra_minutes"))
            or row.get("events") or row.get("penalty_shootout")):
        return False
    scores = row["score"]
    for period in ("current", "halftime", "fulltime", "extratime", "penalty"):
        pair = scores.get(period)
        if (period == "current" or pair is not None) and (
                not isinstance(pair, dict) or not {"home", "away"} <= pair.keys()
                or any(pair[s] is not None for s in ("home", "away"))):
            return False
    stats = row.get("statistics") or {}
    if not isinstance(stats, dict):
        return False
    for side in stats.values():
        if not isinstance(side, dict) or any(v not in (None, 0, "0", "0%") for v in side.values()):
            return False
    return all(row["teams"][s].get(k) is None for s in ("home", "away") for k in ("score", "winner"))


def _starters(side, game, body):
    sheet = body["lineups"].get(side)
    if not sheet or not sheet.get("starters"):
        return []
    if not _same_team(game[side], sheet.get("team")):
        raise ValueError("team sheet belongs to a different club")
    players = sheet["starters"]
    if not isinstance(players, list) or len(players) != 11:
        raise ValueError("incomplete published team sheet")
    result = []
    for player in players:
        if (not isinstance(player, dict) or type(player.get("id")) is not int or player["id"] < 1
                or not news_sources._wire_person(player.get("name"))):
            raise ValueError("unidentified starter")
        result.append({"id": player["id"], "name": player["name"].strip()})
    if len({p["id"] for p in result}) != 11 or len({p["name"].casefold() for p in result}) != 11:
        raise ValueError("repeated starter in a team sheet")
    return result


def _observation(game, index, index_seen, reply, ident, now):
    at = store.iso(max(store.instant(index_seen), store.instant(reply.received_at), store.instant(now)))
    body, published = news_sources._wire_header(reply.body, store.iso(reply.received_at), {})
    row = body["data"]
    kick = store.instant(game["kickoff"])
    if not kick - WINDOW <= store.instant(published) <= store.instant(at) < kick:
        raise ValueError("match report outside the pre-kickoff announcement window")
    if (reply.url != MATCH + str(ident) or type(row.get("fixture_id")) is not int or row["fixture_id"] != ident
            or row.get("competition") != "epl" or row["league"].get("id") != 39
            or row["league"].get("season") != football.season_year(game["season"])
            or store.iso(row["date_utc"]) != store.iso(kick) or not _unplayed(row)
            or not all(_same_team(game[s], row["teams"][s]["name"]) for s in ("home", "away"))):
        raise ValueError("match report identity or pregame state disagrees with the local fixture")
    teams = {side: _starters(side, game, row) for side in ("home", "away")}
    if {p["id"] for p in teams["home"]} & {p["id"] for p in teams["away"]}:
        raise ValueError("starter appears for both clubs")
    count = sum(bool(v) for v in teams.values())
    return {"state": ("unpublished", "partial", "reported")[count], "teams": teams,
            "fixture_id": game["fixture_id"], "kickoff": store.iso(kick), "received_at": at,
            "provider_snapshot": published, "source_url": reply.url, "index_sha256": index,
            "index_received_at": index_seen, "report_received_at": store.iso(reply.received_at),
            "schedule_version": game["version"],
            "response_sha256": hashlib.sha256(reply.body).hexdigest(), "formal_eligible": False}


def _claim(root, now):
    with closing(store.connect(root)) as db:
        db.row_factory = sqlite3.Row
        dbscope.prepare(db, "wire-match-sheets", SCHEMA)
        candidates = [g for g in news._schedule(db, store.iso(now)) if
                      news_scope.competition(g) == "epl" and g["precision"] in {"minute", "second"}
                      and now < store.instant(g["kickoff"]) <= now + WINDOW]
        if not candidates:
            return []
        with db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM wire_sheet_backoff WHERE provider=? AND allowed_after>?",
                          (news_sources.WIRE_HOST, store.iso(now))).fetchone():
                return []
            return [g for g in candidates if db.execute(
                "INSERT INTO wire_sheet_tasks VALUES(?,?,?,?) ON CONFLICT(fixture,kickoff,version) "
                "DO UPDATE SET allowed_after=excluded.allowed_after WHERE allowed_after<=?",
                (g["fixture_id"], g["kickoff"], g["version"], store.iso(now + INTERVAL), store.iso(now))).rowcount]


def _save(root, game, item, retry_after=0):
    with closing(store.connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        item = dict(item, recorded_at=store.now_utc())
        if "provider_snapshot" in item:
            previous = db.execute(
                "SELECT document,digest FROM wire_sheet_observations WHERE fixture=? AND kickoff=? AND version=? "
                "AND json_type(document,'$.provider_snapshot')='text' ORDER BY "
                "json_extract(document,'$.provider_snapshot') DESC,recorded DESC,rowid DESC LIMIT 1",
                (game["fixture_id"], game["kickoff"], item["schedule_version"])).fetchone()
            if previous:
                prior = json.loads(previous[0])
                if store.digest(prior) != previous[1]:
                    raise ValueError("invalid local team sheet fingerprint")
                if item["provider_snapshot"] < prior["provider_snapshot"] or (
                    item["provider_snapshot"] == prior["provider_snapshot"] and prior.get("teams") != item["teams"]
                ):
                    item = {**item, "state": "source_conflict", "reason": "reversed or contradictory publisher snapshot"}
                    del item["teams"]
        db.execute("INSERT OR IGNORE INTO wire_sheet_observations VALUES(?,?,?,?,?,?,?)",
                   (game["fixture_id"], game["kickoff"], item["schedule_version"], item["received_at"], item["recorded_at"],
                    store.digest(item), store.canonical(item)))
        if retry_after:
            expires = store.iso(store.instant(item["received_at"]) + timedelta(seconds=retry_after))
            db.execute("INSERT INTO wire_sheet_backoff VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET "
                       "allowed_after=max(allowed_after,excluded.allowed_after)", (news_sources.WIRE_HOST, expires))
        return item["state"]


def collect(root, *, getter=None):
    now = store.instant(store.now_utc())
    games = _claim(root, now)
    if not games:
        return {"checked": 0, "observations": []}
    get = getter or news_sources._get
    results = []
    errors = (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError)
    try:
        index = get(INDEX)
        if index.url != INDEX:
            raise ValueError("unexpected Touchline fixtures URL")
        doc, _ = news_sources._wire_header(index.body, store.iso(index.received_at), {})
        rows = doc["data"]
        if not isinstance(rows, list):
            raise ValueError("invalid Touchline fixture index")
        sha = hashlib.sha256(index.body).hexdigest()
        upstream_error = None
    except errors as exc:
        upstream_error = exc
    for game in games:
        retry = 0
        try:
            if upstream_error is not None:
                raise upstream_error
            ident = _match_id(game, rows)
            reply = get(MATCH + str(ident))
            item = _observation(game, sha, store.iso(index.received_at), reply, ident, store.now_utc())
        except errors as exc:
            item = {"fixture_id": game["fixture_id"], "kickoff": game["kickoff"], "state": "unavailable",
                    "received_at": store.now_utc(), "reason": str(exc), "formal_eligible": False,
                    "schedule_version": game["version"]}
            retry = max(0, min(86400, int(getattr(exc, "retry_after", 0) or 0)))
            if getattr(exc, "code", None) == 429:
                upstream_error = exc
                retry = max(INTERVAL.total_seconds(), retry)
        state = _save(root, game, item, retry)
        results.append({"fixture_id": game["fixture_id"], "state": state})
    return {"checked": len(results), "observations": results}


def latest(root, game, cutoff):
    file = Path(root) / "runtime.sqlite3"
    if not file.is_file():
        return None
    with closing(sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='wire_sheet_observations'").fetchone():
            return None
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM wire_sheet_observations WHERE fixture=? AND version=("
                         "SELECT f.version FROM fixture_versions f JOIN schedule_receipts r USING(fixture_id,version) "
                         "WHERE f.fixture_id=? AND f.known_at<=? AND r.recorded_at<=? "
                         "ORDER BY f.known_at DESC,f.version DESC LIMIT 1) "
                         "AND kickoff=? AND received<=? AND recorded<=? ORDER BY received DESC,rowid DESC LIMIT 1",
                         (game["fixture_id"], game["fixture_id"], store.iso(cutoff), store.iso(cutoff),
                          store.iso(game["kickoff"]), store.iso(cutoff), store.iso(cutoff))).fetchone()
        if row:
            item = json.loads(row["document"])
            if store.digest(item) != row["digest"] or any(
                    item.get(field) != row[column] for field, column in (
                        ("fixture_id", "fixture"), ("kickoff", "kickoff"), ("schedule_version", "version"),
                        ("received_at", "received"), ("recorded_at", "recorded"))):
                raise ValueError("invalid local team sheet identity, clocks or fingerprint")
            return item
    return None
