"""Season result maintenance and auditable all-or-nothing manual matchweek corrections."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from . import input_manifest, store
from . import result_admission as admission
from . import result_feed as feed


def publications(root):
    from .jobs import review_due_experiments
    from .round_history import publish_round, ready_for_experiment_review

    with closing(admission.database(root)) as db:
        queue = list(
            db.execute("SELECT * FROM rf_publish_queue ORDER BY season,round_id")
        )
        manifests = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='football_input_manifests'"
        ).fetchone()
        latest = (
            db.execute(
                "SELECT digest FROM football_input_manifests ORDER BY observed_at DESC,rowid DESC LIMIT 1"
            ).fetchone()
            if manifests
            else None
        )
    if not queue:
        return {
            "rounds": [],
            "manifest": latest[0] if latest else None,
            "training": "no_change",
        }
    manifest = input_manifest.verify_observed_inputs(root) if latest else None
    reports = []
    for event in queue:
        season, rnd, version = tuple(event)
        with closing(admission.database(root)) as db:
            seals = list(
                db.execute(
                    "SELECT DISTINCT model_version,mode,horizon FROM forecast_seals "
                    "WHERE fixture_id IN (SELECT fixture_id FROM fixture_versions WHERE season=? AND round_id=?)",
                    (season, rnd),
                )
            )
        emitted = []
        for model, mode, horizon in seals or [("B1", "technical", "T-60m")]:
            report = publish_round(
                root, season, rnd, version=model, mode=mode, horizon=horizon
            )
            emitted.append(
                {
                    k: report.get(k)
                    for k in (
                        "status",
                        "mode",
                        "completed",
                        "expected",
                        "model_version",
                        "version",
                        "horizon",
                    )
                }
            )
        reviews = []
        if any(ready_for_experiment_review(r) for r in emitted):
            reviews = review_due_experiments(root, season, rnd)
        reports.append(
            {
                "season": season,
                "round": rnd,
                "reports": emitted,
                "experiment_reviews": reviews,
            }
        )
        with closing(admission.database(root)) as db, db:
            db.execute(
                "DELETE FROM rf_publish_queue WHERE season=? AND round_id=? AND revision=?",
                (season, rnd, version),
            )
    return {
        "rounds": reports,
        "manifest": manifest["digest"] if manifest else None,
        "training": "refreshed" if manifest else "awaiting_observed_training_inputs",
    }


def _fallback(root, year):
    from . import datasets

    at = store.iso(feed.now_utc())
    with closing(admission.database(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM rf_fallback WHERE year=?", (year,)).fetchone()
        if row and store.instant(at) - store.instant(row["attempted"]) < timedelta(
            hours=6 if row["status"] != "updated" else 24
        ):
            return {
                "source": "openfootball",
                "status": "cached",
                "previous": row["status"],
                "attempted": row["attempted"],
            }
        db.execute(
            "INSERT OR REPLACE INTO rf_fallback VALUES(?,?,?)",
            (year, at, "in_progress"),
        )
        first = admission.scope(year - 3)
        last = admission.scope(year)
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='football_input_manifests'"
        ).fetchone():
            r = db.execute(
                "SELECT manifest FROM football_input_manifests ORDER BY observed_at DESC,rowid DESC LIMIT 1"
            ).fetchone()
            if r:
                seasons = [
                    x["season"]
                    for x in json.loads(r[0])["files"]
                    if x["season"] <= last
                ]
                if seasons:
                    first = min(seasons)
    try:
        result = datasets.refresh(root, first, last)
        report = {
            "source": "openfootball",
            "status": "updated",
            "revision": result["revision"],
            "at": at,
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report = {
            "source": "openfootball",
            "status": "unavailable",
            "error": str(exc),
            "at": at,
        }
    with closing(admission.database(root)) as db, db:
        db.execute(
            "UPDATE rf_fallback SET status=? WHERE year=? AND attempted=?",
            (report["status"], year, at),
        )
    return report


def season(root, year, *, offline=False):
    if type(year) is not int or not 2000 <= year <= 2100:
        raise ValueError("invalid EPL season")
    from . import independent_results, result_freshness

    at = store.instant(feed.now_utc())
    wet = []
    secondary = {"source": independent_results.SOURCE, "status": "offline"}
    fallback = {"source": "openfootball", "status": "offline"}
    if not offline:
        try:
            with closing(admission.database(root)) as db:
                boot = db.execute(
                    "SELECT 1 FROM rf_cache WHERE season=? AND scope='season'", (year,)
                ).fetchone()
            if not boot:
                r = feed.sync(root, year)
                wet.append({"scope": "season", "status": r["fetch_status"]})
        except (ValueError, OSError, KeyError, TypeError) as exc:
            wet.append({"scope": "season", "status": "unavailable", "error": str(exc)})
        fallback = _fallback(root, year)
        with closing(admission.database(root)) as db:
            fixtures = store.eligible_fixture_rows(db, season=admission.scope(year))
            checked = {
                r["scope"]: store.instant(r["checked_at"])
                for r in db.execute(
                    "SELECT scope,checked_at FROM rf_cache WHERE season=?", (year,)
                )
            }
            native, _ = admission._raw_rows(root, db, year, store.iso(at))
        secondary = independent_results.sync(root, year, fixtures, at)
        due = {}
        for f in fixtures:
            age = at - store.instant(f["kickoff"])
            rnd = f["round_id"]
            last = checked.get(str(rnd))
            if age < timedelta(minutes=90):
                continue
            events = native.get(f["fixture_id"], [])
            need = bool(
                events
                and events[-1]["score"] is not None
                and admission._anchor(f, events[-1])
                and admission._pair(events) is None
            )
            if f["home_goals"] is not None and not need and age > timedelta(days=28):
                continue
            recent = f["home_goals"] is None and age <= timedelta(hours=48, minutes=90)
            delay = admission.INTERVAL if need or recent else timedelta(hours=6)
            if last and at - last < delay:
                continue
            prior = due.get(rnd, (None, False, False))
            due[rnd] = (
                last or store.instant("2000-01-01T00:00:00Z"),
                need or prior[1],
                recent or prior[2],
            )
        for rnd, (_, need, recent) in sorted(
            due.items(),
            key=lambda item: (not (item[1][1] or item[1][2]), item[1][0], -item[0]),
        )[:3]:
            try:
                r = feed.sync(root, year, round_id=rnd, confirm=need, recent=recent)
                wet.append({"scope": rnd, "status": r["fetch_status"]})
            except (ValueError, OSError, KeyError, TypeError) as exc:
                wet.append({"scope": rnd, "status": "unavailable", "error": str(exc)})
    from . import schedule_consensus

    upcoming = schedule_consensus.sync(root, year, fetch=not offline)
    report = admission.reconcile(root, year)
    score = publications(root)
    report.update(
        upcoming_schedules={
            k: upcoming[k]
            for k in ("registered", "previously_matched", "corrected", "matched")
        },
        fetches=wet,
        fallback=fallback,
        secondary=secondary,
        freshness=result_freshness.status(root, year),
        rounds=score["rounds"],
        manifest=score["manifest"],
        training=score["training"],
    )
    store.save_report(root, "source", f"epl-regulation-{year}", report)
    return report


def _raw_file(file):
    path = Path(file)
    stat = path.stat()
    if not path.is_file() or not 0 < stat.st_size <= 16 * 1024 * 1024:
        raise ValueError("empty, non-file or oversized manual result evidence")
    return path.read_bytes()


def _batch(root, raw, year, rnd, file):
    obj = json.loads(raw)
    if (
        obj.get("season") != admission.scope(year)
        or type(obj.get("round")) is not int
        or obj["round"] != rnd
        or obj.get("rights") not in {"user-owned", "permission"}
        or not isinstance(obj.get("source"), str)
        or not obj["source"].strip()
        or not isinstance(obj.get("matches"), list)
        or not obj["matches"]
    ):
        raise ValueError(
            "require match records, source identity and user-owned or permission evidence"
        )
    relative = Path(obj["evidence_file"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(
            "evidence_file must name a separate local attachment next to the batch"
        )
    evidence = (Path(file).parent / relative).resolve()
    if evidence == Path(file).resolve():
        raise ValueError("the batch and original evidence must be separate")
    attachment = _raw_file(evidence)
    sha = hashlib.sha256(attachment).hexdigest()
    if sha != obj.get("evidence_sha256"):
        raise ValueError(
            "original match report hash differs from the declared evidence"
        )
    manifest = store.archive_bytes(
        root,
        "licensed-manual-round-manifest",
        raw,
        metadata={"season": obj["season"], "round": rnd},
    )
    store.archive_bytes(
        root,
        "licensed-manual-round-original",
        attachment,
        media_type="application/octet-stream",
        metadata={
            "source": obj["source"],
            "rights": obj["rights"],
            "manifest": manifest["sha256"],
        },
    )
    return obj, manifest["sha256"], sha


def manual(root, year, rnd, file, *, whole_round=True):
    from . import independent_results, result_pairings
    from .football import fixture_id

    raw = _raw_file(file)
    obj, ident, sha = _batch(root, raw, year, rnd, file)
    with closing(admission.database(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT detail,rollback FROM rf_batches WHERE id=?", (ident,)
        ).fetchone()
        if previous:
            report = json.loads(previous["detail"])
            if whole_round and report.get("scope") == "selected_matches":
                raise ValueError(
                    "the whole registered matchweek is required in one batch"
                )
            if previous["rollback"]:
                report["rolled_back"] = json.loads(previous["rollback"])
        else:
            at = store.iso(feed.now_utc())
            fixtures = {
                f["fixture_id"]: f
                for f in store.eligible_fixture_rows(db, season=obj["season"])
                if f["round_id"] == rnd
            }
            native, _ = admission._raw_rows(root, db, year, at)
            independent = independent_results.verified(root, db, year, at)
            reference = admission._reference(db, root, obj["season"], at)
            unique = result_pairings.unique_fixtures(fixtures.values())
            seen = set()
            rows = []
            for r in obj["matches"]:
                fid = fixture_id(obj["season"], r["home"], r["away"])
                f = fixtures.get(fid)
                actual = store.iso(r["kickoff"])
                if (
                    not f
                    or fid in seen
                    or fid not in unique
                    or r.get("fixture_id", fid) != fid
                    or f["precision"] != "minute"
                    or store.instant(at) < store.instant(actual) + timedelta(minutes=90)
                    or r.get("status") != "final"
                    or r.get("period") != "regulation"
                    or not str(r.get("locator") or "").strip()
                    or any(
                        type(r.get(k)) is not int or r[k] < 0
                        for k in ("home_goals", "away_goals")
                    )
                ):
                    raise ValueError(
                        "invalid, duplicate or unmatched regulation match and source locator"
                    )
                ol = native.get(fid, [])
                other = independent.get(fid, [])
                evidence = ol[-1:] + other[-1:]
                if any(
                    (e.get("round") is not None and e["round"] != rnd)
                    or (e.get("kickoff") and e["kickoff"] != actual)
                    or e.get("status") in {"conflict", "withdrawn"}
                    or e.get("error")
                    for e in evidence
                ):
                    raise ValueError(
                        "manual kickoff/fixture identity conflicts with archived sources"
                    )
                if actual != f["kickoff"] and (
                    not any(
                        e.get("kickoff") == actual and e.get("round") == rnd
                        for e in evidence
                    )
                    or not str(obj.get("reviewer") or "").strip()
                    or not str(r.get("decision_reason") or "").strip()
                ):
                    raise ValueError(
                        "a different kickoff needs a matching source receipt, reviewer and decision_reason"
                    )
                seen.add(fid)
                prior = admission.last_result(db, fid, f["kickoff"])
                state = admission.last_disposition(db, fid, f["kickoff"], at)
                reviewed = admission.pairing_review(db, fid, f["kickoff"], at)
                if reviewed and reviewed["action"] == "withdraw":
                    raise ValueError(
                        "a withdrawn result pairing requires an explicit review before manual scoring"
                    )
                score = [r["home_goals"], r["away_goals"]]
                cited = native.get(fid, [])
                if (
                    cited
                    and cited[-1]["score"] is not None
                    and cited[-1]["score"] != score
                    or other
                    and other[-1]["score"] is not None
                    and other[-1]["score"] != score
                    or reference.get(fid, score) != score
                    or prior
                    and [prior["home_goals"], prior["away_goals"]] != score
                    or state
                    and state["held"]
                ) and not str(r.get("decision_reason") or "").strip():
                    raise ValueError(
                        "a disputed or corrected score requires decision_reason"
                    )
                rows.append(
                    (
                        f,
                        r,
                        score,
                        dict(prior) if prior else None,
                        dict(state) if state else None,
                    )
                )
            if whole_round and set(fixtures) != seen:
                raise ValueError(
                    "the whole registered matchweek is required in one batch"
                )
            items = []
            for f, r, score, prior, state in rows:
                row = admission._result(
                    db,
                    f,
                    score,
                    "manual-regulation",
                    f"{obj['source']} {r['locator']} / sha256:{sha} / batch:{ident}",
                    at,
                    actual_kickoff=store.iso(r["kickoff"]),
                )
                admission.disposition(db, f, False, "licensed_manual_review", at)
                items.append(
                    {
                        "fixture": f["fixture_id"],
                        "kickoff": f["kickoff"],
                        "actual_kickoff": store.iso(r["kickoff"]),
                        "score": score,
                        "previous": prior,
                        "state": state,
                        "version": row["version"],
                        "reason": r.get("decision_reason"),
                    }
                )
            report = {
                "batch_id": ident,
                "scope": "whole_round" if seen == set(fixtures) else "selected_matches",
                "season": obj["season"],
                "round": rnd,
                "at": at,
                "evidence_sha256": sha,
                "rights": obj["rights"],
                "source": obj["source"],
                "reviewer": obj.get("reviewer"),
                "items": items,
                "status": "applied",
            }
            db.execute(
                "INSERT INTO rf_batches VALUES(?,?,?,?,?,?,NULL)",
                (ident, obj["season"], rnd, at, sha, store.canonical(report)),
            )
            admission.enqueue(db, obj["season"], rnd)
            store.put_report(db, "source", "manual-round/" + ident, report)
    return report | {"maintenance": publications(root)}


def rollback(root, ident, reason):
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a rollback reason is required")
    with closing(admission.database(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        at = store.iso(feed.now_utc())
        row = db.execute("SELECT * FROM rf_batches WHERE id=?", (ident,)).fetchone()
        if not row:
            raise ValueError("unknown manual result batch")
        if row["rollback"]:
            result = json.loads(row["rollback"])
        else:
            details = json.loads(row["detail"])
            fixtures = {
                f["fixture_id"]: f
                for f in store.eligible_fixture_rows(db, season=row["season"])
            }
            changes = []
            for item in details["items"]:
                f = fixtures[item["fixture"]]
                current = admission.last_result(db, item["fixture"], f["kickoff"])
                state = admission.last_disposition(
                    db, item["fixture"], f["kickoff"], at
                )
                if (
                    not current
                    or current["version"] != item["version"]
                    or f["kickoff"] != item["kickoff"]
                    or state
                    and state["at"] > row["at"]
                ):
                    raise ValueError(
                        "later result or schedule change prevents this batch rollback"
                    )
                changes.append((f, item))
            for f, item in changes:
                prior = item["previous"]
                old = item["state"]
                if prior:
                    admission._result(
                        db,
                        f,
                        [prior["home_goals"], prior["away_goals"]],
                        prior["source"],
                        prior["locator"],
                        at,
                        actual_kickoff=prior.get("actual_kickoff"),
                    )
                held = not prior or bool(old and old["held"])
                admission.disposition(
                    db,
                    f,
                    held,
                    (old["reason"] if old and old["held"] else "manual_batch_withdrawn")
                    if held
                    else "prior_result_restored",
                    at,
                )
            result = {
                "batch_id": ident,
                "status": "rolled_back",
                "at": at,
                "reason": reason,
                "count": len(changes),
            }
            db.execute(
                "UPDATE rf_batches SET rollback=? WHERE id=?",
                (store.canonical(result), ident),
            )
            admission.enqueue(db, row["season"], row["round_id"])
            store.put_report(db, "incident", "manual-round-rollback/" + ident, result)
    return result | {"maintenance": publications(root)}
