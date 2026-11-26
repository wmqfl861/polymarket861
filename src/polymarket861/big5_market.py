"""Independent, anonymous Gamma/CLOB research archive for the five domestic leagues."""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from . import big5_market_receipts as receipts
from . import big5_market_schedule as schedule
from . import market_archive as archive
from . import market_provider as provider
from . import market_watch_health
from .store import canonical, iso, now_utc

MARKETS = provider.SPORT_MARKETS
NEW_LEAGUES = ("lal", "bun", "sea", "fl1")
CORE = (
    "moneyline.home",
    "moneyline.draw",
    "moneyline.away",
    "total.2.5",
    "home_total.1.5",
    "away_total.1.5",
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS big5_events (
 slug TEXT PRIMARY KEY, fixture TEXT NOT NULL, sport TEXT NOT NULL, kickoff TEXT NOT NULL,
 home_id TEXT NOT NULL, away_id TEXT NOT NULL, event_id TEXT NOT NULL, receipt TEXT NOT NULL, sha TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS big5_window ON big5_events(sport,kickoff);
CREATE TABLE IF NOT EXISTS big5_event_delays (
 slug TEXT PRIMARY KEY,receipt TEXT NOT NULL,sha TEXT NOT NULL,delays TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS big5_slots (
 fixture TEXT NOT NULL,kickoff TEXT NOT NULL,slot TEXT NOT NULL,expires TEXT NOT NULL,
 attempts INTEGER NOT NULL,success INTEGER NOT NULL DEFAULT 0,report TEXT,
 PRIMARY KEY(fixture,kickoff,slot));
CREATE TABLE IF NOT EXISTS big5_runs (
 id INTEGER PRIMARY KEY, sport TEXT NOT NULL, fixture TEXT NOT NULL, phase TEXT NOT NULL,
 started TEXT NOT NULL, finished TEXT NOT NULL, report TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS big5_run_time ON big5_runs(finished,sport);
CREATE INDEX IF NOT EXISTS big5_fixed_runs ON big5_runs(fixture,phase,started);
CREATE TABLE IF NOT EXISTS big5_last_dispatch(
 fixture TEXT NOT NULL, phase TEXT NOT NULL, started TEXT NOT NULL, PRIMARY KEY(fixture,phase));
"""


def research_root(data_dir):
    return Path(data_dir) / "five-league-market"


def _db(root):
    from . import dbscope

    db = archive.database(root)
    try:
        dbscope.prepare(db, "five-league-market-v1", SCHEMA)
        dbscope.prepare(db, "five-league-market-schedule-v1", schedule.SCHEMA)
        schedule.recover(root, db, _identity)
        schedule.recover_play(root, db, _identity)
    except BaseException:
        db.close()
        raise
    return db


def _sports_row(sports, league):
    series, tag = MARKETS[league]
    rows = [r for r in sports if isinstance(r, dict) and r.get("sport") == league]
    if len(rows) != 1:
        raise ValueError("missing or ambiguous Gamma sport")
    row = rows[0]
    if str(row.get("series")) != series or tag not in str(row.get("tags", "")).split(
        ","
    ):
        raise ValueError("live sport/series/tag differs from verified association")
    return {
        "sport": league,
        "series": series,
        "tag": tag,
        "primary_tag": str(row.get("primaryTagId")),
    }


def _identity(event, league, *, history=None):
    """Require three independent Gamma fields and both domestic team identities."""
    if not isinstance(event, dict):
        raise ValueError("invalid_event")
    match = provider.SPORT_FIXTURES[league].fullmatch(str(event.get("slug", "")))
    if not match or not str(event.get("id", "")).isdigit():
        raise ValueError("not_a_domestic_fixture_slug")
    series, tag = MARKETS[league]
    sport = event.get("sport")
    if (
        not isinstance(sport, dict)
        or sport.get("sport") != league
        or str(sport.get("series")) != series
    ):
        raise ValueError("sport_mismatch")
    if {str(x.get("id")) for x in event.get("series") or [] if isinstance(x, dict)} != {
        series
    }:
        raise ValueError("series_mismatch")
    if tag not in {
        str(x.get("id")) for x in event.get("tags") or [] if isinstance(x, dict)
    }:
        raise ValueError("tag_mismatch")
    rows = event.get("teams") or []
    teams = provider.teams(event)
    if len(rows) != 2 or set(teams) != {"home", "away"}:
        raise ValueError("missing_team_pair")
    ids = []
    for side, abbreviation in zip(("home", "away"), match.group(2, 3)):
        team = teams[side]
        if (
            team.get("league") != league
            or str(team.get("abbreviation", "")).lower() != abbreviation
            or not str(team.get("id", "")).isdigit()
        ):
            raise ValueError("team_membership_mismatch")
        ids.append(str(team["id"]))
    if ids[0] == ids[1]:
        raise ValueError("identical_team_ids")
    published = event.get("startTime")
    if not published and history is not None and schedule.stopped(event):
        prior = history.execute(
            "SELECT kickoff FROM big5_events WHERE slug=? AND event_id=? AND home_id=? AND away_id=?",
            (event["slug"], str(event["id"]), *ids),
        ).fetchone()
        if prior:
            published = prior[0]
    try:
        kickoff = iso(provider.moment(published))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("missing_scheduled_kickoff") from exc
    return (event["slug"], match[1], league, kickoff, *ids, str(event["id"]))


def _record(db, event, identity, reply, sha):
    previous = db.execute(
        "SELECT home_id,away_id,event_id FROM big5_events WHERE slug=?", (identity[0],)
    ).fetchone()
    if previous and tuple(previous) != identity[4:7]:
        raise ValueError("existing event ID or domestic participant IDs changed")
    listings = event.get("markets") or []
    if not isinstance(listings, list) or any(
        not isinstance(m, dict) or not isinstance(m.get("slug"), str) or not m["slug"]
        for m in listings
    ):
        raise ValueError("unidentifiable contract in domestic event")
    archive._ingest(db, event, reply, sha, sport=identity[2])
    delays = schedule.delays(event)
    schedule.record(
        db,
        identity,
        reply.received_at,
        sha,
        delays,
        schedule.stopped(event),
        "validated_receipt",
        event=event,
    )
    db.execute(
        "INSERT INTO big5_event_delays VALUES(?,?,?,?) ON CONFLICT(slug) DO UPDATE SET "
        "receipt=excluded.receipt,sha=excluded.sha,delays=excluded.delays "
        "WHERE (excluded.receipt,excluded.sha)>(big5_event_delays.receipt,big5_event_delays.sha)",
        (identity[0], reply.received_at, sha, canonical(delays)),
    )
    db.execute(
        "INSERT INTO big5_events VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET "
        "kickoff=excluded.kickoff,home_id=excluded.home_id,away_id=excluded.away_id,"
        "event_id=excluded.event_id,receipt=excluded.receipt,sha=excluded.sha "
        "WHERE (excluded.receipt,excluded.sha)>(big5_events.receipt,big5_events.sha)",
        (*identity, reply.received_at, sha),
    )


def _error(exc):
    return {
        "error": str(exc),
        "retry_at": getattr(exc, "retry_at", None),
        "http_status": getattr(exc, "code", None) or getattr(exc, "http_status", None),
    }


def _args(leagues, start, end):
    selected = list(dict.fromkeys(leagues or NEW_LEAGUES))
    first, last = (iso(provider.moment(x)) for x in (start, end))
    if any(s not in MARKETS for s in selected) or first >= last:
        raise ValueError("select known domestic leagues and a positive UTC window")
    return selected, first, last


def _catalog_one(root, league, start, end, pages, page_size, offset, sports):
    initial_offset = offset
    identity = _sports_row(sports, league)
    report = {
        **identity,
        "pages": [],
        "page_walk_complete": False,
        "next_offset": offset,
        "reached_end": False,
        "fixtures": [],
        "rejected_events": {},
        "gaps": [],
    }
    domestic, rejected, seen = defaultdict(set), Counter(), set()
    for _ in range(pages):
        params = {
            "tag_id": identity["tag"],
            "limit": page_size,
            "offset": offset,
            "order": "id",
            "ascending": "true",
            "end_date_min": start,
            "end_date_max": end,
        }
        try:
            events, reply, sha = archive._fetch(
                root, "league_catalog", league, provider.GAMMA + "/events", params
            )
            if not isinstance(events, list):
                raise ValueError("catalog response must be a list")
            report["pages"].append(
                {
                    "offset": offset,
                    "rows": len(events),
                    "sha256": sha,
                    "received_at": reply.received_at,
                    "url": reply.url,
                }
            )
            with closing(_db(root)) as db, db:
                for event in events:
                    event_id = (
                        str(event.get("id", "")) if isinstance(event, dict) else ""
                    )
                    if event_id and event_id in seen:
                        rejected["repeated_event_id"] += 1
                        continue
                    seen.add(event_id)
                    try:
                        fields = _identity(event, league, history=db)
                        in_window = start <= fields[3] < end
                        if (
                            not in_window
                            and not db.execute(
                                "SELECT 1 FROM big5_events WHERE slug=?", (fields[0],)
                            ).fetchone()
                        ):
                            raise ValueError("scheduled_kickoff_outside_window")
                        # An invalid market must not leave a half-ingested event.
                        db.execute("SAVEPOINT league_event")
                        try:
                            _record(db, event, fields, reply, sha)
                        except Exception:
                            db.execute("ROLLBACK TO league_event")
                            raise
                        finally:
                            db.execute("RELEASE league_event")
                        if in_window:
                            domestic[fields[1]].add(fields[0])
                        else:
                            rejected["known_fixture_moved_outside_window"] += 1
                    except (TypeError, ValueError, KeyError) as exc:
                        rejected[str(exc)] += 1
            offset += len(events)
            report["next_offset"] = offset
            if len(events) < page_size:
                report["reached_end"] = True
                if not rejected["repeated_event_id"]:
                    report["page_walk_complete"] = initial_offset == 0
                    report["next_offset"] = None
                else:
                    report["gaps"].append(
                        {
                            "error": "overlapping pages; repeat discovery from initial offset"
                        }
                    )
                    report["next_offset"] = 0
                break
        except (ValueError, OSError) as exc:
            report["gaps"].append(_error(exc))
            break
    report["fixtures"] = sorted(
        slug for slug, siblings in domestic.items() if slug in siblings
    )
    report["auxiliary_without_main"] = sorted(
        slug for slug, siblings in domestic.items() if slug not in siblings
    )
    report["fixtures_with_both_events"] = sum(
        len(siblings) == 2 for siblings in domestic.values()
    )
    report["rejected_events"] = dict(rejected)
    return report


def catalog(data_dir, leagues, start, end, *, pages=10, page_size=25, offset=0):
    selected, start, end = _args(leagues, start, end)
    if not 1 <= page_size <= 100 or not 1 <= pages <= 10000 or offset < 0:
        raise ValueError("invalid catalogue pagination")
    root = research_root(data_dir)
    result = {
        "window": [start, end],
        "initial_offset": offset,
        "leagues": {},
        "denominator": "Gamma domestic events in the query window; independent schedule reconciliation is separate",
    }
    try:
        sports, reply, sha = archive._fetch(
            root, "league_sports", "big5", provider.GAMMA + "/sports"
        )
        if not isinstance(sports, list):
            raise ValueError("sports response must be an array")
        result["sport_source"] = {
            "sha256": sha,
            "received_at": reply.received_at,
            "url": reply.url,
        }
    except (ValueError, OSError) as exc:
        result["leagues"] = {
            s: {
                "gaps": [_error(exc)],
                "page_walk_complete": False,
                "next_offset": offset,
                "fixtures": [],
            }
            for s in selected
        }
        return result
    for sport in selected:
        try:
            result["leagues"][sport] = _catalog_one(
                root, sport, start, end, pages, page_size, offset, sports
            )
        except (ValueError, OSError) as exc:
            result["leagues"][sport] = {
                "gaps": [_error(exc)],
                "page_walk_complete": False,
                "next_offset": offset,
                "fixtures": [],
            }
    return result


def _refresh(root, main):
    """Refresh both public event receipts; never reuse stale contracts after a failed read."""
    league = main.split("-", 1)[0]
    events, gaps = [], []
    pair = None
    for slug in (main, main + "-more-markets"):
        try:
            event, reply, sha = archive._fetch(
                root, "league_event", slug, provider.GAMMA + "/events/slug/" + slug
            )
            with closing(_db(root)) as db, db:
                identity = _identity(event, league, history=db)
                if identity[0] != slug:
                    raise ValueError("unexpected event slug")
                if pair is not None and pair != identity[3:6]:
                    prior = db.execute(
                        "SELECT home_id,away_id,event_id FROM big5_events WHERE slug=?",
                        (slug,),
                    ).fetchone()
                    if pair[1:] == identity[4:6] and (
                        prior is None or tuple(prior) == identity[4:7]
                    ):
                        schedule.record(
                            db,
                            identity,
                            reply.received_at,
                            sha,
                            schedule.delays(event),
                            True,
                            "conflicting_aux_schedule",
                            event=event,
                        )
                    gaps.append(
                        {
                            "slug": slug,
                            "error": "auxiliary event kickoff or team IDs differ from main event",
                        }
                    )
                    continue
                _record(db, event, identity, reply, sha)
            pair = identity[3:6]
            events.append(slug)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            gaps.append({"slug": slug, **_error(exc)})
            if slug == main:
                break
    return {"events": events, "gaps": gaps}


def _selection(contracts, all_lines, *, include_closed=False):
    candidates, invalid = defaultdict(list), Counter()
    for contract in contracts:
        family = contract["family"]
        if family not in provider.FAMILIES:
            continue
        if family == "moneyline":
            key = f"moneyline.{contract['side']}"
        else:
            line = provider.number(contract["line"])
            if line < 0 or line % 1 != Decimal("0.5"):
                invalid["non_half_goal_line"] += 1
                continue
            key = f"{family}.{line.normalize():f}"
        if contract["mapping_error"] or (
            not include_closed and contract["market_open"] is not True
        ):
            invalid[contract["mapping_error"] or "not_confirmed_open"] += 1
            continue
        if len(contract["game_times"]) != 1:
            invalid["ambiguous_contract_kickoff"] += 1
            continue
        candidates[key].append(contract)
    # A token reused by even an unsupported or closed listing is not uniquely bound.
    token_count = Counter(t["token_id"] for c in contracts for t in c["tokens"])
    condition_count = Counter(c["condition_id"] for c in contracts if c["condition_id"])
    selected = {}
    for key, rows in candidates.items():
        if (
            len(rows) != 1
            or condition_count[rows[0]["condition_id"]] != 1
            or any(token_count[t["token_id"]] != 1 for t in rows[0]["tokens"])
        ):
            invalid["ambiguous_slot_or_identifier"] += 1
        elif all_lines or key in CORE:
            selected[key] = rows[0]
    return selected, dict(invalid)


def depth_50(asks):
    """Visible $50 ask-side notional at one instant, before fees. No assumed fill."""
    budget = Decimal("50")
    left, shares = budget, Decimal(0)
    for count, ask in enumerate(asks, 1):
        price, size = provider.number(ask["price"]), provider.number(ask["size"])
        spend = min(left, price * size)
        shares += spend / price
        left -= spend
        if not left:
            average = budget / shares
            return {
                "levels": count,
                "vwap": round(float(average), 8),
                "above_best_ask": round(
                    float(average - provider.number(asks[0]["price"])), 8
                ),
            }
    return {"levels": None, "available_usd": round(float(budget - left), 8)}


def _measure(root, selected, result, window):
    token_keys = {
        t["token_id"]: (key, t["outcome"])
        for key, contract in selected.items()
        for t in contract["tokens"]
    }
    groups = {}
    for key, c in selected.items():
        groups[key] = {
            "condition": c["condition_id"],
            "market_slug": c["slug"],
            "event_slug": c["event_slug"],
            "event_id": c["event_id"],
            "protocol": c["protocol"],
            "rule_sha256": c["rule_sha256"],
            "rule_profile": receipts.rule_profile(c),
            "contract_source_sha256": c.get("source_sha256"),
            "contract_received": c.get("recorded_at"),
            "seconds_delay": c["seconds_delay"],
            "fees_enabled": c["fees_enabled"],
            "fee_schedule": c["fee_schedule"],
            "tokens": {},
        }
    with closing(_db(root)) as db:
        for observation in result["observations"]:
            token = observation["token_id"]
            key, outcome = token_keys[token]
            entry = {"id": token}
            groups[key]["tokens"][outcome] = entry
            if "error" in observation:
                entry["error"] = observation["error"]
                continue
            row = db.execute(
                "SELECT payload FROM ma_books WHERE token=? AND received_at=? AND sha=?",
                (token, observation["received_at"], observation["source_sha256"]),
            ).fetchone()
            if not row:
                entry["error"] = "missing stored receipt"
                continue
            book = json.loads(row["payload"])
            expected = selected[key]
            if (
                book["condition_id"] != expected["condition_id"]
                or book["rule_sha256"] != expected["rule_sha256"]
                or book["contract_source_sha256"] != expected.get("source_sha256")
                or book["contract_recorded_at"] != expected.get("recorded_at")
            ):
                entry["error"] = "book is bound to a different contract receipt"
                continue
            entry.update(
                {
                    k: book[k]
                    for k in (
                        "requested_at",
                        "received_at",
                        "observed_at",
                        "exchange_at",
                        "source_sha256",
                        "pregame",
                    )
                }
            )
            entry["bid_levels"] = len(book["bids"])
            entry["ask_levels"] = len(book["asks"])
            entry["depth_50"] = depth_50(book["asks"])
            entry["latency_ms"] = round(
                1000
                * (
                    provider.moment(book["received_at"])
                    - provider.moment(book["requested_at"])
                ).total_seconds(),
                2,
            )
            if window:
                entry["on_time"] = (
                    provider.moment(window[0])
                    <= provider.moment(book["requested_at"])
                    <= provider.moment(book["received_at"])
                    < provider.moment(window[1])
                )
    for key, c in selected.items():
        for t in c["tokens"]:
            groups[key]["tokens"].setdefault(
                t["outcome"], {"id": t["token_id"], "error": "no observation returned"}
            )
    return groups


def _record_run(root, report, started):
    report["finished"] = now_utc()
    with closing(_db(root)) as db, db:
        db.execute(
            "INSERT INTO big5_runs(sport,fixture,phase,started,finished,report) VALUES(?,?,?,?,?,?)",
            (
                report["league"],
                report["fixture"],
                report["phase"],
                started,
                report["finished"],
                canonical(report),
            ),
        )
    return report


def _sample(
    root,
    fixture,
    *,
    all_lines,
    phase,
    workers=3,
    refresh=True,
    deadline=None,
    window=None,
):
    started = now_utc()
    report = {
        "fixture": fixture,
        "league": fixture.split("-", 1)[0],
        "phase": phase,
        "started": started,
        "capture_window": window,
        "core_expected": len(CORE),
        "core_missing": list(CORE),
        "requested_tokens": 0,
        "received_tokens": 0,
        "two_sided_tokens": 0,
        "fresh_tokens": 0,
        "contracts": {},
    }
    if refresh:
        timer = archive._DEADLINE.set(deadline)
        try:
            sources = _refresh(root, fixture)
        finally:
            archive._DEADLINE.reset(timer)
    else:
        # Only identities accepted into the isolated catalog can reach the book collector.
        with closing(_db(root)) as db:
            events = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM big5_events WHERE fixture=? ORDER BY slug",
                    (fixture,),
                )
            ]
        sources = {"events": [], "gaps": []}
        if events and events[0]["slug"] == fixture:
            main = events[0]
            stale_before = iso(provider.moment(started) - timedelta(minutes=10))
            for event in events:
                if event["receipt"] < stale_before:
                    sources["gaps"].append(
                        {
                            "slug": event["slug"],
                            "error": "validated catalogue receipt older than ten minutes",
                        }
                    )
                elif all(
                    event[k] == main[k] for k in ("kickoff", "home_id", "away_id")
                ):
                    sources["events"].append(event["slug"])
                else:
                    sources["gaps"].append(
                        {"slug": event["slug"], "error": "sibling identity mismatch"}
                    )
    report["event_gaps"] = sources["gaps"]
    contracts = [
        c
        for c in archive.contracts(root, fixture)
        if c["event_slug"] in sources["events"]
    ]
    selected, report["excluded_contracts"] = _selection(contracts, all_lines)
    report["core_missing"] = [key for key in CORE if key not in selected]
    with closing(_db(root)) as db:
        stopped = db.execute(
            "SELECT 1 FROM big5_events e JOIN big5_schedule_receipts s ON "
            "(e.slug,e.receipt,e.sha)=(s.slug,s.received,s.sha) "
            "WHERE e.fixture=? AND s.stopped=1 LIMIT 1",
            (fixture,),
        ).fetchone()
    if (
        fixture not in sources["events"]
        or stopped
        or archive.market_state(root, fixture)
    ):
        report["error"] = "unverified main event or observed live/ended"
    elif len(archive.fixture_times(root, fixture)) != 1:
        report["error"] = "ambiguous scheduled start"
    elif not selected:
        report["error"] = "no eligible open contracts"
    else:
        tokens = [t["token_id"] for c in selected.values() for t in c["tokens"]]
        report["requested_tokens"] = len(tokens)
        result = archive.capture(
            root,
            fixture,
            tokens=tokens,
            workers=workers,
            discovery=sources,
            deadline=deadline,
            native_only=bool(deadline),
        )
        report.update(
            {
                k: result[k]
                for k in (
                    "batch_error",
                    "retry_at",
                    "unmapped_or_closed_requested_tokens",
                )
            }
        )
        report["contracts"] = _measure(root, selected, result, window)
        entries = [
            v
            for c in report["contracts"].values()
            for v in c["tokens"].values()
            if "error" not in v
        ]
        report["received_tokens"] = len(entries)
        report["two_sided_tokens"] = sum(
            bool(v["bid_levels"] and v["ask_levels"]) for v in entries
        )
        report["fresh_tokens"] = sum(bool(v["observed_at"]) for v in entries)
    return _record_run(root, report, started)


def collect(
    data_dir,
    leagues,
    start,
    end,
    *,
    mode="catalog",
    pages=10,
    page_size=25,
    offset=0,
    limit_fixtures=None,
    workers=3,
):
    if (
        mode not in ("catalog", "core", "all")
        or (limit_fixtures is not None and limit_fixtures < 1)
        or not 1 <= workers <= 8
    ):
        raise ValueError("invalid capture mode, fixture limit or worker count")
    result = catalog(
        data_dir, leagues, start, end, pages=pages, page_size=page_size, offset=offset
    )
    root = research_root(data_dir)
    if mode != "catalog":
        for summary in result["leagues"].values():
            fixtures = summary["fixtures"][:limit_fixtures]
            summary["sampled_fixtures"] = len(fixtures)
            summary["unsampled_fixtures"] = len(summary["fixtures"]) - len(fixtures)
            summary["observations"] = [
                _sample(
                    root,
                    fixture,
                    all_lines=mode == "all",
                    phase="manual_" + mode,
                    workers=workers,
                )
                for fixture in fixtures
            ]
    return result


def _slot(kickoff, now, delays=(5,)):
    """A half-open receipt interval. Late starts can never populate an expired phase."""
    elapsed = (now - kickoff).total_seconds()
    if elapsed >= 600:
        return None
    waits = sorted({max(5, delay) for delay in delays} or {5})
    for hours in (24, 1):
        target = kickoff - timedelta(hours=hours)
        windows = [("decision", -25, 0)]
        windows.extend(
            ("execution" if delay == 5 else f"execution_{delay}s", delay, delay + 5)
            for delay in waits
        )
        for role, lo, hi in windows:
            first, end = target + timedelta(seconds=lo), target + timedelta(seconds=hi)
            if first <= now < end:
                return f"t{hours}_{role}", first, end, True
        if (
            target - timedelta(seconds=30)
            <= now
            < target + timedelta(seconds=waits[-1] + 5)
        ):
            return None
    tick = 12 if elapsed >= -120 else (60 if elapsed >= -10800 else 900)
    index = int(elapsed // tick)
    first = kickoff + timedelta(seconds=index * tick)
    end = first + timedelta(seconds=tick)
    if elapsed >= -120:
        # Alternate lines get three closing snapshots, core still gets the full cadence.
        all_lines = index in (-10, -5, -1, 0, 5, 10)
        return "closing", first, end, all_lines
    return "periodic", first, end, False


def _claim(root, fixture, kickoff, name, end, now):
    with closing(_db(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        lease = iso(min(end, now + timedelta(seconds=90)))
        key = (fixture, kickoff, name)
        found = db.execute(
            "SELECT success,expires,attempts FROM big5_slots WHERE fixture=? AND kickoff=? AND slot=?",
            key,
        ).fetchone()
        if found and (
            found["success"] or found["expires"] > iso(now) or found["attempts"] >= 3
        ):
            return None
        attempt = found["attempts"] + 1 if found else 1
        db.execute(
            "INSERT INTO big5_slots VALUES(?,?,?,?,?,0,NULL) ON CONFLICT(fixture,kickoff,slot) DO UPDATE "
            "SET expires=excluded.expires,attempts=excluded.attempts",
            (*key, lease, attempt),
        )
        db.execute(
            "INSERT INTO big5_last_dispatch VALUES(?,?,?) ON CONFLICT(fixture,phase) "
            "DO UPDATE SET started=excluded.started",
            (fixture, name.split("@", 1)[0], iso(now)),
        )
        return attempt


def _finish(root, fixture, kickoff, name, attempt, ok, retry_time, details):
    with closing(_db(root)) as db, db:
        db.execute(
            "UPDATE big5_slots SET success=?,expires=?,report=? "
            "WHERE fixture=? AND kickoff=? AND slot=? AND attempts=?",
            (int(ok), retry_time, canonical(details), fixture, kickoff, name, attempt),
        )


def terminal(data_dir, fixture):
    """Archive actual Data API token receipts. Never turn an exchange price or the score into a payout."""
    from . import settlement

    root = research_root(data_dir)
    with closing(_db(root)) as db:
        event = db.execute(
            "SELECT sport,kickoff FROM big5_events WHERE slug=? AND slug=fixture",
            (fixture,),
        ).fetchone()
    if not event:
        raise ValueError("fixture requires a validated domestic Gamma catalogue entry")
    started = now_utc()
    if event["kickoff"] > started:
        raise ValueError("scheduled start still in the future")
    sources = _refresh(root, fixture)
    report = {
        "league": event["sport"],
        "fixture": fixture,
        "phase": "terminal",
        "started": started,
        "event_gaps": sources["gaps"],
        "contracts": {},
        "terminal_complete": False,
    }
    if fixture not in sources["events"]:
        return _record_run(root, report, started)
    # Delistings must not erase the obligation to follow the original token IDs.
    with closing(_db(root)) as db:
        rows = db.execute(
            "SELECT payload FROM (SELECT payload,row_number() OVER (PARTITION BY condition_id,"
            "market_slug ORDER BY received_at DESC,sha DESC) n FROM ma_contracts WHERE fixture=?) "
            "WHERE n=1",
            (fixture,),
        )
        observed = [json.loads(r[0]) for r in rows]
    selected, report["excluded_contracts"] = _selection(
        observed, True, include_closed=True
    )
    for key, c in selected.items():
        vector = []
        entry = {
            "condition": c["condition_id"],
            "rule_sha256": c["rule_sha256"],
            "tokens": {},
            "status": "pending",
        }
        for t in c["tokens"]:
            try:
                row = settlement.token_status(
                    root, t["token_id"], recheck_terminal=True
                )
                if not row:
                    raise ValueError("missing official token receipt")
                details = {
                    "id": t["token_id"],
                    "status": row["status"],
                    "payout": row["payout"],
                    "received_at": row["resolved_at"],
                    "sha256": row["source_sha256"],
                }
                if row["status"] == "resolved":
                    settlement._verify_token(c, row, t["token_id"])
                    vector.append(provider.number(row["payout"]))
                entry["tokens"][t["outcome"]] = details
            except (ValueError, OSError, KeyError, TypeError) as exc:
                entry["tokens"][t["outcome"]] = {"id": t["token_id"], **_error(exc)}
        if (
            len(vector) == 2
            and sum(vector) == 1
            and set(vector) <= {Decimal(0), Decimal("0.5"), Decimal(1)}
        ):
            entry["status"] = "verified_exchange_vector"
        report["contracts"][key] = entry
    report["terminal_complete"] = (
        bool(selected)
        and not sources["gaps"]
        and not any(
            reason != "non_half_goal_line" for reason in report["excluded_contracts"]
        )
        and all(
            entry["status"] == "verified_exchange_vector"
            for entry in report["contracts"].values()
        )
    )
    return _record_run(root, report, started)


def _job(data_dir, event, job, workers):
    root = research_root(data_dir)
    phase, first, end, all_lines = job
    now = provider.moment(now_utc())
    name = phase + "@" + iso(first)
    fixture, kickoff = event["fixture"], event["kickoff"]
    if not first <= now < end:
        return {"fixture": fixture, "phase": phase, "state": "window_elapsed"}
    attempt = _claim(root, fixture, kickoff, name, end, now)
    if attempt is None:
        return {"fixture": fixture, "phase": phase, "state": "already_claimed"}
    retry_after = None
    fixed = phase.startswith(("t1_", "t24_"))
    witness = {}
    # Stop network work before the lease can be reclaimed by a second process.
    deadline = iso(min(end, now + timedelta(seconds=85)))
    try:
        if phase == "terminal":
            timer = archive._DEADLINE.set(deadline)
            try:
                report = terminal(data_dir, fixture)
            finally:
                archive._DEADLINE.reset(timer)
            ok = report["terminal_complete"]
        else:
            # Catalog/metadata refresh runs separately; read freshly validated snapshots at the narrow windows.
            report = _sample(
                root,
                fixture,
                all_lines=all_lines,
                phase=phase,
                workers=workers,
                refresh=phase == "closing" and now >= provider.moment(kickoff),
                deadline=deadline,
                window=(iso(first), iso(end)),
            )
            report_tokens = [
                v for c in report["contracts"].values() for v in c["tokens"].values()
            ]
            if fixed:
                with closing(_db(root)) as db:
                    witness = receipts.witness(
                        db, root, report, fixture, kickoff, (iso(first), iso(end))
                    )
                ok = bool(witness)
            else:
                ok = (
                    bool(report_tokens)
                    and not report["core_missing"]
                    and all(
                        "error" not in v and v.get("on_time", True) and v["observed_at"]
                        for v in report_tokens
                    )
                )
        retry_after = report.get("retry_at")
        details = {
            "state": "complete" if ok else "partial",
            "received": report.get("received_tokens"),
            "expected": report.get("requested_tokens"),
            "error": report.get("error"),
        }
        if fixed:
            details.update(witness)
        elif ok and phase != "terminal":
            details["first_receipt"] = min(v["received_at"] for v in report_tokens)
            details["finished"] = max(v["received_at"] for v in report_tokens)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        ok = False
        retry_after = getattr(exc, "retry_at", None)
        details = {"state": "failed", **_error(exc)}
    now = provider.moment(now_utc())
    retry = max(
        now + timedelta(seconds=300 if phase == "terminal" else 2),
        provider.moment(retry_after) if retry_after else now,
    )
    _finish(root, fixture, kickoff, name, attempt, ok, iso(retry), details)
    return {"fixture": fixture, "phase": phase, **details}


def _ranked_jobs(root, leagues, start, end, now):
    slots = []
    with closing(_db(root)) as db:
        rows = list(
            db.execute(
                "SELECT * FROM big5_events WHERE slug=fixture AND kickoff>=? "
                "AND kickoff<? ORDER BY kickoff,fixture",
                (start, end),
            )
        )
        playing = {
            r["fixture"]: bool(r["ended"])
            for r in db.execute(
                "SELECT e.fixture,max(s.ended) ended FROM big5_events e JOIN ma_event_states s "
                "ON (e.slug,e.receipt,e.sha)=(s.slug,s.received_at,s.sha) "
                "JOIN big5_events main ON main.slug=e.fixture AND main.kickoff=e.kickoff "
                "JOIN big5_schedule_receipts v ON (e.slug,e.receipt,e.sha)=(v.slug,v.received,v.sha) "
                "WHERE e.kickoff>=? AND e.kickoff<? AND v.stopped=1 GROUP BY e.fixture",
                (start, end),
            )
        }
        last_dispatched = {
            (r["fixture"], r["phase"]): r["started"]
            for r in db.execute(
                "SELECT d.* FROM big5_last_dispatch d JOIN big5_events e ON d.fixture=e.slug "
                "WHERE e.kickoff>=? AND e.kickoff<?",
                (start, end),
            )
        }
        for event in rows:
            if event["sport"] not in leagues:
                continue
            kickoff = provider.moment(event["kickoff"])
            delays = {
                n
                for r in db.execute(
                    "SELECT delays FROM big5_event_delays JOIN big5_events USING(slug) "
                    "WHERE fixture=? AND kickoff=?",
                    (event["fixture"], event["kickoff"]),
                )
                for n in json.loads(r[0])
            }
            ended = playing.get(event["fixture"])
            options = _slot(kickoff, now, delays) if ended is None else None
            if options is None and (
                now >= kickoff + timedelta(hours=2) or (ended and now >= kickoff)
            ):
                # A pending or disputed pair stays in scope. Complete pairs are revisited the following day.
                last = db.execute(
                    "SELECT finished,report FROM big5_runs WHERE fixture=? AND phase='terminal' "
                    "ORDER BY id DESC LIMIT 1",
                    (event["fixture"],),
                ).fetchone()
                period = (
                    86400
                    if last and json.loads(last["report"]).get("terminal_complete")
                    else 900
                )
                first = kickoff + timedelta(
                    seconds=int((now - kickoff).total_seconds() // period) * period
                )
                options = ("terminal", first, first + timedelta(seconds=period), False)
            if options:
                phase, first, last, _ = options
                name = phase + "@" + iso(first)
                prior = db.execute(
                    "SELECT success,attempts,expires FROM big5_slots WHERE fixture=? AND kickoff=? "
                    "AND slot=?",
                    (event["fixture"], event["kickoff"], name),
                ).fetchone()
                if prior and (
                    prior["success"]
                    or prior["attempts"] >= 3
                    or prior["expires"] > iso(now)
                ):
                    continue
                score = 2 if phase == "terminal" else 1 if phase == "periodic" else 0
                slots.append((score, last, event, options))
    return sorted(
        slots,
        key=lambda r: (
            r[0],
            r[1],
            last_dispatched.get((r[2]["fixture"], r[3][0]), ""),
            r[2]["fixture"],
        ),
    )


def watch(
    data_dir,
    leagues,
    start,
    end,
    *,
    cycles=1,
    interval=2,
    workers=4,
    pages=10,
    page_size=25,
    discover=True,
):
    selected, start, end = _args(leagues, start, end)
    if cycles < 0 or not 1 <= workers <= 8 or not 1 <= interval <= 60:
        raise ValueError("cycles >= 0, workers 1..8 and interval 1..60 required")
    root = research_root(data_dir)
    discovery, next_catalog, count = None, None, 0
    states = Counter()
    last_jobs = []
    # The same pool is kept between ticks: games with a slow network never block scheduling other games.
    pending, active = {}, set()
    with (
        market_watch_health.monitor(
            root, selected, start, end, continuous=cycles == 0, discover=discover
        ) as heartbeat,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        while cycles == 0 or count < cycles:
            now = provider.moment(now_utc())
            if discover and (next_catalog is None or now >= next_catalog):
                # Startup discovery establishes the initial cohort. Later refreshes run in the pool.
                if discovery is None:
                    discovery = catalog(
                        data_dir, selected, start, end, pages=pages, page_size=page_size
                    )
                elif "catalog" not in active:
                    task = pool.submit(
                        catalog,
                        data_dir,
                        selected,
                        start,
                        end,
                        pages=pages,
                        page_size=page_size,
                    )
                    pending[task] = "catalog"
                    active.add("catalog")
                complete = all(
                    v["page_walk_complete"] for v in discovery["leagues"].values()
                )
                next_catalog = now + timedelta(seconds=300 if complete else 30)
            for task in list(pending):
                if task.done():
                    key = pending.pop(task)
                    active.discard(key)
                    if key == "catalog":
                        discovery = task.result()
                    else:
                        result = task.result()
                        states[result["state"]] += 1
            jobs = _ranked_jobs(root, selected, start, end, provider.moment(now_utc()))
            for _, _, event, job in jobs:
                key = event["fixture"]
                if len(pending) >= workers and count + 1 != cycles:
                    break
                if key not in active:
                    task = pool.submit(_job, data_dir, event, job, 1)
                    active.add(key)
                    pending[task] = key
            count += 1
            if heartbeat is not None:
                heartbeat(count, discovery)
            if cycles == 0 or count < cycles:
                time.sleep(interval)
        for task, key in pending.items():
            result = task.result()
            if key == "catalog":
                discovery = result
            else:
                last_jobs.append(result)
                states[result["state"]] += 1
    return {
        "cycles": count,
        "states": dict(states),
        "last_jobs": last_jobs,
        "discovery": discovery,
    }


def _quantiles(values):
    if not values:
        return {}
    from math import ceil

    ordered = sorted(values)
    return {
        "p50": ordered[ceil(len(ordered) * 0.5) - 1],
        "p95": ordered[ceil(len(ordered) * 0.95) - 1],
        "max": ordered[-1],
    }


def weekly_report(data_dir, start, end):
    """Use the receipt window for costs; due fixed windows count all catalogued matches, including complete misses."""
    from .blob_storage import blob_size

    _, start, end = _args(tuple(MARKETS), start, end)
    root = research_root(data_dir)
    now = now_utc()
    stats, target_counts = {}, defaultdict(Counter)
    with closing(_db(root)) as db:
        schedule.count_windows(root, db, target_counts, start, end, now)
        history_origins = {
            r[0]: r[1]
            for r in db.execute(
                "SELECT origin,count(*) FROM big5_schedule_receipts WHERE received<? GROUP BY origin",
                (min(now, end),),
            )
        }
        catalogue_flags = [
            {
                "league": r["sport"],
                "live": None if r["live"] is None else bool(r["live"]),
                "ended": None if r["ended"] is None else bool(r["ended"]),
                "receipts": r["n"],
            }
            for r in db.execute(
                "SELECT h.sport,s.live,s.ended,count(*) n FROM big5_schedule_receipts h JOIN "
                "ma_event_states s ON (h.slug,h.received,h.sha)=(s.slug,s.received_at,s.sha) "
                "WHERE h.received>=? AND h.received<? GROUP BY h.sport,s.live,s.ended ORDER BY h.sport,s.live,s.ended",
                (start, min(end, now)),
            )
        ]
        for row in db.execute(
            "SELECT * FROM big5_runs WHERE finished>=? AND finished<? ORDER BY id",
            (start, end),
        ):
            r = json.loads(row["report"])
            if row["phase"] == "terminal":
                target_counts[row["sport"]]["terminal_checks"] += 1
                target_counts[row["sport"]]["terminal_checks_complete"] += bool(
                    r["terminal_complete"]
                )
                continue
            overall = target_counts[row["sport"]]
            overall["fixture_attempts"] += 1
            overall["expected_core_tokens_in_attempts"] += len(CORE) * 2
            overall["missing_core_tokens_in_attempts"] += len(r["core_missing"]) * 2
            overall["requested_tokens"] += r["requested_tokens"]
            overall["received_tokens"] += r["received_tokens"]
            overall["requested_tokens_without_receipt"] += (
                r["requested_tokens"] - r["received_tokens"]
            )
            for key in set(r["contracts"]) | set(CORE):
                family = key.split(".")[0]
                group = stats.setdefault(
                    (row["sport"], family),
                    {
                        "expected_token_observations": 0,
                        "received": 0,
                        "fresh": 0,
                        "two_sided": 0,
                        "cash50_available": 0,
                        "levels_for_50": [],
                        "vwap_above_best_ask": [],
                        "latency_ms": [],
                    },
                )
                group["expected_token_observations"] += 2
                for entry in r["contracts"].get(key, {}).get("tokens", {}).values():
                    if "error" in entry:
                        continue
                    group["received"] += 1
                    group["fresh"] += bool(entry["observed_at"])
                    group["two_sided"] += bool(
                        entry["bid_levels"] and entry["ask_levels"]
                    )
                    group["latency_ms"].append(entry["latency_ms"])
                    depth = entry["depth_50"]
                    if depth["levels"] is not None:
                        group["cash50_available"] += 1
                        group["levels_for_50"].append(depth["levels"])
                        group["vwap_above_best_ask"].append(depth["above_best_ask"])
        requests = [
            dict(r)
            for r in db.execute(
                "SELECT operation,status,COUNT(*) n FROM ma_requests "
                "WHERE received_at>=? AND received_at<? GROUP BY operation,status",
                (start, end),
            )
        ]
        error_codes = {
            str(code): db.execute(
                "SELECT COUNT(*) FROM ma_requests WHERE received_at>=? AND received_at<? AND status='error' AND detail LIKE ?",
                (start, end, f"HTTP Error {code}:%"),
            ).fetchone()[0]
            for code in (429, 503)
        }
        failures = {
            str(r[0]): r[1]
            for r in db.execute(
                "SELECT http_status,COUNT(*) FROM ma_cooldown_causes "
                "WHERE not_before>=? AND not_before<? GROUP BY http_status",
                (start, end),
            )
        }
        blobs = list(
            db.execute(
                "SELECT sha256,MAX(size) size FROM observations WHERE observed_at>=? AND observed_at<? "
                "GROUP BY sha256",
                (start, end),
            )
        )
    for group in stats.values():
        for measure in ("levels_for_50", "vwap_above_best_ask", "latency_ms"):
            group[measure] = _quantiles(group[measure])
    raw_bytes, missing_raw = 0, 0
    for row in blobs:
        try:
            raw_bytes += blob_size(root, row["sha256"])[1]
        except FileNotFoundError:
            missing_raw += 1
    database_bytes = 0
    for path in root.glob("runtime.sqlite3*"):
        try:
            if path.is_file():
                database_bytes += path.stat().st_size
        except FileNotFoundError:
            # Another connection may checkpoint and unlink the WAL during this reading.
            continue
    return {
        "receipt_window": [start, end],
        "schedule_receipt_origins_before_report_end": history_origins,
        "denominator_basis": "Observed domestic schedule and delay versions at each window expiry. "
        "A trace without a verifiable earlier original cannot establish earlier obligations. "
        "Pairs require identical condition, side, outcome tokens, rule and fee terms across matching delay legs.",
        "fixed_window_basis": "Timely, complete, fresh native Gamma-bound CLOB receipts. "
        "Complete windows and identical-contract pairs are partitioned by exchange_state. "
        "Pairs share one uninterrupted main/aux schedule. Prior native evidence that play started "
        "applies to later receipts on the same fixture, including after a flag disappears. "
        "Schedule-only requires a known unchanged schedule before kickoff without contradictory flags. "
        "Receipt completeness does not authorize an entry, CLV or return.",
        "catalogue_exchange_flags": catalogue_flags,
        "generated_at": now,
        "by_league": dict(target_counts),
        "by_league_family": {
            s: {k: v for (l, k), v in stats.items() if l == s}
            for s in MARKETS
            if s in target_counts
        },
        "http_requests": requests,
        "observed_http_status_errors": error_codes,
        "cooldowns_expiring_in_window": failures,
        "raw_unique_bodies": len(blobs),
        "raw_unique_decoded_bytes": sum(r["size"] for r in blobs),
        "raw_unique_on_disk_bytes": raw_bytes,
        "raw_missing_bodies": missing_raw,
        "database_bytes_at_report": database_bytes,
        "depth_basis": "single visible ask book, USD 50 before fees; no trade or guaranteed fill",
        "evidence_grade": "research observations; no inferred actual kickoff, A-grade entry, profit or ROI",
    }


def _window_args(parser):
    parser.add_argument(
        "--league",
        action="append",
        choices=MARKETS,
        help="repeat to select; default: four new leagues",
    )
    parser.add_argument(
        "--start", required=True, help="inclusive UTC timestamp of scheduled kickoff"
    )
    parser.add_argument(
        "--end", required=True, help="exclusive UTC timestamp of scheduled kickoff"
    )
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument("--page-size", type=int, default=25)


def register(commands):
    p = commands.add_parser(
        "league-archive",
        help="discover and sample domestic event and CLOB originals in an isolated research DB",
    )
    _window_args(p)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument(
        "--limit-fixtures", type=int, help="optional sample limit per league"
    )
    p.add_argument("--mode", choices=("catalog", "core", "all"), default="catalog")
    p.add_argument("--workers", type=int, default=3)
    p.set_defaults(
        func=lambda a: collect(
            a.data_dir,
            a.league,
            a.start,
            a.end,
            mode=a.mode,
            pages=a.pages,
            page_size=a.page_size,
            offset=a.offset,
            limit_fixtures=a.limit_fixtures,
            workers=a.workers,
        )
    )
    p = commands.add_parser(
        "league-watch",
        help="foreground tiered collection, with postgame token resolution follow-up",
    )
    _window_args(p)
    p.add_argument(
        "--cycles", type=int, default=1, help="0 for an ongoing foreground process"
    )
    p.add_argument(
        "--interval", type=int, default=2, help="seconds between scheduling ticks"
    )
    p.add_argument("--workers", type=int, default=4)
    p.add_argument(
        "--local-catalog",
        action="store_true",
        help="consume only the existing isolated catalog",
    )
    p.set_defaults(
        func=lambda a: watch(
            a.data_dir,
            a.league,
            a.start,
            a.end,
            cycles=a.cycles,
            interval=a.interval,
            workers=a.workers,
            discover=not a.local_catalog,
            pages=a.pages,
            page_size=a.page_size,
        )
    )
    p = commands.add_parser(
        "league-watchdog",
        help="check the heartbeat for the requested continuous watch scope",
    )
    p.add_argument("--league", action="append", choices=MARKETS)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument(
        "--max-age",
        type=int,
        default=180,
        help="maximum seconds since an advancing scheduler tick",
    )
    p.add_argument(
        "--local-catalog",
        action="store_true",
        help="check a watcher using only its local catalog",
    )
    p.set_defaults(
        func=lambda a: market_watch_health.status(
            research_root(a.data_dir),
            *_args(a.league, a.start, a.end),
            discover=not a.local_catalog,
            max_age=a.max_age,
        ),
        result_exit_code=lambda result: 0 if result["healthy"] else 1,
    )
    p = commands.add_parser(
        "league-terminal",
        help="archive both original public token receipts for validated postgame contracts",
    )
    p.add_argument("--fixture", required=True)
    p.set_defaults(func=lambda a: terminal(a.data_dir, a.fixture))
    p = commands.add_parser(
        "league-report",
        help="summarize research capture costs, coverage and USD 50 visible depth",
    )
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.set_defaults(func=lambda a: weekly_report(a.data_dir, a.start, a.end))
