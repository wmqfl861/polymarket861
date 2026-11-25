"""Prospective schedule revisions from independent OpenLigaDB and Gamma HTTP originals."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from contextlib import ExitStack, closing, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from . import (
    archive_proof,
    dbscope,
    ledger_terms,
    market_provider,
    opportunity,
    result_admission,
    result_feed,
    store,
)

SOURCE = "openligadb+gamma-schedule"
WEEK = timedelta(days=7)
MAX_SHIFT = timedelta(hours=36)
MAX_AGE = timedelta(hours=24)
REFRESH = timedelta(hours=6)
DECISION_LEAD = timedelta(hours=1)
_dispatch_checks = ContextVar("fixed_b1_schedule_checks", default=None)
# The canonical official competition directory confirmed this ID on 2026-11-25.
KNOWN_EPL = {2026: 5996}
SCHEMA = """
CREATE TABLE IF NOT EXISTS epl_schedule_proofs(
 fixture TEXT,version INTEGER,proof_digest TEXT NOT NULL,proof TEXT NOT NULL,PRIMARY KEY(fixture,version));
CREATE TABLE IF NOT EXISTS epl_schedule_holds(
 fixture TEXT,version INTEGER,at TEXT,reason TEXT,PRIMARY KEY(fixture,version,at));
CREATE TABLE IF NOT EXISTS epl_schedule_result_receipts(
 fixture TEXT,version INTEGER,at TEXT NOT NULL,PRIMARY KEY(fixture,version));
CREATE TABLE IF NOT EXISTS epl_schedule_reviews(
 fixture TEXT,version INTEGER,at TEXT,action TEXT,reviewer TEXT,reason TEXT,proof TEXT,
 PRIMARY KEY(fixture,version,at));
CREATE TABLE IF NOT EXISTS epl_schedule_competitions(
 year INTEGER PRIMARY KEY,sha TEXT NOT NULL,received TEXT NOT NULL);
