"""Match GDELT's public news index to registered clubs using a shared release download."""

from __future__ import annotations

import hashlib
import html
import io
import json
import re
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import context_feeds, league_press_labels, league_registry, market_provider, store

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
_words = league_press_labels.words
_clubs = league_press_labels.club_labels
MATCHER_VERSION = "gdelt-club-links-v2"
# Plain city/club names and these multiword labels also describe other subjects.
_NEEDS_CONTEXT = {"athletic club", "as monaco", "as roma", "le mans", "le havre", "crystal palace", "real madrid", "paris fc", "la real"}
# Common words, locations and nicknames easily mentioned in someone else's football story.
# For these, an indexed organization or a fixture pairing anchors the reference.
_WEAK_WORDS = {"nice", "lens", "angers", "como", "inter", "milan", "roma", "spurs", "la real"}
_COMPETITION_HOSTS = {"bundesliga.com", "laliga.com", "legaseriea.it", "ligue1.com", "premierleague.com"}
_FOOTBALL = re.compile(
    r"\b(?:football|soccer|futbol|futebol|fussball|calcio|calciomercato|"
    r"premier league|epl|bundesliga|laliga|la liga|serie a|ligue 1|uefa|"
    r"champions league|europa league|copa del rey|dfb pokal|coppa italia|"
    r"goalkeeper|midfielder|goalkeeping|starting xi|starting eleven|portiere|fussballer|futbolista)\b"
)
_OTHER_SCOPE = re.compile(
    r"\b(?:women(?:s)?|ladies|femenin[oa]s?|femenil|femeni|lyonnes|feminin(?:e|es|o)?|femminile|frauen(?:fussball)?|"
    r"wsl|uwcl|nwsl|liga f|youth|academy|academie|primavera|juvenil|reserves?|"
    r"(?:u|under|sub|moins de) ?(?:1[3-9]|2[0-3])s?|junioren|espoirs|"
    r"basket(?:ball)?|baloncesto|pallacanestro|euroleague|euroliga|nba|wnba|"
    r"rugby|handball|hockey|cricket|tennis|baseball|futsal|esports|yachting|sailing|hypercar|"
    r"motorsports?|motorracing|motor racing|endurance racing|formul[ae] (?:one|1)|f1|grand prix|"
    r"(?:24|vingt quatre) (?:hours|heures)|nfl|ncaa|american football)\b"
)
_OFF_TOPIC = re.compile(
    r"\b(?:tax haven|tax havens|minority|ethnic minorities|roma people|roma community|"
    r"travel guide|tourism|tourisme|city council|royal family|princely family|prince albert|old town)\b"
)
_FOOTBALL_THEME = re.compile(r"(?:^|;)TAX_FNCACT_FOOTBALLER(?:,\d+)?(?:;|$)")
_OPPONENT = re.compile(r"\b(?:v|vs|versus|contra|contre|gegen|ahead of|face[sd]?|take[s]? on)\b")
_OTHER_SPORT_THEME = re.compile(r"(?:^|;)TAX_FNCACT_BASKETBALL_COACH(?:,\d+)?(?:;|$)")
_FIXTURE_LANGUAGE = re.compile(r"\b(?:sports?|team news|line ?ups?|matchday|predictions?|squad|kick ?off|"
                              r"partido|jornada|giornata|spieltag|journee)\b")
_CLUB_ABBREVIATIONS = {"psg", "losc", "bvb"}
_RESERVE_SUFFIX = re.compile(r" (?:ii|b|atletic)\b")
_PAGE_TITLE = re.compile(r"<PAGE_TITLE>([^<]{1,4096})</PAGE_TITLE>")
# Clubs from other competitions sharing a short name, sometimes in football articles.
_OTHER_CLUB = {
    "inter": re.compile(r"\binter (?:de )?(?:miami|turku|baku|escaldes)\b"),
    "barcelona": re.compile(r"\b(?:barcelona (?:sc|sporting club)|barcelona (?:b|atletic))\b"),
    "newcastle": re.compile(r"\bnewcastle (?:united )?jets\b"),
    "spurs": re.compile(r"\bsan antonio(?: spurs)?\b"),
}
_CLUB_SOURCE_PRIORITY = {"indexed_title": 0, "indexed_organization": 1, "article_url": 2}


@lru_cache(maxsize=8)
def _label_pattern(labels):
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in sorted(labels, key=lambda n: (-len(n), n)))
                      + r")(?!\w)")


