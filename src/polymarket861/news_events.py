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
    evidence,
    football,
    input_manifest,
    news,
    news_model,
    news_roster,
    paired_proof,
    pipeline,
    result_freshness,
    schedule_consensus,
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


def _weight_inputs(db, fixture, at):
    """Detect independent result, minutes and lineup receipts at this decision cutoff."""
    params = {"season": fixture["season"], "at": at, "home": fixture["home"], "away": fixture["away"]}
    games = list(db.execute(
        "SELECT fixture_id,kickoff,received,goals,away_goals FROM (" +
        result_freshness._AS_OF.format(times="result_event_times") +
        ") WHERE kickoff<:at AND (home IN (:home,:away) OR away IN (:home,:away)) ORDER BY fixture_id", params,
    ))
    result = {"matches": [tuple(g) for g in games]}
    if not games:
        return result
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE name IN ('news_usage_history','ev_facts','research_events')"
    )}
    if "news_usage_history" in tables:
        result["directory"] = [tuple(r) for r in db.execute(
            """SELECT club,max(known_at) FROM news_usage_history WHERE season=? AND club IN (?,?)
            AND known_at<=? AND received_at<=? AND provider_as_of<=? GROUP BY club ORDER BY club""",
            (fixture["season"], football.team_key(fixture["home"]), football.team_key(fixture["away"]), at, at, at),
        )]
    if "ev_facts" in tables:
        when = evidence.utc(at)
        marks = ",".join("?" for _ in games)
        sources = db.execute(
            f"""SELECT f.source_id,count(*),max(f.rowid),
              (SELECT max(s.rowid) FROM ev_source_versions s WHERE s.source_id=f.source_id
                 AND s.effective_at<=? AND s.recorded_at<=?),
              max((SELECT max(q.rowid) FROM ev_quarantine q JOIN ev_incident_versions iv
                 ON iv.incident_id=q.incident_id AND iv.updated_at=q.since WHERE q.evidence_id=f.evidence_id
                 AND q.since<=? AND json_extract(iv.document,'$.quarantine_recorded_at')<=?))
            FROM ev_facts f WHERE f.fixture_id IN ({marks}) AND f.field='lineup' AND f.team_key IN (?,?)
              AND f.published_at<=? AND f.received_at<=? AND f.valid_from<=?
              AND json_extract(f.document,'$.first_recorded_at')<=? GROUP BY f.source_id ORDER BY f.source_id""",
            (when, when, when, when, *(g["fixture_id"] for g in games),
             evidence.name_key(fixture["home"]), evidence.name_key(fixture["away"]), when, when, when, when),
        )
        result["lineups"] = [tuple(r) for r in sources]
        if result["lineups"] and "research_events" in tables:
            from .jobs_research import _pointer
            result["lineup_source"] = _pointer(db, "source", "lineup", when)
    return result


def _cursor(db, fixture, at, roster_token):
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
    # A different league game may move between publication and kickoff. A
    # fresh wire observation may restore an unchanged but expired state.
    schedule = db.execute(
        """SELECT count(*),max(f.rowid) FROM fixture_versions f JOIN schedule_receipts r USING(fixture_id,version)
        WHERE f.season=? AND (f.home IN (?,?) OR f.away IN (?,?)) AND f.known_at<=? AND r.recorded_at<=?""",
        (fixture["season"], fixture["home"], fixture["away"], fixture["home"], fixture["away"], at, at),
    ).fetchone()
    wire = db.execute(
        "SELECT max(rowid) FROM news_wire_polls WHERE fixture_id=? AND provider_at<=? AND observed_at<=? AND recorded_at<=?",
        (fixture["fixture_id"], at, at, at),
    ).fetchone()[0]
    return store.digest([roster_token, fixture["version"], list(schedule), wire, _weight_inputs(db, fixture, at),
                         [list(r) for r in rows]])


