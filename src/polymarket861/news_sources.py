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


def _conflict(external_id, revision, reason, **details):
    return {"external_id": external_id, "revision": str(revision), "reason": reason, **details}


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
        return _article(source, ident, source["url"], when, "native:" + when, "", "", retracted=True,
                        updated_at=when, native_revision_at=when, legacy_revision=when,
                        time_basis="publisher_withdrawal")

    if atom:
        ident = item.findtext(ATOM + "id")
        link = next((c.get("href") for c in item.findall(ATOM + "link")
                     if c.get("rel", "alternate") == "alternate" and c.get("href")), None)
        title = _contents(item.find(ATOM + "title"))
        body = _contents(item.find(ATOM + "content")) or _contents(item.find(ATOM + "summary"))
        publish = item.findtext(ATOM + "published") or item.findtext(ATOM + "updated")
        updated_marker = item.findtext(ATOM + "updated")
    else:
        link = item.findtext("link")
        ident = item.findtext("guid") or link
        title = _contents(item.find("title"))
        body = _contents(item.find(CONTENT + "encoded")) or _contents(item.find("description"))
        publish = item.findtext("pubDate") or item.findtext(DC + "date")
        updated_marker = item.findtext(ATOM + "updated")
    link = _public_url(link)
    if not ident or len(ident) > 2048 or not (title or body):
        raise ValueError("missing or oversized feed item identity/content")
    published, updated = _as_known(publish, receipt), _as_known(updated_marker or publish, receipt)
    if updated < published:
        raise ValueError("edit time precedes publication")
    # A club-hosted feed can syndicate articles from unrelated publishers.
    if source["authority"] == "official" and urlsplit(link).hostname != urlsplit(source["url"]).hostname:
        raise ValueError("off-origin article cannot inherit official authority")
    text = body if not title or body.startswith(title) else f"{title}. {body}".strip()
    if len(text) > 24000:
        raise ValueError("article exceeds accepted text budget")
    content_revision = store.digest([link, published, updated, title, body])
    # RSS pubDate alone does not identify a revision. Atom/explicit updated does,
    # even when a broken publisher changes the bytes under that same version.
    marker = {"native_revision_at": updated, "legacy_revision": content_revision} if updated_marker else {}
    revision = "native:" + updated if updated_marker else content_revision
    return _article(source, ident, link, published, revision, title, text, updated_at=updated,
                    time_basis="publisher_article", **marker)


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
    accepted, rejected, conflicts = {}, 0, {}
    prior = cursor.get("rows", {})
    native = dict(cursor.get("native", {}))
    for item in items:
        try:
            row = _feed_item(source, item, atom, receipt)
        except (ValueError, TypeError, OverflowError):
            rejected += 1
            continue
        ident, rev = row["external_id"], row["revision"]
        key = store.digest(ident)
        if row.get("native_revision_at"):
            content = row["legacy_revision"]
            earlier = native.get(key, {})
            if earlier.get("revision") == rev and (earlier["content"] != content or earlier.get("blocked")):
                native[key] = {**earlier, "blocked": True}
                conflicts[ident, rev] = _conflict(ident, rev, "contradictory publisher content under one explicit revision", native_revision_at=row["updated_at"])
                if earlier.get("archived", rev) != rev:
                    old_rev = earlier["archived"]
                    conflicts[ident, old_rev] = _conflict(ident, old_rev, "contradictory legacy content under one explicit revision")
                continue
            if not earlier or rev > earlier["revision"]:
                archived = content if prior.get(key) == content else rev
                native[key] = {"revision": rev, "content": content, "archived": archived}
        old = accepted.get(ident)
        if old and row["updated_at"] == old["updated_at"] and row != old:
            for variant in (old, row):
                ver = variant["revision"]
                conflicts[ident, ver] = _conflict(ident, ver, "contradictory publisher entries at one revision")
            continue
        if not old or row["updated_at"] > old["updated_at"]:
            accepted[ident] = row
    if rejected and not accepted and not conflicts:
        raise ValueError(f"all {rejected} publisher feed entries had invalid dates, rights scope or content")
    # Retain the complete bounded feed under this response's first receipt. The
    # extractor drains its own durable queue; a rolling publisher window may
    # already have removed any overflow by the time we poll it again.
    known, pending = {}, []
    for row in accepted.values():
        key = store.digest(row["external_id"])
        if (row["external_id"], row["revision"]) in conflicts:
            continue
        if key in prior and prior[key] in (row["revision"], row.get("legacy_revision")):
            known[key] = row["revision"]
        else:
            pending.append(row)
    pending.sort(key=lambda r: (r["updated_at"], r["external_id"]), reverse=True)
    known.update((store.digest(row["external_id"]), row["revision"]) for row in pending)
    # Rows outside the current feed window remain protected by the stable native
    # revision identity in the immutable archive, without unbounded cursor growth.
    present = {store.digest(row["external_id"]) for row in accepted.values()} | {
        store.digest(ident) for ident, _ in conflicts}
    return pending, {"rows": known, "native": {k: v for k, v in native.items() if k in present}}, {
        "entries": len(items), "invalid_entries": rejected, "revision_conflicts": list(conflicts.values()),
        "pending_entries": 0}


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


