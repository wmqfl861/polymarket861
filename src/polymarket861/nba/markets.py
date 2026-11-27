"""Read-only NBA Gamma/CLOB collector, bounded independently of football."""

import gzip
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from .. import market_provider as api
from . import storage
from .markets_contracts import (
    FAMILIES,
    contracts,
    eligibility,
    fixture,
    price,
    stamp,
    team_code,
)

WINDOWS = {"T-24h": 1440, "T-60m": 60, "T-15m": 15}
WIDTH = timedelta(minutes=5)
EAST = ZoneInfo("America/New_York")
MAX_RPM = 24
SCHEMA = """
CREATE TABLE IF NOT EXISTS nba_market_requests(
 id INTEGER PRIMARY KEY, sent TEXT NOT NULL, url TEXT NOT NULL, params TEXT, received TEXT,
 code INTEGER, sha TEXT, headers TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS nba_market_requests_sent ON nba_market_requests(sent);
CREATE TABLE IF NOT EXISTS nba_market_payloads(sha TEXT PRIMARY KEY, compressed BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS nba_market_state(key TEXT PRIMARY KEY, val TEXT);
CREATE TABLE IF NOT EXISTS nba_market_games(
 id TEXT PRIMARY KEY, game_id TEXT NOT NULL UNIQUE, home TEXT, away TEXT, home_id TEXT, away_id TEXT,
 kickoff TEXT NOT NULL, version INTEGER NOT NULL, first_seen TEXT NOT NULL, seen TEXT NOT NULL,
 started INTEGER NOT NULL, settle_after TEXT);
CREATE TABLE IF NOT EXISTS nba_market_schedule(
 id TEXT,version INTEGER,kickoff TEXT NOT NULL,valid_from TEXT NOT NULL,PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS nba_market_identity_conflicts(
 incident TEXT,party TEXT,id TEXT NOT NULL,game_id TEXT NOT NULL,home_id TEXT NOT NULL,away_id TEXT NOT NULL,
 home TEXT NOT NULL,away TEXT NOT NULL,kickoff TEXT NOT NULL,first_seen TEXT NOT NULL,receipt INTEGER,
 PRIMARY KEY(incident,party));
CREATE INDEX IF NOT EXISTS nba_market_conflict_fixture ON nba_market_identity_conflicts(home,away,kickoff);
CREATE TABLE IF NOT EXISTS nba_market_play(
 id TEXT PRIMARY KEY,observed TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nba_market_choices(
 id TEXT, version INTEGER, window TEXT, family TEXT, contract TEXT, probability REAL, observed TEXT NOT NULL,
 available TEXT NOT NULL, quality TEXT NOT NULL, receipt INTEGER, metadata_receipt INTEGER,
 PRIMARY KEY(id,version,window,family));
CREATE TABLE IF NOT EXISTS nba_market_close(
 id TEXT,version INTEGER,token TEXT,condition_id TEXT,price REAL,observed TEXT,available TEXT,receipt INTEGER,
 PRIMARY KEY(id,version,token,observed));
CREATE TABLE IF NOT EXISTS nba_market_settled(
 condition_id TEXT PRIMARY KEY,payouts TEXT,observed TEXT,receipt INTEGER,official_receipt INTEGER);
CREATE TABLE IF NOT EXISTS nba_market_runs(at TEXT PRIMARY KEY,completed TEXT,state TEXT,details TEXT);
"""


def _clock(now):
    if callable(now):
        return now
    return (
        (
            lambda: (
                api.moment(now)
                if not isinstance(now, datetime)
                else now.astimezone(UTC)
            )
        )
        if now is not None
        else lambda: datetime.now(UTC)
    )