"""


def _db(root):
    db = result_admission.database(root)
    dbscope.prepare(db, "schedule-consensus", SCHEMA)
    return db


def _label(year):
    return f"Premier League {year}/{year + 1}"


def _competition(rows, year):
    candidates = [
        r
        for r in rows
        if r.get("leagueShortcut") == "pl"
        and str(r.get("leagueSeason")) == str(year)
        and r.get("leagueName") == _label(year)
        and str((r.get("sport") or {}).get("sportName", "")).casefold()
        in {"fußball", "fussball", "soccer", "football"}
    ]
    if (
        len(candidates) != 1
        or type(candidates[0].get("leagueId")) is not int
        or candidates[0]["leagueId"] <= 0
    ):
        raise ValueError("unverified or ambiguous OpenLigaDB EPL competition")
    return candidates[0]["leagueId"]


def _league_id(root, db, year, at):
    if year in KNOWN_EPL:
        return KNOWN_EPL[year]
    config = db.execute(
        "SELECT * FROM epl_schedule_competitions WHERE year=? AND received<=?",
        (year, at),
    ).fetchone()
    if not config:
        raise ValueError("no native competition directory registration")
    registry = f"{result_feed.BASE}/getavailableleagues/{year}"
    obs = db.execute(
        "SELECT metadata FROM observations WHERE source='openligadb-competition-registry' AND observed_at=? AND sha256=?",
        (config["received"], config["sha"]),
    ).fetchone()
    if not obs or json.loads(obs[0]).get("url") != registry:
        raise ValueError("unverified competition receipt")
    raw = store.read_blob(root, config["sha"])
    if hashlib.sha256(raw).hexdigest() != config["sha"]:
        raise ValueError("invalid competition directory hash")
    return _competition(json.loads(raw), year)


def _register_league(root, year):
    if year in KNOWN_EPL:
        return
    with closing(_db(root)) as db:
        if db.execute(
            "SELECT 1 FROM epl_schedule_competitions WHERE year=?", (year,)
        ).fetchone():
            return
    url = f"{result_feed.BASE}/getavailableleagues/{year}"
    raw = result_feed._get(url)
    _competition(json.loads(raw), year)
    ref = store.archive_bytes(
        root,
        "openligadb-competition-registry",
        raw,
        metadata={
            "url": url,
            "license": "ODbL-1.0",
            "attribution": result_feed.ATTRIBUTION,
        },
    )
    with closing(_db(root)) as db, db:
        db.execute(
            "INSERT OR IGNORE INTO epl_schedule_competitions VALUES(?,?,?)",
            (year, ref["sha256"], ref["observed_at"]),
        )


def _round(root, db, year, rnd, at):
    # Only collector request rows can introduce a schedule. Uploaded bytes and the legacy candidate cache cannot.
    row = db.execute(
        "SELECT * FROM rf_native_requests WHERE season=? AND scope=? AND received_at<=? "
        "ORDER BY received_at DESC LIMIT 1",
        (year, str(rnd), at),
    ).fetchone()
    url = f"{result_feed.BASE}/getmatchdata/pl/{year}/{rnd}"
    if (
        not row
        or row["url"] != url
        or not store.instant(row["started_at"])
        <= store.instant(row["received_at"])
        <= store.instant(at)
    ):
        raise ValueError("missing native OpenLigaDB matchweek request")
    if store.instant(at) - store.instant(row["received_at"]) > MAX_AGE:
        raise ValueError("stale OpenLigaDB matchweek request")
    obs = db.execute(
        "SELECT metadata FROM observations WHERE source='openligadb-results' AND sha256=? AND observed_at=?",
        (row["sha"], row["received_at"]),
    ).fetchone()
    meta = json.loads(obs[0]) if obs else {}
    if meta.get("url") != url or meta.get("license") != "ODbL-1.0":
        raise ValueError("invalid OpenLigaDB origin or licence")
    blob = dbscope.verified_bytes(
        root,
        store.blob_path(root, row["sha"]),
        row["sha"],
        "invalid OpenLigaDB matchweek archive",
    )
    data = json.loads(blob)
    if not isinstance(data, list) or not data:
        raise ValueError("empty native OpenLigaDB matchweek")
    reviewed = [
        next(result_feed._full_time([native], year, only_round=rnd)) for native in data
    ]
    ids = Counter(row[1] for row in reviewed)
    fixtures = Counter(row[0] for row in reviewed)
    ambiguous_pairs = set()
    league_id = _league_id(root, db, year, at)
    result = {}
    for native, parsed in zip(data, reviewed, strict=True):
        fid, mid, _, kick, score, error = parsed
        teams = [native["team1"], native["team2"]]
        if (
            native.get("leagueName") != _label(year)
            or native.get("leagueId") != league_id
        ):
            raise ValueError("incorrect registered OpenLigaDB EPL season")
        if (
            native.get("matchIsFinished") is not False
            or score is not None
            or error
            or native.get("goals")
            or native.get("matchResults")
            or any(type(t.get("teamId")) is not int or t["teamId"] <= 0 for t in teams)
            or teams[0]["teamId"] == teams[1]["teamId"]
        ):
            continue
        names = [
            re.sub(r"\bF\.C\.", "FC", t["teamName"], flags=re.IGNORECASE) for t in teams
        ]
        pair = tuple(opportunity._club(name) for name in names)
        if (
            not all(pair)
            or pair[0] == pair[1]
            or pair in result
            or ids[mid] != 1
            or fixtures[fid] != 1
        ):
            ambiguous_pairs.add(pair)
            continue
        result[pair] = {
            "fixture": fid,
            "teams": names,
            "kickoff": store.iso(kick),
            "match_id": mid,
            "league_id": league_id,
            "round": rnd,
            "url": url,
            "sha256": row["sha"],
            "received_at": row["received_at"],
            "requested_at": row["started_at"],
        }
    for pair in ambiguous_pairs:
        result[pair] = {
            "error": "ambiguous OpenLigaDB home/away pair or match ID",
            "kickoff": None,
        }
    return result


def _gamma(root, db, r, at):
    slug = r["slug"]
    match = market_provider.FIXTURE.fullmatch(slug)
    if not match or match[5] or r["fixture"] != slug:
        raise ValueError("not a main EPL exchange event")
    if store.instant(at) - store.instant(r["received_at"]) > MAX_AGE:
        raise ValueError("stale Gamma archive")
    native = archive_proof._event_capture(db, root, r["sha"], r["received_at"], slug)
    kickoff = store.iso(native["startTime"])
    tags = {
        str(t.get("slug", "")).casefold()
        for t in native.get("tags", [])
        if isinstance(t, dict)
    }
    if (
        str(native.get("id")) != r["event_id"]
        or kickoff != store.iso(r["kickoff"])
        or kickoff[:10] != match[4]
        or not str(native.get("gameId") or "").isdigit()
        or not {"soccer", "premier-league"} <= tags
        or native.get("active") is not True
        or any(native.get(k) is True for k in ("closed", "archived", "live", "ended"))
    ):
        raise ValueError("ineligible Gamma EPL identity or game state")
    clubs = []
    for index, side in enumerate(("home", "away")):
        choices = [t for t in native.get("teams", []) if t.get("ordering") == side]
        if (
            len(choices) != 1
            or not choices[0].get("name")
            or str(choices[0].get("abbreviation", "")).casefold() != match[2 + index]
        ):
            raise ValueError("Gamma lacks unique ordered EPL participants")
        clubs.append(choices[0]["name"])
    pair = tuple(opportunity._club(n) for n in clubs)
    if len(set(pair)) != 2 or not all(pair):
        raise ValueError("ambiguous Gamma participants")
    templates = set()
    for market in market_provider.normalized_markets(native):
        if market["mapping_error"] or market["family"] not in ("moneyline", "total"):
            continue
        if market["game_times"] != [kickoff]:
            raise ValueError("Gamma regulation market contradicts the event time")
        try:
            _, rule = ledger_terms._interpret(
                market | {"event_teams": native["teams"]}, "yes"
            )
        except ValueError:
            continue
        templates.add(rule["template_sha256"])
    if not templates:
        raise ValueError("no independently parsed regulation market on the main event")
    return pair, {
        "slug": slug,
        "event_id": r["event_id"],
        "game_id": native["gameId"],
        "teams": clubs,
        "kickoff": kickoff,
        "received_at": r["received_at"],
        "sha256": r["sha"],
        "regulation_templates": sorted(templates),
    }


def _events(root, db, now):
    by_pair = defaultdict(list)
    errors = []
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ma_events'").fetchone():
        return by_pair, errors
    # Use the latest version of each primary event, including conflicting rows received together.
    rows = db.execute(
        "WITH latest AS (SELECT *,dense_rank() OVER(PARTITION BY slug ORDER BY received_at DESC) rk "
        "FROM ma_events WHERE slug=fixture AND received_at<=?) SELECT * FROM latest WHERE rk=1 "
        "AND received_at>=? AND julianday(kickoff)>=julianday(?) ORDER BY slug,sha",
        (store.iso(now), store.iso(now - MAX_AGE), store.iso(now - MAX_SHIFT)),
    )
    for r in rows:
        try:
            pair, event = _gamma(root, db, r, store.iso(now))
            by_pair[pair].append(event)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            errors.append({"slug": r["slug"], "reason": str(exc)})
    return by_pair, errors


def _upcoming(db, year, now, markets=None):
    # Retain just-started fixtures in diagnostics to reject late corrections to a future replacement.
    near = {
        pair
        for pair, events in (markets or {}).items()
        if any(now < store.instant(e["kickoff"]) <= now + WEEK for e in events)
    }
    active = set(markets or {})
    approved = {
        (r[0], r[1])
        for r in db.execute("SELECT fixture,version FROM epl_schedule_proofs")
    }
    return [
        f
        for f in store.eligible_fixture_rows(db, season=result_admission.scope(year))
        if now - MAX_SHIFT <= store.instant(f["kickoff"]) <= now + WEEK
        or (
            f["result_version"] is None
            and (
                tuple(opportunity._club(f[k]) for k in ("home", "away")) in near
                or (f["fixture_id"], f["schedule_version"]) in approved
                and now < store.instant(f["kickoff"])
                and tuple(opportunity._club(f[k]) for k in ("home", "away")) in active
            )
        )
    ]


def _choose(f, ol, markets, ambiguous, now, *, reviewed=False):
    if ambiguous:
        raise ValueError("non-unique local fixture identity")
    if ol and ol.get("error"):
        raise ValueError(ol["error"])
    if not ol or ol["fixture"] != f["fixture_id"]:
        raise ValueError("missing uniquely matched OpenLigaDB regulation fixture")
    if len(markets) != 1:
        raise ValueError("missing or ambiguous native Gamma main event")
    proposed = store.instant(ol["kickoff"])
    prior = store.instant(f["kickoff"])
    year = int(f["season"][:4])
    if not (f"{year}-08-01" <= ol["kickoff"][:10] < f"{year + 1}-08-01"):
        raise ValueError("proposed kickoff is outside the registered EPL season")
    if ol["kickoff"] != markets[0]["kickoff"]:
        raise ValueError("independent sources disagree on precise UTC kickoff")
    if proposed <= now or not reviewed and prior <= now:
        raise ValueError("original or proposed kickoff has already passed")
    if f["precision"] not in {"minute", "second"} or f["result_version"] is not None:
        raise ValueError("ineligible registered schedule or result already present")
    if prior != proposed:
        decision = proposed if reviewed else min(prior, proposed)
        if decision - now <= DECISION_LEAD:
            raise ValueError(
                "automatic revision arrived after the earlier T-60 decision"
            )
        if not reviewed and abs(prior - proposed) > MAX_SHIFT:
            raise ValueError("kickoff shift exceeds the automatic 36-hour window")
        if f["schedule_source"] not in {"openfootball", SOURCE}:
            raise ValueError(
                "existing independently adjudicated schedule requires review"
            )
    return markets[0]


def _hold(db, fid, version, at, reason=None):
    old = db.execute(
        "SELECT reason FROM epl_schedule_holds WHERE fixture=? AND version=? AND at<=? ORDER BY at DESC LIMIT 1",
        (fid, version, at),
    ).fetchone()
    if (old[0] if old else None) != reason:
        db.execute(
            "INSERT OR REPLACE INTO epl_schedule_holds VALUES(?,?,?,?)",
            (fid, version, at, reason),
        )


def attest(root, db, fixture, cutoff):
    """Reopen both original witnesses to a derived kickoff, bounded by its real admission and later holds."""
    fid = fixture["fixture_id"]
    version = fixture["version"]
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='epl_schedule_holds'"
    ).fetchone():
        raise ValueError("missing native schedule confirmation registry")
    held = db.execute(
        "SELECT reason FROM epl_schedule_holds WHERE fixture=? AND version=? AND at<=? ORDER BY at DESC LIMIT 1",
        (fid, version, cutoff),
    ).fetchone()
    if held and held[0]:
        raise ValueError(
            "independently reconciled schedule is under review: " + held[0]
        )

    def verify():
        row = db.execute(
            "SELECT * FROM epl_schedule_proofs WHERE fixture=? AND version=?",
            (fid, version),
        ).fetchone()
        if not row:
            raise ValueError("missing original schedule confirmation")
        proof = json.loads(row["proof"])
        cutoff_when_accepted = fixture["recorded_at"]
        if (
            store.digest(proof) != row["proof_digest"]
            or fixture["locator"] != "schedule-proof:" + row["proof_digest"]
            or fid != proof["fixture_id"]
            or fixture["kickoff"] != proof["kickoff"]
            or fixture["round_id"] != proof["round"]
            or fixture["season"] != proof["season"]
        ):
            raise ValueError("schedule identity differs from its frozen confirmation")
        pair = tuple(opportunity._club(fixture[k]) for k in ("home", "away"))
        observed = _round(
            root,
            db,
            int(fixture["season"][:4]),
            fixture["round_id"],
            cutoff_when_accepted,
        ).get(pair)
        gamma = proof["gamma"]
        raw = db.execute(
            "SELECT * FROM ma_events WHERE slug=fixture AND slug=? AND received_at=? AND sha=?",
            (gamma["slug"], gamma["received_at"], gamma["sha256"]),
        ).fetchone()
        if not raw:
            raise ValueError("the original exchange schedule receipt is absent")
        gamma_pair, verified = _gamma(root, db, raw, cutoff_when_accepted)
        if (
            not observed
            or observed != proof["openligadb"]
            or gamma_pair != pair
            or verified != gamma
            or observed["kickoff"] != fixture["kickoff"]
            or verified["kickoff"] != fixture["kickoff"]
            or max(gamma["received_at"], observed["received_at"]) > cutoff_when_accepted
        ):
            raise ValueError("native sources no longer certify the stored kickoff")

    try:
        dbscope.checked(root, ("approved_schedule_sources", fid, version), verify)
    except (KeyError, TypeError, OSError) as exc:
        raise ValueError(
            "original schedule evidence is unavailable or incomplete"
        ) from exc


def result_links(db):
    """Only proven revisions can bridge a certified score onto a differently timed raw fixture."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='epl_schedule_proofs'"
    ).fetchone():
        return {}, {}
    links = {}
    for row in db.execute(
        "SELECT p.*,f.kickoff,f.locator,f.known_at FROM epl_schedule_proofs p JOIN fixture_versions f "
        "ON f.fixture_id=p.fixture AND f.version=p.version WHERE f.source=? AND "
        "(SELECT h.reason FROM epl_schedule_holds h WHERE h.fixture=p.fixture AND h.version=p.version ORDER BY h.at DESC LIMIT 1) IS NULL "
        "ORDER BY f.version",
        (SOURCE,),
    ):
        raw = json.loads(row["proof"])
        if (
            store.digest(raw) == row["proof_digest"]
            and raw["fixture_id"] == row["fixture"]
            and raw["kickoff"] == row["kickoff"]
            and row["locator"] == "schedule-proof:" + row["proof_digest"]
        ):
            links[row["fixture"], row["kickoff"]] = (
                row["known_at"],
                row["proof_digest"],
            )
    reread = {
        (r["fixture"], r["version"]): r["at"]
        for r in db.execute("SELECT * FROM epl_schedule_result_receipts")
    }
    return links, reread


