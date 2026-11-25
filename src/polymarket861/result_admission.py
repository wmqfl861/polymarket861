"""Admit regulation results from independent HTTP receipts and maintain reversible scoring holds."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from contextlib import closing
from datetime import timedelta
from functools import lru_cache

from . import store

INTERVAL = timedelta(minutes=30)
SCHEMA = """
CREATE TABLE IF NOT EXISTS rf_native_requests(
 season INTEGER,scope TEXT,started_at TEXT,received_at TEXT,sha TEXT,url TEXT,
 PRIMARY KEY(season,scope,received_at));
CREATE TABLE IF NOT EXISTS rf_admitted(
 fixture TEXT,kickoff TEXT,at TEXT,score TEXT,proof TEXT, PRIMARY KEY(fixture,kickoff,at));
CREATE TABLE IF NOT EXISTS rf_round_state(
 season TEXT,round_id INTEGER,revision INTEGER NOT NULL, PRIMARY KEY(season,round_id));
CREATE TABLE IF NOT EXISTS rf_publish_queue(
 season TEXT,round_id INTEGER,revision INTEGER NOT NULL, PRIMARY KEY(season,round_id));
CREATE TABLE IF NOT EXISTS rf_batches(
 id TEXT PRIMARY KEY,season TEXT,round_id INTEGER,at TEXT,attachment TEXT,detail TEXT,rollback TEXT);
CREATE TABLE IF NOT EXISTS rf_fallback(
 year INTEGER PRIMARY KEY,attempted TEXT,status TEXT);
