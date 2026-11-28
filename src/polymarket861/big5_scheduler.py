"""Prospective, research-only forecasting for the four additional domestic leagues."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack, closing
from datetime import timedelta
from pathlib import Path

from . import dbscope, domestic_editions, football, football_model, league_player_status, market_watch_health, news_model, pipeline, store
from .big5_market import NEW_LEAGUES


WINDOW = timedelta(minutes=5)
LOOKAHEAD = timedelta(days=7)
SCOPE = "prospective_technical_research"
HEALTH_MAX_AGE = 120
_LOG = logging.getLogger(__name__)
_HEALTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS domestic_scan (id INTEGER PRIMARY KEY CHECK(id=1), started TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS domestic_cycle_checkpoint (
 id INTEGER PRIMARY KEY CHECK(id=1), last_ok TEXT, ok_detail TEXT, failures INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS domestic_poll_batch (
 id INTEGER PRIMARY KEY CHECK(id=1), started TEXT NOT NULL, finished TEXT NOT NULL, error TEXT);
"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS domestic_matches (
 fixture TEXT NOT NULL, kickoff TEXT NOT NULL, metadata TEXT NOT NULL, active INTEGER NOT NULL,
 binding TEXT NOT NULL, checked TEXT NOT NULL, PRIMARY KEY(fixture,kickoff));
CREATE TABLE IF NOT EXISTS domestic_jobs (
 fixture TEXT NOT NULL, kickoff TEXT NOT NULL, horizon TEXT NOT NULL, revision TEXT NOT NULL,
 opens TEXT NOT NULL, deadline TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 attempted TEXT, error TEXT, sealed TEXT, digest TEXT, payload TEXT,
 PRIMARY KEY(fixture,kickoff,horizon,revision));
CREATE TABLE IF NOT EXISTS domestic_scores (
 fixture TEXT NOT NULL, kickoff TEXT NOT NULL, horizon TEXT NOT NULL, revision TEXT NOT NULL,
 result_at TEXT NOT NULL, result_sha TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(fixture,kickoff,horizon,revision,result_at,result_sha));
CREATE TABLE IF NOT EXISTS domestic_news_polls (
 fixture TEXT NOT NULL, kickoff TEXT NOT NULL, due TEXT NOT NULL, state TEXT NOT NULL,
 attempted TEXT, error TEXT, PRIMARY KEY(fixture,kickoff));
CREATE TABLE IF NOT EXISTS domestic_run (
 id INTEGER PRIMARY KEY CHECK(id=1), started TEXT NOT NULL, finished TEXT, state TEXT,
 detail TEXT NOT NULL);
"""


def _clock():
    return store.instant(store.now_utc())


def _error(exc):
    return f"{type(exc).__name__}: {exc}"[:600]


def _record_cycle(db, started, state, detail):
    finished = store.iso(_clock())
    raw = store.canonical(detail)
    with db:
        db.execute("BEGIN IMMEDIATE")
        # Preserve the last completed cycle even on the first failing scan after an upgrade.
        db.execute("INSERT OR IGNORE INTO domestic_cycle_checkpoint "
                   "SELECT id,finished,detail,0 FROM domestic_run WHERE finished IS NOT NULL AND state='ok'")
        updated = db.execute("INSERT INTO domestic_run VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                             "started=excluded.started,finished=excluded.finished,state=excluded.state,"
                             "detail=excluded.detail WHERE excluded.started>=domestic_run.started",
                             (started, finished, state, raw)).rowcount
        if updated and state == "ok":
            summary = store.canonical({k: detail[k] for k in ("eligible_upcoming", "score_revisions_written")
                                       if k in detail})
            db.execute("INSERT OR REPLACE INTO domestic_cycle_checkpoint VALUES(1,?,?,0)", (finished, summary))
        elif updated:
            db.execute("INSERT INTO domestic_cycle_checkpoint VALUES(1,NULL,NULL,1) "
                       "ON CONFLICT(id) DO UPDATE SET failures=failures+1")
            # A league-wide input outage must not leave closed windows pending until the source recovers.
            db.execute("UPDATE domestic_jobs SET state='missed' WHERE sealed IS NULL AND deadline<=? "
                       "AND state IN ('pending','retry','withdrawn')", (finished,))


def _directory(root, league):
    return Path(root) / "forecast-scheduler" / league


def _database(root, league):
    db = store.connect(_directory(root, league))
    try:
        dbscope.prepare(db, "domestic-forecast-scheduler-v1", SCHEMA)
        dbscope.prepare(db, "domestic-forecast-editions-v1", domestic_editions.SCHEMA)
        dbscope.prepare(db, "domestic-forecast-health-v1", _HEALTH_SCHEMA)
    except BaseException:
        db.close()
        raise
    return db


def _codes(leagues):
    selected = tuple(dict.fromkeys(leagues))
    if not selected or any(code not in NEW_LEAGUES for code in selected):
        raise ValueError("select at least one of " + ", ".join(NEW_LEAGUES))
    return selected


def _load(root, code, football_root, market_root):
    from .league_live_inputs import load_inputs

    return load_inputs(root, league=code, football_root=football_root, market_state_dir=market_root)