def _recheck_early_results(db, at):
    # The immutable source file may still place an advanced match in the future.
    # Re-read its registered final after that old timestamp; never invent an earlier learning receipt.
    db.execute(
        "INSERT OR IGNORE INTO epl_schedule_result_receipts "
        "SELECT r.fixture_id,r.version,? FROM result_versions r JOIN result_schedule_refs s USING(fixture_id,version) "
        "JOIN epl_schedule_proofs p ON p.fixture=r.fixture_id AND s.kickoff=json_extract(p.proof,'$.kickoff') "
        "WHERE r.source!='openfootball' GROUP BY r.fixture_id,r.version "
        "HAVING max(r.recorded_at,r.received_at)<=max(json_extract(p.proof,'$.previous')) AND "
        "max(json_extract(p.proof,'$.previous'))<?",
        (at, at),
    )


def _review(db, f, at):
    row = db.execute(
        "SELECT * FROM epl_schedule_reviews WHERE fixture=? AND version=? AND at<=? ORDER BY at DESC LIMIT 1",
        (f["fixture_id"], f["schedule_version"], at),
    ).fetchone()
    return dict(row) if row else None


def readiness(
    root,
    *,
    season=None,
    round_id=None,
    news_days=None,
    team=None,
    fixture_ids=None,
    at=None,
    _with_frontier=False,
    _connection=None,
):
    """Check the currently registered kickoff against local originals without revising it."""
    from .football import team_key

    at = store.iso(at or store.now_utc())
    now = store.instant(at)
    connection = (
        nullcontext(_connection) if _connection is not None else closing(_db(root))
    )
    with ExitStack() as stack:
        db = stack.enter_context(connection)
        snapshot = dbscope.for_connection(db) or stack.enter_context(dbscope.bind(root, db))
        if not db.in_transaction:
            db.execute("BEGIN")
        frontier = _frontier(db) if _with_frontier else None
        registered = [
            dict(r)
            for r in db.execute(
                """WITH latest AS (
                SELECT f.*,s.precision,s.recorded_at,
                  first_value(f.source) OVER (PARTITION BY fixture_id ORDER BY known_at,f.version) first_source,
                  first_value(f.kickoff) OVER (PARTITION BY fixture_id ORDER BY known_at,f.version) first_kickoff,
                  lag(f.kickoff) OVER (PARTITION BY fixture_id ORDER BY known_at,f.version) previous_kickoff,
                  row_number() OVER (PARTITION BY fixture_id ORDER BY known_at DESC,f.version DESC) n
                FROM fixture_versions f JOIN schedule_receipts s USING(fixture_id,version)
                WHERE f.known_at<=? AND s.recorded_at<=? AND (? IS NULL OR f.season=?)
                ) SELECT * FROM latest WHERE n=1 ORDER BY kickoff,fixture_id""",
                (at, at, season, season),
            )
        ]
        future = [f for f in registered if f["kickoff"] > at]
        if fixture_ids is not None:
            targets = [f for f in future if f["fixture_id"] in fixture_ids]
        elif news_days is None:
            if round_id is None and future:
                round_id = future[0]["round_id"]
            targets = [f for f in future if f["round_id"] == round_id]
        else:
            end = store.iso(now + timedelta(days=news_days))
            requested = team_key(team) if team else None
            teams = set()
            targets = []
            for f in future:
                if f["kickoff"] > end:
                    break
                clubs = {(f["season"], team_key(f[k])) for k in ("home", "away")}
                if requested:
                    clubs = {c for c in clubs if c[1] == requested}
                if clubs - teams:
                    targets.append(f)
                teams.update(clubs)
        pairs = Counter(
            (
                f["season"],
                f["round_id"],
                opportunity._club(f["home"]),
                opportunity._club(f["away"]),
            )
            for f in registered
        )
        first_times = Counter(
            (f["season"], f["round_id"], f["first_kickoff"])
            for f in registered
            if f["first_source"] == "openfootball"
        )
        current_times = Counter(
            (f["season"], f["round_id"], f["kickoff"])
            for f in registered
            if f["source"] == "openfootball"
        )
        incidents = {}
        for r in db.execute(
            "SELECT payload FROM reports WHERE kind='incident' AND key LIKE 'epl-schedule-review-%'"
        ):
            for incident in json.loads(r[0]).get("items", []):
                incidents[incident["fixture_id"]] = incident
        events, errors = _events(root, db, now) if targets else ({}, [])
        rounds = {}
        items = []
        for f in targets:
            rnd = f["round_id"]
            key = (f["season"], rnd)
            if key not in rounds:
                try:
                    year = int(f["season"][:4])
                    rounds[key] = _round(root, db, year, rnd, at), None
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    rounds[key] = {}, str(exc)
            pair = tuple(opportunity._club(f[k]) for k in ("home", "away"))
            ol = rounds[key][0].get(pair)
            markets = events.get(pair, [])
            review = _review(db, f | {"schedule_version": f["version"]}, at)
            slot = (f["season"], rnd, f["kickoff"])
            bulk = (
                f["source"] == "openfootball"
                and max(first_times[slot], current_times[slot]) >= 10
            )
            native_times = {e["kickoff"] for e in markets}
            if ol and ol.get("kickoff"):
                native_times.add(ol["kickoff"])
            incident = incidents.get(f["fixture_id"]) or {}
            prior_dispute = (
                incident.get("previous") == f["kickoff"]
                and incident.get("observed_at", at) <= at
            )
            # Missing corroboration alone does not stop an ordinary registered B1 fixture.
            # A held revision, a witnessed change, or ten identical provisional file entries does.
            seal_reason = None
            item = {
                "fixture_id": f["fixture_id"],
                "season": f["season"],
                "round": rnd,
                "home": f["home"],
                "away": f["away"],
                "kickoff": f["kickoff"],
                "version": f["version"],
                "source": f["source"],
                "registered_at": f["recorded_at"],
                "registration_locator": f["locator"],
                "previous_kickoff": f["previous_kickoff"],
                "bulk_placeholder": bulk,
                "openligadb": ol,
                "gamma": markets,
                "review": {k: review[k] for k in ("action", "reviewer", "at", "reason")}
                if review
                else None,
            }
            try:
                if f["precision"] == "date":
                    seal_reason = (
                        "date-only registration cannot anchor a news or decision cutoff"
                    )
                    raise ValueError(seal_reason)
                if review and review["action"] == "withdrawn":
                    seal_reason = "schedule withdrawn by operator: " + review["reason"]
                    raise ValueError(seal_reason)
                if f["source"] == SOURCE:
                    try:
                        attest(root, db, f, at)
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        seal_reason = str(exc)
                        raise
                if rounds[key][1]:
                    raise ValueError(rounds[key][1])
                _choose(
                    f | {"schedule_source": f["source"], "result_version": None},
                    ol,
                    markets,
                    pairs[f["season"], rnd, *pair] != 1,
                    now,
                )
                if ol["kickoff"] != f["kickoff"]:
                    raise ValueError(
                        "agreed native kickoff has not been applied to the registered schedule"
                    )
                item.update(status="confirmed", news_eligible=True)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                if seal_reason is None:
                    if native_times - {f["kickoff"]} or prior_dispute:
                        seal_reason = (
                            "native kickoff conflicts with the registered schedule: "
                            + str(exc)
                        )
                    elif bulk:
                        seal_reason = (
                            "unconfirmed bulk placeholder kickoff for this EPL round"
                        )
                # A separately registered local fixture retains its existing collection path.
                # It is explicitly labelled, and never counts as two-source confirmation.
                local = (
                    f["source"] not in ("openfootball", SOURCE)
                    and f["precision"] != "date"
                    and not review
                    and not seal_reason
                    and len(markets) <= 1
                    and not any(e["kickoff"] != f["kickoff"] for e in markets)
                    and (ol is None or ol.get("kickoff") == f["kickoff"])
                    and pairs[f["season"], rnd, *pair] == 1
                )
                item.update(
                    status="local_registration" if local else "deferred",
                    news_eligible=local,
                    reason=str(exc),
                )
            item["fixed_b1_block_reason"] = seal_reason
            items.append(item)
    confirmed = sum(i["status"] == "confirmed" for i in items)
    blocked = [i["fixture_id"] for i in items if not i["news_eligible"]]
    round_registered = (
        sum(f["round_id"] == round_id for f in registered)
        if round_id is not None
        else None
    )
    complete = round_registered == 10 if round_id is not None else True
    report = {
        "as_of": at,
        "season": season,
        "round": round_id,
        "registered": len(items),
        "registered_in_round": round_registered,
        "expected_in_round": 10 if round_id is not None else None,
        "confirmed": confirmed,
        "blocked_fixtures": blocked,
        "fixed_b1_blocked_fixtures": [
            i["fixture_id"] for i in items if i["fixed_b1_block_reason"]
        ],
        "fixtures": items,
        "state": "unavailable"
        if not items
        else "incomplete_round"
        if not complete
        else "ready"
        if confirmed == len(items)
        else "unconfirmed",
        "source_errors": errors,
        "action": None
        if items and complete and confirmed == len(items)
        else "Register the season with data refresh, then run season run for the event catalogue "
        "and result-feed witnesses. Inspect season readiness before the first full news sync.",
    }
    if _with_frontier:
        report["frontier"] = frontier
        report["originals"] = snapshot.originals
    return report


