"""Prospective schedule revisions from independent OpenLigaDB and Gamma HTTP originals."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from contextlib import closing
from datetime import timedelta

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
# The canonical official competition directory confirmed this ID on 2026-11-25.
KNOWN_EPL = {2026: 5996}
SCHEMA = """
CREATE TABLE IF NOT EXISTS epl_schedule_proofs(
 fixture TEXT,version INTEGER,proof_digest TEXT NOT NULL,proof TEXT NOT NULL,PRIMARY KEY(fixture,version));
CREATE TABLE IF NOT EXISTS epl_schedule_holds(
 fixture TEXT,version INTEGER,at TEXT,reason TEXT,PRIMARY KEY(fixture,version,at));
CREATE TABLE IF NOT EXISTS epl_schedule_result_receipts(
 fixture TEXT,version INTEGER,at TEXT NOT NULL,PRIMARY KEY(fixture,version));
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
    reviewed = list(result_feed._full_time(data, year, only_round=rnd))
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
        if not all(pair) or pair[0] == pair[1] or pair in result:
            raise ValueError("ambiguous OpenLigaDB home/away pair")
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
        "AND julianday(kickoff) BETWEEN julianday(?) AND julianday(?) ORDER BY slug,sha",
        (store.iso(now), store.iso(now - MAX_SHIFT), store.iso(now + WEEK + MAX_SHIFT)),
    )
    for r in rows:
        try:
            pair, event = _gamma(root, db, r, store.iso(now))
            by_pair[pair].append(event)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            errors.append({"slug": r["slug"], "reason": str(exc)})
    return by_pair, errors


def _upcoming(db, year, now):
    # Retain just-started fixtures in diagnostics to reject late corrections to a future replacement.
    return [
        f
        for f in store.eligible_fixture_rows(db, season=result_admission.scope(year))
        if now - MAX_SHIFT <= store.instant(f["kickoff"]) <= now + WEEK
    ]


def _choose(f, ol, markets, ambiguous, now):
    if ambiguous:
        raise ValueError("non-unique local fixture identity")
    if not ol or ol["fixture"] != f["fixture_id"]:
        raise ValueError("missing uniquely matched OpenLigaDB regulation fixture")
    if len(markets) != 1:
        raise ValueError("missing or ambiguous native Gamma main event")
    proposed = store.instant(ol["kickoff"])
    prior = store.instant(f["kickoff"])
    if ol["kickoff"] != markets[0]["kickoff"]:
        raise ValueError("independent sources disagree on precise UTC kickoff")
    if min(prior, proposed) <= now:
        raise ValueError("original or proposed kickoff has already passed")
    if f["precision"] not in {"minute", "second"} or f["result_version"] is not None:
        raise ValueError("ineligible registered schedule or result already present")
    if prior != proposed:
        if min(prior, proposed) - now <= DECISION_LEAD:
            raise ValueError(
                "automatic revision arrived after the earlier T-60 decision"
            )
        if abs(prior - proposed) > MAX_SHIFT:
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


def sync(root, year, *, fetch=False):
    """Verify two native authorities and append safe future revisions atomically."""
    now = store.instant(store.now_utc())
    fetches = []
    with closing(_db(root)) as db:
        future = _upcoming(db, year, now)
        rounds = sorted(
            {f["round_id"] for f in future if store.instant(f["kickoff"]) > now}
        )
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
        forms = _upcoming(db, year, now)
        all_local = store.eligible_fixture_rows(db, season=result_admission.scope(year))

        def clubs(f):
            return tuple(opportunity._club(f[k]) for k in ("home", "away"))

        groups = defaultdict(list)
        for f in all_local:
            groups[(f["round_id"], clubs(f))].append(f["fixture_id"])
        markets, errors = _events(root, db, now)
        rounds = {}
        items = []
        for rnd in sorted({f["round_id"] for f in forms}):
            try:
                rounds[rnd] = _round(root, db, year, rnd, store.iso(now))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                errors.append({"round": rnd, "reason": str(exc)})
        for f in forms:
            fid = f["fixture_id"]
            old = f["kickoff"]
            rnd = f["round_id"]
            pair = clubs(f)
            latest = db.execute(
                "SELECT source FROM fixture_versions WHERE fixture_id=? AND version=?",
                (fid, f["schedule_version"]),
            ).fetchone()
            row = {
                "fixture_id": fid,
                "round": rnd,
                "home": f["home"],
                "away": f["away"],
                "previous": old,
            }
            try:
                stamped = store.now_utc()
                ol = rounds.get(rnd, {}).get(pair)
                event = _choose(
                    f | {"schedule_source": latest[0]},
                    ol,
                    markets.get(pair, []),
                    len(groups[(rnd, pair)]) != 1,
                    store.instant(stamped),
                )
                if latest[0] == SOURCE and old != ol["kickoff"]:
                    anchor = db.execute(
                        "SELECT kickoff FROM fixture_versions WHERE fixture_id=? AND source!=? ORDER BY version DESC LIMIT 1",
                        (fid, SOURCE),
                    ).fetchone()
                    if (
                        not anchor
                        or abs(store.instant(anchor[0]) - store.instant(ol["kickoff"]))
                        > MAX_SHIFT
                    ):
                        raise ValueError(
                            "cumulative change exceeds the independently registered 36-hour window"
                        )
                proof = {
                    "fixture_id": fid,
                    "season": f["season"],
                    "round": rnd,
                    "previous": old,
                    "kickoff": ol["kickoff"],
                    "gamma": event,
                    "openligadb": ol,
                }
                ident = store.digest(proof)
                row.update(kickoff=ol["kickoff"], gamma_slug=event["slug"])
                if old == ol["kickoff"]:
                    if latest[0] == SOURCE:
                        _hold(db, fid, f["schedule_version"], stamped)
                    row["status"] = "unchanged"
                else:
                    new = store.register_fixture_db(
                        db,
                        {
                            "fixture_id": fid,
                            "season": f["season"],
                            "round_id": rnd,
                            "home": f["home"],
                            "away": f["away"],
                            "kickoff": ol["kickoff"],
                            "known_at": stamped,
                            "source": SOURCE,
                            "precision": "minute",
                            "locator": "schedule-proof:" + ident,
                        },
                        recorded_at=stamped,
                    )
                    db.execute(
                        "INSERT INTO epl_schedule_proofs VALUES(?,?,?,?)",
                        (fid, new["version"], ident, store.canonical(proof)),
                    )
                    row.update(
                        status="corrected",
                        version=new["version"],
                        known_at=stamped,
                        proof=ident,
                    )
            except (ValueError, OSError, TypeError, KeyError) as exc:
                row.update(status="deferred", reason=str(exc))
                if latest[0] == SOURCE and store.instant(stamped) < store.instant(old):
                    _hold(db, fid, f["schedule_version"], stamped, str(exc))
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
    return report
