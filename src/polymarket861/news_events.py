"""Append a prospective research forecast when a fixture's player news changes."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path

from . import (
    dbscope,
    football,
    input_manifest,
    news,
    news_model,
    paired_proof,
    pipeline,
    store,
)

PROTOCOL = "epl-news-event-v1"
LEASE = timedelta(minutes=4)
RETRY = timedelta(minutes=2)
RECHECK = timedelta(minutes=20)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_event_work(
 fixture_id TEXT NOT NULL,kickoff TEXT NOT NULL,ticket INTEGER NOT NULL,owner TEXT,
 lease_until TEXT NOT NULL DEFAULT '',cursor TEXT NOT NULL DEFAULT '',review_at TEXT NOT NULL DEFAULT '',
 retry_at TEXT NOT NULL DEFAULT '',retry_manifest TEXT NOT NULL DEFAULT '',last_error TEXT,
 PRIMARY KEY(fixture_id,kickoff));
CREATE TABLE IF NOT EXISTS news_event_updates(
 seq INTEGER PRIMARY KEY,fixture_id TEXT NOT NULL,kickoff TEXT NOT NULL,revision INTEGER NOT NULL,
 previous_digest TEXT REFERENCES news_event_updates(digest),digest TEXT NOT NULL UNIQUE,
 state_sha256 TEXT NOT NULL,document TEXT NOT NULL,UNIQUE(fixture_id,kickoff,revision));
"""


def _now():
    return store.iso(store.now_utc())


@contextmanager
def _writer(root):
    with news._db(root) as db:
        dbscope.prepare(db, "event-news", _SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        yield db


def _catalog(db):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='football_input_manifests'"
    ).fetchone():
        return ""
    return str(
        db.execute(
            "SELECT coalesce(max(rowid),0) FROM football_input_manifests"
        ).fetchone()[0]
    )


def _cursor(db, fixture, at):
    # A newer revision can target another fixture but still supersede this team's
    # original article. Delayed extraction, conflict quarantine and feed withdrawal
    # also change what is admissible without adding an article for this fixture.
    keys = [football.team_key(fixture[k]) for k in ("home", "away")]
    rows = db.execute(
        """SELECT a.team_key,a.source,max(a.seq),
         max((SELECT max(x.seq) FROM news_analysis_runs x WHERE x.article=a.seq AND x.known_at<=?)),
         max((SELECT max(v.rowid) FROM news_revision_conflicts v WHERE v.article=a.seq AND v.recorded_at<=?)),
         max(CASE WHEN f.disabled_at<=? THEN f.disabled_at END)
       FROM news_articles a LEFT JOIN news_feed_config f ON f.id=a.source
       WHERE a.season=? AND a.team_key IN (?,?) AND a.recorded_at<=?
       GROUP BY a.team_key,a.source ORDER BY a.team_key,a.source""",
        (at, at, at, fixture["season"], *keys, at),
    )
    return store.digest([list(r) for r in rows])


