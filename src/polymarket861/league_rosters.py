"""Locally observed first-team data from reviewed Wikimedia revisions."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from html import unescape
from pathlib import Path
from urllib.parse import quote

from . import dbscope, football, league_registry, league_source_rights, news_scope, news_sources, store
from .blob_storage import read_blob


# The reviewed local-language first-team or season pages, pinned by page ID.
# Club names and the season denominator come from the frozen OpenFootball registry.
_ROSTER_SEASON = league_source_rights.SEASON
_ROSTER_DEPENDENCIES = {
    ("fl1", "aj-auxerre"): ("Modèle:Effectif de l'AJ Auxerre 2026-2027", 17498561),
}
_ROSTER_PAGES = {
    'lal': {
        'athletic-club': ('Athletic Club', 31120),
        'ca-osasuna': ('Club Atlético Osasuna', 78835),
        'club-atletico-de-madrid': ('Club Atlético de Madrid', 97193),
        'deportivo-alaves': ('Deportivo Alavés', 4576747),
        'elche-cf': ('Elche Club de Fútbol', 47096),
        'barcelona': ('Fútbol Club Barcelona', 17315),
        'getafe-cf': ('Getafe Club de Fútbol', 115771),
        'levante-ud': ('Levante Unión Deportiva', 119570),
        'malaga-cf': ('Málaga Club de Fútbol', 56635),
        'rc-celta-de-vigo': ('Real Club Celta de Vigo', 2237279),
        'rc-deportivo-la-coruna': ('Real Club Deportivo de La Coruña', 16019),
        'rcd-espanyol-de-barcelona': ('Real Club Deportivo Espanyol', 819752),
        'rayo-vallecano-de-madrid': ('Rayo Vallecano de Madrid', 91526),
        'real-betis-balompie': ('Real Betis Balompié', 1410162),
        'real-madrid-cf': ('Anexo:Temporada 2026-27 del Real Madrid Club de Fútbol', 11508795),
        'real-racing-club-de-santander': ('Real Racing Club de Santander', 1129993),
        'real-sociedad-de-futbol': ('Real Sociedad de Fútbol', 12184),
        'sevilla': ('Sevilla Fútbol Club', 73714),
        'valencia-cf': ('Valencia Club de Fútbol', 46848),
        'villarreal-cf': ('Villarreal Club de Fútbol', 156821),
    },
    'bun': {
        '1-koln': ('1. FC Köln', 53659),
        '1-union-berlin': ('1. FC Union Berlin', 124104),
        '1-fsv-mainz-05': ('1. FSV Mainz 05', 132208),
        'bayer-04-leverkusen': ('Bayer 04 Leverkusen', 54355),
        'borussia-dortmund': ('Borussia Dortmund', 47965),
        'borussia-monchengladbach': ('Borussia Mönchengladbach', 21772),
        'eintracht-frankfurt': ('Eintracht Frankfurt', 88084),
        'augsburg': ('FC Augsburg', 2348171),
        'bayern-munchen': ('FC Bayern München', 3437350),
        'schalke-04': ('FC Schalke 04', 3052752),
        'hamburger-sv': ('Hamburger SV', 19033),
        'rb-leipzig': ('RB Leipzig', 4484236),
        'sc-freiburg': ('SC Freiburg', 101700),
        'sc-paderborn-07': ('SC Paderborn 07', 290375),
        'sv-07-elversberg': ('SV Elversberg', 208337),
        'sv-werder-bremen': ('Werder Bremen', 60780),
        'tsg-1899-hoffenheim': ('Kader der deutschen Fußball-Bundesliga 2026/27', 13962792),
        'vfb-stuttgart': ('VfB Stuttgart', 21787),
    },
    'sea': {
        'ac-milan': ('Associazione Calcio Milan 2026-2027', 11090099),
        'ac-monza': ('Associazione Calcio Monza 2026-2027', 11091641),
        'acf-fiorentina': ('ACF Fiorentina 2026-2027', 11070271),
        'as-roma': ('Associazione Sportiva Roma 2026-2027', 11069457),
        'atalanta-bc': ('Atalanta Bergamasca Calcio 2026-2027', 11108224),
        'bologna-1909': ('Bologna Football Club 1909 2026-2027', 11122391),
        'cagliari-calcio': ('Cagliari Calcio 2026-2027', 11159504),
        'como-1907': ('Como 1907 2026-2027', 11069510),
        'internazionale-milano': ('Football Club Internazionale Milano 2026-2027', 11087995),
        'frosinone-calcio': ('Frosinone Calcio 2026-2027', 11176258),
        'genoa-cfc': ('Genoa Cricket and Football Club 2026-2027', 11070342),
        'juventus': ('Juventus Football Club 2026-2027', 11056453),
        'parma-calcio-1913': ('Parma Calcio 1913 2026-2027', 11127052),
        'ss-lazio': ('Società Sportiva Lazio 2026-2027', 11085510),
        'ssc-napoli': ('Società Sportiva Calcio Napoli 2026-2027', 11088076),
        'torino': ('Torino Football Club 2026-2027', 11056803),
        'us-lecce': ('Unione Sportiva Lecce 2026-2027', 11069944),
        'us-sassuolo-calcio': ('Unione Sportiva Sassuolo Calcio 2026-2027', 11069877),
        'udinese-calcio': ('Udinese Calcio 2026-2027', 11139982),
        'venezia': ('Venezia Football Club 2026-2027', 11163688),
    },
    'fl1': {
        'aj-auxerre': ('Association de la jeunesse auxerroise', 2485479),
        'as-monaco': ('Association sportive de Monaco Football Club', 1694789),
        'angers-sco': ("Angers Sporting Club de l'Ouest", 123860),
        'es-troyes-ac': ('Espérance sportive Troyes Aube Champagne', 134591),
        'lorient': ('Football Club de Lorient', 120787),
        'le-havre-ac': ('Le Havre Athletic Club (football)', 5223071),
        'le-mans': ('Le Mans Football Club', 3766280),
        'lille-osc': ('LOSC Lille', 104405),
        'ogc-nice': ('Olympique Gymnaste Club de Nice', 80464),
        'olympique-lyonnais': ('Olympique lyonnais', 2946983),
        'olympique-de-marseille': ('Olympique de Marseille', 72012),
        'paris': ('Paris Football Club', 436840),
        'paris-saint-germain': ('Saison 2026-2027 du Paris Saint-Germain', 17461845),
        'rc-strasbourg-alsace': ('Racing Club de Strasbourg Alsace', 62519),
        'racing-club-de-lens': ('Racing Club de Lens', 120953),
        'stade-brestois-29': ('Stade brestois 29', 90234),
        'stade-rennais-1901': ('Stade rennais Football Club', 120940),
        'toulouse': ('Toulouse Football Club', 120925),
    },
}


def roster_coverage(team, season, league):
    """Return a reviewed local-language page for the registered club and season."""
    if season != _ROSTER_SEASON or league not in _ROSTER_PAGES:
        return None
    if team not in league_registry.members(league, season):
        return None
    club = league_registry.club_key(league, season, team)
    page = _ROSTER_PAGES[league].get(club)
    if page is None:
        return None
    title, page_id = page
    return {
        "league": league, "season": season, "club": club,
        "language": league_source_rights.LANG[league], "title": title, "page_id": page_id,
        "membership_url": f"{league_registry.SOURCE_ROOT}/{season}/{league_registry.profile(league).source_file}",
        "dependency": _ROSTER_DEPENDENCIES.get((league, club)),
        "club_heading": {
            ("bun", "tsg-1899-hoffenheim"): "TSG 1899 Hoffenheim",
            ("lal", "real-madrid-cf"): "Jugadores con participación en la temporada",
        }.get((league, club)),
    }


_ROSTER_SOURCE = "wikipedia-local-first-team-roster"
_ROSTER_API = "https://api.wikimedia.org/core/v1/wikipedia/{language}/page/{page}"
_ROSTER_PARSER_VERSION = "wiki-roster-2"
_ROSTER_PARSER_KEY = "local-wiki-roster-parser"
_ROSTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_roster_polls (
    league TEXT NOT NULL, season TEXT NOT NULL, club TEXT NOT NULL, attempted_at TEXT NOT NULL,
    next_at TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, PRIMARY KEY(league,season,club)
);
CREATE TABLE IF NOT EXISTS local_roster_projections (
    sha TEXT PRIMARY KEY, document TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS local_roster_receipts (
    league TEXT NOT NULL, season TEXT NOT NULL, club TEXT NOT NULL, ready_at TEXT NOT NULL,
    sha TEXT NOT NULL REFERENCES local_roster_projections(sha),
    PRIMARY KEY(league,season,club,ready_at)
);
"""
_ROSTER_CONCURRENCY = threading.BoundedSemaphore(3)
_ROSTER_REFRESH = timedelta(hours=6)
_ROSTER_MAX_AGE = timedelta(days=7)
_ROSTER_SECTIONS = {
    "es": re.compile(r"(?:plantilla|plantel)(?:\s+y\s+cuerpo técnico)?(?:\s+\d{4}[-/]\d{2,4})?", re.I),
    "de": re.compile(r"(?:(?:aktueller\s+)?kader|profikader|mannschaftskader)"
                     r"(?:\s+(?:der\s+)?(?:saison\s+)?\d{4}/\d{2})?", re.I),
    "it": re.compile(r"rosa(?:\s+\d{4}-\d{4})?", re.I),
    "fr": re.compile(r"effectif professionnel(?:\s+actuel)?", re.I),
}
_SOURCE_COMMENTS = re.compile(r"<!--.*?-->|<ref\b[^>]*?(?:/>|>.*?</ref\s*>)", re.S | re.I)
_ROSTER_MARKUP = re.compile(r"\{\{|\}\}|\[\[|\]\]|\|")
_ROSTER_TEMPLATES = {"es": "jugador de fútbol con esquema", "it": "calciatore in rosa", "fr": "feff joueur"}
_FOREIGN_SQUAD = re.compile(
    r"\b(?:frauen|amat[eé]ure|zweite|u(?:17|19|21|23)|reserve|filial|"
    r"femenin[oa]s?|féminin(?:e|es)?|femminile|réserve)\b", re.I,
)
_SPANISH_STATUS = {
    "lesionado": "injury",
    "lesión": "injury",
}


