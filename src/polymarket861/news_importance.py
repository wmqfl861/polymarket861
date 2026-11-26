"""Point-in-time EPL minutes/starts for the research news arm."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from . import evidence, football, news_minutes, news_roster, store

POLICY = {
    "directory_basis": "mean of minutes/(90*completed EPL matches) and starts/completed EPL matches",
    "required_team_starts_per_match": 11,
    "maximum_goalkeeper_starts_per_match": 1,
    "minimum_team_start_coverage": 0.90,
    "directory_max_age_days": 7,
    "lineup_basis": "share of prior completed EPL matches with a confirmed start",
    "lineup_lookback_matches": 8,
    "minimum_matches": 3,
    "minimum_lineup_coverage": 0.75,
    "minimum_multiplier": 0.25,
    "maximum_multiplier": 1.0,
    "neutral_multiplier": 0.55,
    "neutral_basis": "predeclared squad-average prior (11 regular starters among about 27.5 players)",
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


def directory_players(db, root, fixture, team, cutoff, games=None):
    """Reopen the as-of native directory; key rows only by unique publisher ID."""
    at, club = store.iso(cutoff), football.team_key(team)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='news_usage_history'").fetchone():
        return {}, "no_received_player_directory"
    limit = store.iso(store.instant(at) - timedelta(days=POLICY["directory_max_age_days"]))
    rows = db.execute("SELECT * FROM news_usage_history WHERE season=? AND club=? AND known_at<=? "
                      "AND received_at<=? AND provider_as_of<=? AND provider_as_of>=? ORDER BY known_at DESC",
                      (fixture["season"], club, at, at, at, limit))
    if games is None:
        games = news_minutes.club_games(db, root, fixture["season"], team, at)
    rejected = None
    for row in rows:
        players, reason = _reconciled_directory(root, row, fixture, team, games)
        if players:
            if rejected:
                for player in players.values():
                    player.update(directory_fallback_reason="newer_directory_does_not_reconcile",
                                  rejected_directory_sha256=rejected["original_sha256"],
                                  rejected_provider_as_of=rejected["provider_as_of"])
            return players, None
        if reason != "team_appearances_do_not_reconcile":
            return {}, reason
        if rejected is None:
            rejected = row
    if rejected:
        return {}, "team_appearances_do_not_reconcile"
    old = news_minutes.latest(db, fixture["season"], club, at)
    return {}, "stale_player_directory" if old else "no_received_player_directory"


def appearance_totals(players, played):
    """Bound the club's EPL usage, including transparent losses from departed players."""
    if not 1 <= played <= 38:
        return None
    starts = sum(p["starts"] for p in players)
    required = POLICY["required_team_starts_per_match"] * played
    keepers = sum(p["starts"] for p in players if p["position"] == "GK")
    if (not required * POLICY["minimum_team_start_coverage"] <= starts <= required
            or keepers > POLICY["maximum_goalkeeper_starts_per_match"] * played
            or required - starts < played - keepers
            or any(p["starts"] > played or p.get("appearances", 0) > played or p["minutes"] > 90 * played for p in players)
            or sum(p["minutes"] for p in players) > 11 * 90 * played):
        return None
    return {"team_starts": starts, "expected_team_starts": required,
            "goalkeeper_starts": keepers, "start_coverage": starts / required}