def _claim(root, limit):
    with _writer(root) as db:
        at = _now()
        relevant = {
            tuple(row)
            for row in db.execute(
                """SELECT DISTINCT fixture_id,kickoff FROM news_articles
            WHERE fixture_id IS NOT NULL AND kickoff>? AND recorded_at<=?
            UNION SELECT fixture_id,kickoff FROM news_event_work WHERE kickoff>?""",
                (at, at, at),
            )
        }
        games = (
            [
                f
                for f in news._schedule(db, at)
                if f["kickoff"] > at
                and f["precision"] != "date"
                and (f["fixture_id"], f["kickoff"]) in relevant
            ]
            if relevant
            else []
        )
        due = []
        manifest = _catalog(db)
        for f in games:
            key = (f["fixture_id"], f["kickoff"])
            job = db.execute(
                "SELECT * FROM news_event_work WHERE fixture_id=? AND kickoff=?", key
            ).fetchone()
            if job and (
                (job["owner"] and job["lease_until"] > at)
                or (job["retry_at"] > at and job["retry_manifest"] == manifest)
            ):
                continue
            cursor = _cursor(db, f, at)
            if (
                job
                and not job["last_error"]
                and job["cursor"] == cursor
                and job["review_at"] > at
            ):
                continue
            due.append((job["ticket"] if job else 0, f, cursor))
        due.sort(key=lambda item: (item[0], item[1]["kickoff"], item[1]["fixture_id"]))
        ticket = db.execute(
            "SELECT coalesce(max(ticket),0) FROM news_event_work"
        ).fetchone()[0]
        tasks = []
        for i, (_, f, cursor) in enumerate(due[:limit], 1):
            owner = uuid.uuid4().hex
            key = (f["fixture_id"], f["kickoff"])
            db.execute(
                """INSERT INTO news_event_work(fixture_id,kickoff,ticket,owner,lease_until) VALUES(?,?,?,?,?)
               ON CONFLICT(fixture_id,kickoff) DO UPDATE SET ticket=excluded.ticket,owner=excluded.owner,
               lease_until=excluded.lease_until""",
                (*key, ticket + i, owner, store.iso(store.instant(at) + LEASE)),
            )
            tasks.append(f | {"owner": owner, "cursor": cursor})
        return tasks, len(games), len(due)


def _semantic(fixture, snapshot):
    cut = store.instant(snapshot["cutoff"])
    clubs = {football.team_key(fixture[side]): side for side in ("home", "away")}
    chosen, excluded = news_model._active_claims(
        snapshot["facts"], cut, clubs, news_model.LOCAL, fixture
    )
    # A renewed bulletin, a synonym, a new receipt, or the same extraction under
    # a new roster/model do not represent new player information. Keep neutral
    # rumours and reversals visible, but drop obsolete claims they supersede.
    states = set()
    for item in chosen:
        states.add(
            (
                item["side"],
                item["player_id"],
                item["event"],
                item["status"],
                item["availability"],
                news_model._role(item["position"]),
                "active",
            )
        )
    for item in excluded:
        reason = item["reason"]
        if reason == "availability_restored_or_disputed" and not (
            item["status"] == "denied" or item["availability"] == "available"
        ):
            continue
        if reason not in {
            "unconfirmed_rumour",
            "availability_restored_or_disputed",
            "unresolved_conflict",
            "conflicting_current_availability",
        } and not (
            item.get("event") == "transfer"
            and item.get("status") == "rumour"
            and reason == "unsupported_event"
        ):
            continue
        states.add(
            (
                item["side"],
                item["player_id"],
                item["event"],
                item["status"],
                item["availability"],
                None,
                reason,
            )
        )
    fields = ("side", "player_id", "event", "status", "availability", "role", "state")
    return [dict(zip(fields, v)) for v in sorted(states, key=store.canonical)]


def _review(snapshot):
    cut = store.instant(snapshot["cutoff"])
    deadlines = [cut + RECHECK]
    age = timedelta(days=news_model.POLICY["max_publication_age_days"], microseconds=1)
    for fact in snapshot["facts"]:
        end = fact.get("expires_at") or fact.get("valid_until")
        if end and store.instant(end) > cut:
            deadlines.append(store.instant(end))
        deadline = store.instant(fact["published_at"]) + age
        if deadline > cut:
            deadlines.append(deadline)
    return store.iso(min(deadlines))


def _head(db, key):
    return db.execute(
        "SELECT * FROM news_event_updates WHERE fixture_id=? AND kickoff=? ORDER BY revision DESC LIMIT 1",
        key,
    ).fetchone()


def _verified(row):
    doc = json.loads(row["document"])
    if store.digest(doc) != row["digest"] or any(
        doc[k] != row[k]
        for k in (
            "fixture_id",
            "kickoff",
            "revision",
            "state_sha256",
            "previous_digest",
        )
    ):
        raise ValueError("news event archive failed content verification")
    return doc | {"digest": row["digest"]}