def _roster_section(text, language, club_heading=None):
    headings = list(news_sources._HEADINGS.finditer(text))
    parents = {}
    for i, heading in enumerate(headings):
        depth = len(heading[1])
        parents = {level: name for level, name in parents.items() if level < depth}
        title = news_sources._TEMPLATE.sub("", heading[2]).strip()
        matches = title == club_heading if club_heading else _ROSTER_SECTIONS[language].fullmatch(title)
        if matches and not any(_FOREIGN_SQUAD.search(name) for name in (*parents.values(), heading[2])):
            end = next((item.start() for item in headings[i + 1:] if len(item[1]) <= depth), len(text))
            return text[heading.end():end], title
        parents[depth] = title
    return "", None


def _template_fields(text, body):
    """Read top-level template parameters without splitting player wikilinks."""
    depth, links, fields, start = 1, 0, [], body
    for match in _ROSTER_MARKUP.finditer(text, body, min(len(text), body + 8192)):
        if match[0] == "{{":
            depth += 1
        elif match[0] == "}}":
            depth -= 1
            if not depth:
                return [*fields, text[start:match.start()].strip()]
        elif match[0] == "[[":
            links += 1
        elif match[0] == "]]":
            links -= 1
        elif depth == 1 and not links:
            fields.append(text[start:match.start()].strip())
            start = match.end()
    return []


