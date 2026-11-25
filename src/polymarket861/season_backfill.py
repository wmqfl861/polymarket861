"""Resumable season-wide Polymarket trades and individually measured token price curves."""

import hashlib
import json
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta

from . import market_archive as exchange
from . import market_provider as provider
from . import store

GRAINS = (60, 300, 1800, 10800, 43200)
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
    db.executescript(_SCHEMA)
    return db


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
            for row in rows:
                event = events.get(row["slug"])
                if not event or str(event.get("id")) != row["event_id"]:
                    raise ValueError(
                        "event catalogue index disagrees with the raw payload"
                    )

                def enqueue(kind, selector, begin=0, end=0, fixture=row["fixture"]):
                    key = store.digest([year, kind, selector, begin, end])
                    db.execute(
                        "INSERT OR IGNORE INTO season_history_jobs VALUES(?,?,?,?,?,?,?,0,'pending','','',NULL)",
                        (key, year, fixture, kind, selector, begin, end),
                    )

                sibling = (
                    row["fixture"] + "-more-markets"
                    if row["slug"] == row["fixture"]
                    else row["fixture"]
                )
                if not db.execute(
                    "SELECT 1 FROM ma_events WHERE slug=? LIMIT 1", (sibling,)
                ).fetchone():
                    enqueue("event", sibling)
                enqueue("trades", row["event_id"])
                native = {
                    x["slug"]: x
                    for x in event.get("markets") or []
                    if isinstance(x, dict) and x.get("slug")
                }
                for m in provider.normalized_markets(event):
                    if m.get("mapping_error"):
                        continue
                    src = native[m["slug"]]
                    born = (
                        src.get("startDate")
                        or src.get("createdAt")
                        or event.get("startDate")
                        or first + "T00:00:00Z"
                    )
                    closed = src.get("closedTime") or event.get("closedTime")
                    # A still-open/unknown market's latest full checkpoint advances in 24h
                    # slices on later refreshes, with a fixed initial end at scheduled kickoff.
                    kick = provider.moment(row["kickoff"])
                    end = (
                        provider.moment(closed)
                        if closed
                        else kick + timedelta(days=max(0, (now - kick).days))
                    )
                    finish = int(min(now, end).timestamp())
                    begin = int(provider.moment(born).timestamp())
                    for tok in m["tokens"]:
                        lower = begin
                        while lower < finish:
                            # Overlapping bounds at the endpoint are safe: source rows are deduplicated.
                            upper = min(finish, ((lower // 86400) + 1) * 86400)
                            enqueue("prices", tok["token_id"], lower, upper)
                            lower = upper
                db.execute(
                    "INSERT OR REPLACE INTO season_history_seed VALUES(?,?,?)",
                    (year, row["slug"], sha),
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


def _page(root, job, limit, claim):
    now = store.instant(store.now_utc())
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
        "gap_examples": sample,
        "scope": "Gamma fixtures with scheduled start before observation; source trade and price coverage only",
        "tier_A": 0,
        "execution_roi": None,
    }


def backfill(root, year, *, max_pages=None, workers=4, page_size=250):
    if (
        not 2010 <= year <= 2100
        or not 1 <= workers <= 12
        or not 1 <= page_size <= 1000
        or (max_pages is not None and max_pages < 1)
    ):
        raise ValueError("invalid season backfill bounds")
    now = store.instant(store.now_utc())
    used = 0
    errors = []
    # First seed any previously captured originals even when Gamma is unavailable today.
    _seed(root, year, now)
    catalog_budget = None if max_pages is None else max(1, (max_pages + 2) // 3)
    while catalog_budget is None or used < catalog_budget:
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
        if done:
            break
    # The lease survives concurrent CLI invocations and releases automatically on a crash;
    # ma_walks performs compare-and-swap on the real cursor inside each page transaction.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while max_pages is None or used < max_pages:
            n = workers if max_pages is None else min(workers, max_pages - used)
            stamp = store.instant(store.now_utc())
            claim = uuid.uuid4().hex
            with closing(_db(root)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                items = db.execute(
                    "WITH served AS (SELECT kind,max(last_run) recently FROM season_history_jobs WHERE year=? GROUP BY kind),"
                    " candidates AS (SELECT j.*,s.recently,row_number() OVER(PARTITION BY j.kind ORDER BY "
                    " CASE WHEN j.start>=? THEN 0 ELSE 1 END,CASE WHEN j.grain>0 THEN 0 ELSE 1 END,j.last_run,j.fixture,j.key) n FROM season_history_jobs j "
                    " JOIN served s USING(kind) WHERE j.year=? AND j.state IN ('pending','retry','running') AND j.next_at<=?) "
                    " SELECT * FROM candidates ORDER BY n,recently,CASE kind WHEN 'trades' THEN 0 WHEN 'prices' THEN 1 ELSE 2 END LIMIT ?",
                    (
                        year,
                        int((stamp - timedelta(days=8)).timestamp()),
                        year,
                        store.iso(stamp),
                        n,
                    ),
                ).fetchall()
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
            results = list(pool.map(lambda r: _page(root, r, page_size, claim), items))
            used += len(items)
            if any(item["kind"] == "event" for item in items):
                _seed(root, year, store.instant(store.now_utc()))
            if all(x == "retry" for x in results):
                # A busy public endpoint gets the original Retry-After, including other jobs.
                with closing(_db(root)) as db:
                    if db.execute(
                        "SELECT 1 FROM ma_cooldown WHERE endpoint LIKE 'data-api.%' AND not_before>?",
                        (store.iso(stamp),),
                    ).fetchone():
                        break
    return status(root, year) | {"page_steps": used, "catalog_errors": errors}


def add_parser(sub):
    p = sub.add_parser("backfill", help="按季断点回补已赛赛事成交与逐代币真实价格曲线")
    p.add_argument("year", type=int)
    p.add_argument("--max-pages", type=int)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--page-size", type=int, default=250)
    p.set_defaults(
        func=lambda a: backfill(
            a.data_dir,
            a.year,
            max_pages=a.max_pages,
            workers=a.workers,
            page_size=a.page_size,
        )
    )
