"""NBA research scheduling and operator commands. All timestamps are actual UTC receipts."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from ..store import canonical, digest, instant, iso, now_utc
from . import storage

HORIZONS = {"T-24h": 1440, "T-60m": 60, "T-15m": 15}
WINDOW = timedelta(minutes=5)
EASTERN = ZoneInfo("America/New_York")
_COMPONENT_MAX_AGE = {"markets": 180, "schedule": 2100, "forecast": 180}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS nba_cycles (
    component TEXT PRIMARY KEY, started TEXT NOT NULL, finished TEXT, state TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nba_windows (
    game_id TEXT NOT NULL, epoch INTEGER NOT NULL, horizon TEXT NOT NULL,
    kickoff TEXT NOT NULL, game_day TEXT NOT NULL, deadline TEXT NOT NULL,
    active INTEGER NOT NULL, state TEXT NOT NULL, note TEXT NOT NULL,
    sealed_at TEXT, payload TEXT, sha256 TEXT,
    PRIMARY KEY(game_id,epoch,horizon)
);
CREATE INDEX IF NOT EXISTS nba_windows_due ON nba_windows(state,active,deadline);
CREATE TABLE IF NOT EXISTS nba_forecast_candidates (
    sha256 TEXT PRIMARY KEY, game_id TEXT NOT NULL, epoch INTEGER NOT NULL,
    horizon TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nba_ops_cursor (id INTEGER PRIMARY KEY CHECK(id=1), as_of TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nba_ops_games (
    game_id TEXT PRIMARY KEY, version INTEGER NOT NULL, game_day TEXT NOT NULL,
    stage TEXT NOT NULL, status TEXT NOT NULL, game TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS nba_ops_days ON nba_ops_games(stage,game_day);
CREATE TABLE IF NOT EXISTS nba_grade_queue (game_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS nba_report_queue (kind TEXT NOT NULL, period TEXT NOT NULL, PRIMARY KEY(kind,period));
CREATE INDEX IF NOT EXISTS nba_windows_day ON nba_windows(game_day,active);
CREATE TABLE IF NOT EXISTS nba_grade_versions (
    game_id TEXT NOT NULL, epoch INTEGER NOT NULL, horizon TEXT NOT NULL,
    revision INTEGER NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL,
    PRIMARY KEY(game_id,epoch,horizon,revision)
);
CREATE TABLE IF NOT EXISTS nba_period_reports (
    kind TEXT NOT NULL, period TEXT NOT NULL, revision INTEGER NOT NULL, generated_at TEXT NOT NULL,
    payload TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(kind,period,revision)
);
"""


def _open(root):
    db = storage.connect(root)
    db.executescript(_SCHEMA)
    return db


def _day(stamp):
    return instant(stamp).astimezone(EASTERN).date()


def _schedule(game):
    return (
        game.get("home"),
        game.get("away"),
        game.get("kickoff"),
        game.get("stage"),
        game.get("game_date"),
        game.get("season"),
    )


def _epochs(versions):
    """Keep the first receipt of each schedule, including intervening withdrawals."""
    epochs = []
    current = None
    participants = None
    barred = False
    previous_tip = None
    timely_withdrawal = False
    observed_play = False
    for row in versions:
        status = row["status"]
        if row.get("play_observed"):
            observed_play = True
            if current:
                current["played"] = True
        seen = instant(row["available_at"])
        tip = instant(row["kickoff"]) if row.get("kickoff") else None
        identity = (row["home"], row["away"])
        if participants is not None and participants != identity:
            barred = True
        if (
            current
            and _schedule(row) == _schedule(current["game"])
            and (
                status in ("scheduled", "live", "final")
                or (current["played"] and status == "unknown")
            )
        ):
            current["latest"] = row
            if status in ("live", "final"):
                current["played"] = observed_play = True
            continue
        if current:
            current["active"] = False
            previous_tip = instant(current["game"]["kickoff"])
            timely_withdrawal = status == "postponed" and seen < previous_tip
            current = None
        if status in ("live", "final"):
            observed_play = True
        if status != "scheduled" or not tip or row.get("stage") != "regular":
            continue
        if participants is None:
            participants = identity
        # A feed can relabel an already played game as scheduled with a new future tip.
        if previous_tip is not None and seen >= previous_tip and not timely_withdrawal:
            barred = True
        current = {
            "game": row,
            "latest": row,
            "active": True,
            "played": observed_play,
            "blocked": barred or observed_play or participants != identity,
        }
        epochs.append(current)
        timely_withdrawal = False
    return epochs