def _roster_player(value, number, position=None):
    name = " ".join(unescape(news_sources._WIKILINK.sub(lambda m: m[2] or m[1], value)
                             .replace("'''", "").replace("''", "")).split())
    if (not 1 <= len(name) <= 100 or not any(char.isalpha() for char in name)
            or re.search(r"[\[\]<>|{}\x00-\x1f]", name)):
        return None
    entry = {"name": name}
    if number and re.fullmatch(r"[0-9]{1,2}", number):
        entry["shirt_number"] = int(number)
    target = news_sources._WIKILINK.search(value)
    if target and not any(char in target[1] for char in ":#<>"):
        entry["source_title"] = target[1].strip()
    if position and re.fullmatch(r"[\w -]{1,30}", position):
        entry["source_position"] = position
    return entry


def _roster_templates(section, language):
    template = re.escape(_ROSTER_TEMPLATES[language])
    if language == "es":
        section = re.split(r"\{\{\s*(?:cesiones equipo de fútbol|equipo de fútbol fin)\b",
                           section, maxsplit=1, flags=re.I)[0]
    elif language == "fr":
        section = re.split(r"\{\{\s*feff fin\b", section, maxsplit=1, flags=re.I)[0]
    else:
        section = re.split(r"\{\{\s*calciatore in rosa/fine\b", section, maxsplit=1, flags=re.I)[0]
    rows = list(re.finditer(r"\{\{\s*" + template + r"\s*\|", section, re.I))
    if len(rows) > 100:
        return
    for row in rows:
        fields = {}
        for item in _template_fields(section, row.end()):
            key, sep, value = item.partition("=")
            if sep:
                fields[key.strip().casefold()] = value.strip()
        if language == "it":
            name = fields.get("nome", "")
            number = fields.get("n", "")
            # The season pages mark players who have left with italicised names or numbers.
            if re.match(r"''(?!')", name) or re.match(r"''(?!')", number):
                continue
            yield _roster_player(name, number.strip("' "), fields.get("ruolo"))
        elif language == "es":
            name = fields.get("nombre") or fields.get("name") or ""
            number = next((fields[key].strip("' ") for key in ("num", "no", "n") if key in fields), None)
            player = _roster_player(news_sources._TEMPLATE.sub("", name), number, fields.get("pos"))
            if player is not None:
                markers = {}
                for match in news_sources._TEMPLATE.finditer(name + " " + fields.get("nota", "")):
                    label = match[1].split("|", 1)[0].strip()
                    if label.casefold() in _SPANISH_STATUS:
                        markers[label.casefold()] = label
                if markers:
                    player["roster_markers"] = [
                        {"kind": _SPANISH_STATUS[key], "source_label": markers[key], "match_scope": "unknown"}
                        for key in sorted(markers)
                    ]
            yield player
        else:
            name = " ".join(fields.get(key, "") for key in ("prénom", "nom"))
            yield _roster_player(name, fields.get("num"), fields.get("pos"))


