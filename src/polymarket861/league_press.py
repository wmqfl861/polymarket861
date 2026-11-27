"""Match GDELT's public news index to registered clubs using a shared release download."""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import unicodedata
import zipfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import context_feeds, league_registry, market_provider, store

CREDIT = "The GDELT Project, https://www.gdeltproject.org/"
BASE = context_feeds.GKG_BASE
SCHEMA = """
CREATE TABLE IF NOT EXISTS press_releases (
 edition TEXT PRIMARY KEY, checksum TEXT NOT NULL, received TEXT NOT NULL, season TEXT NOT NULL,
 leagues TEXT NOT NULL, scanned INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS press_clubs (
 edition TEXT NOT NULL, league TEXT NOT NULL, team TEXT NOT NULL, url TEXT NOT NULL,
 document TEXT NOT NULL, PRIMARY KEY(edition,league,team,url));
CREATE INDEX IF NOT EXISTS press_team ON press_clubs(league,team,edition);
"""
# Common multi-word names used in English-language reports. Single-word city names
# can match non-sports news and are not used as unqualified search terms.
ALIASES = {
    "FC Bayern München": ("Bayern Munich", "Bayern Munchen"),
    "Bayer 04 Leverkusen": ("Bayer Leverkusen",),
    "Borussia Mönchengladbach": ("Borussia Moenchengladbach",),
    "TSG 1899 Hoffenheim": ("TSG Hoffenheim",),
    "1. FC Union Berlin": ("Union Berlin",),
    "FC Internazionale Milano": ("Inter Milan", "Internazionale Milano"),
    "Real Madrid CF": ("Real Madrid",),
    "Athletic Club": ("Athletic Bilbao",), "Club Atlético de Madrid": ("Atletico Madrid",),
    "Real Sociedad de Fútbol": ("Real Sociedad",), "Real Betis Balompié": ("Real Betis",),
    "RC Celta de Vigo": ("Celta Vigo",), "Rayo Vallecano de Madrid": ("Rayo Vallecano",),
    "RCD Espanyol de Barcelona": ("RCD Espanyol",),
    "Real Racing Club de Santander": ("Racing Santander", "Racing de Santander"),
    "RC Deportivo La Coruña": ("Deportivo La Coruna", "Deportivo de La Coruna"),
    "Paris Saint-Germain FC": ("Paris Saint Germain",), "Olympique de Marseille": ("Olympique Marseille",),
    "Racing Club de Lens": ("RC Lens",), "Stade Rennais FC 1901": ("Stade Rennais",),
    "RC Strasbourg Alsace": ("RC Strasbourg",), "ES Troyes AC": ("ES Troyes",),
    "Brighton & Hove Albion FC": ("Brighton and Hove Albion",),
}


def _words(value):
    folded = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(re.findall(r"[^\W_]+", "".join(v for v in folded if not unicodedata.combining(v))))


def _clubs(season):
    names = {}
    for league in league_registry.PROFILES:
        for team in league_registry.members(league, season):
            labels = {team, *ALIASES.get(team, ())}
            # Some clubs appear in the index without their corporate suffix.
            short = re.sub(r" (?:FC|AFC)$", "", team)
            if " " in short and short != "Athletic Club":
                labels.add(short)
            for label in labels:
                normal = _words(label)
                if " " in normal and normal != "athletic club":
                    names.setdefault(normal, set()).add((league, team))
    return {name: next(iter(clubs)) for name, clubs in names.items() if len(clubs) == 1}


def _directory(root):
    return Path(root) / "team-press"


