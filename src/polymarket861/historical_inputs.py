"""Authenticate a historical run against input bytes and the first local receipt of an upstream EPL object."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from dataclasses import replace
from functools import lru_cache

from . import football, paired_proof, store

_SCHEMA = """CREATE TABLE IF NOT EXISTS historical_study_receipts(
    report_sha256 TEXT PRIMARY KEY, snapshot_sha256 TEXT NOT NULL, evidence_sha256 TEXT NOT NULL
)"""
_LOCATOR = "https://api.github.com/repos/openfootball/football.json/git/blobs/"


def _authorized_object(db, season, git_sha, revision):
    return football.SOURCE_BLOBS.get(season) == git_sha or bool(
        db.execute(
            "SELECT 1 FROM league_source_objects WHERE revision=? AND season=? AND git_blob=?",
            (revision, season, git_sha),
        ).fetchone()
    )


def _check_rows(db, data_dir, raw_fixtures, rows, side_sha):
    """Reopen the provider season named by every assertion and verify time, identity and value."""
    by_id = {f.fixture_id: f for f in raw_fixtures}
    receipts = {}
    for raw in db.execute(
        "SELECT sha256,observed_at,metadata FROM observations WHERE source='openfootball'"
    ):
        meta = json.loads(raw["metadata"])
        season, git_sha, revision = (
            meta.get(k) for k in ("season", "git_blob", "revision")
        )
        if season and git_sha and _authorized_object(db, season, git_sha, revision):
            key = (season, _LOCATOR + git_sha)
            receipts.setdefault(key, {})[store.iso(raw["observed_at"])] = (
                raw["sha256"],
                git_sha,
            )
    has_side = db.execute(
        "SELECT 1 FROM observations WHERE source='openfootball-sidecar' AND sha256=?",
        (side_sha,),
    ).fetchone()
    if not has_side:
        return "missing_observed_sidecar"

    @lru_cache(maxsize=25)
    def archived_season(season, sha256, git_sha):
        raw = store.blob_path(data_dir, sha256).read_bytes()
        if (
            hashlib.sha256(raw).hexdigest() != sha256
            or football.blob_sha(raw) != git_sha
        ):
            raise ValueError("upstream historical receipt failed content validation")
        payload = json.loads(raw)
        matches = payload["matches"] if isinstance(payload, dict) else payload
        parsed = [football._parse_match(season, m, (), None, git_sha) for m in matches]
        if len({f.fixture_id for f in parsed}) != len(parsed):
            raise ValueError("duplicate identity in archived historical evidence")
        return {f.fixture_id: f for f in parsed if f.status != "quarantined"}

    latest = {}
    for row in rows:
        target = by_id.get(row.get("fixture_id"))
        if not target or target.status == "quarantined":
            return "unknown_or_quarantined_fixture"
        is_schedule = "schedule_observed_at" in row
        # Locally archived EPL snapshots attest an upper bound, never an invented exact publication time.
        allowed = (
            {"fixture_id", "source", "kickoff", "schedule_observed_at"}
            if is_schedule
            else {
                "fixture_id",
                "source",
                "score",
                "archive_at",
                "published_by",
                "finalized_by",
            }
        )
        if set(row) != allowed:
            return "unwitnessed_evidence_fields"
        at = store.iso(
            row["schedule_observed_at"] if is_schedule else row["archive_at"]
        )
        origin = receipts.get((target.season, row["source"]), {}).get(at)
        if not origin:
            return "missing_matching_provider_receipt"
        recorded = archived_season(target.season, *origin).get(target.fixture_id)
        if not recorded or recorded.round != target.round:
            return "archived_fixture_disagrees"
        if is_schedule:
            if (
                recorded.kickoff_precision != "minute"
                or recorded.kickoff != store.instant(row["kickoff"])
            ):
                return "archived_schedule_disagrees"
        else:
            score = tuple(row["score"])
            if (
                store.iso(row["published_by"]) != at
                or store.iso(row["finalized_by"]) != at
                or store.instant(at) <= recorded.kickoff
                or recorded.kickoff != target.kickoff
                or score != (recorded.home_goals, recorded.away_goals)
            ):
                return "archived_score_or_clock_disagrees"
            if target.fixture_id not in latest or at > latest[target.fixture_id][0]:
                latest[target.fixture_id] = at, score
    if any(
        (by_id[fid].home_goals, by_id[fid].away_goals) != score
        for fid, (_, score) in latest.items()
    ):
        return "terminal_label_disagrees_with_season_archive"
    return None


def inspect(data_dir, view):
    doc = view["document"]
    files = doc["files"]
    side_sha = doc["evidence_sha256"]
    pinned = all(football.SOURCE_BLOBS.get(f["season"]) == f["git_blob"] for f in files)
    revision = football.SOURCE_COMMIT if pinned else None
    with closing(paired_proof._open(data_dir)) as db:
        observed = doc["observed_manifest"]
        bound = bool(
            observed and paired_proof._verified_scope(db, observed, files, side_sha)
        )
        if bound and not pinned:
            row = db.execute(
                "SELECT manifest FROM football_input_manifests WHERE digest=?",
                (observed,),
            ).fetchone()
            revision = json.loads(row[0])["revision"]
        reason = None
        if not pinned and not bound:
            reason = "unverified_source_bytes"
        elif side_sha is not None:
            if not bound:
                reason = "unregistered_source_and_sidecar_combination"
            else:
                try:
                    reason = _check_rows(
                        db,
                        data_dir,
                        football.load_fixtures(view["root"]),
                        football.read_json_rows(view["evidence_path"]),
                        side_sha,
                    )
                except (OSError, ValueError, TypeError, KeyError):
                    reason = "missing_or_invalid_underlying_archive"
    timed = side_sha is not None and not reason
    evidence = {
        "policy": "upstream-bytes-local-receipt-v1",
        "snapshot": view["digest"],
        "season_files": files,
        "sidecar_sha256": side_sha,
        "observed_manifest": observed,
        "source_authenticated": pinned or bound,
        "source_revision": revision,
        "timing_authenticated": timed,
        "study_type": "sensitivity"
        if doc["archive_lag_hours"] is not None
        else "strict_coverage",
        "archive_lag_hours": doc["archive_lag_hours"],
        "status": "research_only"
        if reason
        else ("verified_local_receipts" if timed else "source_without_receipt_history"),
        "reason": reason,
    }
    return evidence


def load_for_study(data_dir, root, evidence, lag):
    view = paired_proof.capture_history(data_dir, root, evidence, lag)
    provenance = inspect(data_dir, view)
    fixtures = football.load_fixtures(
        view["root"], evidence_path=view["evidence_path"], archive_lag_hours=lag
    )
    if evidence is not None:
        raw = {f.fixture_id: f for f in football.load_fixtures(view["root"])}
        # The target labels are those of the verified input season. An observation history may vary over time.
        fixtures = [
            replace(
                f,
                home_goals=raw[f.fixture_id].home_goals,
                away_goals=raw[f.fixture_id].away_goals,
                status=raw[f.fixture_id].status,
            )
            for f in fixtures
        ]
    return fixtures, provenance


def label_research_inputs(fixtures):
    return [
        replace(
            f,
            result_versions=tuple(
                v
                if v.grade == "lag_assumption"
                else replace(v, grade="unverified_input")
                for v in f.result_versions
            ),
            schedule_grades=tuple(
                "unverified_input_schedule" for _ in f.schedule_versions
            ),
        )
        if f.result_versions or f.schedule_versions
        else f
        for f in fixtures
    ]


def record(db, report):
    p = report["input_provenance"]
    db.execute(_SCHEMA)
    db.execute(
        "INSERT OR IGNORE INTO historical_study_receipts VALUES(?,?,?)",
        (store.digest(report), p["snapshot"], store.digest(p)),
    )


def authenticated(data_dir, report):
    proof = report.get("input_provenance")
    if (
        not isinstance(proof, dict)
        or proof.get("reason")
        or proof.get("status")
        not in ("verified_local_receipts", "source_without_receipt_history")
    ):
        return False
    if proof.get("archive_lag_hours") != report.get("protocol", {}).get(
        "archive_lag_hours"
    ):
        return False
    try:
        if report.get("source", {}).get("blobs") != {
            r["season"]: r["git_blob"] for r in proof["season_files"]
        }:
            return False
        with closing(paired_proof._open(data_dir)) as db:
            db.execute(_SCHEMA)
            row = db.execute(
                "SELECT snapshot_sha256,evidence_sha256 FROM historical_study_receipts WHERE report_sha256=?",
                (store.digest(report),),
            ).fetchone()
            if not row or (row[0], row[1]) != (proof["snapshot"], store.digest(proof)):
                return False
            obj = db.execute(
                "SELECT document FROM comparison_snapshots WHERE digest=?", (row[0],)
            ).fetchone()
            if not obj:
                return False
            doc = json.loads(obj[0])
            if store.digest(doc) != row[0]:
                return False
        view = paired_proof._snapshot_view(data_dir, doc, proof["snapshot"])
        return inspect(data_dir, view) == proof
    except (ValueError, KeyError, TypeError, OSError):
        return False