_QUOTE_TEMPLATES = {'"', "quote", "quotation", "cquote", "quote inline", "inline quote", "q"}
_TEMPLATE = re.compile(r"\{\{([^{}]*)\}\}")
_WIKILINK = re.compile(r"\[\[([^|\[\]]+)(?:\|([^\[\]]+))?\]\]")


def _quote_text(match):
    name, *params = match[1].split("|")
    if name.strip().casefold() not in _QUOTE_TEMPLATES:
        return " "
    positional = None
    for param in params:
        key, sep, value = param.partition("=")
        if sep and key.strip().casefold() in {"1", "quote", "text", "quotetext"}:
            return "“" + value.strip() + "”"
        if not sep and positional is None:
            positional = param.strip()
    return "“" + positional + "”" if positional else " "


def _wiki_lines(wikitext):
    text = re.sub(r"<!--.*?-->|<ref\b[^>]*?/>|<ref\b[^>]*>.*?</ref\s*>|"
                  r"<blockquote\b[^>]*>.*?</blockquote\s*>", " ", wikitext, flags=re.I | re.S)
    text = re.sub(r"(?ms)^\s*\{\|.*?^\s*\|\}", "\n", text)
    headings = list(_HEADINGS.finditer(text))
    hierarchy = {}
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
            if (len(line) < 25 or len(line) > 16000 or line.startswith(("|", "!", "[[File:", "[[Image:"))
                    or (line.startswith("{") and line.partition("|")[0].lstrip("{").strip().casefold() not in _QUOTE_TEMPLATES)
                    or not _NEWS_WORDS.search(line)):
                continue
            yield line


def _wiki_line(line, inline_quotes):
    if inline_quotes:
        # Resolve piped link labels before parsing named quote template fields.
        line = _WIKILINK.sub(lambda m: m[2] or m[1], line)
    for _ in range(8):
        changed = _TEMPLATE.sub(_quote_text if inline_quotes else " ", line)
        if changed == line:
            break
        line = changed
    line = re.sub(r"\[https?://[^\]]*\]", " ", line)
    if not inline_quotes:
        line = re.sub(r'["“][^"”\n]{1,2000}["”]', " ", line)
        line = _WIKILINK.sub(lambda m: m[2] or m[1], line)
    clean = " ".join(unescape(re.sub(r"<[^>]*>|'{2,5}", "", line)).lstrip("*# ").split())
    if len(clean) >= 25 and _NEWS_WORDS.search(clean) and "{{" not in clean and "[[" not in clean:
        return clean
    return None