def _pregame(market_root, binding, at):
    from .league_live_inputs import still_pregame

    return still_pregame(market_root, binding, at)


def _metadata(fixture, league):
    return {"fixture_id": fixture.fixture_id, "league": league, "season": fixture.season,
            "round": fixture.round, "home": fixture.home, "away": fixture.away,
            "kickoff": store.iso(fixture.kickoff)}


def _register(db, inputs, league, started, observed, errors):
    """A later, complete scan owns the active schedule even if an older scan finishes last."""
    fixtures = {}
    bindings = inputs.provenance["market_fixtures"]
    seen = store.iso(observed)
    with db:
        db.execute("BEGIN IMMEDIATE")
        newest = db.execute("SELECT MAX(started) FROM (SELECT started FROM domestic_scan UNION ALL "
                            "SELECT started FROM domestic_run)").fetchone()[0]
        if newest is not None and newest > started:
            return None
        # A scan in progress must not erase the last completed round used by the health check.
        db.execute("INSERT OR REPLACE INTO domestic_scan VALUES(1,?)", (started,))
        db.execute("UPDATE domestic_matches SET active=0")
        for fixture in inputs.upcoming:
            fid = fixture.fixture_id
            if not observed < fixture.kickoff <= observed + LOOKAHEAD or fixture.quarantine_reason:
                continue
            kickoff = store.iso(fixture.kickoff)
            try:
                binding = bindings[fid]
                if (store.instant(binding["kickoff"]) != fixture.kickoff or
                        store.instant(binding["receipt"]) > observed or
                        any(not binding.get(key) for key in ("slug", "event_id", "home_id", "away_id"))):
                    raise ValueError("market evidence does not establish this current fixture")
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                errors.append({"fixture": fid, "stage": "schedule", "error": _error(exc)})
                continue
            data = _metadata(fixture, league)
            db.execute("INSERT INTO domestic_matches VALUES(?,?,?,1,?,?) "
                       "ON CONFLICT(fixture,kickoff) DO UPDATE SET metadata=excluded.metadata,active=1,"
                       "binding=excluded.binding,checked=excluded.checked",
                       (fid, kickoff, store.canonical(data), store.canonical(binding), seen))
            db.execute("INSERT OR IGNORE INTO domestic_news_polls(fixture,kickoff,due,state) "
                       "VALUES(?,?,?,'pending')", (fid, kickoff, seen))
            fixtures[fid] = fixture
            for horizon, delta in (("preview", None), *pipeline.HORIZONS.items()):
                deadline = fixture.kickoff - delta if delta is not None else fixture.kickoff
                opens = deadline - WINDOW if delta is not None else observed
                db.execute("INSERT OR IGNORE INTO domestic_jobs"
                           "(fixture,kickoff,horizon,revision,opens,deadline,state) "
                           "VALUES(?,?,?,'',?,?,'pending')",
                           (fid, kickoff, horizon, store.iso(opens), store.iso(deadline)))
        # Preserve published forecasts when a game moves, or drops out of the verified inputs.
        # A withdrawn job can become eligible again only after a fresh verified schedule.
        db.execute("UPDATE domestic_jobs AS j SET state='withdrawn' WHERE sealed IS NULL AND "
                   "state IN ('pending','retry') AND NOT EXISTS(SELECT 1 FROM domestic_matches m "
                   "WHERE m.fixture=j.fixture AND m.kickoff=j.kickoff AND m.active=1)")
        db.execute("UPDATE domestic_jobs AS j SET state='pending' WHERE state='withdrawn' AND "
                   "deadline>? AND EXISTS(SELECT 1 FROM domestic_matches m WHERE m.fixture=j.fixture "
                   "AND m.kickoff=j.kickoff AND m.active=1)", (seen,))
        db.execute("UPDATE domestic_jobs SET state='missed' WHERE sealed IS NULL AND deadline<=? "
                   "AND state IN ('pending','retry','withdrawn')", (seen,))
    return fixtures


def _player_poll_errors(states):
    expected = {"observed", "no_published_players", "pending", "not_configured", "free_season_unavailable",
                "rights_unverified", "window_closed", "fixture_not_pregame"}
    return [f"{field}: {state}" for field, state in states.items() if state and state not in expected]


def _news(root, data, at):
    from . import league_press, league_rosters

    try:
        snapshot = league_rosters.fixture_rosters_as_of(root, data, store.iso(at))
    except (OSError, ValueError, sqlite3.Error) as exc:
        snapshot = {"state": "unavailable", "facts": [], "evidence_sha256": None,
                    "error": str(exc), "teams": {}}
    sources = {"rosters": snapshot.get("evidence_sha256")}
    for tag, read in (("press", league_press.fixture_news_as_of), ("player_status", league_player_status.as_of)):
        try:
            evidence = read(root, data, store.iso(at))
        except (OSError, ValueError, sqlite3.Error) as exc:
            evidence = {"state": "unavailable", "error": str(exc)}
        snapshot[tag] = evidence
        sources[tag] = evidence.get("evidence_sha256")
    errors = {k: row["error"] for k, row in (("rosters", snapshot), ("press", snapshot["press"]),
                                               ("player_status", snapshot["player_status"])) if row.get("error")}
    if any(side.get("roster", {}).get("reason") == "roster_original_unavailable"
           for side in snapshot.get("teams", {}).values()):
        errors["rosters"] = "roster_original_unavailable"
    player_faults = _player_poll_errors({field: snapshot["player_status"].get(field, {}).get("state")
                                         for field in ("injuries", "lineup")})
    if player_faults:
        errors["player_status"] = ", ".join(player_faults)
    if errors:
        snapshot["source_errors"] = errors
    snapshot["source_digests"] = sources
    snapshot["evidence_sha256"] = store.digest(sources) if any(sources.values()) else None
    return snapshot


