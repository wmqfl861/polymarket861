"""Prospective phase-one acceptance over consecutive completed Eastern NBA game dates."""

from collections import Counter, defaultdict
from contextlib import closing
import json
from datetime import date, datetime, time, timedelta, timezone

from . import markets, operations_report, storage
from .markets_contracts import team_code
from .markets_schedule import _catalogue_proof
from .model import instant
from .operations import EASTERN, HORIZONS

DAYS = 14
COVERAGE = 0.90
PRIMARY = "T-60m"


def _midnight(day):
    return datetime.combine(day, time.min, EASTERN).astimezone(timezone.utc)


def _stamp(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _inventory(root, start, end):
    """Calendar evidence comes from actual successful collection on each covered date."""
    schedule, venue, scanned, catalogued = (
        defaultdict(list),
        defaultdict(list),
        set(),
        set(),
    )
    first_warm = None
    db = storage.open_existing(root)
    if db is None:
        return schedule, venue, scanned, catalogued, first_warm
    low = _stamp(_midnight(start))
    high = _stamp(_midnight(end + timedelta(days=1)))
    with closing(db):
        db.execute("BEGIN")
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "nba_windows" in tables:
            first_warm = db.execute(
                """SELECT min(game_day) FROM nba_windows
                   WHERE horizon=? AND state='sealed' AND active=1 AND sealed_at<?
                   AND json_extract(payload,'$.warmup.eligible')=1""",
                (PRIMARY, high),
            ).fetchone()[0]
        if "nba_game_versions" in tables:
            # Select the last receipt of each game first, so a reschedule cannot
            # leave its old date in the prospective denominator.
            rows = db.execute(
                """
                WITH latest AS (SELECT game_id, max(version) AS version FROM nba_game_versions
                    WHERE source='balldontlie' AND available_at!='unpublished' AND available_at<?
                    GROUP BY game_id)
                SELECT json_extract(payload,'$.game_date') AS day, g.game_id,
                       json_extract(payload,'$.home_abbr') AS home, json_extract(payload,'$.away_abbr') AS away,
                       json_extract(payload,'$.stage') AS stage, json_extract(payload,'$.status') AS status
                FROM latest l JOIN nba_game_versions g ON g.source='balldontlie'
                    AND g.game_id=l.game_id AND g.version=l.version
                WHERE json_extract(payload,'$.game_date') BETWEEN ? AND ?""",
                (high, str(start), str(end)),
            )
            for row in rows:
                schedule[row["day"]].append(dict(row))
        if "nba_market_schedule" in tables:
            rows = db.execute(
                """
                WITH latest AS (SELECT id, max(version) AS version FROM nba_market_schedule
                    WHERE valid_from<? GROUP BY id)
                SELECT g.id,g.home,g.away,s.kickoff FROM latest l JOIN nba_market_schedule s
                    ON s.id=l.id AND s.version=l.version JOIN nba_market_games g ON g.id=l.id
                WHERE s.kickoff>=? AND s.kickoff<?""",
                (high, low, high),
            )
            for row in rows:
                venue[str(instant(row["kickoff"]).astimezone(EASTERN).date())].append(
                    dict(row)
                )
        if {"nba_schedule_evidence", "nba_schedule_completions"} <= tables:
            rows = db.execute(
                """SELECT s.start,s.end,s.began,c.complete,c.state,c.finished,e.manifest
                FROM nba_schedule_scans s LEFT JOIN nba_schedule_completions c ON s.id=c.scan_id
                LEFT JOIN nba_schedule_evidence e ON e.scan_id=s.id WHERE s.began>=? AND s.began<?
                ORDER BY s.began,s.id""",
                (low, high),
            )
            for row in rows:
                observed = str(instant(row["began"]).astimezone(EASTERN).date())
                if not row["start"] <= observed <= row["end"]:
                    continue
                quality = json.loads(row["manifest"] or "{}").get("quality") or {}
                clean = (
                    row["complete"]
                    and row["state"] == "ok"
                    and row["finished"] < high
                    and quality.get("phase_rows")
                    and quality.get("quarantined_rows") == 0
                    and not any(
                        quality.get(k)
                        for k in (
                            "pagination_errors",
                            "unidentified_rows",
                            "overlapping_ids",
                            "phase_mismatches",
                            "unrecognized_statuses",
                        )
                    )
                )
                if clean:
                    scanned.add(observed)
                else:
                    scanned.discard(observed)
        if "nba_market_runs" in tables:
            ticks = {
                instant(r[0]).astimezone(EASTERN).date()
                for r in db.execute(
                    "SELECT completed FROM nba_market_runs WHERE state='ok' AND completed>=? AND completed<?",
                    (low, high),
                )
            }
            for day in ticks:
                before = _stamp(
                    _midnight(day + timedelta(days=1)) - timedelta(microseconds=1)
                )
                if _catalogue_proof(db, str(day), before):
                    catalogued.add(str(day))
    return schedule, venue, scanned, catalogued, first_warm


def _daily(root, day, source, listings, scanned, catalogued):
    result = operations_report.report(root, "day", day)
    horizons = result.get("report", {}).get("horizons", {})

    def pair(game, home="home", away="away"):
        return team_code(game[home]), team_code(game[away])

    excluded = {
        g["game_id"]
        for g in source
        if g["stage"] in ("preseason", "postseason", "playin", "cup_final")
        or g["status"] in ("cancelled", "postponed", "withdrawn")
    }
    regular = {g["game_id"]: pair(g) for g in source if g["game_id"] not in excluded}
    other_pairs = {pair(g) for g in source if g["game_id"] in excluded} - set(
        regular.values()
    )
    native = [e for e in listings if pair(e) not in other_pairs]
    native_counts = Counter(pair(e) for e in native)
    counts = {}
    for horizon in HORIZONS:
        snapshot = horizons.get(horizon, {})
        fixtures = [
            f
            for f in snapshot.get("readiness", {}).get("fixtures", ())
            if f["game_id"] not in excluded
        ]
        ids = regular | {
            f["game_id"]: pair(f, "home_abbr", "away_abbr") for f in fixtures
        }
        candidates = Counter(ids.values())
        # Reconcile ordered teams on a completed ET date. Ambiguous rows consume
        # their own opportunities and cannot make the denominator smaller.
        unmatched = sum(
            1 for e in native if candidates[pair(e)] != 1 or native_counts[pair(e)] != 1
        )
        due = len(ids) + unmatched
        mature = [f for f in fixtures if f["warm_sealed"] and f["due"]]
        counts[horizon] = {
            "due": due,
            "sealed": sum(f["sealed"] and f["due"] for f in fixtures),
            "warm_sealed": len(mature),
            "eligible_win_quotes": sum(f["qualifying_win_quote"] for f in mature),
            "paired_win": sum(f["paired_win"] for f in mature),
            "unassessed_warm": sum(
                not (f["assessed"] or f["paired_win"]) for f in mature
            ),
            "unmatched_listings": unmatched,
            "missed_by_reason": snapshot.get("missed_by_reason", {}),
        }
    has_games = counts[PRIMARY]["due"] > 0
    return {
        "date_et": day,
        "kind": "game_day"
        if has_games
        else (
            "observed_no_regular_games"
            if scanned and catalogued
            else "unverified_calendar_day"
        ),
        "schedule_scan_succeeded_on_date": scanned,
        "market_collector_succeeded": catalogued,
        "in_scope_listings": len(native),
        "report_revision": result["revision"],
        "horizons": counts,
    }


def _coverage(days, first_warm):
    if first_warm:
        days = [d for d in days if d["date_et"] >= first_warm]
    else:
        # A brand new installation has no observation period yet. Expose gaps
        # after the first real observation without fabricating weeks of outages.
        first_seen = next(
            (
                i
                for i, d in enumerate(days)
                if d["kind"] == "game_day"
                or d["schedule_scan_succeeded_on_date"]
                or d["market_collector_succeeded"]
            ),
            len(days),
        )
        days = days[first_seen:]
    dates = [d["date_et"] for d in days if d["kind"] == "game_day"]
    if dates:
        days = [d for d in days if d["date_et"] >= dates[-DAYS:][0]]
    games = [d for d in days if d["kind"] == "game_day"]
    missing = [
        d["date_et"]
        for d in days
        if not d["schedule_scan_succeeded_on_date"]
        or not d["market_collector_succeeded"]
    ]
    zero = [
        d["date_et"]
        for d in games
        if d["in_scope_listings"] and not d["horizons"][PRIMARY]["warm_sealed"]
    ]
    no_quote = [
        d["date_et"] for d in games if not d["horizons"][PRIMARY]["eligible_win_quotes"]
    ]
    sums = {
        key: sum(d["horizons"][PRIMARY][key] for d in games)
        for key in (
            "due",
            "sealed",
            "warm_sealed",
            "eligible_win_quotes",
            "paired_win",
            "unassessed_warm",
        )
    }
    seal_rate = sums["warm_sealed"] / sums["due"] if sums["due"] else None
    pair_rate = (
        sums["paired_win"] / sums["eligible_win_quotes"]
        if sums["eligible_win_quotes"]
        else None
    )
    issues = []
    for failed, reason in (
        (not first_warm, "no_warm_forward_seal"),
        (len(games) < DAYS, "fewer_than_14_game_days"),
        (bool(missing), "calendar_coverage_incomplete"),
        (bool(zero), "listings_without_warm_seal"),
        (bool(no_quote), "day_without_eligible_win_quotes"),
        (bool(sums["unassessed_warm"]), "pending_quote_assessment"),
        (seal_rate is None or seal_rate < COVERAGE, "warm_seal_rate_below_90pct"),
        (pair_rate is None or pair_rate < COVERAGE, "eligible_win_pairing_below_90pct"),
    ):
        if failed:
            issues.append(reason)
    return {
        "qualified": not issues,
        "reasons": issues,
        "first_warm_game_day": first_warm,
        "observed_game_days": len(games),
        "counted_game_days": len(games) if first_warm else 0,
        "primary_horizon": PRIMARY,
        "target_rate": COVERAGE,
        "calendar_gaps": missing,
        "listing_days_without_warm_seal": zero,
        "days_without_eligible_win_quotes": no_quote,
        "totals": sums,
        "warm_seal_rate": seal_rate,
        "eligible_win_pairing_rate": pair_rate,
        "days": days,
    }


def readiness(root, through=None, *, now=None):
    from .source_acceptance import admission_summary

    yesterday = (now or datetime.now(timezone.utc)).astimezone(
        EASTERN
    ).date() - timedelta(days=1)
    end = through or yesterday
    if isinstance(end, str):
        end = date.fromisoformat(end)
    if end > yesterday:
        raise ValueError("choose a completed America/New_York game date")
    # Forty-five calendar days include the All-Star break. Longer gaps cannot
    # qualify as uninterrupted current operations on two weeks of very old games.
    start = end - timedelta(days=44)
    schedule, venue, scanned, collected, first = _inventory(root, start, end)
    days = []
    for number in range((end - start).days + 1):
        day = str(start + timedelta(days=number))
        days.append(
            _daily(
                root, day, schedule[day], venue[day], day in scanned, day in collected
            )
        )
    coverage = _coverage(days, first)
    sources = admission_summary(root, end)
    differences = markets.kickoff_differences(
        root, as_of=_stamp(_midnight(end + timedelta(days=1)))
    )
    gates = {
        "forward_coverage": coverage["qualified"],
        "keyed_schedule": sources["schedule_verified"],
        "three_paired_schedule_days": differences["enough_days"],
        "admitted_player_source": bool(sources["eligible_player_sources"]),
    }
    return {
        "through_et": str(end),
        "gates": gates,
        "state": "eligible_for_review" if all(gates.values()) else "collecting",
        "report_basis": "latest published day reports for completed dates; seal and price deadlines use original receipts",
        "calendar": coverage,
        "sources": sources,
        "kickoff_differences": differences,
        "football_host_health": "verify separately on the deployed football account",
        "phase_two_activated": False,
        "paper_trading": False,
    }


def register(actions):
    parser = actions.add_parser(
        "readiness", help="验收最近连续14个完整美东比赛日的封存、配对与来源"
    )
    parser.add_argument("--through", type=date.fromisoformat, metavar="YYYY-MM-DD")
    parser.set_defaults(func=lambda a: readiness(a.data_dir, a.through))
