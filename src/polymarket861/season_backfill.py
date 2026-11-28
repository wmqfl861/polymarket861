"""Resumable season-wide Polymarket trades and individually measured token price curves."""

import hashlib
import json
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta

from . import market_archive as exchange
from . import market_provider as provider
from . import dbscope, store

GRAINS = (60, 300, 1800, 10800, 43200)
# Data API v2 publishes minimum retention, not exact expiry. Always probe an older
# grain before classifying it; prioritize the next still-guaranteed tier of a slice.
_RETENTION = (7 * 86400, 60 * 86400, 90 * 86400)
_EXPIRY = """CASE WHEN kind='prices' THEN CASE
 WHEN grain<=0 AND end+:one>=:now THEN start+:one
 WHEN grain<=1 AND end+:five>=:now THEN start+:five
 WHEN grain<=2 AND end+:thirty>=:now THEN start+:thirty END END"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS season_history_catalog(year INTEGER PRIMARY KEY,cursor INTEGER,done INTEGER,updated TEXT);
CREATE TABLE IF NOT EXISTS season_history_seed(year INTEGER,slug TEXT,source_sha TEXT,PRIMARY KEY(year,slug));
CREATE TABLE IF NOT EXISTS season_history_leases(job TEXT PRIMARY KEY,claim TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS season_history_params(job TEXT PRIMARY KEY,page_size INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS season_history_grains(job TEXT,grain INTEGER,summary TEXT,PRIMARY KEY(job,grain));
CREATE TABLE IF NOT EXISTS season_history_jobs(
 key TEXT PRIMARY KEY,year INTEGER,fixture TEXT,kind TEXT,selector TEXT,start INTEGER,end INTEGER,
 grain INTEGER NOT NULL, state TEXT NOT NULL, next_at TEXT NOT NULL, last_run TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS season_history_due ON season_history_jobs(year,state,next_at,last_run);
"""


def _db(root):
    db = exchange.database(root)
    dbscope.prepare(db, "season-backfill", _SCHEMA)
    return db