def _forecast_outputs(engine, fixture, snapshot, at, cached_base=None):
    if cached_base is not None:
        base = dict(cached_base, cutoff=store.iso(at))
    else:
        base = engine.forecast(fixture.home, fixture.away, fixture.kickoff, at, season=fixture.season)
    _, pair = news_model.paired_prediction(base, snapshot.get("facts", ()), home=fixture.home,
                                           away=fixture.away, cutoff=at,
                                           coverage={"evidence_sha256": snapshot.get("evidence_sha256"),
                                                     "both_teams_observed": False})
    scenario = league_player_status.absence_scenario(base, snapshot, cutoff=at, engine=engine)
    return base, pair, scenario


def _changes(db, engine, fixture, snapshot, at):
    domestic_editions.changes(db, engine, fixture, snapshot, at, _forecast_outputs)


def _dispatch(db, engine, fixture, binding, data, job, snapshot, market_root, at):
    key = (job["fixture"], job["kickoff"], job["horizon"], job["revision"])
    deadline = store.instant(job["deadline"])
    columns = "fixture=? AND kickoff=? AND horizon=? AND revision=?"
    if not store.instant(job["opens"]) <= at < deadline or at >= fixture.kickoff:
        return
    try:
        if job["horizon"] == "news_update":
            # Pre-upgrade pending notifications cannot become new full forecasts.
            with db:
                db.execute("UPDATE domestic_jobs SET state='superseded' WHERE " + columns +
                           " AND state IN ('pending','retry') AND sealed IS NULL", key)
            return
        if job["horizon"] == "forecast_update" and domestic_editions.model_source_failed(snapshot):
            raise ValueError("incomplete player evidence; pending forecast update will be reassessed")
        base, pair, scenario = _forecast_outputs(engine, fixture, snapshot, at)
        payload = {"scope": SCOPE, "mode": "technical", "formal_eligible": False,
                   "fixture": data, "horizon": job["horizon"], "input_cutoff": store.iso(at),
                   "deadline": job["deadline"], "schedule_evidence": binding,
                   "baseline": base, "news": snapshot, "pair": pair, "player_scenario": scenario}
        prior = None
        if job["horizon"] == "forecast_update":
            prior = domestic_editions.latest(db, key[:2])
            outputs = domestic_editions.signatures(pair, scenario)
            if not prior or store.digest([prior["digest"], outputs]) != job["revision"]:
                # Another fixed seal may already carry these outputs, or the input
                # moved on while an old revision was pending.
                with db:
                    db.execute("UPDATE domestic_jobs SET state='superseded' WHERE " + columns +
                               " AND state IN ('pending','retry') AND sealed IS NULL", key)
                return
            previous = domestic_editions.read_forecast(db, key[:2], prior["digest"])
            old_outputs = domestic_editions.signatures(previous["pair"], previous.get("player_scenario", {}))
            payload["compared_to_seal"] = prior["digest"]
            payload["changed_outputs"] = [k for k in outputs if outputs[k] != old_outputs[k]]
            if not payload["changed_outputs"]:
                raise ValueError("forecast update has no changed output")
        raw = store.canonical(payload)
        digest = store.digest(payload)
        with db:
            db.execute("BEGIN IMMEDIATE")
            now = _clock()
            if prior and domestic_editions.latest(db, key[:2]) != prior:
                return
            if prior:
                cursor = db.execute("SELECT observed,evidence_sha FROM domestic_change_cursor "
                                    "WHERE fixture=? AND kickoff=?", key[:2]).fetchone()
                if cursor and (cursor["observed"] > store.iso(at) or (not snapshot.get("source_errors")
                               and cursor["evidence_sha"] != snapshot.get("evidence_sha256"))):
                    return
            active = db.execute("SELECT active,binding FROM domestic_matches WHERE fixture=? AND kickoff=?",
                                key[:2]).fetchone()
            current = json.loads(active["binding"]) if active and active["active"] else {}
            same_event = current and all(current[k] == binding[k] for k in
                                         ("kickoff", "slug", "event_id", "home_id", "away_id"))
            if not same_event or not _pregame(market_root, binding, now):
                raise ValueError("schedule or pregame state changed before seal")
            end = _clock()
            if end < at or end >= deadline:
                raise ValueError("prospective window elapsed before seal")
            db.execute("UPDATE domestic_jobs SET state='sealed',attempts=attempts+1,attempted=?,"
                       "sealed=?,payload=?,digest=?,error=NULL WHERE " + columns +
                       " AND state IN ('pending','retry') AND sealed IS NULL",
                       (store.iso(at), store.iso(end), raw, digest, *key))
    except (ValueError, OSError, sqlite3.Error) as exc:
        state = "retry" if _clock() < deadline else "missed"
        with db:
            db.execute("UPDATE domestic_jobs SET state=?,attempts=attempts+1,attempted=?,error=? WHERE "
                       + columns + " AND state IN ('pending','retry') AND sealed IS NULL",
                       (state, store.iso(at), str(exc), *key))