def _claim(root, limit, eligible_schedules):
    with _writer(root) as db:
        at = _now()
        games = [
            f for f in news._schedule(db, at)
            if f["kickoff"] > at and (f["fixture_id"], f["kickoff"], f["version"]) in eligible_schedules
        ] if eligible_schedules else []
        due = []
        manifest = _catalog(db)
        rosters = {}
        for f in games:
            key = (f["fixture_id"], f["kickoff"])
            job = db.execute(
                "SELECT * FROM news_event_work WHERE fixture_id=? AND kickoff=?", key
            ).fetchone()
            if job and job["owner"] and job["lease_until"] > at:
                continue
            # Membership at any club can make another club's player ambiguous.
            # Share the small receipt index across all fixtures in this season.
            year = f["season"]
            if year not in rosters:
                rosters[year] = news_roster.frontier(db, year, at)
            roster = rosters[year]
            cursor = _cursor(db, f, at, roster["token"])
            if (
                job
                and job["cursor"] == cursor
                and (
                    (job["retry_at"] > at and job["retry_manifest"] == manifest)
                    or (not job["last_error"] and job["review_at"] > at)
                )
            ):
                continue
            due.append((job["ticket"] if job else 0, f, cursor, roster["next_at"]))
        due.sort(key=lambda item: (item[0], item[1]["kickoff"], item[1]["fixture_id"]))
        ticket = db.execute(
            "SELECT coalesce(max(ticket),0) FROM news_event_work"
        ).fetchone()[0]
        tasks = []
        for i, (_, f, cursor, roster_expiry) in enumerate(due[:limit], 1):
            owner = uuid.uuid4().hex
            key = (f["fixture_id"], f["kickoff"])
            db.execute(
                """INSERT INTO news_event_work(fixture_id,kickoff,ticket,owner,lease_until) VALUES(?,?,?,?,?)
               ON CONFLICT(fixture_id,kickoff) DO UPDATE SET ticket=excluded.ticket,owner=excluded.owner,
               lease_until=excluded.lease_until""",
                (*key, ticket + i, owner, store.iso(store.instant(at) + LEASE)),
            )
            tasks.append(
                f | {"owner": owner, "cursor": cursor, "roster_expiry": roster_expiry,
                     "schedule_status": eligible_schedules[f["fixture_id"], f["kickoff"], f["version"]]}
            )
        return tasks, len(games), len(due)


_WEIGHT_FIELDS = {"chance_of_playing": None, "duration_multiplier": 1, "importance_multiplier": 1}


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
    result = [dict(zip(fields, v)) for v in sorted(states, key=store.canonical)]
    active = {(item["side"], item["player_id"]): item for item in chosen}
    for state in result:
        if state["state"] == "active":
            item = active[state["side"], state["player_id"]]
            for key, neutral in _WEIGHT_FIELDS.items():
                if item.get(key, neutral) != neutral:
                    state[key] = item[key]
    return result


def _same_contributions(previous, state, pair):
    """Preserve an old event when a new weighting representation gives the same football effects."""
    def categories(items):
        return [{k: v for k, v in row.items() if k not in _WEIGHT_FIELDS} for row in items]

    def shifts(applied):
        return sorted((r["side"], r["player_id"], r["own_log_shift"], r["opponent_log_shift"]) for r in applied)

    return (categories(state) == categories(previous["state"])
            and shifts(pair["applied"]) == shifts(previous["news_pair"]["applied"]))


def _review(snapshot, roster_expiry):
    cut = store.instant(snapshot["cutoff"])
    deadlines = [cut + RECHECK]
    if roster_expiry:
        deadlines.append(store.instant(roster_expiry))
    age = timedelta(days=news_model.POLICY["max_publication_age_days"], microseconds=1)
    for fact in snapshot["facts"]:
        weight_review = news_model.next_weight_review(fact, cut)
        if weight_review:
            deadlines.append(weight_review)
        end = fact.get("expires_at") or fact.get("valid_until")
        if end and store.instant(end) > cut:
            deadlines.append(store.instant(end))
        deadline = store.instant(fact["published_at"]) + age
        if deadline > cut:
            deadlines.append(deadline)
    return store.iso(min(deadlines))


def _head(db, fixture_id):
    return db.execute(
        "SELECT * FROM news_event_updates WHERE fixture_id=? ORDER BY seq DESC LIMIT 1",
        (fixture_id,),
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
            fixture["kickoff"] == previous["kickoff"] and store.digest(state) == previous["state_sha256"]
            if previous else not state
        )

    cut, snapshot, state = sample()
    if same(state):
        return None, _review(snapshot, task["roster_expiry"])
    if "history" not in cache:
        cache["history"] = _history(root, history_root)
    manifest, seen, engine, recipe = cache["history"]
    # Verification may materialize a newly adjudicated manifest. Freeze news and
    # the decision time together after that manifest is actually available.
    cut, snapshot, state = sample()
    if seen > cut:
        raise ValueError("football history was unavailable at decision time")
    if same(state):
        return None, _review(snapshot, task["roster_expiry"])
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
    if (previous and previous["kickoff"] == fixture["kickoff"] and
            _same_contributions(previous, state, pair)):
        return None, _review(snapshot, task["roster_expiry"])
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
        else "schedule_changed"
        if previous["kickoff"] != fixture["kickoff"]
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
    }, _review(snapshot, task["roster_expiry"])