def require_active_epoch(versions):
    """A later label cannot undo observed play or a conflicted participant identity."""
    epochs = _epochs(versions)
    if not epochs or not epochs[-1]["active"]:
        raise ValueError("no active NBA schedule at cutoff")
    epoch = epochs[-1]
    if epoch["blocked"] or epoch["played"] or epoch["latest"]["status"] != "scheduled":
        raise ValueError(
            "schedule blocked: observed play, ambiguous identity or late replacement"
        )
    return epoch


class ScheduleUnverified(ValueError):
    """A research preview may show stale evidence, but may never override a conflict."""


def _reference(root, game, horizon, as_of):
    from . import markets

    return markets.benchmark(
        root,
        game["home_abbr"],
        game["away_abbr"],
        game["kickoff"],
        horizon,
        as_of=as_of,
    )


def _observed_markets(raw, as_of):
    # A later closing reference is reserved for evaluations, never for model inputs.
    allowed = (
        "condition_id",
        "token_id",
        "line",
        "probability",
        "observed_at",
        "available_at",
    )
    return {
        key: {k: quote.get(k) for k in allowed}
        for key, quote in raw.items()
        if isinstance(quote, dict)
        and quote.get("observed_at")
        and instant(quote["observed_at"]) <= instant(as_of)
        and instant(quote.get("available_at") or quote["observed_at"]) <= instant(as_of)
    }


def _windows(db, epoch, now):
    game = epoch["game"]
    tip = instant(game["kickoff"])
    for horizon, minutes in HORIZONS.items():
        deadline = tip - timedelta(minutes=minutes)
        key = (game["game_id"], game["version"], horizon)
        state, note = "pending", ""
        if not epoch["active"]:
            state, note = "superseded", "schedule_withdrawn_or_replaced"
        elif epoch["blocked"]:
            state, note = "withheld", "ambiguous_identity_or_late_replacement"
        elif instant(now) > deadline or epoch["played"]:
            state = "missed"
            note = (
                "schedule_first_observed_after_cutoff"
                if instant(game["available_at"]) > deadline
                else "window_expired_or_play_observed"
            )
        db.execute(
            """INSERT INTO nba_windows(game_id,epoch,horizon,kickoff,game_day,deadline,active,state,note)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(game_id,epoch,horizon) DO UPDATE SET
            active=excluded.active, state=CASE WHEN sealed_at IS NULL THEN excluded.state ELSE state END,
            note=CASE WHEN sealed_at IS NOT NULL THEN note
                      WHEN excluded.state IN ('pending','missed') AND note!='' THEN note ELSE excluded.note END""",
            (
                *key,
                iso(tip),
                str(game["game_date"]),
                iso(deadline),
                int(epoch["active"]),
                state,
                note,
            ),
        )


def _schedule_proof(root, db, game, as_of):
    from . import data, markets

    conflict = markets.fixture_conflict(
        root, game["home_abbr"], game["away_abbr"], game["kickoff"], as_of=as_of
    )
    if conflict:
        raise ValueError("market schedule conflict: " + conflict)
    proof = data.schedule_proof(root, game["game_date"], as_of)
    if not proof or not proof.get("complete") or proof.get("state") != "ok":
        raise ScheduleUnverified(
            "missing or incomplete schedule verification for this game day"
        )
    finish = proof["finished"]
    if (
        instant(finish) < instant(game["available_at"])
        or instant(finish) > instant(as_of)
        or instant(as_of) - instant(proof["began"])
        > timedelta(seconds=_COMPONENT_MAX_AGE["schedule"])
    ):
        raise ScheduleUnverified("stale schedule verification for this game day")
    if (
        db is not None
        and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nba_cycles'"
        ).fetchone()
    ):
        last = db.execute(
            "SELECT finished,detail FROM nba_cycles WHERE component='schedule'"
        ).fetchone()
        if (
            last
            and last["finished"]
            and finish <= last["finished"] <= as_of
            and json.loads(last["detail"]).get("state") != "ok"
        ):
            raise ScheduleUnverified(
                "scheduled source failed since last verified schedule"
            )
    return proof