def wiki_prose(wikitext):
    """Project narrative and inline quoted words, excluding references, tables and block quotations."""
    return {store.digest(clean): clean for line in _wiki_lines(wikitext) if (clean := _wiki_line(line, True))}


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
    previous, active = set(cursor.get("paragraphs", ())), set(cursor.get("active", ()))
    aliases = cursor.get("quote_aliases", {})
    migrating = bool(cursor.get("revid")) and "quote_aliases" not in cursor
    doc, retained_aliases = {}, {}
    for line in _wiki_lines(page["source"]):
        clean = _wiki_line(line, True)
        if clean is None:
            continue
        key = store.digest(clean)
        legacy = aliases.get(key)
        if migrating:
            old = _wiki_line(line, False) if not line.startswith("{") else None
            if old and store.digest(old) in previous:
                legacy = store.digest(old)
            elif old is None:
                # A newly visible quote on upgrade has no proven publication time.
                previous.add(key)
        if legacy and legacy != key:
            retained_aliases[key] = legacy
        doc[legacy or key] = clean
    active_versions = dict(cursor.get("active_versions", {}))
    blocked = set(cursor.get("blocked", ()))
    stats = {"page_revision": ident, "narrative_paragraphs": len(doc), "baseline_only": not bool(cursor.get("revid"))}
    if ident == cursor.get("revid"):
        blocked.update(active - set(doc))
        blocked.update(k for k in set(doc) - previous if _recent_passage(doc[k], date, source["season"]))
        if cursor.get("revision_time", date) != date:
            blocked.update(k for k in active if active_versions.get(k, ident) == ident)
        conflicts = []
        for key in sorted(blocked):
            if key in active and key not in active_versions:
                # Older releases retained each paragraph's ID, but not the specific
                # page revision which introduced it. Bound this hold to that paragraph.
                conflicts.append({"external_id": key, "revision_through": ident,
                                  "reason": "contradictory immutable Wikipedia revision"})
            else:
                conflicts.append(_conflict(key, active_versions.get(key, ident),
                                           "contradictory immutable Wikipedia revision"))
        return [], dict(cursor, paragraphs=sorted(previous), quote_aliases=retained_aliases,
                        blocked=sorted(blocked)), {**stats, "revision_conflicts": conflicts}
    url = f"https://en.wikipedia.org/w/index.php?oldid={ident}"
    rows = []
    if cursor.get("revid"):
        for key in sorted(active - set(doc)):
            rows.append(_article(source, key, url, date, ident, page["title"], "",
                                 retracted=True, updated_at=date, time_basis="publisher_withdrawal"))
            active.remove(key)
            active_versions.pop(key, None)
        for key in sorted((set(doc) - previous) | (blocked & set(doc))):
            if _recent_passage(doc[key], date, source["season"]):
                # "published_at" identifies the revision containing the new-to-us passage.
                # No claim is made that this is the first revision to introduce it.
                rows.append(_article(source, key, url, date, ident, page["title"], doc[key],
                                     updated_at=date, time_basis="observed_publisher_revision"))
                active.add(key)
                active_versions[key] = ident
    # First observation is a baseline. Updating an unrelated score, table or award
    # cannot turn the rest of an encyclopedia page into newly published team news.
    return rows, {"revid": ident, "revision_time": date, "paragraphs": sorted(doc), "quote_aliases": retained_aliases,
                  "active": sorted(active), "active_versions": active_versions}, stats


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
    meta = data.get("meta") if isinstance(data, dict) else None
    if (not isinstance(meta, dict) or not isinstance(meta.get("source"), str)
            or not meta["source"].startswith("Touchline Wire")
            or not isinstance(meta.get("attribution_url"), str) or meta["attribution_url"].rstrip("/") != WIRE_HOST
            or meta.get("license") != "Free for personal, academic, and open-source use with attribution"):
        raise ValueError("Touchline Wire attribution or reuse policy changed")
    snapshot = _as_known(meta.get("timestamp_iso"), received)
    if store.instant(received) - store.instant(snapshot) > timedelta(days=1):
        raise ValueError("stale Touchline Wire response")
    if snapshot < cursor.get("snapshot", ""):
        raise ValueError("Touchline Wire snapshot moved backwards")
    return data, snapshot


WIRE_FIELDS = ("player_id", "display_name", "name", "team", "status", "category", "chance_of_playing")
WIRE_MODEL = "touchline-availability-fields-v1"


