"""Short-window CLOB snapshots for actual frozen paper orders."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import closing
from datetime import timedelta
from http.client import HTTPException

from . import market_archive as archive
from . import market_provider as provider
from . import store
from .archive_proof import original_receipt

_TIMEOUT = 1.5
_HEDGE_AFTER = 1.0
_research_capacity = threading.BoundedSemaphore(1)


def frozen_orders(db, fixture, kickoff, target):
    """Use a sealed plan's fixture, kickoff, cutoff and own delay, including legacy accounts."""
    kickoff, target = store.instant(kickoff), store.instant(target)
    cutoffs = [store.iso(kickoff - timedelta(hours=h)) for h in (1, 24)]
    tables = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    orders = []
    if {"paper_accounts", "paper_intents"} <= tables:
        orders.extend(
            db.execute(
                "SELECT i.fixture,i.cutoff,i.token,i.condition_id,i.fingerprint,i.original,NULL AS slug "
                "FROM paper_intents i JOIN paper_accounts a USING(account) WHERE a.mode IN ('formal','shadow') "
                "AND i.status='frozen' AND i.cutoff IN (?,?) AND CASE WHEN json_valid(i.original) "
                "THEN coalesce(json_extract(i.original,'$.intent.market_slug'),?)=? ELSE "
                "(i.fixture=? OR EXISTS(SELECT 1 FROM ma_contracts c WHERE c.fixture=? AND json_valid(c.payload) "
                "AND json_extract(c.payload,'$.condition_id')=i.condition_id)) END",
                [*cutoffs, fixture, fixture, fixture, fixture],
            )
        )
    if "research_shadow_orders" in tables:
        orders.extend(
            db.execute(
                "SELECT fixture,cutoff,token,condition_id,plan_sha,plan,slug FROM research_shadow_orders "
                "WHERE slug=? AND status='frozen' AND cutoff IN (?,?)",
                [fixture, *cutoffs],
            )
        )
    selected = {}
    for fixture_id, cutoff, token, condition, fingerprint, raw, slug in orders:
        plan = json.loads(raw)
        intent = plan["intent"]
        c = intent["contract"]
        original_slug = intent.get("market_slug")
        if not original_slug and not slug:
            # Directly frozen legacy CLI orders may identify only the registered match.
            if fixture_id == fixture:
                original_slug = fixture
            else:
                archived = db.execute(
                    "SELECT DISTINCT fixture FROM ma_contracts WHERE sha=? AND received_at=? "
                    "AND json_extract(payload,'$.condition_id')=?",
                    (c.get("rule_source_sha256"), c.get("rule_received_at"), condition),
                ).fetchall()
                if len(archived) == 1:
                    original_slug = archived[0][0]
        if (slug or original_slug) != fixture:
            continue
        if (
            store.instant(intent["kickoff"]) != kickoff
            or store.instant(cutoff)
            + timedelta(seconds=max(5, int(c["seconds_delay"])))
            != target
        ):
            continue
        if (
            store.digest(plan) != fingerprint
            or (original_slug is not None and original_slug != fixture)
            or intent["match_id"] != fixture_id
            or store.instant(intent["decision_at"]) != store.instant(cutoff)
            or str(c["token_id"]) != token
            or c["condition_id"] != condition
        ):
            raise ValueError("frozen execution keys disagree with the sealed plan")
        if selected.setdefault(token, condition) != condition:
            raise ValueError("frozen token maps to conflicting conditions")
    return selected


def native_rows(rows, tokens):
    if not isinstance(rows, list):
        raise ValueError("book batch must be an array")
    result = {}
    for row in rows:
        token = str(row.get("asset_id")) if isinstance(row, dict) else None
        if token not in tokens or token in result:
            raise ValueError("unexpected or repeated token in book batch")
        result[token] = row
    return result


def _native_time(book, reply):
    return book["exchange_at"] is not None and store.instant(
        book["exchange_at"]
    ) <= store.instant(reply.received_at)


def _claim(root, key, leg, tokens):
    with closing(archive.database(root)) as db, db:
        return (
            db.execute(
                "INSERT OR IGNORE INTO ma_execution_attempts(fixture,kickoff,target,leg,started,tokens,status) "
                "VALUES(?,?,?,?,?,?,'sent')",
                (*key, leg, archive.now_utc(), store.canonical(sorted(tokens))),
            ).rowcount
            == 1
        )