def _score_fixture(db, fixture, observed):
    version = football.result_as_of(fixture, observed, grades=football.RECEIPT_GRADES)
    if version is None:
        return 0
    received = store.iso(version.available)
    kickoff = store.iso(version.actual_kickoff or fixture.kickoff)
    record = {"score": version.score, "source": version.provenance, "grade": version.grade,
              "received": received}
    excluded = bool(fixture.quarantine_reason and version.score is not None)
    # Stage a fixture's corrections together. A bad historical seal can fail this
    # fixture without hiding valid, independent results elsewhere in the league.
    scored = 0
    head = store.canonical(dict(record, score=None) if excluded else record)
    current = db.execute("SELECT kickoff,result_at,record,complete FROM domestic_result_heads WHERE fixture=?",
                         (fixture.fixture_id,)).fetchone()
    if current and (current["result_at"] > received or tuple(current) == (kickoff, received, head, 1)):
        return 0
    # Publish the current receipt even if a forecast fails verification. During
    # a retry the projection must not continue counting the superseded outcome.
    with db:
        db.execute("INSERT INTO domestic_result_heads(fixture,kickoff,result_at,record) VALUES(?,?,?,?) "
                   "ON CONFLICT(fixture) DO UPDATE SET kickoff=excluded.kickoff,result_at=excluded.result_at,"
                   "record=excluded.record,complete=0 WHERE excluded.result_at>=domestic_result_heads.result_at "
                   "AND (excluded.record<>domestic_result_heads.record OR excluded.kickoff<>domestic_result_heads.kickoff)",
                   (fixture.fixture_id, kickoff, received, head))
    with db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT kickoff,result_at,record,complete FROM domestic_result_heads WHERE fixture=?",
                             (fixture.fixture_id,)).fetchone()
        if tuple(current) != (kickoff, received, head, 0):
            return 0
        if excluded:
            db.execute("UPDATE domestic_result_heads SET complete=1 WHERE fixture=?", (fixture.fixture_id,))
            return 0
        for job in domestic_editions.edition_jobs(db, fixture.fixture_id):
            if job["kickoff"] != kickoff or store.instant(job["sealed"]) >= version.available:
                continue
            result = dict(record, forecast_sha256=job["digest"])
            key = (job["fixture"], job["kickoff"], job["horizon"], job["revision"],
                   received, store.digest(result))
            if db.execute("SELECT 1 FROM domestic_scores WHERE fixture=? AND kickoff=? AND horizon=? AND "
                          "revision=? AND result_at=? AND result_sha=?", key).fetchone():
                continue
            if version.score is not None:
                forecast = domestic_editions.read_forecast(db, key[:2], job["digest"])
                result["scores"] = {
                    arm: football_model.score_probabilities(forecast["pair"][arm]["probabilities"], *version.score)
                    for arm in ("no_news", "with_news")}
                scenario = forecast.get("player_scenario", {})
                if scenario.get("changed"):
                    result["scores"]["uncalibrated_player_scenario"] = football_model.score_probabilities(
                        scenario["probabilities"], *version.score)
            scored += db.execute("INSERT OR IGNORE INTO domestic_scores VALUES(?,?,?,?,?,?,?)",
                                 (*key, store.canonical(result))).rowcount
        db.execute("UPDATE domestic_result_heads SET complete=1 WHERE fixture=?", (fixture.fixture_id,))
    return scored


def _score(db, fixtures, observed, *, errors=None):
    matches = {row[0] for row in db.execute("SELECT DISTINCT fixture FROM domestic_jobs WHERE sealed IS NOT NULL")}
    scored = 0
    for fixture in fixtures:
        if fixture.fixture_id not in matches or not fixture.result_versions:
            continue
        try:
            scored += _score_fixture(db, fixture, observed)
        except (KeyError, ValueError, TypeError, AttributeError, OSError, sqlite3.Error) as exc:
            if errors is None:
                raise
            errors.append({"fixture": fixture.fixture_id, "stage": "score", "error": f"{type(exc).__name__}: {exc}"})
    return scored


