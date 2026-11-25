"""Free, attributed club news inputs. Article hyperlinks are evidence, never fetch targets."""

from __future__ import annotations

import gzip
import hashlib
import io
import ipaddress
import json
import re
import threading
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET

from . import football, independent_results, store
from .market_provider import Reply

WIKI = "wikipedia-season-news"
WIRE = "touchline-availability"
RUMOURS = "touchline-rumours"
WIRE_HOST = "https://touchlinewire.com"
WIKI_LICENSE = independent_results.LICENSE
AGENT = "polymarket861-news/0.4 (+https://github.com/wmqfl861/polymarket861)"
MAX_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 512
RIGHTS = ("fetch", "store", "extract", "model")
# The source uses these display names and club-filter slugs in its public club directory.
WIRE_CLUBS = {
    "brighton-hove-albion": ("Brighton", "brighton"),
    "coventry-city": ("Coventry City", "coventry"),
    "leeds-united": ("Leeds United", "leeds"),
    "manchester-city": ("Man City", "man-city"),
    "manchester-united": ("Man Utd", "man-utd"),
    "newcastle-united": ("Newcastle", "newcastle"),
    "nottingham-forest": ("Nott'm Forest", "nott-m-forest"),
    "tottenham-hotspur": ("Spurs", "tottenham"),
}
# The speculation endpoint identifies clubs by their published short codes. Its text-slug
# parser differs from /injuries (e.g. Nottingham Forest). Use a verified code where known.
_RUMOUR_CODES = {
    "arsenal": "ARS", "aston-villa": "AVL", "bournemouth": "BOU", "brentford": "BRE",
    "brighton-hove-albion": "BHA", "chelsea": "CHE", "coventry-city": "COV", "crystal-palace": "CRY",
    "everton": "EVE", "fulham": "FUL", "hull-city": "HUL", "ipswich-town": "IPS", "leeds-united": "LEE",
    "liverpool": "LIV", "manchester-city": "MCI", "manchester-united": "MUN", "newcastle-united": "NEW",
    "nottingham-forest": "NFO", "sunderland": "SUN", "tottenham-hotspur": "TOT",
}
_ROBOTS = {}
_ROBOTS_LOCK = threading.Lock()


def _public_url(value):
    if not isinstance(value, str) or len(value) > 4096 or re.search(r"[\s\\\x00-\x1f]", value):
        raise ValueError("expected a public HTTPS URL")
    p = urlsplit(value)
    host = p.hostname or ""
    if (p.scheme != "https" or p.username is not None or p.password is not None
            or p.port not in (None, 443) or not re.fullmatch(r"[A-Za-z0-9.-]+", host)
            or host.endswith((".localhost", ".local", ".internal", ".")) or "." not in host):
        raise ValueError("expected a public HTTPS URL without userinfo")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None or not re.search(r"[a-zA-Z]", host.rsplit(".", 1)[-1]):
        raise ValueError("a public publisher DNS name is required")
    return value


def default_sources(team, season):
    year = football.season_year(season)
    key = football.team_key(team)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", key) or len(team) > 100:
        raise ValueError("invalid club name")
    display, slug = WIRE_CLUBS.get(key, (team, key))
    shared = {
        "team": team, "season": season, "authority": "secondary",
        "free": True, "rights": list(RIGHTS),
    }
    wire = {
        **shared, "kind": "touchline", "origin": "touchlinewire.com",
        "license": "Free for personal, academic, and open-source use with attribution",
        "license_url": WIRE_HOST + "/docs#attribution", "terms_url": WIRE_HOST + "/terms.html",
        "attribution": "Tactical Data by Touchline Wire (https://touchlinewire.com). Normalized factual fields.",
        "wire_club": display,
    }
    title = independent_results._title(year, team)
    page_url = "https://api.wikimedia.org/core/v1/wikipedia/en/page/" + quote(title.replace(" ", "_"), safe="")
    return [
        {**wire, "id": WIRE, "url": WIRE_HOST + "/api/v1/injuries?club=" + quote(slug)},
        {**wire, "id": RUMOURS, "url": WIRE_HOST + "/api/v1/rumours?club=" + quote(_RUMOUR_CODES.get(key, key))},
        {**shared, "id": WIKI, "kind": "wiki", "url": page_url,
         "origin": "en.wikipedia.org", "license": "CC BY-SA 4.0", "license_url": WIKI_LICENSE,
         "attribution": "English Wikipedia contributors, CC BY-SA 4.0. Excerpted and stripped of markup.",
         "page_title": title},
    ]