def eligible_news(report):
    return {
        (f["fixture_id"], f["kickoff"], f["version"])
        for f in report["fixtures"]
        if f["news_eligible"]
    }


class NewsScheduleChanged(ValueError):
    """The claimed fixture lacks an admissible schedule for an event publication."""


def require_news(root, db, fixture, at, claimed_status):
    """Reopen the latest native originals in the event writer's locked snapshot."""
    fid = fixture["fixture_id"]
    report = readiness(root, fixture_ids={fid}, at=at, _connection=db)
    row = next(iter(report["fixtures"]), None)
    if not row or (row["version"], row["kickoff"]) != (fixture["version"], fixture["kickoff"]):
        raise NewsScheduleChanged("news schedule registration changed or the fixture started")
    if not row["news_eligible"] or (claimed_status == "confirmed" and row["status"] != "confirmed"):
        raise NewsScheduleChanged("news schedule is no longer eligible: " + row["reason"])
    if row["status"] == "confirmed":
        oldest = min(row["openligadb"]["received_at"], row["gamma"][0]["received_at"])
        return store.iso(store.instant(oldest) + MAX_AGE)
    return None


def _frontier(db):
    """Cheap invalidation for the immutable schedule and provider receipt streams."""
    values = list(
        db.execute(
            """SELECT (SELECT max(rowid) FROM fixture_versions), (SELECT max(rowid) FROM rf_native_requests),
        (SELECT max(rowid) FROM epl_schedule_reviews), (SELECT max(rowid) FROM epl_schedule_holds)"""
        ).fetchone()
    )
    gamma = db.execute("SELECT 1 FROM sqlite_master WHERE name='ma_events'").fetchone()
    values.append(
        db.execute("SELECT max(rowid) FROM ma_events").fetchone()[0] if gamma else None
    )
    return tuple(values)