def _league_tick(root, code, football_root, market_root):
    started = store.iso(_clock())
    detail = {}
    with closing(_database(root, code)) as db:
        try:
            inputs = _load(root, code, football_root, market_root)
            observed = _clock()
            fixture_errors = list(inputs.provenance.get("withheld_fixtures", ()))
            market_source = inputs.provenance.get("market_source", {})
            fixture_errors.extend(market_source.get("fixture_errors", ()))
            market_outage = market_source.get("state") in {"unavailable", "unobserved"}
            # Missing source coverage cannot establish that previously observed matches were withdrawn.
            candidates = {} if market_outage else _register(db, inputs, code, started, observed, fixture_errors)
            if candidates is None:
                return {"state": "newer_cycle_in_progress"}
            engine = football.FootballEngine(inputs.fixtures, league=code, allow_archive_schedule=False)
            detail = {k: v for k, v in inputs.provenance.items() if k != "market_fixtures"}
            snapshots = {}
            for fid, fixture in candidates.items():
                try:
                    cutoff = _clock()
                    data = _metadata(fixture, code)
                    snapshot = _news(root, data, cutoff)
                    for source, error in snapshot.get("source_errors", {}).items():
                        fixture_errors.append({"fixture": fid, "stage": source, "error": error})
                    if snapshot.get("press", {}).get("state") == "stale_index":
                        fixture_errors.append({"fixture": fid, "stage": "press", "error": "stale_index"})
                    _changes(db, engine, fixture, snapshot, cutoff)
                    snapshots[fid] = (cutoff, data, snapshot)
                except Exception as exc:
                    # Without this fixture's confirmed inputs its own seal must wait.
                    fixture_errors.append({"fixture": fid, "stage": "inputs", "error": _error(exc)})
            jobs = db.execute("SELECT j.* FROM domestic_jobs j JOIN domestic_matches m "
                              "ON m.fixture=j.fixture AND m.kickoff=j.kickoff WHERE m.active=1 AND "
                              "j.state IN ('pending','retry') AND j.opens<=? ORDER BY j.deadline,j.fixture",
                              (store.iso(_clock()),)).fetchall()
            for job in jobs:
                fid = job["fixture"]
                if fid not in snapshots or job["kickoff"] != store.iso(candidates[fid].kickoff):
                    continue
                try:
                    at, data, snapshot = snapshots[fid]
                    if store.instant(job["opens"]) > at:
                        at = _clock()
                        snapshot = _news(root, data, at)
                    _dispatch(db, engine, candidates[fid], inputs.provenance["market_fixtures"][fid],
                              data, job, snapshot, market_root, at)
                except Exception as exc:
                    fixture_errors.append({"fixture": fid, "stage": job["horizon"], "error": _error(exc)})
            detail["eligible_upcoming"] = len(candidates)
            detail["score_revisions_written"] = _score(db, inputs.fixtures, _clock(), errors=fixture_errors)
            pending = db.execute("SELECT COUNT(*) FROM domestic_jobs WHERE state='retry'").fetchone()[0]
            detail.update(dispatch_retries=pending, fixture_errors=fixture_errors)
            source_errors = {}
            if market_outage:
                source_errors["market"] = market_source
            refresh = detail.get("openfootball_refresh") or {}
            if refresh.get("state") == "error":
                source_errors["openfootball"] = refresh
            wiki = (detail.get("wikimedia") or {}).get("poll") or {}
            if wiki.get("status") in {"unavailable", "retry_after"}:
                source_errors["wikimedia"] = wiki
            detail["source_errors"] = source_errors
            state = "partial" if pending or fixture_errors or source_errors or detail.get("unregistered_fixtures") else "ok"
        except Exception as exc:
            state = "retry"
            detail["error"] = _error(exc)
        if state != "ok":
            _LOG.warning("Domestic forecast cycle %s %s: %s", code, state,
                         detail.get("error") or detail.get("fixture_errors") or detail.get("source_errors")
                         or detail.get("unregistered_fixtures")
                         or f"{detail.get('dispatch_retries', 0)} forecast retries")
        _record_cycle(db, started, state, detail)
        return {"state": state, **detail}