def _attempt(root, key, leg, markets, deadline):
    started = time.monotonic()
    received = sha = detail = retry_at = None
    state, observed = "failed", set()
    elapsed = None
    bound = archive._DEADLINE.set(deadline)
    try:
        rows, reply, sha = archive._fetch(
            root,
            "books",
            key[0],
            provider.CLOB + "/books",
            {"token_ids": sorted(markets)},
            attempts=1,
            timeout=_TIMEOUT,
            metadata={
                "capture_scope": "frozen_execution",
                "scheduled_kickoff": key[1],
                "execution_target": key[2],
                "execution_deadline": deadline,
                "leg": leg,
            },
        )
        received = reply.received_at
        elapsed = (
            store.instant(received) - store.instant(reply.requested_at)
        ).total_seconds() * 1000
        if (
            not key[2]
            <= store.iso(reply.requested_at)
            <= store.iso(received)
            <= deadline
        ):
            state, detail = (
                "outside_slot",
                "native request/receipt outside execution window",
            )
        elif not provider.fresh_observation(reply):
            state, detail = "stale", "native HTTP freshness check failed"
        else:
            by_token = native_rows(rows, markets)
            for token, raw in by_token.items():
                try:
                    book = provider.normalize_book(
                        raw, reply, markets[token], token, sha
                    )
                    if _native_time(book, reply):
                        observed.add(token)
                except (ValueError, KeyError, TypeError):
                    continue
            state = "received" if observed else "invalid"
            if len(observed) != len(markets):
                detail = "one or more requested books missing or invalid"
    except (ValueError, KeyError, TypeError, OSError, HTTPException) as exc:
        detail, retry_at = str(exc), getattr(exc, "retry_at", None)
        code = getattr(exc, "code", None)
        if retry_at:
            state = "deferred"
        elif code and 400 <= code < 500 and code not in (408, 425):
            state = "rejected"
    finally:
        archive._DEADLINE.reset(bound)
        with closing(archive.database(root)) as db, db:
            db.execute(
                "UPDATE ma_execution_attempts SET status=?,received=?,sha=?,elapsed_ms=?,detail=?,retry_at=? "
                "WHERE fixture=? AND kickoff=? AND target=? AND leg=?",
                (
                    state,
                    received,
                    sha,
                    round(
                        elapsed
                        if elapsed is not None
                        else (time.monotonic() - started) * 1000,
                        3,
                    ),
                    detail,
                    retry_at,
                    *key,
                    leg,
                ),
            )
    return state, observed


def _research(root, key, tokens, deadline):
    if not tokens or not _research_capacity.acquire(blocking=False):
        return False

    def read():
        bound = archive._DEADLINE.set(deadline)
        try:
            archive._fetch(
                root,
                "books_research",
                key[0],
                provider.CLOB + "/books",
                {"token_ids": sorted(tokens)},
                attempts=1,
                timeout=_TIMEOUT,
                metadata={
                    "capture_scope": "non_frozen_research",
                    "execution_target": key[2],
                },
            )
        except (OSError, ValueError, HTTPException):
            pass  # _fetch records the response or error; this operation cannot supply execution evidence.
        finally:
            archive._DEADLINE.reset(bound)
            _research_capacity.release()

    threading.Thread(
        target=read, name="pm861-optional-book-research", daemon=True
    ).start()
    return True


def _select(root, key, markets, attempts):
    from .market_archive import book_summary

    seen = set()
    with closing(archive.database(root)) as db, db:
        for leg in sorted(
            (r for r in attempts if r["status"] == "received"),
            key=lambda r: (r["received"], r["leg"]),
        ):
            reply, _, selector, meta = original_receipt(
                db, root, leg["sha"], leg["received"], {"books"}
            )
            tokens = json.loads(leg["tokens"])
            if selector != key[0] or meta.get("request_token_ids") != tokens:
                continue
            native = native_rows(json.loads(reply.body), tokens)
            times = archive.fixture_times(root, key[0], as_of=reply.received_at)
            if times != [key[1]]:
                continue
            for token in (markets.keys() & native.keys()) - seen:
                try:
                    book = archive._book_value(
                        db,
                        markets[token],
                        token,
                        native[token],
                        reply,
                        leg["sha"],
                        times,
                    )
                except (KeyError, ValueError, TypeError):
                    continue
                if not _native_time(book, reply):
                    continue
                db.execute(
                    "INSERT INTO ma_execution_selected VALUES(?,?,?,?,?,?) ON CONFLICT(fixture,kickoff,target,token) "
                    "DO UPDATE SET received=excluded.received,sha=excluded.sha WHERE (excluded.received,excluded.sha) "
                    "< (ma_execution_selected.received,ma_execution_selected.sha)",
                    (*key, token, reply.received_at, leg["sha"]),
                )
                db.execute(
                    "INSERT OR IGNORE INTO ma_books SELECT token,received,sha,? FROM ma_execution_selected "
                    "WHERE fixture=? AND kickoff=? AND target=? AND token=? AND received=? AND sha=?",
                    (store.canonical(book), *key, token, reply.received_at, leg["sha"]),
                )
                seen.add(token)
        rows = db.execute(
            "SELECT b.payload FROM ma_execution_selected s JOIN ma_books b "
            "ON b.token=s.token AND b.received_at=s.received AND b.sha=s.sha WHERE s.fixture=? AND s.kickoff=? AND s.target=?",
            key,
        )
        books = (json.loads(row[0]) for row in rows)
        return {
            book["token_id"]: book_summary(book)
            for book in books
            if book["token_id"] in markets
        }