def _manifest(get):
    response = get(BASE + "lastupdate.txt", timeout=15)
    releases = []
    for line in response.body.decode("ascii").splitlines():
        m = re.fullmatch(r"(\d+) ([a-fA-F0-9]{32}) https?://data\.gdeltproject\.org/gdeltv2/(\d{14})\.gkg\.csv\.zip", line.strip())
        if m:
            releases.append((int(m[1]), m[2].lower(), m[3]))
    if len(releases) != 1 or not 0 < releases[0][0] <= 20 * 1024 * 1024:
        raise ValueError("no unique, bounded GDELT GKG edition in the manifest")
    size, checksum, edition = releases[0]
    stamp = datetime.strptime(edition, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    if stamp > store.instant(response.received_at) + timedelta(minutes=15) or stamp.minute % 15 or stamp.second:
        raise ValueError("invalid GDELT edition time")
    return stamp, size, checksum


def _parse(raw, edition, received, names):
    # A release is scanned once for all 96 clubs. No article pages are requested.
    matcher = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?!\w)")
    rows = {}
    scanned = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if len(members) != 1 or members[0].filename != edition + ".gkg.csv" or members[0].file_size > 128 * 1024 * 1024:
            raise ValueError("unexpected GDELT archive member")
        with archive.open(members[0]) as lines:
            for line in lines:
                scanned += 1
                if len(line) > 1_048_576:
                    continue
                fields = line.decode("utf-8", errors="replace").rstrip("\r\n").split("\t")
                if len(fields) < 15 or fields[2] != "1" or fields[1] != edition:
                    continue
                try:
                    url = context_feeds._article_url(fields[4])
                    parts = urlsplit(url)
                except ValueError:
                    continue
                if len(url) > 2048:
                    continue
                paths = _words(unquote(parts.path))
                orgs = " ; ".join(_words(v) for v in fields[13].split(";"))
                matches = {m.group(): "article_url" for m in matcher.finditer(paths)}
                matches.update({m.group(): "indexed_organization" for m in matcher.finditer(orgs)})
                for label, via in matches.items():
                    league, team = names[label]
                    key = (league, team, url)
                    rows[key] = {
                        "url": url, "domain": parts.hostname, "source": context_feeds.GKG,
                        "index_seen_at": context_feeds._news_time(edition[:8] + "T" + edition[8:] + "Z"),
                        "received_at": received, "record_id": fields[0], "matched_via": via,
                        "club_label": label, "publisher_published_at": None,
                    }
    return scanned, rows


def collect(root, *, quarters=12, get=market_provider.get):
    if not 1 <= quarters <= 96:
        raise ValueError("quarters must be in [1,96]")
    last, size, checksum = _manifest(get)
    folder = _directory(root)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = {"latest_index_utc": store.iso(last), "received_editions": 0, "cached_editions": 0,
              "errors": {}, "team_links": dict.fromkeys(league_registry.PROFILES, 0)}
    # The manifest sometimes advertises the next quarter before the local clock reaches it.
    lead = int(last > store.instant(store.now_utc()))
    report["deferred_future_edition"] = store.iso(last) if lead else None
    club_maps = {}
    with closing(sqlite3.connect(folder / "index.sqlite3", timeout=15)) as db:
        db.executescript(SCHEMA)
        for step in range(lead, lead + quarters):
            when = last - timedelta(minutes=15 * step)
            year = when.year - (when.month < 8)
            season = f"{year}-{(year + 1) % 100:02d}"
            edition = when.strftime("%Y%m%d%H%M%S")
            previous = db.execute("SELECT checksum FROM press_releases WHERE edition=?", (edition,)).fetchone()
            if previous:
                if step == 0 and previous[0] != checksum:
                    report["errors"][edition] = "GDELT advertised a different checksum for an archived edition"
                else:
                    report["cached_editions"] += 1
                continue
            try:
                if season not in club_maps:
                    club_maps[season] = _clubs(season)
                reply = get(BASE + edition + ".gkg.csv.zip", timeout=18)
                digest = hashlib.md5(reply.body, usedforsecurity=False).hexdigest()
                received = store.iso(reply.received_at)
                if (not 0 < len(reply.body) <= 20 * 1024 * 1024 or when > store.instant(received)
                        or step == 0 and (digest != checksum or size != len(reply.body))):
                    raise ValueError("GDELT release size, checksum or receipt clock does not match")
                scanned, records = _parse(reply.body, edition, received, club_maps[season])
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                report["errors"][edition] = str(exc)[:240]
                continue
            with db:
                ins = db.execute("INSERT OR IGNORE INTO press_releases VALUES(?,?,?,?,?,?)",
                                 (edition, digest, received, season, store.canonical(sorted(league_registry.PROFILES)), scanned))
                if not ins.rowcount:
                    report["cached_editions"] += 1
                    continue
                db.executemany("INSERT INTO press_clubs VALUES(?,?,?,?,?)",
                               ((edition, *key, store.canonical(doc)) for key, doc in records.items()))
            report["received_editions"] += 1
            for league, _, _ in records:
                report["team_links"][league] += 1
    report["state"] = ("unavailable" if not (report["received_editions"] + report["cached_editions"])
                       else "partial" if report["errors"] else "ok")
    return report