def _state(db, key):
    row = db.execute("SELECT val FROM nba_market_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set(db, key, value):
    db.execute("INSERT OR REPLACE INTO nba_market_state VALUES (?,?)", (key, value))


class BudgetError(OSError):
    pass


def _read(db, clock, url, params=None):
    sent = clock()
    with db:
        db.execute("BEGIN IMMEDIATE")
        retry = _state(db, "retry_after")
        if retry and api.moment(retry) > sent:
            raise BudgetError("venue Retry-After in force")
        used = db.execute(
            "SELECT count(*) FROM nba_market_requests WHERE sent>?",
            (stamp(sent - timedelta(minutes=1)),),
        ).fetchone()[0]
        if used >= MAX_RPM:
            raise BudgetError("NBA shared per-minute request budget reached")
        rid = db.execute(
            "INSERT INTO nba_market_requests(sent,url,params) VALUES(?,?,?)",
            (stamp(sent), url, json.dumps(params)),
        ).lastrowid
    try:
        reply = api.get(url, params, timeout=12)
    except (OSError, ValueError) as e:
        code = e.code if isinstance(e, HTTPError) else None
        with db:
            db.execute(
                "UPDATE nba_market_requests SET received=?,code=?,error=? WHERE id=?",
                (stamp(clock()), code, str(e), rid),
            )
            if code == 429:
                now = clock()
                delay = (e.headers or {}).get("Retry-After", "60")
                try:
                    resume = now + timedelta(seconds=max(1, int(delay)))
                except (ValueError, OverflowError):
                    try:
                        resume = max(
                            now + timedelta(seconds=1), parsedate_to_datetime(delay)
                        )
                    except (TypeError, ValueError, OverflowError):
                        resume = now + timedelta(seconds=60)
                _set(db, "retry_after", stamp(resume))
        raise
    sha = hashlib.sha256(reply.body).hexdigest()
    with db:
        db.execute(
            "INSERT OR IGNORE INTO nba_market_payloads VALUES (?,?)",
            (sha, gzip.compress(reply.body, compresslevel=1, mtime=0)),
        )
        db.execute(
            "UPDATE nba_market_requests SET received=?,code=200,sha=?,headers=? WHERE id=?",
            (reply.received_at, sha, json.dumps(reply.headers), rid),
        )
    return json.loads(reply.body), rid, reply


_IDENTITY_FIELDS = ("id", "game_id", "home_id", "away_id", "home", "away")


def _disputed(db, event_id, game_id, home, away, kickoff, through):
    return (
        db.execute(
            "SELECT 1 FROM nba_market_identity_conflicts WHERE first_seen<=? AND "
            "(id=? OR game_id=? OR (home=? AND away=? AND kickoff=?)) LIMIT 1",
            (through, event_id, game_id, home, away, kickoff),
        ).fetchone()
        is not None
    )


def _upsert(db, event, seen, receipt=None, expected=None):
    g = fixture(event)
    ids = tuple(str(g["clubs"][s]["id"]) for s in ("home", "away"))
    identity = (g["id"], g["game_id"], *ids, g["home"], g["away"])
    rows = db.execute(
        "SELECT * FROM nba_market_games WHERE id=? OR game_id=?", identity[:2]
    ).fetchall()
    # A single reply may bridge two distinct registered IDs. Preserve both links.
    known = list(rows)
    if expected is not None:
        known.append(expected)
    known.extend(
        db.execute(
            "SELECT * FROM nba_market_identity_conflicts WHERE id=? OR game_id=?",
            identity[:2],
        )
    )
    conflict = False
    for other in known:
        previous = tuple(other[k] for k in _IDENTITY_FIELDS)
        if previous == identity:
            continue
        previous += (other["kickoff"],)
        claimed = (*identity, g["kickoff"])
        incident = hashlib.sha256(
            json.dumps(sorted((previous, claimed))).encode()
        ).hexdigest()
        for party, claim in (("previous", previous), ("received", claimed)):
            db.execute(
                "INSERT OR IGNORE INTO nba_market_identity_conflicts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (incident, party, *claim, seen, receipt),
            )
        conflict = True
    if conflict or _disputed(
        db, *identity[:2], g["home"], g["away"], g["kickoff"], seen
    ):
        return g, None
    row = rows[0] if rows else None
    if row:
        changed = row["kickoff"] != g["kickoff"]
        version = row["version"] + int(changed)
        db.execute(
            "UPDATE nba_market_games SET kickoff=?,version=?,seen=?,started=? WHERE id=?",
            (
                g["kickoff"],
                version,
                seen,
                bool(
                    row["started"]
                    or g["started"]
                    or (changed and seen >= row["kickoff"])
                ),
                g["id"],
            ),
        )
    else:
        version = 1
        db.execute(
            "INSERT INTO nba_market_games VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (
                g["id"],
                g["game_id"],
                g["home"],
                g["away"],
                *ids,
                g["kickoff"],
                version,
                seen,
                seen,
                g["started"],
            ),
        )
    db.execute(
        "INSERT OR IGNORE INTO nba_market_schedule VALUES(?,?,?,?)",
        (g["id"], version, g["kickoff"], seen),
    )
    reason = None
    if g["unavailable"]:
        reason = "venue_archived_closed_cancelled_or_inactive"
    elif g["started"]:
        reason = "venue_play_or_non_pregame_period_observed"
    elif row and changed and seen >= row["kickoff"]:
        reason = "venue_schedule_replaced_after_previous_tip"
    if reason:
        db.execute(
            "INSERT OR IGNORE INTO nba_market_play VALUES(?,?,?)",
            (g["id"], seen, reason),
        )
    return g, version


