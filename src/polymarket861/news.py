"""Collect free team bulletins, retain their revisions and reconstruct pre-match news."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import date, timedelta
from time import monotonic
from urllib.parse import urlsplit

from . import dbscope, store
from .football import team_key


# One team report is attached to at most the next registered league fixture.
# A new fixture or an expired report needs fresh information.
MAX_AGE = timedelta(days=7)
COVERAGE_AGE = timedelta(hours=6)
POLL_INTERVAL = timedelta(minutes=20)
LEASE = timedelta(minutes=8)
RESEARCH_SOURCE = "epl-team-news"
MAX_BATCH = 100
MAX_TEXT = 180_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_feed_config (
 id TEXT PRIMARY KEY,team_key TEXT NOT NULL,config TEXT NOT NULL,registered_at TEXT NOT NULL,
 disabled_at TEXT);
CREATE TABLE IF NOT EXISTS news_streams (
 season TEXT NOT NULL,team_key TEXT NOT NULL,source TEXT NOT NULL,team TEXT NOT NULL,
 config TEXT NOT NULL,registered_at TEXT NOT NULL,next_at TEXT NOT NULL DEFAULT '',
 started TEXT NOT NULL DEFAULT '',owner TEXT,lease_until TEXT,cursor TEXT,
 provider_after TEXT NOT NULL DEFAULT '',failures INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(season,team_key,source));
CREATE TABLE IF NOT EXISTS news_local_work (
 season TEXT NOT NULL,team_key TEXT NOT NULL,source TEXT NOT NULL,policy TEXT NOT NULL,
 retry_at TEXT,PRIMARY KEY(season,team_key,source));
CREATE TABLE IF NOT EXISTS news_checks (
 id INTEGER PRIMARY KEY,season TEXT NOT NULL,team_key TEXT NOT NULL,source TEXT NOT NULL,
 fixture_id TEXT NOT NULL,kickoff TEXT NOT NULL,checked_at TEXT NOT NULL,state TEXT NOT NULL,
 detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS news_checks_fixture ON news_checks(fixture_id,kickoff,team_key,source,checked_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS news_checks_stream ON news_checks(season,team_key,source,checked_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS news_rejected_items (
 id TEXT PRIMARY KEY,season TEXT NOT NULL,team_key TEXT NOT NULL,source TEXT NOT NULL,
 external_id TEXT,revision TEXT,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,
 original_sha256 TEXT NOT NULL,reason TEXT NOT NULL,response_receipt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS news_articles (
 seq INTEGER PRIMARY KEY,season TEXT NOT NULL,team_key TEXT NOT NULL,source TEXT NOT NULL,
 external_id TEXT NOT NULL,revision TEXT NOT NULL,updated_at TEXT NOT NULL,
 published_at TEXT NOT NULL,received_at TEXT NOT NULL,recorded_at TEXT NOT NULL,url TEXT NOT NULL,
 original_sha256 TEXT NOT NULL,fixture_id TEXT,kickoff TEXT,retracted INTEGER NOT NULL,
 UNIQUE(season,team_key,source,external_id,revision));
CREATE INDEX IF NOT EXISTS news_latest_revision ON news_articles(
 season,team_key,source,external_id,updated_at DESC,seq DESC);
CREATE INDEX IF NOT EXISTS news_articles_fixture ON news_articles(fixture_id,kickoff,recorded_at);
CREATE TABLE IF NOT EXISTS news_poll_proofs (
 id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS news_feed_inbox (
 seq INTEGER PRIMARY KEY,season TEXT NOT NULL,team_key TEXT NOT NULL,source TEXT NOT NULL,
 receipt TEXT NOT NULL REFERENCES news_poll_proofs(id),item_index INTEGER NOT NULL,
 received_at TEXT NOT NULL,payload TEXT NOT NULL,UNIQUE(receipt,item_index));
CREATE INDEX IF NOT EXISTS news_inbox_stream ON news_feed_inbox(season,team_key,source,seq);
CREATE TABLE IF NOT EXISTS news_article_proofs (
 article INTEGER PRIMARY KEY REFERENCES news_articles(seq),receipt TEXT NOT NULL REFERENCES news_poll_proofs(id));
CREATE TABLE IF NOT EXISTS news_revision_conflicts (
 article INTEGER NOT NULL REFERENCES news_articles(seq),other_sha256 TEXT NOT NULL,recorded_at TEXT NOT NULL,
 PRIMARY KEY(article,other_sha256));
CREATE TABLE IF NOT EXISTS news_publisher_conflicts (
 season TEXT NOT NULL,team_key TEXT NOT NULL,source TEXT NOT NULL,external_id TEXT NOT NULL,
 selector_kind TEXT NOT NULL,selector TEXT NOT NULL,recorded_at TEXT NOT NULL,
 receipt TEXT NOT NULL REFERENCES news_poll_proofs(id),reason TEXT NOT NULL,
 fixture_id TEXT NOT NULL,kickoff TEXT NOT NULL,
 PRIMARY KEY(season,team_key,source,external_id,selector_kind,selector));
CREATE TABLE IF NOT EXISTS news_extractions (
 article INTEGER PRIMARY KEY REFERENCES news_articles(seq),known_at TEXT NOT NULL,
 model_version TEXT NOT NULL,roster_sha256 TEXT NOT NULL,claim_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS news_claims (
 id TEXT PRIMARY KEY,article INTEGER NOT NULL REFERENCES news_articles(seq),payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS news_claims_article ON news_claims(article);
CREATE TABLE IF NOT EXISTS news_analysis_runs (
 seq INTEGER PRIMARY KEY,article INTEGER NOT NULL REFERENCES news_articles(seq),policy TEXT NOT NULL,
 known_at TEXT NOT NULL,roster_sha256 TEXT NOT NULL,claim_count INTEGER NOT NULL,metadata TEXT NOT NULL,
 retry_at TEXT);
CREATE INDEX IF NOT EXISTS news_analysis_asof ON news_analysis_runs(article,known_at DESC,seq DESC);
CREATE TABLE IF NOT EXISTS news_claim_runs (
 claim TEXT PRIMARY KEY REFERENCES news_claims(id),run INTEGER NOT NULL REFERENCES news_analysis_runs(seq));
"""


@contextmanager
def _db(root, *, write=False):
    with closing(store.connect(root)) as db:
        dbscope.prepare(db, "team_news", _SCHEMA)
        with db:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db


def _utc():
    return store.iso(store.now_utc())


def _plus(at, delta):
    return store.iso(store.instant(at) + delta)


def _key(task):
    return task["season"], task["team_key"], task["source"]


def _owns(db, task):
    row = db.execute(
        "SELECT owner,lease_until FROM news_streams WHERE season=? AND team_key=? AND source=?",
        _key(task),
    ).fetchone()
    return bool(row and row["owner"] == task["owner"] and row["lease_until"] > _utc())


def add_feed(data_dir, spec):
    from . import news_sources

    config = news_sources.validate_source(spec)
    if config["kind"] != "feed":
        raise ValueError("add-feed requires an authorized feed")
    raw = store.canonical(config)
    with _db(data_dir, write=True) as db:
        old = db.execute(
            "SELECT * FROM news_feed_config WHERE id=?", (config["id"],)
        ).fetchone()
        if old and (old["config"] != raw or old["disabled_at"] is not None):
            raise ValueError(
                "feed identity is immutable; register changed terms under a new id"
            )
        db.execute(
            "INSERT OR IGNORE INTO news_feed_config VALUES (?,?,?,?,NULL)",
            (config["id"], team_key(config["team"]), raw, _utc()),
        )
    return config


def disable_feed(data_dir, ident):
    with _db(data_dir, write=True) as db:
        known = db.execute(
            "SELECT disabled_at FROM news_feed_config WHERE id=?", (ident,)
        ).fetchone()
        if not known:
            raise ValueError("unknown configured feed")
        at = known["disabled_at"] or _utc()
        db.execute("UPDATE news_feed_config SET disabled_at=? WHERE id=?", (at, ident))
        db.execute(
            "UPDATE news_streams SET owner=NULL,lease_until=NULL WHERE source=?",
            (ident,),
        )
    return {"id": ident, "disabled_at": at}


def _schedule(db, at, season=None):
    sql = """WITH known AS (
      SELECT f.*,r.precision,row_number() OVER(PARTITION BY f.fixture_id ORDER BY f.known_at DESC) AS n
      FROM fixture_versions f JOIN schedule_receipts r USING(fixture_id,version)
      WHERE f.known_at<=? AND r.recorded_at<=?"""
    args = [at, at]
    if season:
        sql += " AND f.season=?"
        args.append(season)
    rows = db.execute(
        sql
        + """
      ) SELECT fixture_id,season,home,away,kickoff,precision FROM known WHERE n=1
      ORDER BY kickoff,fixture_id""",
        args,
    )
    return [dict(row) for row in rows]


def _timetables(fixtures):
    teams = defaultdict(list)
    for game in fixtures:
        for side in ("home", "away"):
            teams[game["season"], team_key(game[side])].append(game)
    return teams


