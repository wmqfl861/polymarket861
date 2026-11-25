"""Append-only Polymarket market evidence. History never places orders or generates ROI."""

from __future__ import annotations

from contextlib import closing
from datetime import timedelta
import hashlib
import json
from urllib.error import URLError

from . import market_provider as provider
from .market_provider import (
    CLOB,
    DATA,
    FAMILIES,
    FIXTURE,
    GAMMA,
    moment,
)
from .store import archive_bytes, canonical, connect, digest, iso, now_utc, save_report

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ma_requests (
  id INTEGER PRIMARY KEY, operation TEXT NOT NULL, selector TEXT NOT NULL,
  requested_at TEXT NOT NULL, received_at TEXT NOT NULL, status TEXT NOT NULL, sha TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ma_req_select ON ma_requests(operation,selector,id);
CREATE TABLE IF NOT EXISTS ma_events (
  slug TEXT NOT NULL, fixture TEXT NOT NULL, event_id TEXT NOT NULL,
  kickoff TEXT, received_at TEXT NOT NULL, sha TEXT NOT NULL, PRIMARY KEY(slug,received_at,sha));
CREATE TABLE IF NOT EXISTS ma_contracts (
  fixture TEXT NOT NULL, event_slug TEXT NOT NULL, market_slug TEXT NOT NULL,
  received_at TEXT NOT NULL, sha TEXT NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY(event_slug,market_slug,received_at,sha));
CREATE INDEX IF NOT EXISTS ma_market_fixture ON ma_contracts(fixture,received_at);
CREATE TABLE IF NOT EXISTS ma_books (
  token TEXT NOT NULL, received_at TEXT NOT NULL, sha TEXT NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY(token,received_at,sha));
CREATE TABLE IF NOT EXISTS ma_history (
  kind TEXT NOT NULL, token TEXT NOT NULL, key TEXT NOT NULL, timestamp TEXT NOT NULL,
  resolution INTEGER, sha TEXT NOT NULL, PRIMARY KEY(kind,token,key));
CREATE INDEX IF NOT EXISTS ma_history_cutoff ON ma_history(token,timestamp,kind);
CREATE TABLE IF NOT EXISTS ma_walks (
  key TEXT PRIMARY KEY, cursor TEXT, done INTEGER NOT NULL, updated_at TEXT NOT NULL);
"""


def database(root):
    db = connect(root)
    db.executescript(_SCHEMA)
    return db


def _log(root, operation, selector, start, end, status, sha=None, detail=None):
    with closing(database(root)) as db, db:
        db.execute(
            "INSERT INTO ma_requests(operation,selector,requested_at,received_at,status,sha,detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (operation, selector, start, end, status, sha, detail),
        )


def _fetch(root, op, selector, url, params=None):
    started = now_utc()
    saved = {}
    try:
        reply = provider.get(url, params)
        saved = archive_bytes(
            root,
            "polymarket.com",
            reply.body,
            observed_at=reply.received_at,
            metadata={
                "url": reply.url,
                "requested_at": reply.requested_at,
                "operation": op,
                "request_token_ids": (params or {}).get("token_ids")
                if op == "books"
                else None,
                "http_date": reply.headers.get("date"),
                "age": reply.headers.get("age"),
            },
        )
        data = json.loads(reply.body)
    except (OSError, ValueError, URLError) as exc:
        code = getattr(exc, "code", None)
        _log(
            root,
            op,
            selector,
            started,
            now_utc(),
            "not_found" if code == 404 else "error",
            sha=saved.get("sha256"),
            detail=str(exc),
        )
        raise
    _log(
        root, op, selector, reply.requested_at, reply.received_at, "ok", saved["sha256"]
    )
    return data, reply, saved["sha256"]


def _ingest(db, event, reply, sha):
    match = FIXTURE.fullmatch(str(event.get("slug", "")))
    if not match:
        return False
    db.execute(
        "INSERT OR IGNORE INTO ma_events VALUES (?,?,?,?,?,?)",
        (
            event["slug"],
            match[1],
            str(event["id"]),
            event.get("startTime"),
            reply.received_at,
            sha,
        ),
    )
    for contract in provider.normalized_markets(event):
        contract.update(recorded_at=reply.received_at, source_sha256=sha)
        db.execute(
            "INSERT OR IGNORE INTO ma_contracts VALUES (?,?,?,?,?,?)",
            (
                match[1],
                event["slug"],
                contract["slug"],
                reply.received_at,
                sha,
                canonical(contract),
            ),
        )
    return True


def discover(root, slug):
    match = FIXTURE.fullmatch(slug)
    if not match or match[5]:
        raise ValueError(
            "supply a main EPL fixture slug such as epl-not-che-2026-11-27"
        )
    events, gaps = [], []
    for event_slug in (slug, slug + "-more-markets"):
        try:
            data, reply, sha = _fetch(
                root, "event", event_slug, GAMMA + "/events/slug/" + event_slug
            )
            if not isinstance(data, dict) or data.get("slug") != event_slug:
                raise ValueError("unexpected event identity")
            with closing(database(root)) as db, db:
                _ingest(db, data, reply, sha)
            events.append(event_slug)
        except (ValueError, OSError, URLError) as exc:
            gaps.append(
                {
                    "slug": event_slug,
                    "reason": str(exc),
                    "http_status": getattr(exc, "code", None),
                }
            )
    return {"fixture": slug, "events": events, "gaps": gaps}


def catalog(root, season, *, pages=10, page_size=30, offset=0):
    if (
        not 2010 <= season <= 2100
        or not 1 <= page_size <= 100
        or pages < 1
        or offset < 0
    ):
        raise ValueError("invalid season or pagination")
    sports, _, _ = _fetch(root, "sports", "epl", GAMMA + "/sports")
    league = next((x for x in sports if x.get("sport") == "epl"), {})
    if not league.get("primaryTagId"):
        raise ValueError("EPL league tag not found")
    all_slugs, done = set(), False
    for _ in range(pages):
        params = {
            "tag_id": league["primaryTagId"],
            "limit": page_size,
            "offset": offset,
            "order": "id",
            "ascending": "true",
            "end_date_min": f"{season}-07-01T00:00:00Z",
            "end_date_max": f"{season + 1}-07-01T00:00:00Z",
        }
        data, reply, sha = _fetch(
            root, "catalog", str(season), GAMMA + "/events", params
        )
        if not isinstance(data, list):
            raise ValueError("catalog did not return an array")
        with closing(database(root)) as db, db:
            for event in data:
                if _ingest(db, event, reply, sha):
                    all_slugs.add(FIXTURE.fullmatch(event["slug"])[1])
        offset += len(data)
        if len(data) < page_size:
            done = True
            break
    return {
        "season": season,
        "fixtures_observed": sorted(all_slugs),
        "next_offset": None if done else offset,
        "page_walk_complete": done,
        "denominator": "events matching the dated catalog query, not all EPL fixtures",
    }


def contracts(root, fixture, *, as_of=None):
    with closing(database(root)) as db:
        rows = db.execute(
            """
          WITH current AS (SELECT *,row_number() OVER(PARTITION BY slug ORDER BY received_at DESC,sha DESC) AS n
                            FROM ma_events WHERE fixture=? AND received_at<=?)
          SELECT c.payload FROM ma_contracts c JOIN current e
          ON c.event_slug=e.slug AND c.received_at=e.received_at AND c.sha=e.sha
          WHERE e.n=1 ORDER BY c.event_slug,c.market_slug
        """,
            (fixture, iso(moment(as_of)) if as_of else now_utc()),
        )
        return [json.loads(row["payload"]) for row in rows]


def snapshot_book(root, contract, token):
    if contract["mapping_error"] or token not in {
        x["token_id"] for x in contract["tokens"]
    }:
        raise ValueError("unverified mapping for requested token")
    raw, reply, sha = _fetch(root, "book", token, CLOB + "/book", {"token_id": token})
    return _record_book(root, contract, token, raw, reply, sha)


def _record_book(root, contract, token, raw, reply, sha):
    try:
        book = provider.normalize_book(raw, reply, contract, token, sha)
        # Compare the latest main and child fixture schedules, not the date string in a slug.
        times = fixture_times(root, contract["fixture"], as_of=reply.received_at)
        if len(times) != 1:
            book["pregame"] = None
            book["schedule_conflict"] = times
        with closing(database(root)) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO ma_books VALUES (?,?,?,?)",
                (token, reply.received_at, sha, canonical(book)),
            )
        return book
    except (TypeError, KeyError, ValueError) as exc:
        _log(
            root,
            "book_validation",
            token,
            reply.requested_at,
            reply.received_at,
            "invalid",
            sha,
            str(exc),
        )
        raise ValueError(str(exc)) from exc


def fixture_times(root, fixture, *, as_of=None):
    with closing(database(root)) as db:
        times = db.execute(
            "SELECT kickoff FROM (SELECT kickoff,row_number() OVER(PARTITION BY slug "
            "ORDER BY received_at DESC,sha DESC) n FROM ma_events WHERE fixture=? AND received_at<=?) "
            "WHERE n=1 AND kickoff IS NOT NULL",
            (fixture, as_of or now_utc()),
        )
        return sorted({iso(moment(x["kickoff"])) for x in times})


def stored_books(root, token, *, before=None, limit=2):
    if not 1 <= limit <= 100:
        raise ValueError("limit must be within 1..100")
    with closing(database(root)) as db:
        rows = db.execute(
            "SELECT payload FROM ma_books WHERE token=? AND received_at<=? "
            "ORDER BY received_at DESC,sha LIMIT ?",
            (token, iso(moment(before)) if before else now_utc(), limit),
        )
        return [json.loads(r["payload"]) for r in rows]


def capture(root, fixture, *, tokens=None, workers=4, discovery=None):
    from concurrent.futures import ThreadPoolExecutor

    if not 1 <= workers <= 10:
        raise ValueError("workers must be within 1..10")
    discovered = discovery if discovery is not None else discover(root, fixture)
    # A failed refresh must not reuse obsolete open/closed metadata as a fresh receipt.
    active_events = set(discovered["events"])
    markets = [
        m
        for m in contracts(root, fixture)
        if not m["mapping_error"] and m["event_slug"] in active_events
    ]
    requested = set(tokens or [])
    targets = {
        t["token_id"]: m
        for m in markets
        for t in m["tokens"]
        if (not requested or t["token_id"] in requested)
        and m["market_open"] is not False
    }

    def summary(book):
        return {
            "token_id": book["token_id"],
            "received_at": book["received_at"],
            "observed_at": book["observed_at"],
            "bids": len(book["bids"]),
            "asks": len(book["asks"]),
            "pregame": book["pregame"],
        }

    def one(pair):
        token, market = pair
        try:
            return summary(snapshot_book(root, market, token))
        except (ValueError, OSError, URLError) as exc:
            return {"token_id": token, "error": str(exc)}

    observed = []
    batch_error = None
    if 1 < len(targets) <= 500:
        try:
            rows, reply, sha = _fetch(
                root, "books", fixture, CLOB + "/books", {"token_ids": sorted(targets)}
            )
            if not isinstance(rows, list):
                raise ValueError("book batch must be an array")
            by_id = {}
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("unexpected book structure")
                token = str(row.get("asset_id"))
                if token not in targets or token in by_id:
                    raise ValueError("unexpected or repeated token in book batch")
                by_id[token] = row
            for token, market in targets.items():
                if token not in by_id:
                    observed.append(
                        {
                            "token_id": token,
                            "error": "requested token omitted from batch",
                        }
                    )
                    continue
                try:
                    observed.append(
                        summary(
                            _record_book(root, market, token, by_id[token], reply, sha)
                        )
                    )
                except ValueError as exc:
                    observed.append({"token_id": token, "error": str(exc)})
        except (ValueError, OSError, URLError) as exc:
            batch_error = str(exc)
    if not observed:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            observed = list(pool.map(one, targets.items()))
    return {
        "fixture": fixture,
        "observations": observed,
        "batch_error": batch_error,
        "discovery_gaps": discovered["gaps"],
        "unmapped_or_closed_requested_tokens": sorted(requested - targets.keys()),
    }


def history(
    root,
    kind,
    selector,
    *,
    start=None,
    end=None,
    bucket=None,
    pages=3,
    limit=250,
    restart=False,
):
    if kind not in ("trades", "prices") or not str(selector).isdigit():
        raise ValueError(
            "history requires trades with numeric event ID or prices with numeric token ID"
        )
    if not 1 <= limit <= 1000 or pages < 1:
        raise ValueError("invalid history page budget")
    if kind == "prices" and (start is None or end is None or end <= start):
        raise ValueError("prices require explicit start < end, in UTC epoch seconds")
    if kind == "trades" and any(x is not None for x in (start, end, bucket)):
        raise ValueError(
            "event trades use the three-year window; time and bucket filters do not apply"
        )
    params = (
        {"event_id": selector, "limit": limit}
        if kind == "trades"
        else {"token_id": selector, "start": start, "end": end, "limit": limit}
    )
    if bucket is not None:
        params["bucket_seconds"] = bucket
    url = DATA + ("/v2/trades" if kind == "trades" else "/v2/prices-history")
    key = digest([kind, params])
    with closing(database(root)) as db:
        old = db.execute(
            "SELECT cursor,done FROM ma_walks WHERE key=?", (key,)
        ).fetchone()
    if old and old["done"] and not restart:
        return {
            "kind": kind,
            "selector": selector,
            "complete": True,
            "new_rows": 0,
            "resumed_completed_walk": True,
        }
    cursor = None if restart or not old else old["cursor"]
    expected_state = (old["cursor"], old["done"]) if old else None
    seen, new_count, raw_count, done = set(), 0, 0, False
    for _ in range(pages):
        if cursor in seen:
            raise ValueError("upstream repeated the pagination cursor")
        seen.add(cursor)
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        data, reply, sha = _fetch(root, kind, str(selector), url, query)
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("data"), list)
            or not isinstance(data.get("pagination"), dict)
        ):
            raise ValueError("incomplete pagination envelope")
        page = data["pagination"]
        if "next_cursor" not in page or bool(page["next_cursor"]) != page.get(
            "has_more", bool(page["next_cursor"])
        ):
            raise ValueError("inconsistent pagination envelope")
        nxt = page["next_cursor"]
        if nxt and nxt in seen:
            raise ValueError("pagination returned a repeated cursor")
        with closing(database(root)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            concurrent = db.execute(
                "SELECT cursor,done FROM ma_walks WHERE key=?", (key,)
            ).fetchone()
            if (tuple(concurrent) if concurrent else None) != expected_state:
                raise ValueError(
                    "another worker advanced this walk; resume from its saved cursor"
                )
            for row in data["data"]:
                when = iso(moment(row["timestamp"]))
                price = provider.number(row["price"])
                if not 0 <= price <= 1:
                    raise ValueError("history price outside payout range")
                if kind == "trades":
                    if provider.number(row["size"]) <= 0:
                        raise ValueError("trade has invalid size")
                    token = str(row["token_id"])
                    # A transaction can contain many fills. Do not collapse solely by transaction hash.
                    economic = [
                        row.get(k)
                        for k in (
                            "transaction_hash",
                            "token_id",
                            "condition_id",
                            "side",
                            "timestamp",
                            "size",
                            "price",
                            "proxy_wallet",
                        )
                    ]
                    identity = digest(economic)
                    resolution = None
                else:
                    token = str(selector)
                    resolution = int(row["resolution_seconds"])
                    if resolution < 0:
                        raise ValueError("negative price resolution")
                    identity = digest([when, str(price), resolution])
                raw_count += 1
                new_count += db.execute(
                    "INSERT OR IGNORE INTO ma_history VALUES (?,?,?,?,?,?)",
                    (kind, token, identity, when, resolution, sha),
                ).rowcount
            done = not nxt
            db.execute(
                "INSERT INTO ma_walks VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "cursor=excluded.cursor,done=excluded.done,updated_at=excluded.updated_at",
                (key, nxt, int(done), reply.received_at),
            )
        cursor = nxt
        expected_state = (nxt, int(done))
        if done:
            break
    return {
        "kind": kind,
        "selector": selector,
        "complete": done,
        "new_rows": new_count,
        "read_rows": raw_count,
        "resume_available": bool(cursor),
        "date_filter": "fixed three-year event feed"
        if kind == "trades"
        else [start, end],
    }


def fixture_schedule(root, fixture, markets=None):
    """All kickoffs from the event and its mapped market families."""
    if markets is None:
        markets = contracts(root, fixture)
    return sorted(
        set(fixture_times(root, fixture)).union(
            t for market in markets if market["family"] for t in market["game_times"]
        )
    )


def coverage(
    root,
    *,
    fixtures=None,
    scope="listed or explicitly requested fixtures in the local archive",
):
    """Classify per-token evidence before kickoff. Keep missing fixture-market pairs in the denominator."""
    with closing(database(root)) as db:
        all_rows = db.execute("SELECT DISTINCT fixture FROM ma_events").fetchall()
        seen = {r["fixture"] for r in all_rows}
        for req in db.execute(
            "SELECT DISTINCT selector FROM ma_requests WHERE operation='event'"
        ):
            tag = FIXTURE.fullmatch(req["selector"])
            if tag:
                seen.add(tag[1])
        selected = sorted(set(fixtures) if fixtures is not None else seen)
        if any(not FIXTURE.fullmatch(s) or FIXTURE.fullmatch(s)[5] for s in selected):
            raise ValueError("coverage requires main EPL fixture slugs")
        hist = {
            (r["kind"], r["token"]): r["first"]
            for r in db.execute(
                "SELECT kind,token,min(timestamp) AS first FROM ma_history "
                "WHERE kind='trades' OR resolution>0 GROUP BY kind,token"
            )
        }
        depths, pre_depths = set(), set()
        for r in db.execute("SELECT token,payload FROM ma_books"):
            b = json.loads(r["payload"])
            if b["bids"] and b["asks"]:
                depths.add(r["token"])
                if b["observed_at"] and b["pregame"] is True and b["market_open"]:
                    pre_depths.add(r["token"])
        recent = [
            dict(r)
            for r in db.execute(
                "SELECT operation,selector,status,requested_at,received_at FROM ma_requests ORDER BY id DESC LIMIT 8"
            )
        ]
        runs = {
            r["status"]: r["n"]
            for r in db.execute(
                "SELECT status,count(*) AS n FROM ma_requests GROUP BY status"
            )
        }
        walks = {
            str(bool(r["done"])).lower(): r["n"]
            for r in db.execute("SELECT done,count(*) AS n FROM ma_walks GROUP BY done")
        }
    groups, fixture_details = {}, []
    for fixture in selected:
        match = FIXTURE.fullmatch(fixture)
        season_date = moment(match[4] + "T00:00:00Z")
        y = season_date.year - int(season_date.month < 7)
        season = f"{y}/{str(y + 1)[2:]}"
        markets = contracts(root, fixture)
        # Different kickoff times across a primary and a child contract invalidate this replay.
        times = fixture_schedule(root,fixture,markets)
        cut = times[0] if len(times) == 1 else None
        fixture_details.append(
            {
                "fixture": fixture,
                "season": season,
                "kickoff_candidates": times,
                "schedule_conflict": len(times) > 1,
                "mapped_families": sorted(
                    {m["family"] for m in markets if not m["mapping_error"]}
                ),
                "unmapped_supported_candidates": sum(
                    m["mapping_error"]
                    not in (None, "outside_supported_full_time_families")
                    for m in markets
                ),
            }
        )
        for fam in FAMILIES:
            group = groups.setdefault(
                (season, fam),
                {
                    "season": season,
                    "family": fam,
                    "expected_fixtures": 0,
                    "listed_fixtures": 0,
                    "contracts": 0,
                    "tokens": 0,
                    "two_sided_book_tokens": 0,
                    "pregame_book_tokens": 0,
                    "trade_tokens": 0,
                    "price_tokens": 0,
                    "evidence": {"A": 0, "B": 0, "C": 0, "missing": 0},
                    "unlisted_or_unverified_fixtures": 0,
                },
            )
            selected_markets = [
                m for m in markets if m["family"] == fam and not m["mapping_error"]
            ]
            tokens = {t["token_id"] for m in selected_markets for t in m["tokens"]}
            group["expected_fixtures"] += 1
            group["listed_fixtures"] += bool(tokens)
            group["unlisted_or_unverified_fixtures"] += not tokens
            group["contracts"] += len({m["condition_id"] for m in selected_markets})
            group["tokens"] += len(tokens)
            for token in tokens:
                trade = cut is not None and hist.get(("trades", token), "~") < cut
                price = cut is not None and hist.get(("prices", token), "~") < cut
                group["trade_tokens"] += trade
                group["price_tokens"] += price
                group["two_sided_book_tokens"] += token in depths
                group["pregame_book_tokens"] += token in pre_depths and cut is not None
                group["evidence"]["B" if trade else "C" if price else "missing"] += 1
    result = {
        "schema_version": 1,
        "venue": provider.VENUE,
        "as_of": now_utc(),
        "sample_scope": scope,
        "fixtures": len(selected),
        "groups": list(groups.values()),
        "fixture_details": fixture_details,
        "history_walks_complete": walks.get("true", 0),
        "history_walks_incomplete": walks.get("false", 0),
        "local_request_status_counts": runs,
        "recent_requests": recent,
        "executable_roi": None,
        "limits": [
            "A is not certified by the archive; run the ledger's verified two-time executable replay.",
            "B and C are disjoint token counts. trade_tokens/price_tokens can overlap.",
            "B requires at least one pre-kickoff trade. C requires a positive-resolution pre-kickoff price.",
            "Scheduled kickoff is a research cutoff. Conflicting schedules exclude all affected tokens from B/C.",
            "One book alone, missing market or failed request provides no simulated fill or return.",
            "An unverified or unlisted fixture in this archive does not establish historical market absence.",
        ],
    }
    save_report(root, "market-coverage", "latest", result)
    return result


def next_capture(kickoff, now, *, delay=5):
    """Return next UTC request time and target. Periodic grid is anchored to the fixture."""
    elapsed = (kickoff - now).total_seconds()
    if elapsed <= 0:
        return None
    interval = 900 if elapsed > 10800 else 60
    from math import ceil

    until_next = max(0, (ceil(elapsed / interval) - 1) * interval)
    future = [(kickoff - timedelta(seconds=until_next), "periodic")]
    targets = []
    for h in (24, 1):
        decision = kickoff - timedelta(hours=h)
        targets.extend(
            [
                (decision - timedelta(seconds=15), "decision_book"),
                (decision + timedelta(seconds=max(5, delay)), "execution_book"),
            ]
        )
    targets.append((kickoff - timedelta(seconds=20), "closing_book"))
    # Reserve the decision window for the requested before/after pair. A
    # periodic request at the cutoff itself could block the execution sample.
    decision_times = [kickoff - timedelta(hours=h) for h in (24, 1)]
    future = [
        (t, role)
        for t, role in future
        if not any(
            decision - timedelta(seconds=15)
            <= t
            <= decision + timedelta(seconds=max(5, delay) + 5)
            for decision in decision_times
        )
    ]
    future.extend(targets)
    future = [x for x in future if now < x[0] < kickoff]
    if not future and elapsed > interval:
        return now + timedelta(seconds=interval), "periodic"
    return min(future) if future else None


def watch(root, fixture, *, cycles=1, tokens=None):
    """Foreground collector. Stop at kickoff; a restart keeps actual gaps and previous receipts."""
    import time

    if cycles < 1:
        raise ValueError("cycles must be positive")
    discovered = discover(root, fixture)
    times = fixture_times(root, fixture)
    if len(times) != 1 or fixture not in discovered["events"]:
        raise ValueError("a fresh unambiguous fixture schedule is required")
    targets = contracts(root, fixture)
    delays = {
        max(5, int(m["seconds_delay"]))
        for m in targets
        if m["family"] and not m["mapping_error"] and m["seconds_delay"] is not None
    }
    kickoff = moment(times[0])
    observations = []
    role = "initial"
    for _ in range(cycles):
        now = moment(now_utc())
        if now >= kickoff:
            break
        with closing(database(root)) as db:
            last = db.execute(
                "SELECT received_at FROM ma_requests WHERE operation='heartbeat' AND selector=? "
                "AND status='ok' ORDER BY id DESC LIMIT 1",
                (fixture,),
            ).fetchone()
        cadence = 900 if kickoff - now > timedelta(hours=3) else 60
        if last and (now - moment(last["received_at"])).total_seconds() >= 2 * cadence:
            _log(
                root,
                "gap",
                fixture,
                last["received_at"],
                iso(now),
                "gap",
                detail="two expected sampling intervals missed",
            )
        if role == "periodic":
            discovered = discover(root, fixture)
        # Critical before/after sampling reuses the latest refreshed metadata
        # and requests depth immediately; actual receipts still decide eligibility.
        result = capture(root, fixture, tokens=tokens, discovery=discovered)
        end = now_utc()
        good = any("error" not in b for b in result["observations"])
        _log(
            root,
            "heartbeat",
            fixture,
            iso(now),
            end,
            "ok" if good else "error",
            detail="scheduled market poll",
        )
        observations.append(
            {
                "target": role,
                "started": iso(now),
                "end": end,
                "books": sum("error" not in b for b in result["observations"]),
            }
        )
        if _ + 1 == cycles:
            break
        # Refetches can revise kickoff or put a fixture into a conflict state.
        times = fixture_times(root, fixture)
        if len(times) != 1:
            break
        kickoff = moment(times[0])
        choices = [next_capture(kickoff, moment(end), delay=d) for d in delays or {5}]
        future = [x for x in choices if x]
        if not future:
            break
        target, role = min(future)
        time.sleep(max(0, (target - moment(now_utc())).total_seconds()))
    return {"fixture": fixture, "runs": observations}


def register(subparsers):
    market = subparsers.add_parser(
        "market", help="archive free public international Polymarket data"
    )
    cmd = market.add_subparsers(dest="market_command", required=True)
    p = cmd.add_parser(
        "reindex", help="reprocess stored mappings, preserving previous derived values"
    )
    p.set_defaults(func=lambda a: reindex(a.data_dir))
    p = cmd.add_parser(
        "catalog-summary", help="measure listings in a season from the local archive"
    )
    p.add_argument("season", type=int)
    p.add_argument("--as-of")
    p.set_defaults(func=lambda a: catalog_summary(a.data_dir, a.season, as_of=a.as_of))
    p = cmd.add_parser(
        "discover", help="record a main EPL fixture and its more-markets event"
    )
    p.add_argument("fixture")
    p.set_defaults(func=lambda a: discover(a.data_dir, a.fixture))
    p = cmd.add_parser(
        "catalog", help="walk official EPL directory for a season starting in YEAR"
    )
    p.add_argument("season", type=int)
    p.add_argument("--pages", type=int, default=10)
    p.add_argument("--page-size", type=int, default=30)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(
        func=lambda a: catalog(
            a.data_dir, a.season, pages=a.pages, page_size=a.page_size, offset=a.offset
        )
    )
    p = cmd.add_parser(
        "markets", help="inspect mapped contracts and their exact rule/fee provenance"
    )
    p.add_argument("fixture")
    p.set_defaults(func=lambda a: {"contracts": contracts(a.data_dir, a.fixture)})
    p = cmd.add_parser("capture", help="record fresh depth once for one fixture")
    p.add_argument("fixture")
    p.add_argument("--token", action="append")
    p.add_argument("--workers", type=int, default=4)
    p.set_defaults(
        func=lambda a: capture(a.data_dir, a.fixture, tokens=a.token, workers=a.workers)
    )
    p = cmd.add_parser(
        "watch",
        help="foreground polling with pregame cutoffs and persistent heartbeats",
    )
    p.add_argument("fixture")
    p.add_argument("--token", action="append")
    p.add_argument("--cycles", type=int, default=1)
    p.set_defaults(
        func=lambda a: watch(a.data_dir, a.fixture, tokens=a.token, cycles=a.cycles)
    )
    p = cmd.add_parser(
        "books", help="read archived token depth without querying the network"
    )
    p.add_argument("token")
    p.add_argument("--before", help="UTC cutoff")
    p.add_argument("--limit", type=int, default=2)
    p.set_defaults(
        func=lambda a: {
            "books": stored_books(a.data_dir, a.token, before=a.before, limit=a.limit)
        }
    )
    p = cmd.add_parser(
        "history", help="resume v2 event trades or a fixed token price window"
    )
    p.add_argument("kind", choices=("trades", "prices"))
    p.add_argument("selector", help="event ID for trades, token ID for prices")
    p.add_argument("--start", type=int, help="price window start UTC epoch seconds")
    p.add_argument("--end", type=int)
    p.add_argument("--bucket", type=int)
    p.add_argument("--pages", type=int, default=3)
    p.add_argument("--limit", type=int, default=250)
    p.add_argument(
        "--restart", action="store_true", help="start a new walk of the same request"
    )
    p.set_defaults(
        func=lambda a: history(
            a.data_dir,
            a.kind,
            a.selector,
            start=a.start,
            end=a.end,
            bucket=a.bucket,
            pages=a.pages,
            limit=a.limit,
            restart=a.restart,
        )
    )
    p = cmd.add_parser(
        "replay-inputs",
        help="export token-level research witnesses for the historical ledger",
    )
    p.add_argument("--fixture", action="append", required=True)
    p.add_argument("--account",help="Select the predeclared formal main paper account for archival decision reconstruction")
    p.set_defaults(
        func=lambda a: {"opportunities": replay_inputs(a.data_dir, a.fixture, account=a.account)}
    )
    p = cmd.add_parser(
        "coverage", help="measure archive coverage in an explicit fixture denominator"
    )
    p.add_argument("--fixture", action="append")
    p.set_defaults(func=lambda a: coverage(a.data_dir, fixtures=a.fixture))


def replay_inputs(root, fixtures, *, account=None):
    """Export genuine pregame witnesses to the fail-closed ledger replay, with provenance."""
    from functools import lru_cache
    from .archive_proof import research_tier
    from .store import blob_path

    @lru_cache(maxsize=8)
    def raw_page(sha):
        try:
            body = blob_path(root, sha).read_bytes()
            if hashlib.sha256(body).hexdigest() != sha:
                return []
            rows = json.loads(body)["data"]
            return rows if isinstance(rows, list) else []
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def first_witness(db, kind, token, cutoff, item):
        records = db.execute(
            "SELECT * FROM ma_history WHERE token=? AND kind=? "
            "AND (resolution IS NULL OR resolution>0) AND timestamp<? ORDER BY timestamp,key",
            (token, kind, cutoff),
        )
        expected = "B" if kind == "trades" else "C"
        for witness in records:
            for row in raw_page(witness["sha"]):
                try:
                    if iso(moment(row["timestamp"])) != witness["timestamp"]:
                        continue
                    if kind == "trades" and str(row["token_id"]) != token:
                        continue
                    fields = {
                        k: row[k]
                        for k in (
                            ("price", "size")
                            if kind == "trades"
                            else ("price", "resolution_seconds")
                        )
                    }
                    hint = fields | {
                        "timestamp": witness["timestamp"],
                        "source_sha256": witness["sha"],
                        "token_id": token,
                    }
                    probe = item | {"trades": [], "prices": [], kind: [hint]}
                    if research_tier(db, root, probe) == expected:
                        return [hint]
                except (
                    ValueError,
                    TypeError,
                    KeyError,
                    OverflowError,
                    OSError,
                    AttributeError,
                ):
                    continue
        return []

    fixtures = sorted(set(fixtures))
    examples = []
    with closing(database(root)) as db:
        for fixture in fixtures:
            match = FIXTURE.fullmatch(fixture)
            if not match or match[5]:
                raise ValueError("a main EPL slug is required")
            date = moment(match[4] + "T00:00:00Z")
            season_start = date.year - (date.month < 7)
            season = f"{season_start}/{str(season_start + 1)[2:]}"
            markets = [m for m in contracts(root, fixture) if not m["mapping_error"]]
            schedule = sorted(
                set(fixture_times(root, fixture)).union(
                    t for m in markets for t in m["game_times"]
                )
            )
            cutoff = schedule[0] if len(schedule) == 1 else None
            for family in FAMILIES:
                matched = [m for m in markets if m["family"] == family]
                base = {
                    "match_id": fixture,
                    "market_fixture": fixture,
                    "family": family,
                    "season": season,
                    "venue": provider.VENUE,
                    "listed": bool(matched),
                    "decision_at": None,
                    "research_cutoff": cutoff,
                    "schedule_conflict": len(schedule) > 1,
                    "liquidity": "unverified",
                }
                if not matched:
                    examples.append(
                        base
                        | {
                            "condition_id": None,
                            "token_id": None,
                            "trades": [],
                            "prices": [],
                        }
                    )
                for m in matched:
                    for t in m["tokens"]:
                        token = t["token_id"]
                        item = base | {
                            "condition_id": m["condition_id"],
                            "token_id": token,
                            "outcome": t["outcome"],
                            "market_slug": m["slug"],
                            "market_recorded_at": m["recorded_at"],
                            "market_source_sha256": m["source_sha256"],
                            "trades": [],
                            "prices": [],
                        }
                        if cutoff:
                            for kind in ("trades","prices"):
                                item[kind]=first_witness(db,kind,token,cutoff,item)
                        examples.append(item)
    from .opportunity import archived_inputs
    return archived_inputs(root,fixtures,examples,account=account)


def reindex(root):
    """Rebuild derived event/market mappings from unchanged evidence; preserve revised rows."""
    from .store import blob_path

    changes = 0
    with closing(database(root)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS ma_mapping_changes (
          id INTEGER PRIMARY KEY, updated_at TEXT, source_sha TEXT, event_slug TEXT,
          market_slug TEXT, old_payload TEXT, new_payload TEXT)""")
        batches = db.execute(
            "SELECT DISTINCT sha,received_at FROM ma_events ORDER BY received_at"
        ).fetchall()
        db.execute("BEGIN IMMEDIATE")
        for batch in batches:
            raw = json.loads(blob_path(root, batch["sha"]).read_bytes())
            for event in raw if isinstance(raw, list) else [raw]:
                if not isinstance(event, dict) or not FIXTURE.fullmatch(
                    str(event.get("slug", ""))
                ):
                    continue
                for contract in provider.normalized_markets(event):
                    contract.update(
                        recorded_at=batch["received_at"], source_sha256=batch["sha"]
                    )
                    new = canonical(contract)
                    keys = (
                        event["slug"],
                        contract["slug"],
                        batch["received_at"],
                        batch["sha"],
                    )
                    old = db.execute(
                        "SELECT payload FROM ma_contracts WHERE event_slug=? AND market_slug=? "
                        "AND received_at=? AND sha=?",
                        keys,
                    ).fetchone()
                    if old is None or old[0] == new:
                        continue
                    db.execute(
                        "INSERT INTO ma_mapping_changes(updated_at,source_sha,event_slug,"
                        "market_slug,old_payload,new_payload) VALUES (?,?,?,?,?,?)",
                        (
                            now_utc(),
                            batch["sha"],
                            event["slug"],
                            contract["slug"],
                            old[0],
                            new,
                        ),
                    )
                    db.execute(
                        "UPDATE ma_contracts SET payload=? WHERE event_slug=? AND market_slug=? "
                        "AND received_at=? AND sha=?",
                        (new,) + keys,
                    )
                    changes += 1
    return {"raw_observations": len(batches), "changed_mappings": changes}