def _discover(db, clock):
    now = clock()
    cursor = None
    params = {
        "series_id": "10345",
        "tag_id": "100639",
        "active": "true",
        "closed": "false",
        "start_time_min": stamp(now - timedelta(hours=1)),
        "start_time_max": stamp(now + timedelta(hours=52)),
        "order": "startTime",
        "ascending": "true",
        "limit": 8,
    }
    cursors = set()
    conflicts = set()
    for _ in range(12):
        if cursor:
            params["after_cursor"] = cursor
        payload, receipt, reply = _read(db, clock, api.GAMMA + "/events/keyset", params)
        if (
            not api.fresh_observation(reply)
            or not isinstance(payload, dict)
            or not isinstance(payload.get("events"), list)
        ):
            raise ValueError("stale or unrecognized NBA event catalogue")
        with db:
            db.execute("BEGIN IMMEDIATE")
            for event in payload["events"]:
                if not isinstance(event, dict):
                    raise ValueError("malformed NBA event")
                try:
                    fixture(event)
                except (KeyError, ValueError, TypeError):
                    continue
                game, version = _upsert(db, event, stamp(clock()), receipt)
                if version is None:
                    conflicts.add(f"{game['id']}: venue_identity_conflict")
        cursor = payload.get("next_cursor")
        if not cursor:
            with db:
                _set(db, "catalogue_at", stamp(clock()))
            return sorted(conflicts)
        if cursor in cursors:
            raise ValueError("NBA catalogue cursor repeated")
        cursors.add(cursor)
    raise ValueError("NBA catalogue exceeded bounded pagination")


def _verified_event(db, clock, row):
    # Some edge caches serve the unqualified event URL for over a minute.
    # Use a minute bucket and still verify actual HTTP age and server time.
    event, rid, reply = _read(
        db,
        clock,
        api.GAMMA + "/events/" + row["id"],
        {"_observed_minute": int(clock().timestamp()) // 60},
    )
    if not isinstance(event, dict):
        raise ValueError("invalid event document")
    if not api.fresh_observation(reply):
        raise ValueError("stale event response")
    with db:
        db.execute("BEGIN IMMEDIATE")
        game, version = _upsert(db, event, stamp(clock()), rid, expected=row)
    if version is None:
        raise ValueError("venue_identity_conflict")
    if version != row["version"]:
        raise ValueError("schedule changed during capture")
    return event, game, rid


def _book_veto(db, game, observed, as_of):
    """Cross-check the venue and score feed as actually received at the book time.

    Later receipt/persistence of a final score cannot invalidate an earlier pregame book.
    An earlier raw observation can invalidate an old book on reread once persisted.
    """
    if _disputed(
        db,
        game["id"],
        None,
        game["home"],
        game["away"],
        game["kickoff"],
        min(observed, as_of),
    ):
        return "venue_identity_conflict"
    play = db.execute(
        "SELECT reason FROM nba_market_play WHERE id=? AND observed<=?",
        (game["id"], observed),
    ).fetchone()
    if play:
        return play[0]
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='nba_game_versions'"
    ).fetchone():
        return None
    exact = db.execute(
        "SELECT DISTINCT game_id,json_extract(payload,'$.home_abbr'),json_extract(payload,'$.away_abbr') "
        "FROM nba_game_versions WHERE source='balldontlie' AND received_at<=? AND available_at<=? "
        "AND json_extract(payload,'$.kickoff')=?",
        (observed, as_of, game["kickoff"]),
    )
    ids = {
        r[0]
        for r in exact
        if team_code(r[1]) == game["home"] and team_code(r[2]) == game["away"]
    }
    if not ids:
        return None
    if len(ids) != 1:
        return "ambiguous_score_source_fixture"
    history = db.execute(
        "SELECT payload FROM nba_game_versions WHERE source='balldontlie' AND game_id=? AND received_at<=? "
        "AND available_at<=? ORDER BY version",
        (next(iter(ids)), observed, as_of),
    )
    current = None
    for row in history:
        current = json.loads(row[0])
        if current.get("play_observed") or current["status"] in ("live", "final"):
            return "score_source_observed_play"
    if (
        current["kickoff"] != game["kickoff"]
        or team_code(current["home_abbr"]) != game["home"]
        or team_code(current["away_abbr"]) != game["away"]
        or current["status"] != "scheduled"
    ):
        return "score_source_schedule_conflict"
    return None


