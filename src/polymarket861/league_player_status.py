"""Optional, private, match-scoped observations from the API-Football v3 service."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import closing
from datetime import timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from . import dbscope, evidence, football, football_model, league_player_evidence as usage, league_registry, news_model, store

API = "https://v3.football.api-sports.io"
SOURCE = "api-football-v3"
LEAGUES = {"lal": 140, "bun": 78, "sea": 135, "fl1": 61}
SCENARIO_POLICY_SHA256 = store.digest({
    "scope": "domestic_private_absence_sensitivity_v2", "epl_policy": news_model.POLICY_SHA256,
    "provider_transcripts": "dedicated_season_and_rights_channel",
    "lineup_contradiction": "confirmed_start_or_named_substitute",
    "onset": "verified_injury_start_or_continuous_claim_first_local_observation",
    "absence_age_hours": 6, "roster_age_days": 14,
    "goalkeeper_requires_verified_majority_of_entire_completed_window": True,
})
SCHEMA = """
CREATE TABLE IF NOT EXISTS player_api_budget (
 id INTEGER PRIMARY KEY CHECK(id=1), next_request TEXT NOT NULL, reason TEXT);
CREATE TABLE IF NOT EXISTS player_api_observations (
 fixture TEXT NOT NULL,kickoff TEXT NOT NULL,field TEXT NOT NULL,received TEXT NOT NULL,
 digest TEXT NOT NULL,body TEXT NOT NULL,
 PRIMARY KEY(fixture,kickoff,field,received,digest));