def _reconciled_directory(root, row, fixture, team, games):
    stamp = row["provider_as_of"]
    # An older directory is a valid historical share for the matches it covers.
    # A new or corrected prior final must actually have been known by its timestamp.
    scope = [g for g in games if g["fixture_id"] != fixture["fixture_id"] and g["kickoff"] < stamp]
    played = len(scope)
    if (not POLICY["minimum_matches"] <= played <= 38
            or any(not g["completed"] or not g["received"] or g["received"] > stamp for g in scope)):
        return {}, "prior_results_incomplete_at_directory_snapshot"
    try:
        players = news_minutes.verified_players(root, row, team, fixture["season"])
    except (KeyError, ValueError, TypeError, OSError):
        return {}, "invalid_player_directory_proof"
    totals = appearance_totals(players, played)
    if totals is None:
        return {}, "team_appearances_do_not_reconcile"
    common = {
        "basis": "epl_season_minutes_and_starts", "matches": played, "known_at": row["known_at"],
        "provider_as_of": stamp, "received_at": row["received_at"], "original_sha256": row["original_sha256"],
        "season": fixture["season"], "team": team, **totals,
        "later_completed_matches": sum(g["completed"] and g["kickoff"] >= stamp for g in games),
        "score_disputed_fixtures": [g["fixture_id"] for g in scope if g["hold_reason"] == "cross_source_score_conflict"],
        "fixtures_sha256": store.digest([(g["fixture_id"], g["kickoff"], g["received"]) for g in scope]),
        "url": news_minutes.source_url(team), "attribution": news_minutes.ATTRIBUTION,
    }
    return {p["id"]: common | {k: v for k, v in p.items() if k != "id"}
            | {"provider_player_id": p["id"]} for p in players}, None


def _directory(db, root, fixture, team, cutoff, games, players):
    rows, reason = directory_players(db, root, fixture, team, cutoff, games)
    if not rows:
        return {}, reason
    ordinary = [p for p in players.values() if p.get("identity_basis") != "touchline_directory_id"]
    names = _identities(ordinary)
    grounded = defaultdict(list)
    aliases = defaultdict(list)
    for id_, member in players.items():
        pid = member.get("provider_player_id")
        if type(pid) is not int or pid <= 0 or pid not in rows:
            continue
        reviewed = member.get("identity_basis") == "reviewed_provider_alias" and member.get("alias_receipt")
        joined = (member.get("identity_basis") == "touchline_id_to_roster" and
                  member.get("directory_sha256") == rows[pid]["original_sha256"])
        if reviewed or joined:
            aliases[pid].append(id_)
    for provider_id, p in rows.items():
        ident = _resolve(p["names"], names)
        direct = f"touchline:epl:{provider_id}"
        member = players.get(direct, {})
        # The caller has already revalidated alias receipts or numeric members
        # against the same club and cutoff. Refuse competing asserted owners.
        bound = list(aliases[provider_id])
        if member.get("identity_basis") == "touchline_directory_id" and member.get("provider_player_id") == provider_id:
            bound.append(direct)
        if bound:
            if len(bound) != 1 or ident and bound[0] != ident:
                continue
            ident = bound[0]
        if ident:
            grounded[ident].append(p)
    return {ident: rows[0] for ident, rows in grounded.items() if len(rows) == 1}, "no_unique_player_directory_identity"


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
        "SELECT name FROM sqlite_master WHERE name='ev_facts'"
    )}
    result = {}
    if not any(view["players"] for view in squads.values()):
        return result
    for side in ("home", "away"):
        team = fixture[side]
        club = football.team_key(team)
        players = {p["player_id"]: p for p in squads[club]["players"]}
        if not players:
            continue
        games = [g for g in news_minutes.club_games(db, data_dir, fixture["season"], team, at)
                 if g["fixture_id"] != fixture["fixture_id"]]
        directory, reason = _directory(db, data_dir, fixture, team, at, games, players)
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
            if (row.get("provider_player_id") is not None and
                    row["provider_player_id"] != history["provider_player_id"]):
                return neutral, {"basis": "neutral", "reason": "provider_player_id_mismatch"}
            minutes = history["minutes"]
            if (type(minutes) is not int or not 0 <= minutes <= 90 * matches
                    or not store.instant(history["provider_as_of"]) <= store.instant(history["known_at"])
                    or cutoff - store.instant(history["provider_as_of"]) > timedelta(days=POLICY["directory_max_age_days"])):
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
