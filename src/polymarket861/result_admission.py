"""Admit regulation results from independent HTTP receipts and maintain reversible scoring holds."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from contextlib import closing
from datetime import timedelta
from functools import lru_cache

from . import dbscope, result_pairings, store

INTERVAL = timedelta(minutes=30)
SCHEMA = """
CREATE TABLE IF NOT EXISTS rf_native_requests(
 season INTEGER,scope TEXT,started_at TEXT,received_at TEXT,sha TEXT,url TEXT,
 PRIMARY KEY(season,scope,received_at));
CREATE TABLE IF NOT EXISTS rf_admitted(
 fixture TEXT,kickoff TEXT,at TEXT,score TEXT,proof TEXT, PRIMARY KEY(fixture,kickoff,at));
CREATE TABLE IF NOT EXISTS rf_pairing_reviews(
 fixture TEXT,kickoff TEXT,at TEXT,proof TEXT,action TEXT,reviewer TEXT,reason TEXT,
 PRIMARY KEY(fixture,kickoff,at));
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
    dbscope.prepare(db, "result-admission", SCHEMA)
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
        "SELECT r.*,t.actual_kickoff FROM result_versions r JOIN result_schedule_refs s USING(fixture_id,version) "
        "LEFT JOIN result_event_times t USING(fixture_id,version) WHERE r.fixture_id=? AND s.kickoff=? ORDER BY r.version DESC LIMIT 1",
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


def pairing_review(db, fid, kickoff, at):
    row = db.execute(
        "SELECT * FROM rf_pairing_reviews WHERE fixture=? AND kickoff=? AND at<=? ORDER BY at DESC LIMIT 1",
        (fid, kickoff, at),
    ).fetchone()
    return dict(row) if row else None