def _history(root, history_root):
    observed = input_manifest.verify_observed_inputs(root, history_root)
    with closing(paired_proof._open(root)) as db:
        row = db.execute(
            "SELECT observed_at,manifest FROM football_input_manifests WHERE digest=?",
            (observed["digest"],),
        ).fetchone()
        doc = json.loads(row["manifest"])
        if not paired_proof._verified_scope(
            db, observed["digest"], doc["files"], doc["evidence_sha256"]
        ):
            raise ValueError("football history lacks verified provider objects")
    history = football.load_fixtures(
        Path(observed["root"]), evidence_path=Path(observed["evidence_path"])
    )
    return (
        observed["digest"],
        store.iso(row["observed_at"]),
        football.FootballEngine(history),
        pipeline._build_recipe("B3", football.DEFAULT_LINES),
    )


def _report(root, task, previous, cache, history_root):
    fixture = store.fixture_at(root, task["fixture_id"], prospective=True)
    if fixture["kickoff"] != task["kickoff"] or fixture["precision"] == "date":
        raise ValueError("fixture schedule changed")

    def sample():
        cut = _now()
        if cut >= fixture["kickoff"]:
            raise ValueError("fixture already started")
        snapshot = news.snapshot(root, fixture["fixture_id"], cut)
        if snapshot["cutoff"] != cut or snapshot["fixture_id"] != fixture["fixture_id"]:
            raise ValueError("news snapshot cutoff or fixture changed")
        return cut, snapshot, _semantic(fixture, snapshot)

    def same(state):
        return (
            store.digest(state) == previous["state_sha256"] if previous else not state
        )

    cut, snapshot, state = sample()
    if same(state):
        return None, _review(snapshot)
    if "history" not in cache:
        cache["history"] = _history(root, history_root)
    manifest, seen, engine, recipe = cache["history"]
    # Verification may materialize a newly adjudicated manifest. Freeze news and
    # the decision time together after that manifest is actually available.
    cut, snapshot, state = sample()
    if seen > cut:
        raise ValueError("football history was unavailable at decision time")
    if same(state):
        return None, _review(snapshot)
    model, pair = pipeline._forecast_arms(
        engine,
        fixture["home"],
        fixture["away"],
        store.instant(fixture["kickoff"]),
        store.instant(cut),
        fixture["season"],
        recipe,
        {"player_news": snapshot},
        include_pair=True,
    )
    evidence = model["evidence"]
    if (
        model["fixture_id"] != fixture["fixture_id"]
        or not evidence["result_grades"]
        or not set(evidence["result_grades"]) <= {"archived_upper_bound", "observed"}
        or store.iso(evidence["latest_result_available_at"]) > cut
    ):
        raise ValueError("unverified football fixture or training history")
    return {
        **{
            k: fixture[k]
            for k in ("fixture_id", "season", "round_id", "home", "away", "kickoff")
        },
        "protocol": PROTOCOL,
        "purpose": "event_research",
        "cutoff": cut,
        "schedule_version": fixture["version"],
        "schedule_recorded_at": fixture["recorded_at"],
        "state": state,
        "state_sha256": store.digest(state),
        "trigger": "first_report"
        if not previous
        else "news_changed"
        if state
        else "news_removed",
        "snapshot": snapshot,
        "news_pair": pair,
        "history_manifest_sha256": manifest,
        "history_observed_at": seen,
        "training_sha256": pair["base_training_sha256"],
        "training_evidence": evidence,
        "recipe_sha256": store.digest(recipe),
        "lines": recipe["lines"],
        "fallback_reason": model.get("fallback_reason"),
    }, _review(snapshot)


