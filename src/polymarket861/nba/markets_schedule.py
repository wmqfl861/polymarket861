"""As-of game identity and kickoff comparisons between native, independent schedules."""

import json
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ..market_provider import moment
from . import storage
from .markets_contracts import stamp, team_code

EAST = ZoneInfo("America/New_York")
PAIR_TOLERANCE = timedelta(minutes=30)
MATCHED = {"exact", "within_tolerance"}


def _has(db, table):
    return (
        db is not None
        and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _as_of(value):
    return moment(value) if value is not None else datetime.now(UTC)


def _day(kickoff):
    return moment(kickoff).astimezone(EAST).date().isoformat()


def disputed(db, event_id, game_id, home, away, kickoff, through):
    return (
        db.execute(
            "SELECT 1 FROM nba_market_identity_conflicts WHERE first_seen<=? AND "
            "(id=? OR game_id=? OR (home=? AND away=? AND kickoff=?)) LIMIT 1",
            (through, event_id, game_id, home, away, kickoff),
        ).fetchone()
        is not None
    )


def schedule_match_db(db, home, away, kickoff, *, as_of=None):
    tip, end = moment(kickoff), stamp(_as_of(as_of))
    home, away = team_code(home), team_code(away)
    day, source = _day(tip), stamp(tip)
    result = dict(
        status="missing_venue",
        reason=None,
        source_kickoff=source,
        venue_event_id=None,
        venue_version=None,
        venue_kickoff=None,
        delta_seconds=None,
    )
    if (
        _has(db, "nba_game_versions")
        and len(_score_candidates(db, home, away, kickoff, end, end)) > 1
    ):
        return dict(result, status="ambiguous", reason="ambiguous_score_source_fixture")
    if not _has(db, "nba_market_schedule"):
        return result
    claims = db.execute(
        "SELECT kickoff FROM nba_market_identity_conflicts WHERE home=? AND away=? AND first_seen<=?",
        (home, away, end),
    )
    if any(_day(r[0]) == day for r in claims):
        return dict(
            result, status="identity_conflict", reason="venue_identity_conflict"
        )
    by_id = defaultdict(list)
    for row in db.execute(
        "SELECT g.id,g.game_id,s.version,s.kickoff,p.reason FROM nba_market_games g "
        "JOIN nba_market_schedule s ON s.id=g.id LEFT JOIN nba_market_play p ON p.id=g.id AND p.observed<=? "
        "WHERE g.home=? AND g.away=? AND s.valid_from<=? ORDER BY s.version",
        (end, home, away, end),
    ):
        by_id[row["id"]].append(row)
    # Include displaced venues for this date so a newer event cannot inherit their prices.
    candidates = [
        versions
        for versions in by_id.values()
        if any(_day(r["kickoff"]) == day for r in versions)
    ]
    if len(candidates) > 1:
        return dict(result, status="ambiguous", reason="ambiguous_venue_fixture")
    if not candidates:
        return result
    versions = candidates[0]
    row = versions[-1]
    native = row["kickoff"]
    delta = (moment(native) - tip).total_seconds()
    result.update(
        venue_event_id=row["id"],
        venue_version=row["version"],
        venue_kickoff=native,
        delta_seconds=delta,
        reason=row["reason"],
    )
    if disputed(db, row["id"], row["game_id"], home, away, native, end):
        return dict(
            result, status="identity_conflict", reason="venue_identity_conflict"
        )
    if _day(native) != day or (
        source != native and any(r["kickoff"] == source for r in versions)
    ):
        return dict(
            result, status="schedule_replaced", reason="venue_schedule_replaced"
        )
    if abs(delta) > PAIR_TOLERANCE.total_seconds():
        return dict(
            result,
            status="outside_tolerance",
            reason=row["reason"] or "venue_same_pair_schedule_mismatch",
        )
    return dict(result, status="within_tolerance" if delta else "exact")


def schedule_match(root, home, away, kickoff, *, as_of=None):
    db = storage.open_existing(root)
    if db is None:
        return schedule_match_db(None, home, away, kickoff, as_of=as_of)
    with closing(db):
        return schedule_match_db(db, home, away, kickoff, as_of=as_of)


def fixture_conflict(root, home, away, kickoff, *, as_of=None):
    end = stamp(_as_of(as_of))
    db = storage.open_existing(root)
    if db is None:
        return None
    with closing(db):
        result = schedule_match_db(db, home, away, kickoff, as_of=end)
        veto = score_veto(
            db,
            dict(
                home=team_code(home),
                away=team_code(away),
                kickoff=stamp(moment(kickoff)),
            ),
            end,
            end,
        )
        if veto:
            return veto
    if result["reason"] and result["reason"] != "venue_same_pair_schedule_mismatch":
        return result["reason"]
    if result["status"] in MATCHED and _as_of(as_of) >= moment(result["venue_kickoff"]):
        return "venue_scheduled_tip_reached"
    return None


def _score_candidates(db, home, away, kickoff, observed, end):
    """Bound the ledger read by UTC days, then check both independently named sides and Eastern game date."""
    tip = moment(kickoff)
    bounds = (stamp(tip - timedelta(days=1)), stamp(tip + timedelta(days=1)))
    day = _day(tip)
    fields = """SELECT DISTINCT game_id,json_extract(payload,'$.home_abbr'),
            json_extract(payload,'$.away_abbr'),json_extract(payload,'$.kickoff')
            FROM nba_game_versions WHERE source='balldontlie' AND received_at<=? AND available_at<=? """
    # A known playing opponent can invalidate this game even when its provider lost
    # the precise tip. A date-only observation can veto; it cannot supply a match time.
    rows = db.execute(
        fields
        + "AND json_extract(payload,'$.kickoff') BETWEEN ? AND ? UNION "
        + fields
        + "AND json_extract(payload,'$.kickoff') IS NULL AND json_extract(payload,'$.game_date')=?",
        (observed, end, *bounds, observed, end, day),
    )
    return {
        r[0]
        for r in rows
        if team_code(r[1]) == home
        and team_code(r[2]) == away
        and (not r[3] or _day(r[3]) == day)
    }


def current_score_tip(db, game, end):
    if not _has(db, "nba_game_versions"):
        return None
    ids = _score_candidates(db, game["home"], game["away"], game["kickoff"], end, end)
    if len(ids) != 1:
        return None
    r = db.execute(
        """SELECT payload FROM nba_game_versions WHERE source='balldontlie' AND game_id=?
        AND received_at<=? AND available_at<=? ORDER BY version DESC LIMIT 1""",
        (next(iter(ids)), end, end),
    ).fetchone()
    row = json.loads(r[0])
    if (
        row["status"] != "scheduled"
        or not row.get("kickoff")
        or row.get("play_observed")
        or row.get("stage") != "regular"
        or row.get("stage_basis") not in ("provider_fields", "disjoint_phase_queries")
    ):
        return None
    return row["kickoff"]


def source_epoch(db, home, away, kickoff, end):
    """Return a compatible score schedule and the publication frontier of its last revision."""
    if not _has(db, "nba_game_versions"):
        return True, None
    ids = _score_candidates(db, home, away, kickoff, end, end)
    if not ids:
        return True, None
    if len(ids) != 1:
        return False, None
    previous, floor, seen = None, None, False
    for r in db.execute(
        """SELECT payload,available_at FROM nba_game_versions WHERE source='balldontlie'
            AND game_id=? AND received_at<=? AND available_at<=? ORDER BY version""",
        (next(iter(ids)), end, end),
    ):
        g = json.loads(r["payload"])
        if g.get("play_observed") or g["status"] in ("live", "final"):
            break
        key = (
            (
                team_code(g.get("home_abbr")),
                team_code(g.get("away_abbr")),
                g.get("kickoff"),
                g.get("home"),
                g.get("away"),
                g.get("stage"),
            )
            if g["status"] == "scheduled"
            else None
        )
        if seen and previous != key:
            floor = r["available_at"]
        previous, seen = key, True
    return bool(previous and previous[:3] == (home, away, kickoff)), floor


def score_veto(db, game, observed, as_of):
    if not _has(db, "nba_game_versions"):
        return None
    home, away, native = game["home"], game["away"], game["kickoff"]
    ids = _score_candidates(db, home, away, native, observed, as_of)
    if not ids:
        return None
    if len(ids) != 1:
        return "ambiguous_score_source_fixture"
    history = [
        json.loads(r[0])
        for r in db.execute(
            """SELECT payload FROM nba_game_versions WHERE source='balldontlie' AND game_id=? AND received_at<=?
           AND available_at<=? ORDER BY version""",
            (next(iter(ids)), observed, as_of),
        )
    ]
    current = history[-1]
    if any(v.get("play_observed") or v["status"] in ("live", "final") for v in history):
        return "score_source_observed_play"
    source = current.get("kickoff")
    if (
        not source
        and current["status"] == "scheduled"
        and not any(v.get("kickoff") for v in history)
    ):
        # A date-only schedule cannot pair. It also supplies no affirmative evidence against
        # a native pregame book; a previously withdrawn precise tip remains a conflict.
        return None
    if (
        not source
        or current["status"] != "scheduled"
        or team_code(current["home_abbr"]) != home
        or team_code(current["away_abbr"]) != away
        or _day(source) != _day(native)
    ):
        return "score_source_schedule_conflict"
    if abs(moment(source) - moment(native)) <= PAIR_TOLERANCE and observed >= source:
        return "score_source_scheduled_tip_reached"
    return None


def _catalogue_proof(db, day, before):
    if not _has(db, "nba_market_catalogues"):
        return None
    midnight = datetime.fromisoformat(day).replace(tzinfo=EAST)
    lower, upper = (
        stamp(midnight.astimezone(UTC)),
        stamp((midnight + timedelta(days=1)).astimezone(UTC)),
    )
    scan = db.execute(
        """SELECT completed,receipts FROM nba_market_catalogues WHERE completed<=?
        AND start_bound<=? AND end_bound>=? ORDER BY completed DESC LIMIT 1""",
        (before, lower, upper),
    ).fetchone()
    if not scan:
        return None
    ids = json.loads(scan["receipts"])
    receipts = [
        db.execute(
            """SELECT r.id,r.sha FROM nba_market_requests r JOIN nba_market_payloads p ON r.sha=p.sha
            WHERE r.id=? AND r.code=200 AND r.url LIKE '%/events/keyset' AND r.received<=?""",
            (rid, before),
        ).fetchone()
        for rid in ids
    ]
    if not receipts or not all(receipts):
        return None
    return dict(completed=scan["completed"], page_sha256=[r["sha"] for r in receipts])


def _pregame_receipt(db, match, before):
    if not _has(db, "nba_market_pregame"):
        return None
    r = db.execute(
        """SELECT q.sha FROM nba_market_pregame w JOIN nba_market_requests q ON q.id=w.receipt
        JOIN nba_market_payloads p ON p.sha=q.sha WHERE w.id=? AND w.version=? AND w.seen<=?
        AND q.received<=? AND q.code=200""",
        (match["venue_event_id"], match["venue_version"], stamp(before), stamp(before)),
    ).fetchone()
    return r[0] if r else None


def _regular_day_proof(db, root, day, fixtures, end):
    from .data import schedule_proof

    regular = [f for f in fixtures if f["stage"] == "regular"]
    unresolved = any(
        f["stage"] not in ("regular", "preseason", "playin", "postseason", "cup_final")
        for f in fixtures
    )
    if not regular:
        return 0, False, None, None
    tips = [
        moment(f[k])
        for f in regular
        for k in ("source_kickoff", "venue_kickoff")
        if f.get(k)
    ]
    if not tips:
        return 0, False, None, None
    first = min(tips)
    before = min(end, stamp(first - timedelta(microseconds=1)))
    score = schedule_proof(root, day, before)
    catalogue = _catalogue_proof(db, day, before)
    quality = (score or {}).get("quality") or {}
    phases = {"regular", "preseason"} | (
        {"ist"} if (score or {}).get("end", "") >= "2023-11-03" else set()
    )
    clean = set(quality.get("phase_rows") or {}) == phases and all(
        quality.get(key) == 0
        for key in (
            "quarantined_rows",
            "unidentified_rows",
            "pagination_errors",
            "overlapping_ids",
            "phase_mismatches",
            "unknown_stages",
            "scheduled_without_kickoff",
            "unknown_statuses",
            "unrecognized_statuses",
        )
    )
    paired = sum(f["status"] in MATCHED for f in regular)
    complete = bool(
        day < _day(end)
        and not unresolved
        and paired == len(regular)
        and catalogue
        and score
        and score["complete"]
        and clean
        and score.get("evidence_sha256")
    )
    return paired, complete, score, catalogue


def _score_proof(versions, tip, home, away, before):
    cutoff = stamp(before)
    history = [
        (v, v["receipt_sha256"], v["available_at"])
        for v in versions
        if v["received_at"] <= cutoff and v["available_at"] <= cutoff
    ]
    if not history:
        return (
            "missing_pregame_source",
            "no_score_schedule_before_both_starts",
            None,
            None,
        )
    latest, sha, _ = history[-1]
    state, stage = latest["status"], latest.get("stage", "unknown")
    if any(
        row.get("play_observed") or row["status"] in ("live", "final")
        for row, _, _ in history
    ):
        return "score_play_observed", "score_source_observed_play", sha, stage
    if latest.get("absence"):
        return "score_schedule_withdrawn", latest["absence"], sha, stage
    if state != "scheduled" or not latest.get("kickoff"):
        return (
            "score_schedule_unavailable",
            latest.get("conflict") or latest.get("timing_problem") or state,
            sha,
            stage,
        )
    if (
        latest["kickoff"] != tip
        or team_code(latest.get("home_abbr")) != home
        or team_code(latest.get("away_abbr")) != away
    ):
        return (
            "score_schedule_replaced",
            "different_score_fixture_at_cutoff",
            sha,
            stage,
        )
    old_tip = None
    identities = set()
    for row, _, available in history:
        if row.get("home") and row.get("away"):
            identities.add((row["home"], row["away"]))
        scheduled = row.get("kickoff") if row["status"] == "scheduled" else None
        if scheduled:
            if old_tip and old_tip != scheduled and available >= old_tip:
                return (
                    "score_schedule_replaced",
                    "score_schedule_replaced_after_previous_tip",
                    sha,
                    stage,
                )
            old_tip = scheduled
    if len(identities) > 1:
        return "identity_conflict", "score_source_team_identity_changed", sha, stage
    if stage == "unknown" or latest.get("stage_basis") not in (
        "provider_fields",
        "disjoint_phase_queries",
    ):
        return (
            "unverified_score_stage",
            latest.get("stage_basis") or "unproven_phase",
            sha,
            "unknown",
        )
    return None, None, sha, stage


def kickoff_differences(root, *, as_of=None):
    """Compare only versions independently known before both starts; no postgame schedule reconstruction."""
    end = stamp(_as_of(as_of))
    result = {
        "as_of": end,
        "threshold_seconds": int(PAIR_TOLERANCE.total_seconds()),
        "counts": {},
        "days": [],
        "paired_days": 0,
        "enough_days": False,
    }
    db = storage.open_existing(root)
    if db is None:
        return result
    with closing(db):
        events = {}
        if _has(db, "nba_market_schedule"):
            for r in db.execute(
                """SELECT g.id,g.home,g.away,s.kickoff,s.valid_from FROM nba_market_games g
                JOIN nba_market_schedule s ON s.id=g.id WHERE s.valid_from<=? ORDER BY s.version""",
                (end,),
            ):
                if r["valid_from"] < r["kickoff"]:
                    events[r["id"]] = r
        from .data import game_versions

        source, known, histories = {}, {}, defaultdict(list)
        for g in game_versions(root, as_of=end):
            gid, tip = g["game_id"], g.get("kickoff")
            histories[gid].append(g)
            if tip or g.get("game_date"):
                known[gid] = g
            if (
                tip
                and g["available_at"] < tip
                and g["status"] == "scheduled"
                and not g.get("play_observed")
            ):
                source[(gid, _day(tip))] = g
        counts, days, seen = Counter(), defaultdict(list), set()
        for (game_id, source_day), g in source.items():
            tip, home, away = (
                g["kickoff"],
                team_code(g["home_abbr"]),
                team_code(g["away_abbr"]),
            )
            before = min(_as_of(end), moment(tip) - timedelta(microseconds=1))
            match = schedule_match_db(db, home, away, tip, as_of=before)
            if match["venue_kickoff"]:
                before = min(
                    before, moment(match["venue_kickoff"]) - timedelta(microseconds=1)
                )
                match = schedule_match_db(db, home, away, tip, as_of=before)
            source_issue, source_reason, score_sha, stage = _score_proof(
                histories[game_id], tip, home, away, before
            )
            match["score_receipt_sha256"] = score_sha
            match["venue_receipt_sha256"] = (
                _pregame_receipt(db, match, before) if match["venue_event_id"] else None
            )
            if match["venue_event_id"]:
                seen.add(match["venue_event_id"])
                if match["status"] in MATCHED and match["reason"]:
                    match.update(status="venue_unavailable")
                elif match["status"] in MATCHED and not match["venue_receipt_sha256"]:
                    match.update(
                        status="unverified_venue_pregame",
                        reason="no_positive_pregame_venue_receipt",
                    )
                elif (
                    len(
                        _score_candidates(
                            db, home, away, tip, stamp(before), stamp(before)
                        )
                    )
                    != 1
                ):
                    match.update(
                        status="ambiguous", reason="ambiguous_score_source_fixture"
                    )
            if source_issue:
                match.update(status=source_issue, reason=source_reason)
            days[source_day].append(
                dict(
                    match,
                    home=home,
                    away=away,
                    source_game_id=game_id,
                    stage=stage or g.get("stage", "unknown"),
                )
            )
        # Keep score-only finals, withdrawals and absent kickoff times visible. None can prove
        # a schedule existed before play merely because a score was later published.
        for gid, g in known.items():
            tip = g.get("kickoff")
            day = _day(tip) if tip else g.get("game_date")
            if (gid, day) in source:
                continue
            home, away = team_code(g.get("home_abbr")), team_code(g.get("away_abbr"))
            candidates = [
                e
                for e in events.values()
                if _day(e["kickoff"]) == day and e["home"] == home and e["away"] == away
            ]
            native = candidates[0] if len(candidates) == 1 else None
            if native:
                seen.add(native["id"])
            days[day].append(
                dict(
                    status="missing_pregame_source",
                    reason="no_verified_pregame_score_schedule",
                    source_game_id=gid,
                    source_kickoff=None,
                    venue_event_id=native["id"] if native else None,
                    venue_kickoff=native["kickoff"] if native else None,
                    delta_seconds=None,
                    home=home,
                    away=away,
                    stage=g.get("stage", "unknown"),
                )
            )
        for event_id, event in events.items():
            if event_id not in seen:
                days[_day(event["kickoff"])].append(
                    dict(
                        home=event["home"],
                        away=event["away"],
                        source_game_id=None,
                        stage="unknown",
                        source_kickoff=None,
                        venue_event_id=event_id,
                        venue_kickoff=event["kickoff"],
                        delta_seconds=None,
                        status="missing_pregame_source",
                        reason="no_paired_score_schedule",
                    )
                )
        for day, fixtures in sorted(days.items()):
            items = Counter(i["status"] for i in fixtures)
            counts.update(items)
            paired = sum(items[k] for k in MATCHED)
            regular_paired, complete, proof, catalogue = _regular_day_proof(
                db, root, day, fixtures, end
            )
            result["days"].append(
                dict(
                    date_et=day,
                    counts=dict(items),
                    paired=paired,
                    regular_paired=regular_paired,
                    catalogue_proof=catalogue,
                    complete_day=complete,
                    score_scan_sha256=proof.get("evidence_sha256") if proof else None,
                    fixtures=sorted(
                        fixtures,
                        key=lambda f: (
                            f["source_kickoff"] or f["venue_kickoff"] or "",
                            f["home"],
                        ),
                    ),
                )
            )
            result["paired_days"] += int(complete)
        result.update(counts=dict(counts), enough_days=result["paired_days"] >= 3)
    return result