def _poll(root, code, limit=4):
    """Claim a small batch outside every forecast deadline transaction."""
    from . import league_rosters

    count = 0
    with closing(_database(root, code)) as db:
        for _ in range(limit):
            started = _clock()
            stamp = store.iso(started)
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT p.fixture,p.kickoff,m.metadata FROM domestic_news_polls p "
                                 "JOIN domestic_matches m USING(fixture,kickoff) "
                                 "WHERE m.active=1 AND p.kickoff>? AND p.due<=? "
                                 "ORDER BY p.kickoff,p.due,p.fixture LIMIT 1", (stamp, stamp)).fetchone()
                if not row:
                    break
                fid, kickoff, metadata = row
                # Reclaim work after a process restart even when it stopped mid-request.
                db.execute("UPDATE domestic_news_polls SET state='running',attempted=?,error=NULL,due=? "
                           "WHERE fixture=? AND kickoff=?",
                           (stamp, store.iso(started + timedelta(minutes=5)), fid, kickoff))
            try:
                fixture = json.loads(metadata)
                # Each collector owns its retries, identity, licensing and point-in-time journal.
                # A failed roster source must not silence an independent lineup publisher.
                errors = []
                collecting = False
                for source, collect in (("rosters", league_rosters.collect_fixture_rosters),
                                        ("player_status", league_player_status.collect)):
                    try:
                        if source == "rosters":
                            result = collect(root, fixture, include_poll_status=True)
                            for club, poll in result["collection_polls"].items():
                                if poll["state"] in {"failed", "unknown"}:
                                    errors.append(f"{source}/{club}: {poll['reason'] or poll['state']}")
                                collecting |= poll["state"] == "running"
                        else:
                            result = collect(root, fixture)
                            errors.extend(f"{source}/{issue}" for issue in _player_poll_errors(result))
                    except Exception as exc:
                        errors.append(f"{source}: {_error(exc)}")
                if errors:
                    raise ValueError(" / ".join(errors))
                state, error = "checked", None
                left = store.instant(kickoff) - started
                # Stay fresh through real lineup publication and the fixed T-60m decision.
                # Never schedule the long interval past the next higher-frequency period.
                if left <= timedelta(minutes=90):
                    interval = timedelta(minutes=3)
                elif left <= timedelta(hours=3):
                    interval = min(timedelta(minutes=30), left - timedelta(minutes=90))
                else:
                    interval = min(timedelta(hours=6), left - timedelta(hours=3))
                if collecting:
                    state, interval = "pending", min(interval, timedelta(minutes=5))
            except Exception as exc:
                state, error, interval = "retry", str(exc), timedelta(minutes=5)
                _LOG.warning("Domestic news poll %s %s: %s", code, fid, error)
            with db:
                db.execute("UPDATE domestic_news_polls SET state=?,error=?,due=? "
                           "WHERE fixture=? AND kickoff=? AND attempted=?",
                           (state, error, store.iso(_clock() + interval), fid, kickoff, stamp))
            count += 1
    return count


def _poll_batch(root, code):
    """Persist and log failures outside a particular claim, including an unexpected worker error."""
    started = store.iso(_clock())
    count, error = 0, None
    try:
        count = _poll(root, code)
    except Exception as exc:
        error = _error(exc)
        _LOG.exception("Domestic news batch failed for %s", code)
    with closing(_database(root, code)) as db, db:
        db.execute("INSERT INTO domestic_poll_batch VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                   "started=excluded.started,finished=excluded.finished,error=excluded.error "
                   "WHERE excluded.started>=domestic_poll_batch.started", (started, store.iso(_clock()), error))
        retry = db.execute("SELECT p.error FROM domestic_news_polls p JOIN domestic_matches m USING(fixture,kickoff) "
                           "WHERE m.active=1 AND p.kickoff>? AND p.state='retry' ORDER BY p.attempted DESC LIMIT 1",
                           (store.iso(_clock()),)).fetchone()
    return count, error or (retry[0] if retry else None)


def tick(root: Path, *, leagues, market_state_dir: Path, football_root: Path | None = None,
         collect_news=True):
    selected = _codes(leagues)
    history = Path(football_root) if football_root is not None else Path(root) / "openfootball"
    results = {}
    with ThreadPoolExecutor(max_workers=len(selected)) as workers:
        futures = {code: workers.submit(_league_tick, Path(root), code, history, market_state_dir)
                   for code in selected}
        for code, future in futures.items():
            try:
                results[code] = future.result()
            except Exception as exc:
                # A broken source, filesystem or parser in one league cannot suppress its neighbours.
                results[code] = {"state": "retry", "error": f"{type(exc).__name__}: {exc}"}
        if collect_news:
            polls = {code: workers.submit(_poll_batch, root, code) for code in selected
                     if results[code]["state"] in {"ok", "partial"}}
            for code, future in polls.items():
                try:
                    results[code]["news_polls"], error = future.result()
                except Exception as exc:
                    error = _error(exc)
                    _LOG.exception("Cannot record domestic news poll for %s", code)
                if error:
                    results[code].update(state="partial", news_poll_error=error)
    return {"scope": SCOPE, "leagues": results}