def wire_labels(fields):
    """Map licensed publisher fields directly to the normalized claim vocabulary."""
    status, category, chance = fields.get("status"), fields.get("category"), fields.get("chance_of_playing")
    if chance is not None and (type(chance) is not int or not 0 <= chance <= 100):
        raise ValueError("invalid availability percentage")
    if (status, category) not in (("suspended", "suspension"), ("injured", "injury"), ("doubtful", "injury")):
        raise ValueError("unrecognised availability state")
    if chance is not None:
        availability = "available" if chance == 100 else "out" if chance == 0 else "doubtful"
    elif status == "suspended":
        availability = "out"
    elif status == "doubtful":
        availability = "doubtful"
    else:
        availability = "unknown"
    return category, availability


def wire_fingerprint(fields):
    # A missing optional percentage and an explicit null express the same state.
    return store.digest([fields.get(k) for k in WIRE_FIELDS])


def wire_claim(original, aliases):
    """Resolve the supplied names against this roster; the quote is attribution only."""
    from .news_ai import _name_key

    fields = original["source_fields"]
    event, availability = wire_labels(fields)
    names = {_name_key(fields[k]) for k in ("display_name", "name") if _wire_person(fields.get(k))}
    matches = [aliases[name] for name in names if name in aliases]
    common = set.intersection(*matches) if matches else set()
    # A surname on a shirt cannot overrule an unfamiliar full name.
    if len(common) != 1 or (any(len(n) > 1 for n in names) and
                            not any(len(n) > 1 and n in aliases for n in names)):
        return []
    text = original["text"]
    return [{"player_id": next(iter(common)), "event": event, "status": "reported",
             "availability": availability, "chance_of_playing": fields.get("chance_of_playing"),
             "start": 0, "end": len(text), "quote": text, "model_version": WIRE_MODEL}]


def _wire_row(source, scope, row, snapshot):
    if not isinstance(row, dict) or not _wire_club(source, row.get("team")):
        raise ValueError("missing Touchline Wire row or club identity")
    player_id = row.get("player_id")
    if type(player_id) is not int or player_id < 1:
        raise ValueError("invalid Touchline Wire player identifier")
    name = row.get("display_name") or row.get("name")
    if not _wire_person(name):
        raise ValueError("unrecognised Touchline Wire player name")
    fields = {k: row[k] for k in WIRE_FIELDS if k in row}
    if any(k in fields and fields[k] is not None and not _wire_person(fields[k])
           for k in ("display_name", "name")):
        raise ValueError("unrecognised Touchline Wire player name")
    _, availability = wire_labels(fields)
    status, chance = fields["status"], fields.get("chance_of_playing")
    phrase = {"suspended": "is reported suspended", "doubtful": "is doubtful",
              "injured": "is reported injured and unavailable" if availability == "out" else "is reported injured"}[status]
    text = f"{name} {phrase} for {source['team']}."
    if chance is not None:
        text += f" The provider estimates a {chance}% chance of playing."
    return _article(source, f"{scope}:player:{player_id}", source["url"], snapshot, snapshot,
                    f"{name} — {source['team']} availability", text, observed_state_as_of=snapshot,
                    updated_at=snapshot, time_basis="provider_snapshot", provider_player_id=player_id,
                    source_fields=fields)


def _wire_rejection(source, scope, row, exc):
    # Only attributable scalar factual fields may enter quarantine. Free text,
    # embedded third-party analysis and unscoped rows are represented by a hash.
    fields = {}
    if isinstance(row, dict) and _wire_club(source, row.get("team")):
        fields = {k: row[k] for k in WIRE_FIELDS if k in row and
                  (type(row[k]) is int or row[k] is None or
                   isinstance(row[k], str) and row[k].isprintable() and len(row[k]) <= 120)}
    player = fields.get("player_id")
    # Some JSON implementations emit NaN or lone surrogates in a bad field.
    # Fingerprint that row without copying it into the licensed projection.
    encoded = json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    return {"reason": str(exc), "row_sha256": hashlib.sha256(encoded).hexdigest(),
            "item": {"external_id": f"{scope}:player:{player}" if type(player) is int else "",
                     "revision": store.digest(fields), "source_fields": fields}}