def catalog_summary(root, season, *, as_of=None):
    """Count archived fixture slugs and mapped full-time listings by EPL season."""
    cutoff = iso(moment(as_of or now_utc()))
    with closing(database(root)) as db:
        rows = db.execute(
            "SELECT DISTINCT fixture FROM ma_events WHERE substr(fixture,-10)>=? "
            "AND substr(fixture,-10)<?",
            (f"{season}-07-01", f"{season + 1}-07-01"),
        ).fetchall()
        ids = {r[0] for r in rows}
        counted = {}
        db.execute("CREATE TEMP TABLE catalogue_set(fixture TEXT PRIMARY KEY)")
        db.executemany("INSERT INTO catalogue_set VALUES (?)", [(f,) for f in ids])
        current = """WITH current AS (SELECT *,row_number() OVER(PARTITION BY slug ORDER BY received_at DESC,sha DESC) n
                         FROM ma_events WHERE received_at<=?) """
        totals = db.execute(
            current + "SELECT count(*) n, count(DISTINCT e.fixture) fixtures, "
            "count(DISTINCT CASE WHEN slug=e.fixture AND julianday(kickoff)<julianday(?) THEN e.fixture END) past "
            "FROM current e JOIN catalogue_set r USING (fixture) WHERE n=1",
            (cutoff, cutoff),
        ).fetchone()
        matches = db.execute(
            current
            + """SELECT json_extract(c.payload,'$.family') family,count(distinct c.fixture) fixtures,
                         count(distinct json_extract(c.payload,'$.condition_id')) contracts
                  FROM current e JOIN ma_contracts c ON c.event_slug=e.slug AND c.sha=e.sha AND c.received_at=e.received_at
                  JOIN catalogue_set r ON r.fixture=c.fixture
                  WHERE e.n=1 AND json_extract(c.payload,'$.mapping_error') IS NULL GROUP BY family""",
            (cutoff,),
        )
        for m in matches:
            counted[m["family"]] = {
                "fixtures": m["fixtures"],
                "contracts": m["contracts"],
            }
        for f in FAMILIES:
            counted.setdefault(f, {"fixtures": 0, "contracts": 0})
    return {
        "season": f"{season}/{str(season + 1)[2:]}",
        "as_of": cutoff,
        "verified_fixture_slugs": totals["fixtures"],
        "main_and_auxiliary_events": totals["n"],
        "nominal_main_kickoff_before_as_of": totals["past"],
        "mapped_listings": counted,
        "denominator": "Archived EPL main/auxiliary fixture slugs. Event listings do not prove pregame buying depth.",
    }