@dataclass(frozen=True)
class FixedSchedules:
    root: Path
    frontier: tuple
    checked: str
    fixtures: dict
    originals: dict


def fixed_schedules(root, fixture_ids, at, *, db=None):
    report = readiness(
        root, fixture_ids=fixture_ids, at=at, _with_frontier=True, _connection=db
    )
    return FixedSchedules(
        Path(root).resolve(),
        report["frontier"],
        report["as_of"],
        {f["fixture_id"]: f for f in report["fixtures"]},
        report["originals"],
    )


@contextmanager
def dispatch_checks(checks):
    token = _dispatch_checks.set(checks)
    try:
        yield
    finally:
        _dispatch_checks.reset(token)


def require_fixed(root, fixture, at, checks=None, *, db=None):
    """Reuse one verified dispatch batch until a schedule, original, or hold changes."""
    at = store.iso(at)
    fid = fixture["fixture_id"]
    checks = checks or _dispatch_checks.get()
    row = (
        checks.fixtures.get(fid)
        if checks and checks.root == Path(root).resolve()
        else None
    )
    if row and checks.checked <= at <= store.iso(
        store.instant(checks.checked) + timedelta(minutes=5)
    ):
        receipts = [r["received_at"] for r in row["gamma"]]
        if row["openligadb"] and row["openligadb"].get("received_at"):
            receipts.append(row["openligadb"]["received_at"])
        expired = row["bulk_placeholder"] and any(
            store.instant(stamp) + MAX_AGE < store.instant(at) for stamp in receipts
        )
        if db is None:
            with closing(_db(root)) as connection:
                current = _frontier(connection)
        else:
            current = _frontier(db)
        if current != checks.frontier or expired:
            row = None
        else:
            try:
                dbscope.recheck_originals(checks)
            except dbscope.EvidenceChanged:
                row = None
    else:
        row = None
    if row is None:
        checks = fixed_schedules(root, {fid}, at, db=db)
        row = checks.fixtures.get(fid)
    if not row or (row["kickoff"], row["version"]) != (
        fixture["kickoff"],
        fixture["version"],
    ):
        raise ValueError("registered schedule changed before the fixed B1 decision")
    if row["fixed_b1_block_reason"]:
        raise ValueError(
            "fixed B1 seal awaiting schedule verification: "
            + row["fixed_b1_block_reason"]
        )
    return checks


