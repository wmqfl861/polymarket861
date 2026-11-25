"""Revision-attributed English Wikipedia match reports for the English Premier League."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import lru_cache
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from . import dbscope, store
from .result_feed import AGENT

SOURCE = "wikipedia-en-epl"
API = "https://en.wikipedia.org/w/api.php"
LICENSE = "https://creativecommons.org/licenses/by-sa/4.0/"
LIMIT = 8 * 1024 * 1024
_SCHEMA = """
CREATE TABLE IF NOT EXISTS epl_wiki_receipts(
 year INTEGER NOT NULL, started TEXT NOT NULL, received TEXT NOT NULL, sha256 TEXT NOT NULL,
 titles TEXT NOT NULL, url TEXT NOT NULL, PRIMARY KEY(year,received));
CREATE TABLE IF NOT EXISTS epl_wiki_checks(
 year INTEGER PRIMARY KEY, attempted TEXT NOT NULL, next_check TEXT NOT NULL, status TEXT NOT NULL);
"""


def _title(year, team):
    name = re.sub(r" FC$", " F.C.", re.sub(r" AFC$", " A.F.C.", team.strip()))
    if name == "Bournemouth":
        name = "AFC Bournemouth"
    elif name in {"Hull City", "Sunderland", "Swansea City"}:
        name += " A.F.C."
    elif not name.startswith("AFC ") and not name.endswith("F.C."):
        # Older pinned OpenFootball files commonly omit a club's FC suffix.
        name += " F.C."
    return f"{year}–{(year + 1) % 100:02d} {name} season"


def _url(titles):
    return (
        API
        + "?"
        + urlencode(
            {
                "action": "query",
                "format": "json",
                "formatversion": 2,
                "prop": "revisions",
                "rvprop": "ids|timestamp|content",
                "rvslots": "main",
                "meta": "siteinfo",
                "siprop": "rightsinfo",
                "titles": "|".join(titles),
                "maxlag": 5,
            }
        )
    )


def _get(url):
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": AGENT,
            "Cache-Control": "no-cache",
        },
    )
    with urlopen(req, timeout=30) as response:
        if response.url != url:
            raise ValueError("unexpected Wikimedia API redirect")
        raw = response.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("oversize Wikimedia response")
    return raw


def _query(raw, titles):
    reply = json.loads(raw)
    if "error" in reply:
        raise ValueError("Wikimedia API error: " + str(reply["error"].get("code")))
    query = reply["query"]
    if query["rightsinfo"]["url"].removesuffix("deed.en").rstrip("/") != LICENSE.rstrip(
        "/"
    ):
        raise ValueError("unrecognised Wikimedia reuse licence")
    pages = query["pages"]
    if sorted(p["title"] for p in pages) != sorted(titles):
        raise ValueError("incomplete or unexpected Wikimedia article scope")
    return pages


def _receive(root, year, teams):
    titles = sorted({_title(year, team) for team in teams})
    if len(titles) != 20:
        raise ValueError("a complete English Premier League club roster is required")
    url, started = _url(titles), store.now_utc()
    raw = _get(url)
    received = store.now_utc()
    pages = _query(raw, titles)
    receipt = store.archive_bytes(
        root,
        SOURCE,
        raw,
        observed_at=received,
        metadata={
            "url": url,
            "license": LICENSE,
            "attribution": "English Wikipedia contributors",
        },
    )
    with closing(store.connect(root)) as db:
        dbscope.prepare(db, "independent-results", _SCHEMA)
        with db:
            db.execute(
                "INSERT INTO epl_wiki_receipts VALUES(?,?,?,?,?,?)",
                (
                    year,
                    started,
                    received,
                    receipt["sha256"],
                    store.canonical(titles),
                    url,
                ),
            )
    return {
        "status": "retrieved",
        "source": SOURCE,
        "received": received,
        "sha256": receipt["sha256"],
        "pages": sum("missing" not in page for page in pages),
    }


# Read only the club's Premier League section. Cup and friendly scores have different
# regulation and identity semantics. Unknown layouts cannot supply a positive final.
_SECTION = re.compile(r"^(={2,5})[ \t]*Premier League[ \t]*\1[ \t]*$", re.M | re.I)
_BOX = re.compile(
    r"^[ \t]*\{\{[ \t]*football box(?: collapsible)?[ \t]*\n(.*?)^[ \t]*\}\}[ \t]*$",
    re.M | re.S | re.I,
)
_FIELDS = re.compile(
    r"^[ \t]*\|[ \t]*(round|team[12]|date|time|report|score|result|status)[ \t]*=[ \t]*([^\n]*)",
    re.M | re.I,
)
_LINK = re.compile(r"\[\[([^\[\]|]+)(?:\|([^\[\]]+))?\]\]")
_REPORT = re.compile(
    r"https?://(?:www\.)?premierleague\.com/(?:en/)?match/([1-9]\d*)(?:[/?#][^\s\[\]]*)?",
    re.I,
)


def _plain(value, *, target=False):
    return _LINK.sub(lambda m: m[1] if target else (m[2] or m[1]), value).strip()


def _club(name):
    from .football import team_key

    return team_key(
        _plain(name, target=True).replace("A.F.C.", "AFC").replace("F.C.", "FC")
    )


def _kickoff(day, clock):
    clock = re.fullmatch(r"(\d{1,2}):(\d\d)\s+(BST|GMT|UTC)", _plain(clock))
    if not clock:
        return None
    start = re.fullmatch(
        r"\{\{start date\|(\d{4})\|(\d{1,2})\|(\d{1,2})(?:\|df=y)?\}\}", day, re.I
    )
    try:
        value = (
            datetime(*map(int, start.groups()))
            if start
            else datetime.strptime(day, "%d %B %Y")
        )
        local = value.replace(
            hour=int(clock[1]), minute=int(clock[2]), tzinfo=ZoneInfo("Europe/London")
        )
        if clock[3] == "UTC":
            local = local.replace(tzinfo=ZoneInfo("UTC"))
        elif local.tzname() != clock[3]:
            return None
        return store.iso(local)
    except ValueError:
        return None


def _article(page, year, roster):
    if (
        page.get("missing")
        or page.get("ns") != 0
        or len(page.get("revisions", [])) != 1
    ):
        return
    rev = page["revisions"][0]
    slot = rev["slots"]["main"]
    if (
        slot.get("contentmodel") != "wikitext"
        or type(rev["revid"]) is not int
        or rev["revid"] <= 0
    ):
        return
    value = re.sub(r"<!--.*?-->", "", slot["content"], flags=re.S)
    sections = list(_SECTION.finditer(value))
    if len(sections) != 1:
        return
    section = sections[0]
    end = re.search(
        r"^={2," + str(len(section[1])) + r"}[^=].*?=+[ \t]*$",
        value[section.end() :],
        re.M,
    )
    body = value[section.end() : section.end() + end.start() if end else None]
    title = page["title"]
    club = _club(title.split(" ", 1)[1].removesuffix(" season"))
    published = store.iso(rev["timestamp"])
    attribution = {
        "title": title,
        "revision": rev["revid"],
        "published_at": published,
        "url": f"https://en.wikipedia.org/w/index.php?oldid={rev['revid']}",
    }
    for box in _BOX.finditer(body):
        fields = {}
        for key, v in _FIELDS.findall(box[1]):
            k = key.lower()
            if k in fields:
                fields[k] = ""  # an ambiguous field fails validation
            else:
                fields[k] = v.strip()
        home, away = (_club(fields.get(k, "")) for k in ("team1", "team2"))
        if (
            home == away
            or club not in (home, away)
            or home not in roster
            or away not in roster
        ):
            continue
        round_text = _plain(fields.get("round", ""))
        rnd = (
            int(round_text)
            if re.fullmatch(r"(?:[1-9]|[12]\d|3[0-8])", round_text)
            else None
        )
        kickoff = _kickoff(fields.get("date", ""), fields.get("time", ""))
        if kickoff and store.instant(kickoff).year not in {year, year + 1}:
            kickoff = None
        report = _REPORT.fullmatch(fields.get("report", "").strip("[] "))
        match_id = "premierleague:" + report[1] if report else None
        result = fields.get("result", "").strip().upper()
        pair = re.fullmatch(r"(\d{1,2})\s*[–\-]\s*(\d{1,2})", fields.get("score", ""))
        score, status = None, "unfinished"
        reported_score = (
            list(map(int, pair.groups()))
            if pair and result in {"W", "D", "L"}
            else None
        )
        if result in {"W", "D", "L"}:
            status = "unverified"
            if (
                reported_score
                and kickoff
                and rnd
                and match_id
                and not fields.get("status")
            ):
                h, a = reported_score
                expected = (
                    "D" if h == a else ("W" if (h > a) == (club == home) else "L")
                )
                if result != expected:
                    status = "conflict"
                elif store.instant(published) >= store.instant(kickoff) + timedelta(
                    minutes=90
                ):
                    score, status = reported_score, "final"
        if fields.get("status"):
            marker = fields["status"].lower().strip()
            status = (
                "withdrawn"
                if marker in {"postponed", "abandoned", "canceled", "cancelled", "void"}
                else "unverified"
            )
            score = None
        yield (
            f"epl/{year}-{(year + 1) % 100:02d}/{home}/{away}",
            {
                "club": club,
                "home": home,
                "away": away,
                "round": rnd,
                "kickoff": kickoff,
                "score": score,
                "reported_score": reported_score,
                "status": status,
                "source_match_id": match_id,
                "revision": attribution,
            },
        )


@lru_cache(maxsize=4)
def _decode(raw, titles, year):
    pages = _query(raw, titles)
    roster = {_club(title.split(" ", 1)[1].removesuffix(" season")) for title in titles}
    rows = defaultdict(list)
    for page in pages:
        for fid, outcome in _article(page, year, roster):
            rows[fid].append(outcome)
    claims = {}
    by_match = defaultdict(set)
    all_match_links = defaultdict(set)
    for fid, sources in rows.items():
        for entry in sources:
            if entry["source_match_id"]:
                all_match_links[entry["source_match_id"]].add(fid)
        # A link copied into a future, unplayed placeholder is not a claim of
        # completed match identity. Duplicate *final* match IDs hold both results.
        ids = {
            s["source_match_id"]
            for s in sources
            if s["source_match_id"] and s["status"] == "final"
        }
        for mid in ids:
            by_match[mid].add(fid)
        # Conflicting concrete facts hold a result. A source with an unknown
        # timezone or an unplayed placeholder cannot assert a contrary fact.
        asserted = [s for s in sources if s["status"] != "unfinished"]
        finals = [s for s in asserted if s["status"] == "final"]
        exemplar = (finals or asserted or sources)[0]
        claim = {
            k: exemplar[k]
            for k in (
                "home",
                "away",
                "round",
                "kickoff",
                "score",
                "status",
                "source_match_id",
            )
        }
        contradictory = len({s["club"] for s in sources}) != len(sources)
        comparison = asserted or sources
        for field in ("round", "kickoff", "reported_score", "source_match_id"):
            facts = [s[field] for s in comparison if s[field] is not None]
            contradictory |= any(value != facts[0] for value in facts[1:])
        if contradictory or any(s["status"] == "conflict" for s in asserted):
            claim.update(status="conflict" if asserted else "unverified", score=None)
        elif any(s["status"] == "withdrawn" for s in asserted):
            claim.update(status="withdrawn", score=None)
        refs = sorted((s["revision"] for s in sources), key=lambda r: r["title"])
        claims[fid] = claim | {
            "revisions": refs,
            "supporting_articles": sorted(s["revision"]["title"] for s in finals),
            "url": exemplar["revision"]["url"],
            "source_id": SOURCE,
        }
    for fixtures in by_match.values():
        if len(fixtures) > 1:
            for fid in fixtures:
                claims[fid].update(status="conflict", score=None)
    # An unfinished report may corroborate native scheduling only with an
    # unambiguous event link. Copied links never confer that authority.
    for item in claims.values():
        if (
            item["status"] == "unfinished"
            and len(all_match_links[item["source_match_id"]]) > 1
        ):
            item.update(status="unverified", source_match_id=None)
    return claims


def verified(root, db, year, at):
    """Replay original receipt bytes up to the requested knowledge time, preserving retractions."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='epl_wiki_receipts'"
    ).fetchone():
        return {}
    events = defaultdict(list)
    sql = """SELECT r.*,o.metadata FROM epl_wiki_receipts r LEFT JOIN observations o
             ON o.source=? AND o.observed_at=r.received AND o.sha256=r.sha256
             WHERE r.year=? AND r.received<=? ORDER BY r.received"""
    prefix = f"{year}–{(year + 1) % 100:02d} "
    for rx in db.execute(sql, (SOURCE, year, store.iso(at))):
        seen, sha = rx["received"], rx["sha256"]
        try:
            titles = tuple(json.loads(rx["titles"]))
            meta = json.loads(rx["metadata"])
            if (
                len(set(titles)) != 20
                or any(
                    not t.startswith(prefix) or not t.endswith(" season")
                    for t in titles
                )
                or meta["license"] != LICENSE
                or meta["url"] != rx["url"]
                or rx["url"] != _url(titles)
                or store.instant(rx["started"]) > store.instant(seen)
            ):
                raise ValueError("invalid Wikimedia receipt identity")
            raw = store.read_blob(root, sha)
            if hashlib.sha256(raw).hexdigest() != sha:
                raise ValueError("Wikimedia receipt digest mismatch")
            current = _decode(raw, titles, year)
            lost = "withdrawn"
        except (OSError, ValueError, KeyError, TypeError):
            current, lost = {}, "conflict"
        for fid in current.keys() | events.keys():
            previous = events[fid][-1] if events[fid] else None
            if fid in current:
                claim = current[fid].copy()
                if any(
                    store.instant(r["published_at"]) > store.instant(seen)
                    for r in claim["revisions"]
                ):
                    claim.update(status="conflict", score=None)
                # A previously scoring club retracting its entire match box is as
                # material as an explicit score withdrawal, even if its opponent lags.
                if (
                    previous
                    and previous.get("articles_with_final")
                    and claim["status"] == "final"
                ):
                    now_titles = set(claim["supporting_articles"])
                    if set(previous["articles_with_final"]) - now_titles:
                        claim.update(status="conflict", score=None)
            else:
                claim = previous | {"status": lost, "score": None, "revisions": []}
            if claim["status"] == "final":
                claim["articles_with_final"] = claim["supporting_articles"]
            elif previous:
                claim["articles_with_final"] = previous.get("articles_with_final", [])
            events[fid].append(
                claim | {"received": seen, "started": rx["started"], "sha256": sha}
            )
    return dict(events)


