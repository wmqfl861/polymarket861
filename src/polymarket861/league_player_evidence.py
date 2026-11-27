"""Domestic match observations and season usage from the existing free-source evidence registry."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from . import evidence, football, league_registry, news_importance, news_model, store

SOURCE = "registered-free-evidence"
MAX_ABSENCE_AGE = timedelta(hours=6)
# This provider has its own season, use and retention checks. Its data, even if
# transcribed manually or relayed by a different source, only enters via that adapter.
_DEDICATED_PROVIDER = re.compile(r"(?:^|[^a-z0-9])(?:api[\W_]*football|api[\W_]*sports)(?:\d|[^a-z0-9]|$)", re.I)
_PROVENANCE_KEYS = ("source", "source_id", "source_url", "origin", "origin_id", "origin_url",
                    "upstream", "provider", "provider_id", "locator", "url", "terms_locator")


def full_name(name):
    if not isinstance(name, str):
        return ""
    words = re.findall(r"[^\W\d_]+", unicodedata.normalize("NFKC", name).casefold())
    if len(words) < 2 or any(len(w) == 1 for w in words):
        return ""
    return " ".join(words)


def _registered_original(record, db):
    value = record["value"]
    fields = [record.get(k) for k in _PROVENANCE_KEYS]
    fields.extend(value.get(k) for k in _PROVENANCE_KEYS)
    # A later grant cannot authorize a capture made while the source was disabled.
    for at in dict.fromkeys((record["received_at"], record["first_recorded_at"])):
        registered = evidence.source_at(db, record["source_id"], at, recorded_by=at)
        if "prediction" not in evidence.granted(registered, record["field"], record["method"], conn=db, at=at):
            return False
        fields.extend(registered.get(k) for k in (*_PROVENANCE_KEYS, "name", "discovered_from"))
    return not any(_DEDICATED_PROVIDER.search(unicodedata.normalize("NFKC", field)) for field in fields
                   if isinstance(field, str))


def _sheet_members(value):
    """Return two distinct, fully identified lists; ambiguous published names require review."""
    groups, seen = [], set()
    for field in ("starters", "bench"):
        entries = value.get(field, [])
        if not isinstance(entries, list):
            return None
        members = []
        for row in entries:
            label = row.get("name") if isinstance(row, dict) else row
            ident = full_name(label)
            if not ident or ident in seen:
                return None
            seen.add(ident)
            members.append({"name": label, "player_id": ident})
        groups.append(members)
    return groups if len(groups[0]) == 11 else None


def _scoped(record, fixture, db):
    value = record["value"]
    try:
        return (_registered_original(record, db) and store.instant(record["published_at"]) <= store.instant(record["received_at"])
                <= store.instant(record["first_recorded_at"]) and value["competition"] == fixture["league"] and value["season"] == fixture["season"]
                and store.instant(value["kickoff"]) == store.instant(fixture["kickoff"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def _proof(record):
    return {k: record[k] for k in ("source_id", "evidence_id", "locator", "published_at",
                                     "received_at", "first_recorded_at")}


def _episode_start(db, row, fixture, cutoff):
    """Walk the same claim backwards. A return, withdrawal, gap or invalid revision ends continuity."""
    at = evidence.utc(cutoff)
    query = """SELECT f.document, EXISTS(SELECT 1 FROM ev_quarantine q
               JOIN ev_incident_versions iv ON iv.incident_id=q.incident_id AND iv.updated_at=q.since
               WHERE q.evidence_id=f.evidence_id AND q.since<=? AND
               json_extract(iv.document,'$.quarantine_recorded_at')<=?)
               FROM ev_facts f WHERE source_id=? AND fixture_id=? AND team_key=? AND field=? AND subject=?
               AND origin_id=? AND claim_key=? AND revision<=? AND received_at<=? AND published_at<=?
               AND valid_from<=? ORDER BY revision DESC"""
    args = (at, at, row["source_id"], row["fixture_id"], evidence.name_key(row["team"]), row["field"],
            row["subject"], row["origin_id"], row["claim_key"], row["revision"], at, at, at)
    first, native, expected = row["first_recorded_at"], None, row["revision"]
    for raw, quarantined in db.execute(query, args):
        earlier = json.loads(raw)
        if earlier["first_recorded_at"] > at:
            continue
        value = earlier["value"]
        if (earlier["revision"] != expected or quarantined or earlier["state"] != "active" or
                "prediction" not in earlier["capture_uses"] or value.get("availability") != "out" or
                value.get("certainty") not in ("reported", "confirmed") or not _scoped(earlier, fixture, db)):
            break
        if earlier["valid_until"] and earlier["valid_until"] < first:
            break
        first = min(first, earlier["first_recorded_at"])
        if native is None and earlier["field"] == "injury" and value.get("injury_started_at") is not None:
            try:
                onset = store.instant(value["injury_started_at"])
                if onset <= store.instant(earlier["published_at"]):
                    native = store.iso(onset)
            except (ValueError, TypeError, AttributeError):
                pass
        expected -= 1
    return first, native


def _absences(db, fixture, rows, cutoff):
    by_name = defaultdict(list)
    for row in rows:
        name = full_name(row["subject"])
        if name:
            by_name[name].append(row)
    players = []
    for name, claims in sorted(by_name.items()):
        # A return, doubt, conflict or denial disqualifies an outstanding absence.
        # Independent feeds and injury/suspension reports may describe the same player.
        latest = {}
        for row in sorted(claims, key=lambda r: (r["published_at"], r["first_recorded_at"], r["evidence_id"])):
            latest[row["origin_id"], row["field"], row["claim_key"]] = row
        if any(row["value"].get("availability") != "out" or
               row["value"].get("certainty") not in ("confirmed", "reported") for row in latest.values()):
            continue
        timely = [r for r in latest.values() if cutoff - MAX_ABSENCE_AGE <= store.instant(r["published_at"])]
        if not timely:
            continue
        row = max(timely, key=lambda r: (r["value"]["certainty"] == "confirmed", r["published_at"], r["evidence_id"]))
        proof = _proof(row)
        proof["absence_first_seen_at"], prior_onset = _episode_start(db, row, fixture, cutoff)
        onset = row["value"].get("injury_started_at") if row["field"] == "injury" else None
        if onset:
            try:
                if store.instant(onset) > store.instant(row["published_at"]):
                    continue
            except (ValueError, TypeError, AttributeError):
                continue
        players.append({"player_id": name, "name": row["subject"], "provider_type": "Missing Fixture",
                        "reason": row["field"], "certainty": row["value"]["certainty"],
                        "injury_started_at": onset or prior_onset, **proof})
    return players


def _read_team(db, fixture, side, cutoff, fields):
    team = fixture[side]
    rows, conflicts = {}, set()
    at = evidence.utc(cutoff)
    for field in fields:
        active, _, issues = evidence._collect(db, fixture["fixture_id"], at, "prediction",
                                             field=field, teams=(evidence.name_key(team),), prospective=True)
        if issues:
            conflicts.add(field)
        rows[field] = [r for r in active if _scoped(r, fixture, db)]
    absences = [r for field in ("availability", "injury", "suspension") for r in rows.get(field, ())]
    players = [] if conflicts & {"availability", "injury", "suspension"} else _absences(db, fixture, absences, cutoff)
    sheets = [r for r in rows.get("lineup", ()) if r["value"].get("confirmed") is True]
    sheet, dispute = evidence._selected_lineup(sheets)
    starting, bench = [], []
    sheet_proof = _proof(sheet) if sheet else None
    ambiguous = bool(dispute or "lineup" in conflicts)
    if sheet and not ambiguous:
        members = _sheet_members(sheet["value"])
        if members is not None:
            starting, bench = members
            # Two independently confirmed sheets can agree on the XI and name
            # different substitutes. Any positive listing contradicts a total absence.
            others = {}
            corroboration = set()
            for candidate in sheets:
                additional = _sheet_members(candidate["value"])
                if additional is None:
                    ambiguous = True
                    break
                corroboration.add(candidate["evidence_id"])
                for p in additional[1]:
                    others[p["player_id"]] = p
            bench = [others[k] for k in sorted(others)]
            sheet_proof["confirmed_evidence_ids"] = sorted(corroboration)
        else:
            ambiguous = True
    directories = []
    for record in rows.get("minutes", ()):
        value = record["value"]
        try:
            as_of = store.instant(value["as_of"])
            if (value.get("scope") == "domestic_season_to_date" and
                    cutoff - timedelta(days=news_importance.POLICY["directory_max_age_days"]) <= as_of
                    <= store.instant(record["published_at"])):
                directories.append({"as_of": store.iso(as_of), "players": value.get("players"),
                                    "completed_fixture_ids": value.get("completed_fixture_ids"), **_proof(record)})
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return players, starting, bench, sheet_proof, ambiguous, [] if "minutes" in conflicts else directories


def _prior_sheets(db, fixture, side, cutoff):
    """Only named starters and named substitutes can establish a player's history."""
    at, team = evidence.utc(cutoff), evidence.name_key(fixture[side])
    prefix = f"{fixture['league']}/{fixture['season']}/%"
    games = db.execute("SELECT DISTINCT fixture_id FROM ev_facts WHERE team_key=? AND fixture_id LIKE ? "
                       "AND fixture_id!=? AND field='lineup' AND received_at<=? ORDER BY fixture_id",
                       (team, prefix, fixture["fixture_id"], at))
    sheets = []
    for (fid,) in games:
        # Revisions and withdrawals take effect at the later evaluation cutoff;
        # expiration of an originally pre-kickoff XI need not erase past starts.
        expiries = [row[0] for row in db.execute(
            "SELECT DISTINCT json_extract(document,'$.value.kickoff') FROM ev_facts WHERE fixture_id=? AND "
            "team_key=? AND field='lineup' AND received_at<=?", (fid, team, at)) if row[0]]
        for kick in expiries:
            try:
                start = store.instant(kick)
            except (ValueError, TypeError, AttributeError):
                continue
            if start >= cutoff:
                continue
            active, _, disputes = evidence._collect(db, fid, at, "prediction", field="lineup", teams=(team,),
                                                    prospective=True, expires_after=evidence.utc(start - timedelta(microseconds=1)))
            active = [r for r in active if _scoped(r, {**fixture, "kickoff": start}, db)]
            sheet, issue = evidence._selected_lineup(active)
            if (disputes or issue or not sheet or sheet["value"]["confirmed"] is not True
                    or store.instant(sheet["first_recorded_at"]) >= start):
                continue
            identified = _sheet_members(sheet["value"])
            if identified is None:
                continue
            starters, substitutes = ([p["player_id"] for p in rows] for rows in identified)
            sheets.append({"fixture_id": fid, "kickoff": store.iso(start), "starters": starters,
                           "bench": substitutes, **_proof(sheet)})
    return sheets