def _evaluate(db, f, ol, events, ambiguous, at, approval=None):
    fid, old, version = f["fixture_id"], f["kickoff"], f["schedule_version"]
    source = db.execute(
        "SELECT source FROM fixture_versions WHERE fixture_id=? AND version=?",
        (fid, version),
    ).fetchone()[0]
    row = {
        "fixture_id": fid,
        "round": f["round_id"],
        "home": f["home"],
        "away": f["away"],
        "previous": old,
        "openligadb_kickoff": ol["kickoff"] if ol else None,
        "gamma_kickoffs": [e["kickoff"] for e in events],
    }
    try:
        if approval and (not ol or approval["kickoff"] != ol["kickoff"]):
            raise ValueError("requested kickoff differs from current native evidence")
        reviewed = _review(db, f, at)
        if reviewed and reviewed["action"] == "withdrawn" and not approval:
            raise ValueError("schedule withdrawn by operator: " + reviewed["reason"])
        event = _choose(
            f | {"schedule_source": source},
            ol,
            events,
            ambiguous,
            store.instant(at),
            reviewed=approval is not None,
        )
        proposed = ol["kickoff"]
        if source == SOURCE and old != proposed and not approval:
            anchor = db.execute(
                "SELECT kickoff FROM fixture_versions WHERE fixture_id=? AND source!=? ORDER BY version DESC LIMIT 1",
                (fid, SOURCE),
            ).fetchone()
            if (
                not anchor
                or abs(store.instant(anchor[0]) - store.instant(proposed)) > MAX_SHIFT
            ):
                raise ValueError(
                    "cumulative change exceeds the independently registered 36-hour window"
                )
        proof = {
            "fixture_id": fid,
            "season": f["season"],
            "round": f["round_id"],
            "previous": old,
            "kickoff": proposed,
            "gamma": event,
            "openligadb": ol,
        }
        if approval:
            proof["approval"] = approval | {"at": at}
        ident = store.digest(proof)
        row.update(kickoff=proposed, gamma_slug=event["slug"])
        if old == proposed:
            if source == SOURCE:
                _hold(db, fid, version, at)
            row["status"] = "unchanged"
        else:
            new = store.register_fixture_db(
                db,
                {
                    "fixture_id": fid,
                    "season": f["season"],
                    "round_id": f["round_id"],
                    "home": f["home"],
                    "away": f["away"],
                    "kickoff": proposed,
                    "known_at": at,
                    "source": SOURCE,
                    "precision": "minute",
                    "locator": "schedule-proof:" + ident,
                },
                recorded_at=at,
            )
            version = new["version"]
            db.execute(
                "INSERT INTO epl_schedule_proofs VALUES(?,?,?,?)",
                (fid, version, ident, store.canonical(proof)),
            )
            row.update(status="corrected", version=version, known_at=at, proof=ident)
        if approval and (
            old != proposed or reviewed and reviewed["action"] == "withdrawn"
        ):
            db.execute(
                "INSERT INTO epl_schedule_reviews VALUES(?,?,?,?,?,?,?)",
                (
                    fid,
                    version,
                    at,
                    "approved",
                    approval["reviewer"],
                    approval["reason"],
                    store.canonical(proof),
                ),
            )
        return row
    except (ValueError, OSError, TypeError, KeyError) as exc:
        if approval:
            raise
        row.update(status="deferred", reason=str(exc))
        if source == SOURCE and at < old:
            _hold(db, fid, version, at, str(exc))
        return row