"""


def database(root):
    from . import result_feed

    db = result_feed._db(root)
    db.executescript(SCHEMA)
    return db


def scope(year):
    return f"{year}-{(year + 1) % 100:02d}"


def enqueue(db, season, rnd):
    db.execute(
        "INSERT INTO rf_round_state VALUES(?,?,1) ON CONFLICT(season,round_id) DO UPDATE SET revision=revision+1",
        (season, rnd),
    )
    db.execute(
        "INSERT OR REPLACE INTO rf_publish_queue SELECT * FROM rf_round_state WHERE season=? AND round_id=?",
        (season, rnd),
    )


def last_result(db, fid, kickoff):
    return db.execute(
        "SELECT r.* FROM result_versions r JOIN result_schedule_refs s USING(fixture_id,version) "
        "WHERE r.fixture_id=? AND s.kickoff=? ORDER BY r.version DESC LIMIT 1",
        (fid, kickoff),
    ).fetchone()


def last_disposition(db, fid, kickoff, at):
    return db.execute(
        "SELECT * FROM result_dispositions WHERE fixture_id=? AND kickoff=? AND at<=? "
        "ORDER BY at DESC LIMIT 1",
        (fid, kickoff, at),
    ).fetchone()


def disposition(db, fixture, held, reason, at):
    fid, kickoff = fixture["fixture_id"], fixture["kickoff"]
    previous = last_disposition(db, fid, kickoff, at)
    if (not previous and not held) or (
        previous and (previous["held"], previous["reason"]) == (int(held), reason)
    ):
        return False
    if previous and not held and not previous["held"]:
        return False
    db.execute(
        "INSERT INTO result_dispositions VALUES(?,?,?,?,?)",
        (fid, kickoff, at, int(held), reason),
    )
    enqueue(db, fixture["season"], fixture["round_id"])
    return True


@lru_cache(maxsize=6)
def _decode(raw, year, rnd):
    from . import result_feed

    return tuple(result_feed._full_time(json.loads(raw), year, only_round=rnd))


def _raw_rows(root, db, year, at, *, target=None, round_id=None):
    """Every candidate below is rederived from archived HTTP bytes, including authentic v1 receipts."""
    from . import result_feed as feed

    requests = {
        (r["sha"], r["received_at"]): dict(r)
        for r in db.execute(
            "SELECT * FROM rf_native_requests WHERE season=? AND received_at<=?",
            (year, at),
        )
    }
    legacy = set(
        db.execute(
            "SELECT sha,seen_at FROM rf_cache WHERE season=? AND seen_at<=?", (year, at)
        ).fetchall()
    )
    legacy.update(
        db.execute(
            "SELECT DISTINCT source_sha,observed_at FROM rf_observations WHERE season=? AND observed_at<=?",
            (year, at),
        )
    )
    legacy = {tuple(x) for x in legacy}
    by_fixture, source_errors = defaultdict(list), []
    for obs in db.execute(
        "SELECT observed_at,sha256,metadata FROM observations WHERE source='openligadb-results' "
        "AND observed_at<=? ORDER BY observed_at",
        (at,),
    ):
        stamp, sha = obs["observed_at"], obs["sha256"]
        native = requests.get((sha, stamp))
        if not native and (sha, stamp) not in legacy:
            continue
        url = None
        rnd = None
        base = f"{feed.BASE}/getmatchdata/pl/{year}"
        try:
            meta = json.loads(obs["metadata"])
            url = meta["url"]
            if url == base:
                rnd = None
            elif (
                url.startswith(base + "/")
                and url[len(base) + 1 :].isdigit()
                and str(int(url[len(base) + 1 :])) == url[len(base) + 1 :]
            ):
                rnd = int(url[len(base) + 1 :])
                if not 1 <= rnd <= 38:
                    continue
            else:
                continue
            if round_id is not None and rnd not in (None, round_id):
                continue
            if meta.get("license") != "ODbL-1.0" or (
                native and (native["url"] != url or native["started_at"] > stamp)
            ):
                raise ValueError("invalid native receipt identity")
            raw = store.read_blob(root, sha)
            if hashlib.sha256(raw).hexdigest() != sha:
                raise ValueError("OpenLigaDB archive hash mismatch")
            rows = _decode(raw, year, rnd)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            source_errors.append(
                {
                    "received_at": stamp,
                    "sha256": sha,
                    "scope": None if url == base else rnd,
                    "error": str(exc),
                }
            )
            continue
        present = {row[0] for row in rows}
        for fid, events in by_fixture.items():
            last = events[-1]
            if fid not in present and (rnd is None or rnd == last["round"]):
                events.append(
                    last
                    | {
                        "score": None,
                        "error": "previous_fixture_absent",
                        "sha256": sha,
                        "url": url,
                        "received": stamp,
                        "requested": native["started_at"] if native else stamp,
                    }
                )
        for fid, mid, r, kick, score, error in rows:
            if target is not None and fid != target:
                continue
            by_fixture[fid].append(
                {
                    "season": year,
                    "source_id": mid,
                    "round": r,
                    "kickoff": store.iso(kick),
                    "score": score,
                    "error": error,
                    "received": stamp,
                    "sha256": sha,
                    "url": url,
                    "requested": native["started_at"] if native else stamp,
                }
            )
    return by_fixture, source_errors


def _pair(events):
    """Find two consecutive consistent states separated by the confirmation interval."""
    last = events[-1]
    fields = ("source_id", "round", "kickoff", "score")
    if last["score"] is None or last["error"]:
        return None
    earliest = store.instant(last["kickoff"]) + timedelta(minutes=90)
    # A regression to unfinished or a changed final breaks the chain.
    for event in reversed(events[:-1]):
        if any(event[k] != last[k] for k in fields):
            break
        if store.instant(event["received"]) < earliest:
            break
        if (
            store.instant(last["requested"]) - store.instant(event["received"])
            >= INTERVAL
        ):
            return [event, last]
    return None


def _same_run(events, proof):
    if not proof or not events:
        return False
    current = events[-1]
    return not any(
        r["received"] >= proof[-1]["received"]
        and any(r[k] != current[k] for k in ("source_id", "round", "kickoff", "score"))
        for r in events
    )


def _reference(db, root, season, at):
    """Independent OpenFootball results re-read from the latest observed and hashed season bytes."""
    import tempfile
    from pathlib import Path

    from . import football

    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='football_input_manifests'"
    ).fetchone():
        return {}
    for row in db.execute(
        "SELECT digest,manifest FROM football_input_manifests WHERE observed_at<=? ORDER BY observed_at DESC,rowid DESC",
        (at,),
    ):
        manifest = json.loads(row["manifest"])
        if store.digest(manifest) != row["digest"]:
            raise ValueError("invalid football input manifest")
        item = next((x for x in manifest["files"] if x["season"] == season), None)
        if item:
            raw = store.read_blob(root, item["sha256"])
            if (
                hashlib.sha256(raw).hexdigest() != item["sha256"]
                or football.blob_sha(raw) != item["git_blob"]
            ):
                raise ValueError("OpenFootball fallback raw hash mismatch")
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / season / "en.1.json"
                target.parent.mkdir()
                target.write_bytes(raw)
                return {
                    f.fixture_id: [f.home_goals, f.away_goals]
                    for f in football.load_fixtures(target)
                    if f.home_goals is not None and f.status != "quarantined"
                }
    return {}


def _anchor(f, e):
    return (
        f["precision"] == "minute"
        and f["round_id"] == e["round"]
        and f["kickoff"] == e["kickoff"]
    )


def _result(db, f, score, source, locator, at):
    return store.record_result_db(
        db,
        {
            "fixture_id": f["fixture_id"],
            "home_goals": score[0],
            "away_goals": score[1],
            "archive_at": at,
            "published_by": at,
            "finalized_by": at,
            "kickoff": f["kickoff"],
            "source": source,
            "locator": locator,
        },
        recorded_at=at,
    )


def reconcile(root, year):
    from . import result_feed as feed

    season = scope(year)
    counts = Counter()
    issues = []
    admitted = []
    with closing(database(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        at = store.iso(feed.now_utc())
        sources, errors = _raw_rows(root, db, year, at)
        try:
            reference = _reference(db, root, season, at)
        except (ValueError, KeyError, OSError, TypeError) as exc:
            reference = {}
            errors.append({"source": "openfootball", "scope": None, "error": str(exc)})
        fixtures = {
            f["fixture_id"]: f for f in store.eligible_fixture_rows(db, season=season)
        }
        certified_ids = {
            r[0]
            for r in db.execute("SELECT DISTINCT fixture FROM rf_admitted")
            if r[0] in fixtures
        }
        for fid in sorted(set(sources) | certified_ids):
            events = sources.get(fid, [])
            f = fixtures.get(fid)
            if not f:
                counts["unmapped"] += 1
                continue
            damaged = [
                error for error in errors if error["scope"] in (None, f["round_id"])
            ]
            if not events:
                reason = "missing_original_receipts"
                issues.append({"fixture_id": fid, "reason": reason})
                counts[reason] += 1
                disposition(db, f, True, reason, at)
                continue
            latest = events[-1]
            pair = _pair(events)
            prior = last_result(db, fid, f["kickoff"])
            prior_score = [prior["home_goals"], prior["away_goals"]] if prior else None
            last = last_disposition(db, fid, f["kickoff"], at)
            certified = db.execute(
                "SELECT * FROM rf_admitted WHERE fixture=? AND kickoff=? ORDER BY at DESC LIMIT 1",
                (fid, f["kickoff"]),
            ).fetchone()
            reason = None
            withdrawn_manual = bool(
                prior
                and prior["source"] == "manual-regulation"
                and last
                and last["held"]
                and last["reason"] == "manual_batch_withdrawn"
            )
            if (
                prior
                and prior["source"] == "manual-regulation"
                and not withdrawn_manual
            ):
                at_review = [e for e in events if e["received"] <= prior["recorded_at"]]
                agrees = latest["score"] == prior_score
                previously_reviewed = bool(
                    at_review and latest["score"] == at_review[-1]["score"]
                )
                if (
                    agrees
                    or previously_reviewed
                    or not at_review
                    and latest["score"] is None
                ):
                    if (
                        agrees
                        and last
                        and last["held"]
                        and last["reason"] == "manual_source_disagreement"
                    ):
                        disposition(db, f, False, "manual_source_reconciled", at)
                    counts["manual_reviewed"] += 1
                    continue
                reason = "manual_source_disagreement"
            if latest["score"] is not None and not _anchor(f, latest):
                reason = "schedule_or_round_mismatch"
            elif latest["score"] is not None and (
                reference.get(fid, latest["score"]) != latest["score"]
                or (
                    prior
                    and prior["source"] not in {"openligadb", "manual-regulation"}
                    and (prior["source"] != "openfootball" or fid not in reference)
                    and prior_score != latest["score"]
                )
            ):
                reason = "cross_source_score_conflict"
            elif (
                reason is None
                and certified
                and (latest["score"] != json.loads(certified["score"]) or damaged)
                and not pair
            ):
                reason = "previous_final_changed_or_withdrawn"
            elif reason is None and damaged and certified:
                reason = "original_receipt_unreadable"
            if reason:
                counts[reason] += 1
                issues.append(
                    {
                        "fixture_id": fid,
                        "reason": reason,
                        "source_score": latest["score"],
                        "source_kickoff": latest["kickoff"],
                        "registry_kickoff": f["kickoff"],
                        "reference_score": reference.get(fid, prior_score),
                        "sha256": latest["sha256"],
                    }
                )
                disposition(db, f, True, reason, at)
                continue
            if not pair or damaged:
                counts[
                    "awaiting_second_receipt" if latest["score"] else "unfinished"
                ] += 1
                continue
            score = latest["score"]
            proof = [
                {k: r[k] for k in ("received", "sha256", "url", "source_id")}
                for r in pair
            ]
            if (
                not certified
                or prior_score != score
                or (last and last["held"])
                or not _same_run(events, json.loads(certified["proof"]))
            ):
                db.execute(
                    "INSERT INTO rf_admitted VALUES(?,?,?,?,?)",
                    (
                        fid,
                        f["kickoff"],
                        at,
                        store.canonical(score),
                        store.canonical(proof),
                    ),
                )
                if prior_score != score or withdrawn_manual:
                    _result(
                        db,
                        f,
                        score,
                        "openligadb",
                        f"{latest['url']}#match={latest['source_id']}; receipts {store.digest(proof)}",
                        at,
                    )
                    admitted.append(fid)
                disposition(db, f, False, "verified_regulation", at)
                enqueue(db, season, f["round_id"])
            counts["verified_two_receipts"] += 1
        eligible = store.eligible_fixture_rows(db, season=season)
        overdue = [
            {
                "fixture_id": f["fixture_id"],
                "round": f["round_id"],
                "kickoff": f["kickoff"],
                "reason": f["result_disposition"] or "awaiting_final",
            }
            for f in eligible
            if f["home_goals"] is None
            and store.instant(f["kickoff"]) + timedelta(hours=24) <= store.instant(at)
        ]
        report = {
            "season": season,
            "status": (
                "awaiting_schedule"
                if not eligible
                else "degraded"
                if errors or overdue or issues
                else "awaiting_confirmation"
                if counts["awaiting_second_receipt"]
                else "ok"
            ),
            "at": at,
            "coverage": dict(counts),
            "registered": len(eligible),
            "available_results": sum(f["home_goals"] is not None for f in eligible),
            "new_scores": admitted,
            "quarantined": issues,
            "overdue": overdue,
            "receipt_errors": errors,
            "minimum_confirmation_minutes": int(INTERVAL.total_seconds() / 60),
            "attribution": feed.ATTRIBUTION,
            "license": feed.LICENSE,
            "terms": feed.TERMS,
        }
        store.put_report(db, "source", f"epl-regulation-{year}", report)
        store.put_report(
            db,
            "incident",
            f"epl-overdue-results-{year}",
            {
                "status": "open" if overdue else "clear",
                "after_hours": 24,
                "items": overdue,
                "at": at,
            },
        )
        store.put_report(
            db,
            "incident",
            f"epl-result-conflicts-{year}",
            {
                "status": "quarantined" if issues or errors else "clear",
                "items": issues,
                "receipt_errors": errors,
                "at": at,
            },
        )
    return report


def verified_regulation(root, fixture_id, *, at=None):
    """Return an as-of, raw-reverified 90-minute final; a manual/withdrawn/disputed result cannot settle."""
    from . import result_feed as feed

    cutoff = store.iso(at or feed.now_utc())
    with closing(database(root)) as db:
        row = db.execute(
            "SELECT * FROM rf_admitted WHERE fixture=? AND at<=? ORDER BY at DESC LIMIT 1",
            (fixture_id, cutoff),
        ).fetchone()
        if not row:
            return None
        try:
            f = store.fixture_at(root, fixture_id, at=cutoff, prospective=True)
            if f["kickoff"] != row["kickoff"]:
                return None
            state = last_disposition(db, fixture_id, f["kickoff"], cutoff)
            if state and state["held"]:
                return None
            year = int(f["season"][:4])
            sources, errors = _raw_rows(
                root, db, year, cutoff, target=fixture_id, round_id=f["round_id"]
            )
            snapshots = sources.get(fixture_id, [])
            if (
                any(e["scope"] in (None, f["round_id"]) for e in errors)
                or len(snapshots) < 2
                or not _anchor(f, snapshots[-1])
            ):
                return None
            pair = _pair(snapshots)
            score = json.loads(row["score"])
            stamp = row["at"]
            if (
                not pair
                or pair[-1]["score"] != score
                or _reference(db, root, f["season"], cutoff).get(fixture_id, score)
                != score
            ):
                return None
            observed = db.execute(
                "SELECT r.* FROM result_versions r JOIN result_schedule_refs s USING(fixture_id,version) "
                "WHERE r.fixture_id=? AND s.kickoff=? AND r.recorded_at<=? ORDER BY r.version DESC LIMIT 1",
                (fixture_id, f["kickoff"], cutoff),
            ).fetchone()
            if (
                not observed
                or observed["source"] not in {"openfootball", "openligadb"}
                or [observed["home_goals"], observed["away_goals"]] != score
            ):
                return None
            proof = json.loads(row["proof"])
            if not _same_run(snapshots, proof):
                return None
            if any(
                not any(
                    a["sha256"] == b["sha256"]
                    and a["received"] == b["received"]
                    and a["source_id"] == b["source_id"]
                    and a["score"] == score
                    and a["url"] == b["url"]
                    for a in snapshots
                )
                for b in proof
            ):
                return None
            return {
                "fixture_id": fixture_id,
                "home_goals": score[0],
                "away_goals": score[1],
                "kickoff": f["kickoff"],
                "confirmed_at": stamp,
                "source": "openligadb",
                "proof": proof,
                "status": "final",
                "period": "regulation",
            }
        except (ValueError, KeyError, TypeError, OSError):
            return None