def _make_seal(root, db, game, window, history, start, clock):
    from . import data, model

    horizon = window["horizon"]
    schedule_proof = _schedule_proof(root, db, game, start)
    reference = _observed_markets(_reference(root, game, horizon, start), start)
    lines = {
        k: reference[m]["line"]
        for k, m in (("total", "total"), ("home", "home_total"), ("away", "away_total"))
        if m in reference and reference[m].get("line") is not None
    }
    before = model.predict(history, game, lines=lines, as_of=start)
    body = {
        "scope": "nba_pregame_research",
        "paper_positions": False,
        "game": game,
        "schedule_epoch": window["epoch"],
        "schedule_verification": schedule_proof,
        "horizon": horizon,
        "deadline": window["deadline"],
        "input_cutoff": start,
        "forecast": before,
        "market_at_prediction": reference,
    }
    key = (game["game_id"], window["epoch"], horizon)
    # The clock before a SQLite commit cannot certify first persistence. Stage
    # the exact bytes first. A crash here leaves a non-scoring candidate only.
    candidate_sha = digest(body)
    with db:
        db.execute(
            "INSERT OR IGNORE INTO nba_forecast_candidates VALUES(?,?,?,?,?)",
            (candidate_sha, *key, canonical(body)),
        )
    previous_timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            check = iso(clock())
            _schedule_proof(root, db, game, check)
            versions = data.game_versions(root, game_id=game["game_id"], as_of=check)
            latest = require_active_epoch(versions)
            if (
                latest["game"]["version"] != window["epoch"]
                or latest["latest"]["version"] != game["version"]
            ):
                raise ValueError("schedule changed during prediction")
            saved = db.execute(
                "SELECT * FROM nba_forecast_candidates WHERE sha256=?", (candidate_sha,)
            ).fetchone()
            body = _unpack(saved)
            body["candidate_sha256"] = candidate_sha
            sealed_at = iso(clock())
            body["sealed_at"] = sealed_at
            encoded = canonical(body)
            sha = digest(body)
            written_at = iso(clock())
            if not (
                window["deadline"] >= written_at >= sealed_at >= start
                and instant(start) >= instant(window["deadline"]) - WINDOW
            ):
                raise ValueError("deadline passed during prediction")
            changed = db.execute(
                """UPDATE nba_windows SET state='sealed',note='',sealed_at=?,payload=?,sha256=?
                               WHERE game_id=? AND epoch=? AND horizon=? AND active=1 AND sealed_at IS NULL""",
                (sealed_at, encoded, sha, *key),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT OR IGNORE INTO nba_grade_queue VALUES(?)",
                    (game["game_id"],),
                )
                _dirty_day(db, game["game_date"])
            if iso(clock()) > window["deadline"]:
                raise ValueError("deadline passed while admitting durable forecast")
            # Any outstanding reader can hold a rollback journal commit. Retry the
            # admission on a later tick rather than wait beyond the true cutoff.
            db.execute("PRAGMA busy_timeout=0")
            return changed
    finally:
        db.execute(f"PRAGMA busy_timeout={int(previous_timeout)}")


def _unpack(row):
    obj = json.loads(row["payload"])
    if digest(obj) != row["sha256"]:
        raise ValueError("NBA research record checksum mismatch")
    if "sealed_at" in row.keys() and (
        obj.get("candidate_sha256")
        != digest(
            {k: v for k, v in obj.items() if k not in ("sealed_at", "candidate_sha256")}
        )
        or obj.get("sealed_at") != row["sealed_at"]
        or obj.get("horizon") != row["horizon"]
        or obj.get("schedule_epoch") != row["epoch"]
        or obj.get("deadline") != row["deadline"]
        or obj.get("game", {}).get("game_id") != row["game_id"]
        or iso(obj["game"]["kickoff"]) != row["kickoff"]
    ):
        raise ValueError("NBA forecast metadata mismatch")
    return obj


def _row_versions(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row["game_id"]].append(row)
    return groups


def _dirty_day(db, day):
    start = date.fromisoformat(day)
    monday = start - timedelta(days=start.weekday())
    db.executemany(
        "INSERT OR IGNORE INTO nba_report_queue VALUES(?,?)",
        [("day", day), ("week", monday.isoformat())],
    )