def _title(fields):
    if len(fields) > 26 and (m := _PAGE_TITLE.search(fields[26])):
        title = " ".join(html.unescape(m[1]).split())
        return title[:512] or None
    return None


def _club_matches(names, matcher, path, title, orgs, themes, host):
    # Keep fields and organization entities separate: an adjacent pair of names
    # in unrelated fields cannot manufacture a club or a fixture.
    story_fields = {"article_url": _words(path), "indexed_title": _words(title or "")}
    if (any(_OTHER_SCOPE.search(value) for value in story_fields.values())
            or any(_OTHER_SPORT_THEME.search(value) for value in themes)):
        return {}
    off_topic = any(_OFF_TOPIC.search(value) for value in story_fields.values())
    fields = [(source, value) for source, value in story_fields.items() if value]
    blocked = set()
    for organization in set(map(_words, orgs)):
        if not organization:
            continue
        if _OTHER_SCOPE.search(organization):
            blocked.update(names[m[0]] for m in matcher.finditer(organization))
        else:
            fields.append(("indexed_organization", organization))
    candidates = [(m.group(), source, value, m) for source, value in fields for m in matcher.finditer(value)]
    if not candidates:
        return {}
    context = None
    for source, value in story_fields.items():
        if found := _FOOTBALL.search(value):
            context = f"{source}:{found[0]}"
            break
    if not context and any(host == domain or host.endswith("." + domain) for domain in _COMPETITION_HOSTS):
        context = "competition_domain:" + host
    if not context and any(_FOOTBALL_THEME.search(value) for value in themes):
        context = "indexed_theme:TAX_FNCACT_FOOTBALLER"
    paired = set()
    # "Barcelona vs Sevilla" supplies football context for these two registered clubs.
    # Require a nearby opponent marker and a sports or distinctive club cue.
    for source, value in story_fields.items():
        tokens = [(label, m) for label, via, _, m in candidates if source == via]
        for (a, ma), (b, mb) in zip(tokens, tokens[1:]):
            separator = value[ma.end():mb.start()].strip()
            if names[a] == names[b] or len(separator.split()) > 8 or not _OPPONENT.search(separator):
                continue
            if (context or _FIXTURE_LANGUAGE.search(value) or a in _CLUB_ABBREVIATIONS or b in _CLUB_ABBREVIATIONS
                    or any(" " in label and label not in _NEEDS_CONTEXT for label in (a, b))):
                paired.update(((source, a), (source, b)))
    selected = {}
    for label, _, value, match in candidates:
        if _RESERVE_SUFFIX.match(value[match.end():]):
            blocked.add(names[label])
    for label, source, value, _ in candidates:
        team = names[label]
        if team in blocked:
            continue
        # An organization entity such as "Real Madrid Basketball" must not be
        # credited on an otherwise neutral URL, nor can Inter Miami become Inter.
        other = _OTHER_CLUB.get(label)
        if other and any(other.search(text) for _, text in fields):
            continue
        anchor = context or ("registered_opponent" if (source, label) in paired else None)
        if (label == "athletic club" and not (host == "laliga.com" or host.endswith(".laliga.com"))
                and not any(re.search(r"\b(?:bilbao|san mames|laliga|la liga)\b", text) for _, text in fields)):
            continue
        if (" " not in label or label in _NEEDS_CONTEXT) and (not anchor or off_topic):
            continue
        if (label in _WEAK_WORDS and (source, label) not in paired
                and not (source == "indexed_organization" and value == label)):
            continue
        rank = (-len(label.split()), _CLUB_SOURCE_PRIORITY[source], -len(label), label)
        if team not in selected or rank < selected[team][0]:
            selected[team] = (rank, label, source, anchor or "distinctive_club_name")
    return {team: (label, via, football) for team, (_, label, via, football) in selected.items()}


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
    matcher = _label_pattern(tuple(names))
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
                title = _title(fields)
                # GKG Organizations (14) and V2Organizations (15, with offsets).
                orgs = fields[13].split(";")
                for tagged in fields[14].split(";"):
                    name, separator, offset = tagged.rpartition(",")
                    if separator and offset.isdigit():
                        orgs.append(name)
                matches = _club_matches(names, matcher, unquote(parts.path), title, orgs,
                                        (fields[7], fields[8]), parts.hostname)
                for (league, team), (label, via, context) in matches.items():
                    key = (league, team, url)
                    # Duplicate index rows can omit a title that another row
                    # in this same immutable release actually supplied.
                    if key in rows and (rows[key]["title"] or not title):
                        continue
                    rows[key] = {
                        "url": url, "domain": parts.hostname, "source": context_feeds.GKG,
                        "index_seen_at": context_feeds._news_time(edition[:8] + "T" + edition[8:] + "Z"),
                        "received_at": received, "record_id": fields[0], "matched_via": via,
                        "club_label": label, "publisher_published_at": None, "title": title,
                        "title_source": "gdelt_page_title" if title else None,
                        "matcher_version": MATCHER_VERSION, "football_context": context, "match_relevance": "unverified",
                    }
    return scanned, rows