def capture(root, fixture, *, kickoff, target, deadline, discovery, research=False):
    key = fixture, store.iso(kickoff), store.iso(target)
    deadline = store.iso(
        min(store.instant(deadline), store.instant(target) + timedelta(seconds=5))
    )
    with closing(archive.database(root)) as db:
        frozen = frozen_orders(db, *key)
    report = {
        "fixture": fixture,
        "scope": "frozen_execution",
        "observations": [],
        "batch_error": None,
        "retry_at": None,
        "discovery_gaps": discovery["gaps"],
        "requested_tokens": sorted(frozen),
        "no_frozen_intents": not frozen,
        "unmapped_or_closed_requested_tokens": [],
        "attempt_latency_ms": [],
        "hedge_started": False,
        "research_submitted": False,
        "non_frozen_count": 0,
    }
    if not frozen and not research:
        return report
    latest = archive.contracts(root, fixture)
    active = set(discovery["events"])
    available = {}
    ambiguous = set()
    for market in latest:
        if (
            market["event_slug"] not in active
            or market["mapping_error"]
            or market["market_open"] is False
        ):
            continue
        for outcome in market["tokens"]:
            token = outcome["token_id"]
            if token in available:
                ambiguous.add(token)
            else:
                available[token] = market
    for token in ambiguous:
        del available[token]
    markets = {
        t: available[t]
        for t, condition in frozen.items()
        if t in available and available[t]["condition_id"] == condition
    }
    report["unmapped_or_closed_requested_tokens"] = sorted(
        frozen.keys() - markets.keys()
    )
    remaining = available.keys() - frozen.keys()
    report["non_frozen_count"] = len(remaining)
    if archive.fixture_times(root, fixture) != [key[1]]:
        report["batch_error"] = "fixture kickoff changed or is ambiguous"
        markets = {}
    now = store.instant(archive.now_utc())
    stop = time.monotonic() + (store.instant(deadline) - now).total_seconds()
    in_window = store.instant(key[2]) <= now < store.instant(deadline)
    if in_window and markets:
        with closing(archive.database(root)) as db:
            prior = db.execute(
                "SELECT * FROM ma_execution_attempts WHERE fixture=? AND kickoff=? AND target=? AND leg='primary'",
                key,
            ).fetchone()
        pool = ThreadPoolExecutor(max_workers=2)
        jobs = []
        try:
            if not prior and _claim(root, key, "primary", markets):
                primary = pool.submit(_attempt, root, key, "primary", markets, deadline)
                jobs.append(primary)
                if research:
                    report["research_submitted"] = _research(
                        root, key, remaining, deadline
                    )
                done, _ = wait(
                    jobs, timeout=max(0, min(_HEDGE_AFTER, stop - time.monotonic()))
                )
                state, seen = primary.result() if done else ("sent", set())
                missing = markets.keys() - seen
            elif (
                prior
                and prior["status"] == "sent"
                and (now - store.instant(prior["started"])).total_seconds()
                >= _HEDGE_AFTER
            ):
                state, missing = (
                    "sent",
                    markets.keys(),
                )  # process died or first read is still slow
            elif prior and prior["status"] in (
                "failed",
                "invalid",
                "stale",
                "outside_slot",
                "received",
            ):
                # Resume partial originals even if a previous process stopped before its hedge.
                state = prior["status"]
                missing = markets.keys() - _select(root, key, markets, [prior]).keys()
            else:
                state, missing = "already_claimed", set()
            if (
                missing
                and state not in ("deferred", "rejected")
                and time.monotonic() < stop
                and _claim(root, key, "hedge", missing)
            ):
                jobs.append(
                    pool.submit(
                        _attempt,
                        root,
                        key,
                        "hedge",
                        {t: markets[t] for t in missing},
                        deadline,
                    )
                )
            if jobs:
                wait(jobs, timeout=max(0, stop - time.monotonic()))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    elif research and in_window and not frozen and report["batch_error"] is None:
        report["research_submitted"] = _research(root, key, remaining, deadline)
    with closing(archive.database(root)) as db:
        attempts = db.execute(
            "SELECT * FROM ma_execution_attempts WHERE fixture=? AND kickoff=? AND target=? ORDER BY started,leg",
            key,
        ).fetchall()
    selected = _select(root, key, markets, attempts)
    errors = [r["detail"] for r in attempts if r["detail"]]
    report.update(
        observations=[
            selected.get(t)
            or {"token_id": t, "error": "no valid in-window receipt for frozen token"}
            for t in sorted(frozen)
        ],
        batch_error=report["batch_error"]
        or (
            " | ".join(dict.fromkeys(errors))
            if errors and frozen.keys() - selected.keys()
            else None
        ),
        retry_at=max((r["retry_at"] for r in attempts if r["retry_at"]), default=None),
        attempt_latency_ms=[
            {"leg": r["leg"], "status": r["status"], "ms": r["elapsed_ms"]}
            for r in attempts
        ],
        hedge_started=any(r["leg"] == "hedge" for r in attempts),
    )
    return report
