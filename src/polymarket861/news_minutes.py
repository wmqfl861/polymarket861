"""Locally received, season-to-date EPL appearances from the public player directory."""

from __future__ import annotations

import uuid
from contextlib import closing
from datetime import timedelta
from urllib.parse import urlencode

from . import dbscope, football, news_scope, news_sources, result_freshness, store
from .score_bounds import valid_score

SOURCE = "touchline-epl-player-usage"
ATTRIBUTION = "Tactical Data by Touchline Wire (https://touchlinewire.com). Normalized factual fields."
LIMIT = 65
_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_usage_checks (
 season TEXT NOT NULL, club TEXT NOT NULL, next_at TEXT NOT NULL, owner TEXT,
 PRIMARY KEY(season,club));
CREATE TABLE IF NOT EXISTS news_usage_frontiers (
 season TEXT NOT NULL, club TEXT NOT NULL, checked_frontier TEXT NOT NULL,
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
        player = {"id": ident, "names": names, "position": row["position"], "starts": starts, "minutes": minutes}
        if row.get("appearances") is not None:
            appearances = row["appearances"]
            if type(appearances) is not int or not starts <= appearances <= 38 or (appearances == 0 and minutes):
                raise ValueError("appearance total conflicts with published starts or minutes")
            player["appearances"] = appearances
        players.append(player)
    return stamp, sorted(players, key=lambda p: p["id"])


def verified_players(root, row, team, season):
    """Reopen an original from either the current or the pre-appearances schema."""
    raw = store.read_blob(root, row["original_sha256"])
    stamp, players = parse(raw, team, season, row["received_at"])
    checked = store.digest(players)
    if checked != row["usage_sha256"]:
        legacy = [{k: v for k, v in p.items() if k != "appearances"} for p in players]
        checked = store.digest(legacy)
    if stamp != row["provider_as_of"] or checked != row["usage_sha256"]:
        raise ValueError("invalid player directory proof")
    return players


def _disputed_final(root, db, game, season, at, unique, prior):
    """Recheck current original identities/kickoffs before carrying forward a prior final."""
    from . import independent_results, result_admission, result_pairings

    year = football.season_year(season)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    fid = game["fixture_id"]
    try:
        native, issues = {}, []
        if {"rf_native_requests", "rf_cache", "rf_observations"} <= tables:
            native, issues = result_admission._raw_rows(root, db, year, at, target=fid, round_id=game["round_id"])
        secondary = independent_results.verified(root, db, year, at)
        if issues:
            return None
        if not native.get(fid) and not secondary.get(fid):
            # A separately registered regulation final can establish a manual
            # score-only hold when neither network source has a fixture here.
            return prior
        a, b = native[fid][-1], secondary[fid][-1]
        proof, reason = result_pairings.decision(game, native[fid], secondary[fid], unique, at)
    except (IndexError, KeyError, OSError, ValueError, TypeError):
        return None
    if ((not proof and reason != "cross_source_score_conflict") or a["error"] or b["status"] != "final"
            or not a["source_id"] or not b["source_match_id"]
            or a["round"] != game["round_id"] or b["round"] != game["round_id"]
            or not valid_score(a["score"]) or not valid_score(b["score"])
            or not a["kickoff"] or a["kickoff"] != b["kickoff"]
            or not f"{year}-08-01" <= a["kickoff"][:10] < f"{year + 1}-08-01"):
        return None
    earliest = store.iso(store.instant(a["kickoff"]) + timedelta(minutes=90))
    if min(a["received"], b["received"]) < earliest:
        return None
    if prior and prior[0] == a["kickoff"]:
        return prior

    def since(rows, fields):
        first = rows[-1]["received"]
        for row in reversed(rows[:-1]):
            if row["received"] < earliest or any(row[k] != rows[-1][k] for k in fields):
                break
            first = row["received"]
        return first

    primary = since(native[fid], ("source_id", "round", "kickoff", "score", "error"))
    independent = since(secondary[fid], ("source_id", "source_match_id", "round", "kickoff", "score", "status"))
    return a["kickoff"], max(primary, independent)


def club_games(db, root, season, team, at):
    """One as-of fixture per match. Preserve the first clock of a stable score and kickoff."""
    from . import result_pairings

    club = football.team_key(team)
    schedule = [dict(r) | {"season": season}
                for r in db.execute(result_freshness._AS_OF.format(times="result_event_times"), {"season": season, "at": at})
                if news_scope.competition(r) == "epl"]
    unique = result_pairings.unique_fixtures(schedule)
    fixtures = [g for g in schedule if club in (football.team_key(g["home"]), football.team_key(g["away"]))
                and (g["kickoff"] < at or g["hold_reason"] == "cross_source_score_conflict")]
    if not fixtures:
        return []
    by_key = {(g["fixture_id"], g["scheduled"]): g for g in fixtures}
    outcomes = {}
    for r in db.execute(
        """SELECT r.fixture_id,s.kickoff AS scheduled,coalesce(t.actual_kickoff,s.kickoff) AS played,
                  r.home_goals,r.away_goals,max(r.received_at,r.recorded_at) AS known_at
           FROM result_versions r JOIN result_schedule_refs s USING(fixture_id,version)
           LEFT JOIN result_event_times t USING(fixture_id,version)
           WHERE r.fixture_id IN (SELECT fixture_id FROM fixture_versions WHERE season=?)
             AND r.recorded_at<=? AND r.received_at<=? AND r.published_at<=? AND r.finalized_at<=?
           ORDER BY r.fixture_id,r.version""", (season, at, at, at, at)):
        key = (r["fixture_id"], r["scheduled"])
        if key not in by_key:
            continue
        outcome = (r["played"], r["home_goals"], r["away_goals"])
        if not valid_score(outcome[1:]) or store.iso(store.instant(outcome[0]) + timedelta(minutes=90)) > r["known_at"]:
            outcomes.pop(key, None)
        elif key not in outcomes or outcomes[key][0] != outcome:
            outcomes[key] = (outcome, r["known_at"])
    non_score_holds = {}
    if any(g["hold_reason"] == "cross_source_score_conflict" for g in fixtures):
        non_score_holds = {(r[0],r[1]): r[2] for r in db.execute(
            """SELECT fixture_id,kickoff,max(at) FROM result_dispositions
               WHERE held=1 AND reason!='cross_source_score_conflict' AND at<=?
                 AND fixture_id IN (SELECT fixture_id FROM fixture_versions WHERE season=?)
               GROUP BY fixture_id,kickoff""", (at, season))}
    for key, game in by_key.items():
        hold = game["hold_reason"]
        evidence = outcomes.get(key)
        accepted = valid_score((game["goals"], game["away_goals"])) and game["received"]
        game["completed"] = False
        if game["fixture_id"] not in unique:
            continue
        prior = (evidence[0][0], evidence[1]) if evidence else None
        played = prior if accepted else None
        if hold == "cross_source_score_conflict":
            if prior and prior[1] < non_score_holds.get(key, ""):
                prior = None
            played = _disputed_final(root, db, game, season, at, unique, prior)
        if played:
            game.update(kickoff=played[0], received=played[1], completed=True)
    return sorted((g for g in fixtures if g["kickoff"] < at), key=lambda g: (g["kickoff"], g["fixture_id"]))


def _frontier(games):
    return store.digest([(g["fixture_id"], g["kickoff"], g["completed"], g["received"])
                         for g in games])


def same_snapshot(previous, stamp, usage_sha, required):
    """Reuse an unchanged receipt interval only while its game evidence still covers the team."""
    return (previous is not None and previous["usage_sha256"] == usage_sha
            and not previous["provider_as_of"] < required <= stamp
            and store.instant(stamp) - store.instant(previous["provider_as_of"]) < timedelta(days=6))


def refresh(root, team, season, *, getter=None):
    """Capture actual starts/minutes. A preview, roster prediction or injury count supplies none."""
    clock = store.instant(store.now_utc())
    at, club = store.iso(clock), football.team_key(team)
    if news_scope.wire_coverage(team, season, at=at) is None:
        return {"state": "unavailable", "reason": "source_not_covered"}
    owner = uuid.uuid4().hex
    with closing(_connection(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        games = club_games(db, root, season, team, at)
        frontier = _frontier(games)
        old = db.execute("SELECT c.next_at,c.owner,f.checked_frontier FROM news_usage_checks c "
                         "LEFT JOIN news_usage_frontiers f USING(season,club) WHERE c.season=? AND c.club=?",
                         (season, club)).fetchone()
        if old and old["next_at"] > at and (old["owner"] or old["checked_frontier"] == frontier):
            return {"state": "cached"}
        db.execute("INSERT OR REPLACE INTO news_usage_frontiers VALUES(?,?,?)", (season, club, frontier))
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
        prior_players = None
        if previous:
            try:
                prior_players = verified_players(root, previous, team, season)
            except (ValueError, OSError, KeyError, TypeError):
                pass  # A newly received valid publisher original can restore the evidence.
            if stamp == previous["provider_as_of"] and prior_players is not None and players != prior_players:
                raise ValueError("contradictory player directory at one publisher time")
        from . import news_importance

        required = max((g["received"] for g in games if g["completed"]), default="")
        scope = [g for g in games if g["kickoff"] < stamp]
        incomplete_totals = (len(scope) >= news_importance.POLICY["minimum_matches"]
                             and all(g["completed"] for g in scope)
                             and news_importance.appearance_totals(players, len(scope)) is None)
        if stamp < required or incomplete_totals:
            next_check = timedelta(minutes=30)
        if prior_players is not None and same_snapshot(previous, stamp, digest, required):
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