def local_records(root, fixture, cutoff):
    path = Path(root) / "runtime.sqlite3"
    if not path.is_file():
        return {}
    cutoff = min(store.instant(cutoff), store.instant(fixture["kickoff"]) - timedelta(microseconds=1))
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ev_facts'").fetchone():
            return {}
        groups = db.execute("SELECT DISTINCT team_key,field FROM ev_facts WHERE fixture_id=? AND received_at<=? "
                            "AND field IN ('availability','injury','suspension','lineup','minutes')",
                            (fixture["fixture_id"], evidence.utc(cutoff))).fetchall()
        if not groups:
            return {}
        result = {}
        for side in ("home", "away"):
            fields = [field for team, field in groups if team == evidence.name_key(fixture[side])]
            if not fields:
                continue
            players, starters, bench, sheet, ambiguous, directories = _read_team(db, fixture, side, cutoff, fields)
            if ambiguous:
                result.setdefault("lineup_ambiguity", []).append(side)
            for field, entries, stamp in (("injuries", players, max((p["received_at"] for p in players), default=None)),
                                          ("lineup", starters, sheet and sheet["received_at"])):
                if entries:
                    item = result.setdefault(field, {"source": SOURCE, "state": "observed", "teams": {
                        s: {"state": "unknown", "players": []} for s in ("home", "away")}})
                    item["received_at"] = max(stamp, item.get("received_at", stamp))
                    item["teams"][side] = {"state": "published" if field == "lineup" else "reported",
                                           "players": entries}
                    if field == "lineup":
                        item["teams"][side]["evidence"] = sheet
                        item["teams"][side]["bench"] = bench
            if directories:
                result.setdefault("usage", {})[side] = directories
            if players and (prior := _prior_sheets(db, fixture, side, cutoff)):
                result.setdefault("prior_lineups", {})[side] = prior
        return result


