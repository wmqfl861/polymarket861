"""Point-in-time EPL minutes/starts for the research news arm."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from . import evidence, football, news_minutes, news_roster, result_freshness, store
from .score_bounds import valid_score

POLICY = {
    "directory_basis": "mean of minutes/(90*completed EPL matches) and starts/completed EPL matches",
    "required_team_starts_per_match": 11,
    "required_goalkeeper_starts_per_match": 1,
    "directory_max_age_days": 7,
    "lineup_basis": "share of prior completed EPL matches with a confirmed start",
    "lineup_lookback_matches": 8,
    "minimum_matches": 3,
    "minimum_lineup_coverage": 0.75,
    "minimum_multiplier": 0.25,
    "maximum_multiplier": 1.0,
    "neutral_multiplier": 1.0,
}


def _identities(players):
    """Use only a stable id or an exact, unique published roster name/alias."""
    index = defaultdict(set)
    for player in players:
        ident = player["player_id"]
        for value in (ident, player.get("name"), *player.get("aliases", [])):
            if isinstance(value, str) and value.strip():
                index[evidence.name_key(value)].add(ident)
    return index


def _resolve(values, names):
    labels = [evidence.name_key(v) for v in values if isinstance(v, str)]
    full_names = [v for v in labels if len(v.split()) > 1]
    if full_names and not any(names.get(v) for v in full_names):
        return None
    matches = set().union(*(names.get(v, ()) for v in labels))
    return next(iter(matches)) if len(matches) == 1 else None


def _directory(db, root, fixture, team, cutoff, games, names, tables):
    club = football.team_key(team)
    if "news_usage_history" not in tables:
        return {}, "no_received_player_directory"
    row = news_minutes.latest(db, fixture["season"], club, cutoff)
    if not row:
        return {}, "no_received_player_directory"
    if store.instant(cutoff) - store.instant(row["provider_as_of"]) > timedelta(days=POLICY["directory_max_age_days"]):
        return {}, "stale_player_directory"
    # The denominator must cover this club's prior league fixtures. An unresolved
    # or newly played match makes a cumulative directory unusable until reconciled.
    played = len(games)
    if (played < POLICY["minimum_matches"] or played > 38 or
            any(not g["completed"] or g["kickoff"] >= row["provider_as_of"] or
                g["received"] > row["provider_as_of"] for g in games)):
        return {}, "prior_results_incomplete_at_directory_snapshot"
    try:
        raw = store.read_blob(root, row["original_sha256"])
        stamp, players = news_minutes.parse(raw, team, fixture["season"], row["received_at"])
        if stamp != row["provider_as_of"] or store.digest(players) != row["usage_sha256"]:
            return {}, "invalid_player_directory_proof"
    except (KeyError, ValueError, TypeError, OSError):
        return {}, "invalid_player_directory_proof"
    if (sum(p["starts"] for p in players) != POLICY["required_team_starts_per_match"] * played
            or sum(p["starts"] for p in players if p["position"] == "GK") !=
            POLICY["required_goalkeeper_starts_per_match"] * played
            or any(p["starts"] > played or p["minutes"] > 90 * played for p in players)
            or sum(p["minutes"] for p in players) > 11 * 90 * played):
        return {}, "team_appearances_do_not_reconcile"
    grounded = defaultdict(list)
    for p in players:
        ident = _resolve(p["names"], names)
        if ident:
            grounded[ident].append(p)
    result = {}
    for ident, matches in grounded.items():
        if len(matches) != 1:
            continue
        p = matches[0]
        result[ident] = {
            "basis": "epl_season_minutes_and_starts", "matches": played, "starts": p["starts"],
            "minutes": p["minutes"], "known_at": row["known_at"], "provider_as_of": stamp,
            "original_sha256": row["original_sha256"], "provider_player_id": p["id"],
            "url": news_minutes.source_url(team), "attribution": news_minutes.ATTRIBUTION,
            "fixtures_sha256": store.digest([(g["fixture_id"], g["kickoff"], g["received"]) for g in games]),
        }
    return result, "no_unique_player_directory_identity"


def _lineup_member(row, names, players):
    if isinstance(row, dict):
        ident = row.get("player_id") or row.get("id")
        named = _resolve([row.get("name")], names)
        if isinstance(ident, str) and ident in players:
            return ident if named is None or named == ident else None
        if isinstance(ident, str) and ident.startswith("enwiki:"):
            return None
        return named
    return _resolve([row], names)


def _lineups(db, fixture, team, cutoff, games, players, tables):
    if "ev_facts" not in tables or not games:
        return {}
    # Approved evidence sources, revisions, withdrawals, source permissions and
    # quarantine all use the forecast cutoff, including local registration clocks.
    names = _identities(players.values())
    observations = defaultdict(list)
    window = games[-POLICY["lineup_lookback_matches"]:]
    for game in window:
        if not game["completed"]:
            continue
        sheet, _, conflicts = evidence._collect(
            db, game["fixture_id"], evidence.utc(cutoff), "prediction", field="lineup",
            teams=[evidence.name_key(team)], prospective=True,
            # A pre-match sheet can expire at kickoff for prospective use.
            # Later known withdrawals/corrections still supersede that sheet.
            expires_after=evidence.utc(store.instant(game["kickoff"]) - timedelta(microseconds=1)),
        )
        selected, issue = evidence._selected_lineup(sheet)
        if conflicts or issue or not selected or selected["value"].get("confirmed") is not True:
            continue
        data = selected["value"]
        starters = [_lineup_member(v, names, players) for v in data["starters"]]
        if len(starters) != 11 or len(set(filter(None, starters))) != sum(p is not None for p in starters):
            continue
        bench = data.get("bench", [])
        if not isinstance(bench, list):
            continue
        listed = {_lineup_member(v, names, players) for v in bench} - {None}
        starting = set(starters) - {None}
        if listed & starting:
            continue
        # An incomplete identity join never makes an unlisted player a nonstarter.
        # Even for an entirely resolved XI, membership must have been observed
        # at that match (a later signing has no historical zeroes for this club).
        if len(starting) == 11:
            prior = news_roster.roster_views(db, [team], fixture["season"], game["kickoff"])
            listed.update(p["player_id"] for p in prior[football.team_key(team)]["players"])
        seen = max(store.iso(selected["first_recorded_at"]), store.iso(selected["received_at"]), game["received"])
        for ident in (listed | starting) & players.keys():
            observations[ident].append((selected["evidence_id"], ident in starting, seen))
    result = {}
    for ident, obs in observations.items():
        if (len(obs) >= POLICY["minimum_matches"] and
                len(obs) / len(window) >= POLICY["minimum_lineup_coverage"]):
            result[ident] = {
                "basis": "confirmed_lineup_starts", "matches": len(obs),
                "scheduled_matches": len(window), "starts": sum(start for _, start, _ in obs),
                "known_at": max(stamp for _, _, stamp in obs), "evidence_ids": [eid for eid, _, _ in obs],
            }
    return result


def player_history(db, fixture, cutoff, squads, data_dir):
    """Return the grounded evidence for both current squads using the same SQLite snapshot."""
    at = store.iso(cutoff)
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE name IN ('news_usage_history','ev_facts')"
    )}
    result = {}
    if not any(view["players"] for view in squads.values()):
        return result
    rows = db.execute(result_freshness._AS_OF.format(times="result_event_times"),
                      {"season": fixture["season"], "at": at})
    matches = defaultdict(list)
    for row in rows:
        if row["fixture_id"] == fixture["fixture_id"] or row["kickoff"] >= at:
            continue
        game = dict(row)
        game["completed"] = valid_score((row["goals"], row["away_goals"])) and bool(row["received"])
        for side in ("home", "away"):
            matches[football.team_key(row[side])].append(game)
    for side in ("home", "away"):
        team = fixture[side]
        club = football.team_key(team)
        players = {p["player_id"]: p for p in squads[club]["players"]}
        if not players:
            continue
        games = matches[club]
        directory, reason = _directory(db, data_dir, fixture, team, at, games, _identities(players.values()), tables)
        lineups = _lineups(db, fixture, team, at, games, players, tables) if players.keys() - directory.keys() else {}
        for ident in players:
            weight = directory.get(ident) or lineups.get(ident) or {
                "basis": "neutral", "reason": reason, "matches": 0,
            }
            result[ident] = dict(weight, team=team, season=fixture["season"], player_id=ident)
    return result


def assess(row, cutoff, *, local):
    """Compute the multiplier from bounded counts; never accept a caller's numerical weight."""
    neutral = POLICY["neutral_multiplier"]
    history = row.get("player_importance")
    if not isinstance(history, dict) or not local:
        return neutral, {"basis": "neutral", "reason": "no_local_as_of_player_history"}
    if history.get("basis") == "neutral":
        return neutral, history
    try:
        matches, starts, basis = (history[k] for k in ("matches", "starts", "basis"))
        if (history["player_id"] != row["player_id"] or football.team_key(history["team"]) != football.team_key(row["team"])
                or store.instant(history["known_at"]) > cutoff
                or type(matches) is not int or not POLICY["minimum_matches"] <= matches <= 38
                or type(starts) is not int or not 0 <= starts <= matches):
            raise ValueError
        share = starts / matches
        if basis == "epl_season_minutes_and_starts":
            minutes = history["minutes"]
            if (type(minutes) is not int or not 0 <= minutes <= 90 * matches
                    or not store.instant(history["provider_as_of"]) <= store.instant(history["known_at"])
                    or cutoff - store.instant(history["provider_as_of"]) > timedelta(days=POLICY["directory_max_age_days"])
                    or (row.get("provider_player_id") is not None and
                        row["provider_player_id"] != history["provider_player_id"])):
                raise ValueError
            share = (share + minutes / (90 * matches)) / 2
        elif basis == "confirmed_lineup_starts":
            eligible = history["scheduled_matches"]
            if (type(eligible) is not int or not matches <= eligible <= POLICY["lineup_lookback_matches"]
                    or matches / eligible < POLICY["minimum_lineup_coverage"]
                    or len(set(history["evidence_ids"])) != matches):
                raise ValueError
        else:
            raise ValueError
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        return neutral, {"basis": "neutral", "reason": "invalid_or_unavailable_player_history"}
    return POLICY["minimum_multiplier"] + (POLICY["maximum_multiplier"] - POLICY["minimum_multiplier"]) * share, history