def sync(root, year, fixtures, at=None):
    """One batched league request, a cross-process lease and provider-controlled retry delays."""
    now = store.instant(at or store.now_utc())
    timestamp = store.iso(now)
    participants = {f[side] for f in fixtures for side in ("home", "away")}
    if len(participants) != 20:
        return {"source": SOURCE, "status": "awaiting_complete_club_roster"}
    outstanding = [
        f
        for f in fixtures
        if f["home_goals"] is None
        and timedelta(minutes=90)
        <= now - store.instant(f["kickoff"])
        <= timedelta(days=28)
    ]
    hot = any(
        now - store.instant(f["kickoff"]) <= timedelta(hours=48, minutes=90)
        for f in outstanding
    )
    delay = timedelta(minutes=30) if hot else timedelta(hours=6 if outstanding else 24)
    with closing(store.connect(root)) as db:
        dbscope.prepare(db, "independent-results", _SCHEMA)
        with db:
            db.execute("BEGIN IMMEDIATE")
            check = db.execute(
                "SELECT * FROM epl_wiki_checks WHERE year=?", (year,)
            ).fetchone()
            if (
                check
                and check["status"] != "retrieved"
                and now < store.instant(check["next_check"])
            ):
                return {
                    "source": SOURCE,
                    "status": check["status"],
                    "retry_at": check["next_check"],
                }
            previous = db.execute(
                "SELECT received FROM epl_wiki_receipts WHERE year=? ORDER BY received DESC LIMIT 1",
                (year,),
            ).fetchone()
            if previous:
                since = now - store.instant(previous[0])
                if outstanding and since < delay and delay > timedelta(minutes=30):
                    events = verified(root, db, year, timestamp)
                    for f in outstanding:
                        series = events.get(f["fixture_id"], [])
                        if not series or series[-1]["status"] != "final":
                            continue
                        last = series[-1]
                        same_since = last["received"]
                        for old in reversed(series[:-1]):
                            if any(
                                old[k] != last[k]
                                for k in (
                                    "score",
                                    "round",
                                    "kickoff",
                                    "source_match_id",
                                    "status",
                                )
                            ):
                                break
                            same_since = old["received"]
                        if store.instant(last["started"]) - store.instant(
                            same_since
                        ) < timedelta(minutes=30):
                            delay = timedelta(minutes=30)
                            break
                if since < delay:
                    return {
                        "source": SOURCE,
                        "status": "cached",
                        "received": previous[0],
                        "next_check": store.iso(store.instant(previous[0]) + delay),
                    }
            db.execute(
                "INSERT OR REPLACE INTO epl_wiki_checks VALUES(?,?,?,?)",
                (year, timestamp, store.iso(now + timedelta(minutes=5)), "in_progress"),
            )
    try:
        response = _receive(root, year, participants)
        next_check = store.iso(store.instant(response["received"]) + delay)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        retry = timedelta(minutes=30)
        if isinstance(exc, HTTPError):
            after = exc.headers.get("Retry-After", "")
            if after.isdigit():
                retry = max(retry, timedelta(seconds=int(after)))
            elif after:
                try:
                    retry = max(retry, parsedate_to_datetime(after) - now)
                except (ValueError, TypeError, OverflowError):
                    pass
        next_check = store.iso(now + retry)
        response = {
            "source": SOURCE,
            "status": "unavailable",
            "error": str(exc),
            "retry_at": next_check,
        }
    with closing(store.connect(root)) as db, db:
        db.execute(
            "UPDATE epl_wiki_checks SET next_check=?, status=? WHERE year=? AND attempted=?",
            (next_check, response["status"], year, timestamp),
        )
    return response