def _completed(engine, base, side, at):
    if not engine or engine.league != base.get("league"):
        return None
    completed = {}
    for game in engine.seasons.get(base["season"], ()):
        if game.fixture_id == base["fixture_id"] or base[side] not in (game.home, game.away):
            continue
        if game.quarantine_reason:
            if game.kickoff < at:
                return None
            continue
        latest = football.result_as_of(game, at, grades=football.RECEIPT_GRADES)
        if latest and latest.score is not None and (latest.actual_kickoff or game.kickoff) < at:
            completed[game.fixture_id] = latest.actual_kickoff or game.kickoff
        elif game.kickoff < at:
            # A missing or withdrawn prior final cannot shrink the minutes denominator.
            return None
    return completed


def importance(directories, engine, base, side, name, role, cutoff):
    """Reconcile a complete sporting denominator before deriving a minute/start share."""
    policy = news_importance.POLICY
    neutral = {"basis": "neutral", "reason": "no_eligible_domestic_minutes_and_starts", "matches": 0}
    if not directories:
        return policy["neutral_multiplier"], neutral
    latest = max(d["as_of"] for d in directories)
    current = [d for d in directories if d["as_of"] == latest]
    if len({store.digest([d["completed_fixture_ids"], d["players"]]) for d in current}) != 1:
        return policy["neutral_multiplier"], dict(neutral, reason="conflicting_usage_directories")
    d = current[0]
    try:
        dated = store.instant(d["as_of"])
        if not (cutoff - timedelta(days=policy["directory_max_age_days"]) <= dated <= store.instant(d["published_at"])
                <= store.instant(d["received_at"]) <= store.instant(d["first_recorded_at"]) <= cutoff):
            raise ValueError
        prior = _completed(engine, base, side, dated)
        ids = d["completed_fixture_ids"]
        if (not isinstance(ids, list) or prior is None or len(prior) != len(ids) or set(prior) != set(ids)
                or not policy["minimum_matches"] <= len(prior) <= league_registry.round_count(base["league"], base["season"])):
            raise ValueError
        players = d["players"]
        if not isinstance(players, list) or not 11 <= len(players) <= 100:
            raise ValueError
        by_name, roster_labels, provider_ids = defaultdict(list), set(), set()
        for player in players:
            raw_name = player["name"]
            if not isinstance(raw_name, str) or not raw_name.strip() or len(raw_name) > 150:
                raise ValueError
            label = " ".join(unicodedata.normalize("NFKC", raw_name).casefold().split())
            ident = player.get("player_id")
            if (label in roster_labels or (ident is not None and ident in provider_ids)
                    or player["position"] not in ("GK", "DF", "MF", "FW")
                    or any(type(player[k]) is not int or player[k] < 0 for k in ("starts", "minutes"))
                    or "appearances" in player and (type(player["appearances"]) is not int
                                                    or player["appearances"] < player["starts"]
                                                    or player["appearances"] == 0 and player["minutes"] > 0)):
                raise ValueError
            roster_labels.add(label)
            if ident is not None:
                provider_ids.add(ident)
            by_name[full_name(raw_name)].append(player)
        counts = news_importance.appearance_totals(players, len(prior))
        p, = by_name[name]
        if not counts or (role != "unknown" and news_model._role(p["position"]) != role):
            raise ValueError
        matches = len(prior)
        share = (p["starts"] / matches + p["minutes"] / (90 * matches)) / 2
        multiplier = policy["minimum_multiplier"] + (policy["maximum_multiplier"] - policy["minimum_multiplier"]) * share
        return multiplier, {"basis": "domestic_season_minutes_and_starts", "matches": matches, "minutes": p["minutes"],
                            "starts": p["starts"], "position": p["position"], "competition": base["league"],
                            "season": base["season"], "team": base[side], "completed_fixture_ids": sorted(prior),
                            "as_of": d["as_of"], **_proof(d), **counts}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return policy["neutral_multiplier"], dict(neutral, reason="invalid_or_unreconciled_usage_directory")


