"""Prospective, research-only forecasting for the four additional domestic leagues."""

from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from . import dbscope, football, football_model, league_player_status, news_model, pipeline, store
from .big5_market import NEW_LEAGUES


WINDOW = timedelta(minutes=5)
LOOKAHEAD = timedelta(days=7)
SCOPE = "prospective_technical_research"
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


def _directory(root, league):
    return Path(root) / "forecast-scheduler" / league


def _database(root, league):
    db = store.connect(_directory(root, league))
    try:
        dbscope.prepare(db, "domestic-forecast-scheduler-v1", SCHEMA)
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


def _register(db, inputs, league, started, observed):
    """A later, complete scan owns the active schedule even if an older scan finishes last."""
    fixtures = {}
    bindings = inputs.provenance["market_fixtures"]
    seen = store.iso(observed)
    with db:
        db.execute("BEGIN IMMEDIATE")
        newer = db.execute("SELECT started FROM domestic_run WHERE id=1").fetchone()
        if newer and newer[0] > started:
            return None
        db.execute("INSERT INTO domestic_run VALUES(1,?,NULL,'running','{}') "
                   "ON CONFLICT(id) DO UPDATE SET started=excluded.started,finished=NULL,state='running'",
                   (started,))
        db.execute("UPDATE domestic_matches SET active=0")
        for fixture in inputs.upcoming:
            fid = fixture.fixture_id
            if not observed < fixture.kickoff <= observed + LOOKAHEAD or fixture.quarantine_reason:
                continue
            kickoff = store.iso(fixture.kickoff)
            binding = bindings[fid]
            if store.instant(binding["kickoff"]) != fixture.kickoff or store.instant(binding["receipt"]) > observed:
                raise ValueError(f"{fid}: market evidence does not establish this current kickoff")
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
    snapshot["source_digests"] = sources
    snapshot["evidence_sha256"] = store.digest(sources) if any(sources.values()) else None
    return snapshot


def _changes(db, fixture, snapshot, at):
    fingerprint = snapshot.get("evidence_sha256")
    fid, kick = fixture.fixture_id, store.iso(fixture.kickoff)
    prior = db.execute("SELECT digest,payload FROM domestic_jobs WHERE fixture=? AND kickoff=? AND "
                       "horizon IN ('preview','news_update') AND sealed IS NOT NULL "
                       "ORDER BY sealed DESC LIMIT 1", (fid, kick)).fetchone()
    if not prior:
        return
    previous = json.loads(prior["payload"])
    if previous["news"].get("evidence_sha256") == fingerprint:
        db.execute("UPDATE domestic_jobs SET state='superseded' WHERE fixture=? AND kickoff=? AND "
                   "horizon='news_update' AND state IN ('pending','retry')", (fid, kick))
        return
    # Including the previous seal also represents a later correction back to an earlier state.
    revision = store.digest([prior["digest"], fingerprint])
    db.execute("INSERT OR IGNORE INTO domestic_jobs(fixture,kickoff,horizon,revision,opens,deadline,state) "
               "VALUES(?,?,'news_update',?,?,?,'pending')",
               (fid, kick, revision, store.iso(at), kick))
    db.execute("UPDATE domestic_jobs SET state='superseded' WHERE fixture=? AND kickoff=? AND "
               "horizon='news_update' AND revision<>? AND state IN ('pending','retry')",
               (fid, kick, revision))


def _dispatch(db, engine, fixture, binding, data, job, snapshot, market_root, at):
    key = (job["fixture"], job["kickoff"], job["horizon"], job["revision"])
    deadline = store.instant(job["deadline"])
    columns = "fixture=? AND kickoff=? AND horizon=? AND revision=?"
    if not store.instant(job["opens"]) <= at < deadline or at >= fixture.kickoff:
        return
    try:
        base = engine.forecast(fixture.home, fixture.away, fixture.kickoff, at, season=fixture.season)
        _, pair = news_model.paired_prediction(base, snapshot.get("facts", ()), home=fixture.home,
                                               away=fixture.away, cutoff=at,
                                               coverage={"evidence_sha256": snapshot.get("evidence_sha256"),
                                                         "both_teams_observed": False})
        payload = {"scope": SCOPE, "mode": "technical", "formal_eligible": False,
                   "fixture": data, "horizon": job["horizon"], "input_cutoff": store.iso(at),
                   "deadline": job["deadline"], "schedule_evidence": binding,
                   "baseline": base, "news": snapshot, "pair": pair,
                   "player_scenario": league_player_status.absence_scenario(base, snapshot, cutoff=at)}
        raw = store.canonical(payload)
        digest = store.digest(payload)
        with db:
            db.execute("BEGIN IMMEDIATE")
            now = _clock()
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