def review_schedule(root, year, fid, kickoff, reviewer, reason, *, withdraw=False):
    if not all(isinstance(v, str) and v.strip() for v in (reviewer, reason)):
        raise ValueError("reviewer and reason are required")
    proposed = store.iso(kickoff)
    with closing(_db(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        at = store.now_utc()
        fixtures = store.eligible_fixture_rows(db, season=result_admission.scope(year))
        f = next((x for x in fixtures if x["fixture_id"] == fid), None)
        if not f:
            raise ValueError("unregistered EPL fixture")
        if withdraw:
            if (
                f["kickoff"] != proposed
                or not db.execute(
                    "SELECT 1 FROM epl_schedule_proofs WHERE fixture=? AND version=?",
                    (fid, f["schedule_version"]),
                ).fetchone()
            ):
                raise ValueError(
                    "current schedule differs from the identified revision"
                )
            reviewed = _review(db, f, at)
            if reviewed and reviewed["action"] == "withdrawn":
                return reviewed
            _hold(
                db,
                fid,
                f["schedule_version"],
                at,
                "schedule withdrawn by " + reviewer + ": " + reason,
            )
            db.execute(
                "INSERT INTO epl_schedule_reviews VALUES(?,?,?,?,?,?,?)",
                (fid, f["schedule_version"], at, "withdrawn", reviewer, reason, None),
            )
            return _review(db, f, at)
        from .result_pairings import unique_fixtures

        pair = tuple(opportunity._club(f[k]) for k in ("home", "away"))
        ol = _round(root, db, year, f["round_id"], at)
        events, _ = _events(root, db, store.instant(at))
        return _evaluate(
            db,
            f,
            ol.get(pair),
            events.get(pair, []),
            fid not in unique_fixtures(fixtures),
            at,
            {
                "kickoff": proposed,
                "reviewer": reviewer.strip(),
                "reason": reason.strip(),
            },
        )


def sync(root, year, *, fetch=False):
    """Verify two native authorities and append safe future revisions atomically."""
    now = store.instant(store.now_utc())
    fetches = []
    with closing(_db(root)) as db:
        market_rows, _ = _events(root, db, now) if fetch else ({}, [])
        future = _upcoming(db, year, now, market_rows)
        rounds = sorted({f["round_id"] for f in future if f["result_version"] is None})
        last = {
            r[0]: store.instant(r[1])
            for r in db.execute(
                "SELECT scope,max(received_at) FROM rf_native_requests WHERE season=? GROUP BY scope",
                (year,),
            )
        }
    if fetch and rounds:
        try:
            _register_league(root, year)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            fetches.append({"kind": "competition", "error": str(exc)})
        for rnd in rounds[:3]:
            if str(rnd) in last and now - last[str(rnd)] < REFRESH:
                continue
            try:
                capture = result_feed.sync(
                    root, year, round_id=rnd, confirm=True, require_native=True
                )
                fetches.append({"round": rnd, "status": capture["fetch_status"]})
            except (ValueError, OSError, TypeError, KeyError) as exc:
                fetches.append({"round": rnd, "error": str(exc)})
    with closing(_db(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        now = store.instant(store.now_utc())
        markets, errors = _events(root, db, now)
        forms = _upcoming(db, year, now, markets)
        all_local = store.eligible_fixture_rows(db, season=result_admission.scope(year))

        def clubs(f):
            return tuple(opportunity._club(f[k]) for k in ("home", "away"))

        groups = defaultdict(list)
        for f in all_local:
            groups[(f["round_id"], clubs(f))].append(f["fixture_id"])
        rounds = {}
        items = []
        for rnd in sorted({f["round_id"] for f in forms}):
            try:
                rounds[rnd] = _round(root, db, year, rnd, store.iso(now))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                errors.append({"round": rnd, "reason": str(exc)})
        for f in forms:
            rnd = f["round_id"]
            pair = clubs(f)
            row = _evaluate(
                db,
                f,
                rounds.get(rnd, {}).get(pair),
                markets.get(pair, []),
                len(groups[(rnd, pair)]) != 1,
                store.now_utc(),
            )
            items.append(row)
        _recheck_early_results(db, store.now_utc())
        past = [r for r in items if store.instant(r["previous"]) <= now]
        items = [r for r in items if store.instant(r["previous"]) > now]
        report = {
            "season": result_admission.scope(year),
            "at": store.now_utc(),
            "registered": len(items),
            "late_skipped": past,
            "previously_matched": sum(i["status"] == "unchanged" for i in items),
            "corrected": sum(i["status"] == "corrected" for i in items),
            "matched": sum(i["status"] != "deferred" for i in items),
            "items": items,
            "source_errors": errors,
            "fetches": fetches,
            "attribution": result_feed.ATTRIBUTION,
        }
        store.put_report(db, "source", "epl-upcoming-schedules-" + str(year), report)
        previous = db.execute(
            "SELECT payload FROM reports WHERE kind='incident' AND key=?",
            (f"epl-schedule-review-{year}",),
        ).fetchone()
        unresolved = (
            {r["fixture_id"]: r for r in json.loads(previous[0])["items"]}
            if previous
            else {}
        )
        current = {f["fixture_id"]: f for f in all_local}
        unresolved = {
            fid: r
            for fid, r in unresolved.items()
            if fid in current
            and current[fid]["result_version"] is None
            and current[fid]["kickoff"] == r["previous"]
            and any(t > report["at"] for t in r["gamma_kickoffs"])
        }
        for item in items + past:
            if item["status"] != "deferred":
                unresolved.pop(item["fixture_id"], None)
        delayed = [
            r
            for r in items + past
            if r["status"] == "deferred"
            and r["openligadb_kickoff"]
            and r["gamma_kickoffs"]
            and (
                r["previous"] != r["openligadb_kickoff"]
                or r["gamma_kickoffs"] != [r["previous"]]
            )
        ]
        unresolved.update(
            {r["fixture_id"]: r | {"observed_at": report["at"]} for r in delayed}
        )
        store.put_report(
            db,
            "incident",
            f"epl-schedule-review-{year}",
            {
                "status": "open" if unresolved else "clear",
                "at": report["at"],
                "items": list(unresolved.values()),
            },
        )
    return report
