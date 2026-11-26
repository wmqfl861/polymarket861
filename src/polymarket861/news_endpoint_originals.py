"""Physical dependencies of prospectively declared EPL news-endpoint observations."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from . import (
    football,
    independent_results,
    news_poll_archive,
    news_roster,
    news_sources,
    news_study_plan,
    store,
)
from .market_provider import Reply

BODY_SOURCE = "epl-endpoint-news-response"
# A grant for storing derived headlines, or a free-to-read feed, does not imply a
# licence to retain its full payload. Only the registered, explicitly open text
# feed is copied, never linked article bodies, photographs, or other endpoints.
OPEN_FEED_LICENSES = {"cc0", "cc0 1.0", "cc by 4.0", "cc by-sa 4.0"}
SCHEMA = """CREATE TABLE IF NOT EXISTS news_endpoint_poll_originals(
 receipt TEXT PRIMARY KEY, source TEXT NOT NULL, season TEXT NOT NULL, club TEXT NOT NULL,
 observed_at TEXT NOT NULL, response_sha TEXT NOT NULL, digest TEXT NOT NULL, document TEXT NOT NULL)"""
INDEX = """CREATE INDEX IF NOT EXISTS endpoint_poll_observation ON
 news_endpoint_poll_originals(source,season,club,observed_at,response_sha)"""


def retainable(config):
    return (
        config.get("free") is True
        and set(config.get("rights", ())) == set(news_sources.RIGHTS)
        and config.get("license", "").strip().casefold() in OPEN_FEED_LICENSES
        and config.get("kind") in {"feed", "wiki"}
    )


def _registration(root, task, started):
    if not retainable(task["config"]):
        return False
    with closing(
        sqlite3.connect(
            (Path(root) / "runtime.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
        )
    ) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='news_endpoint_declarations'"
        ).fetchone()
        if not exists:
            return False
        row = db.execute(
            "SELECT digest FROM news_endpoint_declarations WHERE json_extract(manifest,'$.registered_at')<=?"
            " AND ends_at>? LIMIT 1",
            (started, started),
        ).fetchone()
        return row[0] if row else None


def collect(root, task, poll, cursor, started):
    args = (task["config"], task["team"], task["season"], cursor)
    if (
        poll is not news_sources.poll
        or task["season"] not in {"2026-27", "2027-28"}
        or not (task["target"] or {})
        .get("fixture_id", "")
        .startswith(f"epl/{task['season']}/")
    ):
        return poll(*args), None
    registration = _registration(root, task, started)
    if not registration:
        return poll(*args), None
    replies = []

    def get(url):
        reply = news_sources._get(url)
        replies.append(reply)
        return reply

    result = poll(*args, getter=get)
    return result, (replies[0], registration) if len(replies) == 1 else None


def save(root, task, proof_id, proof, result, capture, cursor):
    if capture is None:
        return
    reply, registration = capture
    if (
        result["request_url"] != task["config"]["url"]
        or reply.url != result["request_url"]
        or result["response_sha256"] != proof["response_sha256"]
    ):
        raise ValueError("endpoint news capture scope mismatch")
    meta = {
        "receipt": proof_id,
        "source": task["source"],
        "season": task["season"],
        "team": task["team_key"],
        "url": reply.url,
        "license": task["config"]["license"],
        "license_url": task["config"]["license_url"],
        "registration": registration,
        "recorded_at": store.now_utc(),
    }
    archive = store.archive_bytes(
        root,
        BODY_SOURCE,
        reply.body,
        observed_at=proof["observed_at"],
        media_type="application/octet-stream",
        metadata=meta,
    )
    if archive["sha256"] != proof["response_sha256"]:
        raise ValueError("endpoint news native response digest mismatch")
    # Only cache-control affects the adapter. Other response headers can contain
    # network/session identifiers and are outside the experiment.
    headers = {
        k.lower(): v for k, v in reply.headers.items() if k.lower() == "cache-control"
    }
    manifest = {
        "metadata": meta,
        "config": task["config"],
        "cursor": cursor,
        "headers": headers,
        "parsed_sha256": store.digest(result),
    }
    with closing(store.connect(root)) as db, db:
        db.execute(SCHEMA)
        db.execute(INDEX)
        db.execute(
            "INSERT OR IGNORE INTO news_endpoint_poll_originals VALUES(?,?,?,?,?,?,?,?)",
            (
                proof_id,
                task["source"],
                task["season"],
                task["team_key"],
                proof["observed_at"],
                archive["sha256"],
                store.digest(manifest),
                store.canonical(manifest),
            ),
        )


def _require(test, reason):
    if not test:
        raise ValueError(reason)


def _roster(db, root, team, season, reference):
    digest, seen = reference["digest"], reference["seen_at"]
    row = db.execute(
        """SELECT c.document FROM news_roster_receipts r JOIN news_roster_content c USING(digest)
                       WHERE r.season=? AND r.club=? AND r.seen_at=? AND r.digest=?""",
        (season, football.team_key(team), seen, digest),
    ).fetchone()
    _require(row, "missing registered roster receipt")
    doc = json.loads(row[0])
    title = news_roster._title(team, football.season_year(season))
    _require(
        store.digest(doc) == digest
        and doc["team"] == team
        and doc["template"] == title
        and doc["source"] == news_roster.SOURCE
        and doc["season"] == season
        and doc["attribution"] == news_roster.ATTRIBUTION
        and doc["license"] == independent_results.LICENSE
        and doc["source_url"]
        == f"https://en.wikipedia.org/w/index.php?oldid={doc['revision']}",
        "roster identity mismatch",
    )

    def original(sha, url, role, earliest=None):
        metadata = {
            "url": url,
            "role": role,
            "attribution": news_roster.ATTRIBUTION,
            "license": independent_results.LICENSE,
        }
        receipt = db.execute(
            """SELECT observed_at FROM observations WHERE source=? AND sha256=? AND metadata=?
                         AND observed_at<=? AND (? IS NULL OR observed_at>=?) ORDER BY observed_at DESC LIMIT 1""",
            (
                news_roster.SOURCE,
                sha,
                store.canonical(metadata),
                seen,
                earliest,
                earliest,
            ),
        ).fetchone()
        _require(receipt, "missing scoped roster source receipt")
        return store.read_blob(root, sha), receipt[0]

    raw, stamp = original(
        doc["squad_sha256"], independent_results._url([title]), "squad"
    )
    revision, published, links, unlinked = news_roster.parse_squad(
        raw, title, team, stamp
    )
    _require(
        revision == doc["revision"]
        and published == doc["published_at"]
        and doc["membership_sha256"]
        == store.digest(
            {"links": {k: sorted(v) for k, v in links.items()}, "unlinked": unlinked}
        ),
        "roster revision mismatch",
    )
    players, unresolved = {}, []
    names = sorted(links)
    batches = [
        {k: links[k] for k in names[offset : offset + 45]}
        for offset in range(0, len(names), 45)
    ]
    _require(
        len(batches) == len(doc["identity_sha256"]), "missing roster identity batch"
    )
    for batch, sha in zip(batches, doc["identity_sha256"], strict=True):
        raw, stamp = original(
            sha, news_roster._identity_url(batch), "player_identities", stamp
        )
        people, missing = news_roster._people(raw, batch)
        _require(not players.keys() & people.keys(), "duplicate native identity")
        players.update(people)
        unresolved.extend(missing)
    _require(
        [players[k] for k in sorted(players)] == doc["players"]
        and sorted(unresolved) == doc["unresolved_titles"]
        and unlinked == doc["unlinked_player_rows"],
        "roster projection differs from native identities",
    )


def _verified_poll(db, root, ident, season, team, source, cutoff, cache):
    key = (ident, season, team, source)
    if key in cache:
        return cache[key]
    proof = news_poll_archive.receipt(db, ident)
    _require(
        (proof["source"], proof["season"], proof["team"])
        == (source, season, football.team_key(team))
        and proof["hash_basis"] == "upstream_response_bytes"
        and proof["requested_at"]
        <= proof["received_at"]
        <= proof["observed_at"]
        <= cutoff,
        "invalid original poll identity or times",
    )
    row = db.execute(
        "SELECT * FROM news_endpoint_poll_originals WHERE receipt=?", (ident,)
    ).fetchone()
    _require(
        row
        and (
            row["source"],
            row["season"],
            row["club"],
            row["observed_at"],
            row["response_sha"],
        )
        == (
            source,
            season,
            proof["team"],
            proof["observed_at"],
            proof["response_sha256"],
        ),
        "missing private retained response",
    )
    doc = json.loads(row["document"])
    recorded = store.iso(doc["metadata"]["recorded_at"])
    reg = db.execute(
        "SELECT digest,manifest,ends_at FROM news_endpoint_declarations WHERE digest=?",
        (doc["metadata"]["registration"],),
    ).fetchone()
    _require(
        reg
        and store.digest(json.loads(reg["manifest"])) == reg["digest"]
        and json.loads(reg["manifest"])["registered_at"]
        <= proof["observed_at"]
        <= recorded
        <= cutoff
        and recorded < reg["ends_at"],
        "response was not retained contemporaneously under this registration",
    )
    config = news_sources.validate_source(doc["config"])
    subscribed = db.execute(
        """SELECT config FROM news_streams WHERE season=? AND team_key=? AND source=?
           AND registered_at<=?""",
        (season, proof["team"], source, proof["requested_at"]),
    ).fetchone()
    _require(
        subscribed
        and json.loads(subscribed[0]) == config
        and doc["config"] == config
        and retainable(config)
        and config["team"] == team
        and config["id"] == source
        and config["url"] == proof["request_url"]
        and store.digest(doc) == row["digest"],
        "source configuration or parse proof mismatch",
    )
    meta = {
        "receipt": ident,
        "source": source,
        "season": season,
        "team": proof["team"],
        "url": proof["request_url"],
        "license": config["license"],
        "license_url": config["license_url"],
        "registration": reg["digest"],
        "recorded_at": recorded,
    }
    _require(
        meta == doc["metadata"]
        and db.execute(
            "SELECT 1 FROM observations WHERE source=? AND sha256=? AND observed_at=? AND metadata=?",
            (
                BODY_SOURCE,
                row["response_sha"],
                proof["observed_at"],
                store.canonical(meta),
            ),
        ).fetchone(),
        "missing original response observation",
    )
    raw = store.read_blob(root, row["response_sha"])
    reply = Reply(
        raw,
        proof["requested_at"],
        proof["received_at"],
        doc["headers"],
        proof["request_url"],
    )
    parsed = news_sources.poll(
        config, team, season, doc["cursor"], getter=lambda _: reply
    )
    _require(
        store.digest(parsed) == doc["parsed_sha256"]
        and parsed["response_sha256"] == proof["response_sha256"],
        "original adapter projection mismatch",
    )
    cache[key] = (proof, parsed, config)
    return cache[key]


def _coverage_poll(db, root, fixture, side, source, cutoff, cache):
    history = news_poll_archive.history(db, "news_checks")
    row = db.execute(
        f"""SELECT * FROM {history} WHERE fixture_id=? AND season=? AND team_key=? AND source=?
                      AND checked_at<=? ORDER BY checked_at DESC,id DESC LIMIT 1""",
        (
            fixture["fixture_id"],
            fixture["season"],
            football.team_key(side["team"]),
            source["id"],
            cutoff,
        ),
    ).fetchone()
    _require(
        row
        and row["checked_at"] == source["checked_at"]
        and row["kickoff"] == source["recorded_kickoff"],
        "missing fixture source-coverage check",
    )
    detail = json.loads(row["detail"])
    observed = source["source_observed_at"]
    _require(
        detail["checked_at"] == row["checked_at"]
        and detail["source_observed_at"] == observed
        and detail["response_hash_basis"] == "upstream_response_bytes"
        and observed <= row["checked_at"],
        "invalid coverage-check receipt",
    )
    original = db.execute(
        """SELECT receipt FROM news_endpoint_poll_originals WHERE source=? AND season=?
                      AND club=? AND observed_at=? AND response_sha=?""",
        (
            source["id"],
            fixture["season"],
            row["team_key"],
            observed,
            detail["response_sha256"],
        ),
    ).fetchall()
    _require(len(original) == 1, "missing or ambiguous original for source coverage")
    proof, parsed, config = _verified_poll(
        db,
        root,
        original[0][0],
        fixture["season"],
        side["team"],
        source["id"],
        cutoff,
        cache,
    )
    _require(
        proof["observed_at"] == observed
        and config["origin"] == source["origin"]
        and parsed.get("invalid_entries", 0) == source.get("invalid_entries", 0)
        and bool(parsed.get("baseline_only"))
        == bool(detail.get("provider_counts", {}).get("baseline_only")),
        "source coverage differs from its parsed response",
    )


def _fact(db, root, ident, fixture, cutoff, cache, rosters):
    from . import news

    row = db.execute(
        """SELECT c.payload,a.original_sha256,a.season,a.team_key,a.source,a.recorded_at,
                       a.received_at,p.receipt,r.known_at,r.policy,r.roster_sha256,r.metadata
                       FROM news_claims c JOIN news_articles a ON a.seq=c.article JOIN news_article_proofs p ON p.article=a.seq
                       JOIN news_claim_runs l ON l.claim=c.id JOIN news_analysis_runs r ON r.seq=l.run AND r.article=a.seq
                       WHERE c.id=? AND a.fixture_id=?""",
        (ident, fixture["fixture_id"]),
    ).fetchone()
    _require(row, "missing native article/extraction provenance")
    fact = json.loads(row["payload"])
    _require(
        store.digest(fact) == ident
        and fact["original_sha256"] == row["original_sha256"]
        and fact["source_receipt"] == row["receipt"]
        and fact["roster_sha256"] == row["roster_sha256"]
        and fact["analysis_policy"] == row["policy"]
        and fact["extractor_config_sha256"]
        == json.loads(row["metadata"])["extractor_config_sha256"]
        and fact["known_at"] == row["known_at"]
        and row["received_at"] <= row["recorded_at"] <= row["known_at"] <= cutoff,
        "archived extraction differs from sealed fact",
    )
    proof, parsed, config = _verified_poll(
        db,
        root,
        row["receipt"],
        fixture["season"],
        fact["team"],
        row["source"],
        cutoff,
        cache,
    )
    _require(
        proof["response_sha256"] == fact["response_sha256"]
        and fact["response_hash_basis"] == proof["hash_basis"],
        "article receipt differs from extraction",
    )
    original = json.loads(store.read_blob(root, row["original_sha256"]))
    candidates = [
        item
        for item in parsed["articles"]
        if item.get("external_id") == fact["external_id"]
        and str(item.get("revision")) == fact["revision"]
    ]
    _require(
        any(
            news._document(item, config, proof["observed_at"]) == original
            for item in candidates
        ),
        "article absent from original source response",
    )
    # Extraction may have used an earlier roster than the one at the prediction cutoff.
    team = fact["team"]
    history = news_roster.roster_views(db, [team], fixture["season"], row["known_at"])[
        football.team_key(team)
    ]
    _require(
        store.digest(history["players"]) == fact["roster_sha256"],
        "historical extraction roster mismatch",
    )
    roster_key = (team, history["digest"], history["seen_at"])
    if roster_key not in rosters:
        _roster(db, root, team, fixture["season"], history)
        rosters.add(roster_key)


def verify(db, root, fixture, snapshot):
    """Re-open only originals used for identified news or positive source coverage."""
    cutoff = snapshot["cutoff"]
    cache, rosters = {}, set()
    try:
        for label in ("home", "away"):
            side = snapshot["coverage"][label]
            sources = [s for s in side["sources"] if news_study_plan._study_source(s)]
            reference = side["roster"]
            # Empty/unknown rosters that contributed no eligibility or player are not dependencies.
            if sources and reference.get("state") == "available":
                _roster(db, root, side["team"], fixture["season"], reference)
                rosters.add((side["team"], reference["digest"], reference["seen_at"]))
            for source in sources:
                _coverage_poll(db, root, fixture, side, source, cutoff, cache)
        for fact in snapshot["facts"]:
            for ident in set(fact.get("evidence_ids", ())) | {fact["id"]}:
                _fact(db, root, ident, fixture, cutoff, cache, rosters)
        return True
    except (KeyError, IndexError, TypeError, OSError, ValueError, sqlite3.Error):
        return False
