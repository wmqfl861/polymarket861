"""Rebuild the publication-time panel from immutable English Wikipedia revisions."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from polymarket861 import (
    football,
    independent_results as wiki,
    news_roster,
    news_sources,
    store,
)

SEASON = "2025-26"
ROUNDS = (1, 2, 3)
SHIRTS = (1, 4, 9, 10)
PLAN = {
    "season": SEASON,
    "official_rounds": list(ROUNDS),
    "horizon": "T-60m",
    "expected_fixtures": 30,
    "result_availability_assumption_hours": 24,
    "club_source": "English Wikipedia club season review/news prose",
    "club_window": "new since prior EPL kickoff or seven days, whichever is later",
    "biography_source": "separate sensitivity: historic squad shirts 1,4,9,10, club-career prose",
    "biography_window": "new since prior EPL kickoff or fourteen days, whichever is later",
    "biography_date": "explicit dated prose or a publication date cited in that same season-specific paragraph",
    "authority": "secondary",
    "membership": "all fixtures, including zero news, by season and official round alone",
    "eligibility": "contemporaneously dated prose with a parent-verified newly introduced sentence",
}
PANELS = ("club", "club_and_selected_biographies")
_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-Þ0-9])")


def cohort(fixtures):
    matches = sorted(
        (f for f in fixtures if f.season == SEASON and f.round in ROUNDS),
        key=lambda f: (f.kickoff, f.fixture_id),
    )
    if (
        any(sum(f.round == rnd for f in matches) != 10 for rnd in ROUNDS)
        or any(f.kickoff_precision != "minute" for f in matches)
        or len({f.fixture_id for f in matches}) != 30
    ):
        raise ValueError("the fixed panel requires thirty minute-precision fixtures")
    return matches


def text_sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Archive:
    """Retain original HTTP bytes and acquisition timestamps outside the published report."""

    def __init__(self, root, *, offline=False):
        self.root = Path(root) / "raw"
        self.root.mkdir(parents=True, exist_ok=True)
        self.offline = offline

    @staticmethod
    def _write(path, content):
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            saved = Path(stream.name)
            try:
                stream.write(content)
            except BaseException:
                saved.unlink(missing_ok=True)
                raise
        try:
            saved.replace(path)
        finally:
            saved.unlink(missing_ok=True)

    def _load(self, key, params):
        metadata = self.root / (key + ".json")
        url = wiki.API + "?" + urlencode(params)
        if metadata.exists():
            receipt = json.loads(metadata.read_bytes())
            actual = urlsplit(receipt["request_url"])
            expected = urlsplit(url)
            if (actual.scheme, actual.netloc, actual.path) != (
                expected.scheme,
                expected.netloc,
                expected.path,
            ) or parse_qs(actual.query) != parse_qs(expected.query):
                raise ValueError("archived Wikimedia request selectors changed")
            path = self.root / (receipt["sha256"] + ".body")
            if not path.exists():
                path = self.root / (key + ".source")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
                raise ValueError("archived Wikimedia response changed")
            return raw, receipt
        if self.offline:
            raise ValueError(f"missing cached Wikimedia response {key}")
        started = store.now_utc()
        raw = wiki._get(url)
        reply = {
            "request_url": url,
            "fetch_started_at": started,
            "received_at": store.now_utc(),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        if "query" not in json.loads(raw):
            raise ValueError("Wikimedia revision unavailable")
        self._write(self.root / (reply["sha256"] + ".body"), raw)
        self._write(metadata, json.dumps(reply).encode())
        return raw, reply

    def get(self, title, *, at=None, oldid=None):
        if (at is None) == (oldid is None):
            raise ValueError("supply a historical deadline or an exact oldid")
        identity = (title, store.iso(at) if at is not None else int(oldid))
        key = hashlib.sha256(repr(identity).encode()).hexdigest()
        params = {
            "action": "query",
            "format": "json",
            "formatversion": 2,
            "prop": "revisions",
            "rvprop": "ids|timestamp|content",
            "rvslots": "main",
            "meta": "siteinfo",
            "siprop": "rightsinfo",
            "maxlag": 5,
        }
        if at is not None:
            params.update(
                titles=title, rvstart=store.iso(at)[:19] + "Z", rvdir="older", rvlimit=1
            )
        else:
            params["revids"] = str(int(oldid))
        raw, receipt = self._load(key, params)
        (page,) = wiki._query(raw, [title])
        versions = page.get("revisions", [])
        if len(versions) != 1:
            raise ValueError("missing historical revision")
        rev = versions[0]
        pub = store.iso(rev["timestamp"])
        ident = rev["revid"]
        if (
            rev["slots"]["main"].get("contentmodel") != "wikitext"
            or type(ident) is not int
            or ident <= 0
            or type(rev["parentid"]) is not int
            or (oldid is not None and ident != oldid)
            or (at is not None and store.instant(pub) > store.instant(at))
            or pub > store.iso(receipt["received_at"])
        ):
            raise ValueError("Wikimedia revision identity or publication time mismatch")
        body = rev["slots"]["main"]["content"]
        return {
            "title": title,
            "id": ident,
            "parentid": rev["parentid"],
            "published_at": pub,
            "url": f"https://en.wikipedia.org/w/index.php?oldid={ident}",
            "sha256": text_sha(body),
            "received_at": receipt["received_at"],
            "http_sha256": receipt["sha256"],
            "text": body,
            "raw": raw,
        }


def public_receipt(page):
    return {k: v for k, v in page.items() if k not in ("raw", "text")}


def paragraphs(raw, kind):
    if kind == "biography":
        # Reuse live source's prose filters on the player's club-career section only.
        section = re.search(
            r"^==\s*Club career\s*==\s*$([\s\S]*?)(?=^==[^=]|\Z)", raw, re.I | re.M
        )
        raw = "==Review==\n" + section[1] if section else ""
    return list(news_sources.wiki_prose(raw).values())


def story_origin(archive, page, sentence, kind, start):
    """A later revision that edited an unrelated paragraph cannot renew the news timestamp."""
    for _ in range(200):
        if store.instant(page["published_at"]) <= start:
            return None
        if page["parentid"] == 0:
            return page, None
        previous = archive.get(page["title"], oldid=page["parentid"])
        if (
            previous["id"] >= page["id"]
            or previous["published_at"] > page["published_at"]
        ):
            raise ValueError("broken Wikimedia parent chain")
        if not any(sentence in line for line in paragraphs(previous["text"], kind)):
            return page, previous
        page = previous
    return None


def historic_squad(page, team):
    _, _, links, unlinked = news_roster.parse_squad(
        page["raw"], page["title"], team, page["received_at"]
    )
    players = {
        title: {
            "player_id": "enwiki-title:" + title,
            "name": news_roster._name(title),
            "aliases": sorted(set(aliases) | {news_roster._name(title)}),
        }
        for title, aliases in sorted(links.items())
    }
    # Numbers select the separate biography diagnostic; they are not playing positions.
    plain = re.sub(
        r"<!--.*?-->|<noinclude\b[^>]*>.*?</noinclude\s*>",
        "",
        page["text"],
        flags=re.I | re.S,
    )
    shirts = {}
    for row in re.finditer(
        r"\{\{\s*football squad2 player\s*\|([^\n]*?)\}\}", plain, re.I
    ):
        number = re.search(
            r"(?:^|\|)\s*(?:no|num|number)\s*=\s*(\d+)\s*(?=\||$)", row[1], re.I
        )
        name = re.search(r"(?:^|\|)\s*name\s*=\s*\[\[([^\[\]|]+)", row[1], re.I)
        if number and name and name[1] in players and int(number[1]) in SHIRTS:
            shirts.setdefault(int(number[1]), set()).add(name[1])
    return (
        list(players.values()),
        sorted(next(iter(names)) for names in shirts.values() if len(names) == 1),
        unlinked,
    )


def _event_date(match):
    month = news_sources._MONTHS.index(match[2].capitalize()) + 1
    year = int(match[3]) if match[3] else 2025 + (month < 7)
    return date(year, month, int(match[1]))


def _calendar_evidence(page, sentence, kind, start, cut):
    """An old biographical event must not inherit the date of an unrelated new edit or access-date."""
    stamp = store.instant(page["published_at"]).date()
    dated = list(news_sources._DATE.finditer(sentence))
    if (
        dated
        and (kind == "club" or all(m[3] for m in dated))
        and news_sources._recent_passage(sentence, page["published_at"], SEASON)
    ):
        dates = [_event_date(m) for m in dated]
        if all(start.date() <= day <= min(cut.date(), stamp) for day in dates):
            return {
                "basis": "narrative_event_date",
                "dates": sorted({d.isoformat() for d in dates}),
            }
    if kind != "biography" or dated:
        return None
    # Some biographical reports say “upcoming season” and date the news in an inline reference.
    # Extract only citation publication dates from the archived wikitext; never fetch article text.
    hierarchy = {}
    for line in page["text"].splitlines():
        heading = news_sources._HEADINGS.fullmatch(line.strip())
        if heading:
            level = len(heading[1])
            hierarchy = {n: text for n, text in hierarchy.items() if n < level}
            hierarchy[level] = heading[2].strip()
            continue
        if (
            hierarchy.get(2, "").casefold() != "club career"
            or not re.search(r"2025[–-](?:20)?26", " ".join(hierarchy.values()) + line)
            or "<ref" not in line
        ):
            continue
        clean = news_sources.wiki_prose("==Review==\n" + line)
        if not any(sentence in para for para in clean.values()):
            continue
        citations = re.findall(r"<ref\b[^>]*>.*?</ref\s*>", line, re.I | re.S)
        dates = []
        for citation in citations:
            for value in re.findall(r"\|\s*date\s*=\s*([^|}<>]+)", citation, re.I):
                for fmt in ("%d %B %Y", "%Y-%m-%d", "%B %d, %Y"):
                    try:
                        dates.append(datetime.strptime(value.strip(), fmt).date())
                        break
                    except ValueError:
                        continue
        if dates and all(start.date() <= d <= min(stamp, cut.date()) for d in dates):
            return {
                "basis": "cited_publication_dates_in_season_paragraph",
                "dates": sorted({d.isoformat() for d in dates}),
            }
    return None


def source_stories(archive, title, kind, cut, start, proofs):
    closing = archive.get(title, at=cut)
    proofs[closing["id"]] = public_receipt(closing)
    ref = {"closing": closing["id"], "kind": kind, "title": title}
    if store.instant(closing["published_at"]) <= start:
        return ref, []
    opening = archive.get(title, at=start) if kind == "club" else None
    if opening:
        proofs[opening["id"]] = public_receipt(opening)
        ref["opening"] = opening["id"]
    old = paragraphs(opening["text"], kind) if opening else []
    stories = []
    for paragraph in paragraphs(closing["text"], kind):
        for index, sentence in enumerate(_BOUNDARY.split(paragraph)):
            if (
                len(sentence) > 1600
                or not news_sources._NEWS_WORDS.search(sentence)
                or any(sentence in line for line in old)
            ):
                continue
            if not _calendar_evidence(closing, sentence, kind, start, cut):
                continue
            introduced = story_origin(archive, closing, sentence, kind, start)
            if not introduced:
                continue
            origin, parent = introduced
            date_evidence = _calendar_evidence(origin, sentence, kind, start, cut)
            if not date_evidence:
                continue
            if parent:
                proofs[parent["id"]] = public_receipt(parent)
            proofs[origin["id"]] = public_receipt(origin)
            stories.append(
                {
                    "origin": origin["id"],
                    "previous": origin["parentid"],
                    "text": sentence,
                    "known_from": kind,
                    "date_evidence": date_evidence,
                    "sentence_sha256": text_sha(sentence),
                    "closing_paragraph_sha256": text_sha(paragraph),
                    "sentence_index": index,
                }
            )
    return ref, stories


def club_input(archive, fixture, team, previous):
    year = football.season_year(SEASON)
    cut = fixture.kickoff - timedelta(hours=1)
    provenance, errors, refs, stories = {}, [], [], []
    title = news_roster._title(team, year)
    roster, biographies, unlinked, squad = [], [], None, None
    try:
        squad = archive.get(title, at=cut)
        provenance[squad["id"]] = public_receipt(squad)
        roster, biographies, unlinked = historic_squad(squad, team)
    except (ValueError, OSError, KeyError) as exc:
        errors.append({"title": title, "reason": str(exc)})
    for kind, name in [("club", wiki._title(year, team))] + [
        ("biography", n) for n in biographies
    ]:
        start = cut - timedelta(days=7 if kind == "club" else 14)
        if previous:
            start = max(start, previous)
        try:
            ref, found = source_stories(archive, name, kind, cut, start, provenance)
            refs.append(ref)
            stories.extend(found)
        except (ValueError, OSError, KeyError) as exc:
            errors.append({"title": name, "reason": str(exc)})
    return {
        "fixture_id": fixture.fixture_id,
        "team": team,
        "cutoff": store.iso(cut),
        "roster_revision": squad["id"] if squad else None,
        "roster_count": len(roster),
        "unlinked_squad_rows": unlinked,
        "selected_biography_count": len(biographies),
        "roster": roster if stories else [],
        "sources": refs,
        "stories": stories,
        "gaps": errors,
    }, provenance


def capture(fixtures, cache, workers=2, *, offline=False):
    if not 1 <= workers <= 3:
        raise ValueError("use one to three Wikimedia workers")
    dates, requests = {}, []
    for fixture in cohort(fixtures):
        for team in (fixture.home, fixture.away):
            requests.append((fixture, team, dates.get(team)))
            dates[team] = fixture.kickoff
    archive = Archive(cache, offline=offline)
    snapshots, revisions = [], {}

    def one(args):
        return club_input(archive, *args)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for side, receipts in pool.map(one, requests):
            snapshots.append(side)
            for key, receipt in receipts.items():
                if key in revisions and revisions[key]["sha256"] != receipt["sha256"]:
                    raise ValueError(
                        "different bodies returned for one Wikimedia revision"
                    )
                if (
                    key not in revisions
                    or receipt["received_at"] < revisions[key]["received_at"]
                ):
                    revisions[key] = receipt
    return {
        "plan": PLAN,
        "attribution": "English Wikipedia contributors",
        "license": wiki.LICENSE,
        "revisions": {str(k): v for k, v in sorted(revisions.items())},
        "snapshot": snapshots,
    }