def from_lineups(sheets, engine, base, side, name, cutoff, fallback):
    policy = news_importance.POLICY
    prior = _completed(engine, base, side, cutoff)
    if not prior:
        return policy["neutral_multiplier"], fallback
    window = dict(sorted(prior.items(), key=lambda p: (p[1], p[0]))[-policy["lineup_lookback_matches"]:])
    seen = {}
    for sheet in sheets:
        fid = sheet["fixture_id"]
        if fid not in window or store.instant(sheet["kickoff"]) != window[fid]:
            continue
        if name in sheet["starters"] or name in sheet["bench"]:
            seen[fid] = (name in sheet["starters"], sheet["evidence_id"])
    if len(seen) < policy["minimum_matches"] or len(seen) / len(window) < policy["minimum_lineup_coverage"]:
        return policy["neutral_multiplier"], fallback
    starts = sum(start for start, _ in seen.values())
    multiplier = policy["minimum_multiplier"] + (policy["maximum_multiplier"] - policy["minimum_multiplier"]) * starts / len(seen)
    return multiplier, {"basis": "confirmed_domestic_lineup_starts", "competition": base["league"], "season": base["season"],
                        "team": base[side], "matches": len(seen), "starts": starts, "scheduled_matches": len(window),
                        "directory_reason": fallback["reason"], "evidence_ids": [e for _, e in seen.values()]}