def validate_source(source):
    """Normalize an explicitly licensed RSS/Atom publisher or reconstruct a known default."""
    if not isinstance(source, dict):
        raise ValueError("news source must be an object")
    if source.get("id") in {WIRE, RUMOURS, WIKI}:
        expected = next(s for s in default_sources(source["team"], source["season"]) if s["id"] == source["id"])
        if source != expected:
            raise ValueError("built-in news sources use their published endpoints and licenses")
        return expected
    ident = source.get("id", "")
    if not isinstance(ident, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", ident):
        raise ValueError("invalid news source ID")
    if source.get("kind") not in {"rss", "atom", "feed"} or source.get("free") is not True:
        raise ValueError("register an explicitly free RSS/Atom source")
    if not isinstance(source.get("rights"), (tuple, list)) or set(source["rights"]) != set(RIGHTS):
        raise ValueError("the feed grant must cover fetch, store, extract and model")
    url = _public_url(source.get("url"))
    if urlsplit(url).fragment:
        raise ValueError("configure the exact HTTPS feed endpoint without a fragment")
    license_url = _public_url(source.get("license_url"))
    authority = source.get("authority", "secondary")
    if authority not in {"official", "secondary"}:
        raise ValueError("news authority must be official or secondary")
    fields = {}
    for field in ("team", "license", "attribution"):
        v = source.get(field)
        if not isinstance(v, str) or not v.strip() or len(v) > 500:
            raise ValueError(f"news source requires {field}")
        fields[field] = v.strip()
    origin = source.get("origin") or urlsplit(url).hostname
    if not isinstance(origin, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", origin):
        raise ValueError("invalid upstream origin")
    return dict(fields, kind="feed", id=ident, url=url, origin=origin.lower(), free=True,
                rights=list(RIGHTS), authority=authority, license_url=license_url)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _download(url, limit):
    req = Request(url, headers={"User-Agent": AGENT, "Accept-Encoding": "gzip"})
    start = store.now_utc()
    deadline = time.monotonic() + 20
    with build_opener(_NoRedirect()).open(req, timeout=10) as resp:
        buf = bytearray()
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("news publisher read deadline exceeded")
            block = resp.read1(min(65536, limit + 1 - len(buf)))
            if not block:
                break
            buf.extend(block)
            if len(buf) > limit:
                raise ValueError("publisher response exceeds its byte budget")
        body = bytes(buf)
        if resp.headers.get("Content-Encoding", "").casefold() == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                body = compressed.read(limit + 1)
            if len(body) > limit:
                raise ValueError("uncompressed publisher response exceeds its byte budget")
        return Reply(body, start, store.now_utc(), dict(resp.headers), resp.geturl())


def _permitted(url):
    # User-registered endpoints are the only external feed targets. Item links are never followed.
    p = urlsplit(url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    clock = time.monotonic()
    with _ROBOTS_LOCK:
        entry = _ROBOTS.get(robots_url)
    if entry is None or entry[0] <= clock:
        try:
            answer = _download(robots_url, 256 * 1024)
            lines = answer.body.decode("utf-8-sig", "replace").splitlines()
        except HTTPError as exc:
            if (robots_url == "https://api.wikimedia.org/robots.txt" and exc.code in (301, 302, 307, 308)
                    and exc.headers.get("Location") == "https://www.mediawiki.org/wiki/Wikimedia_APIs"):
                # The Core REST API directs robots to the public API policy page. Follow
                # exactly this publisher-controlled robots redirect, never feed/item redirects.
                policy = _download("https://www.mediawiki.org/wiki/Wikimedia_APIs", 256 * 1024)
                lines = policy.body.decode("utf-8-sig", "replace").splitlines()
            elif exc.code in (404, 410):
                lines = []
            else:
                raise
        parser = RobotFileParser()
        parser.parse(lines)
        entry = (clock + 3600, parser)
        with _ROBOTS_LOCK:
            if len(_ROBOTS) >= 256:
                _ROBOTS.clear()
            _ROBOTS[robots_url] = entry
    if not entry[1].can_fetch("polymarket861-news", url):
        raise ValueError("publisher robots.txt disallows this endpoint")


def _get(url):
    _public_url(url)
    try:
        _permitted(url)
        return _download(url, MAX_BYTES)
    except HTTPError as exc:
        retry = exc.headers.get("Retry-After", "")
        if re.fullmatch(r"\d{1,10}", retry):
            exc.retry_after = int(retry)
        elif retry:
            try:
                exc.retry_after = max(0, (store.instant(_stamp(retry)) - store.instant(store.now_utc())).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
        raise


def _stamp(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing publisher timestamp")
    try:
        return store.iso(value)
    except ValueError:
        return store.iso(parsedate_to_datetime(value))


def _as_known(value, receipt):
    date = _stamp(value)
    if date > receipt:
        raise ValueError("publisher time is after local receipt")
    return date


def _article(source, ident, url, published, revision, title, text, **metadata):
    return {
        "external_id": ident, "url": url, "published_at": published, "revision": str(revision),
        "title": title, "text": text, "license": source["license"], "license_url": source["license_url"],
        "origin": source["origin"], "attribution": source["attribution"], **metadata,
    }


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.content = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        name = tag.split(":")[-1]
        if name in ("script", "style"):
            self.hidden += 1
        if name in ("br", "p", "li", "div"):
            self.content.append(" ")

    def handle_endtag(self, tag):
        if self.hidden and tag.split(":")[-1] in ("script", "style"):
            self.hidden -= 1
        self.content.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.content.append(data)


def _plain(fragment):
    parser = _Text()
    parser.feed(fragment)
    parser.close()
    return " ".join("".join(parser.content).split())


def _contents(node):
    if node is None:
        return ""
    return _plain((node.text or "") + "".join(ET.tostring(c, encoding="unicode") for c in node))


ATOM = "{http://www.w3.org/2005/Atom}"
TOMBSTONE = "{http://purl.org/atompub/tombstones/1.0}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
DC = "{http://purl.org/dc/elements/1.1/}"


def _feed_item(source, item, atom, receipt):
    if item.tag == TOMBSTONE + "deleted-entry":
        ident = item.get("ref")
        when = _as_known(item.get("when"), receipt)
        if not ident or len(ident) > 2048:
            raise ValueError("invalid Atom deleted-entry reference")
        return _article(source, ident, source["url"], when, when, "", "", retracted=True,
                        updated_at=when, time_basis="publisher_withdrawal")

    if atom:
        ident = item.findtext(ATOM + "id")
        link = next((c.get("href") for c in item.findall(ATOM + "link")
                     if c.get("rel", "alternate") == "alternate" and c.get("href")), None)
        title = _contents(item.find(ATOM + "title"))
        body = _contents(item.find(ATOM + "content")) or _contents(item.find(ATOM + "summary"))
        publish = item.findtext(ATOM + "published") or item.findtext(ATOM + "updated")
        update = item.findtext(ATOM + "updated") or publish
    else:
        link = item.findtext("link")
        ident = item.findtext("guid") or link
        title = _contents(item.find("title"))
        body = _contents(item.find(CONTENT + "encoded")) or _contents(item.find("description"))
        publish = item.findtext("pubDate") or item.findtext(DC + "date")
        update = item.findtext(ATOM + "updated") or publish
    link = _public_url(link)
    if not ident or len(ident) > 2048 or not (title or body):
        raise ValueError("missing or oversized feed item identity/content")
    published, updated = _as_known(publish, receipt), _as_known(update, receipt)
    if updated < published:
        raise ValueError("edit time precedes publication")
    # A club-hosted feed can syndicate articles from unrelated publishers.
    if source["authority"] == "official" and urlsplit(link).hostname != urlsplit(source["url"]).hostname:
        raise ValueError("off-origin article cannot inherit official authority")
    text = body if not title or body.startswith(title) else f"{title}. {body}".strip()
    if len(text) > 24000:
        raise ValueError("article exceeds accepted text budget")
    return _article(source, ident, link, published, store.digest([link, published, updated, title, body]),
                    title, text, updated_at=updated, time_basis="publisher_article")


def _feed(source, raw, receipt, cursor):
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.I) or b"\x00" in raw:
        raise ValueError("unsupported XML encoding or document type")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("invalid RSS/Atom response") from exc
    if root.tag == ATOM + "feed":
        items = [e for e in root if e.tag in {ATOM + "entry", TOMBSTONE + "deleted-entry"}]
        atom = True
    elif root.tag == "rss" and root.find("channel") is not None:
        items = root.findall("./channel/item")
        atom = False
    else:
        raise ValueError("expected RSS 2.0 or Atom")
    if len(items) > MAX_ITEMS:
        raise ValueError("feed item limit exceeded")
    accepted, rejected = {}, 0
    for item in items:
        try:
            row = _feed_item(source, item, atom, receipt)
        except (ValueError, TypeError, OverflowError):
            rejected += 1
            continue
        if row["external_id"] in accepted:
            old = accepted[row["external_id"]]
            if row["updated_at"] == old["updated_at"] and row != old:
                raise ValueError("conflicting publisher revisions in one feed")
            if row["updated_at"] <= old["updated_at"]:
                continue
        accepted[row["external_id"]] = row
    if rejected and not accepted:
        raise ValueError(f"all {rejected} publisher feed entries had invalid dates, rights scope or content")
    # Process a large publisher feed across bounded polls, prioritizing the freshest
    # edits for imminent matches. Entries falling out of an RSS window are not withdrawals.
    prior = cursor.get("rows", {})
    known, pending = {}, []
    for row in accepted.values():
        key = store.digest(row["external_id"])
        if prior.get(key) == row["revision"]:
            known[key] = row["revision"]
        else:
            pending.append(row)
    pending.sort(key=lambda r: (r["updated_at"], r["external_id"]), reverse=True)
    batch = pending[:100]
    known.update((store.digest(row["external_id"]), row["revision"]) for row in batch)
    return batch, {"rows": known}, {"entries": len(items), "invalid_entries": rejected,
                                    "pending_entries": len(pending) - len(batch)}


_HEADINGS = re.compile(r"^(={2,6})\s*([^=\n]+?)\s*\1\s*$", re.M)
_PROSE_SECTIONS = re.compile(
    r"(?:review|season (?:review|summary|overview|highlights)|news|team news|squad news|injur(?:y|ies)(?: updates?)?)$", re.I
)
_MONTHS = "January February March April May June July August September October November December".split()
_DATE = re.compile(r"\b([0-3]?\d)(?:st|nd|rd|th)?\s+(" + "|".join(_MONTHS)
                   + r")(?:\s+(\d{4}))?\b", re.I)
_NEWS_WORDS = re.compile(
    r"\b(injur\w*|recover\w*|suspend\w*|suspension|fit(?:ness)?|doubt\w*|return\w*|"
    r"illness|ill|unavailable|ruled out|training|absen\w*|surg\w*|out for|sideline\w*|"
    r"red cards?|yellow cards?|sent off|booked)\b", re.I
)


def wiki_prose(wikitext):
    """Narrative paragraphs in review/news sections; never scores, citations, or roster rows."""
    text = re.sub(r"<!--.*?-->|<ref\b[^>]*?/>|<ref\b[^>]*>.*?</ref\s*>|"
                  r"<blockquote\b[^>]*>.*?</blockquote\s*>", " ", wikitext, flags=re.I | re.S)
    text = re.sub(r"(?ms)^\s*\{\|.*?^\s*\|\}", "\n", text)
    headings = list(_HEADINGS.finditer(text))
    hierarchy, result = {}, {}
    for i, section in enumerate(headings):
        depth = len(section[1])
        hierarchy = {k: v for k, v in hierarchy.items() if k < depth}
        hierarchy[depth] = section[2].strip()
        if (not any(_PROSE_SECTIONS.fullmatch(name) for name in hierarchy.values())
                or any(name.lower() in ("background", "previous season", "statistics", "matches")
                       for name in hierarchy.values())):
            continue
        end = headings[i+1].start() if i + 1 < len(headings) else len(text)
        for line in text[section.end():end].splitlines():
            line = line.strip()
            if (len(line) < 25 or len(line) > 16000 or line.startswith(("|", "!", "{", "[[File:", "[[Image:"))
                    or not _NEWS_WORDS.search(line)):
                continue
            # Keep Wikipedia's authored paraphrase, excluding marked direct quotations, citations,
            # template expansion, table markup and external link labels from the licensed projection.
            for _ in range(8):
                changed = re.sub(r"\{\{[^{}]*\}\}", " ", line)
                if changed == line:
                    break
                line = changed
            line = re.sub(r"\[https?://[^\]]*\]|[\"“][^\"”\n]{1,2000}[\"”]", " ", line)
            line = re.sub(r"\[\[([^|\[\]]+)(?:\|([^\[\]]+))?\]\]", lambda m: m[2] or m[1], line)
            clean = " ".join(unescape(re.sub(r"<[^>]*>|'{2,5}", "", line)).lstrip("*# ").split())
            if len(clean) >= 25 and _NEWS_WORDS.search(clean) and "{{" not in clean and "[[" not in clean:
                result[store.digest(clean)] = clean
    return result


def _recent_passage(text, stamp, season):
    # A new page revision can narrate an event from years ago. Only dated contemporaneous
    # passages enter the live collector. Untimed passages can still be inspected by a researcher.
    dates = list(_DATE.finditer(text))
    if not dates:
        return False
    year = football.season_year(season)
    when = store.instant(stamp).date()
    for item in dates:
        month = _MONTHS.index(item[2].capitalize()) + 1
        try:
            d = datetime(int(item[3]) if item[3] else year + (month < 7), month, int(item[1])).date()
        except ValueError:
            return False
        if abs((d - when).days) > 7:
            return False
    return True


def _wiki(source, raw, receipt, cursor):
    page = json.loads(raw)
    license_url = page.get("license", {}).get("url", "")
    if (page.get("title") != source["page_title"] or page.get("content_model") != "wikitext"
            or not isinstance(page.get("source"), str)
            or license_url.removesuffix("deed.en").rstrip("/") != WIKI_LICENSE.rstrip("/")):
        raise ValueError("unverified Wikipedia page identity or reuse rights")
    latest = page.get("latest", {})
    ident = latest.get("id")
    date = _as_known(latest.get("timestamp"), receipt)
    if type(ident) is not int or ident < 1 or ident < cursor.get("revid", 0):
        raise ValueError("invalid or regressing Wikipedia revision")
    doc = wiki_prose(page["source"])
    previous, active = set(cursor.get("paragraphs", ())), set(cursor.get("active", ()))
    if ident == cursor.get("revid") and set(doc) != previous:
        raise ValueError("Wikipedia narrative changed under the same revision")
    url = f"https://en.wikipedia.org/w/index.php?oldid={ident}"
    rows = []
    if cursor.get("revid"):
        for key in sorted(active - set(doc)):
            rows.append(_article(source, key, url, date, ident, page["title"], "",
                                 retracted=True, updated_at=date, time_basis="publisher_withdrawal"))
            active.remove(key)
        for key in sorted(set(doc) - previous):
            if _recent_passage(doc[key], date, source["season"]):
                # "published_at" identifies the revision containing the new-to-us passage.
                # No claim is made that this is the first revision to introduce it.
                rows.append(_article(source, key, url, date, ident, page["title"], doc[key],
                                     updated_at=date, time_basis="observed_publisher_revision"))
                active.add(key)
    # First observation is a baseline. Updating an unrelated score, table or award
    # cannot turn the rest of an encyclopedia page into newly published team news.
    return rows, {"revid": ident, "paragraphs": sorted(doc), "active": sorted(active)}, {
        "page_revision": ident, "narrative_paragraphs": len(doc), "baseline_only": not bool(cursor.get("revid"))
    }


def _wire_club(source, value):
    if not isinstance(value, str):
        return False
    return football.team_key(value) in {
        football.team_key(source["team"]), football.team_key(source["wire_club"])
    }


def _wire_person(value):
    return (isinstance(value, str) and 2 <= len(value) <= 120 and all(
        c.isalpha() or c in " .’-'" for c in value) and any(c.isalpha() for c in value))


def _wire_header(raw, received, cursor):
    data = json.loads(raw)
    meta = data.get("meta", {})
    if (not str(meta.get("source", "")).startswith("Touchline Wire")
            or meta.get("attribution_url", "").rstrip("/") != WIRE_HOST
            or meta.get("license") != "Free for personal, academic, and open-source use with attribution"):
        raise ValueError("Touchline Wire attribution or reuse policy changed")
    snapshot = _as_known(meta.get("timestamp_iso"), received)
    if store.instant(received) - store.instant(snapshot) > timedelta(days=1):
        raise ValueError("stale Touchline Wire response")
    if snapshot < cursor.get("snapshot", ""):
        raise ValueError("Touchline Wire snapshot moved backwards")
    return data, snapshot


def _wire_availability(source, raw, received, cursor):
    data, snapshot = _wire_header(raw, received, cursor)
    rows, meta = data.get("data"), data["meta"]
    if (not isinstance(rows, list) or type(meta.get("count")) is not int
            or len(rows) != meta["count"] or len(rows) > MAX_ITEMS):
        raise ValueError("incomplete Touchline Wire availability snapshot")
    now = store.instant(snapshot)
    if football.season_year(source["season"]) != now.year - (now.month < 7):
        raise ValueError("current availability snapshot belongs to a different season")
    ids, documents = set(), []
    scope = store.digest([source["season"], football.team_key(source["team"])])[:16]
    for row in rows:
        # An unknown club filter may return the entire league with HTTP 200.
        if not _wire_club(source, row.get("team")):
            raise ValueError("Touchline Wire did not honour the requested club filter")
        player_id = row.get("player_id")
        if type(player_id) is not int or player_id < 1 or player_id in ids:
            raise ValueError("missing or ambiguous Touchline Wire player identifier")
        name = row.get("display_name") or row.get("name")
        if not _wire_person(name):
            raise ValueError("unrecognised Touchline Wire player name")
        ids.add(player_id)
        status = row.get("status")
        chance = row.get("chance_of_playing")
        if chance is not None and (type(chance) is not int or not 0 <= chance <= 100):
            raise ValueError("invalid availability percentage")
        if status == "suspended" and row.get("category") == "suspension":
            phrase = "is reported suspended"
        elif status == "doubtful" and row.get("category") == "injury":
            phrase = "is doubtful"
        elif status == "injured" and row.get("category") == "injury":
            phrase = "is reported injured and unavailable" if chance == 0 else "is reported injured"
        else:
            raise ValueError("unrecognised availability state")
        title = f"{name} — {source['team']} availability"
        text = f"{name} {phrase} for {source['team']}."
        if chance is not None:
            text += f" The provider estimates a {chance}% chance of playing."
        external = f"{scope}:player:{player_id}"
        fields = {k: row[k] for k in ("player_id", "display_name", "name", "team", "status",
                                      "category", "chance_of_playing") if k in row}
        # Each genuinely new provider snapshot is a new observation of a standing status.
        # This field is not an original injury announcement timestamp.
        documents.append(_article(source, external, source["url"], snapshot, snapshot, title, text,
                                  observed_state_as_of=snapshot, updated_at=snapshot,
                                  time_basis="provider_snapshot", provider_player_id=player_id,
                                  source_fields=fields))
    for old in sorted(set(cursor.get("players", ())) - ids):
        documents.append(_article(source, f"{scope}:player:{old}", source["url"], snapshot,
                                  snapshot, "Removed from availability wire", "", retracted=True,
                                  updated_at=snapshot, time_basis="provider_snapshot"))
    semantic = store.digest([(d["external_id"], d["text"]) for d in sorted(
        (d for d in documents if not d.get("retracted")), key=lambda d: d["external_id"])])
    if cursor.get("snapshot") == snapshot:
        if cursor.get("digest") != semantic:
            raise ValueError("publisher sent contradictory availability at the same timestamp")
        documents = []
    return documents, {"snapshot": snapshot, "digest": semantic, "players": sorted(ids)}, {"player_rows": len(rows)}


def _wire_rumours(source, raw, received, cursor):
    data, snapshot = _wire_header(raw, received, cursor)
    payload = data.get("data", {})
    rows, team = payload.get("rumours"), source["team"]
    if (not isinstance(rows, list) or len(rows) > MAX_ITEMS or payload.get("count") != len(rows)
            or payload.get("season") != source["season"].replace("-", "/")
            or not payload.get("club_filter")
            or payload.get("club_filter") != _RUMOUR_CODES.get(football.team_key(team), payload["club_filter"])):
        raise ValueError("incomplete or unscoped Touchline Wire speculation feed")
    documents, seen = [], {}
    for row in rows:
        name, external = row.get("subject_name"), row.get("id")
        # Prospective signings are not current squad members. Do not associate players
        # at another club (or a manager) with the scheduled club's availability.
        if row.get("rumour_type") != "transfer" or not _wire_club(source, row.get("current_club")):
            continue
        if not _wire_person(name) or not isinstance(external, str) or not 0 < len(external) <= 256:
            continue
        published = _as_known(row.get("published_at"), received)
        change = row.get("last_updated") or published
        if isinstance(change, str) and re.fullmatch(r"\d{4}-\d\d-\d\d", change):
            day = datetime.strptime(change, "%Y-%m-%d").date()
            if day > store.instant(snapshot).date():
                raise ValueError("future rumour revision date")
            # The provider supplies a calendar day here. For a later correction the
            # snapshot is a witnessed upper bound; do not invent a midnight timestamp.
            day_end = store.iso(f"{(day + timedelta(days=1)).isoformat()}T00:00:00Z")
            updated = published if day <= store.instant(published).date() else min(day_end, snapshot)
        else:
            updated = max(published, _as_known(change, received))
        status = row.get("status")
        targets = [name for name in (row.get("target_club_names") or ())
                   if _wire_person(name) and not _wire_club(source, name)]
        origin = row.get("cited_origin") or row.get("source_outlet") or source["origin"]
        if not isinstance(origin, str) or len(origin) > 180:
            origin = source["origin"]
        fingerprint = store.digest([external, published, change, status, name, origin, targets])
        seen[external] = fingerprint
        if cursor.get("rows", {}).get(external) == fingerprint:
            continue
        if status != "active":
            # Withdrawal removes an existing rumour. It does not confirm a transfer.
            if status in ("debunked", "ruled_out", "confirmed", "resolved"):
                documents.append(_article(source, external, source["url"], published,
                                          store.digest([fingerprint, updated]), "Withdrawn transfer speculation", "", retracted=True,
                                          updated_at=updated, time_basis="publisher_withdrawal"))
            continue
        title = f"Transfer speculation about {name}"
        target_phrase = f" to join {targets[0]}" if len(targets) == 1 else ""
        text = f"There are rumours that {name} could leave {team}{target_phrase}. The report is unconfirmed."
        fields = {k: row[k] for k in ("id", "rumour_type", "subject_name", "current_club",
                                      "target_club_names", "status", "published_at", "last_updated",
                                      "source_outlet", "cited_origin") if k in row}
        documents.append(_article(source, external, source["url"], published,
                                  store.digest([fingerprint, updated]), title, text, updated_at=updated,
                                  time_basis="publisher_report", origin=origin,
                                  evidence_class="transfer_rumour", source_fields=fields))
    return documents, {"snapshot": snapshot, "rows": seen}, {"upstream_rumours": len(rows)}


def poll(source, team, season, cursor=None, *, getter=None):
    """Fetch one bounded, explicitly scoped source. A failed read never advances the cursor."""
    source = validate_source(source)
    if football.team_key(team) != football.team_key(source["team"]) or source.get("season", season) != season:
        raise ValueError("feed belongs to another team or season")
    football.season_year(season)
    cursor = dict(cursor or {})
    binding = store.digest([source["id"], source["url"], football.team_key(team), season])
    if cursor and cursor.get("binding") != binding:
        raise ValueError("news cursor belongs to another feed subscription")
    url = source["url"]
    answer = (getter or _get)(url)
    if answer.url != url or not isinstance(answer.body, bytes) or len(answer.body) > MAX_BYTES:
        raise ValueError("response does not match requested feed")
    requested, received = store.iso(answer.requested_at), store.iso(answer.received_at)
    if received < requested:
        raise ValueError("invalid source receipt interval")
    if source["id"] == WIRE:
        adapter = _wire_availability
    elif source["id"] == RUMOURS:
        adapter = _wire_rumours
    elif source["id"] == WIKI:
        adapter = _wiki
    else:
        adapter = _feed
    rows, next_cursor, stats = adapter(source, answer.body, received, cursor)
    digest = hashlib.sha256(answer.body).hexdigest()
    cache = {k.lower(): v for k, v in answer.headers.items()}.get("cache-control", "")
    max_age = max((int(i) for i in re.findall(r"\b(?:s-maxage|max-age)=(\d{1,10})\b", cache)), default=0)
    next_poll = store.iso(store.instant(received) + timedelta(seconds=max(300, min(max_age, 31536000))))
    return {
        "status": "ok", "source": source["id"], "request_url": url, "requested_at": requested,
        "received_at": received, "response_sha256": digest, "next_poll_after": next_poll,
        "articles": rows, "cursor": dict(next_cursor, binding=binding), **stats,
    }