def _score(db, fixtures, observed):
    by_id = {f.fixture_id: f for f in fixtures if f.result_versions}
    scored = 0
    jobs = db.execute("SELECT fixture,kickoff,horizon,revision,sealed,digest FROM domestic_jobs "
                      "WHERE sealed IS NOT NULL").fetchall()
    for job in jobs:
        fixture = by_id.get(job["fixture"])
        if not fixture:
            continue
        versions = [v for v in fixture.result_versions if v.available <= observed and
                    v.grade in football.RECEIPT_GRADES]
        if not versions:
            continue
        version = max(versions, key=lambda v: v.available)
        if fixture.quarantine_reason and version.score is not None:
            continue
        if store.iso(version.actual_kickoff or fixture.kickoff) != job["kickoff"]:
            continue
        if store.instant(job["sealed"]) >= version.available:
            continue
        record = {"score": version.score, "source": version.provenance, "grade": version.grade,
                  "received": store.iso(version.available), "forecast_sha256": job["digest"]}
        fingerprint = store.digest(record)
        key = (job["fixture"], job["kickoff"], job["horizon"], job["revision"],
               record["received"], fingerprint)
        if db.execute("SELECT 1 FROM domestic_scores WHERE fixture=? AND kickoff=? AND horizon=? AND "
                      "revision=? AND result_at=? AND result_sha=?", key).fetchone():
            continue
        if version.score is not None:
            original = db.execute("SELECT payload FROM domestic_jobs WHERE fixture=? AND kickoff=? "
                                  "AND horizon=? AND revision=?", key[:4]).fetchone()
            forecast = json.loads(original[0])
            if store.digest(forecast) != job["digest"]:
                raise ValueError("forecast digest mismatch before scoring")
            record["scores"] = {
                arm: football_model.score_probabilities(forecast["pair"][arm]["probabilities"], *version.score)
                for arm in ("no_news", "with_news")}
            scenario = forecast.get("player_scenario", {})
            if scenario.get("changed"):
                record["scores"]["uncalibrated_player_scenario"] = football_model.score_probabilities(
                    scenario["probabilities"], *version.score)
        with db:
            scored += db.execute("INSERT OR IGNORE INTO domestic_scores VALUES(?,?,?,?,?,?,?)",
                                 (*key, store.canonical(record))).rowcount
    return scored


def _league_tick(root, code, football_root, market_root):
    started = store.iso(_clock())
    detail = {}
    with closing(_database(root, code)) as db:
        try:
            inputs = _load(root, code, football_root, market_root)
            observed = _clock()
            candidates = _register(db, inputs, code, started, observed)
            if candidates is None:
                return {"state": "newer_cycle_in_progress"}
            engine = football.FootballEngine(inputs.fixtures, league=code, allow_archive_schedule=False)
            detail = {k: v for k, v in inputs.provenance.items() if k != "market_fixtures"}
            snapshots = {}
            # Read completed source evidence from disk. Asynchronous network polls cannot block fixed windows.
            for fid, fixture in candidates.items():
                cutoff = _clock()
                data = _metadata(fixture, code)
                snapshot = _news(root, data, cutoff)
                snapshots[fid] = (cutoff, data, snapshot)
                with db:
                    _changes(db, fixture, snapshot, cutoff)
            jobs = db.execute("SELECT j.* FROM domestic_jobs j JOIN domestic_matches m "
                              "ON m.fixture=j.fixture AND m.kickoff=j.kickoff WHERE m.active=1 AND "
                              "j.state IN ('pending','retry') AND j.opens<=? ORDER BY j.deadline,j.fixture",
                              (store.iso(_clock()),)).fetchall()
            for job in jobs:
                fid = job["fixture"]
                fixture = candidates.get(fid)
                if not fixture or job["kickoff"] != store.iso(fixture.kickoff):
                    continue
                at, data, snapshot = snapshots[fid]
                if store.instant(job["opens"]) > at:
                    at = _clock()
                    snapshot = _news(root, data, at)
                _dispatch(db, engine, fixture, inputs.provenance["market_fixtures"][fid],
                          data, job, snapshot, market_root, at)
            detail["eligible_upcoming"] = len(candidates)
            detail["score_revisions_written"] = _score(db, inputs.fixtures, _clock())
            pending = db.execute("SELECT COUNT(*) FROM domestic_jobs WHERE state='retry'").fetchone()[0]
            detail["dispatch_retries"] = pending
            state = "partial" if pending else "ok"
        except Exception as exc:
            state = "retry"
            detail["error"] = f"{type(exc).__name__}: {exc}"
        with db:
            db.execute("INSERT INTO domestic_run VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "started=excluded.started,finished=excluded.finished,state=excluded.state,"
                       "detail=excluded.detail WHERE excluded.started>=domestic_run.started",
                       (started, store.iso(_clock()), state, store.canonical(detail)))
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
                for collect in (league_rosters.collect_fixture_rosters, league_player_status.collect):
                    try:
                        collect(root, fixture)
                    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
                        errors.append(str(exc))
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
            except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
                state, error, interval = "retry", str(exc), timedelta(minutes=5)
            with db:
                db.execute("UPDATE domestic_news_polls SET state=?,error=?,due=? "
                           "WHERE fixture=? AND kickoff=? AND attempted=?",
                           (state, error, store.iso(_clock() + interval), fid, kickoff, stamp))
            count += 1
    return count


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
            polls = {code: workers.submit(_poll, root, code) for code in selected
                     if results[code]["state"] in {"ok", "partial"}}
            for code, future in polls.items():
                try:
                    results[code]["news_polls"] = future.result()
                except Exception as exc:
                    results[code]["news_poll_error"] = f"{type(exc).__name__}: {exc}"
    return {"scope": SCOPE, "leagues": results}