def _seed_jobs(year, row, event, now, first):
    fixture = row["fixture"]

    def job(kind, selector, begin=0, end=0):
        key = store.digest([year, kind, selector, begin, end])
        return key, fixture, kind, selector, begin, end

    sibling = fixture + "-more-markets" if row["slug"] == fixture else fixture
    yield job("event", sibling)
    yield job("trades", row["event_id"])
    native = {
        x["slug"]: x for x in event.get("markets") or []
        if isinstance(x, dict) and x.get("slug")
    }
    for m in provider.normalized_markets(event):
        if m.get("mapping_error"):
            continue
        src = native[m["slug"]]
        born = (src.get("startDate") or src.get("createdAt")
                or event.get("startDate") or first + "T00:00:00Z")
        closed = src.get("closedTime") or event.get("closedTime")
        # An open market's latest checkpoint advances in whole days from kickoff.
        kick = provider.moment(row["kickoff"])
        end = provider.moment(closed) if closed else kick + timedelta(days=max(0, (now - kick).days))
        finish = int(min(now, end).timestamp())
        begin = int(provider.moment(born).timestamp())
        for tok in m["tokens"]:
            lower = begin
            while lower < finish:
                upper = min(finish, ((lower // 86400) + 1) * 86400)
                yield job("prices", tok["token_id"], lower, upper)
                lower = upper


def _seed(root, year, now):
    """Read existing Gamma originals once per revision, including every auxiliary event."""
    first, last = f"{year}-07-01", f"{year + 1}-07-01"
    with closing(_db(root)) as db:
        rows = db.execute(
            "WITH latest AS (SELECT *,row_number() OVER(PARTITION BY slug ORDER BY received_at DESC,sha DESC) n "
            "FROM ma_events WHERE received_at<=?) SELECT e.*,s.source_sha FROM latest e LEFT JOIN season_history_seed s "
            "ON s.year=? AND s.slug=e.slug WHERE e.n=1 AND substr(e.fixture,-10)>=? AND substr(e.fixture,-10)<? "
            "AND julianday(e.kickoff)<julianday(?) AND (s.source_sha IS NULL OR s.source_sha<>e.sha)",
            (store.iso(now), year, first, last, store.iso(now)),
        ).fetchall()
    by_blob = defaultdict(list)
    for row in rows:
        by_blob[row["sha"]].append(row)
    for sha, rows in by_blob.items():
        raw = store.read_blob(root, sha)
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError("catalogue archive digest mismatch")
        body = json.loads(raw)
        events = {
            v["slug"]: v
            for v in (body if isinstance(body, list) else [body])
            if isinstance(v, dict) and v.get("slug")
        }
        with closing(_db(root)) as db, db:
            # Temporary rows are private to this connection. Preparing a whole
            # catalogue revision, including existing-key lookups, leaves WAL writers free.
            db.execute("CREATE TEMP TABLE seed_jobs(key,fixture,kind,selector,start,end)")
            db.execute("CREATE TEMP TABLE seed_slugs(slug PRIMARY KEY,received_at)")
            for row in rows:
                event = events.get(row["slug"])
                if not event or str(event.get("id")) != row["event_id"]:
                    raise ValueError(
                        "event catalogue index disagrees with the raw payload"
                    )

                db.executemany(
                    "INSERT INTO temp.seed_jobs VALUES(?,?,?,?,?,?)",
                    _seed_jobs(year, row, event, now, first),
                )
                db.execute("INSERT INTO temp.seed_slugs VALUES(?,?)", (row["slug"], row["received_at"]))
            db.execute(
                "DELETE FROM temp.seed_jobs WHERE EXISTS "
                "(SELECT 1 FROM season_history_jobs j WHERE j.key=seed_jobs.key)"
            )
            db.commit()

            db.execute("BEGIN IMMEDIATE")
            # A concurrent seeder may have published overlapping slices since the
            # read phase. Ignore only duplicate keys, retaining any additional dates.
            db.execute(
                "INSERT OR IGNORE INTO season_history_jobs "
                "SELECT j.key,?,j.fixture,j.kind,j.selector,j.start,j.end,0,'pending','','',NULL "
                "FROM temp.seed_jobs j "
                "WHERE j.kind<>'event' OR NOT EXISTS(SELECT 1 FROM ma_events e WHERE e.slug=j.selector) "
                "ORDER BY j.rowid", (year,),
            )
            # A slower seeder may still contribute jobs from its verified original,
            # but cannot rewind a slug already seeded from a later receipt.
            db.execute(
                "INSERT INTO season_history_seed SELECT ?,slug,? FROM temp.seed_slugs WHERE true "
                "ON CONFLICT(year,slug) DO UPDATE SET source_sha=excluded.source_sha "
                "WHERE season_history_seed.source_sha IS NOT excluded.source_sha AND NOT EXISTS ("
                "SELECT 1 FROM ma_events e JOIN temp.seed_slugs s ON s.slug=e.slug "
                "WHERE e.slug=excluded.slug AND e.sha=season_history_seed.source_sha "
                "AND (e.received_at,e.sha)>(s.received_at,excluded.source_sha))", (year, sha),
            )


def _catalog(root, year, now):
    with closing(_db(root)) as db:
        old = db.execute(
            "SELECT * FROM season_history_catalog WHERE year=?", (year,)
        ).fetchone()
    if (
        old
        and old["done"]
        and now - store.instant(old["updated"]) < timedelta(hours=12)
    ):
        return True, 0
    offset = (old["cursor"] or 0) if old and not old["done"] else 0
    for page_size in (50, 25, 10, 1):
        try:
            receipt = exchange.catalog(
                root, year, pages=1, page_size=page_size, offset=offset
            )
            break
        except OSError as exc:
            if getattr(exc, "code", None) not in (406, 413) or page_size == 1:
                raise
    with closing(_db(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT * FROM season_history_catalog WHERE year=?", (year,)
        ).fetchone()
        if (tuple(current) if current else None) != (tuple(old) if old else None):
            return bool(current and current["done"]), 1
        db.execute(
            "INSERT OR REPLACE INTO season_history_catalog VALUES(?,?,?,?)",
            (
                year,
                receipt["next_offset"],
                int(receipt["page_walk_complete"]),
                store.iso(now),
            ),
        )
    return receipt["page_walk_complete"], 1


def _walk(root, job, limit):
    kind = job["kind"]
    if kind == "event":
        with closing(_db(root)) as db:
            present = db.execute(
                "SELECT 1 FROM ma_events WHERE slug=? LIMIT 1", (job["selector"],)
            ).fetchone()
        if not present:
            try:
                exchange.observe_event(root, job["selector"])
            except OSError as exc:
                if getattr(exc, "code", None) in (404, 410):
                    return (
                        "unlisted",
                        0,
                        {
                            "http_status": exc.code,
                            "reason": "exchange event absent on inspection",
                        },
                    )
                raise
        return (
            "done",
            0,
            {"source": "existing_archive" if present else "fresh_gamma_observation"},
        )
    with closing(_db(root)) as db, db:
        db.execute(
            "INSERT OR IGNORE INTO season_history_params VALUES(?,?)",
            (job["key"], limit),
        )
        limit = db.execute(
            "SELECT page_size FROM season_history_params WHERE job=?", (job["key"],)
        ).fetchone()[0]
    grain = GRAINS[job["grain"]] if kind == "prices" else None
    args = (
        {"start": job["start"], "end": job["end"], "bucket": grain}
        if kind == "prices"
        else {}
    )
    result = exchange.history(root, kind, job["selector"], limit=limit, pages=1, **args)
    stamp = store.now_utc()
    if not result["complete"]:
        return (
            "pending",
            job["grain"],
            {"last_page": result, "coverage": "cursor_saved"},
        )
    params = (
        {"event_id": job["selector"], "limit": limit}
        if kind == "trades"
        else {
            "token_id": job["selector"],
            "start": job["start"],
            "end": job["end"],
            "limit": limit,
            "bucket_seconds": grain,
        }
    )
    key = store.digest([kind, params])
    with closing(_db(root)) as db:
        pages = db.execute(
            "SELECT items,resolutions FROM ma_walk_pages WHERE key=?", (key,)
        ).fetchall()
        if pages:
            count = sum(x["items"] for x in pages)
            resolutions = sorted(
                {v for x in pages for v in json.loads(x["resolutions"])}
            )
        elif kind == "prices":
            count = db.execute(
                "SELECT count(*) FROM ma_history WHERE kind='prices' AND token=? AND resolution=? "
                "AND timestamp>=? AND timestamp<=?",
                (
                    job["selector"],
                    grain,
                    store.iso(datetime.fromtimestamp(job["start"], UTC)),
                    store.iso(datetime.fromtimestamp(job["end"], UTC)),
                ),
            ).fetchone()[0]
            resolutions = [grain] if count else []
        else:
            # Legacy completed trades walks retain their original log and indexed rows.
            count = None
            resolutions = []
    detail = {
        "finished_at": stamp,
        "requested_bucket": grain,
        "actual_resolutions": resolutions,
        "rows_in_walk": count,
        "completed_walk": key,
        "research_only": True,
    }
    if kind == "prices":
        with closing(_db(root)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO season_history_grains VALUES(?,?,?)",
                (job["key"], grain, store.canonical(detail)),
            )
    if (
        kind == "prices"
        and not any(r > 0 for r in resolutions)
        and job["grain"] + 1 < len(GRAINS)
    ):
        return (
            "pending",
            job["grain"] + 1,
            detail | {"coverage": "empty_at_requested_grain"},
        )
    return ("empty" if count == 0 else "done"), job["grain"], detail


def _page(root, job, limit, claim, deadline=None):
    now = store.instant(store.now_utc())
    bound = exchange._DEADLINE.set(deadline)
    try:
        state, grain, detail = _walk(root, job, limit)
        retry = ""
    except (OSError, ValueError, KeyError, TypeError) as exc:
        state, grain, detail = (
            "retry",
            job["grain"],
            {"error": str(exc), "http_status": getattr(exc, "code", None)},
        )
        retry = getattr(exc, "retry_at", None) or store.iso(now + timedelta(minutes=5))
    finally:
        exchange._DEADLINE.reset(bound)
    with closing(_db(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        owner = db.execute(
            "SELECT claim FROM season_history_leases WHERE job=?", (job["key"],)
        ).fetchone()
        if not owner or owner[0] != claim:
            return "superseded"
        db.execute(
            "UPDATE season_history_jobs SET state=?,grain=?,next_at=?,last_run=?,detail=? WHERE key=?",
            (state, grain, retry, store.iso(now), store.canonical(detail), job["key"]),
        )
    return state


def status(root, year):
    with closing(_db(root)) as db:
        rows = db.execute(
            "SELECT kind,state,count(*) n FROM season_history_jobs WHERE year=? GROUP BY kind,state",
            (year,),
        ).fetchall()
        catalogue = db.execute(
            "SELECT * FROM season_history_catalog WHERE year=?", (year,)
        ).fetchone()
        fixtures = db.execute(
            "SELECT count(DISTINCT fixture) FROM season_history_jobs WHERE year=?",
            (year,),
        ).fetchone()[0]
        tokens = db.execute(
            "SELECT count(DISTINCT selector) FROM season_history_jobs WHERE year=? AND kind='prices'",
            (year,),
        ).fetchone()[0]
        earliest = db.execute(
            "SELECT min(next_at) FROM season_history_jobs WHERE year=? AND state='retry'",
            (year,),
        ).fetchone()[0]
        cooldowns = [
            dict(row)
            for row in db.execute(
                "SELECT endpoint,not_before FROM ma_cooldown WHERE not_before>? ORDER BY not_before",
                (store.now_utc(),),
            )
        ]
        sample = [
            dict(r)
            for r in db.execute(
                "SELECT fixture,kind,selector,state,detail FROM season_history_jobs "
                "WHERE year=? AND state IN ('retry','empty') ORDER BY last_run DESC LIMIT 5",
                (year,),
            )
        ]
    counts = {k: {} for k in ("event", "trades", "prices")}
    for row in rows:
        counts[row["kind"]][row["state"]] = row["n"]
    return {
        "year": year,
        "catalog_complete": bool(catalogue and catalogue["done"]),
        "catalog_next_offset": catalogue["cursor"] if catalogue else 0,
        "fixtures": fixtures,
        "outcome_tokens": tokens,
        "jobs": counts,
        "next_retry_at": earliest,
        "active_cooldowns": cooldowns,
        "gap_examples": sample,
        "scope": "Gamma fixtures with scheduled start before observation; source trade and price coverage only",
        "tier_A": 0,
        "execution_roi": None,
    }


def _next_jobs(db, year, stamp, count):
    # Give all due kinds a turn, then sort price slices by earliest loss within the
    # documented floors. Origin-wide Retry-After also blocks untouched sibling jobs.
    cooldowns = {
        r[0]
        for r in db.execute(
            "SELECT endpoint FROM ma_cooldown WHERE not_before>?", (store.iso(stamp),)
        )
    }
    endpoints = {
        "event": ("gamma-api.polymarket.com", "/events"),
        "trades": ("data-api.polymarket.com", "/v2/trades"),
        "prices": ("data-api.polymarket.com", "/v2/prices-history"),
    }
    allowed = [
        k
        for k, (host, path) in endpoints.items()
        if host not in cooldowns and host + path not in cooldowns
    ]
    if not allowed:
        return []
    params = {
        "year": year,
        "stamp": store.iso(stamp),
        "now": int(stamp.timestamp()),
        "one": _RETENTION[0],
        "five": _RETENTION[1],
        "thirty": _RETENTION[2],
        "n": count,
    }
    for i, kind in enumerate(allowed):
        params[f"k{i}"] = kind
    kinds = ",".join(f":k{i}" for i in range(len(allowed)))
    return db.execute(
        f"""WITH served AS (SELECT kind,max(last_run) recently FROM season_history_jobs
                 WHERE year=:year GROUP BY kind),
             due AS (SELECT j.*, {_EXPIRY} expires FROM season_history_jobs j
                 WHERE year=:year AND kind IN ({kinds}) AND state IN ('pending','retry','running')
                   AND next_at<=:stamp),
             ranked AS (SELECT d.*, s.recently, row_number() OVER(PARTITION BY d.kind ORDER BY
                 d.expires IS NULL, d.expires, (d.last_run=''), d.last_run, d.end, d.key) n
                 FROM due d JOIN served s USING(kind))
             SELECT * FROM ranked ORDER BY n,recently,CASE kind WHEN 'trades' THEN 0 WHEN 'prices' THEN 1 ELSE 2 END
             LIMIT :n""",
        params,
    ).fetchall()


def backfill(
    root, year, *, max_pages=None, workers=4, page_size=1000, max_seconds=None
):
    if (
        not 2010 <= year <= 2100
        or not 1 <= workers <= 12
        or not 1 <= page_size <= 1000
        or (max_pages is not None and max_pages < 1)
        or (max_seconds is not None and not (0 < max_seconds <= 86400))
    ):
        raise ValueError("invalid season backfill bounds")
    now = store.instant(store.now_utc())
    started = time.monotonic()
    stop = started + max_seconds if max_seconds is not None else float("inf")
    deadline = (
        store.iso(now + timedelta(seconds=max_seconds))
        if max_seconds is not None
        else None
    )
    used = 0
    errors = []
    # First seed any previously captured originals even when Gamma is unavailable today.
    _seed(root, year, now)
    catalog_budget = None if max_pages is None else max(1, (max_pages + 2) // 3)
    while time.monotonic() < stop and (catalog_budget is None or used < catalog_budget):
        bound = exchange._DEADLINE.set(deadline)
        try:
            done, pages = _catalog(root, year, store.instant(store.now_utc()))
            used += pages
            _seed(root, year, store.instant(store.now_utc()))
        except (OSError, ValueError) as exc:
            errors.append(
                {
                    "op": "catalog",
                    "error": str(exc),
                    "retry_at": getattr(exc, "retry_at", None),
                }
            )
            break
        finally:
            exchange._DEADLINE.reset(bound)
        if done:
            break
    # The lease survives concurrent CLI invocations and releases automatically on a crash;
    # ma_walks performs compare-and-swap on the real cursor inside each page transaction.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while time.monotonic() < stop and (max_pages is None or used < max_pages):
            n = workers if max_pages is None else min(workers, max_pages - used)
            stamp = store.instant(store.now_utc())
            claim = uuid.uuid4().hex
            with closing(_db(root)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                items = _next_jobs(db, year, stamp, n)
                db.executemany(
                    "INSERT OR REPLACE INTO season_history_leases VALUES(?,?)",
                    [(r["key"], claim) for r in items],
                )
                db.executemany(
                    "UPDATE season_history_jobs SET state='running',next_at=? WHERE key=?",
                    [
                        (store.iso(stamp + timedelta(minutes=3)), r["key"])
                        for r in items
                    ],
                )
            if not items:
                break
            list(pool.map(lambda r: _page(root, r, page_size, claim, deadline), items))
            used += len(items)
            if any(item["kind"] == "event" for item in items):
                _seed(root, year, store.instant(store.now_utc()))

    elapsed = time.monotonic() - started
    return status(root, year) | {
        "page_steps": used,
        "catalog_errors": errors,
        "elapsed_seconds": round(elapsed, 3),
        "page_steps_per_minute": round(60 * used / elapsed, 2) if elapsed else None,
        "time_budget_reached": time.monotonic() >= stop,
    }


def add_parser(sub):
    p = sub.add_parser("backfill", help="按季断点回补已赛赛事成交与逐代币真实价格曲线")
    p.add_argument("year", type=int)
    p.add_argument("--max-pages", type=int)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--max-seconds", type=float, help="本次回补总时间预算（秒）")
    p.add_argument("--page-size", type=int, default=1000)
    p.set_defaults(
        func=lambda a: backfill(
            a.data_dir,
            a.year,
            max_pages=a.max_pages,
            workers=a.workers,
            page_size=a.page_size,
            max_seconds=a.max_seconds,
        )
    )
