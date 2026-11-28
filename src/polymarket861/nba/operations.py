"""NBA research scheduling and operator commands. All timestamps are actual UTC receipts."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..store import canonical, digest, instant, iso, now_utc
from . import storage

HORIZONS = {"T-24h": 1440, "T-60m": 60, "T-15m": 15}
WINDOW = timedelta(minutes=5)
# The services tick every minute. Allow another tick only when there is
# enough time for its startup, model fit and durable commit.
FINAL_SEAL_MARGIN = timedelta(seconds=75)
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
CREATE INDEX IF NOT EXISTS nba_window_deadlines ON nba_windows(deadline,game_day);
CREATE TABLE IF NOT EXISTS nba_pairing_outcomes (
    game_id TEXT NOT NULL, epoch INTEGER NOT NULL, horizon TEXT NOT NULL,
    assessed_at TEXT NOT NULL, win_quote TEXT, sha256 TEXT NOT NULL, PRIMARY KEY(game_id,epoch,horizon)
);
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


def _reference(root, game, horizon, as_of, source_epoch_at=None):
    from . import markets

    return markets.benchmark(
        root,
        game["home_abbr"],
        game["away_abbr"],
        game["kickoff"],
        horizon,
        as_of=as_of,
        **({"source_epoch_at": source_epoch_at} if source_epoch_at is not None else {}),
    )


def _decision_bounds(kickoff, venue_kickoff, horizon, source_epoch_at=None):
    source, venue = instant(kickoff), instant(venue_kickoff)
    offset = timedelta(minutes=HORIZONS[horizon])
    opened = max(source, venue) - offset - WINDOW
    if source_epoch_at:
        opened = max(opened, instant(source_epoch_at))
    return opened, min(source, venue) - offset


def _observed_markets(raw, as_of, kickoff, horizon, source_epoch_at=None):
    # A later closing reference is reserved for evaluations, never for model inputs.
    allowed = (
        "condition_id",
        "token_id",
        "line",
        "probability",
        "observed_at",
        "available_at",
        "venue_event_id",
        "venue_version",
        "source_kickoff",
        "venue_kickoff",
        "delta_seconds",
        "source_epoch_at",
    )
    result = {}
    for key, quote in raw.items():
        if (
            not isinstance(quote, dict)
            or not quote.get("observed_at")
            or not quote.get("available_at")
        ):
            continue
        opened, deadline = _decision_bounds(
            kickoff, quote.get("venue_kickoff") or kickoff, horizon, source_epoch_at
        )
        available = instant(quote["available_at"])
        if (
            opened <= instant(quote["observed_at"]) <= available <= instant(as_of)
            and available < deadline
        ):
            result[key] = {k: quote[k] for k in allowed if k in quote}
    return result


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
    if proof and proof.get("state") not in ("ok", "running", None):
        raise ScheduleUnverified("latest schedule scan failed: " + str(proof["state"]))
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
    from . import data, markets, model, warmup

    horizon = window["horizon"]
    schedule_proof = _schedule_proof(root, db, game, start)
    match_args = (root, game["home_abbr"], game["away_abbr"], game["kickoff"])
    match = markets.schedule_match(*match_args, as_of=start)
    source_epoch_at = None
    if window["epoch"] > 1:
        versions = data.game_versions(root, game_id=game["game_id"], as_of=start)
        epoch = require_active_epoch(versions)["game"]
        if epoch["version"] != window["epoch"]:
            raise ValueError("schedule changed before prediction")
        if any(
            v["version"] < window["epoch"]
            and v["status"] in ("scheduled", "postponed", "withdrawn")
            for v in versions
        ):
            source_epoch_at = max(epoch["available_at"], epoch["received_at"])
    reference = _observed_markets(
        _reference(root, game, horizon, start, source_epoch_at),
        start,
        game["kickoff"],
        horizon,
        source_epoch_at,
    )
    # After the intersection closes a new receipt cannot qualify. A disjoint
    # clock still permits an unpaired sporting forecast on the source clock.
    opened, deadline = _decision_bounds(
        game["kickoff"],
        match.get("venue_kickoff") or game["kickoff"],
        horizon,
        source_epoch_at,
    )
    if (
        "win" not in reference
        and match["status"] != "outside_tolerance"
        and max(opened, instant(start)) < deadline
        and instant(window["deadline"]) - instant(start) > FINAL_SEAL_MARGIN
    ):
        return None
    lines = {
        k: reference[m]["line"]
        for k, m in (("total", "total"), ("home", "home_total"), ("away", "away_total"))
        if m in reference and reference[m].get("line") is not None
    }
    before = model.predict(history, game, lines=lines, as_of=start)
    if "win" in reference:
        quote_state = "paired_win"
    elif match["status"] in ("exact", "within_tolerance") and opened >= deadline:
        quote_state = "no_shared_decision_window"
    else:
        quote_state = "no_qualified_win_quote"
    body = {
        "scope": "nba_pregame_research",
        "paper_positions": False,
        "game": game,
        "schedule_epoch": window["epoch"],
        "source_epoch_at": source_epoch_at,
        "schedule_verification": schedule_proof,
        "market_schedule": match,
        "decision_quote_state": quote_state,
        "warmup": warmup.coverage(root, history, game, start),
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
            if reference:
                current = markets.schedule_match(*match_args, as_of=check)
                if any(
                    current.get(k) != match.get(k)
                    for k in (
                        "status",
                        "venue_event_id",
                        "venue_version",
                        "venue_kickoff",
                    )
                ):
                    raise ValueError("market schedule changed during prediction")
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
    if since and since < start:
        # Existing withheld windows and unregistered fixtures may become due
        # without a new source revision. Publish that change exactly once.
        dates = {
            r[0]
            for r in db.execute(
                "SELECT DISTINCT game_day FROM nba_windows WHERE deadline>=? AND deadline<?",
                (since, start),
            )
        }
        lower, upper = instant(since), instant(start)
        unregistered = db.execute(
            """SELECT g.game_day,json_extract(g.game,'$.kickoff') AS tip FROM nba_ops_games g
               WHERE g.stage='regular' AND g.game_day BETWEEN ? AND ? AND NOT EXISTS(
                 SELECT 1 FROM nba_windows w WHERE w.game_id=g.game_id AND w.active=1
               )""",
            (str(_day(since)), str(_day(upper + timedelta(days=1)))),
        )
        for game_day, tip in unregistered:
            if tip:
                if any(
                    lower <= instant(tip) - timedelta(minutes=m) < upper
                    for m in HORIZONS.values()
                ):
                    dates.add(game_day)
            elif str(_day(since)) <= game_day < str(_day(start)):
                dates.add(game_day)
        for day in dates:
            _dirty_day(db, day)
    db.execute("INSERT OR REPLACE INTO nba_ops_cursor VALUES(1,?)", (start,))


def _catalogue_coverage(db, as_of):
    """Check the independent venue calendar against today's local T-60m work."""
    from .markets_contracts import team_code

    day = _day(as_of)
    tips = []
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='nba_market_schedule'"
    ).fetchone():
        other_stages = {
            (team_code(r[0]), team_code(r[1]))
            for r in db.execute(
                """SELECT json_extract(game,'$.home_abbr'),json_extract(game,'$.away_abbr')
                   FROM nba_ops_games WHERE game_day=? AND stage IN ('preseason','playin','postseason','cup_final')""",
                (str(day),),
            )
        }
        rows = db.execute(
            """SELECT s.kickoff,g.home,g.away FROM nba_market_schedule s
               JOIN nba_market_games g USING(id) WHERE s.version=(
                  SELECT MAX(v.version) FROM nba_market_schedule v WHERE v.id=s.id AND v.valid_from<=?
               ) AND s.kickoff>=? AND s.kickoff<?""",
            (
                as_of,
                iso(datetime.combine(day, time.min, EASTERN)),
                iso(datetime.combine(day + timedelta(days=1), time.min, EASTERN)),
            ),
        )
        tips = [
            instant(r["kickoff"])
            for r in rows
            if (r["home"], r["away"]) not in other_stages
        ]
    due = sum(t - timedelta(minutes=60) < instant(as_of) for t in tips)
    rows = db.execute(
        """SELECT state,deadline FROM nba_windows
           WHERE active=1 AND game_day=? AND horizon='T-60m'""",
        (str(day),),
    ).fetchall()
    registered = len(rows)
    sealed = sum(r["state"] == "sealed" for r in rows)
    alert = None
    if tips and not registered:
        alert = "market_catalog_without_regular_schedule"
    elif due and not sealed and any(r["deadline"] < as_of for r in rows):
        alert = "market_catalog_due_without_seal"
    return {
        "game_day": str(day),
        "catalogued_games": len(tips),
        "catalogued_t60_due": due,
        "registered_t60": registered,
        "sealed_t60": sealed,
        "alert": alert,
    }