def collect(root, *, quarters=12, get=market_provider.get):
    if not 1 <= quarters <= 96:
        raise ValueError("quarters must be in [1,96]")
    last, size, checksum = _manifest(get)
    folder = _directory(root)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = {"matcher_version": MATCHER_VERSION, "latest_index_utc": store.iso(last), "received_editions": 0, "cached_editions": 0,
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


def _visible_article(article, league, season, team):
    if not isinstance(article, dict) or not isinstance(article.get("url"), str):
        return None
    if article.get("matcher_version") == MATCHER_VERSION:
        return article
    # Earlier releases retained only URL, matched label and match source. Verify
    # what survives without claiming a newly acquired title was known back then.
    try:
        parts = urlsplit(context_feeds._article_url(article.get("url")))
    except ValueError:
        return None
    orgs = [article.get("club_label") or ""] if article.get("matched_via") == "indexed_organization" else []
    names = _clubs(season)
    matches = _club_matches(names, _label_pattern(tuple(names)), unquote(parts.path), None, orgs, (), parts.hostname)
    result = matches.get((league, team))
    if result is None:
        return None
    label, via, context = result
    return {**article, "title": None, "title_source": None, "matcher_version": MATCHER_VERSION,
            "legacy_rechecked": True, "club_label": label, "matched_via": via, "football_context": context,
            "match_relevance": "unverified"}


def _fill_known_titles(db, league, season, team, at, items):
    # A new GKG edition often repeats a URL with an empty XMLExtras field.
    # Reuse only a title that was actually received by this cutoff.
    missing = {a["url"]: a for a in items if not a.get("title")}
    if missing:
        marks = ",".join("?" for _ in missing)
        prior = db.execute("SELECT c.document FROM press_clubs c JOIN press_releases r USING(edition) "
                           "WHERE c.league=? AND c.team=? AND r.season=? AND r.received<=? AND "
                           f"c.url IN ({marks}) AND json_type(c.document,'$.title')='text' "
                           "ORDER BY c.edition DESC", (league, team, season, at, *missing))
        for raw, in prior:
            article = _visible_article(json.loads(raw), league, season, team)
            if article and article.get("title") and article["url"] in missing:
                newest = missing.pop(article["url"])
                newest.update(title=article["title"], title_source=article.get("title_source"),
                              title_received_at=article["received_at"], title_record_id=article["record_id"])
                if not missing:
                    break


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
                    article = _visible_article(json.loads(row[0]), league, season, side["team"])
                    if article and article["index_seen_at"] >= start and article["url"] not in seen:
                        side["items"].append(article)
                        seen.add(article["url"])
                    if len(seen) == 15:
                        break
                _fill_known_titles(db, league, season, side["team"], at, side["items"])
    fresh = bool(latest and store.instant(at) - datetime.strptime(latest, "%Y%m%d%H%M%S").replace(tzinfo=UTC) <= timedelta(hours=2))
    for side in teams.values():
        if latest:
            side["state"] = "links_observed" if side["items"] else ("no_links_in_index_window" if fresh else "stale_index")
    # A repeated edition may reorder identical URLs. Only a change to a club's
    # links or their observed titles changes the discovery evidence digest.
    links = [sorted((item["url"], item.get("title")) for item in side["items"]) for side in teams.values()]
    return {"state": ("current_index" if fresh else "stale_index") if latest else "unobserved",
            "attribution": CREDIT, "scope": "club news discovery; club mention, match relevance unverified",
            "teams": teams, "latest_index_edition": latest,
            "evidence_sha256": store.digest(links) if any(links) else None}


def register_feeds(commands):
    parser = commands.add_parser("league-press", help="轮询五大联赛球队的 GDELT 新闻链接")
    parser.add_argument("--quarters", type=int, default=12, help="回看多少个 15 分钟索引批次")
    parser.set_defaults(func=lambda a: collect(a.data_dir, quarters=a.quarters),
                        result_exit_code=lambda result: int(result["state"] == "unavailable"))
