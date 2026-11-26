"""Inspect and verify the pinned five-league archive, membership and field rights."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, date, datetime
from itertools import permutations
from pathlib import Path

from . import league_registry as registry
from .score_bounds import valid_score

AUDITED_START = "2018-19"
AUDITED_END = "2025-26"
ROUND = re.compile(r"Matchday ([1-9][0-9]?)")
COVID_RULING = "https://www.conseil-etat.fr/fr/arianeweb/CE/decision/2020-10-23/440810"


def _years(first: str, last: str) -> list[str]:
    start, end = registry.season_start_year(first), registry.season_start_year(last)
    if start > end:
        raise ValueError("Start season must precede end season")
    return [f"{y:04d}-{(y + 1) % 100:02d}" for y in range(start, end + 1)]


def exception_record(exception: registry.MatchException) -> dict:
    return {**asdict(exception), "train_regulation": exception.train_regulation}


def verify_season(history: Path, league: str, season: str) -> dict:
    prof = registry.profile(league)
    relative_path = Path(season) / prof.source_file
    raw = (history / relative_path).read_bytes()
    blob = registry.git_blob(raw)
    if blob != registry.source_blob(league, season):
        raise ValueError(f"{relative_path}: Git blob differs from {registry.SOURCE_COMMIT}")
    rows = json.loads(raw)["matches"]
    expected_members = registry.members(league, season)
    expected_rounds = registry.round_count(league, season)
    by_round, paired, forms, statuses, special, disposition = (Counter() for _ in range(6))
    round_clubs = defaultdict(set)
    missing_by_round = Counter()
    outside_stage = Counter()
    invalid_dates = []
    timing = Counter()
    dates = []
    incident_rows = []
    raw_names = set()
    window = registry.season_window(league, season)
    for m in rows:
        matchday = ROUND.fullmatch(str(m.get("round", "")))
        if not matchday:
            outside_stage[str(m.get("round"))] += 1
            continue
        rnd = int(matchday[1])
        h, a = m["team1"], m["team2"]
        if h == a or round_clubs[rnd].intersection((h, a)):
            raise ValueError(f"{relative_path}: repeated club in round {rnd}: {h}, {a}")
        round_clubs[rnd].update((h, a))
        raw_names.update((h, a))
        paired[h, a] += 1
        by_round[rnd] += 1
        day = date.fromisoformat(m["date"])
        if league == "epl" and season == "2019-20" and (day.year, day.month) == (2019, 7):
            timing["pinned_epl_july_year_correction"] += 1
            day = day.replace(year=2020)
        if not window[0] <= day < window[1]:
            invalid_dates.append(m["date"])
        dates.append(day)
        timing["minute" if m.get("time") else "date_only"] += 1
        score = m.get("score")
        if isinstance(score, dict):
            score = score.get("ft")
            forms["object"] += 1
        elif isinstance(score, list):
            forms["array"] += 1
        else:
            forms["absent"] += 1
        status = m.get("status") or "unmarked"
        statuses[status] += 1
        if not valid_score(score):
            missing_by_round[rnd] += 1
        case = registry.fixture_exception(league, season, h, a)
        if case:
            if case.archive_date != m["date"]:
                raise ValueError(f"{relative_path}: special date mismatch at {h} vs {a}")
            special[case.kind] += 1
            # COVID cancellations share the same governing ruling. Teams and date remain
            # individually addressable in the frozen membership asset and input blob.
            if case.kind != "cancelled":
                incident_rows.append({"round": rnd, "home": h, "away": a,
                                      "source_status": None if status == "unmarked" else status,
                                      "source_score": score, **exception_record(case)})
            if case.train_regulation and valid_score(score):
                disposition["regulation_label_after_verified_completion_date_only"] += 1
            else:
                disposition["quarantined_exception"] += 1
        elif status != "unmarked":
            disposition["unverified_source_status"] += 1
            incident_rows.append({"round": rnd, "home": h, "away": a,
                                  "kind": "unverified_source_status", "source_status": status})
        elif valid_score(score):
            disposition["ordinary_scored"] += 1
        else:
            disposition["missing_or_invalid_regulation_score"] += 1
    registry.validate_members(league, season, raw_names, require_complete=True)
    expected_pairs = set(permutations(expected_members, 2))
    if set(paired) != expected_pairs or len(paired) != sum(paired.values()):
        raise ValueError(f"{relative_path}: missing or duplicate ordered regular-season opponents")
    if set(by_round) != set(range(1, expected_rounds + 1)) or any(
            n != len(expected_members) // 2 for n in by_round.values()):
        raise ValueError(f"{relative_path}: incomplete regular round")
    if invalid_dates:
        raise ValueError(f"{relative_path}: dates outside registered season: {sorted(set(invalid_dates))}")
    return {
        "league": league, "season": season, "path": relative_path.as_posix(),
        "git_blob_sha1": blob, "sha256": hashlib.sha256(raw).hexdigest(),
        "source_url": f"{registry.SOURCE_ROOT}/{relative_path.as_posix()}",
        "team_count": len(raw_names), "rounds": len(by_round), "regular_rows": sum(by_round.values()),
        "ordered_pairs": len(paired), "non_regular_stages": dict(outside_stage),
        "source_dates_after_pinned_repairs": [min(dates).isoformat(), max(dates).isoformat()],
        "source_clock_zone": prof.timezone, "time_precision": dict(timing),
        "raw_score_shape": dict(forms), "raw_status": dict(statuses),
        "missing_or_invalid_scores_by_round": dict(sorted(missing_by_round.items())),
        "special_by_kind": dict(special), "dispositions": dict(disposition),
        "exceptions_besides_season_cancellations": incident_rows,
        **({"season_cancellations_ruling": COVID_RULING} if special["cancelled"] else {}),
    }


def season_members(league: str, season: str) -> dict:
    names = registry.members(league, season)
    year = registry.season_start_year(season)
    prev = f"{year - 1:04d}-{year % 100:02d}"
    current = {registry.club_key(league, season, name): name for name in names}
    prior = {}
    if prev in registry.source_blobs(league):
        prior = {registry.club_key(league, prev, name): name for name in registry.members(league, prev)}
    def row(key, raw):
        if not prior:
            change = "previous_season_unavailable"
        elif key not in prior:
            change = "entered_league"
        else:
            change = "retained" if raw == prior[key] else "retained_source_spelling_changed"
        return {"club_id": f"{league}/{key}", "source_name": raw, "change": change,
                "previous_source_name": prior.get(key)}
    return {"league": league, "season": season, "stage": "regular", "blob": registry.source_blob(league, season),
            "clubs": [row(key, raw) for key, raw in sorted(current.items())],
            "left_league": [f"{league}/{key}" for key in sorted(prior.keys() - current.keys())]}


def inventory(history: Path, *, first: str = AUDITED_START, last: str = AUDITED_END, leagues=None) -> dict:
    leagues = list(registry.PROFILES) if leagues is None else list(leagues)
    years = _years(first, last)
    records = [verify_season(history, code, season) for code in leagues for season in years]
    audit_years = set(_years("2018-19", "2024-25"))
    joins = {}
    for code in leagues:
        aliases = registry.alias_rows(code)
        joins[code] = {"explicit_source_alias_pairs": len(aliases),
                       "clubs_split_in_2018_19_to_2024_25_without_aliases": sum(
                            all(audit_years.intersection(spans) for spans in r["effective_seasons_by_spelling"].values())
                            for r in aliases)}
    totals = Counter()
    for record in records:
        totals.update(record["dispositions"])
    return {"registry": registry.REGISTRY_VERSION, "commit": registry.SOURCE_COMMIT,
            "license": f"{registry.SOURCE_ROOT}/LICENSE.md",
            "first_season": first, "last_season": last, "verified_files": len(records),
            "regular_rows": sum(row["regular_rows"] for row in records),
            "verification": "Git blob bytes, season membership, unique oriented home/away pairs and full regular rounds",
            "result_clock": "Archive final scores are retrospective. Normal eligibility here is technical reconstruction only. Resumed/replayed scores may train only after the verified completion date and configured lag. The archive alone supplies no contemporary receipt or actual first-whistle evidence.",
            "exception_scope": "All source statuses plus independently documented unmarked fixture exceptions in this register. An unmarked row is not independent proof that an interruption never occurred.",
            "alias_audit": joins, "dispositions": dict(totals), "files": records}


def local_source_health(data_dir: Path, season: str, leagues=None) -> dict:
    """Latest local collector observations, including failures; no implicit field approval."""
    leagues = list(registry.PROFILES) if leagues is None else list(leagues)
    health = {f"{league}/{registry.club_key(league, season, raw)}": {}
              for league in leagues for raw in registry.members(league, season)}
    path = Path(data_dir).expanduser() / "runtime.sqlite3"
    state = "no_local_database"
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            db.execute("BEGIN")
            relations = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
            relation = next((r for r in ("news_check_history", "news_checks") if r in relations), None)
            state = "no_news_collection_schema" if relation is None else "read_local_checks"
            if relation:
                for league in leagues:
                    # IDs scoped to this competition and this exact season. An opaque
                    # legacy ID has no proven league in this view.
                    query = f"""SELECT team_key, source, fixture_id, checked_at, state FROM (
                         SELECT team_key, source, fixture_id, checked_at, state,
                                row_number() OVER (PARTITION BY team_key, source ORDER BY checked_at DESC, id DESC) AS rank
                         FROM {relation} WHERE season=? AND fixture_id GLOB ?
                      ) WHERE rank=1 ORDER BY team_key, source"""
                    for club, source, fixture, at, result in db.execute(query, (season, f"{league}/{season}/*")):
                        key = f"{league}/{club}"
                        if key in health:
                            health[key][source] = {"fixture": fixture, "last_check_utc": at, "collector_state": result}
    return {"season": season, "checked_at": datetime.now(UTC).isoformat(), "state": state,
            "registered_teams": len(health), "teams_with_local_checks": sum(bool(v) for v in health.values()),
            "clubs": health, "scope": "Latest locally recorded collector check for each explicitly scoped club/source, including failures. For actual fixture-window coverage use news snapshot, which verifies the receipt, freshness, field and scope."}


def _league_option(parser):
    parser.add_argument("--league", choices=list(registry.PROFILES))


def register(commands):
    from . import league_source_rights, league_study, news_endpoint_study

    parser = commands.add_parser("leagues", help="五国赛事、赛季成员、历史异常、来源与基线")
    subcommands = parser.add_subparsers(dest="league_action", required=True)
    listing = subcommands.add_parser("list", help="各联赛常规赛赛制和来源显示时区")
    listing.add_argument("--season", default="2026-27")
    listing.set_defaults(func=lambda a: [
        {**asdict(p), "source_file": p.source_file, "season": a.season, "stage": "regular",
         "teams": registry.team_count(code, a.season), "rounds": registry.round_count(code, a.season),
         "fixtures": registry.fixture_count(code, a.season)} for code, p in registry.PROFILES.items()])
    clubs = subcommands.add_parser("clubs", help="赛季成员、同队显示名变更与进出联赛")
    _league_option(clubs)
    clubs.add_argument("--season", default="2026-27")
    clubs.set_defaults(func=lambda a: [season_members(c, a.season) for c in ([a.league] if a.league else registry.PROFILES)])
    aliases = subcommands.add_parser("aliases", help="OpenFootball 别名、有效赛季和出处")
    _league_option(aliases)
    aliases.set_defaults(func=lambda a: [r for c in ([a.league] if a.league else registry.PROFILES) for r in registry.alias_rows(c)])
    rights = subcommands.add_parser("sources", help="逐队字段级来源、权利、缺项与备用来源")
    _league_option(rights)
    rights.add_argument("--season", default="2026-27")
    rights.set_defaults(func=lambda a: league_source_rights.rights_register([a.league] if a.league else None, season=a.season))
    health = subcommands.add_parser("health", help="本机逐队逐来源的最后一次检查、检查状态及缺件分母")
    _league_option(health)
    health.add_argument("--season", default="2026-27")
    health.set_defaults(func=lambda a: local_source_health(a.data_dir, a.season, [a.league] if a.league else None))
    audit = subcommands.add_parser("inventory", help="逐个校验本地固定历史文件，并报告已知异常")
    audit.add_argument("history", type=Path, help="包含 YYYY-YY/de.1.json 等文件的根目录")
    _league_option(audit)
    audit.add_argument("--from-season", default=AUDITED_START)
    audit.add_argument("--to-season", default=AUDITED_END)
    audit.set_defaults(func=lambda a: inventory(a.history, first=a.from_season, last=a.to_season, leagues=[a.league] if a.league else None))
    league_study.register(subcommands)
    news_endpoint_study.register(subcommands)
