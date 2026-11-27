"""Inspect and verify the pinned five-league archive, membership and field rights."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import date
from itertools import permutations
from pathlib import Path

from . import context_feeds, league_press, league_registry as registry, league_rosters, news_roster, news_sources, store
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


@contextmanager
def _health_database(path):
    """Read one coherent snapshot without initializing a collector or running migrations."""
    if not path.is_file():
        yield None, set()
        return
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        relations = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        yield db, relations


def _source_check(clubs, key, source, kind, at, state, **fields):
    clubs[key][source] = {"last_check_utc": at, "collector_state": state, "kind": kind,
                          "has_observation": False, **fields}
    return clubs[key][source]


def _stored_checks(db, tables, season, leagues, clubs, at):
    relation = next((r for r in ("news_check_history", "news_checks") if r in tables), None)
    if relation is None:
        return False
    columns = {r[1] for r in db.execute(f"PRAGMA table_info({relation})")}
    # Pre-receipt databases recorded the attempts alone.
    detail = ("CASE WHEN json_valid(detail) THEN CASE WHEN json_type(detail)='object' THEN detail END END"
              if "detail" in columns else "NULL")
    for league in leagues:
        query = f"""WITH checks AS (
              SELECT team_key,source,fixture_id,checked_at,state,id,{detail} AS report
              FROM {relation} WHERE season=? AND fixture_id GLOB ? AND checked_at<=?
            ), ranked AS (
              SELECT *,row_number() OVER (PARTITION BY team_key,source ORDER BY checked_at DESC,id DESC) AS position,
                max(CASE WHEN json_extract(report,'$.articles_added')>0 OR json_extract(report,'$.claims_added')>0
                    THEN checked_at END) OVER (PARTITION BY team_key,source) AS last_item
              FROM checks)
            SELECT team_key,source,fixture_id,checked_at,state,report,last_item FROM ranked WHERE position=1"""
        for club, source, fixture, checked, state, raw, last_item in db.execute(query, (season, f"{league}/{season}/*", at)):
            key = f"{league}/{club}"
            if key not in clubs:
                continue
            report = json.loads(raw) if raw else {}
            _source_check(clubs, key, source, "team_news", checked, state, fixture=fixture,
                          response_sha256=report.get("response_sha256"), provider_counts=report.get("provider_counts"),
                          latest_observation_utc=last_item, has_observation=bool(last_item))
        if "news_articles" not in tables:
            continue
        # The legacy wire also stores season/club articles without a fixture. Its reviewed
        # provider scope is English clubs only. All other rows need an explicit fixture.
        query = """WITH revisions AS (
             SELECT *,row_number() OVER (PARTITION BY team_key,source,external_id
                        ORDER BY updated_at DESC,seq DESC) AS revision_rank FROM news_articles
             WHERE season=? AND recorded_at<=? AND (fixture_id GLOB ? OR
               (fixture_id IS NULL AND ? AND source IN (?,?)))
           ), active AS (
             SELECT *,row_number() OVER (PARTITION BY team_key,source ORDER BY recorded_at DESC,seq DESC) AS position,
                    count(*) OVER (PARTITION BY team_key,source) AS total
             FROM revisions WHERE revision_rank=1 AND retracted=0)
           SELECT team_key,source,fixture_id,recorded_at,url,total FROM active WHERE position=1"""
        params = (season, at, f"{league}/{season}/*", league == "epl", news_sources.WIRE, news_sources.RUMOURS)
        for club, source, fixture, received, url, total in db.execute(query, params):
            key = f"{league}/{club}"
            if key not in clubs:
                continue
            row = clubs[key].get(source)
            if row is None:
                row = _source_check(clubs, key, source, "team_news", None, "article_received", fixture=fixture)
            row.update(has_observation=True, latest_observation_utc=max(row.get("latest_observation_utc") or "", received),
                       article_count=total, latest_article={"recorded_at": received, "url": url})
    return True


def _legacy_rosters(db, tables, season, teams, clubs, at):
    if "epl" not in teams or not tables.intersection({"news_roster_checks", "news_roster_receipts"}):
        return False
    source = news_roster.SOURCE
    for club, status, raw in (db.execute("SELECT club,status,report FROM news_roster_checks WHERE season=?", (season,))
                              if "news_roster_checks" in tables else ()):
        key = f"epl/{club}"
        if key in clubs:
            report = json.loads(raw)
            # Failed and running checks in this schema have no attempt timestamp.
            checked = report.get("seen_at") if report.get("status") == status else None
            _source_check(clubs, key, source, "roster", checked, status, has_roster=False,
                          roster_players=0, latest_receipt_utc=None)
    if "news_roster_receipts" not in tables:
        return True
    for club, seen, digest in news_roster._latest(db, season, at):
        key = f"epl/{club}"
        if key not in clubs:
            continue
        row = clubs[key].get(source)
        if row is None:
            row = _source_check(clubs, key, source, "roster", None, "receipt_only")
        record = db.execute("SELECT document FROM news_roster_content WHERE digest=?", (digest,)).fetchone() if (
            "news_roster_content" in tables) else None
        document = json.loads(record[0]) if record else {}
        valid = (document.get("source") == source and document.get("season") == season
                 and document.get("team") in teams["epl"] and teams["epl"][document["team"]] == key
                 and store.digest(document) == digest and not document.get("withheld"))
        count = len(document.get("players", [])) if valid else 0
        row.update(latest_receipt_utc=seen, has_roster=bool(count), roster_players=count, has_observation=bool(count),
                   roster_state=("expired" if at >= news_roster._expiry(seen) else "available") if count else "unknown")
    return True


def _domestic_rosters(db, tables, root, season, teams, clubs, at):
    if not tables.intersection({"local_roster_polls", "local_roster_receipts"}):
        return False
    source = league_rosters._ROSTER_SOURCE
    scopes = {key: scope for league, names in teams.items() for raw, key in names.items()
              if (scope := league_rosters.roster_coverage(raw, season, league))}
    if "local_roster_polls" in tables:
        for league, club, attempt, state, reason in db.execute(
                "SELECT league,club,attempted_at,state,reason FROM local_roster_polls WHERE season=? AND attempted_at<=?",
                (season, at)):
            key = f"{league}/{club}"
            if key in scopes:
                _source_check(clubs, key, source, "roster", attempt, state, reason=reason,
                              latest_receipt_utc=None, has_roster=False, roster_players=0)
    if "local_roster_receipts" not in tables:
        return "local_roster_polls" in tables
    for league, club, ready in db.execute(
            "SELECT league,club,max(ready_at) FROM local_roster_receipts WHERE season=? AND ready_at<=? GROUP BY league,club",
            (season, at)):
        key = f"{league}/{club}"
        if key not in scopes:
            continue
        row = clubs[key].get(source)
        if row is None:
            row = _source_check(clubs, key, source, "roster", None, "receipt_only")
        # Verify what the latest receipt actually contained, independently of the
        # outcome of a subsequent poll. Age is exposed separately from receipt.
        if "local_roster_projections" in tables:
            roster = league_rosters._local_roster_as_of(db, root, scopes[key], ready)
        else:
            roster = {"players": [], "reason": "missing_roster_projection"}
        count = len(roster["players"])
        fresh = store.instant(at) - store.instant(ready) <= league_rosters._ROSTER_MAX_AGE
        row.update(latest_receipt_utc=ready, has_roster=bool(count), roster_players=count, has_observation=bool(count),
                   roster_state=("available" if fresh else "expired") if count else roster.get("reason") or "unknown")
    return True


def _press_health(db, tables, season, teams, clubs, at):
    if "press_releases" not in tables:
        return {}
    batches = {}
    # Editions may be backfilled later than a newer edition. Keep the newest
    # indexed edition and the latest local scan clock independently.
    query = """WITH releases AS (
          SELECT r.*,j.value AS league FROM press_releases r JOIN json_each(r.leagues) j
          WHERE r.season=? AND r.received<=?
        ), ranked AS (
          SELECT *,row_number() OVER (PARTITION BY league ORDER BY edition DESC) AS position,
                 max(received) OVER (PARTITION BY league) AS last_scan FROM releases)
        SELECT league,edition,received,scanned,last_scan FROM ranked WHERE position=1"""
    source = context_feeds.GKG
    for league, edition, received, scanned, last_scan in db.execute(query, (season, at)):
        if league not in teams:
            continue
        batches[league] = {"edition": edition, "received_at": received, "records_scanned": scanned,
                           "last_scan_utc": last_scan, "attribution": league_press.CREDIT}
        state = "checked_no_links" if "press_clubs" in tables else "link_table_unavailable"
        for key in teams[league].values():
            _source_check(clubs, key, source, "news_links", last_scan, state, latest_shared_edition=edition,
                          observed_links=0 if "press_clubs" in tables else None, latest_link=None)
    if batches and "press_clubs" in tables:
        query = """WITH links AS (
              SELECT c.league,c.team,c.edition,c.url,c.document,r.received,
                     row_number() OVER (PARTITION BY c.league,c.team ORDER BY c.edition DESC,c.url) AS position
              FROM press_clubs c JOIN press_releases r USING(edition)
              WHERE r.season=? AND r.received<=? AND c.league IN (SELECT value FROM json_each(r.leagues))
            ), totals AS (SELECT league,team,count(DISTINCT url) AS total FROM links GROUP BY league,team)
            SELECT l.league,l.team,l.edition,l.url,l.document,l.received,t.total
            FROM links l JOIN totals t USING(league,team) WHERE l.position=1"""
        for league, raw_name, edition, url, document, received, total in db.execute(query, (season, at)):
            key = teams.get(league, {}).get(raw_name)
            if key is None:
                continue
            link = json.loads(document)
            clubs[key][source].update(collector_state="links_observed", has_observation=True, observed_links=total,
                                      latest_link={"edition": edition, "url": url, "received_at": received,
                                                   "index_seen_at": link.get("index_seen_at"),
                                                   "matched_via": link.get("matched_via"), "club_label": link.get("club_label")})
    return batches


def _health_totals(groups):
    rows = list(groups)
    checked = sum(bool(group) for group in rows)
    observed = sum(any(record["has_observation"] for record in group) for group in rows)
    return {"teams_with_local_checks": checked, "teams_with_observations": observed,
            "teams_checked_without_observations": checked - observed, "teams_without_checks": len(rows) - checked}


def local_source_health(data_dir: Path, season: str, leagues=None, *, epl_data_dir=None) -> dict:
    """Report club-scoped checks and received items from the local collector stores."""
    leagues = list(registry.PROFILES) if leagues is None else list(leagues)
    teams = {league: {raw: f"{league}/{registry.club_key(league, season, raw)}"
                      for raw in registry.members(league, season)} for league in leagues}
    clubs = {key: {} for names in teams.values() for key in names.values()}
    root = Path(data_dir).expanduser()
    paths = {root / "runtime.sqlite3": list(leagues)}
    if epl_data_dir is not None and "epl" in teams:
        legacy = Path(epl_data_dir).expanduser() / "runtime.sqlite3"
        if legacy.is_file() and legacy.resolve() != (root / "runtime.sqlite3").resolve():
            paths[root / "runtime.sqlite3"].remove("epl")
            paths[legacy] = ["epl"]
    at = store.now_utc()
    found_db = found_schema = False
    for path, competitions in paths.items():
        if not competitions:
            continue
        with _health_database(path) as (db, tables):
            found_db |= db is not None
            found_schema |= _stored_checks(db, tables, season, competitions, clubs, at)
            if "epl" in competitions:
                found_schema |= _legacy_rosters(db, tables, season, teams, clubs, at)
    roster_root = root / "five-league-rosters"
    with _health_database(roster_root / "runtime.sqlite3") as (db, tables):
        found_db |= db is not None
        found_schema |= _domestic_rosters(db, tables, roster_root, season, teams, clubs, at)
    with _health_database(root / "team-press" / "index.sqlite3") as (db, tables):
        found_db |= db is not None
        found_schema |= "press_releases" in tables
        batches = _press_health(db, tables, season, teams, clubs, at)
    sources = sorted({source for group in clubs.values() for source in group})
    by_source = {s: _health_totals([group[s]] if s in group else [] for group in clubs.values()) for s in sources}
    news = _health_totals([r for r in group.values() if r["kind"] != "roster"] for group in clubs.values())
    return {"season": season, "checked_at": at, "state": ("read_local_checks" if found_schema else
            "no_news_collection_schema" if found_db else "no_local_database"), "registered_teams": len(clubs),
            **_health_totals(list(group.values()) for group in clubs.values()), "news": news, "by_source": by_source,
            "latest_shared_gdelt_batch_by_league": batches, "clubs": clubs,
            "scope": "Checks and received items for the selected season. News observations count recorded club-specific items. "
                     "Roster counts describe the latest stored receipt; roster_state gives its freshness. "
                     "GDELT counts distinct matched URLs across stored batches. Source totals use all selected registered teams "
                     "as the denominator. Fixture-window eligibility is checked by news snapshot."}


def _league_option(parser):
    parser.add_argument("--league", choices=list(registry.PROFILES))


def register(commands):
    from . import league_openfootball_refresh, league_source_rights, league_study, news_endpoint_study

    parser = commands.add_parser("leagues", help="五国赛事、赛季成员、历史异常、来源与基线")
    subcommands = parser.add_subparsers(dest="league_action", required=True)
    download = subcommands.add_parser("prepare-history", help="下载并核验各联赛最近九季的固定 OpenFootball 训练文件")
    download.add_argument("directory", type=Path)
    download.set_defaults(func=lambda a: league_openfootball_refresh.prepare_history(a.directory))
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
    health.add_argument("--epl-data-dir", type=Path, help="另读原英超运行目录，合并其逐队收件")
    health.set_defaults(func=lambda a: local_source_health(a.data_dir, a.season, [a.league] if a.league else None,
                                                         epl_data_dir=a.epl_data_dir))
    audit = subcommands.add_parser("inventory", help="逐个校验本地固定历史文件，并报告已知异常")
    audit.add_argument("history", type=Path, help="包含 YYYY-YY/de.1.json 等文件的根目录")
    _league_option(audit)
    audit.add_argument("--from-season", default=AUDITED_START)
    audit.add_argument("--to-season", default=AUDITED_END)
    audit.set_defaults(func=lambda a: inventory(a.history, first=a.from_season, last=a.to_season, leagues=[a.league] if a.league else None))
    league_study.register(subcommands)
    news_endpoint_study.register(subcommands)