def _claim(data_dir, limit, days, only_team, season, force, defaults, settings_id):
    at = _utc()
    until = _plus(at, timedelta(days=days))
    key_filter = team_key(only_team) if only_team else None
    with _db(data_dir, write=True) as db:
        tables = _timetables(_schedule(db, at, season))
        registered = defaultdict(list)
        for row in db.execute(
            "SELECT * FROM news_feed_config WHERE disabled_at IS NULL ORDER BY id"
        ):
            registered[row["team_key"]].append(json.loads(row["config"]))
        tasks = []
        for (year, club), games in tables.items():
            if key_filter and club != key_filter:
                continue
            target = next((g for g in games if at < g["kickoff"] <= until), None)
            if not target or target["precision"] == "date":
                continue
            name = next(
                target[s] for s in ("home", "away") if team_key(target[s]) == club
            )
            for config in (*defaults(name, year), *registered.get(club, ())):
                ident = config["id"]
                # The configured stream is local, and may never impersonate the default publisher.
                key = (year, club, ident)
                serialized = store.canonical(config)
                db.execute(
                    """INSERT OR IGNORE INTO news_streams
                    (season,team_key,source,team,config,registered_at) VALUES (?,?,?,?,?,?)""",
                    (*key, name, serialized, at),
                )
                row = db.execute(
                    "SELECT * FROM news_streams WHERE season=? AND team_key=? AND source=?",
                    key,
                ).fetchone()
                if row["config"] != serialized:
                    raise ValueError("news source id collides with an existing source")
                local = db.execute(
                    "SELECT policy,retry_at FROM news_local_work WHERE season=? AND team_key=? AND source=?",
                    key,
                ).fetchone()
                network_due = (force or row["next_at"] <= at) and row[
                    "provider_after"
                ] <= at
                local_due = local and (
                    local["policy"] != settings_id
                    or (local["retry_at"] and (force or local["retry_at"] <= at))
                )
                tasks.append((row, target, games, network_due, local_due))
        due = sorted(
            (
                t
                for t in tasks
                if (t[3] or t[4]) and (not t[0]["owner"] or t[0]["lease_until"] <= at)
            ),
            key=lambda t: (
                t[0]["started"],
                t[1]["kickoff"],
                t[0]["team_key"],
                t[0]["source"],
            ),
        )
        claimed = []
        for row, target, games, network_due, _ in due[:limit]:
            owner = uuid.uuid4().hex
            db.execute(
                """UPDATE news_streams SET owner=?,lease_until=?,started=?
                WHERE season=? AND team_key=? AND source=?""",
                (owner, _plus(at, LEASE), at, *_key(row)),
            )
            claimed.append(
                dict(row)
                | {
                    "owner": owner,
                    "config": json.loads(row["config"]),
                    "target": target,
                    "games": games,
                    "network_due": bool(network_due),
                    "settings_id": settings_id,
                }
            )
    return claimed, len(tasks), len(due)