def _german_roster(section, season, club_heading=None):
    tables = re.finditer(r"(?ms)^[ \t]*\{\|(.*?)^[ \t]*\|\}", section)
    table = next((item for item in tables if not club_heading or "wikitable" in item[1].split("\n", 1)[0]), None)
    if not table:
        return
    mentioned = set(re.findall(r"(?i)\bkader\s+saison\s+(\d{4}/\d{2})\b", table[1]))
    if mentioned and mentioned != {season.replace("-", "/")}:
        return
    club_totals = (
        "angegeben sind die Bundesligaspiele und -tore für den BVB." in section
        and bool(re.search(r"(?m)^!\s*BL-Spiele.*\n!\s*BL-Tore.*\n!\s*im Verein seit.*\n!\s*Vertrag bis.*\n!\s*vorheriger Verein", table[1]))
    )
    stats_as_of = re.search(r"Stand Spiele und Tore:\s*([^<\n]+)", section)
    for line in table[1].splitlines():
        if not re.match(r"^\s*\|[^\-+}]", line) or "||" not in line:
            continue
        cells = line.lstrip(" \t|").split("||")
        for index in range(min(2, len(cells))):
            number = cells[index].strip("' |").removeprefix("{{0}}")
            if re.fullmatch(r"\d{1,2}", number):
                break
        else:
            continue
        if len(cells) < index + 3:
            continue
        # Usually the flag follows the squad number. A table can list the flag after the player.
        flag = re.search(r"\{\{\s*[A-Z]{2,4}\s*\|\s*#", cells[index + 1])
        name_cell = cells[index + (2 if flag or club_heading else 1)]
        person = re.search(r"\{\{\s*PersonZelle\s*\|([^{}|]*)\|([^{}|]+)", name_cell, re.I)
        link = news_sources._WIKILINK.search(name_cell)
        name = " ".join(person.groups()) if person else (link[0] if link else "")
        if not name:
            name = news_sources._TEMPLATE.sub("", re.sub(r'^.*?=\s*"[^"]*"\s*\|', "", name_cell)).strip()
            name = re.sub(r"</?[^>]+>|&nbsp;", "", name).strip()
        player = _roster_player(name, number)
        if player is not None and club_totals and len(cells) == index + 9:
            appearances, goals = (
                re.sub(r"\{\{0(?:\|0+)?\}\}", "", value).strip() for value in cells[-5:-3]
            )
            if all(re.fullmatch(r"\d{1,4}", value) for value in (appearances, goals)):
                player["league_totals"] = {
                    "competition": "bun", "scope": "club_career",
                    "appearances": int(appearances), "goals": int(goals),
                    "as_of_source_text": stats_as_of[1].strip() if stats_as_of else None,
                }
        yield player