def _wire_availability(source, raw, received, cursor):
    data, snapshot = _wire_header(raw, received, cursor)
    rows, meta = data.get("data"), data["meta"]
    if (not isinstance(rows, list) or type(meta.get("count")) is not int
            or len(rows) != meta["count"] or len(rows) > MAX_ITEMS):
        raise ValueError("incomplete Touchline Wire availability snapshot")
    now = store.instant(snapshot)
    if football.season_year(source["season"]) != now.year - (now.month < 7):
        raise ValueError("current availability snapshot belongs to a different season")
    # A positive foreign club means the provider returned a different population.
    if any(isinstance(row, dict) and isinstance(row.get("team"), str) and row["team"].strip()
           and not _wire_club(source, row["team"]) for row in rows):
        raise ValueError("Touchline Wire did not honour the requested club filter")
    scope = store.digest([source["season"], football.team_key(source["team"])])[:16]
    by_player, fingerprints, rejected, blocked, malformed = {}, {}, [], set(), set()
    for row in rows:
        try:
            doc = _wire_row(source, scope, row, snapshot)
        except ValueError as exc:
            rejected.append(_wire_rejection(source, scope, row, exc))
            if isinstance(row, dict) and type(row.get("player_id")) is int:
                malformed.add(str(row["player_id"]))
            continue
        key = str(doc["provider_player_id"])
        fingerprint = store.digest(doc["source_fields"])  # compatible with 0.4.0 cursors
        if key in fingerprints and fingerprints[key] != fingerprint:
            blocked.add(key)
        else:
            fingerprints[key], by_player[key] = fingerprint, doc
    blocked.update(malformed & by_player.keys())
    prior = cursor.get("states", {})
    previous_ids = {str(i) for i in cursor.get("players", ())}
    same = cursor.get("snapshot") == snapshot
    previous_blocked = set(cursor.get("blocked", ()))
    if same:
        blocked.update(previous_blocked)
        blocked.update(k for k in prior.keys() & fingerprints.keys() if prior[k] != fingerprints[k])
        if not rejected and cursor.get("complete", True):
            blocked.update(previous_ids ^ fingerprints.keys())
    conflicts = [_conflict(f"{scope}:player:{key}", snapshot,
                           "contradictory player availability at one provider snapshot",
                           native_revision_at=snapshot) for key in sorted(blocked)]
    observations = [doc for key, doc in by_player.items() if key not in blocked]
    documents = [doc for key, doc in by_player.items() if key not in blocked
                 and (prior.get(key) != fingerprints[key] or key in previous_blocked)]
    # A partial list cannot prove that a previously listed player disappeared.
    # Carry possible members forward until a complete, newer response resolves them.
    active = dict(prior) if rejected or same else {}
    active.update(fingerprints)
    ids = set(active) | (previous_ids if rejected or same else set())
    if not same and not rejected:
        for old in sorted(previous_ids - ids):
            documents.append(_article(source, f"{scope}:player:{old}", source["url"], snapshot,
                                      snapshot, "Removed from availability wire", "", retracted=True,
                                      updated_at=snapshot, time_basis="provider_snapshot"))
    return documents, {"snapshot": snapshot, "states": active, "players": sorted(map(int, ids)),
                       "blocked": sorted(blocked), "complete": not rejected}, {
        "player_rows": len(rows), "invalid_entries": len(rejected), "wire_rejections": rejected,
        "wire_observations": observations, "provider_state_as_of": snapshot, "revision_conflicts": conflicts}