def _nonempty(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("invalid news " + name)
    return value


def _document(item, source, received):
    if not isinstance(item, dict):
        raise ValueError("invalid news article")
    deleted = item.get("retracted", False)
    if not isinstance(deleted, bool):
        raise ValueError("invalid news withdrawal flag")
    external = _nonempty(item.get("external_id"), "external id", 2048)
    version = item.get("revision")
    if type(version) not in (int, str):
        raise ValueError("news article requires a publisher revision identity")
    revision = _nonempty(str(version), "revision", 1024)
    url = _nonempty(item.get("url"), "attribution URL", 4096)
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("invalid article attribution URL")
    published = store.iso(item["published_at"])
    revised = store.iso(item.get("updated_at") or item.get("revised_at") or published)
    state = item.get("observed_state_as_of")
    if state is not None:
        state = store.iso(state)
        if (
            source.get("kind") != "touchline"
            or item.get("time_basis") != "provider_snapshot"
            or state != revised
        ):
            raise ValueError("unsupported standing-state reaffirmation")
    if not published <= revised <= received:
        raise ValueError("publication/revision is in the future or out of order")
    text = item.get("text", "")
    if (
        not isinstance(text, str)
        or len(text) > MAX_TEXT
        or (not deleted and not text.strip())
    ):
        raise ValueError("invalid news bulletin text")
    title = item.get("title", "")
    if not isinstance(title, str) or len(title) > 1000:
        raise ValueError("invalid news headline")
    label = item.get("origin") or source["origin"]
    if not isinstance(label, str) or len(label) > 180 or not label.isprintable():
        raise ValueError("invalid article upstream origin")
    origin = " ".join(label.split()).casefold()
    # Some attributable wires supply outlet names and others give their URL.
    # These labels are never used as fetch endpoints.
    if origin.startswith(("http://", "https://")):
        origin = urlsplit(origin).hostname or source["origin"]
    origin = origin or source["origin"]
    authority = (
        source["authority"] if origin == source["origin"].casefold() else "secondary"
    )
    fields = item.get("source_fields") or {}
    if not isinstance(fields, dict) or len(store.canonical(fields)) > 16_384:
        raise ValueError("invalid licensed source-field projection")
    return {
        "external_id": external,
        "revision": revision,
        "published_at": published,
        "updated_at": revised,
        "url": url,
        "retracted": deleted,
        "title": title,
        "text": text,
        "attribution": _nonempty(
            item.get("attribution") or source.get("attribution"), "attribution", 4096
        ),
        "license": source["license"],
        "license_url": source.get("license_url"),
        "source": source["id"],
        "origin": origin,
        "cited_origin": label,
        "authority": authority,
        "observed_state_as_of": state,
        "time_basis": item.get("time_basis", "publisher_article"),
        "evidence_class": item.get("evidence_class"),
        "provider_player_id": item.get("provider_player_id"),
        "source_fields": fields,
    }


def _target_for(publication, games, now):
    # Includes intervening past league matches. A last-week bulletin cannot roll
    # over to this week's opponent simply because the machine started today.
    upcoming = next((g for g in games if g["kickoff"] > publication), None)
    if (
        upcoming
        and upcoming["precision"] != "date"
        and upcoming["kickoff"] > now
        and upcoming["kickoff"] <= _plus(publication, MAX_AGE)
    ):
        return upcoming
    return None


def _poll_proof(root, task, response, local_start, received, stage_feed):
    raw_digest = response.get("response_sha256")
    if raw_digest is not None and (
        not isinstance(raw_digest, str)
        or not re.fullmatch(r"[a-fA-F0-9]{64}", raw_digest)
    ):
        raise ValueError("invalid source response fingerprint")
    requested = store.iso(response.get("requested_at") or local_start)
    network_received = store.iso(response.get("received_at") or received)
    if not local_start <= requested <= network_received <= received:
        raise ValueError("invalid original network receipt clocks")
    proof = {
        "source": task["source"],
        "season": task["season"],
        "team": task["team_key"],
        "requested_at": requested,
        "received_at": network_received,
        "observed_at": received,
        "request_url": response.get("request_url") or task["config"].get("url"),
        "response_sha256": raw_digest or store.digest(
            {"articles": response["articles"], "revision_conflicts": response["revision_conflicts"]}
            if response.get("revision_conflicts") else response["articles"]
        ),
        "hash_basis": "upstream_response_bytes"
        if raw_digest
        else "adapter_article_projection",
    }
    ident = store.digest(proof)
    with _db(root, write=True) as db:
        if not _owns(db, task):
            raise ValueError("news collection lease expired")
        db.execute(
            "INSERT OR IGNORE INTO news_poll_proofs VALUES (?,?)",
            (ident, store.canonical(proof)),
        )
        if stage_feed:
            # A rolling feed can remove an entry before the next tick or while
            # this process restarts. Capture the authorized original projections
            # atomically with their first physical response and actual receipt.
            db.executemany(
                """INSERT OR IGNORE INTO news_feed_inbox
                (season,team_key,source,receipt,item_index,received_at,payload) VALUES (?,?,?,?,?,?,?)""",
                ((*_key(task), ident, i, received, store.canonical(item))
                 for i, item in enumerate(response["articles"])),
            )
    return ident, proof


def _matches_marker(kind, marker, revision, updated):
    if kind == "native_at":
        # A witnessed newer edition already supersedes older statements about
        # this same story, even if both variants of the new edition collided in
        # a single response before either could be archived as an article.
        return updated <= marker
    if kind == "wiki_through":
        return revision.isascii() and revision.isdecimal() and int(revision) <= int(marker)
    return kind == "revision" and revision == marker


def _provider_conflicts(root, task, conflicts, received, receipt):
    """Keep exact upstream contradictions independent of the moving provider cursor.

    Link to already captured versions in the event worker's receipt stream. If a
    collision was first seen within one response, retain its native identity so
    either variant stays isolated even if it returns alone later.
    """
    if not isinstance(conflicts, list) or len(conflicts) > 2000:
        raise ValueError("invalid publisher revision conflicts")
    prepared = []
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            raise ValueError("invalid publisher revision contradiction")
        external = _nonempty(conflict.get("external_id"), "conflicted external id", 2048)
        reason = _nonempty(conflict.get("reason"), "conflict reason", 512)
        selectors = []
        for key in ("revision", "legacy_revision"):
            value = conflict.get(key)
            if type(value) in (int, str):
                selectors.append(("revision", _nonempty(str(value), "conflicted revision", 1024)))
        if conflict.get("native_revision_at"):
            stamp = store.iso(conflict["native_revision_at"])
            if stamp > received:
                raise ValueError("publisher conflict revision is in the future")
            selectors.append(("native_at", stamp))
        through = conflict.get("revision_through")
        if through is not None:
            if task["config"].get("kind") != "wiki" or type(through) is not int or through <= 0:
                raise ValueError("invalid publisher revision boundary")
            selectors.append(("wiki_through", str(through)))
        before = conflict.get("latest_observed_before")
        if before is not None:
            before = store.iso(before)
            if task["source"] != "touchline-rumours" or before > received:
                raise ValueError("invalid scoped prior publisher observation")
            selectors.append(("observed_before", before))
        if not selectors:
            raise ValueError("missing publisher revision identity")
        prepared.append((external, reason, selectors))
    with _db(root, write=True) as db:
        if not _owns(db, task):
            raise ValueError("news collection lease expired")
        at = _utc()
        for external, reason, selectors in prepared:
            key = (*_key(task), external)
            old = db.execute(
                """SELECT seq,revision,updated_at,recorded_at FROM news_articles WHERE season=? AND team_key=?
                AND source=? AND external_id=? ORDER BY updated_at DESC,seq DESC""", key
            ).fetchall()
            for kind, marker in selectors[:]:
                if kind == "observed_before":
                    prior = next((row for row in old if row["recorded_at"] <= marker), None)
                    if prior is not None:
                        selectors.append(("revision", prior["revision"]))
            for kind, marker in selectors:
                db.execute(
                    "INSERT OR IGNORE INTO news_publisher_conflicts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (*key, kind, marker, at, receipt, reason, task["target"]["fixture_id"], task["target"]["kickoff"]),
                )
                for row in old:
                    if _matches_marker(kind, marker, row["revision"], row["updated_at"]):
                        db.execute(
                            """INSERT INTO news_revision_conflicts SELECT ?,receipt,recorded_at FROM news_publisher_conflicts
                            WHERE season=? AND team_key=? AND source=? AND external_id=? AND selector_kind=? AND selector=?
                            ON CONFLICT(article,other_sha256) DO NOTHING""", (row["seq"], *key, kind, marker)
                        )


def _open_conflicts(root, task):
    with _db(root) as db:
        return db.execute(
            """SELECT count(DISTINCT m.external_id) FROM news_publisher_conflicts m
            LEFT JOIN news_articles a ON a.seq=(SELECT n.seq FROM news_articles n WHERE n.season=m.season
                AND n.team_key=m.team_key AND n.source=m.source AND n.external_id=m.external_id
                ORDER BY n.updated_at DESC,n.seq DESC LIMIT 1)
            WHERE m.season=? AND m.team_key=? AND m.source=? AND m.fixture_id=? AND m.kickoff=?
            AND (a.seq IS NULL OR EXISTS (SELECT 1 FROM news_revision_conflicts q WHERE q.article=a.seq))""",
            (*_key(task), task["target"]["fixture_id"], task["target"]["kickoff"]),
        ).fetchone()[0]


def _native_legacy_match(root, db, key, item, obj):
    if not item.get("native_revision_at"):
        return None
    if store.iso(item["native_revision_at"]) != obj["updated_at"]:
        raise ValueError("native revision clock differs from article revision")
    rows = db.execute(
        """SELECT revision,original_sha256 FROM news_articles WHERE season=? AND team_key=? AND source=?
        AND external_id=? AND updated_at=?""", (*key, obj["updated_at"])
    ).fetchall()
    found = False
    for row in rows:
        if not re.fullmatch(r"[0-9a-f]{64}", row["revision"]):
            continue
        prior = json.loads(store.read_blob(root, row["original_sha256"]))
        if any(prior[k] != obj[k] for k in ("url", "title", "text", "published_at", "retracted")):
            return False
        found = True
    return True if found else None


def _archive(root, task, item, received, proof_id):
    obj = _document(item, task["config"], received)
    raw = store.canonical(obj).encode()
    sha = hashlib.sha256(raw).hexdigest()
    unique = (*_key(task), obj["external_id"], obj["revision"])
    with _db(root) as db:
        old = db.execute(
            """SELECT seq,original_sha256 FROM news_articles WHERE season=? AND team_key=?
            AND source=? AND external_id=? AND revision=?""",
            unique,
        ).fetchone()
        legacy_match = _native_legacy_match(root, db, unique[:-1], item, obj)
    if legacy_match is not False and ((old and old["original_sha256"] == sha) or legacy_match is True):
        return 0
    store.archive_bytes(
        root,
        "epl-team-news",
        raw,
        observed_at=received,
        metadata={"source_id": task["source"], "team": task["team_key"]},
    )
    if old or legacy_match is False:
        marker = {"external_id": obj["external_id"], "revision": obj["revision"],
                  "reason": "same news revision has different content"}
        if item.get("native_revision_at"):
            marker["native_revision_at"] = obj["updated_at"]
        _provider_conflicts(root, task, [marker], received, proof_id)
        raise ValueError("same news revision has different content; revision isolated")
    with _db(root, write=True) as db:
        if not _owns(db, task):
            raise ValueError("news collection lease expired")
        recorded = _utc()
        # A schedule may change while the provider request is in flight.
        games = _timetables(_schedule(db, recorded, task["season"])).get(
            (task["season"], task["team_key"]), []
        )
        target = _target_for(
            obj["observed_state_as_of"] or obj["published_at"], games, recorded
        )
        ins = db.execute(
            """INSERT OR IGNORE INTO news_articles
            (season,team_key,source,external_id,revision,updated_at,published_at,received_at,
             recorded_at,url,original_sha256,fixture_id,kickoff,retracted)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                *unique,
                obj["updated_at"],
                obj["published_at"],
                received,
                recorded,
                obj["url"],
                sha,
                target["fixture_id"] if target else None,
                target["kickoff"] if target else None,
                int(obj["retracted"]),
            ),
        )
        if ins.rowcount:
            db.execute(
                "INSERT INTO news_article_proofs VALUES (?,?)",
                (ins.lastrowid, proof_id),
            )
            markers = db.execute(
                """SELECT selector_kind,selector,receipt,recorded_at FROM news_publisher_conflicts
                WHERE season=? AND team_key=? AND source=? AND external_id=?""", unique[:-1]
            )
            for marker in markers:
                if _matches_marker(marker["selector_kind"], marker["selector"], obj["revision"], obj["updated_at"]):
                    db.execute("INSERT OR IGNORE INTO news_revision_conflicts VALUES (?,?,?)",
                               (ins.lastrowid, marker["receipt"], marker["recorded_at"]))
        return ins.rowcount


# Exclude an earlier state as soon as the replacement was locally recorded,
# including the time spent extracting a revised article. Re-reading an old
# version doesn't supersede a later publication.
_NEWER = """NOT EXISTS (SELECT 1 FROM news_articles n WHERE n.season=a.season
 AND n.team_key=a.team_key AND n.source=a.source AND n.external_id=a.external_id
 AND n.recorded_at<=? AND (n.updated_at>a.updated_at OR (n.updated_at=a.updated_at AND n.seq>a.seq)))
 AND NOT EXISTS (SELECT 1 FROM news_revision_conflicts v WHERE v.article=a.seq AND v.recorded_at<=?)"""


_CUP_CONTEXT = re.compile(
    r"\b(champions league|europa league|conference league|fa cup|league cup|carabao cup|"
    r"world cup|international duty|(?:pre[- ]?season|international|club) friendly|"
    r"friendly (?:match|against|with)|youth league|under[- ](?:18|21|23)|women['’]s)\b",
    re.I,
)
# Look ahead so two opponent references in one sentence are checked separately.
_OPPONENT = re.compile(
    r"(?=\b(?:against|versus|vs\.?|(?:trip|visit|journey|travell?ing|travels?)\s+to|"
    r"(?:visit|visits)\s+(?:of|from)|(?:game|match|tie|fixture|clash|meeting)\s+"
    r"(?:with|at|away\s+to)|(?:away|at\s+home)\s+to)\s+([\w’' .&-]{1,75}))",
    re.I,
)
_NON_OPPONENT = re.compile(r"^(?:the (?:odds|clock)|medical advice)\b", re.I)


_ALIASES = {
    "manchester-united": ("man-utd", "man-united", "red-devils"),
    "manchester-city": ("man-city",),
    "newcastle-united": ("newcastle", "magpies"),
    "tottenham-hotspur": ("tottenham", "spurs"),
    "aston-villa": ("villa",),
    "nottingham-forest": ("forest", "nottm-forest", "notts-forest"),
    "wolverhampton-wanderers": ("wolves",),
    "brighton-hove-albion": ("brighton",),
    "west-ham-united": ("west-ham",),
    "crystal-palace": ("palace",),
    "leeds-united": ("leeds",),
    "ipswich-town": ("ipswich",),
    "hull-city": ("hull",),
    "coventry-city": ("coventry",),
}


def fixture_relevant(quote, title, game, team):
    """Match the stated opponent, competition and local match date before assigning news."""
    combined = quote + " " + title
    if _CUP_CONTEXT.search(combined):
        return False
    home, away = team_key(game["home"]), team_key(game["away"])
    clubs = {home: away, away: home}
    opponent = clubs.get(team_key(team))
    if not opponent:
        return False
    aliases = (opponent, *_ALIASES.get(opponent, ()))
    for match in _OPPONENT.finditer(combined):
        target = match[1].strip()
        if _NON_OPPONENT.match(target):
            continue
        named = team_key(target).removeprefix("the-")
        if not any(named == alias or named.startswith(alias + "-") for alias in aliases):
            return False
    if _RECAP.search(combined) and not _FUTURE.search(quote):
        return False
    # Named dates in an explicitly match-specific claim must agree with this
    # fixture. Unanchored dates of diagnosis are not interpreted as kickoff.
    from zoneinfo import ZoneInfo

    kick = store.instant(game["kickoff"]).astimezone(ZoneInfo("Europe/London"))
    for match in _GAME_DATE.finditer(combined):
        day = _MONTHS.get(match["month"][:3].lower())
        if day and (kick.month, kick.day) != (day, int(match["day"])):
            return False
    for match in _GAME_US_DATE.finditer(combined):
        if (kick.month, kick.day) != (
            _MONTHS[match["month"][:3].lower()],
            int(match["day"]),
        ):
            return False
    for match in _GAME_ISO_DATE.finditer(combined):
        try:
            if kick.date() != date.fromisoformat(match[1]):
                return False
        except ValueError:
            return False
    for match in _GAME_WEEKDAY.finditer(combined):
        if _WEEKDAYS[kick.weekday()] != match[1].casefold():
            return False
    return True


_RECAP = re.compile(
    r"\b(?:match report|post[- ]match|round[- ]up|highlights|after the final whistle|"
    r"last (?:week|match|game|season)|yesterday['’]s (?:match|win|defeat|draw))\b"
    r"|\b(?:missed|sat out|was sidelined for|was ruled out of) (?:the |their |his )?(?:match|game|win|defeat|draw)\b",
    re.I,
)
_FUTURE = re.compile(
    r"\b(?:will|still|remains?|next (?:match|game)|set to miss|is currently)\b", re.I
)
_MONTHS = {
    name: i
    for i, name in enumerate(
        (
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ),
        1,
    )
}
_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_GAME_DATE = re.compile(
    r"\b(?:match|game|fixture|clash|tie|derby) (?:on |of )?(?P<day>[0-3]?[0-9])"
    r"(?:st|nd|rd|th)? (?P<month>Jan[a-z]*|Feb[a-z]*|Mar[a-z]*|Apr[a-z]*|May|Jun[a-z]*"
    r"|Jul[a-z]*|Aug[a-z]*|Sep[a-z]*|Oct[a-z]*|Nov[a-z]*|Dec[a-z]*)\b",
    re.I,
)
_GAME_US_DATE = re.compile(
    r"\b(?:match|game|fixture|clash|tie|derby) (?:on |of )?"
    r"(?P<month>Jan[a-z]*|Feb[a-z]*|Mar[a-z]*|Apr[a-z]*|May|Jun[a-z]*"
    r"|Jul[a-z]*|Aug[a-z]*|Sep[a-z]*|Oct[a-z]*|Nov[a-z]*|Dec[a-z]*) (?P<day>[0-3]?[0-9])"
    r"(?:st|nd|rd|th)?\b",
    re.I,
)
_GAME_ISO_DATE = re.compile(
    r"\b(?:match|game|fixture|clash|tie|derby) (?:on |of )?(\d{4}-\d{2}-\d{2})\b", re.I
)
_GAME_WEEKDAY = re.compile(
    r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)['’]s "
    r"(?:match|game|fixture|clash|tie|derby)\b",
    re.I,
)


def _grounded(claim, original, roster):
    ident = str(claim["player_id"])
    person = roster.get(ident)
    start, end = claim["start"], claim["end"]
    if (
        not person
        or type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(original["text"])
        or original["text"][start:end] != claim["quote"]
    ):
        raise ValueError("ungrounded player or evidence span")
    if (
        claim["status"]
        not in ("confirmed", "reported", "rumour", "denied", "uncertain")
        or claim["event"]
        not in ("injury", "suspension", "availability", "return", "transfer")
        or claim["availability"] not in ("out", "doubtful", "available", "unknown")
        or not str(claim.get("model_version", "")).strip()
    ):
        raise ValueError("unknown AI extraction vocabulary/version")
    status = claim["status"]
    if status == "confirmed" and original["authority"] != "official":
        status = "reported"
    fact = {
        k: claim[k]
        for k in ("event", "availability", "quote", "start", "end", "model_version")
    }
    fact.update(
        player_id=ident,
        player_name=person.get("name") or person.get("player_name"),
        status=status,
    )
    if "confidence" in claim:
        c = claim["confidence"]
        if type(c) not in (float, int) or not 0 <= c <= 1:
            raise ValueError("invalid extraction confidence")
        fact["confidence"] = c
    if person.get("position"):
        fact["position"] = person["position"]
    if person.get("source_url"):
        fact["roster_url"] = person["source_url"]
    if person.get("seen_at"):
        fact["roster_seen_at"] = store.iso(person["seen_at"])
    return fact


def _claim_key(fact):
    return tuple(fact[k] for k in ("player_id", "event", "status", "availability"))


def _prior_states(db, article):
    previous = db.execute(
        """SELECT seq,retracted FROM news_articles WHERE season=? AND team_key=? AND source=?
        AND external_id=? AND (updated_at<? OR (updated_at=? AND seq<?))
        ORDER BY updated_at DESC,seq DESC LIMIT 1""",
        (
            article["season"],
            article["team_key"],
            article["source"],
            article["external_id"],
            article["updated_at"],
            article["updated_at"],
            article["seq"],
        ),
    ).fetchone()
    if not previous or previous["retracted"]:
        return {}
    pred = previous["seq"]
    if db.execute(
        "SELECT 1 FROM news_revision_conflicts WHERE article=? LIMIT 1", (pred,)
    ).fetchone():
        return {}
    rows = db.execute(
        """SELECT c.payload FROM news_claims c LEFT JOIN news_claim_runs link ON link.claim=c.id
        WHERE c.article=? AND coalesce(link.run,0)=coalesce((SELECT seq FROM news_analysis_runs
        WHERE article=? ORDER BY known_at DESC,seq DESC LIMIT 1),0)""",
        (pred, pred),
    )
    before = {}
    for row in rows:
        fact = json.loads(row["payload"])
        before[_claim_key(fact)] = fact.get("effective_at") or fact["published_at"]
    return before


def _extract_pending(root, task, players, extract, settings, decision):
    # Identity and aliases affect whether a name can be resolved. A fresh roster
    # receipt with unchanged members doesn't create redundant model work.
    stable_roster = [
        {
            key: p.get(key)
            for key in ("player_id", "name", "aliases", "position", "team")
        }
        for p in sorted(players, key=lambda x: str(x["player_id"]))
    ]
    policy = store.digest([settings, stable_roster])
    at = _utc()
    target = task["target"]
    with _db(root) as db:
        articles = db.execute(
            """SELECT a.*,p.receipt receipt_id,pp.payload source_proof,count(*) OVER () pending_total FROM news_articles a
            LEFT JOIN news_article_proofs p ON p.article=a.seq LEFT JOIN news_poll_proofs pp ON pp.id=p.receipt
            LEFT JOIN news_analysis_runs r ON r.seq=(SELECT seq FROM news_analysis_runs
                WHERE article=a.seq ORDER BY known_at DESC,seq DESC LIMIT 1)
            WHERE a.fixture_id=? AND a.kickoff=? AND a.season=? AND a.team_key=? AND a.source=?
            AND a.retracted=0 AND (r.seq IS NULL OR r.policy<>? OR r.retry_at<=?)
            AND a.kickoff>? AND """
            + _NEWER
            + " ORDER BY a.seq LIMIT ?",
            (
                target["fixture_id"],
                target["kickoff"],
                *_key(task),
                policy,
                at,
                at,
                at,
                at,
                MAX_BATCH,
            ),
        ).fetchall()
    roster_sha = store.digest(players)
    identities = {str(p["player_id"]): p for p in players}
    total = 0
    backends = Counter()
    fallbacks = Counter()
    abstentions = 0
    begun = monotonic()
    remaining = articles[0]["pending_total"] if articles else 0
    for article in articles:
        if monotonic() - begun > 25:
            break
        original = json.loads(store.read_blob(root, article["original_sha256"]))
        proof = json.loads(article["source_proof"]) if article["source_proof"] else {}
        rows = []
        claims = extract(
            original["text"],
            players,
            authority=original["authority"],
            title=original["title"],
            team=task["team"],
        )
        meta = (
            dict(decision())
            if decision
            else {
                "backend": "injected",
                "model_version": claims[0]["model_version"]
                if claims
                else settings["extractor"],
            }
        )
        for claim in claims:
            fact = _grounded(claim, original, identities)
            if fixture_relevant(fact["quote"], original["title"], target, task["team"]):
                rows.append(fact)
        meta["fixture_scope_excluded"] = len(claims) - len(rows)
        with _db(root, write=True) as db:
            if not _owns(db, task):
                raise ValueError("news extraction lease expired")
            known = _utc()
            model_version = meta["model_version"]
            retry = None
            if meta.get("local_requested") and meta.get("fallback_reason"):
                reason = str(meta.get("local_status") or "unavailable")
                fallbacks[reason] += 1
                retry = _plus(known, timedelta(minutes=30))
            db.execute(
                "INSERT OR IGNORE INTO news_extractions VALUES (?,?,?,?,?)",
                (article["seq"], known, model_version, roster_sha, len(rows)),
            )
            run_id = db.execute(
                """INSERT INTO news_analysis_runs
                (article,policy,known_at,roster_sha256,claim_count,metadata,retry_at) VALUES (?,?,?,?,?,?,?)""",
                (
                    article["seq"],
                    policy,
                    known,
                    roster_sha,
                    len(rows),
                    store.canonical(meta),
                    retry,
                ),
            ).lastrowid
            remaining -= 1
            backends[meta["backend"]] += 1
            abstentions += not rows
            prior = (
                _prior_states(db, article)
                if rows and not original["observed_state_as_of"]
                else {}
            )
            for fact in rows:
                # A genuine changed statement takes its publisher revision
                # time; a cosmetic correction cannot revive an older diagnosis.
                effective = original["observed_state_as_of"] or prior.get(
                    _claim_key(fact), original["updated_at"]
                )
                fact.update(
                    team=task["team"],
                    fixture_id=article["fixture_id"],
                    kickoff=article["kickoff"],
                    source=original["source"],
                    origin=original["origin"],
                    authority=original["authority"],
                    url=original["url"],
                    title=original["title"],
                    attribution=original["attribution"],
                    license=original["license"],
                    license_url=original["license_url"],
                    original_sha256=article["original_sha256"],
                    revision=original["revision"],
                    external_id=original["external_id"],
                    published_at=article["published_at"],
                    received_at=article["received_at"],
                    known_at=known,
                    extracted_at=known,
                    roster_sha256=roster_sha,
                    updated_at=original["updated_at"],
                    effective_at=effective,
                    time_basis=original["time_basis"],
                    observed_state_as_of=original["observed_state_as_of"],
                    evidence_class=original["evidence_class"],
                    source_receipt=article["receipt_id"],
                    response_sha256=proof.get("response_sha256"),
                    response_hash_basis=proof.get("hash_basis"),
                    extractor_backend=meta["backend"],
                    analysis_policy=policy,
                    analysis_run=run_id,
                )
                fid = store.digest(fact)
                db.execute(
                    "INSERT OR IGNORE INTO news_claims VALUES (?,?,?)",
                    (fid, article["seq"], store.canonical(fact)),
                )
                db.execute(
                    "INSERT OR IGNORE INTO news_claim_runs VALUES (?,?)", (fid, run_id)
                )
                total += 1
    return {
        "claims_added": total,
        "backlog": remaining > 0,
        "analysis_remaining": remaining,
        "analysis_backends": dict(backends),
        "fallbacks": dict(fallbacks),
        "zero_claim_analyses": abstentions,
    }


def _finish(root, task, detail, cursor, provider_after):
    with _db(root, write=True) as db:
        if not _owns(db, task):
            detail.update(state="lease_lost", issues=["news stream lease lost"])
            return detail
        at = _utc()
        failed = 0 if detail["state"] == "ok" else min(10, task["failures"] + 1)
        delay = (
            POLL_INTERVAL
            if not failed
            else timedelta(minutes=min(60, 3 * 2 ** (failed - 1)))
        )
        if detail.get("provider_pending", 0):
            delay = timedelta(minutes=1)
        next_at = max(_plus(at, delay), provider_after or "")
        db.execute(
            """UPDATE news_streams SET next_at=?,provider_after=?,owner=NULL,lease_until=NULL,
            failures=?,cursor=? WHERE season=? AND team_key=? AND source=? AND owner=?""",
            (next_at, provider_after or "", failed, cursor, *_key(task), task["owner"]),
        )
        retry = None
        if detail["state"] == "no_roster" or any(
            s.startswith("extraction:") for s in detail["issues"]
        ):
            retry = _plus(at, min(delay, timedelta(minutes=15)))
        if detail.get("extraction_pending") or detail.get("inbox_pending") or detail.get("archive_incomplete"):
            retry = _plus(at, timedelta(minutes=1))
        if detail["state"] == "degraded":
            retry = _plus(at, timedelta(minutes=30))
        db.execute(
            """INSERT INTO news_local_work VALUES (?,?,?,?,?) ON CONFLICT(season,team_key,source)
            DO UPDATE SET policy=excluded.policy,retry_at=excluded.retry_at""",
            (*_key(task), task["settings_id"], retry),
        )
        detail.update(checked_at=at, next_at=next_at, local_retry_at=retry)
        db.execute(
            """INSERT INTO news_checks
            (season,team_key,source,fixture_id,kickoff,checked_at,state,detail) VALUES (?,?,?,?,?,?,?,?)""",
            (
                *_key(task),
                task["target"]["fixture_id"],
                task["target"]["kickoff"],
                at,
                detail["state"],
                store.canonical(detail),
            ),
        )
    return detail


def _reject(root, task, item, received, receipt, exc):
    # Keep the actual authorized adapter item privately for diagnosis. A
    # malformed article must not stall the cursor of an otherwise usable feed.
    raw = store.canonical(item).encode()
    if len(raw) > 2_000_000:
        raise ValueError("invalid publisher item too large to quarantine")
    evidence = store.archive_bytes(
        root,
        "epl-news-rejected",
        raw,
        observed_at=received,
        metadata={"team": task["team_key"], "source_id": task["source"]},
    )
    reason = str(exc)[:240]
    ident = store.digest([*_key(task), evidence["sha256"], reason])
    obj = item if isinstance(item, dict) else {}
    with _db(root, write=True) as db:
        if not _owns(db, task):
            raise ValueError("news collection lease expired")
        db.execute(
            """INSERT INTO news_rejected_items VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen,response_receipt=excluded.response_receipt""",
            (
                ident,
                *_key(task),
                str(obj.get("external_id", ""))[:512],
                str(obj.get("revision", ""))[:120],
                received,
                received,
                evidence["sha256"],
                reason,
                receipt,
            ),
        )


def _accept_item(root, task, item, received, receipt, detail):
    try:
        detail["articles_added"] += _archive(root, task, item, received, receipt)
    except (KeyError, TypeError, ValueError) as exc:
        detail["rejected_items"] = detail.get("rejected_items", 0) + 1
        _reject(root, task, item, received, receipt, exc)
        if len(detail["issues"]) < 3:
            detail["issues"].append("article: " + str(exc)[:240])


def _drain_feed_inbox(root, task, detail):
    with _db(root) as db:
        pending = db.execute(
            """SELECT seq,received_at,receipt,payload FROM news_feed_inbox
            WHERE season=? AND team_key=? AND source=? ORDER BY seq LIMIT ?""", (*_key(task), MAX_BATCH)
        ).fetchall()
    for row in pending:
        _accept_item(root, task, json.loads(row["payload"]), row["received_at"], row["receipt"], detail)
        # A crash between archiving and acknowledging simply retries the immutable row.
        with _db(root, write=True) as db:
            if not _owns(db, task):
                raise ValueError("news collection lease expired")
            db.execute("DELETE FROM news_feed_inbox WHERE seq=?", (row["seq"],))
    with _db(root) as db:
        return db.execute(
            "SELECT count(*) FROM news_feed_inbox WHERE season=? AND team_key=? AND source=?", _key(task)
        ).fetchone()[0]


def _poll_stream(root, task, poll, roster, roster_issue, extract, settings, decision):
    from .news_sources import MAX_ITEMS

    target = task["target"]
    detail = {
        "team": task["team"],
        "season": task["season"],
        "source": task["source"],
        "fixture_id": target["fixture_id"],
        "roster_players": len(roster),
        "articles_added": 0,
        "claims_added": 0,
        "issues": [],
        "state": "ok",
    }
    if roster_issue:
        detail["roster_status"] = roster_issue
    cursor = task["cursor"]
    provider_after = task["provider_after"]
    issues = detail["issues"]
    detail["polled"] = task["network_due"]
    if task["network_due"]:
        try:
            local_start = _utc()
            result = poll(
                task["config"],
                task["team"],
                task["season"],
                json.loads(cursor) if cursor else None,
            )
            received = _utc()
            if not isinstance(result, dict) or result.get("status") in {
                "error",
                "failed",
            }:
                raise ValueError("provider failed to return a completed observation")
            if result.get("next_poll_after"):
                provider_after = store.iso(result["next_poll_after"])
            articles = result["articles"]
            stage_feed = (task["config"].get("kind") == "feed" and task["config"].get("free") is True
                and result.get("response_sha256") and result.get("request_url") == task["config"].get("url")
                and isinstance(result.get("entries"), int))
            limit = MAX_ITEMS if stage_feed else MAX_BATCH
            if not isinstance(articles, list) or len(articles) > limit:
                raise ValueError("invalid or over-limit news batch")
            detail["provider_counts"] = {
                k: result[k]
                for k in (
                    "player_rows",
                    "upstream_rumours",
                    "invalid_entries",
                    "pending_entries",
                    "baseline_only",
                    "revisions",
                )
                if k in result and type(result[k]) in (bool, int)
            }
            pending = result.get("pending_entries", 0)
            if type(pending) is not int or pending < 0:
                raise ValueError("invalid publisher continuation count")
            detail["provider_pending"] = pending
            if result.get("invalid_entries"):
                issues.append("publisher feed included invalid entries")
            candidate_cursor = store.canonical(result.get("cursor") or {})
            if len(candidate_cursor) > 2_000_000:
                raise ValueError("news cursor exceeds the storage bound")
            proof_id, proof = _poll_proof(root, task, result, local_start, received, stage_feed)
            detail["source_observed_at"] = received
            detail["response_sha256"] = proof["response_sha256"]
            detail["response_hash_basis"] = proof["hash_basis"]
            conflicts = result.get("revision_conflicts", [])
            if conflicts:
                _provider_conflicts(root, task, conflicts, received, proof_id)
                detail["revision_conflicts"] = len(conflicts)
                issues.append("publisher returned contradictory content for a single revision")
            if not stage_feed:
                for item in articles:
                    _accept_item(root, task, item, received, proof_id, detail)
            cursor = candidate_cursor
            detail["network_complete"] = not issues and detail["provider_pending"] == 0
        except Exception as exc:
            issues.append("poll: " + str(exc)[:240])
            retry = getattr(exc, "retry_after", None)
            if isinstance(retry, (int, float)) and 0 < retry <= 86400:
                provider_after = max(
                    provider_after or "", _plus(_utc(), timedelta(seconds=retry))
                )
    else:
        # Process already archived material immediately after a roster or model
        # becomes available, without polling inside the publisher's cache TTL.
        with _db(root) as db:
            last = db.execute(
                """SELECT detail FROM news_checks WHERE season=? AND team_key=? AND source=?
                AND fixture_id=? AND kickoff=? AND json_extract(detail,'$.polled')=1
                ORDER BY checked_at DESC,id DESC LIMIT 1""",
                (*_key(task), target["fixture_id"], target["kickoff"]),
            ).fetchone()
        if last:
            prior = json.loads(last[0])
            for field in (
                "source_observed_at",
                "response_sha256",
                "response_hash_basis",
                "provider_counts",
                "provider_pending",
                "revision_conflicts",
                "network_complete",
            ):
                if field in prior:
                    detail[field] = prior[field]
            detail["network_complete"] = prior.get("network_complete", prior.get("fetch_complete", False))
            if not detail["network_complete"] and not detail.get("provider_pending"):
                issues.append("latest source check incomplete for this fixture")
        else:
            issues.append("no previously successful source receipt for this fixture")
    try:
        if task["config"].get("kind") == "feed":
            detail["inbox_pending"] = _drain_feed_inbox(root, task, detail)
        outstanding = _open_conflicts(root, task)
        if outstanding:
            if not detail.get("revision_conflicts"):
                issues.append("an earlier publisher revision contradiction remains unresolved")
            detail["revision_conflicts"] = max(outstanding, detail.get("revision_conflicts", 0))
    except Exception as exc:
        detail["archive_incomplete"] = True
        issues.append("archive: " + str(exc)[:240])
    detail["fetch_complete"] = bool(detail.get("network_complete") and not issues and not detail.get("inbox_pending"))
    backlog = False
    if roster:
        try:
            analysis = _extract_pending(root, task, roster, extract, settings, decision)
            backlog = analysis.pop("backlog")
            detail["extraction_pending"] = backlog
            detail.update(analysis)
        except Exception as exc:
            issues.append("extraction: " + str(exc)[:240])
    if issues:
        detail["state"] = "error"
    elif not roster:
        detail["state"] = "no_roster"
        issues.append("no usable contemporaneous player roster")
    elif detail.get("provider_pending", 0):
        detail["state"] = "pending"
        issues.append(
            f"{detail['provider_pending']} publisher feed entries await collection"
        )
    elif detail.get("inbox_pending") or backlog:
        detail["state"] = "pending"
        issues.append("locally captured news articles await processing")
    elif detail.get("fallbacks"):
        detail["state"] = "degraded"
        issues.append(
            "configured local language model fell back to the bundled extractor"
        )
    detail["pending_entries"] = (detail.get("provider_pending", 0) + detail.get("inbox_pending", 0)
                                + detail.get("analysis_remaining", 0))
    return _finish(root, task, detail, cursor, provider_after)


def sync(
    data_dir,
    *,
    limit=4,
    workers=2,
    lookahead_days=30,
    team=None,
    season=None,
    force=False,
    poller=None,
    roster_refresh=None,
    roster_reader=None,
    extractor=None,
    defaults=None,
):
    """Claim bounded feed work. Network and inference run outside SQLite transactions."""
    from . import news_ai, news_roster, news_sources

    if not (1 <= limit <= 80 and 1 <= workers <= 8 and 1 <= lookahead_days <= 90):
        raise ValueError("news limit 1–80, workers 1–8, lookahead 1–90 days")
    poll = poller or news_sources.poll
    refresh = roster_refresh or news_roster.refresh
    read = roster_reader or news_roster.as_of
    if extractor is None:
        from . import news_local_ai

        extract = news_local_ai.extract
        decision = news_local_ai.last_decision
        cfg = news_local_ai.status()
        settings = {
            "extractor": news_ai.MODEL_VERSION,
            "local_protocol": news_local_ai.PROTOCOL,
            "local_state": cfg["state"],
            "local_model": cfg.get("model_id"),
            "local_endpoint_sha256": cfg.get("endpoint_sha256"),
        }
    else:
        extract = extractor
        decision = None
        settings = {
            "extractor": getattr(
                extract,
                "model_version",
                getattr(extract, "__qualname__", type(extract).__name__),
            )
        }
    tasks, eligible, due = _claim(
        data_dir,
        limit,
        lookahead_days,
        team,
        season,
        force,
        defaults or news_sources.default_sources,
        store.digest(settings),
    )
    by_club = defaultdict(list)
    for task in tasks:
        by_club[task["team"], task["season"]].append(task)

    def run_club(group):
        club, season_id = group[0]["team"], group[0]["season"]
        roster_issue = None
        try:
            report = refresh(data_dir, club, season_id)
            if report.get("status") not in ("updated", "cached", "ok"):
                roster_issue = str(report.get("status"))[:160]
        except Exception as exc:
            roster_issue = str(exc)[:160]
        try:
            players = read(data_dir, club, season_id, _utc())
        except Exception as exc:
            roster_issue = str(exc)[:160]
            players = []
        outcomes = []
        for task in group:
            try:
                result = _poll_stream(
                    data_dir,
                    task,
                    poll,
                    players,
                    roster_issue,
                    extract,
                    settings,
                    decision,
                )
            except Exception as exc:
                # A broken source must not suppress a healthy source or other club.
                result = {
                    "season": task["season"],
                    "team": club,
                    "source": task["source"],
                    "fixture_id": task["target"]["fixture_id"],
                    "state": "error",
                    "articles_added": 0,
                    "claims_added": 0,
                    "issues": [str(exc)[:240]],
                }
                # If the database cannot finish the attempt, its lease will expire.
                result = _finish(
                    data_dir, task, result, task["cursor"], task["provider_after"]
                )
            outcomes.append(result)
        return outcomes

    streams = []
    if by_club:
        with ThreadPoolExecutor(max_workers=min(workers, len(by_club))) as pool:
            for group in pool.map(run_club, by_club.values()):
                streams.extend(group)
    successes = sum(s["state"] == "ok" for s in streams)
    if streams:
        with _db(data_dir, write=True) as db:
            store.put_report(db, "news-coverage", "epl-teams", _status_db(db))
            _publish_research_health(db)
    return {
        "state": (
            "idle"
            if not tasks
            else "ok"
            if successes == len(tasks)
            else "partial"
            if successes
            else "pending"
            if all(s["state"] == "pending" for s in streams)
            else "error"
        ),
        "eligible_streams": eligible,
        "due_streams": due,
        "attempted": len(streams),
        "succeeded": successes,
        "pending_streams": sum(s["state"] == "pending" for s in streams),
        "pending_entries": sum(s.get("pending_entries", 0) for s in streams),
        "failures": len(streams) - successes,
        "articles_added": sum(s["articles_added"] for s in streams),
        "claims_added": sum(s["claims_added"] for s in streams),
        "streams": streams,
    }


def _empty_side(team=None):
    return {"team": team, "observed": False, "sources": []}


def _empty_snapshot(fixture_id, at, reason, fixture=None):
    return {
        "fixture_id": fixture_id,
        "cutoff": at,
        "facts": [],
        "coverage": {
            "home": _empty_side(fixture and fixture["home"]),
            "away": _empty_side(fixture and fixture["away"]),
            "both_teams_observed": False,
            "observed_teams": 0,
            "reason": reason,
        },
    }


def _side_coverage(db, game, side, at):
    club = team_key(game[side])
    rows = db.execute(
        """SELECT s.source,s.config,
          (SELECT c.detail FROM news_checks c WHERE c.season=s.season AND c.team_key=s.team_key
          AND c.source=s.source AND c.fixture_id=? AND c.kickoff=? AND c.checked_at<=?
          ORDER BY c.checked_at DESC,c.id DESC LIMIT 1) detail
        FROM news_streams s LEFT JOIN news_feed_config f ON f.id=s.source
        WHERE s.season=? AND s.team_key=? AND s.registered_at<=?
          AND (f.disabled_at IS NULL OR f.disabled_at>?) ORDER BY s.source""",
        (game["fixture_id"], game["kickoff"], at, game["season"], club, at, at),
    )
    sources = []
    for row in rows:
        raw = json.loads(row["detail"]) if row["detail"] else {}
        stamp = raw.get("checked_at")
        observed = raw.get("source_observed_at") or stamp
        state = raw.get("state", "never_checked")
        if observed and observed < _plus(at, -COVERAGE_AGE):
            state = "stale"
        eligible = (
            state in ("ok", "degraded")
            and row["source"] != "touchline-rumours"
            and not (raw.get("provider_counts") or {}).get("baseline_only")
        )
        sources.append(
            {
                "id": row["source"],
                "origin": json.loads(row["config"])["origin"],
                "status": state,
                "checked_at": stamp,
                "source_observed_at": observed,
                "availability_observed": eligible,
                "fetch_complete": bool(raw.get("fetch_complete")),
                "pending_entries": raw.get("pending_entries", raw.get("provider_pending", 0)),
                "revision_conflicts": raw.get("revision_conflicts", 0),
                "roster_status": raw.get("roster_status"),
                "roster_players": raw.get("roster_players", 0),
                "issues": raw.get("issues", []),
            }
        )
    return {
        "team": game[side],
        "sources": sources,
        "observed": any(s["availability_observed"] for s in sources),
    }


def _rank(fact):
    return (fact["effective_at"], fact["known_at"], fact["id"])


def _current_claims(facts):
    """Resolve same-origin updates, collapse syndication and expose disagreements."""
    by_player = defaultdict(list)
    for fact in facts:
        topic = "transfer" if fact["event"] == "transfer" else "availability"
        by_player[(team_key(fact["team"]), fact["player_id"], topic)].append(fact)
    result = []
    for group, player_facts in sorted(by_player.items()):
        origins = defaultdict(list)
        for fact in player_facts:
            origins[fact["origin"]].append(fact)
        retained = []
        for stories in origins.values():
            # A later speculative item cannot overwrite a publisher's verified update.
            strong = [
                f for f in stories if f["status"] in ("confirmed", "denied", "reported")
            ]
            keep = strong or stories
            latest = max(f["effective_at"] for f in keep)
            newest = [f for f in keep if f["effective_at"] == latest]
            retained.extend(newest)
            official_versions = [
                f
                for f in keep
                if f["authority"] == "official" and f["status"] == "confirmed"
            ]
            # A syndicated paraphrase, even when it names the club as the
            # underlying source, cannot erase the surviving club original.
            # The SQL revision filter has already removed replaced originals.
            if official_versions and any(f["authority"] != "official" for f in newest):
                official_latest = max(f["effective_at"] for f in official_versions)
                retained.extend(
                    f
                    for f in official_versions
                    if f["effective_at"] == official_latest and f not in newest
                )
        official = [
            f
            for f in retained
            if f["authority"] == "official" and f["status"] in ("confirmed", "denied")
        ]
        if official:
            last_official = max(f["effective_at"] for f in official)
            # Older speculation is resolved by an authoritative club statement.
            retained = [f for f in retained if f["effective_at"] >= last_official]
        credible = [
            f for f in retained if f["status"] in ("confirmed", "denied", "reported")
        ]
        polarities = {
            (
                "denied"
                if f["availability"] == "unknown" and f["status"] == "denied"
                else f["availability"]
            )
            for f in credible
            if f["availability"] != "unknown" or f["status"] == "denied"
        }
        conflict = len(polarities) > 1
        equivalent = defaultdict(list)
        for fact in retained:
            equivalent[(fact["event"], fact["availability"], fact["status"])].append(
                fact
            )
        for kind, copies in sorted(equivalent.items()):
            first = max(copies, key=lambda f: (f["authority"] == "official", *_rank(f)))
            cluster = store.digest([first["fixture_id"], *group, *kind])
            result.append(
                first
                | {
                    "cluster_id": cluster,
                    "conflict": conflict,
                    "evidence_ids": sorted(f["id"] for f in copies),
                    "origins": sorted({f["origin"] for f in copies}),
                }
            )
    return result


def snapshot(data_dir, fixture_id, cutoff):
    """A read-only observation as known at the cutoff, with team-by-team coverage."""
    import sqlite3
    from pathlib import Path

    at = store.iso(cutoff)
    file = Path(data_dir).expanduser() / "runtime.sqlite3"
    if not file.exists():
        return _empty_snapshot(fixture_id, at, "no_database")
    with closing(
        sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        tables = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('news_articles','news_analysis_runs','fixture_versions','schedule_receipts')"
            )
        }
        if "fixture_versions" not in tables or "schedule_receipts" not in tables:
            return _empty_snapshot(fixture_id, at, "no_contemporaneous_schedule")
        while True:
            game = db.execute(
                """SELECT f.* FROM fixture_versions f JOIN schedule_receipts r USING(fixture_id,version)
                WHERE f.fixture_id=? AND f.known_at<=? AND r.recorded_at<=?
                ORDER BY f.known_at DESC LIMIT 1""",
                (fixture_id, at, at),
            ).fetchone()
            if not game:
                return _empty_snapshot(fixture_id, at, "no_contemporaneous_schedule")
            if "news_articles" not in tables:
                return _empty_snapshot(fixture_id, at, "news_not_collected", game)
            actual = db.execute(
                """SELECT t.actual_kickoff FROM result_versions r
                JOIN result_schedule_refs s USING(fixture_id,version)
                LEFT JOIN result_event_times t USING(fixture_id,version)
                WHERE r.fixture_id=? AND s.kickoff=? AND r.recorded_at<=?
                AND coalesce((SELECT held FROM result_dispositions
                    WHERE fixture_id=? AND kickoff=? AND at<=? ORDER BY at DESC LIMIT 1),0)=0
                ORDER BY r.version DESC LIMIT 1""",
                (fixture_id, game["kickoff"], at, fixture_id, game["kickoff"], at),
            ).fetchone()
            latest = (
                min(game["kickoff"], actual[0])
                if actual and actual[0]
                else game["kickoff"]
            )
            if latest > at:
                break
            # Reconstruct the schedule and evidence that were in fact known
            # before the observed or advertised kickoff, using one DB snapshot.
            at = _plus(latest, -timedelta(microseconds=1))
        coverage = {
            side: _side_coverage(db, game, side, at) for side in ("home", "away")
        }
        # A player can transfer, a club can withdraw its squad, and a fresh
        # squad at another club can make a previously clear identity ambiguous.
        # Read membership from the same point-in-time database snapshot as
        # the articles. The roster captured during extraction is only its
        # original provenance.
        from .news_roster import roster_views

        squads = roster_views(db, [game["home"], game["away"]], game["season"], at)
        eligible = {}
        for side in ("home", "away"):
            club = team_key(game[side])
            roster = squads[club]
            eligible[club] = {str(p["player_id"]): p for p in roster["players"]}
            coverage[side]["roster"] = {
                k: roster.get(k)
                for k in (
                    "state",
                    "seen_at",
                    "expires_at",
                    "digest",
                    "ambiguous_player_ids",
                )
            }
            coverage[side]["roster"].update(
                eligible_players=len(eligible[club]), excluded_claims=0
            )
            if not eligible[club]:
                for source in coverage[side]["sources"]:
                    source["availability_observed"] = False
        run_join = ""
        run_filter = ""
        args = (fixture_id, game["kickoff"], at, at, at, at, at, at, at)
        if "news_analysis_runs" in tables:
            run_join = " LEFT JOIN news_claim_runs cr ON cr.claim=c.id "
            run_filter = """ AND coalesce(cr.run,0)=coalesce((SELECT x.seq FROM news_analysis_runs x
                WHERE x.article=a.seq AND x.known_at<=? ORDER BY x.known_at DESC,x.seq DESC LIMIT 1),0)"""
            args += (at,)
        rows = db.execute(
            """SELECT c.id,c.payload,a.original_sha256 FROM news_articles a
            JOIN news_extractions e ON a.seq=e.article JOIN news_claims c ON c.article=a.seq"""
            + run_join
            + """ LEFT JOIN news_feed_config f ON f.id=a.source
            WHERE a.fixture_id=? AND a.kickoff=? AND a.retracted=0 AND a.published_at<=?
            AND a.received_at<=? AND a.recorded_at<=? AND e.known_at<=?
            AND (f.disabled_at IS NULL OR f.disabled_at>?) AND """
            + _NEWER
            + run_filter,
            args,
        ).fetchall()
    facts = []
    originals = {}
    bad_sources = set()
    for row in rows:
        fact = json.loads(row["payload"])
        sha = row["original_sha256"]
        if sha not in originals:
            try:
                originals[sha] = json.loads(store.read_blob(data_dir, sha))
            except (OSError, ValueError):
                originals[sha] = None
        raw = originals[sha]
        if (
            not raw
            or fact["original_sha256"] != sha
            or store.digest(fact) != row["id"]
            or raw["text"][fact["start"] : fact["end"]] != fact["quote"]
        ):
            bad_sources.add((team_key(fact["team"]), fact["source"]))
            continue
        club = team_key(fact["team"])
        player = eligible.get(club, {}).get(str(fact["player_id"]))
        if player is None:
            for side in ("home", "away"):
                if club == team_key(game[side]):
                    coverage[side]["roster"]["excluded_claims"] += 1
            continue
        facts.append(
            fact
            | {
                "id": row["id"],
                "effective_at": fact.get("effective_at", fact["published_at"]),
                "roster_validated_at": squads[club]["seen_at"],
                "active_roster_sha256": squads[club]["digest"],
            }
        )
    for side in coverage.values():
        for src in side["sources"]:
            if (team_key(side["team"]), src["id"]) in bad_sources:
                src["status"] = "invalid_archive"
                src["availability_observed"] = False
        if side["roster"]["excluded_claims"]:
            # The feed was actually polled, but could not supply a fully
            # squad-grounded comparison for this club.
            side["roster"]["coverage_gap"] = "reported_player_no_longer_unique_member"
            for source in side["sources"]:
                source["availability_observed"] = False
        side["observed"] = any(s["availability_observed"] for s in side["sources"])
    observed = sum(side["observed"] for side in coverage.values())
    coverage.update(observed_teams=observed, both_teams_observed=observed == 2)
    return {
        "fixture_id": fixture_id,
        "cutoff": at,
        "facts": _current_claims(facts),
        "coverage": coverage,
    }


def as_of(data_dir, fixture_id, cutoff):
    return snapshot(data_dir, fixture_id, cutoff)["facts"]


def _health_issue(detail):
    if not detail:
        return None
    state = detail.get("state")
    counts = detail.get("provider_counts") or {}
    if detail.get("revision_conflicts"):
        return "revision_conflict", "invalid"
    if detail.get("rejected_items") or counts.get("invalid_entries"):
        return "rejected_items", "invalid"
    if state == "pending" and detail.get("provider_pending"):
        return "provider_backlog", "late"
    return {
        "no_roster": ("roster_unavailable", "freshness_unknown"),
        "degraded": ("model_degraded", "error"),
        "error": ("collector_error", "error"),
        "lease_lost": ("interrupted", "late"),
        "pending": ("analysis_backlog", "late"),
    }.get(state)


def research_health(db):
    """Allowlisted issue categories and coarse counts, recomputed from durable checks.

    Checkpoint/cursor, item text, identities, URLs, decision and result fields
    never enter the researcher copy. A progressing publisher backlog stays an
    operational status; two nonprogressing observations qualify for research.
    """
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='news_checks'"
    ).fetchone():
        return {}
    rows = db.execute(
        """SELECT c.detail current,p.detail previous,
        CASE s.source WHEN 'touchline-availability' THEN 'availability'
         WHEN 'touchline-rumours' THEN 'rumours' WHEN 'wikipedia-season-news' THEN 'wiki'
         ELSE 'publisher_feed' END AS category
        FROM news_streams s
        LEFT JOIN news_feed_config f ON f.id=s.source
        JOIN news_checks c ON c.id=(SELECT id FROM news_checks n WHERE n.season=s.season
          AND n.source=s.source AND n.team_key=s.team_key ORDER BY n.checked_at DESC,n.id DESC LIMIT 1)
        JOIN news_checks p ON p.id=(SELECT id FROM news_checks n WHERE n.season=s.season
          AND n.source=s.source AND n.team_key=s.team_key ORDER BY n.checked_at DESC,n.id DESC LIMIT 1 OFFSET 1)
        WHERE f.disabled_at IS NULL AND c.checked_at>=?""",
        (_plus(_utc(), -timedelta(days=2)),),
    )
    affected = Counter()
    for row in rows:
        current = json.loads(row["current"])
        previous = json.loads(row["previous"])
        issue = _health_issue(current)
        if not issue or issue != _health_issue(previous):
            continue
        if (
            issue[0] in ("provider_backlog", "analysis_backlog")
            and current.get("pending_entries", current.get("provider_pending", 0))
            < previous.get("pending_entries", previous.get("provider_pending", 0))
        ):
            continue
        affected[(row["category"], *issue)] += 1
    return {
        source + "." + cause: {
            "status": status,
            "affected_streams": ("1" if n == 1 else "2–4" if n < 5 else "5+"),
        }
        for (source, cause, status), n in sorted(affected.items())
    }


def _publish_research_health(db):
    report = {"source_id": RESEARCH_SOURCE, "health": research_health(db)}
    prior = db.execute(
        "SELECT payload FROM reports WHERE kind='source' AND key=?", (RESEARCH_SOURCE,)
    ).fetchone()
    if prior is None and not report["health"]:
        return
    if prior is None or store.canonical(report) != prior[0]:
        store.put_report(db, "source", RESEARCH_SOURCE, report)


def _status_db(db, team=None):
    where = " WHERE s.team_key=?" if team else ""
    values = (team_key(team),) if team else ()
    rows = db.execute(
        """SELECT s.season,s.team,s.source,s.next_at,s.started,
      (SELECT detail FROM news_checks c WHERE c.season=s.season AND c.team_key=s.team_key
       AND c.source=s.source ORDER BY c.checked_at DESC,c.id DESC LIMIT 1) AS detail
       FROM news_streams s"""
        + where
        + " ORDER BY s.season,s.team_key,s.source",
        values,
    )
    streams = []
    for r in rows:
        stream = dict(r)
        last = stream.pop("detail")
        stream["latest"] = json.loads(last) if last else None
        streams.append(stream)
    counts = {
        t: db.execute("SELECT count(*) FROM " + t).fetchone()[0]
        for t in (
            "news_articles",
            "news_extractions",
            "news_claims",
            "news_rejected_items",
            "news_publisher_conflicts",
            "news_analysis_runs",
        )
    }
    return {
        "streams": streams,
        "archive_counts": counts,
        "pending_entries": sum(
            (s["latest"] or {}).get("pending_entries", (s["latest"] or {}).get("provider_pending", 0)) for s in streams
        ),
        "last_states": dict(
            Counter((s["latest"] or {}).get("state", "never_checked") for s in streams)
        ),
    }


def status(data_dir, team=None):
    with _db(data_dir) as db:
        report = _status_db(db, team)
    from . import news_local_ai

    return report | {"extractor": news_local_ai.status()}


def sources(data_dir, team=None, season=None):
    from . import news_sources

    today = store.instant(_utc())
    year = today.year - (today.month < 7)
    season_id = season or f"{year}-{(year + 1) % 100:02d}"
    with _db(data_dir) as db:
        listed = [
            json.loads(row["config"])
            | {"registered_at": row["registered_at"], "disabled_at": row["disabled_at"]}
            for row in db.execute("SELECT * FROM news_feed_config ORDER BY id")
            if not team or row["team_key"] == team_key(team)
        ]
    club = team or "Chelsea FC"
    return {
        "example_team": club,
        "season": season_id,
        "default": news_sources.default_sources(club, season_id),
        "registered": listed,
    }


def article(data_dir, sha):
    with _db(data_dir) as db:
        records = db.execute(
            """SELECT p.id,p.payload FROM news_articles a LEFT JOIN news_article_proofs link
            ON link.article=a.seq LEFT JOIN news_poll_proofs p ON p.id=link.receipt
            WHERE a.original_sha256=?""",
            (sha,),
        ).fetchall()
        if not records:
            raise ValueError("unknown archived news document")
        conflicts = [
            dict(row) for row in db.execute(
                """SELECT DISTINCT m.reason,m.recorded_at AS observed_at,m.receipt,
                json_extract(p.payload,'$.response_sha256') AS response_sha256
                FROM news_articles a JOIN news_revision_conflicts c ON c.article=a.seq
                JOIN news_publisher_conflicts m ON m.receipt=c.other_sha256 AND m.season=a.season
                AND m.team_key=a.team_key AND m.source=a.source AND m.external_id=a.external_id
                JOIN news_poll_proofs p ON p.id=m.receipt
                WHERE a.original_sha256=? ORDER BY m.recorded_at""", (sha,)
            )
        ]
        analyses = [
            dict(r) | {"metadata": json.loads(r["metadata"])}
            for r in db.execute(
                """SELECT x.policy,x.known_at,x.roster_sha256,x.claim_count,x.metadata,x.retry_at FROM news_analysis_runs x
               JOIN news_articles a ON a.seq=x.article WHERE a.original_sha256=? ORDER BY x.known_at,x.seq""",
                (sha,),
            )
        ]
    proofs = [
        json.loads(r["payload"]) | {"id": r["id"]} for r in records if r["payload"]
    ]
    return json.loads(store.read_blob(data_dir, sha)) | {
        "original_sha256": sha,
        "collection_receipts": proofs,
        "revision_conflicts": conflicts,
        "analyses": analyses,
    }


def register(subparsers):
    commands = subparsers.add_parser(
        "news", help="球队消息、球员核验和赛前配对研究"
    ).add_subparsers(required=True)
    run = commands.add_parser("sync", help="轮换采集未来赛程的球队消息")
    run.add_argument("--limit", type=int, default=4)
    run.add_argument("--workers", type=int, default=2)
    run.add_argument("--lookahead-days", type=int, default=30)
    run.add_argument("--team")
    run.add_argument("--season")
    run.add_argument(
        "--force",
        action="store_true",
        help="重新检查本地未到期的流，遵守上游最早请求时间",
    )

    def collect_and_update(a):
        from .season import sync_news

        return sync_news(
            a.data_dir,
            limit=a.limit,
            workers=a.workers,
            team=a.team,
            season=a.season,
            force=a.force,
            lookahead_days=a.lookahead_days,
        )

    run.set_defaults(func=collect_and_update)
    updates = commands.add_parser("updates", help="查看由赛前消息变化触发的研究预测")
    updates.add_argument("fixture_id", nargs="?")
    updates.add_argument("--limit", type=int, default=20)

    def list_changes(a):
        from .news_events import list_updates

        return list_updates(a.data_dir, a.fixture_id, limit=a.limit)

    updates.set_defaults(func=list_changes)
    event = commands.add_parser("reforecast", help="检查近期球员消息变化并刷新对应预测")
    event.add_argument("--limit", type=int, default=2)

    def refresh_games(a):
        from .news_events import refresh

        return refresh(a.data_dir, limit=a.limit)

    event.set_defaults(func=refresh_games)
    ls = commands.add_parser("sources")
    ls.add_argument("--team")
    ls.add_argument("--season")
    ls.set_defaults(func=lambda a: sources(a.data_dir, a.team, a.season))
    add = commands.add_parser(
        "add-feed", help="登记有明确免费采集、留存、提取和建模权限的 RSS/Atom 源"
    )
    add.add_argument("id")
    add.add_argument("--team", required=True)
    add.add_argument("--url", required=True)
    add.add_argument("--license", required=True)
    add.add_argument("--license-url", required=True)
    add.add_argument("--attribution", required=True)
    add.add_argument("--origin")
    add.add_argument(
        "--authority", choices=("secondary", "official"), default="secondary"
    )
    add.add_argument(
        "--grant-free-use",
        action="store_true",
        required=True,
        help="确认上列免费许可允许自动采集、留存、信息提取和建模",
    )

    def create(a):
        spec = {
            "id": a.id,
            "kind": "feed",
            "team": a.team,
            "url": a.url,
            "license": a.license,
            "license_url": a.license_url,
            "attribution": a.attribution,
            "authority": a.authority,
            "free": a.grant_free_use,
            "rights": ["fetch", "store", "extract", "model"],
        }
        if a.origin:
            spec["origin"] = a.origin
        return add_feed(a.data_dir, spec)

    add.set_defaults(func=create)
    off = commands.add_parser("disable-feed")
    off.add_argument("id")
    off.set_defaults(func=lambda a: disable_feed(a.data_dir, a.id))
    roster = commands.add_parser("roster", help="查看新闻识别使用的已核球队名单")
    roster.add_argument("team")
    roster.add_argument("season")
    roster.add_argument("--at")

    def squad(a):
        from .news_roster import as_of as roster_at

        return roster_at(a.data_dir, a.team, a.season, a.at or _utc())

    roster.set_defaults(func=squad)
    health = commands.add_parser("status")
    health.add_argument("--team")
    health.set_defaults(func=lambda a: status(a.data_dir, a.team))
    facts = commands.add_parser("facts", help="查看某场比赛当时已处理的球员消息和覆盖")
    facts.add_argument("fixture_id")
    facts.add_argument("--at")
    facts.set_defaults(
        func=lambda a: snapshot(a.data_dir, a.fixture_id, a.at or _utc())
    )
    original = commands.add_parser("article")
    original.add_argument("sha256")
    original.set_defaults(func=lambda a: article(a.data_dir, a.sha256))
    ev = commands.add_parser("evaluate")
    ev.add_argument("season")
    ev.add_argument("round", type=int, nargs="?")
    ev.add_argument("--horizon", choices=("T-60m", "T-24h"), default="T-60m")
    ev.add_argument(
        "--mode", choices=("technical", "formal", "replay"), default="technical"
    )

    def evaluate(a):
        from .news_evaluation import evaluate_round, evaluate_season

        if a.round is None:
            return evaluate_season(a.data_dir, a.season, horizon=a.horizon, mode=a.mode)
        return evaluate_round(
            a.data_dir, a.season, a.round, horizon=a.horizon, mode=a.mode
        )

    ev.set_defaults(func=evaluate)
