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
    contract_inventory,
    eligibility,
    fixture,
    price,
    stamp,
    spread_archives,
    selected_line,
    team_code,
)

from .markets_schedule import (
    MATCHED,
    PAIR_TOLERANCE as PAIR_TOLERANCE,
    disputed as _disputed,
    fixture_conflict as fixture_conflict,
    kickoff_differences,
    schedule_match as schedule_match,
    schedule_match_db,
    score_veto,
    current_score_tip,
    source_epoch,
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
CREATE TABLE IF NOT EXISTS nba_market_catalogues(
 completed TEXT PRIMARY KEY,start_bound TEXT NOT NULL,end_bound TEXT NOT NULL,receipts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nba_market_pregame(
 id TEXT,version INTEGER,seen TEXT NOT NULL,receipt INTEGER NOT NULL,PRIMARY KEY(id,version));
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
CREATE TABLE IF NOT EXISTS nba_market_contract_issues(
 metadata_receipt INTEGER,family TEXT,reason TEXT,count INTEGER NOT NULL,
 PRIMARY KEY(metadata_receipt,family,reason));
CREATE TABLE IF NOT EXISTS nba_market_spread_archives(
 id TEXT,version INTEGER,window TEXT,condition_id TEXT,metadata_receipt INTEGER,book_receipt INTEGER,
 observed TEXT NOT NULL,PRIMARY KEY(id,version,window,condition_id,book_receipt));
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
    if (
        receipt is not None
        and g["pregame"]
        and seen < g["kickoff"]
        and not (row and (row["started"] or (changed and seen >= row["kickoff"])))
    ):
        db.execute(
            "INSERT OR IGNORE INTO nba_market_pregame VALUES(?,?,?,?)",
            (g["id"], version, seen, receipt),
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
    receipts = []
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
        receipts.append(receipt)
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
                completed = stamp(clock())
                _set(db, "catalogue_at", completed)
                db.execute(
                    "INSERT OR IGNORE INTO nba_market_catalogues VALUES(?,?,?,?)",
                    (
                        completed,
                        params["start_time_min"],
                        params["start_time_max"],
                        json.dumps(receipts),
                    ),
                )
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
    return score_veto(db, game, observed, as_of)


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


def _paired_key(horizon, source_tip, floor):
    # The original native window is immutable. Each actual score schedule revision
    # can acquire one separate basket from the intersection of the two named windows.
    return "|".join(("paired", horizon, source_tip, floor or "first"))


def _paired_window(db, game, horizon, now):
    end = stamp(now)
    source = current_score_tip(db, game, end)
    if not source:
        return None
    match = schedule_match_db(db, game["home"], game["away"], source, as_of=end)
    if (
        match["status"] not in MATCHED
        or match["reason"]
        or match["venue_event_id"] != game["id"]
        or match["venue_version"] != game["version"]
    ):
        return None
    offset = timedelta(minutes=WINDOWS[horizon])
    native = api.moment(game["kickoff"])
    lower = max(native, api.moment(source)) - offset - WIDTH
    upper = min(native, api.moment(source)) - offset
    if not lower <= now < upper:
        return None
    eligible, floor = source_epoch(db, game["home"], game["away"], source, end)
    if not eligible:
        return None
    return (
        _paired_key(horizon, source, floor),
        max(lower, api.moment(floor)) if floor else lower,
        upper,
    )


def _snapshot(db, clock, row, window, paired=None):
    event, game, meta_id = _verified_event(db, clock, row)
    veto = _book_veto(db, game, stamp(clock()), stamp(clock()))
    if veto:
        raise ValueError(veto)
    if row["started"] or not game["pregame"]:
        raise ValueError("venue cannot verify pregame status")
    due = api.moment(row["kickoff"]) - timedelta(minutes=WINDOWS.get(window, 0))
    label, lower = window, due - WIDTH
    if paired:
        if _paired_window(db, row, window, clock()) != paired:
            raise ValueError("paired schedule or window changed before capture")
        label, lower, due = paired
    candidates, excluded = [], defaultdict(list)
    first = _first_tip(db, game["id"])
    checked = stamp(clock())
    inventory, issues = contract_inventory(
        event, game, main_only=False, native_only=True
    )
    with db:
        db.executemany(
            "INSERT OR IGNORE INTO nba_market_contract_issues VALUES(?,?,?,?)",
            [
                (meta_id, family, reason, n)
                for family, found in issues.items()
                for reason, n in found.items()
            ]
            + [(meta_id, family, "checked", 1) for family in FAMILIES],
        )
    for c in inventory:
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
    # Core price collection always has priority. Spread books can share a core HTTP request
    # when both native tokens fit under the venue limit. They never become selections.
    if len(tokens) > 500:
        raise ValueError("full-game candidate tokens exceed the native batch limit")
    core_tokens = set(tokens)
    spreads, spill = [], set()
    for candidate in [] if paired else sorted(spread_archives(event, game)):
        extra = set(candidate[1:])
        if (
            core_tokens
            and not (extra & (core_tokens | spill))
            and len(core_tokens | spill | extra) <= 500
        ):
            spreads.append(candidate)
            spill.update(extra)
    tokens = sorted(core_tokens | spill)
    observed, receipt, quotes, indexed = clock(), None, {}, {}

    empty = set()
    if tokens:
        payload, receipt, reply = _read(
            db, clock, api.CLOB + "/books", {"token_ids": tokens}
        )
        if not api.fresh_observation(reply) or not isinstance(payload, list):
            raise ValueError("stale or malformed batch of books")
        bad_spreads = set()
        for book in payload:
            if not isinstance(book, dict) or str(book.get("asset_id")) not in tokens:
                raise ValueError("unexpected asset in batch")
            t = str(book["asset_id"])
            if t in indexed:
                if t in spill:
                    bad_spreads.add(t)
                    continue
                raise ValueError("duplicate asset in batch")
            indexed[t] = book
        for t in bad_spreads:
            indexed.pop(t)
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
        if not lower <= observed <= available < due:
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
                (row["id"], row["version"], label),
            )
            for family in FAMILIES:
                same = [c for c in candidates if c["family"] == family]
                best = selected_line(same, quotes)
                quality = "unlisted"
                for kind in (
                    "rule_template_mismatch",
                    "unverified_contract",
                    "listed_inactive",
                ):
                    if issues[family][kind]:
                        quality = kind
                        break
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
                        label,
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
            if paired and _paired_window(db, row, window, upper) != paired:
                blocked = (
                    blocked or "paired schedule or window changed before publication"
                )
            if upper >= due or blocked:
                db.execute("DELETE" + where, (arg,))
            else:
                for condition, first_token, second_token in spreads:
                    if all(
                        indexed.get(t, {}).get("market") == condition
                        for t in (first_token, second_token)
                    ):
                        db.execute(
                            "INSERT OR IGNORE INTO nba_market_spread_archives VALUES(?,?,?,?,?,?,?)",
                            (
                                row["id"],
                                row["version"],
                                label,
                                condition,
                                meta_id,
                                receipt,
                                stamp(observed),
                            ),
                        )
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

    return [
        f"{family} {reason} ({n})"
        for family, found in sorted(issues.items())
        for reason, n in sorted(found.items())
        if reason in {"rule_template_mismatch", "unverified_contract"}
    ]


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
            paired = None
            if window != "CLOSE":
                original = db.execute(
                    """SELECT observed,available FROM nba_market_choices WHERE id=? AND version=?
                    AND window=? AND available<>'pending' LIMIT 1""",
                    (g["id"], g["version"], window),
                ).fetchone()
                if original:
                    paired = _paired_window(db, g, window, now)
                    if not paired:
                        continue
                    label, lower, until = paired
                    if (
                        stamp(lower)
                        <= original["observed"]
                        <= original["available"]
                        < stamp(until)
                    ):
                        continue
                    if db.execute(
                        "SELECT 1 FROM nba_market_choices WHERE id=? AND version=? AND window=? AND available<>'pending'",
                        (g["id"], g["version"], label),
                    ).fetchone():
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
            runs.append((until, g["id"], window, g, paired))
    for _, _, window, g, paired in sorted(runs):
        try:
            errors.extend(
                f"{g['id']} {window}: {reason}"
                for reason in _snapshot(db, clock, g, window, paired)
            )
        except BudgetError:
            raise
        except (ValueError, KeyError, TypeError, OSError) as e:
            errors.append(f"{g['id']} {window}: {e}")


def collect(root, *, now=None):
    """One scheduling tick. The DB budget covers all callers sharing the NBA root."""
    from . import reconciliation

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
            try:
                reconciliation.collect_native_only(db, stamp(clock()))
            except (sqlite3.Error, ValueError, OSError) as exc:
                errors.append(f"payout reconciliation: {exc}")
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


def benchmark(root, home, away, kickoff, horizon, *, as_of=None, source_epoch_at=None):
    """Frozen, research-only home/over references for precisely matched teams and time."""
    db = storage.open_existing(root)
    if db is None:
        return {}
    with closing(db):
        if not _ready(db) or horizon not in WINDOWS:
            return {}
        end = stamp(_clock(as_of)())
        match = schedule_match_db(db, home, away, kickoff, as_of=end)
        if match["status"] not in MATCHED:
            return {}
        game = dict(
            id=match["venue_event_id"],
            version=match["venue_version"],
            kickoff=match["venue_kickoff"],
            home=team_code(home),
            away=team_code(away),
        )
        kick = match["source_kickoff"]
        native = match["venue_kickoff"]
        native_due = api.moment(native) - timedelta(minutes=WINDOWS[horizon])
        source_due = api.moment(kick) - timedelta(minutes=WINDOWS[horizon])
        due = min(native_due, source_due)
        earliest = max(native_due, source_due) - WIDTH
        compatible, floor = source_epoch(
            db, game["home"], game["away"], kick, min(end, stamp(source_due))
        )
        paired_label = _paired_key(horizon, kick, floor)
        if source_epoch_at is not None:
            floor = max(floor or "", stamp(api.moment(source_epoch_at)))
        if not compatible or earliest >= due:
            return {}
        close_compatible, close_floor = source_epoch(
            db, game["home"], game["away"], kick, min(end, min(kick, native))
        )
        first = _first_tip(db, game["id"])
        result = {}
        for row in db.execute(
            "SELECT * FROM nba_market_choices WHERE id=? AND version=? AND window IN (?,?) "
            "AND quality='ok' AND available<=? ORDER BY available,family",
            (game["id"], game["version"], horizon, paired_label, end),
        ):
            if row["family"] in result:
                continue
            observed_at, available_at = (
                api.moment(row["observed"]),
                api.moment(row["available"]),
            )
            if not earliest <= observed_at <= available_at < due or (
                floor and row["observed"] < floor
            ):
                continue
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
                close_observed = api.moment(observed["observed"])
                close_available = api.moment(observed["available"])
                if (
                    not max(api.moment(native), api.moment(kick)) - WIDTH
                    <= close_observed
                    <= close_available
                    < min(api.moment(native), api.moment(kick))
                ):
                    close_reason = "outside_paired_pregame_close"
                    continue
                close_reason = eligibility(
                    c, kick, observed["available"], original_kickoff=first
                ) or _book_veto(db, game, observed["available"], end)
                if not close_reason and (
                    not close_compatible
                    or (close_floor and observed["observed"] < close_floor)
                    or (floor and observed["observed"] < floor)
                ):
                    close_reason = "score_schedule_replaced"
                if not close_reason:
                    close = observed
                    break
            result[row["family"]] = {
                "source_kickoff": kick,
                "venue_kickoff": native,
                "venue_event_id": game["id"],
                "venue_version": game["version"],
                "delta_seconds": match["delta_seconds"],
                "condition_id": c["condition_id"],
                "token_id": c["token_id"],
                "line": c["line"],
                "probability": row["probability"],
                "observed_at": row["observed"],
                "available_at": row["available"],
                "close_probability": close["price"]
                if close and end >= max(kick, native)
                else None,
                "close_at": close["observed"]
                if close and end >= max(kick, native)
                else None,
                "close_excluded_reason": close_reason,
            }
        return result


def _contract_alerts(db, now):
    """Keep a failed template visible between decision ticks until a newer check or 24 hours."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='nba_market_contract_issues'"
    ).fetchone():
        return []
    return [
        dict(
            event_id=r["url"].rsplit("/", 1)[-1],
            family=r["family"],
            reason=r["reason"],
            count=r["count"],
            observed_at=r["received"],
            metadata_receipt=r["id"],
        )
        for r in db.execute(
            """WITH last_check AS (
             SELECT q.url,i.family,max(q.id) AS id FROM nba_market_requests q
             JOIN nba_market_contract_issues i ON i.metadata_receipt=q.id
             WHERE q.received BETWEEN ? AND ? AND i.reason='checked' GROUP BY q.url,i.family)
             SELECT q.url,s.family,i.reason,i.count,q.received,q.id FROM last_check s
             JOIN nba_market_requests q ON q.id=s.id
             JOIN nba_market_contract_issues i ON i.metadata_receipt=s.id AND i.family=s.family
             WHERE i.reason IN ('rule_template_mismatch','unverified_contract') ORDER BY q.id,s.family,i.reason""",
            (stamp(now - timedelta(days=1)), stamp(now)),
        )
    ]


def status(root, *, now=None):
    from . import reconciliation

    clock = _clock(now)
    db = storage.open_existing(root)
    if db is None:
        return {
            "state": "unavailable",
            "days": {},
            "latest_attempt": None,
            "last_success": None,
            "payout_reconciliation": reconciliation.summary(None),
        }
    with closing(db):
        if not _ready(db):
            return {
                "state": "unavailable",
                "days": {},
                "latest_attempt": None,
                "last_success": None,
                "payout_reconciliation": reconciliation.summary(db),
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
        for table, query, field in (
            (
                "nba_market_contract_issues",
                "SELECT q.received, sum(i.count) FROM nba_market_contract_issues i "
                "JOIN nba_market_requests q ON q.id=i.metadata_receipt WHERE i.reason='rule_template_mismatch' "
                "GROUP BY i.metadata_receipt",
                "rule_template_rejections",
            ),
            (
                "nba_market_spread_archives",
                "SELECT observed,count(*) FROM nba_market_spread_archives GROUP BY observed",
                "spread_books_archived",
            ),
        ):
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (table,)
            ).fetchone():
                for at, n in db.execute(query):
                    days[str(api.moment(at).astimezone(EAST).date())][field] += n
        prev = db.execute(
            "SELECT * FROM nba_market_runs ORDER BY at DESC LIMIT 1"
        ).fetchone()
        success = db.execute(
            "SELECT max(completed) FROM nba_market_runs WHERE state='ok'"
        ).fetchone()[0]
        catalogue = _state(db, "catalogue_at")
        state = "unavailable" if prev is None else prev["state"]
        alerts = _contract_alerts(db, now_dt)
        if prev and (
            alerts
            or not catalogue
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
            "rule_template_mismatch",
            "rule_template_rejections",
            "listed_inactive",
            "unverified_contract",
            "spread_books_archived",
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
            "contract_alerts": alerts,
            "payout_reconciliation": reconciliation.summary(db),
            "denominator": "catalogued NBA games x three elapsed windows x four families",
            "days": {k: {f: v[f] for f in fields} for k, v in sorted(days.items())},
        }


def register(subparsers):
    market = subparsers.add_parser("markets", help="Read-only NBA market snapshots")
    commands = market.add_subparsers(dest="nba_markets_command", required=True)
    for name, fn in (
        ("collect", collect),
        ("status", status),
        ("kickoff-differences", kickoff_differences),
    ):
        parser = commands.add_parser(name)
        parser.set_defaults(func=lambda args, f=fn: f(args.data_dir))