def _sync_schedule(root, db, start):
    """Process newly durable versions only. Source revisions and report work share one commit."""
    from . import data

    prev = db.execute("SELECT as_of FROM nba_ops_cursor WHERE id=1").fetchone()
    since = prev[0] if prev else None
    if since and start < since:
        raise ValueError("NBA receipt clock moved backwards")
    groups = _row_versions(data.game_versions(root, as_of=start, since=since))
    for gid, versions in groups.items():
        game = versions[-1]
        cached = db.execute(
            "SELECT version,game_day,stage FROM nba_ops_games WHERE game_id=?", (gid,)
        ).fetchone()
        if cached and cached["version"] >= game["version"]:
            continue
        if cached:
            if cached["stage"] == "regular":
                _dirty_day(db, cached["game_day"])
            versions = data.game_versions(root, game_id=gid, as_of=start)
        if game["stage"] == "regular":
            _dirty_day(db, game["game_date"])
        for epoch in _epochs(versions):
            _windows(db, epoch, start)
            _dirty_day(db, epoch["game"]["game_date"])
        db.execute(
            "INSERT OR REPLACE INTO nba_ops_games VALUES(?,?,?,?,?,?)",
            (
                gid,
                game["version"],
                game["game_date"],
                game["stage"],
                game["status"],
                canonical(game),
            ),
        )
        db.execute(
            """INSERT OR IGNORE INTO nba_grade_queue
                      SELECT ? WHERE EXISTS (SELECT 1 FROM nba_windows WHERE game_id=? AND sealed_at IS NOT NULL)""",
            (gid, gid),
        )
    db.execute("INSERT OR REPLACE INTO nba_ops_cursor VALUES(1,?)", (start,))


def forecast_cycle(root, *, clock=now_utc):
    from .operations_report import refresh_grades, refresh_reports

    failures = []
    sealed = 0
    with closing(_open(root)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            start = iso(clock())
            _sync_schedule(root, db, start)
            games = db.execute("SELECT COUNT(*) FROM nba_ops_games").fetchone()[0]
            pending = db.execute(
                """SELECT w.*, g.game FROM nba_windows w JOIN nba_ops_games g USING(game_id)
                    WHERE w.state='pending' AND w.active=1 AND w.deadline BETWEEN ? AND ?
                    ORDER BY w.deadline,w.game_id""",
                (start, iso(instant(start) + WINDOW)),
            ).fetchall()
            history = (
                [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT game FROM nba_ops_games WHERE stage='regular'"
                    )
                ]
                if pending
                else []
            )
        for row in pending:
            try:
                sealed += _make_seal(
                    root, db, json.loads(row["game"]), row, history, start, clock
                )
            except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
                error = str(exc)
                failures.append(
                    {
                        "game_id": row["game_id"],
                        "horizon": row["horizon"],
                        "error": error,
                    }
                )
                with db:
                    db.execute(
                        """UPDATE nba_windows SET note=? WHERE game_id=? AND epoch=? AND horizon=?
                                  AND sealed_at IS NULL""",
                        (error, row["game_id"], row["epoch"], row["horizon"]),
                    )
        finish = iso(clock())
        with db:
            db.execute("BEGIN IMMEDIATE")
            for (day,) in db.execute(
                "SELECT DISTINCT game_day FROM nba_windows WHERE state='pending' AND deadline<?",
                (finish,),
            ).fetchall():
                _dirty_day(db, day)
            db.execute(
                """UPDATE nba_windows SET state='missed',note=CASE WHEN note=''
                          THEN 'window_expired' ELSE note END WHERE state='pending' AND deadline<?""",
                (finish,),
            )
        grades, grading_errors = refresh_grades(db, root, finish)
        failures.extend(grading_errors)
        reports = refresh_reports(db, finish)
    return {
        "state": "partial" if failures or not games else "ok",
        "schedule_games": games,
        "reason": "no_observed_schedule" if not games else None,
        "sealed": sealed,
        "grade_revisions": grades,
        "report_versions": reports,
        "fixture_errors": failures,
    }