def _wire_rumours(source, raw, received, cursor):
    data, snapshot = _wire_header(raw, received, cursor)
    payload = data.get("data", {})
    rows, team = payload.get("rumours"), source["team"]
    if (not isinstance(rows, list) or len(rows) > MAX_ITEMS or payload.get("count") != len(rows)
            or payload.get("season") != source["season"].replace("-", "/")
            or not payload.get("club_filter")
            or payload.get("club_filter") != _RUMOUR_CODES.get(football.team_key(team), payload["club_filter"])):
        raise ValueError("incomplete or unscoped Touchline Wire speculation feed")
    documents, seen, revisions = [], {}, {}
    prior, prior_versions = cursor.get("rows", {}), cursor.get("revisions", {})
    prev_blocked = set(cursor.get("blocked", ()))
    same_snapshot = cursor.get("snapshot") == snapshot
    blocked = set(prev_blocked) if same_snapshot else set()
    native_times = {}
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
        day_only = isinstance(change, str) and re.fullmatch(r"\d{4}-\d\d-\d\d", change)
        if day_only:
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
        if external in seen and seen[external] != fingerprint:
            blocked.add(external)
        old_token = native_times.get(external) or cursor.get("native_times", {}).get(external)
        native = bool(row.get("last_updated")) and not day_only
        if native:
            native_times[external] = updated
            if old_token == updated and (prior.get(external) != fingerprint or external in prev_blocked):
                blocked.add(external)
        seen[external] = fingerprint
        legacy = store.digest([fingerprint, updated])
        rev = "native:" + updated if native else legacy
        revision_meta = {"native_revision_at": updated, "legacy_revision": legacy} if native else {}
        if external in prev_blocked and not native and not same_snapshot:
            # Coarse last_updated dates can legitimately contain several versions.
            # A later dataset explicitly corroborates the once-disputed observation.
            rev = store.digest([fingerprint, updated, snapshot])
        revisions[external] = rev
        if prior.get(external) == fingerprint and external not in prev_blocked:
            revisions[external] = prior_versions.get(external, legacy)
            continue
        if status != "active":
            # Withdrawal removes an existing rumour. It does not confirm a transfer.
            if status in ("debunked", "ruled_out", "confirmed", "resolved"):
                documents.append(_article(source, external, source["url"], published,
                                          rev, "Withdrawn transfer speculation", "", retracted=True,
                                          updated_at=updated, time_basis="publisher_withdrawal", **revision_meta))
            continue
        title = f"Transfer speculation about {name}"
        target_phrase = f" to join {targets[0]}" if len(targets) == 1 else ""
        text = f"There are rumours that {name} could leave {team}{target_phrase}. The report is unconfirmed."
        fields = {k: row[k] for k in ("id", "rumour_type", "subject_name", "current_club",
                                      "target_club_names", "status", "published_at", "last_updated",
                                      "source_outlet", "cited_origin") if k in row}
        documents.append(_article(source, external, source["url"], published,
                                  rev, title, text, updated_at=updated,
                                  time_basis="publisher_report", origin=origin,
                                  evidence_class="transfer_rumour", source_fields=fields, **revision_meta))
    if same_snapshot:
        blocked.update(k for k in prior.keys() | seen.keys() if prior.get(k) != seen.get(k))
    conflicts = [_conflict(k, rev, "contradictory transfer report at one provider revision",
                           **({"native_revision_at": native_times[k]} if k in native_times else {}))
                 for k in sorted(blocked) for rev in sorted({v for v in (prior_versions.get(k), revisions.get(k)) if v})]
    if same_snapshot:
        # Old cursors kept just the previous content fingerprint. Intake can identify
        # the exact prior archive row as it stood before this collision was received.
        conflicts.extend({"external_id": k, "latest_observed_before": received,
                          "reason": "contradictory transfer report at one provider revision"}
                         for k in sorted(blocked) if k in prior and k not in prior_versions)
        return [], dict(cursor, blocked=sorted(blocked)), {"upstream_rumours": len(rows), "revision_conflicts": conflicts}
    return [d for d in documents if d["external_id"] not in blocked], {
        "snapshot": snapshot, "rows": seen, "revisions": revisions, "native_times": native_times,
        "blocked": sorted(blocked)}, {"upstream_rumours": len(rows), "revision_conflicts": conflicts}


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
