"""Receipt-time maturity gate and resumable, free-budget score backfill."""

from collections import Counter
from contextlib import closing
from datetime import date, timedelta
from hashlib import sha256
import json

from . import data, model, storage
from .venue import EASTERN

POLICY = "previous-complete-season-and-current-through-last-completed-et-day-v1"
DAY = timedelta(days=1)
CHUNK_DAYS = 92
# Verified modern schedule formats only. Changing team count or season format
# requires checking the league's published schedule before extending this table.
TEAM_GAMES = {2020: 72, **dict.fromkeys(range(2021, 2027), 82)}
TEAMS = 30
# NBA season-opening dates (ET), from each annual schedule announcement.
OPENING = {year: date.fromisoformat(day) for year, day in (
    (2020, "2020-12-22"), (2021, "2021-10-19"), (2022, "2022-10-18"),
    (2023, "2023-10-24"), (2024, "2024-10-22"), (2025, "2025-10-21"),
    (2026, "2026-10-20"),
)}


def _days(start, end):
    for i in range((end - start).days + 1):
        yield start + i * DAY


def _window(season):
    # Includes the full modern regular season and both shortened 2020/21 endpoints.
    return OPENING.get(season, date(season, 10, 1)), date(season + 1, 6, 30)


def _season_counts(history, season, through=None):
    counts = Counter()
    played = set()
    unresolved_days = set()
    scheduled_slots = set()
    opponents = set()
    duplicates = 0
    for game in history:
        if game.get("source") != "balldontlie" or game["season"] != season:
            continue
        day = model.day_of(game)
        if through and day > through:
            continue
        if game.get("absence") == "absent_from_complete_range_scan" and not game.get("play_observed"):
            continue
        if game.get("stage") == "unknown":
            unresolved_days.add(day)
            continue
        if game.get("stage") != "regular" or game.get("status") in ("postponed", "cancelled", "withdrawn"):
            continue
        if (not model.eligible(game)
                or game.get("stage_basis") not in ("provider_fields", "disjoint_phase_queries")):
            unresolved_days.add(day)
            continue
        scores = game.get("home_score"), game.get("away_score")
        if (any(type(x) is not int or x <= 0 for x in scores) or scores[0] == scores[1]
                or game["home"] == game["away"] or not (_window(season)[0] <= day <= _window(season)[1])):
            unresolved_days.add(day)
            continue
        slots = {(day, game["home"]), (day, game["away"])}
        if game["game_id"] in played or slots & scheduled_slots:
            duplicates += 1
        played.add(game["game_id"])
        scheduled_slots.update(slots)
        counts.update((game["home"], game["away"]))
        opponents.add(frozenset((game["home"], game["away"])))
    expected = TEAM_GAMES.get(season)
    complete = (expected is not None and len(played) == TEAMS * expected // 2
                and len(counts) == TEAMS and set(counts.values()) == {expected}
                and len(opponents) == TEAMS * (TEAMS-1) // 2 and not unresolved_days and not duplicates)
    return ({"season": season, "finals": len(played), "teams": len(counts),
             "expected_finals": TEAMS * expected // 2 if expected else None,
             "expected_per_team": expected, "different_pairings": len(opponents), "team_min": min(counts.values(), default=0),
             "team_max": max(counts.values(), default=0), "duplicate_slots": duplicates,
             "unresolved_dates": len(unresolved_days), "complete": complete}, unresolved_days)


def _verified_days(root, start, end, cutoff):
    """Latest scan covering each date, already observed after that ET date ended."""
    db = storage.open_existing(root)
    if db is None:
        return set()
    with closing(db):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='nba_schedule_evidence'").fetchone():
            return set()
        rows = db.execute("""
            SELECT s.start,s.end,s.began,c.finished,c.complete,e.sha,
                   json_extract(e.manifest,'$.quality') AS quality
              FROM nba_schedule_scans s LEFT JOIN nba_schedule_completions c ON c.scan_id=s.id
              LEFT JOIN nba_schedule_evidence e ON e.scan_id=s.id
             WHERE s.start<=? AND s.end>=? AND s.began<=?
             ORDER BY s.began,s.id
        """, (end.isoformat(), start.isoformat(), data._iso(cutoff)))
        verified = set()
        for row in rows:
            first = max(start, date.fromisoformat(row["start"]))
            last = min(end, date.fromisoformat(row["end"]))
            # A schedule collected before play cannot certify an elapsed game day.
            ended = model.instant(row["began"]).astimezone(EASTERN).date() - DAY
            last = min(last, ended)
            if first > last:
                continue
            quality = json.loads(row["quality"]) if row["quality"] else {}
            phases = {"regular", "preseason"}
            if row["end"] >= "2023-11-03":
                phases.add("ist")
            issues = ("quarantined_rows", "unidentified_rows", "pagination_errors",
                      "overlapping_ids", "phase_mismatches", "unknown_stages")
            valid = (row["complete"] and row["finished"] and row["finished"] <= data._iso(cutoff)
                     and row["sha"] and phases <= quality.get("phase_rows", {}).keys()
                     and all(quality.get(key) == 0 for key in issues))
            if valid:
                verified.update(_days(first, last))
            else:
                verified.difference_update(_days(first, last))
    return verified


def coverage(root, history, fixture, as_of):
    """Small immutable proof recorded with each seal; caller supplies the receipt-filtered view."""
    cutoff = model.instant(as_of)
    history = [g for g in history if model._received_by(g, cutoff)]
    season = fixture["season"]
    through = min(model.day_of(fixture), cutoff.astimezone(EASTERN).date()) - DAY
    start, end = _window(season)
    through = min(through, end)
    previous, _ = _season_counts(history, season - 1)
    current, _ = _season_counts(history, season, through)
    days = max(0, (through - start).days + 1)
    previous_start, previous_end = _window(season - 1)
    required_previous = (previous_end - previous_start).days + 1
    all_days = _verified_days(root, previous_start, max(previous_end, through), cutoff)
    verified_previous = sum(previous_start <= d <= previous_end for d in all_days)
    verified_current = sum(start <= d <= through for d in all_days)
    reason = "ready"
    if season not in TEAM_GAMES or season - 1 not in TEAM_GAMES:
        reason = "season_format_unverified"
    elif not previous["complete"]:
        reason = "previous_season_incomplete"
    elif verified_previous != required_previous:
        reason = "previous_schedule_coverage_incomplete"
    elif current["duplicate_slots"] or current["unresolved_dates"]:
        reason = "current_results_unresolved"
    elif verified_current != days:
        reason = "current_schedule_coverage_incomplete"
    elif days and not current["finals"]:
        reason = "current_results_empty"
    elif through >= end and not current["complete"]:
        reason = "current_season_incomplete"
    return {"policy": POLICY, "eligible": reason == "ready", "reason": reason,
            "previous_season": season - 1, "previous_finals": previous["finals"],
            "expected_previous_finals": previous["expected_finals"], "previous_teams": previous["teams"],
            "previous_team_min": previous["team_min"], "previous_team_max": previous["team_max"],
            "previous_unresolved": previous["unresolved_dates"], "current_season": season,
            "through": through.isoformat(), "current_finals": current["finals"],
            "current_unresolved": current["unresolved_dates"], "current_duplicate_slots": current["duplicate_slots"],
            "required_previous_scan_days": required_previous, "previous_scan_days": verified_previous,
            "required_scan_days": days, "scan_days": verified_current}


def _backfill(root, seasons, through, *, sync=data.sync_bdl, clock=data._now):
    """Published chunks survive interruption. Completed but deficient seasons get a fresh pass."""
    attempts = []
    history = data.games_as_of(root, as_of=clock())
    for season in seasons:
        first, last = _window(season)
        end = min(through, last)
        if first > end:
            continue
        counts, unresolved = _season_counts(history, season, through)
        verified = _verified_days(root, first, end, clock())
        if not counts["complete"] and last <= through and len(verified) == (end-first).days + 1:
            # Missing finals can have no local row and hence no known day to retry.
            verified.clear()
        if not counts["finals"] or counts["duplicate_slots"]:
            verified.clear()
        for chunk_start in (first + i * DAY for i in range(0, (end-first).days + 1, CHUNK_DAYS)):
            chunk_end = min(chunk_start + (CHUNK_DAYS-1) * DAY, end)
            span = set(_days(chunk_start, chunk_end))
            needed = (span - verified) | (span & unresolved)
            if not needed:
                continue
            result = sync(root, min(needed), max(needed))
            attempts.append({k: result[k] for k in ("start", "end", "state", "complete", "pages",
                                                     "retry_at", "scan_id", "evidence_sha256") if k in result})
            if not result["complete"]:
                return attempts
    return attempts


def _target(season, through):
    return {"season": season, "game_date": (through + DAY).isoformat()}


def _warmup_command(args, *, clock=data._now, sync=data.sync_bdl):
    today = model.instant(clock()).astimezone(EASTERN).date()
    season = args.season if args.season is not None else today.year - int(today.month < 7)
    if season not in TEAM_GAMES or season - 1 not in TEAM_GAMES:
        raise ValueError("no verified prior/current NBA schedule format for this season")
    through = args.through or min(today - DAY, _window(season)[1])
    if not (date(season, 7, 1) - DAY <= through <= min(today - DAY, _window(season)[1])):
        raise ValueError("warmup end must be an elapsed Eastern date in the selected season")
    attempts = _backfill(args.data_dir, (season-1, season), through, clock=clock, sync=sync) if args.sync else []
    cutoff = clock()
    history = data.games_as_of(args.data_dir, as_of=cutoff)
    result = coverage(args.data_dir, history, _target(season, through), cutoff)
    return {**result, "attempts": attempts, "as_of": data._iso(cutoff),
            "source": "balldontlie", "free_budget_requests_per_minute": 4}


def _modern_command(args, *, clock=data._now, sync=data.sync_bdl):
    from . import model_replay

    # Freeze the warmup as well as the evaluated seasons and recipe. Unrelated
    # older rows in the deployed database must not change this comparison.
    seasons = range(2020, 2026)
    end = _window(2025)[1]
    if model.instant(clock()).astimezone(EASTERN).date() <= end:
        raise ValueError("modern study window has not fully elapsed")
    attempts = _backfill(args.data_dir, seasons, end, clock=clock, sync=sync) if args.sync else []
    cutoff = clock()
    history = data.games_as_of(args.data_dir, as_of=cutoff)
    checks = []
    verified = _verified_days(args.data_dir, _window(2020)[0], end, cutoff)
    for season in seasons:
        start, stop = _window(season)
        counts, _ = _season_counts(history, season)
        counts["required_scan_days"] = (stop - start).days + 1
        counts["scan_days"] = sum(start <= d <= stop for d in verified)
        counts["complete"] &= counts["scan_days"] == counts["required_scan_days"]
        checks.append(counts)
    ready = all(check["complete"] for check in checks)
    result = {"state": "complete" if ready else "incomplete_verified_seasons", "complete": ready,
              "source": "balldontlie", "data_grade": "technical_final_archive",
              "warmup_season": 2020, "evaluated_seasons": [2021, 2025], "recipe": model.RECIPE,
              "market_receipts": 0, "paper_eligible": False, "season_coverage": checks, "attempts": attempts,
              "as_of": data._iso(cutoff), "b2b_margin_points_fixed": model.B2B_MARGIN_POINTS}
    if ready:
        rows = [g for g in history if g["season"] in seasons and model.eligible(g)]
        canonical = sorted((g["game_id"], g["game_date"], g["season"], g["home"], g["away"],
                            g.get("neutral"), g.get("home_abbr"), g.get("away_abbr"),
                            g["home_score"], g["away_score"]) for g in rows)
        result["normalized_scores_sha256"] = sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest()
        result["replays"] = {f"lag{lag*24}": model_replay.backtest(
            rows, first_season=2021, last_season=2025, lag_days=lag, replicates=10000)
            for lag in (1, 2)}
    return result


def register(actions):
    p = actions.add_parser("warmup", help="Validate or resume the full prior season and season-to-date BDL receipts")
    p.add_argument("--season", type=int, help="season starting year; defaults to current Eastern season")
    p.add_argument("--through", type=date.fromisoformat, help="inclusive elapsed Eastern date (default yesterday)")
    p.add_argument("--sync", action="store_true", help="backfill missing periods using the shared free request budget")
    p.set_defaults(func=_warmup_command, result_exit_code=lambda r: int(not r["eligible"]))
    p = actions.add_parser("modern-study", help="Fixed 2021/22–2025/26 pooled technical replay with a full 2020/21 warmup")
    p.add_argument("--sync", action="store_true", help="resume missing periods via the free BDL games endpoint")
    p.set_defaults(func=_modern_command, result_exit_code=lambda r: int(not r["complete"]))
