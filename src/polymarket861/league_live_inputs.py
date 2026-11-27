"""Locally observed, league-scoped inputs for prospective technical forecasts."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zlib
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import timedelta
from email.utils import parsedate_to_datetime
from functools import lru_cache
from heapq import merge
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from . import archive_proof, big5_market, big5_market_schedule, football
from . import independent_results as wiki
from . import league_openfootball_refresh, league_registry as registry, league_wiki_cache
from . import market_provider, store
from .score_bounds import valid_score


@dataclass(frozen=True)
class LeagueInputs:
    fixtures: tuple[football.Fixture, ...]
    upcoming: tuple[football.Fixture, ...]
    provenance: dict


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pinned_receipts(
 league TEXT, season TEXT, blob TEXT, seen TEXT NOT NULL, PRIMARY KEY(league,season,blob));
CREATE TABLE IF NOT EXISTS wiki_receipts(
 league TEXT, season TEXT, revision INTEGER, published TEXT, seen TEXT, title TEXT,
 sha TEXT, body BLOB, PRIMARY KEY(league,season,revision));
CREATE INDEX IF NOT EXISTS wiki_receipt_timeline ON wiki_receipts(league,season,title,seen,revision,published,sha);
CREATE TABLE IF NOT EXISTS wiki_archive_generation(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), token BLOB NOT NULL);
CREATE TRIGGER IF NOT EXISTS wiki_archive_insert AFTER INSERT ON wiki_receipts BEGIN
 INSERT INTO wiki_archive_generation VALUES(1,randomblob(16)) ON CONFLICT(singleton) DO UPDATE SET token=excluded.token;
END;
CREATE TRIGGER IF NOT EXISTS wiki_archive_update AFTER UPDATE ON wiki_receipts BEGIN
 INSERT INTO wiki_archive_generation VALUES(1,randomblob(16)) ON CONFLICT(singleton) DO UPDATE SET token=excluded.token;
END;
CREATE TRIGGER IF NOT EXISTS wiki_archive_delete AFTER DELETE ON wiki_receipts BEGIN
 INSERT INTO wiki_archive_generation VALUES(1,randomblob(16)) ON CONFLICT(singleton) DO UPDATE SET token=excluded.token;
END;
CREATE TABLE IF NOT EXISTS wiki_checks(
 league TEXT, season TEXT, attempted TEXT, status TEXT, detail TEXT, PRIMARY KEY(league,season));
CREATE TABLE IF NOT EXISTS gamma_bindings(
 league TEXT, slug TEXT, seen TEXT, sha TEXT, fixture_id TEXT, kickoff TEXT,
 home_id TEXT, away_id TEXT, event_id TEXT, PRIMARY KEY(league,slug,seen,sha));
"""

# Exact identity crosswalks for different source spellings, restricted to the
# observed domestic season. Gamma aliases additionally bind the numeric club ID.
_GAMMA_NAMES = {
    ("lal", "2026-27", "3284301", "Real Racing Club"): "Real Racing Club de Santander",
    ("lal", "2026-27", "3284602", "RC Deportivo A Coruña"): "RC Deportivo La Coruña",
    ("bun", "2026-27", "101135", "BV Borussia 09 Dortmund"): "Borussia Dortmund",
}
_WIKI_NAMES = {
    "lal": {
        "Athletic Bilbao": "Athletic Club", "Atlético Madrid": "Club Atlético de Madrid",
        "Deportivo de A Coruña": "RC Deportivo La Coruña", "RCD Espanyol": "RCD Espanyol de Barcelona",
        "Racing de Santander": "Real Racing Club de Santander", "Rayo Vallecano": "Rayo Vallecano de Madrid",
        "Real Betis": "Real Betis Balompié", "Real Sociedad": "Real Sociedad de Fútbol",
    },
    "bun": {"SV Elversberg": "SV 07 Elversberg", "FC Bayern Munich": "FC Bayern München"},
    "sea": {"Inter Milan": "FC Internazionale Milano"},
    "fl1": {"RC Lens": "Racing Club de Lens", "Stade Rennais FC": "Stade Rennais FC 1901"},
}
_WIKI_PAGES = {"lal": "La Liga", "bun": "Bundesliga", "sea": "Serie A", "fl1": "Ligue 1"}
# These regular matchday 23 games moved forward for the Supercopa. The
# reviewed revision's football box includes both clubs, local kickoff and
# the competition's match-specific report link. Bind the complete source
# text to the review; later articles cannot inherit the reviewed facts.
_MOVED = {
    1388201732: ("2026–27 FC Barcelona season", "FC Barcelona", "Club Atlético de Madrid",
                 "2026-12-02T20:30:00.000000Z",
                 "a4b2922a7e976c01e43b45c6aace2c46bc0a6170aacbe790f495071a81366ed8",
                 "fc-barcelona-atletico-de-madrid-23"),
    1387899597: ("2026–27 Real Madrid CF season", "Real Sociedad de Fútbol", "Real Madrid CF",
                 "2026-12-02T18:00:00.000000Z",
                 "12b4a0c94cb1a10180e4797f9e2955fda6768e831798c20a080279eb36e49353",
                 "real-sociedad-real-madrid-23"),
}