def _run(root, task, cache, history_root):
    key = task["fixture_id"], task["kickoff"]
    basis = {"fixture_id": key[0], "kickoff": key[1]}
    try:
        with closing(store.connect(root)) as db:
            row = _head(db, key[0])
            previous = _verified(row) if row else None
        doc, review = _report(root, task, previous, cache, history_root)
        with _writer(root) as db, dbscope.bind(root, db) as originals:
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
            if doc and task["version"] != doc["schedule_version"]:
                raise schedule_consensus.NewsScheduleChanged("event was computed on a different schedule revision")
            schedule_expiry = schedule_consensus.require_news(
                root, db, task, at, task["schedule_status"]
            )
            store.ensure_unfinished_at(db, *key, at)
            roster = news_roster.frontier(db, task["season"], at)
            if _cursor(db, task, at, roster["token"]) != task["cursor"]:
                raise ValueError("news changed during calculation")
            head = _head(db, key[0])
            if (head["digest"] if head else None) != (
                previous["digest"] if previous else None
            ):
                raise ValueError("previous event changed during calculation")
            at = _now()
            item = basis | {"status": "unchanged"}
            if doc:
                # Computing cannot legitimately cross a validity deadline or go
                # back in time. Re-evaluate at the new cutoff on the next pass.
                if not doc["cutoff"] <= at < min(review, key[1]):
                    raise ValueError("news validity changed during calculation")
                doc.update(
                    created_at=at,
                    # A legacy fixture can have multiple per-kickoff chains
                    # which independently numbered their sealed revisions.
                    revision=db.execute("SELECT coalesce(max(revision),0)+1 FROM news_event_updates WHERE fixture_id=?",
                                        (key[0],)).fetchone()[0],
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
                item.update(status="published", digest=ident, revision=doc["revision"])
            db.execute(
                "UPDATE news_event_work SET owner=NULL,lease_until='',cursor=?,review_at=?,retry_at='',retry_manifest='',last_error=NULL WHERE fixture_id=? AND kickoff=?",
                (task["cursor"], review, *key),
            )
            try:
                dbscope.recheck_originals(originals)
            except dbscope.EvidenceChanged as exc:
                raise schedule_consensus.NewsScheduleChanged(str(exc)) from exc
            finished = _now()
            if schedule_expiry and finished > schedule_expiry:
                raise schedule_consensus.NewsScheduleChanged("native news schedule confirmation expired during publication")
            if finished >= min(review, key[1]):
                raise ValueError("news validity changed during calculation")
        return item
    except (ValueError, KeyError, OSError, sqlite3.OperationalError) as exc:
        reason = str(exc)
        with _writer(root) as db:
            at = store.instant(_now())
            # A newly arrived story should be eligible on the very next pass.
            schedule_changed = isinstance(exc, schedule_consensus.NewsScheduleChanged)
            retry = at if schedule_changed or reason == "news changed during calculation" else at + RETRY
            db.execute(
                "UPDATE news_event_work SET owner=NULL,lease_until='',cursor=?,retry_at=?,retry_manifest=?,last_error=? WHERE fixture_id=? AND kickoff=? AND owner=?",
                (
                    task["cursor"],
                    store.iso(retry),
                    _catalog(db),
                    reason,
                    *key,
                    task["owner"],
                ),
            )
        if schedule_changed:
            basis["reason_code"] = "schedule_readiness"
        return basis | {"status": "deferred", "reason": reason}


def refresh(data_dir, *, limit=2, history_root=None):
    """Process at most limit prospective fixtures. Retries survive restarts."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("event refresh limit must be from 1 through 100")
    at = _now()
    with news._db(data_dir) as db:
        dbscope.prepare(db, "event-news", _SCHEMA)
        relevant = {
            r[0] for r in db.execute(
                """SELECT DISTINCT fixture_id FROM news_articles
                WHERE fixture_id IS NOT NULL AND recorded_at<=? UNION SELECT fixture_id FROM news_event_work""",
                (at,),
            )
        }
    schedule = schedule_consensus.readiness(data_dir, fixture_ids=relevant, at=at) if relevant else None
    eligible_schedules = {
        (f["fixture_id"], f["kickoff"], f["version"]): f["status"]
        for f in schedule["fixtures"] if f["news_eligible"]
    } if schedule else {}
    tasks, eligible, due = _claim(data_dir, limit, eligible_schedules)
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
        "schedule_blocked_fixtures": sorted(
            set(schedule["blocked_fixtures"] if schedule else ())
            | {i["fixture_id"] for i in items if i.get("reason_code") == "schedule_readiness"}
        ),
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