def _parse_local_roster(text, language, season, *, template=False, club_heading=None):
    cleaned = _SOURCE_COMMENTS.sub("", text)
    section, heading = (cleaned, None) if template else _roster_section(cleaned, language, club_heading)
    if not section:
        return [], "no_supported_first_team_section"
    season_label = rf"\b{season[:4]}[-/](?:{season[:2]})?{season[-2:]}\b"
    if language == "es" and not club_heading and not re.search(season_label, (heading or "") + section):
        return [], "season_not_verified"
    if language == "fr" and not re.search(season_label, re.split(r"\{\{\s*feff joueur\b", section, 1, re.I)[0]):
        return [], "season_not_verified"
    if language == "de":
        rows = _german_roster(section, season, club_heading)
    else:
        rows = _roster_templates(section, language)
    players = {}
    for row in rows:
        if row is None:
            continue
        name = row["name"].casefold()
        if name in players and players[name] != row:
            return [], "ambiguous_roster_name"
        players[name] = row
    if not 11 <= len(players) <= 65:
        return [], "unverified_roster_count"
    return [players[name] for name in sorted(players)], None


def _roster_page(scope, title, page_id, getter):
    language = scope["language"]
    url = _ROSTER_API.format(language=language, page=quote(title.replace(" ", "_"), safe=""))
    answer = getter(url)
    if answer.url != url or not isinstance(answer.body, bytes) or len(answer.body) > news_sources.MAX_BYTES:
        raise ValueError("response does not match Wikimedia roster request")
    page = json.loads(answer.body)
    if not isinstance(page, dict):
        raise ValueError("expected a Wikimedia page object")
    latest, rights = page.get("latest"), page.get("license")
    if not isinstance(latest, dict) or not isinstance(rights, dict):
        raise ValueError("missing Wikimedia roster revision or reuse rights")
    licence = rights.get("url")
    received, requested = store.iso(answer.received_at), store.iso(answer.requested_at)
    if (type(page.get("id")) is not int or page["id"] != page_id or page.get("title") != title
            or page.get("content_model") != "wikitext" or not isinstance(page.get("source"), str)
            or type(latest.get("id")) is not int or latest["id"] < 1 or requested > received
            or licence not in {news_sources.WIKI_LICENSE, news_sources.WIKI_LICENSE.rstrip("/"), news_sources.WIKI_LICENSE + "deed." + language}):
        raise ValueError("unverified Wikimedia roster page identity, revision or reuse rights")
    published = news_sources._as_known(latest.get("timestamp"), received)
    origin = f"https://{language}.wikipedia.org"
    revision = latest["id"]
    metadata = {
        "source": _ROSTER_SOURCE, "authority": "secondary", "language": language,
        "title": title, "page_id": page_id, "revision_id": revision,
        "revision_url": f"{origin}/w/index.php?oldid={revision}",
        "authors_url": f"{origin}/w/index.php?curid={page_id}&action=history",
        "requested_at": requested, "received_at": received, "published_at": published,
        "source_text_sha256": hashlib.sha256(page["source"].encode()).hexdigest(),
        "license": licence, "attribution": "Wikipedia contributors",
        "projection": "First-team roster fields from an identified Wikimedia revision",
        "membership_url": scope["membership_url"],
        "terms": league_source_rights.SOURCES["wikipedia"]["terms"],
        "request_url": url,
    }
    return page["source"], answer.body, metadata


