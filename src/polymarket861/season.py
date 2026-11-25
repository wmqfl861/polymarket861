"""One durable EPL matchday coordinator for collection, forecasts and paper accounts."""

import json
import sys
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from .season_alerts import emit as _alert
from . import market_archive as exchange
from . import (
    dbscope,
    opportunity,
    runner,
    season_alerts,
    season_backfill,
    season_health,
    store,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS season_fixtures(
 fixture TEXT PRIMARY KEY,kickoff TEXT NOT NULL,plan TEXT NOT NULL,first_seen TEXT NOT NULL,updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS season_slots(
 key TEXT PRIMARY KEY,fixture TEXT NOT NULL,kickoff TEXT NOT NULL,target TEXT NOT NULL,deadline TEXT NOT NULL,
 role TEXT NOT NULL,stage TEXT NOT NULL,state TEXT NOT NULL,owner TEXT,until TEXT,detail TEXT);
CREATE INDEX IF NOT EXISTS season_slot_due ON season_slots(stage,state,target,deadline);
CREATE INDEX IF NOT EXISTS season_slot_open_deadline ON season_slots(deadline) WHERE state IN ('pending','running');
CREATE INDEX IF NOT EXISTS season_slot_revision ON season_slots(fixture,kickoff,state);
CREATE INDEX IF NOT EXISTS season_paper_detail ON season_slots(target DESC) WHERE stage='paper' AND detail IS NOT NULL;
CREATE INDEX IF NOT EXISTS season_main_kickoff ON ma_events(julianday(kickoff),slug,received_at) WHERE slug=fixture;
CREATE TABLE IF NOT EXISTS season_services(
 service TEXT PRIMARY KEY,next_at TEXT NOT NULL,owner TEXT,until TEXT,updated TEXT,result TEXT);
CREATE TABLE IF NOT EXISTS season_retries(slot TEXT PRIMARY KEY,attempts INTEGER NOT NULL,not_before TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS season_alerts(
 key TEXT PRIMARY KEY,fixture TEXT,role TEXT,occurred TEXT,reason TEXT);
CREATE INDEX IF NOT EXISTS season_alert_time ON season_alerts(occurred DESC);
CREATE TABLE IF NOT EXISTS season_alert_causes(
 cause TEXT PRIMARY KEY,key TEXT NOT NULL,occurred TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS season_alert_delivery(
 key TEXT PRIMARY KEY,available TEXT NOT NULL,attempts INTEGER NOT NULL,delivered TEXT,error TEXT);
CREATE INDEX IF NOT EXISTS season_alert_available ON season_alert_delivery(available) WHERE delivered IS NULL;
"""


def _db(root):
    db = exchange.database(root)
    dbscope.prepare(db, "season-coordinator", _SCHEMA)
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

    return key


def _schedule_origin(db, root, fixture, kick, at):
    """Earliest authentic main-event receipt in the current uninterrupted kickoff revision."""
    from .archive_proof import original_receipt

    records = db.execute(
        "SELECT received_at,sha,event_id,kickoff FROM ma_events WHERE slug=? "
        "AND received_at<=? ORDER BY received_at,sha",
        (fixture, store.iso(at)),
    ).fetchall()

    def matches(record):
        try:
            return store.instant(record["kickoff"]) == kick
        except (ValueError, TypeError):
            return False

    def genuine(record):
        try:
            reply, op, selector, _ = original_receipt(
                db, root, record["sha"], record["received_at"], {"event", "catalog"}
            )
            body = json.loads(reply.body)
            events = (
                [body]
                if op == "event" and selector == fixture and isinstance(body, dict)
                else body
                if op == "catalog" and isinstance(body, list)
                else []
            )
            hits = [
                e
                for e in events
                if isinstance(e, dict)
                and e.get("slug") == fixture
                and str(e.get("id")) == record["event_id"]
            ]
            return len(hits) == 1 and store.instant(
                hits[0]["startTime"]
            ) == store.instant(record["kickoff"])
        except (KeyError, ValueError, TypeError, OSError):
            return False

    boundary = next(
        (r["received_at"] for r in reversed(records) if not matches(r) and genuine(r)),
        None,
    )
    for r in records:
        if (
            (boundary is None or r["received_at"] > boundary)
            and matches(r)
            and genuine(r)
        ):
            return store.instant(r["received_at"]), r["sha"], boundary is not None
    return None, None, boundary is not None


def _critical_windows(
    db, fixture, kick, known_delays, now, schedule_seen, first_planned, revised, source
):
    """Record fixed measurement opportunities even if discovery happened after a cutoff."""

    def record(target, deadline, role, stage, horizon):
        key = _add(db, fixture, store.iso(kick), target, deadline, role, stage)
        if deadline >= now:
            return
        if schedule_seen is None:
            reason = "schedule_observation_unverified"
        elif schedule_seen > deadline:
            reason = "reschedule_seen_late" if revised else "first_seen_late"
        else:
            reason = "collector_window_missed"
        detail = {
            "reason": reason,
            "horizon": horizon,
            "scheduled_at": store.iso(target),
            "deadline": store.iso(deadline),
            "schedule_seen_at": store.iso(schedule_seen) if schedule_seen else None,
            "first_planned_at": store.iso(first_planned),
            "attribution_version": 3,
            "checked_at": store.iso(now),
            "schedule_source_sha256": source,
        }
        changed = db.execute(
            "UPDATE season_slots SET state='missed',owner=NULL,until=NULL,detail=? "
            "WHERE key=? AND state IN ('pending','superseded')",
            (store.canonical(detail), key),
        ).rowcount
        if not changed:
            prior = db.execute(
                "SELECT detail FROM season_slots WHERE key=? AND state='missed'", (key,)
            ).fetchone()
            earlier = json.loads(prior[0]) if prior and prior[0] else {}
            if earlier.get("reason") in (
                "collector_window_missed",
                "first_seen_late",
                "reschedule_seen_late",
            ) and not earlier.get("attribution_version"):
                # Upgrade prior local diagnoses without replacing receipts or issuing a duplicate alert.
                detail["previous_attribution"] = earlier["reason"]
                db.execute(
                    "UPDATE season_slots SET detail=? WHERE key=?",
                    (store.canonical(detail), key),
                )
                db.execute(
                    "UPDATE season_alerts SET reason=? WHERE key=?",
                    (f"{reason} {horizon} deadline {store.iso(deadline)}", key),
                )
        if changed:
            _alert(
                db,
                key,
                fixture,
                role,
                f"{reason} {horizon} deadline {store.iso(deadline)}",
            )

    for hours, horizon in ((24, "T-24h"), (1, "T-60m")):
        cutoff = kick - timedelta(hours=hours)
        record(cutoff - timedelta(seconds=25), cutoff, "decision_book", "book", horizon)
        if hours == 1:
            for seconds in (15, 4):
                record(
                    cutoff - timedelta(seconds=seconds),
                    cutoff,
                    "decision",
                    "paper",
                    horizon,
                )
        for delay in known_delays:
            target = cutoff + timedelta(seconds=delay)
            record(
                target, target + timedelta(seconds=5), "execution_book", "book", horizon
            )
            if hours == 1:
                record(
                    target + timedelta(seconds=6),
                    target + timedelta(seconds=70),
                    "recover",
                    "paper",
                    horizon,
                )


def _plan(root, now):
    lower = store.iso(now - timedelta(minutes=10))
    upper = store.iso(now + timedelta(days=7))
    with closing(_db(root)) as db, db:
        # Discover by kickoff, then look up the latest receipt for each candidate. A
        # later postponement must beat an older receipt inside the seven-day window.
        # Recheck each previously tracked fixture with one primary-key seek, including
        # fixtures moved out of range. The season's old per-minute receipts stay cold.
        current = db.execute(
            "WITH candidates(slug) AS (SELECT slug FROM ma_events WHERE slug=fixture "
            "AND julianday(kickoff) BETWEEN julianday(?) AND julianday(?) AND received_at<=? "
            "UNION SELECT fixture FROM season_fixtures) "
            "SELECT e.fixture,e.kickoff,e.received_at,e.sha,s.plan,s.first_seen,s.kickoff AS prior "
            "FROM candidates c JOIN ma_events e ON e.rowid=(SELECT m.rowid FROM ma_events m "
            "WHERE m.slug=c.slug AND m.received_at<=? ORDER BY received_at DESC,sha DESC LIMIT 1) "
            "LEFT JOIN season_fixtures s ON s.fixture=c.slug WHERE e.slug=e.fixture AND "
            "(julianday(e.kickoff) BETWEEN julianday(?) AND julianday(?) OR "
            "(s.fixture IS NOT NULL AND julianday(s.kickoff)<>julianday(e.kickoff)))",
            (lower, upper, store.iso(now), store.iso(now), lower, upper),
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
            old = json.loads(row["plan"]) if row["plan"] else {}
            revised = bool(row["prior"] and row["prior"] != kickoff)
            planned = (
                now
                if revised or not row["first_seen"]
                else store.instant(
                    old.get("first_planned_at")
                    or old.get("schedule_seen_at")
                    or row["first_seen"]
                )
            )
            if not lower <= kickoff <= upper:
                if revised:
                    since, origin, _ = _schedule_origin(db, root, tag, kick, now)
                    db.execute(
                        "UPDATE season_fixtures SET kickoff=?,plan=?,updated=? WHERE fixture=?",
                        (
                            kickoff,
                            store.canonical(
                                {
                                    "outside_window": True,
                                    "schedule_seen_at": store.iso(since)
                                    if since
                                    else None,
                                    "schedule_source_sha256": origin,
                                    "first_planned_at": store.iso(now),
                                    "attribution_version": 3,
                                }
                            ),
                            store.iso(now),
                            tag,
                        ),
                    )
                continue
            if revised:
                # A schedule can return to a previously archived kickoff. Preserve receipts
                # and terminal gaps, and reinstate only windows that can still be observed.
                db.execute(
                    "DELETE FROM season_retries WHERE slot IN (SELECT key FROM season_slots "
                    "WHERE fixture=? AND kickoff=? AND state='superseded' AND deadline>=?)",
                    (tag, kickoff, store.iso(now)),
                )
                db.execute(
                    "UPDATE season_slots SET state='pending',owner=NULL,until=NULL,detail=NULL "
                    "WHERE fixture=? AND kickoff=? AND state='superseded' AND deadline>=?",
                    (tag, kickoff, store.iso(now)),
                )

            latest = db.execute(
                "SELECT received_at,sha FROM ma_events WHERE fixture=? AND received_at<=? "
                "ORDER BY received_at DESC,sha DESC LIMIT 1",
                (tag, store.iso(now)),
            ).fetchone()
            version = store.digest(
                [
                    "critical-windows-v3/hourly-far-books",
                    kickoff,
                    latest["received_at"],
                    latest["sha"],
                ]
            )
            # The main receipt changes far more often than the grid. Skip recalculating an
            # unchanged schedule in the seconds leading up to a capture.
            if row["prior"] == kickoff and old.get("version") == version:
                continue
            seen, source, has_prior = _schedule_origin(db, root, tag, kick, now)
            seen_text = store.iso(seen) if seen else None
            if (
                old.get("attribution_version") == 3
                and old.get("schedule_seen_at") != seen_text
            ):
                planned = now
            active = db.execute(
                "WITH latest AS (SELECT *,row_number() OVER(PARTITION BY slug ORDER BY received_at DESC,sha DESC) n "
                "FROM ma_events WHERE fixture=? AND received_at<=?) SELECT DISTINCT json_extract(c.payload,'$.seconds_delay') delay "
                "FROM latest e JOIN ma_contracts c ON c.event_slug=e.slug AND c.received_at=e.received_at AND c.sha=e.sha "
                "WHERE n=1 AND json_extract(c.payload,'$.mapping_error') IS NULL",
                (tag, store.iso(now)),
            )
            known_delays = sorted(
                {max(5, int(r["delay"])) for r in active if type(r["delay"]) is int}
            )
            delays = known_delays or [5]
            if revised or old.get("far_book_minutes") != 60:
                # Preserve real receipts, leases and terminal gaps across the cadence upgrade.
                db.execute(
                    "UPDATE season_slots SET state='superseded',detail=? WHERE fixture=? AND kickoff=? "
                    "AND stage='book' AND role='periodic' AND state='pending' AND target>=? "
                    "AND unixepoch(kickoff)-unixepoch(target)>172800 "
                    "AND (unixepoch(kickoff)-unixepoch(target))%3600<>0",
                    (
                        store.canonical({"reason": "far_book_hourly"}),
                        tag,
                        kickoff,
                        store.iso(now),
                    ),
                )
            db.execute(
                "INSERT INTO season_fixtures VALUES(?,?,?,?,?) ON CONFLICT(fixture) DO UPDATE SET kickoff=excluded.kickoff,"
                "plan=excluded.plan,updated=excluded.updated",
                (
                    tag,
                    kickoff,
                    store.canonical(
                        {
                            "delays": delays,
                            "known_delays": known_delays,
                            "schedule_seen_at": seen_text,
                            "schedule_source_sha256": source,
                            "first_planned_at": store.iso(planned),
                            "attribution_version": 3,
                            "version": version,
                            "far_book_minutes": 60,
                        }
                    ),
                    store.iso(now),
                    store.iso(now),
                ),
            )
            if (
                row["prior"] == kickoff
                and old.get("attribution_version") == 3
                and old.get("schedule_seen_at") == seen_text
                and old.get("known_delays") == known_delays
            ):
                continue
            _critical_windows(
                db,
                tag,
                kick,
                known_delays,
                now,
                seen,
                planned,
                revised or has_prior,
                source,
            )

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
                if role in ("periodic", "closing_book"):
                    deadline = target + timedelta(
                        seconds=12 if role == "closing_book" else 45
                    )
                    before = int((kick - target).total_seconds())
                    if (
                        role == "closing_book"
                        or before <= 48 * 3600
                        or before % 3600 == 0
                    ):
                        _add(db, tag, kickoff, target, deadline, role, "book")
                    if (
                        role == "closing_book"
                        or int((kick - target).total_seconds()) % 900 == 0
                    ):
                        _add(db, tag, kickoff, target, deadline, "state", "metadata")
                elif role == "execution_book" and not known_delays:
                    # Sampling may continue with the monitor's default. Absent exchange delay
                    # evidence cannot create a missing executable quote in the denominator.
                    _add(
                        db,
                        tag,
                        kickoff,
                        target,
                        target + timedelta(seconds=5),
                        "execution_probe",
                        "book",
                    )
                cursor = target


def _status_events(root, fixture, at):
    events = []
    with closing(exchange.database(root)) as db:
        for slug in (fixture, fixture + "-more-markets"):
            row = db.execute(
                "SELECT slug FROM ma_events WHERE slug=? AND fixture=? AND received_at<=? "
                "ORDER BY received_at DESC,sha DESC LIMIT 1",
                (slug, fixture, at),
            ).fetchone()
            if row:
                events.append(row["slug"])
    return {
        "events": events,
        "gaps": []
        if len(events) == 2
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
    from . import schedule_consensus

    schedule_consensus.sync(root, year_of(now), fetch=False)
    return {"fixtures": len(result["fixtures_observed"]), "window": result["window"]}


def sync_news(root, *, history_root=None, **options):
    """Reconcile new source observations, then process pending pregame news changes."""
    from . import news, news_events

    result = news.sync(root, **options)
    try:
        updates = news_events.refresh(root, limit=2, history_root=history_root)
    except (OSError, ValueError, RuntimeError) as exc:
        updates = {"state": "error", "reason": str(exc), "items": []}
    return result | {"prediction_updates": updates}


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
    if name == "news":
        return sync_news(root, history_root=options.get("football_root"))
    if name == "settlements":
        from .settlement import sync

        return sync(root, limit=50)
    if name == "history":
        return season_backfill.backfill(
            root, year_of(now), max_pages=96, workers=4, page_size=1000, max_seconds=85
        )
    raise ValueError("unknown season service")


def _paper(root, fixture):
    from .shadow import run

    output = {}
    for name, fn, account, action_key in (
        ("formal", opportunity.process, "main", "decisions"),
        ("shadow", run, "research-shadow", "actions"),
    ):
        started = store.now_utc()
        try:
            output[name] = season_health.account_summary(
                account, fn(root, fixture, account=account), action_key, started
            )
        except Exception as exc:
            output[name] = {
                "account": account,
                "as_of": started,
                "state": "error",
                "error": str(exc) or type(exc).__name__,
                "error_type": type(exc).__name__,
            }
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
    discovery = _status_events(root, fixture, store.now_utc())
    if item["role"] == "execution_book":
        result = exchange.capture_execution(
            root,
            fixture,
            target=item["target"],
            deadline=item["deadline"],
            kickoff=item["kickoff"],
            discovery=discovery,
            research=item.get("execution_research", False),
        )
    else:
        result = exchange.capture(
            root,
            fixture,
            workers=2,
            discovery=discovery,
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
        **{
            k: result[k]
            for k in (
                "scope",
                "requested_tokens",
                "no_frozen_intents",
                "attempt_latency_ms",
                "hedge_started",
                "research_submitted",
                "non_frozen_count",
            )
            if k in result
        },
    }


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
    "news": 60,
}


class Coordinator:
    def __init__(
        self,
        root,
        *,
        workers=6,
        football_root=None,
        evidence=None,
        once=False,
        alert_command=None,
        execution_research=False,
    ):
        if not 1 <= workers <= 32:
            raise ValueError("workers must be within 1..32")
        self.root = root
        self.workers = workers
        self.identity = uuid.uuid4().hex
        self.options = {"football_root": football_root, "evidence": evidence}
        self.execution_research = execution_research
        self.alert_argv = season_alerts.command_args(alert_command)
        self.delivery_pool = (
            ThreadPoolExecutor(max_workers=1) if self.alert_argv else None
        )
        self.delivery = None
        self.next_delivery = 0.0
        self.pools = {
            name: ThreadPoolExecutor(
                max_workers=size, thread_name_prefix="season-" + name
            )
            for name, size in (
                ("book", workers),
                ("metadata", max(2, workers // 2)),
                ("paper", max(2, workers // 2)),
                ("service", 5),
                ("news", 1),
            )
        }
        self.futures = {}
        self.next_plan = 0.0
        self.once = once
        self.services_started = set()
        self.first_tick = True

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
                db.execute("BEGIN IMMEDIATE")
                if not db.execute(
                    "SELECT 1 FROM season_services WHERE service=? AND owner=?",
                    (name, self.identity),
                ).fetchone():
                    return
                problems = list(season_health.service_problems(name, result))
                for cause, reason in problems:
                    season_alerts.report_cause(db, None, name, cause, reason, stamp)
                if error:
                    season_alerts.report_cause(
                        db, None, name, "service_exception", error, stamp
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
                        store.canonical(
                            {
                                "error": error,
                                "health": "error"
                                if error
                                else "degraded"
                                if problems
                                else "ok",
                                "summary": summary,
                            }
                        ),
                        name,
                        self.identity,
                    ),
                )
                if name == "catalog":
                    self.next_plan = 0.0
                return
            db.execute("BEGIN IMMEDIATE")
            owned = db.execute(
                "SELECT 1 FROM season_slots WHERE key=? AND owner=? AND state='running'",
                (row["key"], self.identity),
            ).fetchone()
            if not owned:
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
            elif kind == "paper" and not error:
                state = season_health.paper_state(db, result, row, stamp)
            elif kind == "book" and result.get("no_frozen_intents"):
                state = "done"
            elif kind == "book" and not result.get("fresh_by_deadline"):
                state = "gap"
            elif kind == "book" and result["fresh_by_deadline"] < result["books"]:
                state = "partial"
            elif kind == "metadata" and (error or result.get("gaps")):
                state = "gap"
            if kind in ("book", "metadata") and state == "gap":
                retry_at = result.get("retry_at") or max(
                    (
                        g.get("retry_at")
                        for g in result.get("gaps", [])
                        if g.get("retry_at")
                    ),
                    default=None,
                )
                if retry_at and clock < store.instant(row["deadline"]):
                    retry = max(
                        store.instant(retry_at), clock + timedelta(milliseconds=250)
                    )
                    old = db.execute(
                        "SELECT attempts FROM season_retries WHERE slot=?",
                        (row["key"],),
                    ).fetchone()
                    if retry < store.instant(row["deadline"]) and (
                        not old or old[0] < 2
                    ):
                        db.execute(
                            "INSERT INTO season_retries VALUES(?,1,?) ON CONFLICT(slot) DO UPDATE SET "
                            "attempts=attempts+1,not_before=excluded.not_before",
                            (row["key"], store.iso(retry)),
                        )
                        state = "pending"
            if kind == "paper" and error:
                season_alerts.report_cause(
                    db, row["fixture"], "paper", "task_exception", error, stamp
                )
            elif state in ("error", "gap", "partial") and kind != "paper":
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
            detail_error = error
            if kind == "paper" and state == "error" and not detail_error:
                detail_error = " | ".join(
                    f"{name}: {result[name]['error']}"
                    for name in ("formal", "shadow")
                    if result[name].get("error")
                )
            db.execute(
                "UPDATE season_slots SET state=?,owner=NULL,until=NULL,detail=? WHERE key=? AND owner=?",
                (
                    state,
                    store.canonical({"error": detail_error, **result}),
                    row["key"],
                    self.identity,
                ),
            )
            # Invoke paper immediately after a genuinely received execution or closing book.
            if (
                kind == "book"
                and state in ("done", "partial")
                and not result.get("no_frozen_intents")
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
            if self.first_tick:
                previous = db.execute(
                    "SELECT updated FROM season_services WHERE service='heartbeat'"
                ).fetchone()
                if (
                    previous
                    and previous[0]
                    and (now - store.instant(previous[0])).total_seconds() > 90
                ):
                    for f in db.execute(
                        "SELECT fixture,kickoff FROM season_fixtures WHERE kickoff BETWEEN ? AND ? "
                        "AND first_seen<?",
                        (
                            store.iso(now - timedelta(minutes=10)),
                            store.iso(now + timedelta(days=7)),
                            store.iso(now - timedelta(seconds=90)),
                        ),
                    ):
                        _add(
                            db,
                            f["fixture"],
                            f["kickoff"],
                            now,
                            now + timedelta(seconds=60),
                            "resume",
                            "book",
                        )
                    _alert(
                        db,
                        store.digest(["season_restart", previous[0]]),
                        None,
                        "heartbeat_gap",
                        "resumed after collector heartbeat at " + previous[0],
                    )
                self.first_tick = False
            next_critical = db.execute(
                "SELECT min(target) FROM season_slots WHERE state IN ('pending','running') AND stage='book' "
                "AND role IN ('decision_book','execution_book') AND deadline>=? AND target<=?",
                (stamp, store.iso(now + timedelta(seconds=100))),
            ).fetchone()[0]
            for name in _SERVICES:
                if self.once and name in self.services_started:
                    continue
                if next_critical and (
                    name in ("history", "news")
                    or (
                        name == "results"
                        and next_critical <= store.iso(now + timedelta(seconds=45))
                    )
                ):
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
                pool = self.pools["news" if name == "news" else "service"]
                fut = pool.submit(
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
                    "AND NOT EXISTS(SELECT 1 FROM season_retries r WHERE r.slot=s.key AND r.not_before>?) "
                    "AND NOT EXISTS(SELECT 1 FROM season_slots a WHERE a.stage=s.stage AND a.fixture=s.fixture AND a.state='running' AND a.until>=?)) "
                    "SELECT * FROM pick WHERE n=1 ORDER BY deadline,target,fixture LIMIT ?",
                    (stage, stamp, stamp, stamp, stamp, stamp, free),
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
                    item = dict(row)
                    if self.execution_research and item["role"] == "execution_book":
                        item["execution_research"] = True
                    fut = self.pools[stage].submit(_slot, self.root, item)
                    self.futures[fut] = (stage, dict(row))
                    added += 1
            db.execute(
                "INSERT INTO season_services(service,next_at,updated) VALUES('heartbeat','',?) ON CONFLICT(service) DO UPDATE SET updated=excluded.updated",
                (stamp,),
            )
        if self.delivery_pool and time.monotonic() >= self.next_delivery:
            if self.delivery is None or self.delivery.done():
                if self.delivery is not None:
                    self.delivery.result()
                self.delivery = self.delivery_pool.submit(
                    season_alerts.deliver, self.root, self.alert_argv
                )
            self.next_delivery = time.monotonic() + 1
        return added

    def close(self):
        for pool in self.pools.values():
            pool.shutdown(wait=True)
        for f, (kind, row) in self.futures.items():
            self._finish(f, kind, row)
        self.futures.clear()
        if self.delivery_pool:
            self.delivery_pool.shutdown(wait=True)
            if self.once:
                # A fast one-shot run may finish before the next delivery poll. Flush
                # newly committed alerts only after all acquisition workers have stopped.
                end = time.monotonic() + season_alerts.TIMEOUT
                while time.monotonic() < end:
                    sent = season_alerts.deliver(self.root, self.alert_argv)
                    if not sent or sent["error"]:
                        break


def status(root, year=None):
    from . import result_freshness

    now = store.instant(store.now_utc())
    with closing(_db(root)) as db:
        states = {
            r["state"]: r["n"]
            for r in db.execute(
                "SELECT state,count(*) n FROM season_slots GROUP BY state"
            )
        }
        weekly = [
            dict(r)
            for r in db.execute(
                "SELECT s.role,s.state,count(*) AS slots FROM season_slots s JOIN season_fixtures f "
                "ON f.fixture=s.fixture AND f.kickoff=s.kickoff WHERE s.deadline>=? AND s.deadline<=? "
                "AND s.role IN ('decision_book','execution_book','decision','recover') GROUP BY s.role,s.state",
                (store.iso(now - timedelta(days=7)), store.iso(now)),
            )
        ]
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
        paper = [
            dict(r) | {"detail": json.loads(r["detail"])}
            for r in db.execute(
                "SELECT fixture,role,target,state,detail FROM season_slots WHERE stage='paper' "
                "AND detail IS NOT NULL ORDER BY target DESC LIMIT 8"
            )
        ]
        delivery = {
            "undelivered_last_hour": db.execute(
                "SELECT count(*) FROM season_alerts a JOIN season_alert_delivery d USING(key) "
                "WHERE a.occurred>=? AND d.delivered IS NULL",
                (store.iso(now - timedelta(hours=1)),),
            ).fetchone()[0],
            "recent_attempts": [
                dict(r)
                for r in db.execute(
                    "SELECT a.role,a.occurred,d.attempts,d.delivered,d.error FROM season_alerts a "
                    "JOIN season_alert_delivery d USING(key) WHERE d.attempts>0 ORDER BY a.occurred DESC LIMIT 5"
                )
            ],
        }
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
        "data_dir": str(Path(root).expanduser().resolve()),
        "observed_at": store.iso(now),
        "heartbeat": heartbeat[0] if heartbeat else None,
        "heartbeat_stale": not heartbeat
        or (now - store.instant(heartbeat[0])).total_seconds() > 90,
        "seven_day_fixtures": fixtures,
        "football_schedule": _football_coverage(root, now),
        "slots": states,
        "critical_windows_last_seven_days": weekly,
        "next_capture_or_action": next_row,
        "services": services,
        "recent_alerts": alerts,
        "recent_paper_slots": paper,
        "alert_delivery": delivery,
        "result_freshness": result_freshness.status(
            root, year or year_of(now), at=store.iso(now)
        ),
        "backfill": season_backfill.status(root, year or year_of(now)),
    }


def run(
    root,
    *,
    repeat=False,
    workers=6,
    football_root=None,
    evidence=None,
    alert_command=None,
    execution_research=False,
):
    agent = Coordinator(
        root,
        workers=workers,
        football_root=football_root,
        evidence=evidence,
        once=not repeat,
        alert_command=alert_command,
        execution_research=execution_research,
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
    p.add_argument(
        "--alert-command", help="告警处理程序及固定参数；每条 JSON 从标准输入读取"
    )
    p.add_argument(
        "--execution-research",
        action="store_true",
        help="执行簿时段并行另采非冻结方向的研究行情",
    )
    p.set_defaults(
        func=lambda a: run(
            a.data_dir,
            repeat=a.repeat,
            workers=a.workers,
            football_root=a.football_root,
            evidence=a.evidence,
            alert_command=a.alert_command,
            execution_research=a.execution_research,
        )
    )
    p = commands.add_parser("status")
    p.add_argument("--year", type=int)
    p.set_defaults(func=lambda a: status(a.data_dir, a.year))
    p = commands.add_parser("watchdog", help="独立检查最后一次主循环心跳")
    p.add_argument("--alert-command", help="告警处理程序及固定参数")
    p.set_defaults(
        func=lambda a: season_alerts.heartbeat(
            a.data_dir, alert_command=a.alert_command
        )
    )
    season_backfill.add_parser(commands)