def _first_tip(db, event_id):
    row = db.execute(
        "SELECT kickoff FROM nba_market_schedule WHERE id=? ORDER BY version LIMIT 1",
        (event_id,),
    ).fetchone()
    return row[0] if row else None


def _contract_identity(c):
    # Previously frozen records may predate the parsed start/deadline display fields.
    return json.dumps(
        {k: v for k, v in c.items() if k not in ("original_kickoff", "start_deadline")},
        sort_keys=True,
    )


def _snapshot(db, clock, row, window):
    event, game, meta_id = _verified_event(db, clock, row)
    veto = _book_veto(db, game, stamp(clock()), stamp(clock()))
    if veto:
        raise ValueError(veto)
    if row["started"] or not game["pregame"]:
        raise ValueError("venue cannot verify pregame status")
    due = api.moment(row["kickoff"]) - timedelta(minutes=WINDOWS.get(window, 0))
    candidates, excluded = [], defaultdict(list)
    first = _first_tip(db, game["id"])
    checked = stamp(clock())
    for c in contracts(event, game, main_only=window != "CLOSE", native_only=True):
        reason = eligibility(c, game["kickoff"], checked, original_kickoff=first)
        if reason:
            excluded[c["family"]].append((c, reason))
        else:
            candidates.append(c)
    if window == "CLOSE":
        previous = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT contract FROM nba_market_choices WHERE id=? AND version=? AND contract IS NOT NULL AND available<>'pending'",
                (row["id"], row["version"]),
            )
        ]
        required = {_contract_identity(r) for r in previous}
        candidates = [r for r in candidates if _contract_identity(r) in required]
    tokens = sorted({t for c in candidates for t in (c["token_id"], c["opposite"])})
    observed, receipt, quotes = clock(), None, {}
    empty = set()
    if tokens:
        payload, receipt, reply = _read(
            db, clock, api.CLOB + "/books", {"token_ids": tokens}
        )
        if not api.fresh_observation(reply) or not isinstance(payload, list):
            raise ValueError("stale or malformed batch of books")
        indexed = {}
        for book in payload:
            if not isinstance(book, dict) or str(book.get("asset_id")) not in tokens:
                raise ValueError("unexpected asset in batch")
            t = str(book["asset_id"])
            if t in indexed:
                raise ValueError("duplicate asset in batch")
            indexed[t] = book
        observed = api.moment(reply.received_at)
        for c in candidates:
            quote = price(c, indexed, observed)
            if quote:
                quotes[c["condition_id"]] = quote
            elif any(
                t in indexed
                and indexed[t].get("market") == c["condition_id"]
                and (indexed[t].get("bids") == [] or indexed[t].get("asks") == [])
                for t in (c["token_id"], c["opposite"])
            ):
                empty.add(c["condition_id"])
    with db:
        db.execute("BEGIN IMMEDIATE")
        available = clock()
        veto = _book_veto(db, game, stamp(available), stamp(available))
        if veto:
            raise ValueError(veto)
        if not due - WIDTH <= observed <= available < due:
            raise ValueError("arrival or persistence outside decision window")
        if window == "CLOSE":
            for c in candidates:
                q = quotes.get(c["condition_id"])
                if q:
                    db.execute(
                        "INSERT OR IGNORE INTO nba_market_close VALUES(?,?,?,?,?,?,?,?)",
                        (
                            row["id"],
                            row["version"],
                            c["token_id"],
                            c["condition_id"],
                            q[1],
                            stamp(observed),
                            "pending",
                            receipt,
                        ),
                    )
        else:
            db.execute(
                "DELETE FROM nba_market_choices WHERE id=? AND version=? AND window=? AND available='pending'",
                (row["id"], row["version"], window),
            )
            for family in FAMILIES:
                same = [c for c in candidates if c["family"] == family]
                good = [c for c in same if c["condition_id"] in quotes]
                best = (
                    min(good, key=lambda c: quotes[c["condition_id"]][0])
                    if good
                    else None
                )
                quality = "unlisted"
                if same:
                    quality = (
                        "empty_book"
                        if all(c["condition_id"] in empty for c in same)
                        else "invalid_book"
                    )
                tracked = best
                if best:
                    quality = "ok"
                elif not same and excluded[family]:
                    # Keep the native condition to track actual venue payouts independently of game scores.
                    tracked, quality = min(
                        excluded[family], key=lambda x: x[0]["condition_id"]
                    )
                db.execute(
                    "INSERT OR IGNORE INTO nba_market_choices VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["id"],
                        row["version"],
                        window,
                        family,
                        json.dumps(tracked) if tracked else None,
                        quotes[best["condition_id"]][1] if best else None,
                        stamp(observed),
                        "pending",
                        quality,
                        receipt,
                        meta_id,
                    ),
                )
    # The first transaction durably stages the derived rows. Readers ignore pending
    # rows, including after a crash. Measure eligibility AFTER that commit returns.
    closing_book = window == "CLOSE"
    table = "nba_market_close" if closing_book else "nba_market_choices"
    selector = "receipt=?" if closing_book else "metadata_receipt=?"
    arg = receipt if closing_book else meta_id
    where = f" FROM {table} WHERE {selector} AND available='pending'"
    old_timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
    blocked = None
    try:
        db.execute("PRAGMA busy_timeout=0")
        with db:
            db.execute("BEGIN IMMEDIATE")
            upper = clock()
            blocked = _book_veto(db, game, stamp(upper), stamp(upper))
            if upper >= due or blocked:
                db.execute("DELETE" + where, (arg,))
            else:
                db.execute(
                    f"UPDATE {table} SET available=? WHERE {selector} AND available='pending'",
                    (stamp(upper), arg),
                )
    except sqlite3.OperationalError as e:
        raise ValueError("selection publication unavailable before cutoff") from e
    finally:
        db.execute(f"PRAGMA busy_timeout={int(old_timeout)}")
    if blocked or upper >= due:
        raise ValueError(
            blocked or "derived selection became durable after decision cutoff"
        )