def forecast_cycle(root, *, clock=now_utc):
    from . import data, reconciliation
    from .operations_report import refresh_grades, refresh_pairing, refresh_reports

    failures = []
    sealed = deferred = 0
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
                        "SELECT game FROM nba_ops_games WHERE stage IN ('regular','unknown')"
                    )
                ]
                if pending
                else []
            )
        for row in pending:
            try:
                admitted = _make_seal(
                    root, db, json.loads(row["game"]), row, history, start, clock
                )
                if admitted is None:
                    deferred += 1
                    with db:
                        db.execute(
                            """UPDATE nba_windows SET note='waiting_for_win_quote'
                               WHERE game_id=? AND epoch=? AND horizon=? AND state='pending'
                                 AND note<>'waiting_for_win_quote'""",
                            (row["game_id"], row["epoch"], row["horizon"]),
                        )
                else:
                    sealed += admitted
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
        refresh_pairing(db, root, finish)
        reconciliation.refresh(db, finish)
        grades, grading_errors = refresh_grades(db, root, finish)
        failures.extend(grading_errors)
        reports = refresh_reports(db, finish)
        evidence = _catalogue_coverage(db, finish)
    proof = data.schedule_proof(root, str(_day(finish)), finish)
    failed_scan = proof and proof.get("state") not in ("ok", "running")
    reason = evidence["alert"] or (
        "schedule_verification_failed"
        if failed_scan
        else "no_observed_schedule"
        if not games
        else None
    )
    return {
        "state": "partial" if failures or reason else "ok",
        "schedule_games": games,
        "reason": reason,
        "sealed": sealed,
        "waiting_for_win_quote": deferred,
        "catalogue_coverage": evidence,
        "schedule_scan_state": proof.get("state") if proof else None,
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


def status(root, *, component="all", clock=now_utc):
    from . import reconciliation

    if component not in ("all", *_COMPONENT_MAX_AGE):
        raise ValueError("unknown NBA watchdog component")
    rows = {}
    counts = {}
    payouts = reconciliation.summary(None)
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
            payouts = reconciliation.summary(db)
    status_by_component = {}
    now = instant(clock())
    for k, limit in _COMPONENT_MAX_AGE.items():
        if component != "all" and k != component:
            continue
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
        "scope": component,
        "components": status_by_component,
        "windows": counts,
        "payout_reconciliation": payouts,
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
        cmd = subparsers.add_parser(name)
        cmd.add_argument(
            "--component", choices=("all", *_COMPONENT_MAX_AGE), default="all"
        )
        cmd.set_defaults(
            func=lambda a: status(a.data_dir, component=a.component),
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
