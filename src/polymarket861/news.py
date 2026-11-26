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

from . import dbscope, news_poll_archive, news_scope, store
from .football import team_key
from .news_retention import retention
from .news_scope import competition_target, injury_headline


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
CREATE INDEX IF NOT EXISTS news_articles_original ON news_articles(original_sha256);
CREATE TABLE IF NOT EXISTS news_poll_proofs (
 id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS news_wire_states (
 article INTEGER PRIMARY KEY REFERENCES news_articles(seq),state_sha256 TEXT NOT NULL,
 first_seen_at TEXT NOT NULL,absence_kind TEXT,absence_first_seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS news_wire_compactions (
 original_sha256 TEXT PRIMARY KEY,checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS news_wire_manifests (
 id TEXT PRIMARY KEY,articles TEXT NOT NULL,projection TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS news_wire_polls (
 receipt TEXT PRIMARY KEY REFERENCES news_poll_proofs(id),season TEXT NOT NULL,team_key TEXT NOT NULL,
 fixture_id TEXT,observed_at TEXT NOT NULL,provider_at TEXT NOT NULL,recorded_at TEXT NOT NULL,articles TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS news_wire_poll_fixture ON news_wire_polls(fixture_id,observed_at DESC,recorded_at DESC);
CREATE INDEX IF NOT EXISTS news_wire_poll_team ON news_wire_polls(season,team_key,fixture_id,observed_at DESC);
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
""" + news_poll_archive.SCHEMA


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
      ) SELECT fixture_id,season,home,away,kickoff,precision,version FROM known WHERE n=1
      ORDER BY kickoff,fixture_id""",
        args,
    )
    return [dict(row) for row in rows]


def _timetables(fixtures, league="epl"):
    teams = defaultdict(list)
    for game in fixtures:
        if news_scope.competition(game) != league:
            continue
        for side in ("home", "away"):
            teams[game["season"], team_key(game[side])].append(game)
    return teams


def _claim(data_dir, limit, days, only_team, season, force, defaults, settings_id, eligible_schedules):
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
            if (target["fixture_id"], target["kickoff"], target["version"]) not in eligible_schedules:
                continue
            name = next(
                target[s] for s in ("home", "away") if team_key(target[s]) == club
            )
            for config in (*defaults(name, year), *registered.get(club, ())):
                ident = config["id"]
                league = news_scope.competition(target)
                if (ident == "touchline-availability" and
                        not news_scope.wire_coverage(name, year, league, at)):
                    continue
                if ident == "wikipedia-season-news" and league != "epl":
                    continue
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


def _wire_projection(response):
    from .news_sources import wire_fingerprint

    return {"states": {r["external_id"]: wire_fingerprint(r["source_fields"]) for r in response["wire_observations"]},
            "rejected": sorted(r["row_sha256"] for r in response["wire_rejections"])}


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
    if "wire_observations" in response and task["source"] == "touchline-availability":
        if not raw_digest or proof["request_url"] != task["config"]["url"]:
            raise ValueError("missing original wire response receipt")
        proof["provider_state_as_of"] = store.iso(response["provider_state_as_of"])
        proof["wire_projection_sha256"] = store.digest(_wire_projection(response))
        scope = news_scope.wire_coverage(task["team"], task["season"], news_scope.competition(task["target"]),
                                         proof["provider_state_as_of"])
        if not scope or response.get("coverage_evidence") != scope:
            raise ValueError("source_not_covered: availability receipt lacks verified club and competition scope")
        proof["coverage_evidence"] = scope
    ident = store.digest(proof)
    with _db(root, write=True) as db:
        if not _owns(db, task):
            raise ValueError("news collection lease expired")
        db.execute(
            "INSERT OR IGNORE INTO news_poll_proofs VALUES (?,?)",
            (ident, store.canonical(proof)),
        )
        # An exactly repeated physical receipt can be referenced by a new
        # collector after a restore. Count its hash once; retain its original
        # compact wire ordering and recorded-at clock.
        db.execute("UPDATE news_poll_ticks SET standalone_proof=1 WHERE receipt=? AND standalone_proof=0", (ident,))
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
            WHERE m.season=? AND m.team_key=? AND m.source=? AND m.fixture_id=?
            AND (a.seq IS NULL OR EXISTS (SELECT 1 FROM news_revision_conflicts q WHERE q.article=a.seq))""",
            (*_key(task), task["target"]["fixture_id"]),
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
        return 0, old["seq"] if old else None
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
        return ins.rowcount, ins.lastrowid if ins.rowcount else None


# Exclude an earlier state as soon as the replacement was locally recorded,
# including the time spent extracting a revised article. Re-reading an old
# version doesn't supersede a later publication.
_NEWER = """NOT EXISTS (SELECT 1 FROM news_articles n WHERE n.season=a.season
 AND n.team_key=a.team_key AND n.source=a.source AND n.external_id=a.external_id
 AND n.recorded_at<=? AND (n.updated_at>a.updated_at OR (n.updated_at=a.updated_at AND n.seq>a.seq)))
 AND NOT EXISTS (SELECT 1 FROM news_revision_conflicts v WHERE v.article=a.seq AND v.recorded_at<=?)"""


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


def fixture_relevant(quote, title, game, team, *, antecedent=""):
    """Match the stated opponent, competition and local match date before assigning news."""
    target, headline = competition_target(quote), competition_target(title)
    if target is None:
        return False
    if headline is None:
        if not injury_headline(title, target):
            return False
        headline = ""
    combined = target + " " + headline
    if antecedent and not fixture_relevant(antecedent, "", game, team):
        if not (_OPPONENT.search(combined) or _LEAGUE_OR_DURATION.search(combined)):
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
    if _RECAP.search(combined) and not _FUTURE.search(target):
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
        if _WEEKDAYS[kick.weekday()] != (match[1] or match[2]).casefold():
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
_LEAGUE_OR_DURATION = re.compile(
    r"\b(?:premier league|premiership|epl|(?:upcoming|coming|rest of the|remainder of the) season|"
    r"(?:for|out for) (?:one|two|three|four|several|[0-9]+) (?:weeks?|months?))\b", re.I,
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
_DAY_NAME = r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
_MATCH_NOUN = r"(?:match|game|fixture|clash|tie|derby)"
_GAME_WEEKDAY = re.compile(
    rf"\b{_DAY_NAME}['’]s {_MATCH_NOUN}\b|\b{_MATCH_NOUN} on {_DAY_NAME}\b", re.I
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
    if "antecedent" in claim:
        anchor = claim["antecedent"]
        if (not isinstance(anchor, dict) or type(anchor.get("start")) is not int
                or type(anchor.get("end")) is not int
                or not 0 <= anchor["start"] < anchor["end"] <= start
                or original["text"][anchor["start"]:anchor["end"]] != anchor.get("quote")):
            raise ValueError("ungrounded pronoun antecedent")
        fact["antecedent"] = {k: anchor[k] for k in ("start", "end", "quote")}
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
    from . import news_ai, news_sources, news_wire_identity

    structured = task["source"] == news_sources.WIRE and task["config"].get("kind") == "touchline"
    aliases = news_ai._roster_index(players)[1] if structured else None
    at = _utc()
    target = task["target"]
    with _db(root) as db:
        directory = news_wire_identity.directory(db, root, target, task["team"], at, players) if structured else {}
        policy_input = [news_sources.WIRE_MODEL if structured else settings, stable_roster]
        if structured:
            policy_input.append(news_wire_identity.signature(directory))
        policy = store.digest(policy_input)
        articles = db.execute(
            """SELECT a.*,p.receipt receipt_id,pp.payload source_proof,count(*) OVER () pending_total FROM news_articles a
            LEFT JOIN news_article_proofs p ON p.article=a.seq LEFT JOIN news_poll_proofs pp ON pp.id=p.receipt
            LEFT JOIN news_analysis_runs r ON r.seq=(SELECT seq FROM news_analysis_runs
                WHERE article=a.seq ORDER BY known_at DESC,seq DESC LIMIT 1)
            WHERE a.fixture_id=? AND a.season=? AND a.team_key=? AND a.source=?
            AND a.retracted=0 AND (r.seq IS NULL OR r.policy<>? OR r.retry_at<=?)
            AND """
            + _NEWER
            + " ORDER BY a.seq LIMIT ?",
            (
                target["fixture_id"],
                *_key(task),
                policy,
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
        provider_identity = None
        if structured:
            claims = news_sources.wire_claim(original, aliases)
            claims, provider_identity = news_wire_identity.bind(original, aliases, claims, directory)
            meta = {"backend": "structured_wire", "model_version": news_sources.WIRE_MODEL,
                    "unmatched_player": not claims}
            if provider_identity:
                meta["provider_identity"] = news_wire_identity.proof(provider_identity, article["original_sha256"])
        else:
            claims = extract(original["text"], players, authority=original["authority"],
                             title=original["title"], team=task["team"])
            meta = dict(decision()) if decision else {
                "backend": "injected", "model_version": claims[0]["model_version"] if claims else settings["extractor"]
            }
        for claim in claims:
            claimant = identities
            if provider_identity and provider_identity["identity_basis"] == "touchline_directory_id":
                claimant = {provider_identity["player_id"]: provider_identity}
            fact = _grounded(claim, original, claimant)
            if provider_identity:
                fact["provider_identity"] = meta["provider_identity"]
            if structured:
                fact.update(chance_of_playing=claim["chance_of_playing"],
                            provider_player_id=original["provider_player_id"])
            rows.append(fact)
        # Persist grounded candidates once. The publication window, opponent
        # and stated match day are evaluated against the schedule at each cutoff.
        meta["fixture_scope"] = "at_snapshot"
        meta["extractor_config_sha256"] = store.digest(settings)
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
                    extractor_config_sha256=meta["extractor_config_sha256"],
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


def _finish(root, task, detail, cursor, provider_after, receipt=None):
    with _db(root, write=True) as db:
        if not _owns(db, task):
            detail.update(state="lease_lost", issues=["news stream lease lost"])
            return detail
        at = _utc()
        failed = 0 if detail["state"] in ("ok", "partial") else min(10, task["failures"] + 1)
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
        if detail.get("unmatched_players"):
            retry = _plus(at, POLL_INTERVAL)
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
        check = db.execute(
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
        news_poll_archive.track(db, check.lastrowid, receipt)
    return detail


def _reject(root, task, item, received, receipt, exc):
    # Keep the actual authorized adapter item privately for diagnosis. A
    # malformed article must not stall the cursor of an otherwise usable feed.
    raw = store.canonical(item).encode()
    if len(raw) > 2_000_000:
        raise ValueError("invalid publisher item too large to quarantine")
    reason = str(exc)[:240]
    sha = hashlib.sha256(raw).hexdigest()
    ident = store.digest([*_key(task), sha, reason])
    with _db(root) as db:
        known = db.execute("SELECT 1 FROM news_rejected_items WHERE id=?", (ident,)).fetchone()
    if not known or not store.blob_path(root, sha).is_file():
        store.archive_bytes(root, "epl-news-rejected", raw, observed_at=received,
                            metadata={"team": task["team_key"], "source_id": task["source"]})
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
                sha,
                reason,
                receipt,
            ),
        )


def _accept_item(root, task, item, received, receipt, detail):
    try:
        added, seq = _archive(root, task, item, received, receipt)
        detail["articles_added"] += added
        return seq
    except (KeyError, TypeError, ValueError) as exc:
        detail["rejected_items"] = detail.get("rejected_items", 0) + 1
        _reject(root, task, item, received, receipt, exc)
        if len(detail["issues"]) < 3:
            detail["issues"].append("article: " + str(exc)[:240])


def _wire_absence(fields):
    # Status/percentage updates can describe one continuous injury. A return,
    # withdrawal, suspension replacing an injury, or dispute breaks that episode.
    return fields["category"] if fields.get("chance_of_playing") != 100 else None


def _wire_previous(root, db, key, *, cutoff=None):
    from .news_sources import wire_fingerprint

    # A read of a legacy cutoff may run on an unmigrated, read-only database.
    # Reconstruct only from original editions and conflicts known by that date.
    if cutoff:
        columns = "NULL state_sha256,NULL absence_kind,NULL first_seen_at,NULL absence_first_seen_at"
        join = ""
    else:
        columns = "w.state_sha256,w.absence_kind,w.first_seen_at,w.absence_first_seen_at"
        join = "LEFT JOIN news_wire_states w ON w.article=a.seq"
    at = cutoff or _utc()
    versions = db.execute(
        f"""SELECT a.seq,a.fixture_id,a.received_at,a.original_sha256,a.retracted,
        EXISTS (SELECT 1 FROM news_revision_conflicts c WHERE c.article=a.seq AND c.recorded_at<=?) disputed,
        {columns} FROM news_articles a {join} WHERE a.season=? AND a.team_key=? AND a.source=? AND a.external_id=?
        AND a.recorded_at<=? AND a.received_at<=? ORDER BY a.updated_at DESC,a.seq DESC""", (at, *key, at, at))
    previous = None
    same_state = same_absence = True
    for row in versions:
        if row["retracted"] or row["disputed"]:
            break
        fingerprint, kind = row["state_sha256"], row["absence_kind"]
        if fingerprint is None:
            # Upgrade a 0.4.0 stream lazily, walking back only the uninterrupted
            # run. Legacy provider timestamps were refreshed on every poll.
            try:
                fields = json.loads(store.read_blob(root, row["original_sha256"]))["source_fields"]
                fingerprint, kind = wire_fingerprint(fields), _wire_absence(fields)
            except (OSError, KeyError, TypeError, ValueError):
                break
        if previous is None:
            previous = dict(row) | {"state_sha256": fingerprint, "absence_kind": kind}
        same_state = same_state and previous["state_sha256"] == fingerprint
        same_absence = same_absence and previous["absence_kind"] == kind and kind is not None
        if same_state:
            previous["first_seen_at"] = row["first_seen_at"] or row["received_at"]
        if same_absence:
            previous["absence_first_seen_at"] = row["absence_first_seen_at"] or row["received_at"]
        if row["state_sha256"] or not (same_state or same_absence):
            break
    return previous


def _collect_wire(root, task, result, received, receipt, detail):
    from .news_sources import wire_fingerprint

    states = result["wire_observations"]
    snapshot = store.iso(result["provider_state_as_of"])
    for rejection in result["wire_rejections"]:
        _reject(root, task, rejection["item"], received, receipt, rejection["reason"])
        detail["rejected_items"] = detail.get("rejected_items", 0) + 1
    for item in result["articles"]:
        if item.get("retracted"):
            _accept_item(root, task, item, received, receipt, detail)
    # Decide against the latest durable version for this identity. A stream
    # cursor alone cannot distinguish another fixture, an interrupted write,
    # or a reinstatement of a state that also appeared earlier in the season.
    with _db(root) as db:
        games = _timetables(_schedule(db, _utc(), task["season"])).get((task["season"], task["team_key"]), ())
        target = _target_for(snapshot, games, _utc())
        fixture = target["fixture_id"] if target else None
        previous = {doc["external_id"]: _wire_previous(root, db, (*_key(task), doc["external_id"]))
                    for doc in states}
    witnessed = []
    for doc in states:
        fields = doc["source_fields"]
        fingerprint, kind = wire_fingerprint(fields), _wire_absence(fields)
        old = previous[doc["external_id"]]
        same = old is not None and old["state_sha256"] == fingerprint
        if same and old["fixture_id"] == fixture:
            seq = old["seq"]
        else:
            seq = _accept_item(root, task, doc, received, receipt, detail)
        if seq is not None:
            first = old["first_seen_at"] if same else received
            absence = old["absence_first_seen_at"] if old and kind and old["absence_kind"] == kind else received
            witnessed.append((seq, fingerprint, first, kind, absence or first))
    with _db(root, write=True) as db:
        if not _owns(db, task):
            raise ValueError("news collection lease expired")
        db.executemany("INSERT OR IGNORE INTO news_wire_states VALUES (?,?,?,?,?)", witnessed)
        # A recurring set of editions shares its immutable membership proof.
        # Each later poll needs only this pointer and its own network clocks/hash.
        articles = sorted(row[0] for row in witnessed)
        projection = _wire_projection(result)
        manifest = store.digest([articles, projection])
        db.execute("INSERT OR IGNORE INTO news_wire_manifests VALUES (?,?,?)",
                   (manifest, store.canonical(articles), store.canonical(projection)))
        db.execute("""INSERT OR IGNORE INTO news_wire_polls SELECT ?,?,?,?,?,?,?,?
            WHERE NOT EXISTS (SELECT 1 FROM news_poll_ticks WHERE receipt=? AND wire_order IS NOT NULL)""",
            (receipt, task["season"], task["team_key"], fixture, received, snapshot, _utc(), manifest, receipt))


def _wire_witnesses(root, db, game, at, selected):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE name IN "
                                      "('news_wire_polls','news_wire_manifests')")}
    manifests = "news_wire_manifests" if "news_wire_manifests" in tables else "(SELECT NULL id,NULL articles,NULL projection)"
    polls = news_poll_archive.history(db, "news_wire_polls")
    receipts = news_poll_archive.history(db, "news_poll_proofs")
    rows = db.execute(
        f"""WITH latest AS (
        SELECT j.value article,p.receipt,p.observed_at,p.provider_at,m.id AS manifest,m.projection,m.articles,
          row_number() OVER (PARTITION BY j.value ORDER BY p.observed_at DESC,p.recorded_at DESC,p.rowid DESC) n
        FROM {polls} p LEFT JOIN {manifests} m ON m.id=p.articles
        JOIN json_each(CASE WHEN json_valid(m.articles) THEN m.articles
             WHEN json_valid(p.articles) THEN p.articles END) j
        WHERE p.fixture_id=? AND p.observed_at<=? AND p.recorded_at<=? AND p.provider_at<=?)
        SELECT l.*,s.state_sha256,s.first_seen_at,s.absence_first_seen_at,a.external_id
        FROM latest l JOIN news_wire_states s ON s.article=l.article JOIN news_articles a ON a.seq=l.article
        WHERE l.n=1""",
        (game["fixture_id"], at, at, at)) if "news_wire_polls" in tables else ()
    witnesses, proofs = {}, {}
    for row in rows:
        receipt = row["receipt"]
        if receipt not in proofs:
            saved = db.execute(f"SELECT payload FROM {receipts} WHERE id=?", (receipt,)).fetchone()
            proof = json.loads(saved[0]) if saved else {}
            valid = store.digest(proof) == receipt
            states = proof.get("wire_states", {})
            if row["manifest"]:
                try:
                    projection = json.loads(row["projection"])
                    states = projection["states"]
                    valid = (valid and isinstance(states, dict)
                             and store.digest(projection) == proof.get("wire_projection_sha256")
                             and store.digest([json.loads(row["articles"]), projection]) == row["manifest"])
                except (KeyError, ValueError, TypeError):
                    valid = False
            proofs[receipt] = proof | {"states": states} if valid else {}
        proof = proofs[receipt]
        if (proof.get("states", {}).get(row["external_id"]) == row["state_sha256"]
                and proof.get("observed_at") == row["observed_at"]
                and proof.get("provider_state_as_of") == row["provider_at"]):
            witnesses[row["article"]] = {
                "first_seen_at": row["first_seen_at"], "absence_first_seen_at": row["absence_first_seen_at"],
                "last_seen_at": row["observed_at"], "last_seen_state_as_of": row["provider_at"],
                "last_seen_receipt": receipt, "last_seen_response_sha256": proof["response_sha256"],
            }
    missing = {row["article_seq"] for row in selected} - witnesses.keys()
    if missing:
        legacy = db.execute("""SELECT a.seq,a.season,a.team_key,a.source,a.external_id,a.received_at,a.updated_at,
            p.receipt FROM news_articles a LEFT JOIN news_article_proofs p ON p.article=a.seq
            WHERE a.source='touchline-availability' AND a.seq IN (SELECT value FROM json_each(?))""",
            (store.canonical(sorted(missing)),))
        for row in legacy:
            state = _wire_previous(root, db, (*_key(row), row["external_id"]), cutoff=at)
            if state and state["seq"] == row["seq"]:
                witnesses[row["seq"]] = {
                    "first_seen_at": state["first_seen_at"],
                    "absence_first_seen_at": state["absence_first_seen_at"] or state["first_seen_at"],
                    "last_seen_at": row["received_at"], "last_seen_state_as_of": row["updated_at"],
                    "last_seen_receipt": row["receipt"],
                }
    return witnesses


def _wire_unmatched(root, task):
    # The count reflects every valid row in the latest receipt, including
    # unchanged states. A new roster can re-resolve them without a new fetch.
    with _db(root) as db:
        rows = db.execute(
            """SELECT a.original_sha256,r.claim_count,r.metadata FROM json_each((
            SELECT coalesce(m.articles,CASE WHEN json_valid(p.articles) THEN p.articles END)
            FROM news_wire_polls p LEFT JOIN news_wire_manifests m ON m.id=p.articles WHERE p.season=? AND p.team_key=? AND p.fixture_id=?
            ORDER BY p.observed_at DESC,p.recorded_at DESC,p.rowid DESC LIMIT 1)) j
            JOIN news_articles a ON a.seq=j.value LEFT JOIN news_analysis_runs r ON r.seq=(
            SELECT x.seq FROM news_analysis_runs x WHERE x.article=j.value ORDER BY x.known_at DESC,x.seq DESC LIMIT 1)""",
            (task["season"], task["team_key"], task["target"]["fixture_id"]),
        ).fetchall()
    reviewed, unknown = [], 0
    for row in rows:
        item = json.loads(row["metadata"]).get("provider_identity") if row["metadata"] else None
        if item:
            reviewed.append(item)
        elif not row["claim_count"]:
            unknown += 1
    return {"unmatched_players": unknown + len(reviewed), "unresolved_players": unknown,
            "provider_identity_reviews": reviewed}


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
    from .news_sources import MAX_ITEMS, WIRE

    target = task["target"]
    detail = {
        "team": task["team"],
        "season": task["season"],
        "source": task["source"],
        "fixture_id": target["fixture_id"],
        "roster_players": len(roster),
        "extractor_config_sha256": store.digest(settings),
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
    proof_id = None
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
            wire = task["source"] == WIRE and "wire_observations" in result
            if wire and (not isinstance(result["wire_observations"], list) or len(result["wire_observations"]) > MAX_ITEMS
                         or not isinstance(result.get("wire_rejections"), list) or len(result["wire_rejections"]) > MAX_ITEMS):
                raise ValueError("invalid wire observation batch")
            # A complete turnover can include every current row and withdrawals
            # for every member of the preceding bounded snapshot.
            limit = 2 * MAX_ITEMS if wire else MAX_ITEMS if stage_feed else MAX_BATCH
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
            if wire:
                detail["provider_state_as_of"] = result["provider_state_as_of"]
                detail["coverage_evidence"] = proof["coverage_evidence"]
                _collect_wire(root, task, result, received, proof_id, detail)
            elif not stage_feed:
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
                AND fixture_id=? AND json_extract(detail,'$.polled')=1
                ORDER BY checked_at DESC,id DESC LIMIT 1""",
                (*_key(task), target["fixture_id"]),
            ).fetchone()
        if last:
            prior = json.loads(last[0])
            for field in (
                "source_observed_at",
                "response_sha256",
                "response_hash_basis",
                "provider_state_as_of",
                "coverage_evidence",
                "rejected_items",
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
            if task["source"] == WIRE:
                detail.update(_wire_unmatched(root, task))
                if detail["unmatched_players"]:
                    detail["identity_only_partial"] = bool(
                        detail["provider_identity_reviews"] and not detail["unresolved_players"] and
                        not issues and detail.get("fetch_complete") and not backlog and
                        not detail.get("provider_pending") and not detail.get("inbox_pending")
                    )
                    issues.append(f"{detail['unmatched_players']} listed players lack a direct roster name match")
        except Exception as exc:
            issues.append("extraction: " + str(exc)[:240])
    wire_gap = (task["source"] == WIRE and detail.get("source_observed_at") and
                (detail.get("unmatched_players") or detail.get("rejected_items")
                 or detail.get("revision_conflicts")))
    if issues:
        detail["state"] = "partial" if wire_gap else "error"
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
    return _finish(root, task, detail, cursor, provider_after, proof_id)


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
    importance_getter=None,
):
    """Claim bounded feed work. Network and inference run outside SQLite transactions."""
    from . import news_ai, news_roster, news_sources, schedule_consensus

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
    # Upgrades reanalyse recoverable old rows under their actual local analysis
    # time, including weekday candidates the previous collector discarded.
    settings["fixture_scope"] = "cutoff-schedule-v1"
    schedules = schedule_consensus.readiness(
        data_dir, season=season, news_days=lookahead_days, team=team, at=_utc()
    )
    tasks, eligible, due = _claim(
        data_dir,
        limit,
        lookahead_days,
        team,
        season,
        force,
        defaults or news_sources.active_sources,
        store.digest(settings),
        schedule_consensus.eligible_news(schedules),
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
        usage = None
        if any(task["source"] == news_sources.WIRE for task in group) and (poller is None or importance_getter is not None):
            from . import news_minutes

            # An observed directory can resolve the very first injury batch.
            # Its failure still permits the ordinary roster-only path.
            usage = news_minutes.refresh(data_dir, club, season_id, getter=importance_getter)
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
        for result in outcomes:
            if result["source"] == news_sources.WIRE and usage is not None:
                result["player_usage"] = usage
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
            "awaiting_schedule"
            if not tasks and schedules["blocked_fixtures"]
            else "idle"
            if not tasks
            else "ok"
            if successes == len(tasks) and not schedules["blocked_fixtures"]
            else "partial"
            if successes or any(s["state"] == "partial" for s in streams)
            else "pending"
            if all(s["state"] == "pending" for s in streams)
            else "error"
        ),
        "eligible_streams": eligible,
        "schedule_readiness": schedules,
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


def _side_coverage(db, root, game, side, at, games):
    club = team_key(game[side])
    checks = news_poll_archive.history(db, "news_checks")
    rows = db.execute(
        """SELECT s.source,s.config FROM news_streams s
        LEFT JOIN news_feed_config f ON f.id=s.source
        WHERE s.season=? AND s.team_key=? AND s.registered_at<=?
          AND (f.disabled_at IS NULL OR f.disabled_at>?) ORDER BY s.source""",
        (game["season"], club, at, at),
    )
    sources = []
    for row in rows:
        check = db.execute(
            f"""SELECT id,season,team_key,source,fixture_id,kickoff,checked_at,detail FROM {checks} WHERE season=? AND team_key=?
                AND source=? AND fixture_id=? AND checked_at<=?
                ORDER BY checked_at DESC,id DESC LIMIT 1""",
            (game["season"], club, row["source"], game["fixture_id"], at),
        ).fetchone()
        raw = json.loads(check["detail"]) if check else {}
        if not news_poll_archive.verified_check(db, check, raw):
            raw = {"state": "invalid_poll_evidence", "checked_at": check["checked_at"],
                   "issues": ["archived poll receipt or wire membership failed verification"]}
        stamp = raw.get("checked_at")
        observed = raw.get("source_observed_at") or (stamp if raw.get("state") != "invalid_poll_evidence" else None)
        freshness = min(observed, raw.get("provider_state_as_of") or observed) if observed else None
        state = raw.get("state", "never_checked")
        if freshness and freshness < _plus(at, -COVERAGE_AGE):
            state = "stale"
        elif observed and any(next((g["fixture_id"] for g in games if g["kickoff"] > stamp), None) != game["fixture_id"]
                              for stamp in {freshness, observed}):
            state = "different_next_fixture"
        coverage_evidence = None
        if row["source"] == "touchline-availability" and state in ("ok", "degraded", "partial"):
            scope = news_scope.wire_coverage(game[side], game["season"], news_scope.competition(game),
                                             raw.get("provider_state_as_of") or at)
            if scope is None:
                state = "source_not_covered"
            elif not news_poll_archive.wire_coverage_received(db, root, check, raw, scope, json.loads(row["config"])):
                state = "unverified_wire_coverage"
            else:
                coverage_evidence = scope
        if row["source"] == "wikipedia-season-news" and news_scope.competition(game) != "epl":
            state = "source_not_covered"
        if news_scope.competition(game) != "epl" and state in ("ok", "degraded", "partial"):
            state = "league_news_not_validated"
        counts = raw.get("provider_counts") or {}
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
                "recorded_kickoff": check["kickoff"] if check else None,
                "checked_at": stamp,
                "source_observed_at": observed,
                "provider_state_as_of": raw.get("provider_state_as_of"),
                "availability_observed": eligible,
                **({"coverage_evidence": coverage_evidence} if coverage_evidence else {}),
                "study_eligible": eligible or (state == "partial" and raw.get("identity_only_partial") is True),
                "unresolved_players": raw.get("unresolved_players", raw.get("unmatched_players", 0)),
                "provider_identity_reviews": raw.get("provider_identity_reviews", []),
                "extractor_config_sha256": raw.get("extractor_config_sha256"),
                "fetch_complete": bool(raw.get("fetch_complete")),
                "pending_entries": raw.get("pending_entries", raw.get("provider_pending", 0)),
                "revision_conflicts": raw.get("revision_conflicts", 0),
                "unmatched_players": raw.get("unmatched_players", 0),
                "invalid_entries": counts.get("invalid_entries", 0),
                "rejected_items": raw.get("rejected_items", 0),
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


def _current_claims(facts, cutoff):
    """Resolve same-origin updates, collapse syndication and expose disagreements."""
    from . import news_model

    by_player = defaultdict(list)
    cut = store.instant(cutoff)
    for fact in facts:
        topic = "transfer" if fact["event"] == "transfer" else "availability"
        # An outdated standing feed is still auditable. It can neither clear a
        # separate club's fresh report nor tag it as disputed.
        isolated = (not news_scope.suspension_applies(fact, news_scope.competition(fact))
                    or (fact.get("source") == news_model.POLICY["wire_source"]
                        and not news_model.usable_wire_observation(fact, cut)))
        by_player[(team_key(fact["team"]), fact["player_id"], topic, fact["id"] if isolated else "")].append(fact)
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
            states = {(f["event"], f["availability"]) for f in official if f["effective_at"] == last_official}
            # An older, still-reaffirmed identical wire absence carries its episode
            # start. The club determines confidence when it confirms the same injury.
            retained = [f for f in retained if f["effective_at"] >= last_official
                        or (f.get("source") == news_model.POLICY["wire_source"]
                            and (f["event"], f["availability"]) in states)]
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
            cluster = store.digest([first["fixture_id"], *group[:3], *kind])
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
                """SELECT f.*,r.recorded_at,r.precision FROM fixture_versions f JOIN schedule_receipts r USING(fixture_id,version)
                WHERE f.fixture_id=? AND f.known_at<=? AND r.recorded_at<=?
                ORDER BY f.known_at DESC LIMIT 1""",
                (fixture_id, at, at),
            ).fetchone()
            if not game:
                return _empty_snapshot(fixture_id, at, "no_contemporaneous_schedule")
            if "news_articles" not in tables:
                return _empty_snapshot(fixture_id, at, "news_not_collected", game)
            if game["precision"] == "date":
                return _empty_snapshot(fixture_id, at, "no_precise_schedule", game)
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
        timetables = _timetables(_schedule(db, at, game["season"]), news_scope.competition(game))
        club_games = {team_key(game[side]): timetables[game["season"], team_key(game[side])]
                      for side in ("home", "away")}
        coverage = {side: _side_coverage(db, data_dir, game, side, at, club_games[team_key(game[side])])
                    for side in ("home", "away")}
        if "news_analysis_runs" in tables:
            # 0.4.0 persisted a count for candidates it discarded under the old
            # kickoff. Until a new analysis really completes they remain unknown.
            gaps = set(map(tuple, db.execute(
                """SELECT DISTINCT a.team_key,a.source FROM news_articles a
                JOIN news_analysis_runs x ON x.seq=(SELECT z.seq FROM news_analysis_runs z
                    WHERE z.article=a.seq AND z.known_at<=? ORDER BY z.known_at DESC,z.seq DESC LIMIT 1)
                WHERE a.fixture_id=? AND a.kickoff<>? AND a.recorded_at<=? AND a.retracted=0
                AND json_extract(x.metadata,'$.fixture_scope_excluded')>0 AND """ + _NEWER,
                (at, fixture_id, game["kickoff"], at, at, at),
            )))
            for side in coverage.values():
                for src in side["sources"]:
                    if src["availability_observed"] and (team_key(side["team"]), src["id"]) in gaps:
                        src.update(status="scope_reanalysis_pending", availability_observed=False)
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
        args = (fixture_id, at, at, at, at, at, at, at)
        if "news_analysis_runs" in tables:
            run_join = " LEFT JOIN news_claim_runs cr ON cr.claim=c.id "
            run_filter = """ AND coalesce(cr.run,0)=coalesce((SELECT x.seq FROM news_analysis_runs x
                WHERE x.article=a.seq AND x.known_at<=? ORDER BY x.known_at DESC,x.seq DESC LIMIT 1),0)"""
            args += (at,)
        rows = db.execute(
            """SELECT c.id,c.payload,a.seq AS article_seq,a.original_sha256 FROM news_articles a
            JOIN news_extractions e ON a.seq=e.article JOIN news_claims c ON c.article=a.seq"""
            + run_join
            + """ LEFT JOIN news_feed_config f ON f.id=a.source
            WHERE a.fixture_id=? AND a.retracted=0 AND a.published_at<=?
            AND a.received_at<=? AND a.recorded_at<=? AND e.known_at<=?
            AND (f.disabled_at IS NULL OR f.disabled_at>?) AND """
            + _NEWER
            + run_filter,
            args,
        ).fetchall()
        from . import news_ai, news_importance, news_wire_identity

        # Reopen the latest scoped native directory at the requested cutoff.
        # A later transfer, withdrawn alias or corrupt file cannot be used to
        # preserve an older single-provider identity by accident.
        checked = {}
        proof_rows = defaultdict(list)
        for row in rows:
            stored = json.loads(row["payload"])
            if stored.get("provider_identity"):
                proof_rows[team_key(stored["team"])].append(stored["provider_identity"])
        for side in ("home", "away"):
            club = team_key(game[side])
            wire_source = next((src for src in coverage[side]["sources"]
                                if src["id"] == "touchline-availability"), None)
            reviews = wire_source["provider_identity_reviews"] if wire_source else []
            if not reviews and not proof_rows[club]:
                continue
            members = squads[club]["players"]
            aliases = news_ai._roster_index(members)[1]
            directory = news_wire_identity.directory(db, data_dir, game, game[side], at, members)
            extra, reviewed_members = {}, {}
            for item in [*reviews, *proof_rows[club]]:
                key = store.digest(item)
                if key in checked:
                    continue
                good = news_wire_identity.revalidates(data_dir, item, directory, aliases)
                checked[key] = good
                if good:
                    member = directory[item["provider_player_id"]]
                    if item["identity_basis"] == "touchline_directory_id":
                        extra[member["player_id"]] = member
                    else:
                        reviewed_members[member["player_id"]] = {
                            "provider_player_id": member["provider_player_id"],
                            "identity_basis": member["identity_basis"],
                            "directory_sha256": member["original_sha256"],
                            "alias_receipt": member.get("alias_receipt"),
                        }
            if wire_source and reviews:
                wire_source["directory_evidence_verified"] = all(checked[store.digest(x)] for x in reviews)
                if not wire_source["directory_evidence_verified"]:
                    wire_source["study_eligible"] = False
            eligible[club].update(extra)
            squads[club] = dict(squads[club], players=[
                *(p | reviewed_members.get(p["player_id"], {}) for p in members), *extra.values()
            ])
        importance = news_importance.player_history(db, game, at, squads, data_dir) if rows else {}
        witnesses = _wire_witnesses(data_dir, db, game, at, rows) if rows else {}
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
        antecedent = fact.get("antecedent") or {}
        if (
            not raw
            or fact["original_sha256"] != sha
            or store.digest(fact) != row["id"]
            or raw["text"][fact["start"] : fact["end"]] != fact["quote"]
            or (antecedent and raw["text"][antecedent["start"]:antecedent["end"]] != antecedent["quote"])
        ):
            bad_sources.add((team_key(fact["team"]), fact["source"]))
            continue
        club = team_key(fact["team"])
        wire = {}
        if fact["source"] == "touchline-availability" and raw.get("time_basis") != "provider_snapshot":
            continue
        if raw.get("time_basis") == "provider_snapshot":
            if not news_scope.wire_coverage(fact["team"], game["season"], news_scope.competition(game),
                                            raw["observed_state_as_of"]):
                continue
            fields = raw.get("source_fields") or {}
            wire = {
                **{k: fields[k] for k in ("suspension_competition", "injury_started_at", "injury_date") if k in fields},
                "first_seen_at": fact["received_at"],
                "last_seen_at": fact["received_at"],
                "last_seen_state_as_of": raw["observed_state_as_of"],
                "chance_of_playing": (raw.get("source_fields") or {}).get("chance_of_playing"),
                "provider_player_id": raw.get("provider_player_id"),
            } | witnesses.get(row["article_seq"], {})
        scope_at = wire.get("last_seen_state_as_of") or fact["published_at"]
        target = _target_for(scope_at, club_games.get(club, ()), at)
        if (not target or target["fixture_id"] != fixture_id or not fixture_relevant(
                fact["quote"], raw["title"], game, fact["team"], antecedent=antecedent.get("quote", ""))):
            continue
        player = eligible.get(club, {}).get(str(fact["player_id"]))
        identity = fact.get("provider_identity")
        if player is None or (identity and not checked.get(store.digest(identity))):
            for side in ("home", "away"):
                if club == team_key(game[side]):
                    coverage[side]["roster"]["excluded_claims"] += 1
            continue
        facts.append(
            fact
            | wire
            | {
                "id": row["id"],
                "recorded_kickoff": fact["kickoff"],
                "kickoff": game["kickoff"],
                "player_importance": importance.get(str(fact["player_id"])),
                "effective_at": fact.get("effective_at", fact["published_at"]),
                "roster_validated_at": squads[club]["seen_at"],
                "active_roster_sha256": squads[club]["digest"],
                **({"directory_identity_validated_at": player["seen_at"],
                    "active_directory_sha256": player["original_sha256"]} if
                    identity and player.get("identity_basis") == "touchline_directory_id" else {}),
            }
        )
    # Two independently named active absences must not be added twice if
    # the source-only claimant still has a plausible unresolved roster alias.
    active_names = {(team_key(f["team"]), f["player_id"]) for f in facts
                    if f["event"] in ("injury", "suspension", "return", "availability")
                    and news_scope.suspension_applies(f, news_scope.competition(game)) and f["status"] in ("reported", "confirmed")}
    distinct = []
    for fact in facts:
        club = team_key(fact["team"])
        person = eligible[club][fact["player_id"]]
        overlaps = [pid for pid in person.get("possible_roster_ids", ()) if (club, pid) in active_names]
        if overlaps and news_scope.suspension_applies(fact, news_scope.competition(game)):
            side = next(s for s in coverage.values() if team_key(s["team"]) == club)
            side["roster"]["excluded_claims"] += 1
            side["roster"].setdefault("possible_duplicate_claims", []).append(
                {"provider_player_id": person["provider_player_id"], "unresolved_roster_ids": overlaps})
        else:
            distinct.append(fact)
    facts = distinct
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
        for src in side["sources"]:
            if src["status"] != "partial":
                src["study_eligible"] = src["availability_observed"]
            elif not side["roster"]["eligible_players"] or side["roster"]["excluded_claims"]:
                src["study_eligible"] = False
        side["observed"] = any(s["availability_observed"] for s in side["sources"])
        side["study_qualified"] = any(s["study_eligible"] for s in side["sources"])
        for src in side["sources"]:
            if (src["id"] == "touchline-availability" and src["status"] == "partial" and
                    (src["unmatched_players"] or src["invalid_entries"] or src["rejected_items"]
                     or src["revision_conflicts"])):
                side["coverage_gap"] = "incomplete_availability_feed"
                side["observed"] = False
                side["study_qualified"] = bool(src["study_eligible"])
    observed = sum(side["observed"] for side in coverage.values())
    qualified = sum(side["study_qualified"] for side in coverage.values())
    coverage.update(observed_teams=observed, both_teams_observed=observed == 2,
                    study_qualified_teams=qualified, both_teams_study_qualified=qualified == 2)
    return {
        "fixture_id": fixture_id,
        "cutoff": at,
        "schedule": {k: game[k] for k in ("version", "known_at", "recorded_at", "kickoff", "precision")},
        "facts": _current_claims(facts, at),
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
    if detail.get("unmatched_players"):
        return "unmatched_players", "invalid"
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


def weight_status(db, team=None):
    from . import news_weight_audit

    at = _utc()
    return news_weight_audit.current_usage(db, _schedule(db, at), at, team)


def _status_db(db, team=None):
    where = " WHERE s.team_key=?" if team else ""
    values = (team_key(team),) if team else ()
    rows = db.execute(
        """SELECT s.season,s.team,s.source,s.next_at,s.started,
      (SELECT json_object('first_checked_at',r.first_checked_at,'last_checked_at',r.last_checked_at,
                          'observations',r.observations) FROM news_poll_runs r
       WHERE r.season=s.season AND r.team_key=s.team_key AND r.source=s.source
       ORDER BY r.id DESC LIMIT 1) AS compact_run,
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
        archived = stream.pop("compact_run")
        if archived:
            stream["latest_compacted_run"] = json.loads(archived)
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
            "news_poll_proofs",
            "news_wire_polls",
            "news_wire_manifests",
        )
    }
    storage = news_poll_archive.inventory(db)
    counts["news_poll_proofs"] += storage["individually_replayable_checks_in_runs"] - storage["reopened_standalone_proofs"]
    counts["news_wire_polls"] += storage["wire_confirmations_in_runs"]
    return {
        "streams": streams,
        "archive_counts": counts,
        "poll_storage": storage,
        "player_weights": weight_status(db, team),
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
        "default": news_sources.active_sources(club, season_id),
        "registered": listed,
    }


def article(data_dir, sha):
    with _db(data_dir) as db:
        records = db.execute(
            """SELECT a.seq,a.season,a.team_key,a.fixture_id,p.id,p.payload FROM news_articles a LEFT JOIN news_article_proofs link
            ON link.article=a.seq LEFT JOIN news_poll_proofs p ON p.id=link.receipt
            WHERE a.original_sha256=?""",
            (sha,),
        ).fetchall()
        if not records:
            raise ValueError("unknown archived news document")
        polls = news_poll_archive.history(db, "news_wire_polls")
        # Restrict each history query to this fixture and team before looking
        # up membership. A single original may appear in multiple editions.
        confirmations = []
        for record in records:
            latest = db.execute(
                f"""SELECT s.first_seen_at,s.absence_first_seen_at,p.observed_at last_seen_at,
                p.provider_at last_seen_state_as_of,p.receipt last_seen_receipt,p.recorded_at,p.rowid AS wire_order,
                count(*) OVER () AS confirmations FROM news_wire_states s JOIN {polls} p
                ON p.fixture_id IS ? AND p.season=? AND p.team_key=?
                LEFT JOIN news_wire_manifests m ON m.id=p.articles
                WHERE s.article=? AND s.article IN (SELECT value FROM json_each(coalesce(m.articles,
                     CASE WHEN json_valid(p.articles) THEN p.articles END)))
                ORDER BY p.observed_at DESC,p.recorded_at DESC,p.rowid DESC LIMIT 1""",
                (record["fixture_id"],record["season"],record["team_key"],record["seq"]),
            ).fetchone()
            if latest:
                confirmations.append(dict(latest))
        standing = {}
        if confirmations:
            standing = max(confirmations, key=lambda r: (r["last_seen_at"],r["recorded_at"],r["wire_order"]))
            standing["confirmations"] = sum(r["confirmations"] for r in confirmations)
            del standing["recorded_at"], standing["wire_order"]
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
        **({"standing_state": dict(standing)} if standing else {}),
        "revision_conflicts": conflicts,
        "analyses": analyses,
    }


def register(subparsers):
    commands = subparsers.add_parser(
        "news", help="球队消息、球员核验和赛前配对研究"
    ).add_subparsers(required=True)
    from . import news_admission

    news_admission.add_commands(commands)
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
    from .news_wire_identity import register_alias
    alias = commands.add_parser("player-alias", help="登记或撤回单支球队本赛季的具名球员身份核对")
    alias.add_argument("team")
    alias.add_argument("season")
    alias.add_argument("provider_id", type=int)
    alias.add_argument("player_id")
    alias.add_argument("--reviewer", required=True)
    alias.add_argument("--reason", required=True)
    alias.add_argument("--withdraw", action="store_true")
    alias.set_defaults(func=lambda a: register_alias(a.data_dir, a.team, a.season, a.provider_id, a.player_id,
                                                     a.reviewer, a.reason, withdraw=a.withdraw))
    health = commands.add_parser("status")
    health.add_argument("--team")
    health.set_defaults(func=lambda a: status(a.data_dir, a.team))
    proof = commands.add_parser("receipt", help="按 SHA-256 核验单次新闻轮询的实际时间和响应摘要")
    proof.add_argument("sha256")
    def show_receipt(a):
        with _db(a.data_dir) as db:
            return news_poll_archive.receipt(db, a.sha256)
    proof.set_defaults(func=show_receipt)
    cold = commands.add_parser("retention", help="盘点已完赛且被取代的旧伤停版本，保留可重放证据")
    cold.add_argument("--days", type=int, default=90)
    cold.add_argument("--compact", action="store_true", help="在校验旧版 SHA-256 后压缩存储原件")
    cold.add_argument("--limit", type=int, default=500, help="每次最多压缩的原件数")
    cold.add_argument("--backup-to", help="另存并校验完整压缩冷备份")
    cold.set_defaults(func=lambda a: retention(a.data_dir, days=a.days, backup_to=a.backup_to,
                                               compact=a.compact, limit=a.limit))
    facts = commands.add_parser("facts", help="查看某场比赛当时已处理的球员消息和覆盖")
    facts.add_argument("fixture_id")
    facts.add_argument("--at")
    facts.set_defaults(
        func=lambda a: snapshot(a.data_dir, a.fixture_id, a.at or _utc())
    )
    original = commands.add_parser("article")
    original.add_argument("sha256")
    original.set_defaults(func=lambda a: article(a.data_dir, a.sha256))
    from . import news_era_replay

    news_era_replay.register(commands)
    ev = commands.add_parser("evaluate")
    ev.add_argument("season")
    ev.add_argument("round", type=int, nargs="?")
    ev.add_argument("--horizon", choices=("T-60m", "T-24h"), default="T-60m")
    ev.add_argument("--accumulate", action="store_true", help="accumulate declared prospective pairs from 2026-27 through this season, by probability era")
    ev.add_argument("--policy", help="single-season news policy, e.g. current, v1, or its exact digest")
    ev.add_argument(
        "--mode", choices=("technical", "formal", "replay"), default="technical"
    )

    def evaluate(a):
        from .news_evaluation import evaluate_accumulation, evaluate_round, evaluate_season

        if a.accumulate:
            if a.round is not None or a.policy is not None:
                raise ValueError("--accumulate takes a through-season and includes all declared eras separately")
            return evaluate_accumulation(a.data_dir, a.season, horizon=a.horizon, mode=a.mode)
        if a.round is None:
            return evaluate_season(a.data_dir, a.season, horizon=a.horizon, mode=a.mode, policy=a.policy)
        return evaluate_round(
            a.data_dir, a.season, a.round, horizon=a.horizon, mode=a.mode, policy=a.policy
        )

    ev.set_defaults(func=evaluate)