def _settle(db, clock, row):
    if _disputed(
        db,
        row["id"],
        row["game_id"],
        row["home"],
        row["away"],
        row["kickoff"],
        stamp(clock()),
    ):
        raise ValueError("venue_identity_conflict")
    event, _, event_rid = _verified_event(db, clock, row)
    native = {m.get("conditionId"): m for m in event.get("markets", [])}
    saved = db.execute(
        "SELECT DISTINCT contract FROM nba_market_choices WHERE id=? AND contract IS NOT NULL AND available<>'pending' AND quality IN ('ok','rule_deadline_exceeded','rule_original_schedule_conflict')",
        (row["id"],),
    ).fetchall()
    pending = False
    for rec in saved:
        c = json.loads(rec[0])
        cid = c["condition_id"]
        if db.execute(
            "SELECT 1 FROM nba_market_settled WHERE condition_id=?", (cid,)
        ).fetchone():
            continue
        market = native.get(cid, {})
        raw_tokens = list(map(str, api.sequence(market.get("clobTokenIds"))))
        prices = api.sequence(market.get("outcomePrices"))
        if (
            market.get("closed") is not True
            or market.get("umaResolutionStatus") != "resolved"
            or len(prices) != 2
            or set(raw_tokens) != {c["token_id"], c["opposite"]}
            or market.get("description") != c["rules"]
        ):
            pending = True
            continue
        values = {
            t: float(api.number(v)) for t, v in zip(raw_tokens, prices, strict=True)
        }
        if sorted(values.values()) not in ([0.0, 1.0], [0.5, 0.5]):
            raise ValueError("nonterminal outcome vector")
        receipts = []
        for t, v in values.items():
            data, rid, _ = _read(db, clock, api.DATA + "/v2/tokens", {"token_id": t})
            resolution = api.resolution_row(data, t)
            if resolution is None or not resolution["resolved"]:
                break
            if (
                resolution["condition_id"] != cid
                or api.number(resolution["final_price"]) != v
            ):
                raise ValueError("venue payout conflict")
            receipts.append(rid)
        if len(receipts) == 2:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if _disputed(
                    db,
                    row["id"],
                    row["game_id"],
                    row["home"],
                    row["away"],
                    row["kickoff"],
                    stamp(clock()),
                ):
                    raise ValueError("venue_identity_conflict")
                db.execute(
                    "INSERT OR IGNORE INTO nba_market_settled VALUES(?,?,?,?,?)",
                    (
                        cid,
                        json.dumps(values),
                        stamp(clock()),
                        event_rid,
                        json.dumps(receipts),
                    ),
                )
        else:
            pending = True
    with db:
        retry = stamp(clock() + timedelta(hours=3)) if pending else "done"
        db.execute(
            "UPDATE nba_market_games SET settle_after=? WHERE id=?", (retry, row["id"])
        )