def _status_entry(root, code, fixture_id, health_only):
    directory = _directory(root, code)
    service = market_watch_health.status(directory, [code], None, None, discover=False, max_age=HEALTH_MAX_AGE)
    service = {k: service.get(k) for k in ("state", "scheduler_alive", "run_id", "started_at",
                                           "last_heartbeat", "age_seconds", "ticks")}
    entry = {"state": "not_started", "healthy": False, "last_cycle_age_seconds": None,
             "service": service, "last_cycle": None}
    path = directory / "runtime.sqlite3"
    if not path.is_file():
        return entry
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "domestic_run" not in tables:
            entry["state"] = "initializing"
            return entry
        cycle = db.execute("SELECT * FROM domestic_run WHERE id=1").fetchone()
        now = _clock()
        at = store.iso(now)
        age = None
        if cycle:
            detail = json.loads(cycle["detail"])
            if health_only:
                detail = {k: detail[k] for k in ("error", "eligible_upcoming", "dispatch_retries", "fixture_errors",
                                                 "unregistered_fixtures", "source_errors") if k in detail}
            entry["last_cycle"] = {**detail, "state": cycle["state"],
                                   "started": cycle["started"], "finished": cycle["finished"]}
            if cycle["finished"]:
                age = (now - store.instant(cycle["finished"])).total_seconds()
                entry["last_cycle_age_seconds"] = age
        checkpoint = (db.execute("SELECT * FROM domestic_cycle_checkpoint WHERE id=1").fetchone()
                      if "domestic_cycle_checkpoint" in tables else None)
        if checkpoint:
            entry["consecutive_incomplete_cycles"] = checkpoint["failures"]
            previous = json.loads(checkpoint["ok_detail"]) if checkpoint["ok_detail"] else {}
            entry["last_successful_cycle"] = {"finished": checkpoint["last_ok"],
                                              "eligible_upcoming": previous.get("eligible_upcoming")}
        batch = (db.execute("SELECT * FROM domestic_poll_batch WHERE id=1").fetchone()
                 if "domestic_poll_batch" in tables else None)
        entry["news_poll_batch"] = dict(batch) if batch else None
        entry["news_poll_failures"] = [dict(row) for row in db.execute(
            "SELECT p.fixture,p.kickoff,p.state,p.attempted,p.due,p.error FROM domestic_news_polls p "
            "JOIN domestic_matches m USING(fixture,kickoff) WHERE m.active=1 AND p.kickoff>? "
            "AND (p.state='retry' OR (p.state='running' AND p.due<=?)) ORDER BY p.kickoff,p.fixture LIMIT 25",
            (at, at))]
        # Only the advancing daemon establishes liveness. A recent successful one-shot is still inspectable.
        if age is not None and (age < 0 or age > HEALTH_MAX_AGE):
            entry["state"] = "clock_conflict" if age < 0 else "stale"
        elif not service["scheduler_alive"]:
            entry["state"] = "unmonitored" if service["state"] == "missing" else service["state"]
        elif cycle is None or age is None:
            entry["state"] = "initializing"
        elif cycle["state"] == "ok" and (entry["news_poll_failures"] or (batch and batch["error"])):
            entry["state"] = "partial"
        else:
            entry["state"] = cycle["state"]
        entry["healthy"] = entry["state"] == "ok"
        if health_only:
            return entry
        entry.update(eligible_upcoming=[], dispatch={}, news_polls={}, score_revisions=0)
        for horizon, state, n in db.execute("SELECT horizon,state,COUNT(*) FROM domestic_jobs "
                                            "GROUP BY horizon,state ORDER BY horizon,state"):
            entry["dispatch"].setdefault(horizon, {})[state] = n
        entry["news_polls"] = dict(db.execute("SELECT state,COUNT(*) FROM domestic_news_polls GROUP BY state"))
        entry["score_revisions"] = db.execute("SELECT COUNT(*) FROM domestic_scores").fetchone()[0]
        entry.update(domestic_editions.evaluation(db))
        matches = db.execute("SELECT * FROM domestic_matches WHERE active=1 AND kickoff>? "
                             "ORDER BY kickoff,fixture", (at,))
        for match in matches:
            data = json.loads(match["metadata"])
            latest = db.execute("SELECT * FROM domestic_jobs WHERE fixture=? AND kickoff=? "
                                "AND sealed IS NOT NULL ORDER BY sealed DESC LIMIT 1",
                                (match["fixture"], match["kickoff"])).fetchone()
            if latest:
                payload = json.loads(latest["payload"])
                players = payload["news"].get("player_status", {})
                data["player_status"] = {
                    name: {"state": players.get(name, {}).get("state", "unknown"),
                           "home": len(players.get(name, {}).get("teams", {}).get("home", {}).get("players", [])),
                           "away": len(players.get(name, {}).get("teams", {}).get("away", {}).get("players", []))}
                    for name in ("injuries", "lineup")}
                data.update(latest_seal={"horizon": latest["horizon"], "sealed": latest["sealed"],
                            "model": payload["baseline"]["model"], "digest": latest["digest"],
                            "probabilities": payload["baseline"]["probabilities"],
                            "training_matches": payload["baseline"]["evidence"]["training_matches"],
                            "news_status": payload["pair"]["status"],
                            "player_scenario_status": payload.get("player_scenario", {}).get("status")})
            entry["eligible_upcoming"].append(data)
        if fixture_id and fixture_id.split("/")[0] == code:
            evaluation = domestic_editions.evaluation(db, fixture_id)
            entry["fixture_evaluation"] = evaluation["fixture_evaluation"]
            entry["fixture_evidence_changes"] = evaluation["fixture_evidence_changes"]
            entry["fixture_dispatch"] = [dict(row) for row in db.execute(
                "SELECT fixture,kickoff,horizon,revision,opens,deadline,state,attempts,attempted,error,"
                "sealed,digest,payload FROM domestic_jobs WHERE fixture=? ORDER BY kickoff,opens,sealed",
                (fixture_id,))]
            for row in entry["fixture_dispatch"]:
                if row["payload"]:
                    row["payload"] = json.loads(row["payload"])
            entry["fixture_results"] = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM domestic_scores WHERE fixture=? ORDER BY result_at", (fixture_id,))]
        return entry


