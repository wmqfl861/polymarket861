"""One durable EPL matchday coordinator for collection, forecasts and paper accounts."""

import json
import sys
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from . import market_archive as exchange
from . import opportunity, runner, season_backfill, store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS season_fixtures(
 fixture TEXT PRIMARY KEY,kickoff TEXT NOT NULL,plan TEXT NOT NULL,first_seen TEXT NOT NULL,updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS season_slots(
 key TEXT PRIMARY KEY,fixture TEXT NOT NULL,kickoff TEXT NOT NULL,target TEXT NOT NULL,deadline TEXT NOT NULL,
 role TEXT NOT NULL,stage TEXT NOT NULL,state TEXT NOT NULL,owner TEXT,until TEXT,detail TEXT);
CREATE INDEX IF NOT EXISTS season_slot_due ON season_slots(stage,state,target,deadline);
CREATE TABLE IF NOT EXISTS season_services(
 service TEXT PRIMARY KEY,next_at TEXT NOT NULL,owner TEXT,until TEXT,updated TEXT,result TEXT);
CREATE TABLE IF NOT EXISTS season_alerts(
 key TEXT PRIMARY KEY,fixture TEXT,role TEXT,occurred TEXT,reason TEXT);
"""


def _db(root):
    db = exchange.database(root)
    db.executescript(_SCHEMA)
    return db


def year_of(now):
    return now.year - (now.month < 7)


def _add(db, fixture, kickoff, when, deadline, role, stage):
    target = store.iso(when)
    key = store.digest([fixture, kickoff, target, role, stage])
    db.execute(
        "INSERT OR IGNORE INTO season_slots VALUES(?,?,?,?,?,?,?,'pending',NULL,NULL,NULL)",
        (key, fixture, kickoff, target, store.iso(deadline), role, stage),
    )


def _plan(root, now):
    lower = store.iso(now - timedelta(minutes=10))
    upper = store.iso(now + timedelta(days=7))
    with closing(_db(root)) as db, db:
        current = db.execute(
            "WITH latest AS (SELECT *,row_number() OVER(PARTITION BY slug ORDER BY received_at DESC,sha DESC) n "
            "FROM ma_events WHERE received_at<=?) SELECT e.fixture,e.kickoff,e.received_at,e.sha,s.plan,s.kickoff AS prior "
            "FROM latest e LEFT JOIN season_fixtures s ON s.fixture=e.fixture WHERE e.n=1 AND e.slug=e.fixture "
            "AND (julianday(e.kickoff) BETWEEN julianday(?) AND julianday(?) OR s.fixture IS NOT NULL)",
            (store.iso(now), lower, upper),
        ).fetchall()
        for row in current:
            tag = row["fixture"]
            kick = store.instant(row["kickoff"])
            kickoff = store.iso(kick)
            # Offset, verified schedule and delays participate in the plan. New token listings
            # at an unchanged delay are collected by the same scheduled all-token book.
            if row["prior"] and row["prior"] != kickoff:
                db.execute(
                    "UPDATE season_slots SET state='superseded' WHERE fixture=? AND kickoff<>? AND state IN ('pending','running')",
                    (tag, kickoff),
                )
            if not lower <= kickoff <= upper:
                continue
            latest = db.execute(
                "SELECT received_at,sha FROM ma_events WHERE fixture=? AND received_at<=? "
                "ORDER BY received_at DESC,sha DESC LIMIT 1",
                (tag, store.iso(now)),
            ).fetchone()
            version = store.digest([kickoff, latest["received_at"], latest["sha"]])
            # The main receipt changes far more often than the grid. Skip recalculating an
            # unchanged schedule in the seconds leading up to a capture.
            old = json.loads(row["plan"]) if row["plan"] else {}
            if row["prior"] == kickoff and old.get("version") == version:
                continue
            active = db.execute(
                "WITH latest AS (SELECT *,row_number() OVER(PARTITION BY slug ORDER BY received_at DESC,sha DESC) n "
                "FROM ma_events WHERE fixture=? AND received_at<=?) SELECT DISTINCT json_extract(c.payload,'$.seconds_delay') delay "
                "FROM latest e JOIN ma_contracts c ON c.event_slug=e.slug AND c.received_at=e.received_at AND c.sha=e.sha "
                "WHERE n=1 AND json_extract(c.payload,'$.mapping_error') IS NULL",
                (tag, store.iso(now)),
            )
            delays = sorted(
                {max(5, int(r["delay"])) for r in active if type(r["delay"]) is int}
            ) or [5]
            db.execute(
                "INSERT INTO season_fixtures VALUES(?,?,?,?,?) ON CONFLICT(fixture) DO UPDATE SET kickoff=excluded.kickoff,"
                "plan=excluded.plan,updated=excluded.updated",
                (
                    tag,
                    kickoff,
                    store.canonical({"delays": delays, "version": version}),
                    store.iso(now),
                    store.iso(now),
                ),
            )
            if row["prior"] == kickoff and old.get("delays") == delays:
                continue
            if row["prior"] != kickoff:
                _add(
                    db,
                    tag,
                    kickoff,
                    now,
                    now + timedelta(seconds=60),
                    "initial",
                    "book",
                )
                _add(
                    db,
                    tag,
                    kickoff,
                    now,
                    now + timedelta(seconds=60),
                    "state",
                    "metadata",
                )
            cursor = now - timedelta(microseconds=1)
            while True:
                choices = [exchange.next_capture(kick, cursor, delay=d) for d in delays]
                future = [v for v in choices if v]
                if not future:
                    break
                target, role = min(future)
                # All targets advance strictly. A slot cannot be backdated on restart.
                if target <= cursor:
                    raise ValueError("non-advancing capture schedule")
                if role == "decision_book":
                    deadline = target + timedelta(seconds=25)
                elif role == "execution_book":
                    deadline = target + timedelta(seconds=5)
                elif role == "closing_book":
                    deadline = target + timedelta(seconds=12)
                else:
                    deadline = target + timedelta(seconds=45)
                _add(db, tag, kickoff, target, deadline, role, "book")
                if role == "closing_book" or (
                    role == "periodic"
                    and int((kick - target).total_seconds()) % 900 == 0
                ):
                    _add(db, tag, kickoff, target, deadline, "state", "metadata")
                if (
                    role == "decision_book"
                    and abs((kick - target).total_seconds() - 3625) < 1
                ):
                    for offset in (10, 21):
                        _add(
                            db,
                            tag,
                            kickoff,
                            target + timedelta(seconds=offset),
                            deadline,
                            "decision",
                            "paper",
                        )
                if (
                    role == "execution_book"
                    and abs((kick - target).total_seconds() - 3600) < 120
                ):
                    _add(
                        db,
                        tag,
                        kickoff,
                        target + timedelta(seconds=6),
                        target + timedelta(seconds=70),
                        "recover",
                        "paper",
                    )
                cursor = target


def _status_events(root, fixture, at):
    with closing(exchange.database(root)) as db:
        rows = db.execute(
            "SELECT slug,received_at FROM (SELECT *,row_number() OVER(PARTITION BY slug "
            "ORDER BY received_at DESC,sha DESC) n FROM ma_events WHERE fixture=? AND received_at<=?) WHERE n=1",
            (fixture, at),
        ).fetchall()
    return {
        "events": [r["slug"] for r in rows],
        "gaps": []
        if len(rows) == 2
        else [{"reason": "no archived auxiliary event at capture"}],
    }


def _football_coverage(root, now):
    end = now + timedelta(days=7)
    expected = [
        f
        for f in store.eligible_fixtures(root)
        if now <= store.instant(f["kickoff"]) <= end
    ]
    with closing(_db(root)) as db:
        slots = db.execute(
            "SELECT fixture,kickoff FROM season_fixtures WHERE kickoff BETWEEN ? AND ?",
            (store.iso(now), store.iso(end)),
        ).fetchall()
    by_time = {}
    for row in slots:
        by_time.setdefault(row["kickoff"], []).append(row["fixture"])
    linked = []
    missing = []
    for f in expected:
        matches = []
        for slug in by_time.get(store.iso(f["kickoff"]), ()):
            try:
                opportunity.verified_fixture(
                    root, slug, f["fixture_id"], store.iso(now)
                )
                matches.append(slug)
            except (OSError, ValueError, KeyError):
                continue
        if len(matches) == 1:
            linked.append({"fixture_id": f["fixture_id"], "slug": matches[0]})
        else:
            missing.append(
                {
                    "fixture_id": f["fixture_id"],
                    "kickoff": f["kickoff"],
                    "reason": "exchange schedule or participants not uniquely reconciled",
                }
            )
    return {
        "scheduled_football_fixtures": len(expected),
        "independently_matched": len(linked),
        "unmatched": missing,
        "denominator_available": bool(expected),
    }


def _daily_catalog(root, now):
    start = store.iso(now - timedelta(hours=12))
    end = store.iso(now + timedelta(days=7, hours=12))
    result = exchange.catalog_window(
        root, "epl-week", start, end, pages=30, page_size=50
    )
    if not result["page_walk_complete"]:
        raise ValueError("seven-day event catalogue exceeded page budget")
    return {"fixtures": len(result["fixtures_observed"]), "window": result["window"]}


def _service(root, name, now, options):
    if name == "catalog":
        return _daily_catalog(root, now)
    if name == "forecast":
        return runner.run_due(
            root,
            options.get("football_root"),
            evidence_path=options.get("evidence"),
            workers=2,
        )
    if name == "results":
        from .result_feed import sync_season

        return sync_season(root, year_of(now))
    if name == "settlements":
        from .settlement import sync

        return sync(root, limit=50)
    if name == "history":
        return season_backfill.backfill(root, year_of(now), max_pages=24, workers=3)
    raise ValueError("unknown season service")


def _paper(root, fixture):
    from .shadow import run

    output = {}
    for name, fn, account in (
        ("formal", opportunity.process, "main"),
        ("shadow", run, "research-shadow"),
    ):
        try:
            value = fn(root, fixture, account=account)
            output[name] = {
                "account": account,
                "as_of": value.get("as_of"),
                "gaps": len(value.get("gaps") or []),
                "decisions": len(value.get("decisions") or [])
                if isinstance(value.get("decisions"), list)
                else value.get("decisions"),
            }
        except Exception as exc:
            output[name] = {"error": str(exc)}
    return output


def _slot(root, item):
    fixture = item["fixture"]
    if item["stage"] == "paper":
        return _paper(root, fixture)
    if item["stage"] == "metadata":
        report = exchange.discover(root, fixture)
        return {
            "events": report["events"],
            "gaps": report["gaps"],
            "exchange_started": exchange.market_state(root, fixture),
        }
    if exchange.market_state(root, fixture):
        return {"exchange_started": True, "books": 0}
    result = exchange.capture(
        root,
        fixture,
        workers=2,
        discovery=_status_events(root, fixture, store.now_utc()),
        deadline=item["deadline"],
    )
    rows = result["observations"]
    deadline = store.instant(item["deadline"])
    good = [
        r
        for r in rows
        if r.get("observed_at") and store.instant(r["received_at"]) <= deadline
    ]
    return {
        "books": len(rows),
        "fresh_by_deadline": len(good),
        "timely_two_sided": sum(bool(r["bids"] and r["asks"]) for r in good),
        "explicit_pregame": sum(r["pregame"] is True for r in good),
        "failed": sum("error" in r for r in rows),
        "earliest_received": min(
            (r["received_at"] for r in rows if r.get("received_at")), default=None
        ),
        "last_received": max(
            (r["received_at"] for r in rows if r.get("received_at")), default=None
        ),
        "batch_error": result["batch_error"],
        "retry_at": result.get("retry_at"),
        "discovery_gaps": result["discovery_gaps"],
    }


def _alert(db, key, fixture, role, reason):
    now = store.now_utc()
    inserted = db.execute(
        "INSERT OR IGNORE INTO season_alerts VALUES(?,?,?,?,?)",
        (key, fixture, role, now, reason),
    ).rowcount
    if inserted:
        print(
            store.canonical(
                {"season_alert": role, "fixture": fixture, "reason": reason, "at": now}
            ),
            file=sys.stderr,
            flush=True,
        )


def _expire(db, now):
    expired = db.execute(
        "SELECT key,fixture,role,stage,target FROM season_slots WHERE state IN ('pending','running') "
        "AND deadline<? AND (state='pending' OR until<?)",
        (now, now),
    ).fetchall()
    for row in expired:
        db.execute(
            "UPDATE season_slots SET state='missed',owner=NULL,until=NULL WHERE key=?",
            (row["key"],),
        )
        if row["role"] in (
            "decision_book",
            "execution_book",
            "decision",
            "recover",
            "closing_book",
        ):
            _alert(
                db,
                row["key"],
                row["fixture"],
                row["role"],
                "timed observation missing at " + row["target"],
            )


_SERVICES = {
    "catalog": 1800,
    "forecast": 30,
    "results": 900,
    "settlements": 180,
    "history": 120,
}


class Coordinator:
    def __init__(
        self, root, *, workers=6, football_root=None, evidence=None, once=False
    ):
        if not 1 <= workers <= 32:
            raise ValueError("workers must be within 1..32")
        self.root = root
        self.workers = workers
        self.identity = uuid.uuid4().hex
        self.options = {"football_root": football_root, "evidence": evidence}
        self.pools = {
            name: ThreadPoolExecutor(
                max_workers=size, thread_name_prefix="season-" + name
            )
            for name, size in (
                ("book", workers),
                ("metadata", max(2, workers // 2)),
                ("paper", max(2, workers // 2)),
                ("service", 5),
            )
        }
        self.futures = {}
        self.next_plan = 0.0
        self.once = once
        self.services_started = set()

    def _finish(self, future, kind, row):
        clock = store.instant(store.now_utc())
        stamp = store.iso(clock)
        try:
            result = future.result()
            error = None
        except Exception as exc:
            result = {}
            error = str(exc) or type(exc).__name__
        with closing(_db(self.root)) as db, db:
            if kind == "service":
                name = row["service"]
                if error:
                    _alert(
                        db, store.digest([name, stamp[:13], error]), None, name, error
                    )
                summary = (
                    result
                    if name != "forecast"
                    else {
                        k: result[k]
                        for k in (
                            "counts",
                            "next_window_opens",
                            "awaiting_exact_kickoff",
                        )
                        if k in result
                    }
                )
                if name == "forecast":
                    for job in result.get("jobs", []):
                        if job.get("state") in ("missed", "error"):
                            _alert(
                                db,
                                store.digest(
                                    [
                                        "forecast",
                                        job.get("key"),
                                        job.get("state"),
                                        job.get("fixture_id"),
                                        job.get("cutoff"),
                                    ]
                                ),
                                job.get("fixture_id"),
                                job.get("horizon", "forecast"),
                                str(job.get("error") or job["state"]),
                            )
                db.execute(
                    "UPDATE season_services SET next_at=?,owner=NULL,until=NULL,updated=?,result=? WHERE service=? AND owner=?",
                    (
                        store.iso(
                            clock + timedelta(seconds=30 if error else _SERVICES[name])
                        ),
                        stamp,
                        store.canonical({"error": error, "summary": summary}),
                        name,
                        self.identity,
                    ),
                )
                if name == "catalog":
                    self.next_plan = 0.0
                return
            state = "error" if error else "done"
            if result.get("exchange_started"):
                state = "ended"
                _add(
                    db,
                    row["fixture"],
                    row["kickoff"],
                    clock,
                    clock + timedelta(minutes=2),
                    "observe_live",
                    "paper",
                )
                db.execute(
                    "UPDATE season_slots SET state='ended' WHERE fixture=? AND kickoff=? AND state='pending' "
                    "AND stage IN ('book','metadata')",
                    (row["fixture"], row["kickoff"]),
                )
            elif kind == "book" and not result.get("fresh_by_deadline"):
                state = "gap"
            elif kind == "book" and result["fresh_by_deadline"] < result["books"]:
                state = "partial"
            elif kind == "metadata" and (error or result.get("gaps")):
                state = "gap"
            if state in ("error", "gap", "partial"):
                _alert(
                    db,
                    row["key"],
                    row["fixture"],
                    row["role"],
                    error
                    or str(
                        result.get("batch_error")
                        or result.get("gaps")
                        or "no verified timely observation"
                    ),
                )
            db.execute(
                "UPDATE season_slots SET state=?,owner=NULL,until=NULL,detail=? WHERE key=? AND owner=?",
                (
                    state,
                    store.canonical({"error": error, **result}),
                    row["key"],
                    self.identity,
                ),
            )
            # Invoke paper immediately after a genuinely received execution or closing book.
            if (
                kind == "book"
                and state in ("done", "partial")
                and row["role"] in ("decision_book", "execution_book", "closing_book")
            ):
                until = store.instant(row["deadline"])
                if row["role"] == "decision_book":
                    if (store.instant(row["kickoff"]) - until) != timedelta(hours=1):
                        return
                else:
                    until = max(until, clock + timedelta(seconds=50))
                if clock <= until:
                    _add(
                        db,
                        row["fixture"],
                        row["kickoff"],
                        clock,
                        until,
                        "after_" + row["role"],
                        "paper",
                    )
            if kind == "metadata":
                self.next_plan = 0.0

    def tick(self):
        for f, (kind, row) in list(self.futures.items()):
            if f.done():
                self._finish(f, kind, row)
                del self.futures[f]
        now = store.instant(store.now_utc())
        stamp = store.iso(now)
        if time.monotonic() >= self.next_plan:
            _plan(self.root, now)
            self.next_plan = time.monotonic() + 15
        added = 0
        with closing(_db(self.root)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            _expire(db, stamp)
            near = db.execute(
                "SELECT 1 FROM season_slots WHERE state IN ('pending','running') AND stage='book' "
                "AND role IN ('decision_book','execution_book') AND deadline>=? AND target<=? LIMIT 1",
                (stamp, store.iso(now + timedelta(seconds=45))),
            ).fetchone()
            for name in _SERVICES:
                if self.once and name in self.services_started:
                    continue
                if near and name in ("history", "results"):
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO season_services(service,next_at) VALUES(?,'')",
                    (name,),
                )
                row = db.execute(
                    "SELECT * FROM season_services WHERE service=?", (name,)
                ).fetchone()
                if row["next_at"] > stamp or (row["owner"] and row["until"] > stamp):
                    continue
                db.execute(
                    "UPDATE season_services SET owner=?,until=? WHERE service=?",
                    (self.identity, store.iso(now + timedelta(minutes=5)), name),
                )
                fut = self.pools["service"].submit(
                    _service, self.root, name, now, self.options
                )
                self.futures[fut] = ("service", dict(row))
                self.services_started.add(name)
                added += 1
            for stage, size in (
                ("book", self.workers),
                ("metadata", max(2, self.workers // 2)),
                ("paper", max(2, self.workers // 2)),
            ):
                free = size - sum(kind == stage for kind, _ in self.futures.values())
                if free <= 0:
                    continue
                rows = db.execute(
                    "WITH pick AS (SELECT s.*,row_number() OVER(PARTITION BY fixture ORDER BY deadline,key) n FROM season_slots s "
                    "WHERE stage=? AND target<=? AND deadline>=? AND (state='pending' OR (state='running' AND until<?)) "
                    "AND NOT EXISTS(SELECT 1 FROM season_slots a WHERE a.stage=s.stage AND a.fixture=s.fixture AND a.state='running' AND a.until>=?)) "
                    "SELECT * FROM pick WHERE n=1 ORDER BY deadline,target,fixture LIMIT ?",
                    (stage, stamp, stamp, stamp, stamp, free),
                ).fetchall()
                for row in rows:
                    db.execute(
                        "UPDATE season_slots SET state='running',owner=?,until=? WHERE key=?",
                        (
                            self.identity,
                            store.iso(now + timedelta(seconds=90)),
                            row["key"],
                        ),
                    )
                    fut = self.pools[stage].submit(_slot, self.root, dict(row))
                    self.futures[fut] = (stage, dict(row))
                    added += 1
            db.execute(
                "INSERT INTO season_services(service,next_at,updated) VALUES('heartbeat','',?) ON CONFLICT(service) DO UPDATE SET updated=excluded.updated",
                (stamp,),
            )
        return added

    def close(self):
        for pool in self.pools.values():
            pool.shutdown(wait=True)
        for f, (kind, row) in self.futures.items():
            self._finish(f, kind, row)
        self.futures.clear()


def status(root, year=None):
    now = store.instant(store.now_utc())
    with closing(_db(root)) as db:
        states = {
            r["state"]: r["n"]
            for r in db.execute(
                "SELECT state,count(*) n FROM season_slots GROUP BY state"
            )
        }
        heartbeat = db.execute(
            "SELECT updated FROM season_services WHERE service='heartbeat'"
        ).fetchone()
        next_row = db.execute(
            "SELECT min(target) FROM season_slots WHERE state='pending' AND target>=?",
            (store.iso(now),),
        ).fetchone()[0]
        services = [
            dict(r)
            for r in db.execute(
                "SELECT service,next_at,updated,result FROM season_services WHERE service<>'heartbeat'"
            )
        ]
        alerts = [
            dict(r)
            for r in db.execute(
                "SELECT fixture,role,occurred,reason FROM season_alerts ORDER BY occurred DESC LIMIT 12"
            )
        ]
        fixtures = db.execute(
            "SELECT count(*) FROM season_fixtures WHERE kickoff>=? AND kickoff<=?",
            (
                store.iso(now - timedelta(minutes=10)),
                store.iso(now + timedelta(days=7)),
            ),
        ).fetchone()[0]
    for s in services:
        if s["result"]:
            s["result"] = json.loads(s["result"])
    return {
        "observed_at": store.iso(now),
        "heartbeat": heartbeat[0] if heartbeat else None,
        "heartbeat_stale": not heartbeat
        or (now - store.instant(heartbeat[0])).total_seconds() > 90,
        "seven_day_fixtures": fixtures,
        "football_schedule": _football_coverage(root, now),
        "slots": states,
        "next_capture_or_action": next_row,
        "services": services,
        "recent_alerts": alerts,
        "backfill": season_backfill.status(root, year or year_of(now)),
    }


def run(root, *, repeat=False, workers=6, football_root=None, evidence=None):
    agent = Coordinator(
        root,
        workers=workers,
        football_root=football_root,
        evidence=evidence,
        once=not repeat,
    )
    try:
        while True:
            try:
                added = agent.tick()
                if not repeat and not agent.futures and not added:
                    break
                if agent.futures:
                    wait(
                        list(agent.futures),
                        timeout=0.20 if repeat else 1,
                        return_when=FIRST_COMPLETED,
                    )
                elif repeat:
                    time.sleep(0.25)
            except Exception as exc:
                if not repeat:
                    raise
                print(
                    store.canonical({"season_error": str(exc)}),
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(1)
    finally:
        agent.close()
    return status(root)


def register(subparsers):
    commands = subparsers.add_parser(
        "season", help="全英超采集、比赛日调度及历史回补"
    ).add_subparsers(dest="season_action", required=True)
    p = commands.add_parser("run", help="协调未来七天全部已挂牌英超比赛")
    p.add_argument("--repeat", action="store_true")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--football-root", type=Path)
    p.add_argument("--evidence", type=Path)
    p.set_defaults(
        func=lambda a: run(
            a.data_dir,
            repeat=a.repeat,
            workers=a.workers,
            football_root=a.football_root,
            evidence=a.evidence,
        )
    )
    p = commands.add_parser("status")
    p.add_argument("--year", type=int)
    p.set_defaults(func=lambda a: status(a.data_dir, a.year))
    season_backfill.add_parser(commands)