def _process(db, clock, errors):
    now = clock()
    runs = []
    for g in db.execute(
        "SELECT * FROM nba_market_games WHERE started=0 AND kickoff>? ORDER BY kickoff,id",
        (stamp(now),),
    ):
        for window, minutes in {**WINDOWS, "CLOSE": 0}.items():
            until = api.moment(g["kickoff"]) - timedelta(minutes=minutes)
            if not until - WIDTH <= now < until:
                continue
            if (
                window != "CLOSE"
                and db.execute(
                    "SELECT 1 FROM nba_market_choices WHERE id=? AND version=? AND window=? AND available<>'pending'",
                    (g["id"], g["version"], window),
                ).fetchone()
            ):
                continue
            if window == "CLOSE":
                if not db.execute(
                    "SELECT 1 FROM nba_market_choices WHERE id=? AND version=? AND quality='ok' AND available<>'pending'",
                    (g["id"], g["version"]),
                ).fetchone():
                    continue
                if db.execute(
                    "SELECT 1 FROM nba_market_close WHERE id=? AND version=? AND observed>? AND available<>'pending'",
                    (g["id"], g["version"], stamp(now - timedelta(seconds=45))),
                ).fetchone():
                    continue
            runs.append((until, g["id"], window, g))
    for _, _, window, g in sorted(runs):
        try:
            _snapshot(db, clock, g, window)
        except BudgetError:
            raise
        except (ValueError, KeyError, TypeError, OSError) as e:
            errors.append(f"{g['id']} {window}: {e}")


def collect(root, *, now=None):
    """One scheduling tick. The DB budget covers all callers sharing the NBA root."""
    clock = _clock(now)
    with closing(storage.connect(root)) as db:
        db.executescript(SCHEMA)
        started = stamp(clock())
        with db:
            db.execute("BEGIN IMMEDIATE")
            lease = _state(db, "lease")
            if lease and api.moment(lease) > clock():
                return {"state": "ok", "busy": True, "errors": []}
            lease = stamp(clock() + timedelta(minutes=10))
            _set(db, "lease", lease)
        errors = []
        try:
            _process(db, clock, errors)
            catalogue = _state(db, "catalogue_at")
            if not catalogue or clock() - api.moment(catalogue) >= timedelta(
                minutes=15
            ):
                try:
                    errors.extend(_discover(db, clock))
                    _process(db, clock, errors)
                except BudgetError:
                    raise
                except (ValueError, KeyError, TypeError, OSError) as e:
                    errors.append("catalogue: " + str(e))
            past = db.execute(
                "SELECT * FROM nba_market_games g WHERE kickoff<? AND (settle_after IS NULL OR settle_after<?) "
                "AND EXISTS (SELECT 1 FROM nba_market_choices c WHERE c.id=g.id AND quality IN ('ok','rule_deadline_exceeded','rule_original_schedule_conflict') AND available<>'pending') "
                "ORDER BY coalesce(settle_after,kickoff),id LIMIT 2",
                (stamp(clock() - timedelta(hours=2)), stamp(clock())),
            ).fetchall()
            for row in past:
                # Allow the rest of the queue to advance after a temporary provider failure.
                with db:
                    db.execute(
                        "UPDATE nba_market_games SET settle_after=? WHERE id=?",
                        (stamp(clock() + timedelta(minutes=5)), row["id"]),
                    )
                try:
                    _settle(db, clock, row)
                except BudgetError:
                    raise
                except (ValueError, KeyError, TypeError, OSError) as e:
                    errors.append(f"{row['id']} settlement: {e}")
        except (OSError, ValueError) as e:
            errors.append(str(e))
        finally:
            state = "degraded" if errors else "ok"
            with db:
                db.execute(
                    "DELETE FROM nba_market_state WHERE key='lease' AND val=?", (lease,)
                )
                db.execute(
                    "INSERT OR REPLACE INTO nba_market_runs VALUES(?,?,?,?)",
                    (started, stamp(clock()), state, json.dumps(errors)),
                )
        return {
            "state": state,
            "errors": errors,
            "at": started,
            "catalogue_at": _state(db, "catalogue_at"),
        }