def _season(league, stamp):
    local = football.utc(stamp).astimezone(ZoneInfo(registry.profile(league).timezone))
    year = local.year - (local.month < 8)
    return f"{year}-{(year + 1) % 100:02d}"


def _market_root(directory):
    root = Path(directory)
    nested = big5_market.research_root(root)
    return nested if (nested / "runtime.sqlite3").is_file() else root


def _market_db(root):
    db = sqlite3.connect((root / "runtime.sqlite3").resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    return db


def _identity_conflicted(db, slug, at):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='big5_identity_conflicts'").fetchone():
        return False
    return db.execute(
        "SELECT 1 FROM big5_identity_conflicts WHERE slug=? AND received<=? AND received>=COALESCE(("
        "SELECT MAX(received) FROM big5_schedule_receipts WHERE slug=? AND received<=? "
        "AND origin='validated_receipt'),'') LIMIT 1", (slug, at, slug, at)).fetchone() is not None


def _eligible_market(db, binding, at):
    slug, kick = binding["slug"], store.iso(binding["kickoff"])
    start = store.iso(binding["receipt"])
    if not max(start, binding.get("known_at", start)) <= at < kick:
        return False
    head = db.execute("SELECT * FROM big5_events WHERE slug=? AND fixture=slug", (slug,)).fetchone()
    if not head or tuple(head[k] for k in ("home_id", "away_id", "event_id")) != tuple(
        binding[k] for k in ("home_id", "away_id", "event_id")
    ):
        return False
    if head["receipt"] <= at and head["kickoff"] != kick:
        return False
    stale = db.execute(
        "SELECT 1 FROM big5_catalog_http WHERE fixture=? AND received<=? AND fresh=0", (slug, at)
    ).fetchone()
    return (not stale and big5_market_schedule.play_history(db, slug, start, at) is None
            and big5_market_schedule.consistent_during(db, slug, kick, start, at))


def still_pregame(market_state_dir, binding, as_of):
    """Recheck the registered market identity, schedule and subsequent play/stop signals at seal time."""
    root = _market_root(market_state_dir)
    try:
        with closing(_market_db(root)) as db:
            db.execute("BEGIN")
            at = store.iso(as_of)
            if _identity_conflicted(db, binding["slug"], at) or not _eligible_market(db, binding, at):
                return False
            row = db.execute(
                "SELECT * FROM big5_schedule_receipts WHERE slug=? AND received<=? AND origin='validated_receipt' "
                "ORDER BY received DESC,sha DESC LIMIT 1", (binding["slug"], at),
            ).fetchone()
            if row is None:
                return False
            if (row["received"], row["sha"]) == (binding.get("last_verified"), binding.get("last_sha256")):
                return True
            season, league = binding["season"], binding["league"]
            event, identity = _lookup_event(db, root, league, row, {})
            return (identity[3:] == tuple(binding[k] for k in ("kickoff", "home_id", "away_id", "event_id"))
                    and _gamma_clubs(event, identity, league, season) == (binding["home"], binding["away"]))
    except (sqlite3.Error, OSError, KeyError, TypeError, ValueError):
        return False


def _wiki_title(league, season):
    name = f"{season.replace('-', '–')} {_WIKI_PAGES[league]}"
    return name if league == "sea" else f"Template:{name} table"


def _wiki_scores(raw, league, season):
    title = _wiki_title(league, season)
    page, = wiki._query(raw, [title])
    if page.get("ns") != (0 if league == "sea" else 10):
        raise ValueError("unexpected Wikimedia league page namespace")
    rev, = page["revisions"]
    if type(rev.get("revid")) is not int or rev["revid"] <= 0:
        raise ValueError("missing Wikimedia revision ID")
    content = re.sub(r"<!--.*?-->", "", rev["slots"]["main"]["content"], flags=re.S)
    members = set(registry.members(league, season))
    teams = {}
    for key, link in re.findall(r"^\s*\|\s*name_([^\s=]+)\s*=\s*\[\[([^\]|\n]+)(?:\|[^\]\n]+)?\]\][ \t]*$", content, re.M):
        name = _WIKI_NAMES.get(league, {}).get(link, link) if season == "2026-27" else link
        if name not in members or key in teams:
            raise ValueError("ambiguous Wikimedia club or unregistered club name")
        teams[key] = name
    if set(teams.values()) != members or len(teams) != len(members):
        raise ValueError("incomplete Wikimedia domestic club identities")
    scores = {}
    for h, a, cell in re.findall(r"^[ \t]*\|[ \t]*match_([^\s_=]+)_([^\s_=]+)[ \t]*=[ \t]*([^\n]*)$", content, re.M):
        if h not in teams or a not in teams or h == a:
            raise ValueError("unexpected Wikimedia domestic opponents")
        fid = football.fixture_id(season, teams[h], teams[a], league=league)
        if fid in scores:
            raise ValueError("duplicate Wikimedia fixture")
        cell = cell.strip()
        linked = re.fullmatch(r"\[\[[^\[\]|]+\|([^\[\]|]+)\]\]", cell)
        value = linked[1] if linked else cell
        pair = re.fullmatch(r"([0-9]{1,2})\s*[-–]\s*([0-9]{1,2})", value)
        goals = tuple(map(int, pair.groups())) if pair else None
        scores[fid] = goals if valid_score(goals) else None
    if len(scores) != registry.fixture_count(league, season):
        raise ValueError("incomplete Wikimedia regular-season results matrix")
    return rev["revid"], store.iso(rev["timestamp"]), scores


def _poll_wiki(db, league, season):
    if league not in _WIKI_PAGES:
        return
    started = store.now_utc()
    with db:
        claim = db.execute(
            "INSERT INTO wiki_checks VALUES(?,?,?,'polling','') ON CONFLICT(league,season) DO UPDATE "
            "SET attempted=excluded.attempted,status='polling' WHERE wiki_checks.attempted<=? "
            "AND (wiki_checks.status<>'retry_after' OR wiki_checks.detail<=excluded.attempted)",
            (league, season, started, store.iso(football.utc(started) - timedelta(hours=1))),
        ).rowcount
    if not claim:
        return
    title = _wiki_title(league, season)
    try:
        raw = wiki._get(wiki._url([title]))
        seen = store.now_utc()
        rev, published, sha, packed = league_wiki_cache.prepare_receipt(raw, league, season, _wiki_scores)
        latest = db.execute("SELECT max(revision) FROM wiki_receipts WHERE league=? AND season=? AND title=?",
                            (league, season, title)).fetchone()[0]
        if published > seen or latest is not None and rev < latest:
            raise ValueError("stale or future Wikimedia revision")
        with db:
            db.execute("INSERT OR IGNORE INTO wiki_receipts VALUES(?,?,?,?,?,?,?,?)",
                       (league, season, rev, published, seen, title, sha, packed))
        status, detail = "observed", str(rev)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        status, detail = "unavailable", str(exc)
        retry = (getattr(exc, "headers", None) or {}).get("Retry-After")
        if retry:
            try:
                until = (football.utc(started) + timedelta(seconds=int(retry))
                         if retry.isdecimal() else parsedate_to_datetime(retry))
                status, detail = "retry_after", store.iso(until)
            except (ValueError, TypeError, OverflowError):
                pass
    with db:
        db.execute("UPDATE wiki_checks SET status=?,detail=? WHERE league=? AND season=? AND attempted=?",
                   (status, detail, league, season, started))


def _observe_moves(db):
    present = {row[0] for row in db.execute(
        "SELECT revision FROM wiki_receipts WHERE league='lal' AND season='2026-27'"
    )}
    if _MOVED.keys() <= present:
        return
    started = store.now_utc()
    with db:
        acquired = db.execute(
            "INSERT INTO wiki_checks VALUES('lal-moves','2026-27',?,'polling','') "
            "ON CONFLICT(league,season) DO UPDATE SET attempted=excluded.attempted,status='polling' "
            "WHERE wiki_checks.attempted<=?",
            (started, store.iso(football.utc(started) - timedelta(hours=1))),
        ).rowcount
    if not acquired:
        return
    titles = [row[0] for row in _MOVED.values()]
    url = urlsplit(wiki._url(titles))
    params = parse_qs(url.query)
    del params["titles"]
    params["revids"] = ["|".join(map(str, _MOVED))]
    try:
        raw = wiki._get(url._replace(query=urlencode(params, doseq=True)).geturl())
        seen = store.now_utc()
        additions = []
        for page in wiki._query(raw, titles):
            revision, = page["revisions"]
            rev = revision["revid"]
            title, _, _, _, sha, _ = _MOVED[rev]
            content = revision["slots"]["main"]["content"].encode()
            published = store.iso(revision["timestamp"])
            if (page.get("ns") != 0 or page["title"] != title or published > seen
                    or hashlib.sha256(content).hexdigest() != sha):
                raise ValueError("changed licensed schedule reference")
            additions.append(("lal", "2026-27", rev, published, seen, title, sha, zlib.compress(content)))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        with db:
            db.execute("UPDATE wiki_checks SET status='unavailable',detail=? "
                       "WHERE league='lal-moves' AND season='2026-27' AND attempted=?", (str(exc), started))
        return
    with db:
        db.executemany("INSERT OR IGNORE INTO wiki_receipts VALUES(?,?,?,?,?,?,?,?)", additions)
        db.execute("UPDATE wiki_checks SET status='observed',detail='' "
                   "WHERE league='lal-moves' AND season='2026-27' AND attempted=?", (started,))


def _schedule_moves(db, league, season, at):
    if (league, season) != ("lal", "2026-27") or not _MOVED:
        return {}
    result = {}
    placeholders = ",".join("?" for _ in _MOVED)
    for row in db.execute("SELECT revision,title,seen,body,sha FROM wiki_receipts WHERE league=? AND season=? "
                          f"AND seen<=? AND revision IN ({placeholders})", (league, season, at, *_MOVED)):
        title, home, away, kick, sha, report = _MOVED[row["revision"]]
        if (row["title"] != title or row["sha"] != sha
                or hashlib.sha256(zlib.decompress(row["body"])).hexdigest() != sha):
            raise ValueError("changed archived schedule reference")
        fid = football.fixture_id(season, home, away, league=league)
        result[fid, kick] = {
            "observed_at": row["seen"], "url": f"https://en.wikipedia.org/w/index.php?oldid={row['revision']}",
            "title": title, "content_sha256": sha, "license": wiki.LICENSE,
            "attribution": "English Wikipedia contributors",
            "competition_report": "https://www.laliga.com/en-GB/match/temporada-2026-2027-laliga-ea-sports-" + report,
        }
    return result


@lru_cache(maxsize=96)
def _verified_file(path, league, blob, signature):
    fixtures = tuple(football.load_fixtures(path, league=league))
    if not fixtures or any(f.source_blob != blob for f in fixtures):
        raise ValueError(f"{path}: unverified domestic archive bytes")
    return fixtures


def _read_file(path, league, blob):
    path = Path(path)
    stat = path.stat()
    return _verified_file(path, league, blob, (stat.st_dev, stat.st_ino, stat.st_size, stat.st_ctime_ns))


def _pins(db, football_root, league, season, live):
    rows = {}
    year = registry.season_start_year(season)
    for path in sorted(Path(football_root).glob(f"????-??/{registry.profile(league).source_file}")):
        s = path.parent.name
        if not year - 3 <= registry.season_start_year(s) <= year:
            continue
        blob = registry.source_blob(league, s)
        fixtures = _read_file(path, league, blob)
        if live:
            with db:
                db.execute("INSERT OR IGNORE INTO pinned_receipts VALUES(?,?,?,?)",
                           (league, s, blob, store.now_utc()))
        receipt = db.execute("SELECT seen FROM pinned_receipts WHERE league=? AND season=? AND blob=?",
                             (league, s, blob)).fetchone()
        if receipt:
            rows[s] = (receipt[0], blob, fixtures)
    if live and season not in rows:
        raise ValueError(f"no verified {league}/{season} fixture archive in {football_root}")
    return rows


def _lookup_event(db, root, league, receipt, cache):
    slug, seen, sha = (receipt[k] for k in ("slug", "received", "sha"))
    request = (seen, sha)
    if request not in cache:
        reply, op, selector, _ = archive_proof.original_receipt(
            db, root, sha, seen, {"league_event", "league_catalog"}
        )
        if not market_provider.fresh_observation(reply):
            raise ValueError("stale Gamma HTTP receipt")
        payload = json.loads(reply.body)
        if op == "league_event" and isinstance(payload, dict) and selector == payload.get("slug"):
            events = [payload]
        elif op == "league_catalog" and selector == league and isinstance(payload, list):
            events = payload
        else:
            raise ValueError("wrong domestic Gamma response selector")
        # Catalogue pages can be large. Keep only the original fields used by
        # the existing identity and play-state parsers while processing this league.
        retained = {}
        for event in events:
            if isinstance(event, dict) and isinstance(event.get("slug"), str):
                key = event["slug"]
                retained[key] = None if key in retained else {k: v for k, v in event.items() if k != "markets"}
        cache[request] = retained
    event = cache[request].get(slug)
    if event is None:
        raise ValueError("Gamma response lacks a unique primary event")
    identity = big5_market._identity(event, league)
    if (identity[:3] != (slug, slug, league) or identity[3] != receipt["kickoff"]
            or big5_market_schedule.pregame_flags(event, seen) not in {"explicit_pregame", "schedule_only"}):
        raise ValueError("Gamma original does not confirm the candidate schedule")
    return event, identity


def _bind_event(db, root, league, season, receipt, fixtures, cache, moves):
    event, identity = _lookup_event(db, root, league, receipt, cache)
    kick = football.utc(identity[3])
    if _season(league, kick) != season or football.utc(receipt["received"]) >= kick:
        raise ValueError("Gamma event is outside this pre-match season")
    names = _gamma_clubs(event, identity, league, season)
    fid = football.fixture_id(season, *names, league=league)
    fixture = fixtures.get(fid)
    if (not fixture or (fixture.home, fixture.away) != names or fixture.quarantine_reason
            or fixture.status not in {"scheduled", "timed"}):
        raise ValueError("unregistered or excluded oriented regular-season fixture")
    # Date-only matchdays frequently use Sunday for every game from Friday
    # through Monday. A distant reverse fixture / postponed match needs new
    # football evidence before it can be joined to a Gamma event.
    zone = ZoneInfo(registry.profile(league).timezone)
    corroborated = fixture.round == 23 and (fid, identity[3]) in moves
    if abs((kick.astimezone(zone).date() - fixture.kickoff.astimezone(zone).date()).days) > 3 and not corroborated:
        raise ValueError("domestic archive and Gamma dates disagree")
    return (league, receipt["slug"], receipt["received"], receipt["sha"], fid, identity[3], *identity[4:7])


def _gamma_clubs(event, identity, league, season):
    participants = market_provider.teams(event)
    names = []
    for side, club_id in zip(("home", "away"), identity[4:6]):
        name = participants[side].get("name")
        name = _GAMMA_NAMES.get((league, season, club_id, name), name)
        if name not in registry.members(league, season):
            raise ValueError("Gamma participant does not match a registered club")
        names.append(name)
    return tuple(names)


def _binding(row):
    return {"slug": row["slug"], "receipt": row["seen"], "sha256": row["sha"], "kickoff": row["kickoff"],
            "home_id": row["home_id"], "away_id": row["away_id"], "event_id": row["event_id"]}


def _recent_rolling_catalog(root, league, at):
    """A complete rolling catalog can legitimately contain no upcoming events for a league."""
    path = root / "watch-health.sqlite3"
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='watch_catalog_success'").fetchone():
            return False
        since = store.iso(football.utc(at) - timedelta(minutes=10))
        for (raw,) in db.execute("SELECT scope FROM watch_catalog_success WHERE league=? "
                                 "AND received_at BETWEEN ? AND ?", (league, since, at)):
            scope = json.loads(raw)
            if (scope.get("start") == scope.get("end") == "rolling" and scope.get("discover") is True
                    and league in scope.get("leagues", ())):
                return True
    return False


def _market_schedules(local, market_state_dir, league, season, by_id, at, pin_seen, *, replay):
    market_root = _market_root(market_state_dir)
    schedules, active, gaps = {}, {}, Counter()
    cache = {}
    source = {"state": "available"}
    moves = _schedule_moves(local, league, season, at)
    if (market_root / "runtime.sqlite3").is_file():
        with closing(_market_db(market_root)) as db:
            db.execute("BEGIN")
            if replay:
                events = db.execute(
                    "SELECT e.* FROM big5_events e JOIN (SELECT DISTINCT fixture FROM big5_schedule_receipts "
                    "WHERE sport=? AND slug=fixture AND received<=? AND kickoff>?) c ON c.fixture=e.slug",
                    (league, at, at),
                ).fetchall()
            else:
                events = db.execute(
                    "SELECT * FROM big5_events WHERE slug=fixture AND sport=? AND kickoff>?", (league, at)
                ).fetchall()
            source["candidate_events"] = len(events)
            now = football.utc(at)
            end = now + timedelta(days=7)
            end_stamp = store.iso(end)
            if (not replay and not any(at < event["kickoff"] <= end_stamp for event in events)
                    and any(now < f.kickoff <= end and f.status in {"scheduled", "timed"}
                            and not f.quarantine_reason for f in by_id.values())
                    and not _recent_rolling_catalog(market_root, league, at)):
                source.update(state="unobserved", error="no upcoming market events or recent complete league catalog")
            for head in events:
                rows = db.execute(
                    "SELECT * FROM big5_schedule_receipts WHERE fixture=? AND slug=fixture AND received<=? "
                    "AND origin='validated_receipt' ORDER BY received,sha", (head["slug"], at)
                )
                current = None
                latest_row = None
                latest_error = None
                for row in rows:
                    latest_row = row
                    latest_error = None
                    if row["stopped"]:
                        current = None
                        continue
                    if current == row["kickoff"]:
                        continue
                    known = local.execute(
                        "SELECT * FROM gamma_bindings WHERE league=? AND slug=? AND seen=? AND sha=?",
                        (league, row["slug"], row["received"], row["sha"]),
                    ).fetchone()
                    if known is None:
                        try:
                            value = _bind_event(db, market_root, league, season, row, by_id, cache, moves)
                            if tuple(value[6:]) != tuple(head[k] for k in ("home_id", "away_id", "event_id")):
                                raise ValueError("changed Gamma event or participant IDs")
                        except (KeyError, TypeError, ValueError, OSError) as exc:
                            latest_error = str(exc)
                            gaps[latest_error] += 1
                            continue
                        with local:
                            local.execute("INSERT OR IGNORE INTO gamma_bindings VALUES(?,?,?,?,?,?,?,?,?)", value)
                    current = row["kickoff"]
                known = local.execute(
                    "SELECT * FROM gamma_bindings WHERE league=? AND slug=? AND seen<=? ORDER BY seen DESC LIMIT 1",
                    (league, head["slug"], at),
                ).fetchone()
                if not known or not latest_row or latest_row["stopped"]:
                    continue
                ids = ("home_id", "away_id", "event_id")
                if _identity_conflicted(db, head["slug"], at):
                    latest_error = "registered Gamma event or participant IDs changed"
                    gaps[latest_error] += 1
                elif head["receipt"] <= at:
                    first = local.execute("SELECT home_id,away_id,event_id FROM gamma_bindings "
                                          "WHERE league=? AND slug=? AND seen<=? ORDER BY seen,sha LIMIT 1",
                                          (league, head["slug"], at)).fetchone()
                    if tuple(first) != tuple(head[k] for k in ids):
                        latest_error = "registered Gamma event or participant IDs changed"
                        gaps[latest_error] += 1
                if current and not latest_error and _eligible_market(db, _binding(known), at):
                    try:
                        verified = _bind_event(db, market_root, league, season, latest_row, by_id, cache, moves)
                        if verified[4:] != tuple(known[k] for k in ("fixture_id", "kickoff", *ids)):
                            raise ValueError("latest Gamma original disagrees with registered fixture identity")
                    except (KeyError, TypeError, ValueError, OSError) as exc:
                        latest_error = str(exc)
                        gaps[latest_error] += 1
                    else:
                        binding = _binding(known)
                        fixture = by_id[known["fixture_id"]]
                        binding.update(last_verified=latest_row["received"], last_sha256=latest_row["sha"],
                                       league=league, season=season, home=fixture.home, away=fixture.away)
                        move = moves.get((fixture.fixture_id, known["kickoff"]))
                        if move:
                            binding["reschedule_source"] = move
                        binding["known_at"] = max(pin_seen, known["seen"], (move or {}).get("observed_at", ""))
                        active.setdefault(known["fixture_id"], []).append(binding)
                fixture = by_id.get(known["fixture_id"])
                if (latest_error and fixture and not fixture.quarantine_reason and fixture.status in {"scheduled", "timed"}
                        and at < head["kickoff"] <= end_stamp and at < known["kickoff"] <= end_stamp
                        and big5_market_schedule.play_history(db, head["slug"], known["seen"], at) != "not_pregame"):
                    source.setdefault("fixture_errors", []).append(
                        {"fixture": known["fixture_id"], "stage": "market_schedule", "error": latest_error})
    else:
        gaps["market database unavailable"] += 1
        source = {"state": "unavailable", "error": "market database unavailable"}
    for r in local.execute("SELECT * FROM gamma_bindings WHERE league=? AND seen<=? ORDER BY seen,sha", (league, at)):
        if r["fixture_id"] not in by_id:
            continue
        timeline = schedules.setdefault(r["fixture_id"], [])
        move = moves.get((r["fixture_id"], r["kickoff"])) or {}
        stamp = football.utc(max(pin_seen, r["seen"], move.get("observed_at", "")))
        kick = football.utc(r["kickoff"])
        if timeline and timeline[-1][0] == stamp:
            timeline[-1] = (stamp, kick)
        elif not timeline or timeline[-1][1] != kick:
            timeline.append((stamp, kick))
    active = {fid: rows[0] for fid, rows in active.items() if len(rows) == 1}
    return schedules, active, dict(gaps), source


def _wiki_history(db, league, season, at):
    revisions = []
    metadata = []
    title = _wiki_title(league, season) if league in _WIKI_PAGES else ""
    verified = league_wiki_cache.verified_rows(db, league, season, title, at, _wiki_scores)
    for rev, published, seen, sha, scores in verified:
        link = f"https://en.wikipedia.org/w/index.php?oldid={rev}"
        revisions.append((seen, "wikipedia", f"{link}#{sha}", published, scores))
        metadata.append({"revision": rev, "title": title, "url": link, "sha256": sha,
                         "observed_at": seen, "published_at": published})
    return revisions, metadata


def _score_versions(fixture, observations, schedule):
    state, versions, published_scores = {}, [], set()
    ever_confirmed = set()
    active_kickoff = None
    # These are already-observed schedule receipts, not a later kickoff read back
    # into history. Invalidate the old sporting label as soon as the accepted
    # replacement is known, even if neither score publisher polls again.
    moves = ((store.iso(stamp), "schedule", f"schedule changed to {store.iso(kick)}", None, kick)
             for stamp, kick in schedule)
    for seen, source, locator, published, value in merge(moves, observations, key=lambda row: row[0]):
        if source == "schedule":
            if active_kickoff != value:
                if versions and versions[-1].score is not None:
                    versions.append(football.ResultVersion(football.utc(seen), None, "archived_upper_bound",
                                                           locator, actual_kickoff=versions[-1].actual_kickoff))
                state.clear()
                ever_confirmed.clear()
                published_scores.clear()
                active_kickoff = value
            continue
        result = None
        forbidden = False
        if source == "openfootball":
            # A removed fixture, administrative decision, unknown status or damaged
            # score explicitly withdraws any prior sporting label for this match.
            forbidden = value is None or value.quarantine_reason is not None
            if value and value.status == "completed" and not forbidden:
                result = (value.home_goals, value.away_goals)
        else:
            result = value
        if active_kickoff is None:
            active_kickoff = fixture.kickoff
        kick = active_kickoff
        earliest = kick + league_wiki_cache.MATCH_DURATION
        if result is not None and (football.utc(published) < earliest or football.utc(seen) < earliest):
            result = None
        if result is not None:
            ever_confirmed.add(source)
        state[source] = (result, forbidden or result is None and source in ever_confirmed)
        published_scores = {score for score, _ in state.values() if score is not None}
        held = any(blocked for _, blocked in state.values()) or len(published_scores) > 1
        score = None if held or not published_scores else next(iter(published_scores))
        previous = versions[-1] if versions else None
        # A withdrawal invalidates the sporting result for the kickoff it was
        # attached to, even if a new schedule already advertises a future date.
        event_kickoff = previous.actual_kickoff if score is None and previous else kick
        previous_score = previous.score if previous else None
        moved_score = previous is not None and score is not None and event_kickoff != previous.actual_kickoff
        if score != previous_score or moved_score:
            if held:
                locator = f"withheld on {source} receipt {locator}"
            versions.append(football.ResultVersion(football.utc(seen), score, "archived_upper_bound", locator,
                                                   actual_kickoff=event_kickoff))
    return tuple(versions), len(published_scores) > 1


def load_inputs(state_root, *, league, football_root, market_state_dir, as_of=None):
    """Observe live sources, or reconstruct their locally received state at an explicit UTC cutoff."""
    live = as_of is None
    at = store.iso(as_of or store.now_utc())
    season = _season(league, at)
    root = Path(state_root)
    folder = root / "sources" / "domestic-live-inputs"
    folder.mkdir(parents=True, exist_ok=True)
    with closing(league_wiki_cache.connect(folder / "receipts.sqlite3")) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        pins = _pins(db, football_root, league, season, live)
        refresh = {"state": "local_replay"}
        if live:
            refresh = league_openfootball_refresh.refresh(root, league, season)
            _poll_wiki(db, league, season)
            if (league, season) == ("lal", "2026-27"):
                _observe_moves(db)
            at = store.now_utc()
        provenance = {"league": league, "season": season, "as_of": at, "mode": "technical",
                      "openfootball_refresh": refresh, "pinned_seasons": {}, "openfootball_revisions": [],
                      "wikimedia_revisions": [], "market_fixtures": {}, "market_gaps": {}}
        rows, observations = {}, defaultdict(list)
        for s, (seen, blob, fixtures) in pins.items():
            if seen > at:
                continue
            url = f"{registry.SOURCE_ROOT}/{s}/{registry.profile(league).source_file}"
            provenance["pinned_seasons"][s] = {"git_blob": blob, "observed_at": seen, "url": url}
            for f in fixtures:
                rows[f.fixture_id] = f
                observations[f.fixture_id].append((seen, "openfootball", f"{url}#{blob}", seen, f))
        if season not in provenance["pinned_seasons"]:
            provenance["status"] = "current_season_identity_not_yet_observed"
            return LeagueInputs((), (), provenance)
        current = {key: f for key, f in rows.items() if f.season == season}
        latest = dict(current)
        last_blob = pins[season][1]
        for seen, revision, blob, path in league_openfootball_refresh.snapshots(root, league, season, at):
            url = f"https://github.com/openfootball/football.json/blob/{revision}/{season}/{path.name}"
            provenance["openfootball_revisions"].append({"git_blob": blob, "commit": revision,
                                                         "observed_at": seen, "url": url})
            if blob == last_blob:
                continue
            last_blob = blob
            fixtures = _read_file(path, league, blob)
            latest = {f.fixture_id: f for f in fixtures}
            # Keep independently registered games usable. A new oriented fixture cannot
            # join market evidence, training or scoring until a reviewed schedule admits it.
            provenance["unregistered_fixtures"] = sorted(latest.keys() - current.keys())
            for fid in provenance["unregistered_fixtures"]:
                del latest[fid]
            for fid, f in current.items():
                revised = latest.get(fid)
                if revised is not None and revised.round != f.round:
                    revised = replace(revised, status="quarantined", home_goals=None, away_goals=None,
                                      quarantine_reason="source_matchday_changed")
                    latest[fid] = revised
                observations[fid].append((max(pins[season][0], seen), "openfootball", f"{url}#{blob}",
                                          seen, revised))
        wiki_history, provenance["wikimedia_revisions"] = _wiki_history(db, league, season, at)
        check = db.execute("SELECT attempted,status,detail FROM wiki_checks WHERE league=? AND season=? "
                           "AND attempted<=?", (league, season, at)).fetchone()
        provenance["wikimedia"] = {"poll": dict(check) if check else None,
                                    "attribution": "English Wikipedia contributors", "license": wiki.LICENSE}
        schedules, active, gaps, market_source = _market_schedules(db, market_state_dir, league, season, latest,
                                                                   at, pins[season][0], replay=not live)
    provenance["market_gaps"] = gaps
    provenance["market_source"] = market_source
    wiki_changes = league_wiki_cache.score_observations(
        league, season, _wiki_scores, wiki_history,
        {fid: latest.get(fid, f) for fid, f in current.items()}, schedules, pins[season][0])
    for fid, changes in wiki_changes.items():
        observations[fid].extend(changes)
    fixtures, upcoming, conflicts, withheld, current_scores = [], [], [], [], 0
    for fid, f in rows.items():
        if f.season == season:
            f = latest.get(fid) or replace(f, status="quarantined", quarantine_reason="removed_from_source",
                                            home_goals=None, away_goals=None)
        timeline = tuple(schedules.get(fid, ()))
        versions, conflict = _score_versions(f, sorted(observations[fid], key=lambda v: v[0]), timeline)
        if conflict:
            conflicts.append(fid)
        score = versions[-1].score if versions else None
        if f.quarantine_reason and score is not None:
            raise ValueError(f"{fid}: quarantined fixture still has an active score")
        if f.quarantine_reason and versions:
            # Its current state is a withdrawal. Earlier as-of loads still
            # expose the previous label and its actual first receipt. Keep the
            # source disposition on the fixture for score consumers.
            disposition = f.status
            f = replace(f, status="withdrawn", quarantine_reason=f"{disposition}: {f.quarantine_reason}")
        f = replace(f, home_goals=score[0] if score else None, away_goals=score[1] if score else None,
                    result_versions=versions, schedule_versions=timeline,
                    schedule_known_at=timeline[-1][0] if timeline else None,
                    schedule_grades=("archived_schedule_upper_bound",) * len(timeline),
                    kickoff=timeline[-1][1] if timeline else f.kickoff,
                    kickoff_precision="minute" if timeline else f.kickoff_precision,
                    status=f.status if f.quarantine_reason else "completed" if score else "scheduled")
        fixtures.append(f)
        if f.season == season and score is not None:
            current_scores += 1
        if fid in active and timeline and not f.quarantine_reason and score is None and f.kickoff > football.utc(at):
            if football._late_replacement(timeline):
                changed, old, new = next((stamp, prev, current) for (_, prev), (stamp, current)
                                          in zip(timeline, timeline[1:]) if current != prev and stamp >= prev)
                withheld.append({"fixture": fid, "stage": "schedule",
                                 "error": "replacement received after original kickoff; no pre-kickoff cancellation",
                                 "previous_kickoff": store.iso(old), "replacement_kickoff": store.iso(new),
                                 "observed_at": store.iso(changed)})
            elif all(v.actual_kickoff != f.kickoff for v in versions):
                upcoming.append(f)
                provenance["market_fixtures"][fid] = active[fid]
    provenance.update(status="observed", current_season_scores=current_scores, conflicting_scores=conflicts,
                      withheld_fixtures=withheld)
    return LeagueInputs(tuple(fixtures), tuple(sorted(upcoming, key=lambda f: (f.kickoff, f.fixture_id))),
                        provenance)