def status(root: Path, *, leagues=NEW_LEAGUES, fixture_id=None):
    report = {"scope": SCOPE, "formal_eligible": False, "leagues": {}}
    for code in _codes(leagues):
        path = _directory(root, code) / "runtime.sqlite3"
        if not path.is_file():
            report["leagues"][code] = {"state": "not_started"}
            continue
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='domestic_run'").fetchone():
                report["leagues"][code] = {"state": "initializing"}
                continue
            cycle = db.execute("SELECT * FROM domestic_run").fetchone()
            entry = {"state": cycle["state"] if cycle else "initializing", "last_cycle": None,
                     "eligible_upcoming": [], "dispatch": {}, "news_polls": {}, "score_revisions": 0}
            if cycle:
                entry["last_cycle"] = {"started": cycle["started"], "finished": cycle["finished"],
                                       **json.loads(cycle["detail"])}
            for horizon, state, n in db.execute("SELECT horizon,state,COUNT(*) FROM domestic_jobs "
                                                "GROUP BY horizon,state ORDER BY horizon,state"):
                entry["dispatch"].setdefault(horizon, {})[state] = n
            entry["news_polls"] = dict(db.execute("SELECT state,COUNT(*) FROM domestic_news_polls GROUP BY state"))
            entry["score_revisions"] = db.execute("SELECT COUNT(*) FROM domestic_scores").fetchone()[0]
            at = store.iso(_clock())
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
                entry["fixture_dispatch"] = [dict(row) for row in db.execute(
                    "SELECT fixture,kickoff,horizon,revision,opens,deadline,state,attempts,attempted,error,"
                    "sealed,digest,payload FROM domestic_jobs WHERE fixture=? ORDER BY kickoff,opens,sealed",
                    (fixture_id,))]
                for row in entry["fixture_dispatch"]:
                    if row["payload"]:
                        row["payload"] = json.loads(row["payload"])
                entry["fixture_results"] = [json.loads(row[0]) for row in db.execute(
                    "SELECT payload FROM domestic_scores WHERE fixture=? ORDER BY result_at", (fixture_id,))]
            report["leagues"][code] = entry
    return report


def run(root, *, leagues, market_state_dir, football_root=None, repeat=False, interval=30):
    if not 1 <= interval <= 3600:
        raise ValueError("interval must be between 1 and 3600 seconds")
    selected = _codes(leagues)
    if not repeat:
        return tick(root, leagues=selected, market_state_dir=market_state_dir, football_root=football_root)
    # Polling can take several requests per team. Each league has one independent poller.
    with ThreadPoolExecutor(max_workers=len(selected)) as pollers:
        pending = {}
        while True:
            ticked = time.monotonic()
            result = tick(root, leagues=selected, market_state_dir=market_state_dir,
                          football_root=football_root, collect_news=False)
            for code, state in result["leagues"].items():
                if state["state"] in {"ok", "partial"} and (code not in pending or pending[code].done()):
                    pending[code] = pollers.submit(_poll, root, code)
            time.sleep(max(0, ticked + interval - time.monotonic()))


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
        result_exit_code=lambda r: int(any(v["state"] == "retry" for v in r["leagues"].values())))
    show = sub.add_parser("status", help="查看四联赛赛程、封存预测、窗口和实收赛果")
    show.add_argument("--league", choices=NEW_LEAGUES, dest="leagues", action="append")
    show.add_argument("--fixture")
    show.set_defaults(func=lambda a: status(a.data_dir, leagues=a.leagues or NEW_LEAGUES, fixture_id=a.fixture))