def _ready(db):
    return (
        db is not None
        and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nba_market_choices'"
        ).fetchone()
    )


def fixture_conflict(root, home, away, kickoff, *, as_of=None):
    """Return affirmative venue evidence against a scheduled pregame prediction."""
    db = storage.open_existing(root)
    if db is None:
        return None
    with closing(db):
        if not _ready(db):
            return None
        end = stamp(_clock(as_of)())
        tip = (
            api.moment(kickoff)
            if not isinstance(kickoff, datetime)
            else kickoff.astimezone(UTC)
        )
        exact = stamp(tip)
        if _disputed(db, None, None, team_code(home), team_code(away), exact, end):
            return "venue_identity_conflict"
        day = tip.astimezone(EAST).date()
        versions = defaultdict(list)
        for row in db.execute(
            "SELECT g.id,s.version,s.kickoff,p.reason FROM nba_market_games g "
            "JOIN nba_market_schedule s ON s.id=g.id LEFT JOIN nba_market_play p ON p.id=g.id AND p.observed<=? "
            "WHERE g.home=? AND g.away=? AND s.valid_from<=? ORDER BY s.version",
            (end, team_code(home), team_code(away), end),
        ):
            versions[row["id"]].append(row)
        candidates = [
            v for v in versions.values() if any(r["kickoff"] == exact for r in v)
        ]
        if not candidates:
            # Detect a same-pair/same-day conflicting start even without a prior exact timestamp.
            possible = [
                v
                for v in versions.values()
                if api.moment(v[-1]["kickoff"]).astimezone(EAST).date() == day
            ]
            return "venue_same_pair_schedule_mismatch" if possible else None
        if len(candidates) > 1:
            return "ambiguous_venue_fixture"
        current = candidates[0][-1]
        if _disputed(
            db, current["id"], None, team_code(home), team_code(away), exact, end
        ):
            return "venue_identity_conflict"
        if current["kickoff"] != exact:
            return "venue_schedule_replaced"
        return current["reason"]


def benchmark(root, home, away, kickoff, horizon, *, as_of=None):
    """Frozen, research-only home/over references for precisely matched teams and time."""
    db = storage.open_existing(root)
    if db is None:
        return {}
    with closing(db):
        if not _ready(db) or horizon not in WINDOWS:
            return {}
        end = stamp(_clock(as_of)())
        kick = stamp(
            api.moment(kickoff)
            if not isinstance(kickoff, datetime)
            else kickoff.astimezone(UTC)
        )
        games = db.execute(
            "SELECT g.id,g.home,g.away,s.kickoff,s.version FROM nba_market_games g JOIN nba_market_schedule s ON s.id=g.id "
            "WHERE g.home=? AND g.away=? AND s.kickoff=? AND s.version=(SELECT max(v.version) "
            "FROM nba_market_schedule v WHERE v.id=g.id AND v.valid_from<=?)",
            (team_code(home), team_code(away), kick, end),
        ).fetchall()
        if len(games) != 1:
            return {}
        game = games[0]
        first = _first_tip(db, game["id"])
        result = {}
        for row in db.execute(
            "SELECT * FROM nba_market_choices WHERE id=? AND version=? AND window=? "
            "AND quality='ok' AND available<=?",
            (game["id"], game["version"], horizon, end),
        ):
            if _book_veto(db, game, row["available"], end):
                continue
            c = json.loads(row["contract"])
            if eligibility(c, kick, row["available"], original_kickoff=first):
                continue
            close, close_reason = None, None
            for observed in db.execute(
                "SELECT * FROM nba_market_close WHERE id=? AND version=? AND token=? "
                "AND condition_id=? AND available<=? ORDER BY observed DESC",
                (game["id"], game["version"], c["token_id"], c["condition_id"], end),
            ):
                close_reason = eligibility(
                    c, kick, observed["available"], original_kickoff=first
                ) or _book_veto(db, game, observed["available"], end)
                if not close_reason:
                    close = observed
                    break
            result[row["family"]] = {
                "condition_id": c["condition_id"],
                "token_id": c["token_id"],
                "line": c["line"],
                "probability": row["probability"],
                "observed_at": row["observed"],
                "available_at": row["available"],
                "close_probability": close["price"] if close and end >= kick else None,
                "close_at": close["observed"] if close and end >= kick else None,
                "close_excluded_reason": close_reason,
            }
        return result


