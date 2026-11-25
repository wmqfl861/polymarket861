"""Point-in-time first-team identity from revisioned English Wikipedia squad templates."""

from __future__ import annotations

import html
import json
import re
import unicodedata
import uuid
from collections import Counter
from contextlib import closing
from datetime import timedelta
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import urlencode

from . import dbscope, independent_results as wiki, store
from .football import season_year, team_key

SOURCE = "enwiki-first-team-squad"
ATTRIBUTION = "English Wikipedia contributors"
_REFRESH = timedelta(hours=6)
_MAX_AGE = timedelta(days=7)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_roster_content (
 digest TEXT PRIMARY KEY, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS news_roster_receipts (
 season TEXT NOT NULL, club TEXT NOT NULL, seen_at TEXT NOT NULL, digest TEXT NOT NULL,
 PRIMARY KEY(season,club,seen_at));
CREATE TABLE IF NOT EXISTS news_roster_checks (
 season TEXT NOT NULL, club TEXT NOT NULL, claim TEXT NOT NULL, next_at TEXT NOT NULL,
 status TEXT NOT NULL, report TEXT NOT NULL, PRIMARY KEY(season,club));
"""
_ENTRY = re.compile(r"\{\{\s*football squad2 player\s*\|([^\n]*?)\}\}", re.I)
_LINK = re.compile(r"(?:^|\|)\s*name\s*=\s*\[\[([^\[\]|]+)(?:\|([^\[\]|]+))?\]\]", re.I)
_FOLD = str.maketrans(
    {
        "ø": "o",
        "Ø": "O",
        "đ": "d",
        "Đ": "D",
        "ł": "l",
        "Ł": "L",
        "æ": "ae",
        "Æ": "Ae",
        "œ": "oe",
        "Œ": "Oe",
        "ı": "i",
        "ß": "ss",
    }
)


# A player can have several public categories. Conflicting role groups stay unknown.
_ROLE_CATEGORIES = {
    "Category:Men's association football " + label: role
    for role, labels in {
        "goalkeeper": ("goalkeepers",),
        "defender": (
            "defenders",
            "fullbacks",
            "full-backs",
            "central defenders",
            "centre-backs",
        ),
        "midfielder": (
            "midfielders",
            "central midfielders",
            "defensive midfielders",
            "attacking midfielders",
        ),
        "forward": ("forwards", "wingers", "strikers"),
    }.items()
    for label in labels
}


def _key(name):
    return team_key(name.replace("A.F.C.", "AFC").replace("F.C.", "FC"))


def _title(team, year):
    if (
        not isinstance(team, str)
        or len(team) > 100
        or not re.fullmatch(r"[\w .&’'–-]+", team)
        or not _key(team)
    ):
        raise ValueError("expected a club name from an EPL fixture")
    return (
        "Template:"
        + wiki._title(year, team).split(" ", 1)[1].removesuffix(" season")
        + " squad"
    )


def _name(value):
    value = html.unescape(value.replace("_", " "))
    return " ".join(re.sub(r"\s*\([^()]+\)$", "", value).split())


def _aliases(names):
    forms = set()
    for name in names:
        if not name:
            continue
        forms.add(name)
        latin = unicodedata.normalize("NFKD", name.translate(_FOLD))
        forms.add("".join(c for c in latin if not unicodedata.combining(c)))
    return sorted(forms)


class _MissingTemplate(ValueError):
    pass


def parse_squad(raw, title, team, received):
    """Return first-team linked names from one licence- and scope-checked Wikimedia revision."""
    (page,) = wiki._query(raw, [title])
    if page.get("missing") and page.get("ns") == 10:
        raise _MissingTemplate("squad template was withdrawn or does not exist")
    revs = page.get("revisions", [])
    if page.get("missing") or page.get("ns") != 10 or len(revs) != 1:
        raise ValueError("club squad template unavailable")
    rev = revs[0]
    slot = rev["slots"]["main"]
    if (
        slot["contentmodel"] != "wikitext"
        or type(rev["revid"]) is not int
        or rev["revid"] < 1
        or store.instant(rev["timestamp"]) > store.instant(received)
    ):
        raise ValueError("invalid squad revision or future publication")
    # Commented squad rows can describe loaned-out/former players.
    body = re.sub(
        r"<!--.*?-->|<noinclude\b[^>]*>.*?</noinclude\s*>",
        "",
        slot["content"],
        flags=re.S | re.I,
    )
    header = re.search(
        r"\{\{\s*football squad\s*\|(.*?)\|\s*list\s*=", body, re.I | re.S
    )
    owner = header and re.search(
        r"(?:^|\|)\s*teamname\s*=\s*([^|}\n]+)", header[1], re.I
    )
    if not owner or _key(owner[1].strip()) != _key(team):
        raise ValueError("squad header does not match the registered club")
    # Only the first team list in this navbox is authoritative; never read manager/other tables.
    section = body[header.end() :]
    boundary = re.search(
        r"</div\s*>|\{\{\s*football squad manager\b|^\s*\}\}", section, re.I | re.M
    )
    if boundary:
        section = section[: boundary.start()]
    links, unlinked = {}, 0
    for player in _ENTRY.finditer(section):
        link = _LINK.search(player[1])
        if not link or any(c in link[1] for c in "#:<>{}"):
            unlinked += 1
            continue
        target = html.unescape(link[1].strip().replace("_", " "))
        if len(target) > 180:
            raise ValueError("unexpected player page title")
        display = _name(link[2] or target)
        links.setdefault(target, set()).add(display)
    if not 11 <= len(links) + unlinked <= 65:
        raise ValueError("incomplete or oversized first-team squad")
    return rev["revid"], store.iso(rev["timestamp"]), links, unlinked


def _identity_url(names):
    return (
        wiki.API
        + "?"
        + urlencode(
            {
                "action": "query",
                "format": "json",
                "formatversion": 2,
                "prop": "pageprops|categories",
                "clcategories": "|".join(_ROLE_CATEGORIES),
                "cllimit": "max",
                "ppprop": "wikibase_item|disambiguation",
                "redirects": 1,
                "maxlag": 5,
                "titles": "|".join(names),
            }
        )
    )


def _people(raw, targets):
    reply = json.loads(raw)
    if "error" in reply or "continue" in reply:
        raise ValueError("incomplete Wikipedia identity lookup")
    query = reply["query"]
    redirects = {}
    for row in (*query.get("normalized", []), *query.get("redirects", [])):
        if row.get("tofragment") or row.get("from") in redirects:
            raise ValueError("ambiguous player page redirect")
        redirects[row["from"]] = row["to"]
    pages = {row["title"]: row for row in query["pages"]}
    reached, resolved, missing = set(), {}, []
    for target, display in targets.items():
        title, path = target, set()
        while title in redirects:
            if title in path:
                raise ValueError("circular player page redirect")
            path.add(title)
            title = redirects[title]
        if title not in pages:
            raise ValueError("Wikipedia reply omitted a requested player page")
        reached.add(title)
        page = pages[title]
        props = page.get("pageprops", {})
        page_id = page.get("pageid")
        if (
            page.get("missing")
            or page.get("ns") != 0
            or type(page_id) is not int
            or page_id < 1
            or "disambiguation" in props
        ):
            missing.append(target)
            continue
        ident = f"enwiki:{page_id}"
        player = resolved.setdefault(
            ident,
            {
                "player_id": ident,
                "name": _name(title),
                "aliases": [],
                "player_url": f"https://en.wikipedia.org/?curid={page_id}",
            },
        )
        if player["name"] != _name(title):
            raise ValueError("inconsistent player page identity")
        categories = sorted(
            {
                c["title"]
                for c in page.get("categories", [])
                if c.get("ns") == 14 and c.get("title") in _ROLE_CATEGORIES
            }
        )
        roles = {_ROLE_CATEGORIES[c] for c in categories}
        if categories:
            player["position_categories"] = categories
        if len(roles) == 1:
            player["position"] = next(iter(roles))
        qid = props.get("wikibase_item")
        if qid and re.fullmatch(r"Q[1-9][0-9]*", qid):
            player["wikidata_id"] = qid
        player["aliases"] = _aliases(
            [*player["aliases"], *display, _name(target), _name(title)]
        )
    if reached != set(pages):
        raise ValueError("unexpected page in player identity lookup")
    return resolved, missing


def _connection(data_dir):
    db = store.connect(data_dir)
    dbscope.prepare(db, "news-roster", _SCHEMA)
    return db


def _fetch(data_dir, getter, url, role):
    raw = getter(url)
    received = store.now_utc()
    if not isinstance(raw, bytes) or len(raw) > wiki.LIMIT:
        raise ValueError("invalid squad API body")
    receipt = store.archive_bytes(
        data_dir,
        SOURCE,
        raw,
        observed_at=received,
        metadata={
            "url": url,
            "role": role,
            "attribution": ATTRIBUTION,
            "license": wiki.LICENSE,
        },
    )
    return raw, receipt["sha256"], received


def _collect(data_dir, team, season, title, getter):
    raw, squad_sha, received = _fetch(data_dir, getter, wiki._url([title]), "squad")
    base = {
        "source": SOURCE,
        "attribution": ATTRIBUTION,
        "license": wiki.LICENSE,
        "season": season,
        "team": team,
        "template": title,
        "squad_sha256": squad_sha,
    }
    try:
        revision, published, links, unlinked = parse_squad(raw, title, team, received)
    except _MissingTemplate as exc:
        return base | {
            "revision": None,
            "published_at": None,
            "source_url": wiki._url([title]),
            "withheld": str(exc),
            "players": [],
            "identity_sha256": [],
            "unlinked_player_rows": 0,
            "unresolved_titles": [],
        }
    names = sorted(links)
    players, missing, proof = {}, [], []
    for offset in range(0, len(names), 45):
        batch = {name: links[name] for name in names[offset : offset + 45]}
        raw, identity_sha, _ = _fetch(
            data_dir, getter, _identity_url(batch), "player_identities"
        )
        results, unknown = _people(raw, batch)
        if players.keys() & results.keys():
            raise ValueError("duplicate player across identity batches")
        players.update(results)
        missing.extend(unknown)
        proof.append(identity_sha)
    if len(players) < 11:
        raise ValueError("fewer than eleven identifiable first-team players")
    membership = {
        "links": {k: sorted(v) for k, v in links.items()},
        "unlinked": unlinked,
    }
    return base | {
        "revision": revision,
        "published_at": published,
        "membership_sha256": store.digest(membership),
        "source_url": f"https://en.wikipedia.org/w/index.php?oldid={revision}",
        "identity_sha256": proof,
        "unlinked_player_rows": unlinked,
        "unresolved_titles": sorted(missing),
        "players": [players[k] for k in sorted(players)],
    }


def _previous_revision(db, season, club):
    row = db.execute(
        """SELECT c.document FROM news_roster_receipts r JOIN news_roster_content c ON c.digest=r.digest
           WHERE r.season=? AND r.club=? AND json_array_length(c.document,'$.players')>0
           ORDER BY seen_at DESC LIMIT 1""",
        (season, club),
    ).fetchone()
    return json.loads(row[0]) if row else None


def _inconsistent_revision(old, new):
    if not old or not new.get("players"):
        return False
    if new["revision"] < old["revision"] or new["published_at"] < old["published_at"]:
        return True
    return new["revision"] == old["revision"] and (
        new["published_at"] != old["published_at"]
        or (
            old.get("membership_sha256")
            and new["membership_sha256"] != old["membership_sha256"]
        )
    )


def _retry(error, at):
    delay = timedelta(minutes=15)
    if isinstance(error, HTTPError) and error.headers:
        retry = error.headers.get("Retry-After", "")
        try:
            until = (
                timedelta(seconds=int(retry))
                if retry.isdigit()
                else parsedate_to_datetime(retry) - at
            )
            delay = max(delay, until)
        except (TypeError, ValueError, OverflowError):
            pass
    return store.iso(at + min(delay, timedelta(days=7)))


def refresh(data_dir, team, season, *, getter=wiki._get):
    """Observe today's squad. Past seasons can only use snapshots previously observed locally."""
    year = season_year(season)
    now = store.instant(store.now_utc())
    title, club = _title(team, year), _key(team)
    base = {"team": team, "season": season, "source": SOURCE}
    if now.year - int(now.month < 7) != year:
        return base | {"status": "out_of_season", "player_count": 0}
    claim = uuid.uuid4().hex
    lease = store.iso(now + timedelta(minutes=4))
    with closing(_connection(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT * FROM news_roster_checks WHERE season=? AND club=?", (season, club)
        ).fetchone()
        if old and old["next_at"] > store.iso(now):
            report = json.loads(old["report"])
            state = {"updated": "cached", "running": "busy"}.get(
                old["status"], old["status"]
            )
            return base | report | {"status": state, "next_at": old["next_at"]}
        db.execute(
            "INSERT OR REPLACE INTO news_roster_checks VALUES(?,?,?,?,?,?)",
            (season, club, claim, lease, "running", old["report"] if old else "{}"),
        )
    document = None
    try:
        document = _collect(data_dir, team, season, title, getter)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        failure = error
    with closing(_connection(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            "SELECT 1 FROM news_roster_checks WHERE season=? AND club=? AND claim=?",
            (season, club, claim),
        ).fetchone():
            return base | {"status": "superseded"}
        seen = store.now_utc()
        if document is None:
            report = {"status": "error", "error": str(failure)}
            next_at = _retry(failure, store.instant(seen))
        else:
            if _inconsistent_revision(_previous_revision(db, season, club), document):
                document = document | {
                    "players": [],
                    "withheld": "provider returned a regressed or inconsistent roster revision",
                }
            fingerprint = store.digest(document)
            db.execute(
                "INSERT OR IGNORE INTO news_roster_content VALUES(?,?)",
                (fingerprint, store.canonical(document)),
            )
            db.execute(
                "INSERT INTO news_roster_receipts VALUES(?,?,?,?)",
                (season, club, seen, fingerprint),
            )
            report = {
                k: document[k]
                for k in (
                    "source_url",
                    "revision",
                    "published_at",
                    "squad_sha256",
                    "identity_sha256",
                    "unlinked_player_rows",
                    "unresolved_titles",
                )
            }
            report.update(
                seen_at=seen,
                player_count=len(document["players"]),
                known_position_count=sum("position" in p for p in document["players"]),
            )
            if document.get("withheld"):
                report.update(status="error", error=document["withheld"])
                next_at = store.iso(store.instant(seen) + timedelta(minutes=15))
            else:
                report["status"] = "updated"
                next_at = store.iso(store.instant(seen) + _REFRESH)
        db.execute(
            "UPDATE news_roster_checks SET status=?, report=?, next_at=? WHERE season=? AND club=?",
            (report["status"], store.canonical(report), next_at, season, club),
        )
    return base | report | {"next_at": next_at}


def as_of(data_dir, team, season, cutoff):
    """Fresh locally known identities for one club. Withhold concurrent cross-club claims."""
    season_year(season)
    through = store.instant(cutoff)
    earliest = store.iso(through - _MAX_AGE)
    with closing(_connection(data_dir)) as db:
        rows = db.execute(
            """SELECT r.club, r.seen_at, r.digest, c.document FROM news_roster_receipts AS r
               JOIN (SELECT club, max(seen_at) AS latest FROM news_roster_receipts
                     WHERE season=? AND seen_at BETWEEN ? AND ? GROUP BY club) AS t
                 ON r.club=t.club AND r.seen_at=t.latest
               JOIN news_roster_content AS c ON c.digest=r.digest WHERE r.season=?""",
            (season, earliest, store.iso(through), season),
        ).fetchall()
    counts, ours = Counter(), None
    for row in rows:
        doc = json.loads(row["document"])
        if (
            store.digest(doc) != row["digest"]
            or doc["season"] != season
            or _key(doc["team"]) != row["club"]
        ):
            raise ValueError("stored roster identity or digest mismatch")
        counts.update(p["player_id"] for p in doc["players"])
        if row["club"] == _key(team):
            ours = doc, row["seen_at"]
    if not ours:
        return []
    doc, seen = ours
    source = {
        k: doc[k]
        for k in (
            "team",
            "season",
            "source",
            "source_url",
            "squad_sha256",
            "identity_sha256",
            "revision",
            "published_at",
            "license",
            "attribution",
        )
    }
    return [
        p | source | {"seen_at": seen}
        for p in doc["players"]
        if counts[p["player_id"]] == 1
    ]