def fixture_news_as_of(root, fixture, cutoff):
    league, season = fixture["league"], fixture["season"]
    at = min(store.iso(cutoff), store.iso(fixture["kickoff"]))
    start = store.iso(store.instant(at) - timedelta(days=3))
    members = set(league_registry.members(league, season))
    clubs = [fixture["home"], fixture["away"]]
    if len(set(clubs)) != 2 or not set(clubs) <= members:
        raise ValueError("news lookup requires two registered clubs in this competition and season")
    teams = {side: {"team": team, "items": [], "state": "unobserved"} for side, team in zip(("home", "away"), clubs)}
    dbpath = _directory(root) / "index.sqlite3"
    latest = None
    if dbpath.is_file():
        with closing(sqlite3.connect(dbpath.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            db.execute("BEGIN")
            table = db.execute("SELECT 1 FROM sqlite_master WHERE name='press_releases'").fetchone()
            if table:
                latest = db.execute("SELECT max(edition) FROM press_releases WHERE season=? AND received<=?",
                                    (season, at)).fetchone()[0]
            for side in teams.values():
                if not latest:
                    continue
                rows = db.execute("SELECT c.document FROM press_clubs c JOIN press_releases r USING(edition) "
                                  "WHERE c.league=? AND c.team=? AND r.season=? AND r.received<=? AND "
                                  "c.edition>=? ORDER BY c.edition DESC,c.url",
                                  (league, side["team"], season, at, store.instant(start).strftime("%Y%m%d%H%M%S")))
                seen = set()
                for row in rows:
                    article = json.loads(row[0])
                    if article["index_seen_at"] >= start and article["url"] not in seen:
                        side["items"].append(article)
                        seen.add(article["url"])
                    if len(seen) == 15:
                        break
    fresh = bool(latest and store.instant(at) - datetime.strptime(latest, "%Y%m%d%H%M%S").replace(tzinfo=UTC) <= timedelta(hours=2))
    for side in teams.values():
        if latest:
            side["state"] = "links_observed" if side["items"] else ("no_links_in_index_window" if fresh else "stale_index")
    links = [[i["url"] for i in side["items"]] for side in teams.values()]
    return {"state": ("current_index" if fresh else "stale_index") if latest else "unobserved",
            "attribution": CREDIT, "scope": "club news discovery; club mention, match relevance unverified",
            "teams": teams, "latest_index_edition": latest,
            "evidence_sha256": store.digest(links) if any(links) else None}


def register_feeds(commands):
    parser = commands.add_parser("league-press", help="轮询五大联赛球队的 GDELT 新闻链接")
    parser.add_argument("--quarters", type=int, default=12, help="回看多少个 15 分钟索引批次")
    parser.set_defaults(func=lambda a: collect(a.data_dir, quarters=a.quarters),
                        result_exit_code=lambda result: int(result["state"] == "unavailable"))