def status(root, *, now=None):
    clock = _clock(now)
    db = storage.open_existing(root)
    if db is None:
        return {
            "state": "unavailable",
            "days": {},
            "latest_attempt": None,
            "last_success": None,
        }
    with closing(db):
        if not _ready(db):
            return {
                "state": "unavailable",
                "days": {},
                "latest_attempt": None,
                "last_success": None,
            }
        days = defaultdict(Counter)
        now_dt = clock()
        for game in db.execute("SELECT * FROM nba_market_games ORDER BY kickoff"):
            kickoff = api.moment(game["kickoff"])
            day = str(kickoff.astimezone(EAST).date())
            stats = days[day]
            stats["catalogued_games"] += 1
            for window, minutes in WINDOWS.items():
                if now_dt < kickoff - timedelta(minutes=minutes):
                    continue
                stats["due"] += 4
                rows = db.execute(
                    "SELECT quality FROM nba_market_choices WHERE id=? AND version=? AND window=? AND available<>'pending'",
                    (game["id"], game["version"], window),
                ).fetchall()
                stats["missed"] += 4 - len(rows)
                for r in rows:
                    stats[{"ok": "collected"}.get(r[0], r[0])] += 1
        for row in db.execute("SELECT sent FROM nba_market_requests WHERE code=429"):
            days[str(api.moment(row[0]).astimezone(EAST).date())]["http_429"] += 1
        for row in db.execute("SELECT at,state FROM nba_market_runs"):
            stats = days[str(api.moment(row[0]).astimezone(EAST).date())]
            stats["ticks"] += 1
            stats["degraded_ticks"] += int(row[1] != "ok")
        for row in db.execute(
            "SELECT DISTINCT s.condition_id,g.kickoff FROM nba_market_settled s "
            "JOIN nba_market_choices c ON json_extract(c.contract,'$.condition_id')=s.condition_id "
            "JOIN nba_market_games g ON c.id=g.id"
        ):
            days[str(api.moment(row[1]).astimezone(EAST).date())][
                "settled_conditions"
            ] += 1
        prev = db.execute(
            "SELECT * FROM nba_market_runs ORDER BY at DESC LIMIT 1"
        ).fetchone()
        success = db.execute(
            "SELECT max(completed) FROM nba_market_runs WHERE state='ok'"
        ).fetchone()[0]
        catalogue = _state(db, "catalogue_at")
        state = "unavailable" if prev is None else prev["state"]
        if prev and (
            not catalogue
            or now_dt - api.moment(prev["completed"]) > timedelta(minutes=5)
            or now_dt - api.moment(catalogue) > timedelta(minutes=20)
        ):
            state = "degraded"
        fields = (
            "catalogued_games",
            "due",
            "collected",
            "empty_book",
            "invalid_book",
            "unlisted",
            "rule_deadline_exceeded",
            "rule_original_schedule_conflict",
            "missed",
            "http_429",
            "ticks",
            "degraded_ticks",
            "settled_conditions",
        )
        return {
            "state": state,
            "latest_attempt": prev["at"] if prev else None,
            "last_success": success,
            "last_errors": json.loads(prev["details"]) if prev else [],
            "catalogue_at": catalogue,
            "request_budget_per_minute": MAX_RPM,
            "denominator": "catalogued NBA games x three elapsed windows x four families",
            "days": {k: {f: v[f] for f in fields} for k, v in sorted(days.items())},
        }


def register(subparsers):
    market = subparsers.add_parser("markets", help="Read-only NBA market snapshots")
    commands = market.add_subparsers(dest="nba_markets_command", required=True)
    for name, fn in (("collect", collect), ("status", status)):
        parser = commands.add_parser(name)
        parser.set_defaults(func=lambda args, f=fn: f(args.data_dir))