def _run(root, task, cache, history_root):
    key = task["fixture_id"], task["kickoff"]
    basis = {"fixture_id": key[0], "kickoff": key[1]}
    try:
        with closing(store.connect(root)) as db:
            row = _head(db, key)
            previous = _verified(row) if row else None
        doc, review = _report(root, task, previous, cache, history_root)
        with _writer(root) as db, dbscope.bind(root, db):
            at = _now()
            own = db.execute(
                "SELECT owner FROM news_event_work WHERE fixture_id=? AND kickoff=?",
                key,
            ).fetchone()
            if own[0] != task["owner"]:
                return basis | {
                    "status": "deferred",
                    "reason": "another worker owns this fixture",
                }
            latest = store.fixture_at(root, key[0], at=at, prospective=True)
            if (
                latest["kickoff"] != key[1]
                or latest["precision"] == "date"
                or at >= key[1]
                or (doc and latest["version"] != doc["schedule_version"])
            ):
                raise ValueError("fixture changed or already started")
            store.ensure_unfinished_at(db, *key, at)
            if _cursor(db, task, at) != task["cursor"]:
                raise ValueError("news changed during calculation")
            head = _head(db, key)
            if (head["digest"] if head else None) != (
                previous["digest"] if previous else None
            ):
                raise ValueError("previous event changed during calculation")
            item = basis | {"status": "unchanged"}
            if doc:
                # Computing cannot legitimately cross a validity deadline or go
                # back in time. Re-evaluate at the new cutoff on the next pass.
                if not doc["cutoff"] <= at < min(review, key[1]):
                    raise ValueError("news validity changed during calculation")
                doc.update(
                    created_at=at,
                    revision=previous["revision"] + 1 if previous else 1,
                    previous_digest=previous["digest"] if previous else None,
                )
                ident = store.digest(doc)
                db.execute(
                    """INSERT INTO news_event_updates(fixture_id,kickoff,revision,previous_digest,digest,state_sha256,document)
                    VALUES(?,?,?,?,?,?,?)""",
                    (
                        *key,
                        doc["revision"],
                        doc["previous_digest"],
                        ident,
                        doc["state_sha256"],
                        store.canonical(doc),
                    ),
                )
                store.put_report(db, "news-update", ident, doc | {"digest": ident})
                if _now() >= min(review, key[1]):
                    raise ValueError("news validity changed during calculation")
                item.update(status="published", digest=ident, revision=doc["revision"])
            db.execute(
                "UPDATE news_event_work SET owner=NULL,lease_until='',cursor=?,review_at=?,retry_at='',retry_manifest='',last_error=NULL WHERE fixture_id=? AND kickoff=?",
                (task["cursor"], review, *key),
            )
        return item
    except (ValueError, KeyError, OSError, sqlite3.OperationalError) as exc:
        reason = str(exc)
        with _writer(root) as db:
            at = store.instant(_now())
            # A newly arrived story should be eligible on the very next pass.
            retry = at if reason == "news changed during calculation" else at + RETRY
            db.execute(
                "UPDATE news_event_work SET owner=NULL,lease_until='',retry_at=?,retry_manifest=?,last_error=? WHERE fixture_id=? AND kickoff=? AND owner=?",
                (store.iso(retry), _catalog(db), reason, *key, task["owner"]),
            )
        return basis | {"status": "deferred", "reason": reason}


def refresh(data_dir, *, limit=2, history_root=None):
    """Process at most limit prospective fixtures. Retries survive restarts."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("event refresh limit must be from 1 through 100")
    tasks, eligible, due = _claim(data_dir, limit)
    cache = {}
    items = [_run(data_dir, t, cache, history_root) for t in tasks]
    counts = {
        s: sum(i["status"] == s for i in items)
        for s in ("published", "unchanged", "deferred")
    }
    return {
        "state": "idle" if not tasks else "partial" if counts["deferred"] else "ok",
        "eligible": eligible,
        "due": due,
        "attempted": len(items),
        **counts,
        "items": items,
    }


def list_updates(data_dir, fixture_id=None, limit=20):
    """Read newest immutable event revisions across fixtures or for one fixture."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("event listing limit must be from 1 through 1000")
    path = Path(data_dir).expanduser() / "runtime.sqlite3"
    if not path.is_file():
        return []
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    ) as db:
        db.row_factory = sqlite3.Row
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='news_event_updates'"
        ).fetchone():
            return []
        where = " WHERE fixture_id=?" if fixture_id is not None else ""
        args = (fixture_id, limit) if where else (limit,)
        rows = db.execute(
            "SELECT * FROM news_event_updates" + where + " ORDER BY seq DESC LIMIT ?",
            args,
        )
        return [_verified(row) for row in rows]