def _roster_db(root):
    db = store.connect(root)
    try:
        dbscope.prepare(db, "local-wiki-rosters-v1", _ROSTER_SCHEMA)
    except BaseException:
        db.close()
        raise
    return db


def _ensure_parser_version(db, now):
    row = db.execute("SELECT value FROM core_metadata WHERE name=?", (_ROSTER_PARSER_KEY,)).fetchone()
    if not row or row[0] != _ROSTER_PARSER_VERSION:
        db.execute("UPDATE local_roster_polls SET next_at=? WHERE next_at>?", (now, now))
        db.execute(
            "INSERT INTO core_metadata VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (_ROSTER_PARSER_KEY, _ROSTER_PARSER_VERSION),
        )


def _roster_update(db, key, projection, ready):
    row = db.execute(
        "SELECT r.ready_at,p.document FROM local_roster_receipts r JOIN local_roster_projections p USING(sha) "
        "WHERE league=? AND season=? AND club=? AND ready_at<? ORDER BY ready_at DESC LIMIT 1", (*key, ready)
    ).fetchone()
    if not row:
        return
    previous_at, raw = row
    previous = json.loads(raw)
    old_pages = {page["page_id"]: page for page in previous["evidence"]}
    for page in projection["evidence"]:
        old = old_pages.get(page["page_id"])
        if old is None:
            continue
        if page["revision_id"] < old["revision_id"] or page["published_at"] < old["published_at"]:
            raise ValueError("publisher returned an older roster revision")
        if (page["revision_id"] == old["revision_id"]
                and page["source_text_sha256"] != old.get("source_text_sha256", page["source_text_sha256"])):
            projection["players"] = []
            projection["reason"] = "conflicting_wikimedia_revision"
            return
    if (previous.get("parser_version") != projection["parser_version"] or old_pages.keys() != {
        page["page_id"] for page in projection["evidence"]
    } or not previous["players"] or not projection["players"]):
        return
    old_names = {item["name"].casefold(): item["name"] for item in previous["players"]}
    names = {item["name"].casefold(): item["name"] for item in projection["players"]}
    if old_names.keys() == names.keys():
        if previous.get("changes"):
            projection["changes"] = previous["changes"]
        return
    projection["changes"] = {
        "scope": "observed_roster_revision",
        "observed_at": ready,
        "compared_with": previous_at,
        "added": [names[k] for k in sorted(names.keys() - old_names.keys())],
        "removed": [old_names[k] for k in sorted(old_names.keys() - names.keys())],
        "prior_evidence": previous["evidence"],
        "evidence": projection["evidence"],
    }