def _capture(root, component, clock):
    from . import data, markets

    start = iso(clock())
    with closing(_open(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        prev = db.execute(
            "SELECT started,state FROM nba_cycles WHERE component=?", (component,)
        ).fetchone()
        if (
            prev
            and prev["state"] == "running"
            and instant(start) - instant(prev["started"])
            < timedelta(seconds=_COMPONENT_MAX_AGE[component])
        ):
            return {"state": "busy", "component": component}
        db.execute(
            """INSERT INTO nba_cycles VALUES(?,?,NULL,'running','{}')
                   ON CONFLICT(component) DO UPDATE SET started=excluded.started,state='running'""",
            (component, start),
        )
    try:
        if component == "markets":
            detail = markets.collect(root)
        elif component == "schedule":
            day = _day(start)
            detail = data.sync_bdl(
                root, day - timedelta(days=14), day + timedelta(days=7)
            )
        else:
            detail = forecast_cycle(root, clock=clock)
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        detail = {"state": "unavailable", "error": str(exc)}
    with closing(_open(root)) as db, db:
        written = db.execute(
            "UPDATE nba_cycles SET finished=?,state=?,detail=? WHERE component=? AND started=?",
            (
                iso(clock()),
                detail.get("state", "unavailable"),
                canonical(detail),
                component,
                start,
            ),
        ).rowcount
    return detail if written else {"state": "superseded_cycle"}


def run(root, component="all", *, clock=now_utc):
    if component == "all":
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = {
                k: pool.submit(_capture, root, k, clock)
                for k in ("markets", "schedule")
            }
            components = {k: f.result() for k, f in jobs.items()}
        components["forecast"] = _capture(root, "forecast", clock)
    elif component in _COMPONENT_MAX_AGE:
        components = {component: _capture(root, component, clock)}
    else:
        raise ValueError("unknown NBA service component")
    return {
        "state": "ok"
        if all(c.get("state") == "ok" for c in components.values())
        else "degraded",
        "components": components,
        "paper_trading": False,
    }


def status(root, *, clock=now_utc):
    rows = {}
    counts = {}
    db = storage.open_existing(root)
    if db is not None:
        with closing(db):
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='nba_cycles'"
            ).fetchone():
                rows = {
                    r["component"]: dict(r)
                    for r in db.execute("SELECT * FROM nba_cycles")
                }
                counts = dict(
                    db.execute(
                        "SELECT state,COUNT(*) FROM nba_windows WHERE active=1 GROUP BY state"
                    )
                )
    status_by_component = {}
    now = instant(clock())
    for k, limit in _COMPONENT_MAX_AGE.items():
        row = rows.get(k)
        if row:
            age = (now - instant(row["finished"] or row["started"])).total_seconds()
            running = row["state"] == "running"
            last_run = json.loads(row["detail"])
            health = last_run.get("state", "starting") if running else row["state"]
            if not 0 <= age <= limit:
                health = "stale"
            status_by_component[k] = {
                "state": health,
                "running": running,
                "age_seconds": age,
                "started": row["started"],
                "finished": row["finished"],
                "last_run": last_run,
            }
        else:
            status_by_component[k] = {"state": "not_started"}
    healthy = all(v["state"] == "ok" for v in status_by_component.values())
    return {
        "state": "ok" if healthy else "degraded",
        "components": status_by_component,
        "windows": counts,
        "player_data": "no_admitted_pregame_source",
        "paper_trading": False,
    }


def forecast_show(root, game_id):
    db = storage.open_existing(root)
    out = []
    if db is not None:
        with closing(db):
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='nba_windows'"
            ).fetchone():
                for row in db.execute(
                    "SELECT * FROM nba_windows WHERE game_id=? ORDER BY epoch,deadline",
                    (game_id,),
                ):
                    rec = {k: v for k, v in dict(row).items() if k != "payload"}
                    if row["sealed_at"]:
                        rec["prediction"] = _unpack(row)
                    rec["grade_history"] = [
                        {
                            "revision": g["revision"],
                            "recorded_at": g["recorded_at"],
                            "grade": _unpack(g),
                        }
                        for g in db.execute(
                            """SELECT * FROM nba_grade_versions
                                             WHERE game_id=? AND epoch=? AND horizon=? ORDER BY revision""",
                            (game_id, row["epoch"], row["horizon"]),
                        )
                    ]
                    out.append(rec)
    return {
        "game_id": game_id,
        "windows": out,
        "state": "available" if out else "unavailable",
    }


def register(subparsers):
    from .operations_report import report

    cmd = subparsers.add_parser(
        "run", help="Run an NBA collection or forecasting cycle"
    )
    cmd.add_argument("--component", choices=("all", *_COMPONENT_MAX_AGE), default="all")
    cmd.set_defaults(
        func=lambda a: run(a.data_dir, a.component),
        result_exit_code=lambda r: int(r["state"] != "ok"),
    )
    for name in ("status", "watchdog"):
        subparsers.add_parser(name).set_defaults(
            func=lambda a: status(a.data_dir),
            result_exit_code=lambda r: int(r["state"] != "ok"),
        )
    cmd = subparsers.add_parser(
        "report", help="Read a versioned Eastern game day or week report"
    )
    parser = cmd.add_mutually_exclusive_group(required=True)
    parser.add_argument("--date", metavar="YYYY-MM-DD")
    parser.add_argument("--week", metavar="MONDAY")
    cmd.add_argument("--revision", type=int)
    cmd.set_defaults(
        func=lambda a: report(
            a.data_dir, "day" if a.date else "week", a.date or a.week, a.revision
        )
    )
    cmd = subparsers.add_parser(
        "forecast-show", help="Inspect sealed predictions and scoring revisions"
    )
    cmd.add_argument("game_id")
    cmd.set_defaults(func=lambda a: forecast_show(a.data_dir, a.game_id))