def review_pairing(root, year, fid, proof, action, reviewer, reason):
    """Version an operator's stop or release for an exact previously archived pairing."""
    from . import result_feed, result_operations

    if action not in {"withdraw", "release"} or not all(
        isinstance(s, str) and s.strip() for s in (reviewer, reason)
    ):
        raise ValueError("pairing review requires an action, reviewer and reason")
    with closing(database(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        at = store.iso(result_feed.now_utc())
        f = next(
            (
                f
                for f in store.eligible_fixture_rows(db, season=scope(year))
                if f["fixture_id"] == fid
            ),
            None,
        )
        record = db.execute(
            "SELECT * FROM rf_admitted WHERE fixture=? AND at<=? ORDER BY at DESC LIMIT 1",
            (fid, at),
        ).fetchone()
        if not f or not record or f["kickoff"] != record["kickoff"]:
            raise ValueError("no current pairing for this fixture")
        payload = json.loads(record["proof"])
        if not isinstance(payload, dict) or store.digest(payload) != proof:
            raise ValueError(
                "the supplied proof differs from the latest result pairing"
            )
        previous = pairing_review(db, fid, f["kickoff"], at)
        if not previous or previous["proof"] != proof or previous["action"] != action:
            if action == "release" and (
                not previous or previous["action"] != "withdraw"
            ):
                raise ValueError("there is no withdrawn pairing to release")
            db.execute(
                "INSERT INTO rf_pairing_reviews VALUES(?,?,?,?,?,?,?)",
                (
                    fid,
                    f["kickoff"],
                    at,
                    proof,
                    action,
                    reviewer.strip(),
                    reason.strip(),
                ),
            )
            if action == "withdraw":
                disposition(db, f, True, "operator_withdrew_result_pairing", at)
        review = pairing_review(db, fid, f["kickoff"], at)
    if action == "release":
        # Re-derive all evidence from originals. A release alone cannot admit a score.
        reconcile(root, year)
    return review | {"maintenance": result_operations.publications(root)}


@lru_cache(maxsize=6)
def _decode(raw, year, rnd):
    from . import result_feed

    return tuple(result_feed._fixture_rows(json.loads(raw), year, only_round=rnd))


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
    owners = defaultdict(set)
    for fid, history in by_fixture.items():
        if history[-1]["error"] != "previous_fixture_absent":
            owners[history[-1]["source_id"]].add(fid)
    collided = {mid for mid, fids in owners.items() if len(fids) > 1}
    if collided:
        for history in by_fixture.values():
            if history[-1]["source_id"] in collided:
                history[-1] = history[-1] | {
                    "score": None,
                    "error": "ambiguous_source_match_id",
                }
    if target is not None:
        return {target: by_fixture.get(target, [])}, source_errors
    return by_fixture, source_errors


def _pair(events):
    """Find two consecutive consistent states separated by the confirmation interval."""
    last = events[-1]
    fields = ("source_id", "round", "kickoff", "score", "error")
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
        and any(
            r[k] != current[k]
            for k in ("source_id", "round", "kickoff", "score", "error")
        )
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


def _result(db, f, score, source, locator, at, *, actual_kickoff=None):
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
        actual_kickoff=actual_kickoff,
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
        from . import independent_results

        sources, errors = _raw_rows(root, db, year, at)
        try:
            independent = independent_results.verified(root, db, year, at)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            independent = {}
            errors.append(
                {"source": "independent-results", "scope": None, "error": str(exc)}
            )
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
        unique = result_pairings.unique_fixtures(fixtures.values())
        paired = []
        for fid in sorted(set(sources) | set(independent) | certified_ids):
            events = sources.get(fid, [])
            f = fixtures.get(fid)
            if not f:
                counts["unmapped"] += 1
                continue
            damaged = [
                error for error in errors if error["scope"] in (None, f["round_id"])
            ]
            proof, pairing_reason = result_pairings.decision(
                f, events, independent.get(fid, []), unique, at
            )
            latest = events[-1] if events else None
            pair = _pair(events) if events else None
            prior = last_result(db, fid, f["kickoff"])
            prior_score = [prior["home_goals"], prior["away_goals"]] if prior else None
            last = last_disposition(db, fid, f["kickoff"], at)
            certified = db.execute(
                "SELECT * FROM rf_admitted WHERE fixture=? AND kickoff=? ORDER BY at DESC LIMIT 1",
                (fid, f["kickoff"]),
            ).fetchone()
            if not latest and not certified and not proof and not pairing_reason:
                counts["unfinished"] += 1
                continue
            reason = None
            withdrawn_manual = bool(
                prior
                and prior["source"] == "manual-regulation"
                and last
                and last["held"]
                and last["reason"] == "manual_batch_withdrawn"
            )
            old_proof = json.loads(certified["proof"]) if certified else None
            native_score = latest["score"] if latest else None
            score = proof["score"] if proof else native_score
            review = pairing_review(db, fid, f["kickoff"], at)
            if review and review["action"] == "withdraw":
                reason = "operator_withdrew_result_pairing"
            elif fid not in unique:
                reason = "ambiguous_local_fixture"
            elif latest and latest["error"] in {
                "ambiguous_source_fixture",
                "ambiguous_source_match_id",
                "ambiguous_90_minute_final",
            }:
                reason = latest["error"]
            elif pairing_reason:
                reason = pairing_reason
            elif isinstance(old_proof, dict) and not proof:
                reason = "paired_result_evidence_withdrawn"
            elif not latest and not proof:
                reason = "missing_original_receipts"
            elif score is not None and reference.get(fid, score) != score:
                reason = "cross_source_score_conflict"
            elif (
                prior
                and prior["source"] == "manual-regulation"
                and not withdrawn_manual
            ):
                at_review = [e for e in events if e["received"] <= prior["recorded_at"]]
                previously_reviewed = bool(
                    at_review and native_score == at_review[-1]["score"]
                )
                if (
                    score == prior_score
                    or previously_reviewed
                    or not at_review
                    and score is None
                ):
                    if (
                        score == prior_score
                        and last
                        and last["held"]
                        and last["reason"] == "manual_source_disagreement"
                    ):
                        disposition(db, f, False, "manual_source_reconciled", at)
                    counts["manual_reviewed"] += 1
                    continue
                reason = "manual_source_disagreement"
            elif score is not None and not proof and not _anchor(f, latest):
                reason = "schedule_or_round_mismatch"
            elif (
                score is not None
                and prior
                and prior["source"]
                not in {"openligadb", "manual-regulation", result_pairings.SOURCE}
                and (prior["source"] != "openfootball" or fid not in reference)
                and prior_score != score
            ):
                reason = "cross_source_score_conflict"
            elif (
                reason is None
                and certified
                and (score != json.loads(certified["score"]) or damaged)
                and not (pair or proof)
            ):
                reason = "previous_final_changed_or_withdrawn"
            elif reason is None and damaged and certified:
                reason = "original_receipt_unreadable"
            if reason:
                counts[reason] += 1
                if reason.startswith("awaiting_") and not certified:
                    continue
                issues.append(
                    {
                        "fixture_id": fid,
                        "reason": reason,
                        "source_score": native_score,
                        "source_kickoff": latest["kickoff"] if latest else None,
                        "registry_kickoff": f["kickoff"],
                        "reference_score": reference.get(fid, prior_score),
                        "sha256": latest["sha256"] if latest else None,
                        "independent": independent.get(fid, [])[-1:],
                    }
                )
                disposition(db, f, True, reason, at)
                continue
            if proof and not damaged:
                changed = (
                    not isinstance(old_proof, dict)
                    or result_pairings.signature(old_proof)
                    != result_pairings.signature(proof)
                    or prior_score != score
                    or withdrawn_manual
                    or bool(last and last["held"])
                )
                if changed:
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
                    _result(
                        db,
                        f,
                        score,
                        result_pairings.SOURCE,
                        "result-pairing:" + store.digest(proof),
                        at,
                        actual_kickoff=proof["actual_kickoff"],
                    )
                    admitted.append(fid)
                    disposition(db, f, False, "verified_regulation_pairing", at)
                    enqueue(db, season, f["round_id"])
                else:
                    proof = old_proof
                counts[proof["method"]] += 1
                paired.append(
                    {
                        k: proof[k]
                        for k in (
                            "fixture_id",
                            "round",
                            "registry_kickoff",
                            "actual_kickoff",
                            "score",
                            "method",
                        )
                    }
                    | {"proof_sha256": store.digest(proof)}
                )
                continue
            if not pair or damaged:
                counts["awaiting_second_receipt" if native_score else "unfinished"] += 1
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
            "postmatch_pairings": paired,
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
            sources, errors = _raw_rows(root, db, year, cutoff, target=fixture_id)
            snapshots = sources.get(fixture_id, [])
            from . import independent_results

            independent = independent_results.verified(root, db, year, cutoff)
            local = db.execute(
                "WITH ranked AS (SELECT f.*,row_number() OVER(PARTITION BY fixture_id ORDER BY known_at DESC) n "
                "FROM fixture_versions f JOIN schedule_receipts s USING(fixture_id,version) "
                "WHERE f.season=? AND f.known_at<=? AND s.recorded_at<=?) SELECT * FROM ranked WHERE n=1",
                (f["season"], cutoff, cutoff),
            ).fetchall()
            unique = result_pairings.unique_fixtures(local)
            fresh, reason = result_pairings.decision(
                f, snapshots, independent.get(fixture_id, []), unique, cutoff
            )
            if (
                fixture_id not in unique
                or reason
                or any(e["scope"] in (None, f["round_id"]) for e in errors)
            ):
                return None
            saved = json.loads(row["proof"])
            if isinstance(saved, dict):
                if not fresh or result_pairings.signature(
                    saved
                ) != result_pairings.signature(fresh):
                    return None
                # Reproduce the ORIGINAL proof at the actual admission cutoff, even if
                # newer receipts report the same score. A mutation cannot replace it.
                old = [e for e in snapshots if e["received"] <= row["at"]]
                others = [
                    e
                    for e in independent.get(fixture_id, [])
                    if e["received"] <= row["at"]
                ]
                original, error = result_pairings.decision(
                    f, old, others, unique, row["at"]
                )
                score = json.loads(row["score"])
                if error or original != saved or saved["score"] != score:
                    return None
                if (
                    _reference(db, root, f["season"], cutoff).get(fixture_id, score)
                    != score
                ):
                    return None
                current = db.execute(
                    "SELECT r.*,t.actual_kickoff FROM result_versions r JOIN result_schedule_refs s USING(fixture_id,version) "
                    "LEFT JOIN result_event_times t USING(fixture_id,version) "
                    "WHERE fixture_id=? AND s.kickoff=? AND r.recorded_at<=? ORDER BY version DESC LIMIT 1",
                    (fixture_id, f["kickoff"], cutoff),
                ).fetchone()
                if (
                    not current
                    or current["source"] != result_pairings.SOURCE
                    or (
                        [current["home_goals"], current["away_goals"]] != score
                        or current["actual_kickoff"] != saved["actual_kickoff"]
                        or current["locator"] != "result-pairing:" + store.digest(saved)
                    )
                ):
                    return None
                return {
                    "fixture_id": fixture_id,
                    "home_goals": score[0],
                    "away_goals": score[1],
                    "kickoff": saved["actual_kickoff"],
                    "registry_kickoff": f["kickoff"],
                    "confirmed_at": row["at"],
                    "source": result_pairings.SOURCE,
                    "proof": saved,
                    "status": "final",
                    "period": "regulation",
                }
            if len(snapshots) < 2 or not _anchor(f, snapshots[-1]):
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