"""
# Exact alternative provider spellings. Each target must identify a registered member
# of the given league and season. A new spelling never falls through to a fuzzy match.
NAMES = {
    "lal": {"Real Madrid":"Real Madrid CF", "Barcelona":"FC Barcelona", "Atletico Madrid":"Club Atlético de Madrid",
            "Athletic Club":"Athletic Club", "Celta Vigo":"RC Celta de Vigo", "Real Betis":"Real Betis Balompié",
            "Real Sociedad":"Real Sociedad de Fútbol", "Alaves":"Deportivo Alavés", "Oviedo":"Real Oviedo",
            "Espanyol":"RCD Espanyol de Barcelona", "Rayo Vallecano":"Rayo Vallecano de Madrid",
            "Osasuna":"CA Osasuna", "Villarreal":"Villarreal CF", "Sevilla":"Sevilla FC",
            "Valencia":"Valencia CF", "Getafe":"Getafe CF", "Levante":"Levante UD",
            "Mallorca":"RCD Mallorca", "Girona":"Girona FC", "Elche":"Elche CF",
            "Racing Santander":"Real Racing Club de Santander", "Deportivo La Coruna":"RC Deportivo La Coruña"},
    "bun": {"Bayern München":"FC Bayern München", "Werder Bremen":"SV Werder Bremen",
            "Borussia Monchengladbach":"Borussia Mönchengladbach", "Mainz 05":"1. FSV Mainz 05",
            "VfL Wolfsburg":"VfL Wolfsburg", "FC Augsburg":"FC Augsburg", "1899 Hoffenheim":"TSG 1899 Hoffenheim",
            "Bayer Leverkusen":"Bayer 04 Leverkusen", "FC St. Pauli":"FC St. Pauli 1910",
            "SC Freiburg":"SC Freiburg", "Union Berlin":"1. FC Union Berlin", "FC Koln":"1. FC Köln",
            "Holstein Kiel":"Holstein Kiel", "Hamburger SV":"Hamburger SV"},
    "sea": {"Inter":"FC Internazionale Milano", "AC Milan":"AC Milan", "AS Roma":"AS Roma",
            "Napoli":"SSC Napoli", "Juventus":"Juventus FC", "Atalanta":"Atalanta BC",
            "Bologna":"Bologna FC 1909", "Fiorentina":"ACF Fiorentina", "Lazio":"SS Lazio",
            "Sassuolo":"US Sassuolo Calcio", "Udinese":"Udinese Calcio", "Como":"Como 1907",
            "Lecce":"US Lecce", "Torino":"Torino FC", "Cagliari":"Cagliari Calcio",
            "Parma":"Parma Calcio 1913", "Genoa":"Genoa CFC", "Verona":"Hellas Verona FC"},
    "fl1": {"Paris Saint Germain":"Paris Saint-Germain FC", "Marseille":"Olympique de Marseille",
            "Lyon":"Olympique Lyonnais", "Brest":"Stade Brestois 29", "Monaco":"AS Monaco FC",
            "Lille":"Lille OSC", "Lens":"Racing Club de Lens", "Rennes":"Stade Rennais FC 1901",
            "Strasbourg":"RC Strasbourg Alsace", "Le Havre":"Le Havre AC", "Toulouse":"Toulouse FC",
            "Nice":"OGC Nice", "Angers":"Angers SCO", "Nantes":"FC Nantes", "Lorient":"FC Lorient",
            "Metz":"FC Metz", "Auxerre":"AJ Auxerre"},
}


class Unavailable(Exception):
    def __init__(self, state):
        super().__init__(state)
        self.state = state


def _key():
    return os.environ.get("PM861_API_FOOTBALL_KEY", "").strip()


def _provider_state(root, fixture, *, at=None):
    if not _key():
        return "not_configured"
    checked_at = store.instant(at or store.now_utc())
    year = checked_at.year
    # The publisher documents Year-4 through Year-2 for the Free plan. A key
    # for a paid account does not extend this application's free-only coverage.
    if not year - 4 <= league_registry.season_start_year(fixture["season"]) <= year - 2:
        return "free_season_unavailable"
    path = Path(root) / "runtime.sqlite3"
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='ev_source_versions'").fetchone():
                at = evidence.utc(checked_at)
                manifest = evidence.source_at(db, SOURCE, at, recorded_by=at)
                if (manifest and manifest.get("status") == "active" and manifest.get("free") is True
                        and manifest.get("retain_private_observations") is True
                        and all("prediction" in evidence.granted(manifest, f, "auto") for f in ("injury", "lineup"))):
                    return "available"
    return "rights_unverified"


def _path(root):
    return Path(root) / "player-status"


def _db(root):
    db = store.connect(_path(root))
    try:
        dbscope.prepare(db, "domestic-player-status-v1", SCHEMA)
    except BaseException:
        db.close()
        raise
    return db


def _name(league, season, name):
    if not isinstance(name, str):
        return None
    members = league_registry.members(league, season)
    if name in members:
        return name
    key = football.team_key(NAMES[league].get(name, name))
    matches = [club for club in members if football.team_key(club) == key]
    return matches[0] if len(matches) == 1 else None


def _id(value):
    return value if type(value) is int and value > 0 else None


def _issue(root, route, params):
    now = store.instant(store.now_utc())
    with closing(_db(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        limit = db.execute("SELECT next_request,reason FROM player_api_budget WHERE id=1").fetchone()
        if limit and store.instant(limit[0]) > now and limit[1] != "request_spacing":
            raise Unavailable(limit[1] or "provider_backoff")
        slot = max(now, store.instant(limit[0])) if limit else now
        db.execute("INSERT INTO player_api_budget VALUES(1,?,'request_spacing') ON CONFLICT(id) DO UPDATE "
                   "SET next_request=excluded.next_request,reason=excluded.reason",
                   (store.iso(slot + timedelta(seconds=1)),))
    delay = (slot - now).total_seconds()
    if delay > 0:
        time.sleep(delay)
        with closing(_db(root)) as db:
            current = db.execute("SELECT next_request,reason FROM player_api_budget WHERE id=1").fetchone()
            if current[1] != "request_spacing" and store.instant(current[0]) > store.instant(store.now_utc()):
                raise Unavailable(current[1])
    try:
        res = httpx.get(API + "/" + route, params=params,
                        headers={"x-apisports-key": _key()}, timeout=10, follow_redirects=False)
    except httpx.HTTPError:
        raise Unavailable("network_error") from None
    received = store.now_utc()
    state = None
    wait = 0
    if res.status_code in (401, 403):
        state, wait = "subscription_or_access_denied", 3600
    elif res.status_code == 429:
        state, wait = "rate_limited", 60
    elif res.status_code != 200:
        state, wait = "provider_http_error", 60
    try:
        obj = res.json() if res.status_code == 200 else {}
        if not isinstance(obj, dict):
            raise ValueError
        errors = obj.get("errors")
        if errors:
            fields = " ".join(map(str, errors)).casefold()
            if "request" in fields or "rate" in fields:
                state, wait = "rate_limited", 60
            else:
                state, wait = "provider_rejected_request", 600
        if state is None and (obj.get("get") != route or not isinstance(obj.get("response"), list)
                              or obj.get("results") != len(obj["response"])
                              or obj.get("paging", {}).get("total", 1) not in (0, 1)):
            state, wait = "incomplete_response", 60
    except (ValueError, TypeError, AttributeError):
        state, wait = "malformed_response", 60
    local = store.instant(received)
    if res.headers.get("x-ratelimit-requests-remaining") == "0":
        wait = max(wait, (local.replace(hour=0, minute=0, second=0, microsecond=0)
                          + timedelta(days=1) - local).total_seconds())
    if res.headers.get("x-ratelimit-remaining") == "0":
        wait = max(wait, 60)
    retry = res.headers.get("Retry-After")
    if retry and wait:
        try:
            extra = float(retry) if retry.isdigit() else (parsedate_to_datetime(retry) - local).total_seconds()
            wait = max(wait, min(extra, 86400))
        except (TypeError, ValueError, OverflowError):
            pass
    if wait:
        with closing(_db(root)) as db, db:
            db.execute("INSERT INTO player_api_budget VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "next_request=excluded.next_request,reason=excluded.reason "
                       "WHERE excluded.next_request>=player_api_budget.next_request",
                       (store.iso(local + timedelta(seconds=wait)), state or "rate_limited"))
    if state:
        raise Unavailable(state)
    stamp = None
    if res.headers.get("date"):
        try:
            stamp = store.iso(parsedate_to_datetime(res.headers["date"]))
        except (TypeError, ValueError, OverflowError):
            pass
    return obj["response"], {"received_at": received, "provider_http_date": stamp}


def _identity(fixture, rows):
    code, season = fixture["league"], fixture["season"]
    kick = store.instant(fixture["kickoff"])
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        data, comp, clubs = (row.get(k) or {} for k in ("fixture", "league", "teams"))
        try:
            home, away = clubs["home"], clubs["away"]
            if (comp["id"] != LEAGUES[code] or comp["season"] != int(season[:4]) or
                    store.instant(data["date"]) != kick or data["timestamp"] != int(kick.timestamp()) or
                    _name(code, season, home["name"]) != fixture["home"] or
                    _name(code, season, away["name"]) != fixture["away"] or
                    any(not _id(it["id"]) for it in (data, home, away)) or home["id"] == away["id"]):
                continue
            if data["status"]["short"] != "NS":
                raise Unavailable("fixture_not_pregame")
            matches.append({"fixture_id": data["id"], "home": home["id"], "away": away["id"]})
        except (ValueError, TypeError, KeyError):
            continue
    if len(matches) != 1:
        raise Unavailable("fixture_identity_unmatched" if not matches else "fixture_identity_ambiguous")
    return matches[0]


def _empty():
    return {"home": {"state": "unknown", "players": []}, "away": {"state": "unknown", "players": []}}


def _players(field, rows, identity, fixture):
    data = _empty()
    ids = {identity[s]: s for s in ("home", "away")}
    seen_teams = set()
    seen_players = set()
    for row in rows:
        try:
            team = row["team"]
            side = ids[team["id"]]
            if _name(fixture["league"], fixture["season"], team["name"]) != fixture[side]:
                raise ValueError
            if field == "injuries":
                match, comp, p = row["fixture"], row["league"], row["player"]
                if (match["id"] != identity["fixture_id"] or store.instant(match["date"]) !=
                        store.instant(fixture["kickoff"]) or comp["id"] != LEAGUES[fixture["league"]] or
                        comp["season"] != int(fixture["season"][:4]) or not _id(p["id"])
                        or not isinstance(p["name"], str) or not p["name"].strip()):
                    raise ValueError
                if (side, p["id"]) in seen_players:
                    raise ValueError
                seen_players.add((side, p["id"]))
                data[side]["players"].append({"player_id": p["id"], "name": p["name"],
                                              "provider_type": p.get("type"), "reason": p.get("reason")})
                data[side]["state"] = "reported"
            else:
                if side in seen_teams:
                    raise ValueError
                seen_teams.add(side)
                starters = row.get("startXI")
                if not isinstance(starters, list) or len(starters) != 11:
                    continue
                def members(entries):
                    if not isinstance(entries, list):
                        raise ValueError
                    group = []
                    for entry in entries:
                        p = entry["player"]
                        if (not _id(p["id"]) or not isinstance(p["name"], str) or not p["name"].strip()
                                or p["id"] in seen_players):
                            raise ValueError
                        seen_players.add(p["id"])
                        group.append({"player_id": p["id"], "name": p["name"], "position": p.get("pos")})
                    return sorted(group, key=lambda p: p["player_id"])
                data[side] = {"state": "published", "players": members(starters),
                              "bench": members(row.get("substitutes", []))}
        except (KeyError, ValueError, TypeError):
            raise Unavailable("player_data_identity_mismatch") from None
    for side in data.values():
        side["players"].sort(key=lambda p: p["player_id"])
    return data


def _save(db, fixture, field, body, received):
    body.update(source=SOURCE, received_at=received)
    db.execute("INSERT OR IGNORE INTO player_api_observations VALUES(?,?,?,?,?,?)",
               (fixture["fixture_id"], store.iso(fixture["kickoff"]), field, received,
                store.digest(body), store.canonical(body)))


def collect(root, fixture):
    access = _provider_state(root, fixture)
    if access != "available":
        return {"state": access}
    code = fixture["league"]
    if code not in LEAGUES:
        raise ValueError("unsupported league for player status")
    kick = store.instant(fixture["kickoff"])
    if store.instant(store.now_utc()) >= kick:
        return {"state": "window_closed"}
    league_registry.validate_members(code, fixture["season"], (fixture["home"], fixture["away"]))
    if fixture["fixture_id"] != football.fixture_id(fixture["season"], fixture["home"], fixture["away"], league=code):
        raise ValueError("scoped fixture identity mismatch")
    fields = ["injuries"]
    if kick - store.instant(store.now_utc()) <= timedelta(hours=3):
        fields.append("lineup")
    with closing(_db(root)) as db:
        try:
            rows, verified = _issue(root, "fixtures", {"league": LEAGUES[code], "season": fixture["season"][:4],
                                                       "date": kick.date().isoformat(), "timezone": "UTC"})
            identity = _identity(fixture, rows)
        except Unavailable as exc:
            with db:
                for field in fields:
                    _save(db, fixture, field, {"state": exc.state, "teams": _empty()}, store.now_utc())
            return {"state": exc.state}
        states = {}
        for field in fields:
            route = "injuries" if field == "injuries" else "fixtures/lineups"
            try:
                rows, clock = _issue(root, route, {"fixture": identity["fixture_id"]})
                teams = _players(field, rows, identity, fixture)
                state = "observed" if any(v["players"] for v in teams.values()) else "no_published_players"
                if store.instant(clock["received_at"]) >= kick:
                    raise Unavailable("window_closed")
                body = {"state": state, "teams": teams, "provider_fixture_id": identity["fixture_id"],
                        "verified_fixture": verified, **clock}
            except Unavailable as exc:
                body = {"state": exc.state, "teams": _empty(), "provider_fixture_id": identity["fixture_id"]}
            received = body.get("received_at") or store.now_utc()
            with db:
                _save(db, fixture, field, body, received)
            states[field] = body["state"]
    return states


def as_of(root, fixture, cutoff):
    access = _provider_state(root, fixture)
    configured = bool(_key())
    default = "pending" if access == "available" else access
    result = {"source": SOURCE, "scope": "private_research", "configured": configured, "api_football_state": access}
    for field in ("injuries", "lineup"):
        result[field] = {"state": default, "teams": _empty()}
    dbpath = _path(root) / "runtime.sqlite3"
    if access == "available" and dbpath.is_file():
        with closing(sqlite3.connect(dbpath.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            db.execute("BEGIN")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='player_api_observations'").fetchone():
                for field in ("injuries", "lineup"):
                    row = db.execute("SELECT body,digest FROM player_api_observations WHERE fixture=? AND kickoff=? AND "
                                     "field=? AND received<=? AND received<? ORDER BY received DESC,digest DESC LIMIT 1",
                                     (fixture["fixture_id"], store.iso(fixture["kickoff"]), field,
                                      store.iso(cutoff), store.iso(fixture["kickoff"]))).fetchone()
                    if row:
                        saved = json.loads(row[0])
                        if store.digest(saved) != row[1]:
                            result[field]["state"] = "observation_digest_mismatch"
                        elif _provider_state(root, fixture, at=saved["received_at"]) == "available":
                            result[field] = saved
                        else:
                            result[field]["state"] = "rights_unverified"
    local = usage.local_records(root, fixture, cutoff)
    result.update(local)
    if local:
        result["source"] = usage.SOURCE
    semantic = {k: {"state": result[k]["state"], "teams": result[k]["teams"]}
                for k in ("injuries", "lineup") if result[k]["state"] in {"observed", "no_published_players"}}
    if local.get("lineup_ambiguity"):
        semantic["lineup_ambiguity"] = local["lineup_ambiguity"]
    for field in ("usage", "prior_lineups"):
        if local.get(field):
            semantic[field] = {side: [item["evidence_id"] for item in items] for side, items in local[field].items()}
    result["evidence_sha256"] = store.digest(semantic) if semantic else None
    return result


# The positions persisted by the existing roster parsers are source-native.
# A single letter has a different meaning across language templates.
_ROLES = {
    "lal": {"por": "goalkeeper", "portero": "goalkeeper", "arq": "goalkeeper", "def": "defence",
            "med": "midfield", "cen": "midfield", "del": "forward"},
    "sea": {"p": "goalkeeper", "portiere": "goalkeeper", "d": "defence", "c": "midfield", "a": "forward"},
    "fl1": {"g": "goalkeeper", "gb": "goalkeeper", "gardien": "goalkeeper", "d": "defence",
            "déf": "defence", "m": "midfield", "mil": "midfield", "a": "forward", "att": "forward"},
    "bun": {"torwart": "goalkeeper", "abwehr": "defence", "mittelfeld": "midfield", "sturm": "forward"},
}


def absence_scenario(base, snapshot, *, cutoff, engine=None):
    """Point-in-time absence sensitivity with a verified independent club roster."""
    out = {"scope": "uncalibrated_private_research", "changed": False, "applied": [],
           "status": "no_qualified_match_absences", "excluded": [],
           "policy_sha256": SCENARIO_POLICY_SHA256, "epl_policy_sha256": news_model.POLICY_SHA256,
           "probabilities": base["probabilities"]}
    if base["model"] != "B1" or not base.get("rates"):
        return dict(out, status="no_fitted_team_rates")
    status = snapshot.get("player_status", {})
    injuries = status.get("injuries", {})
    field_at = injuries.get("received_at")
    origin = injuries.get("source", status.get("source"))
    if origin not in (SOURCE, usage.SOURCE) or injuries.get("state") != "observed" or not field_at:
        return out
    when = store.instant(cutoff)
    received = store.instant(field_at)
    if not when - timedelta(hours=6) <= received <= when < store.instant(base["kickoff"]):
        return dict(out, status="outdated_match_observation")
    # Full-scope native forecasts must match the roster receipt even after a fixture moves.
    for key in ("fixture_id", "league", "season"):
        if key in base and snapshot.get(key) != base[key]:
            return dict(out, status="fixture_scope_mismatch")
    shifts = {"home": 0.0, "away": 0.0}
    training = None
    starters = status.get("lineup", {})
    start_receipt = starters.get("received_at")
    if starters.get("state") != "observed" or not start_receipt or store.instant(start_receipt) > when:
        starters = {}
    for side in ("home", "away"):
        roster = snapshot.get("teams", {}).get(side, {}).get("roster", {})
        verified = roster.get("ready_at")
        members = roster.get("players", ()) if (roster.get("evidence") and verified and
                    when - timedelta(days=14) <= store.instant(verified) <= when) else ()
        matchday = starters.get("teams", {}).get(side, {})
        starting, bench = matchday.get("players", ()), matchday.get("bench", ())
        for p in injuries.get("teams", {}).get(side, {}).get("players", ()):
            pid, name = p.get("player_id"), usage.full_name(p.get("name"))
            matching = [m for m in members if name and usage.full_name(m.get("name")) == name]
            reason = None
            if side in status.get("lineup_ambiguity", ()):
                reason = "published_matchday_squad_identity_unresolved"
            elif str(p.get("provider_type", "")).strip().casefold() != "missing fixture":
                reason = "no_explicit_fixture_absence"
            elif len(matching) != 1 or (origin == SOURCE and not _id(pid)):
                reason = "independent_full_name_identity_missing"
            elif origin == usage.SOURCE and (pid != name or not p.get("evidence_id")):
                reason = "registered_player_evidence_missing"
            elif any(s.get("player_id") == pid or usage.full_name(s.get("name")) == name for s in starting):
                reason = "listed_in_published_starting_eleven"
            elif any(s.get("player_id") == pid or usage.full_name(s.get("name")) == name for s in bench):
                reason = "listed_on_published_bench"
            if reason:
                out["excluded"].append({"side": side, "player_id": pid, "reason": reason})
                continue
            position = matching[0].get("source_position")
            role = _ROLES.get(base.get("league"), {}).get(str(position).casefold(), news_model._role(position))
            multiplier, history = usage.importance(status.get("usage", {}).get(side), engine, base, side, name, role, when)
            if history["basis"] == "neutral" and status.get("prior_lineups", {}).get(side):
                multiplier, history = usage.from_lineups(status["prior_lineups"][side], engine, base, side,
                                                         name, when, history)
            if role == "unknown" and history.get("position"):
                role = news_model._role(history["position"])
            if role == "unknown":
                out["excluded"].append({"side": side, "player_id": pid, "reason": "position_unconfirmed"})
                continue
            keeper_window = history.get("scheduled_matches", history["matches"])
            if role == "goalkeeper" and (not keeper_window or 2 * history["starts"] <= keeper_window):
                out["excluded"].append({"side": side, "player_id": pid, "role": role,
                                        "reason": "no_verified_majority_of_goalkeeper_starts",
                                        "importance_multiplier": multiplier, "player_importance": history})
                continue
            confidence = p.get("certainty") if origin == usage.SOURCE else "reported"
            if confidence not in ("reported", "confirmed"):
                continue
            absence_weight = news_model.POLICY["absence_weight"][confidence]["out"]
            onset = p.get("injury_started_at") or p.get("absence_first_seen_at") or p.get("first_recorded_at") or field_at
            if engine and training is None:
                training = engine.fit(base["season"], when, base["fixture_id"]).team_kickoffs
            club = league_registry.club_key(base["league"], base["season"], base[side]) if training else None
            duration = news_model.duration_evidence((training or {}).get(club), store.instant(onset))
            weight = absence_weight * multiplier * duration["duration_multiplier"]
            own, opponent = news_model.POLICY["role_log_rate_effects"][role]
            shifts[side] += weight * own
            shifts["away" if side == "home" else "home"] += weight * opponent
            out["applied"].append({"side": side, "player_id": pid, "name": p["name"], "role": role,
                                   "weight": weight, "absence_weight": absence_weight,
                                   "importance_multiplier": multiplier, "player_importance": history, **duration,
                                   "onset_at": onset, "onset_basis": ("documented_injury_onset"
                                                                     if p.get("injury_started_at") else "local_observation"),
                                   "evidence_grade": confidence + "_fixture_absence", "source_id": p.get("source_id", SOURCE),
                                   "evidence_id": p.get("evidence_id"), "provider_fixture_id": injuries.get("provider_fixture_id")})
    if not out["applied"]:
        return out
    bound = news_model.POLICY["max_abs_log_adjustment_per_team"]
    rates = {side: base["rates"][side] * math.exp(max(-bound, min(bound, shift))) for side, shift in shifts.items()}
    try:
        matrix, _ = football_model._goals_matrix(rates["home"], rates["away"], base["rates"]["rho"])
        probs, totals = football_model.probabilities(matrix, tuple(map(float, base["totals"]["total"])))
    except ValueError:
        return dict(out, status="scenario_outside_model_domain")
    return dict(out, status="uncalibrated_absence_sensitivity", changed=probs != base["probabilities"],
                probabilities=probs, totals=totals, rates=dict(rates, rho=base["rates"]["rho"]))