def status(root: Path, *, leagues=NEW_LEAGUES, fixture_id=None, health_only=False):
    report = {"scope": SCOPE, "formal_eligible": False, "max_age_seconds": HEALTH_MAX_AGE, "leagues": {}}
    for code in _codes(leagues):
        try:
            report["leagues"][code] = _status_entry(root, code, fixture_id, health_only)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            report["leagues"][code] = {"state": "unavailable", "healthy": False, "error": _error(exc)}
    report["healthy"] = all(row["healthy"] for row in report["leagues"].values())
    return report


def run(root, *, leagues, market_state_dir, football_root=None, repeat=False, interval=30):
    if not 1 <= interval <= 3600:
        raise ValueError("interval must be between 1 and 3600 seconds")
    selected = _codes(leagues)
    if not repeat:
        return tick(root, leagues=selected, market_state_dir=market_state_dir, football_root=football_root)
    history = Path(football_root) if football_root is not None else Path(root) / "openfootball"
    with market_watch_health.stop_signals(True), ExitStack() as scope:
        heartbeats = {code: scope.enter_context(market_watch_health.monitor(
            _directory(root, code), [code], None, None, continuous=True, discover=False, notify_systemd=False))
            for code in selected}
        workers = scope.enter_context(ThreadPoolExecutor(max_workers=len(selected)))
        pollers = scope.enter_context(ThreadPoolExecutor(max_workers=len(selected)))
        cycles, polls = {}, {}
        completed = dict.fromkeys(selected, 0)
        progressed = set()
        ready = False

        def collect_cycles():
            for code, task in list(cycles.items()):
                if not task.done():
                    continue
                del cycles[code]
                try:
                    state = task.result()["state"]
                except Exception:
                    _LOG.exception("Cannot complete or persist domestic forecast cycle for %s", code)
                    continue
                if state == "newer_cycle_in_progress":
                    continue
                completed[code] += 1
                heartbeats[code](completed[code], None)
                progressed.add(code)
                if code not in polls and state in {"ok", "partial"}:
                    polls[code] = pollers.submit(_poll_batch, root, code)

        try:
            while True:
                ticked = time.monotonic()
                for code, poll in list(polls.items()):
                    if poll.done():
                        del polls[code]
                        try:
                            poll.result()
                        except Exception:
                            _LOG.exception("Cannot persist domestic news poll for %s", code)
                collect_cycles()
                for code in selected:
                    if code not in cycles:
                        cycles[code] = workers.submit(_league_tick, Path(root), code, history, market_state_dir)
                # Slow source I/O stays in its own league lane across ticks. Quick startup can
                # publish READY immediately, while an indefinitely blocked lane never feeds systemd.
                wait(cycles.values(), timeout=min(interval, 1))
                collect_cycles()
                if len(progressed) == len(selected):
                    market_watch_health._notify("WATCHDOG=1" if ready else "READY=1\nWATCHDOG=1")
                    ready = True
                    progressed.clear()
                time.sleep(max(0, ticked + interval - time.monotonic()))
        finally:
            market_watch_health._notify("STOPPING=1")


def _watchdog_exit(report, *, alert_only=False):
    states = {row["state"] for row in report["leagues"].values()}
    if not states or states - {"ok", "partial"}:
        return 1
    if "partial" in states and not alert_only:
        return 2
    return 0


def register(commands):
    parser = commands.add_parser("big5-scheduler", help="四个欧洲联赛的赛前研究预测、新闻和评分")
    sub = parser.add_subparsers(dest="scheduler_action", required=True)
    start = sub.add_parser("run", help="采集一次，或持续运行研究调度")
    start.add_argument("--league", choices=NEW_LEAGUES, dest="leagues", action="append")
    start.add_argument("--football-root", type=Path)
    start.add_argument("--market-state-dir", type=Path, required=True)
    start.add_argument("--repeat", action="store_true")
    start.add_argument("--interval", type=int, default=30)
    start.set_defaults(func=lambda a: run(a.data_dir, leagues=a.leagues or NEW_LEAGUES,
        market_state_dir=a.market_state_dir, football_root=a.football_root,
        repeat=a.repeat, interval=a.interval),
        result_exit_code=lambda r: int(any(v["state"] in {"partial", "retry"} for v in r["leagues"].values())))
    show = sub.add_parser("status", help="查看四联赛赛程、封存预测、窗口和实收赛果")
    show.add_argument("--league", choices=NEW_LEAGUES, dest="leagues", action="append")
    show.add_argument("--fixture")
    show.set_defaults(func=lambda a: status(a.data_dir, leagues=a.leagues or NEW_LEAGUES, fixture_id=a.fixture),
                      result_exit_code=lambda r: int(not r["healthy"]))
    check = sub.add_parser("watchdog", help="仅检查常驻调度进度、联赛状态和新闻采集错误")
    check.add_argument("--league", choices=NEW_LEAGUES, dest="leagues", action="append")
    check.add_argument("--alert-on-unavailable", dest="result_exit_code", action="store_const",
                       const=lambda r: _watchdog_exit(r, alert_only=True),
                       help="仅在调度停滞、未启动、重试或时钟冲突时使服务失败；partial 仍保留在 JSON 中")
    check.set_defaults(func=lambda a: status(a.data_dir, leagues=a.leagues or NEW_LEAGUES, health_only=True),
                       result_exit_code=_watchdog_exit)