def _refresh_local_roster(root, scope, getter):
    key = tuple(scope[field] for field in ("league", "season", "club"))
    started = store.now_utc()
    with closing(_roster_db(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _ensure_parser_version(db, started)
        old = db.execute("SELECT next_at FROM local_roster_polls WHERE league=? AND season=? AND club=?", key).fetchone()
        if old and old["next_at"] > started:
            return
        next_at = store.iso(store.instant(started) + timedelta(minutes=5))
        db.execute("INSERT INTO local_roster_polls VALUES(?,?,?,?,?,'running',NULL) "
                   "ON CONFLICT(league,season,club) DO UPDATE SET attempted_at=excluded.attempted_at,"
                   "next_at=excluded.next_at,state='running',reason=NULL", (*key, started, next_at))
    reason, projection = None, None
    interval = _ROSTER_REFRESH
    try:
        with _ROSTER_CONCURRENCY:
            text, body, page = _roster_page(scope, scope["title"], scope["page_id"], getter)
            originals = [(body, page)]
            dependency = scope["dependency"]
            if dependency:
                section, _ = _roster_section(_SOURCE_COMMENTS.sub("", text), scope["language"])
                transclusion = dependency[0].split(":", 1)[1]
                if re.search(r"\{\{\s*" + re.escape(transclusion) + r"\s*\}\}", section, re.I):
                    text, body, document = _roster_page(scope, *dependency, getter)
                    originals.append((body, document))
                else:
                    dependency = None
        players, reason = _parse_local_roster(text, scope["language"], scope["season"],
                                             template=bool(dependency), club_heading=scope["club_heading"])
        evidence = []
        for body, metadata in originals:
            original = store.archive_bytes(root, _ROSTER_SOURCE, body,
                                           observed_at=metadata["received_at"], metadata=metadata)
            evidence.append({**metadata, "source_sha256": original["sha256"]})
        projection = {"parser_version": _ROSTER_PARSER_VERSION,
                      "players": players, "reason": reason, "evidence": evidence}
    except (ValueError, TypeError, OSError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        interval = timedelta(seconds=max(900, min(getattr(exc, "retry_after", 0), 86400)))
    with closing(_roster_db(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        ready = store.now_utc()
        if projection is not None:
            try:
                _roster_update(db, key, projection, ready)
            except ValueError as exc:
                reason, projection, interval = str(exc), None, timedelta(minutes=15)
        if projection is not None:
            reason = projection["reason"]
        next_at = store.iso(store.instant(ready) + interval)
        state = "failed" if projection is None else ("unknown" if reason else "received")
        change = db.execute("UPDATE local_roster_polls SET next_at=?,state=?,reason=? "
                            "WHERE league=? AND season=? AND club=? AND attempted_at=?",
                            (next_at, state, reason, *key, started))
        if change.rowcount and projection is not None:
            sha = store.digest(projection)
            db.execute("INSERT OR IGNORE INTO local_roster_projections VALUES(?,?)",
                       (sha, store.canonical(projection)))
            db.execute("INSERT OR IGNORE INTO local_roster_receipts VALUES(?,?,?,?,?)", (*key, ready, sha))


def _roster_fixture(fixture):
    league, season = news_scope.competition(fixture), fixture["season"]
    league_registry.profile(league)
    teams = {side: fixture[side] for side in ("home", "away")}
    expected = football.fixture_id(season, **teams, league=league)
    _, _, home_key, away_key = expected.split("/")
    if fixture["fixture_id"] != expected or home_key == away_key:
        raise ValueError("fixture identity does not match the roster scope")
    return league, season, teams


def collect_fixture_rosters(state_dir, fixture, *, getter=None):
    """Refresh the two league-scoped rosters through the normal publisher permission checks."""
    league, season, teams = _roster_fixture(fixture)
    root = Path(state_dir) / "five-league-rosters"
    scopes = [scope for team in teams.values()
              if (scope := roster_coverage(team, season, league))]
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(_refresh_local_roster, root, scope, getter or news_sources._get) for scope in scopes]
        for task in tasks:
            task.result()
    return fixture_rosters_as_of(state_dir, fixture, store.now_utc())


def _local_roster_as_of(db, root, scope, cutoff):
    unknown = {"status": "unknown", "players": []}
    if scope is None:
        return {**unknown, "reason": "no_reviewed_club_page"}
    if db is None:
        return {**unknown, "reason": "no_local_receipt_by_cutoff"}
    key = tuple(scope[field] for field in ("league", "season", "club"))
    row = db.execute("SELECT r.ready_at,r.sha,p.document FROM local_roster_receipts r "
                     "JOIN local_roster_projections p USING(sha) "
                     "WHERE league=? AND season=? AND club=? AND ready_at<=? ORDER BY ready_at DESC LIMIT 1",
                     (*key, cutoff)).fetchone()
    if row is None:
        return {**unknown, "reason": "no_local_receipt_by_cutoff"}
    ready, sha, raw = row
    document = json.loads(raw)
    if store.digest(document) != sha:
        return {**unknown, "reason": "roster_original_unavailable"}
    if store.instant(cutoff) - store.instant(ready) > _ROSTER_MAX_AGE:
        return {**unknown, "reason": "roster_receipt_stale"}
    try:
        for evidence in document["evidence"]:
            if not evidence["published_at"] <= evidence["received_at"] <= ready:
                raise ValueError("invalid roster receipt clock")
            read_blob(root, evidence["source_sha256"])
    except (OSError, ValueError):
        return {**unknown, "reason": "roster_original_unavailable"}
    return {
        "status": "candidate" if document["players"] else "unknown",
        "ready_at": ready, "source": _ROSTER_SOURCE, **document,
    }


def fixture_rosters_as_of(state_dir, fixture, cutoff):
    """Read locally observed identity evidence at a fixture cutoff without opening a writer."""
    league, season, teams = _roster_fixture(fixture)
    cutoff = store.iso(cutoff)
    if fixture.get("kickoff"):
        cutoff = min(cutoff, store.iso(store.instant(fixture["kickoff"]) - timedelta(microseconds=1)))
    root = Path(state_dir) / "five-league-rosters"
    path = root / "runtime.sqlite3"
    scopes = {side: roster_coverage(team, season, league) for side, team in teams.items()}
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) if path.is_file() else None
    try:
        if db is not None:
            db.execute("BEGIN")
            initialized = db.execute("SELECT 1 FROM sqlite_master WHERE name='local_roster_receipts'").fetchone()
        else:
            initialized = False
        views = {side: _local_roster_as_of(db if initialized else None, root, scope, cutoff)
                 for side, scope in scopes.items()}
    finally:
        if db is not None:
            db.close()
    players = {side: roster["players"] for side, roster in views.items()}
    covered = sum(bool(entries) for entries in players.values())
    return {
        "league": league, "season": season, "fixture_id": fixture["fixture_id"], "cutoff": cutoff,
        "state": "partial" if covered else "unknown", "facts": [],
        "evidence_sha256": store.digest([fixture["fixture_id"], players]) if covered else None,
        "teams": {side: _roster_team(team, views[side]) for side, team in teams.items()},
    }


def _roster_team(team, roster):
    markers = [{"player": player["name"], **marker} for player in roster["players"]
               for marker in player.get("roster_markers", ())]
    availability = {"status": "roster_marker_only" if markers else "unknown", "items": markers}
    if markers:
        availability["evidence"] = roster["evidence"]
    return {
        "team": team,
        "roster": roster,
        "news": {"status": "unknown", "items": []},
        "availability": availability,
        "suspensions": {"status": "unknown", "items": []},
        "lineup": {"status": "unknown", "players": []},
    }
