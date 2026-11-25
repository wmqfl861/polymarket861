"""Reproducible input identities for historical and simultaneous prospective model comparisons."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path

from . import dbscope
from . import football, input_manifest, store

POLICY = "shared-available-inputs-v1"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS comparison_snapshots(digest TEXT PRIMARY KEY, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS league_source_objects(
    revision TEXT NOT NULL,season TEXT NOT NULL,git_blob TEXT NOT NULL,PRIMARY KEY(revision,season)
);
CREATE TABLE IF NOT EXISTS comparison_frames(
    fixture TEXT NOT NULL,kickoff TEXT NOT NULL,horizon TEXT NOT NULL,mode TEXT NOT NULL,protocol TEXT NOT NULL,
    digest TEXT NOT NULL,document TEXT NOT NULL,
    PRIMARY KEY(fixture,kickoff,horizon,mode,protocol)
);
"""


class SealedInputConflict(ValueError):
    """The key already belongs to a different frozen historical input set."""


def _open(data_dir):
    db = store.connect(data_dir)
    dbscope.prepare(db, "paired-proof", _SCHEMA)
    return db


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _retain(data_dir, raw, kind):
    sha = _sha(raw)
    path = store.blob_path(data_dir, sha)
    if path.is_file() and _sha(store.read_blob(data_dir, sha)) == sha:
        with closing(store.connect(data_dir)) as db:
            tracked = db.execute(
                "SELECT 1 FROM observations WHERE sha256=? LIMIT 1", (sha,)
            ).fetchone()
        if tracked:
            return sha
    return store.archive_bytes(data_dir, kind, raw)["sha256"]


def _trusted(db, files, evidence_sha):
    if (
        evidence_sha is None
        or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='football_input_manifests'"
        ).fetchone()
    ):
        return None
    for row in db.execute(
        "SELECT digest,manifest FROM football_input_manifests ORDER BY observed_at DESC"
    ):
        m = json.loads(row["manifest"])
        if store.digest(m) != row["digest"]:
            raise ValueError("invalid locally observed source manifest")
        if files == m["files"] and evidence_sha == m["evidence_sha256"]:
            return row["digest"]
    return None


def attest_provider_files(data_dir, revision, expected):
    # Called only by the downloader after the public provider's Git objects have
    # been checked. A local fixture import cannot certify an upstream EPL corpus.
    with closing(_open(data_dir)) as db, db:
        for season, sha in expected.items():
            old = db.execute(
                "SELECT git_blob FROM league_source_objects WHERE revision=? AND season=?",
                (revision, season),
            ).fetchone()
            if old and old[0] != sha:
                raise ValueError("provider revision names inconsistent league content")
            db.execute(
                "INSERT OR IGNORE INTO league_source_objects VALUES(?,?,?)",
                (revision, season, sha),
            )


def _verified_scope(db, ident, files, aux):
    if not ident:
        return False
    row = db.execute(
        "SELECT manifest FROM football_input_manifests WHERE digest=?", (ident,)
    ).fetchone()
    if not row:
        return False
    m = json.loads(row[0])
    if store.digest(m) != ident or files != m["files"] or m["evidence_sha256"] != aux:
        return False
    for f in files:
        if football.SOURCE_BLOBS.get(f["season"]) == f["git_blob"]:
            continue
        if not db.execute(
            "SELECT 1 FROM league_source_objects WHERE revision=? AND season=? AND git_blob=?",
            (m["revision"], f["season"], f["git_blob"]),
        ).fetchone():
            return False
    return True


def _snapshot_view(data_dir, doc, ident):
    base = Path(data_dir) / "comparison" / ident
    for f in doc["files"]:
        raw = store.read_blob(data_dir, f["sha256"])
        if _sha(raw) != f["sha256"] or football.blob_sha(raw) != f["git_blob"]:
            raise ValueError("historical source archive failed verification")
        input_manifest._install(base / "sources" / f["season"] / "en.1.json", raw)
    path = None
    if doc["evidence_sha256"] is not None:
        raw = store.read_blob(data_dir, doc["evidence_sha256"])
        if _sha(raw) != doc["evidence_sha256"]:
            raise ValueError("historical timing evidence failed verification")
        path = base / "evidence.jsonl"
        input_manifest._install(path, raw)
    root = base / "sources"
    if sorted(p.parent.name for p in root.glob("????-??/en.1.json")) != [
        f["season"] for f in doc["files"]
    ]:
        raise ValueError("unexpected season in historical snapshot")
    return {"digest": ident, "document": doc, "root": root, "evidence_path": path}


def capture_history(data_dir, root, evidence_path, lag):
    root = Path(root)
    paths = [root] if root.is_file() else sorted(root.glob("????-??/en.1.json"))
    if not paths:
        raise ValueError("no source season to archive")
    files = []
    for p in paths:
        season = p.parent.name
        football.season_year(season)
        if p.name != "en.1.json":
            raise ValueError("expected an EPL season/en.1.json")
        raw = p.read_bytes()
        files.append(
            {
                "season": season,
                "sha256": _retain(data_dir, raw, "replay-football-source"),
                "git_blob": football.blob_sha(raw),
            }
        )
    side = (
        _retain(data_dir, Path(evidence_path).read_bytes(), "replay-football-timing")
        if evidence_path is not None
        else None
    )
    with closing(_open(data_dir)) as db, db:
        doc = {
            "format": 1,
            "files": files,
            "evidence_sha256": side,
            "archive_lag_hours": lag,
            "observed_manifest": _trusted(db, files, side),
        }
        ident = store.digest(doc)
        db.execute(
            "INSERT OR IGNORE INTO comparison_snapshots VALUES(?,?)",
            (ident, store.canonical(doc)),
        )
    return _snapshot_view(data_dir, doc, ident)


def exported_replay_inputs(data_dir, ident):
    """Rebuild the byte-identical source and sidecar from an archived run, offline."""
    with closing(_open(data_dir)) as db:
        row = db.execute(
            "SELECT document FROM comparison_snapshots WHERE digest=?", (ident,)
        ).fetchone()
    if not row:
        raise ValueError("unknown historical snapshot")
    doc = json.loads(row[0])
    if store.digest(doc) != ident:
        raise ValueError("damaged historical snapshot")
    result = _snapshot_view(data_dir, doc, ident)
    return {
        "digest": ident,
        "root": str(result["root"]),
        "evidence_path": str(result["evidence_path"])
        if result["evidence_path"]
        else None,
        **doc,
    }


def frozen_manifest(data_dir, ident):
    with closing(store.connect(data_dir)) as db:
        row = db.execute(
            "SELECT manifest FROM football_input_manifests WHERE digest=?", (ident,)
        ).fetchone()
    if not row or store.digest(json.loads(row[0])) != ident:
        raise ValueError("missing or damaged frozen football manifest")
    return input_manifest._materialize(data_dir, json.loads(row[0]), ident)


def live_frame(data_dir, key, build):
    """All workers and retries share the first frame durably chosen for this decision."""
    where = "fixture=? AND kickoff=? AND horizon=? AND mode=? AND protocol=?"
    with closing(_open(data_dir)) as db:
        row = db.execute(
            "SELECT digest,document FROM comparison_frames WHERE " + where, key
        ).fetchone()
    if not row:
        frame = build()
        with closing(_open(data_dir)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO comparison_frames VALUES(?,?,?,?,?,?,?)",
                (*key, store.digest(frame), store.canonical(frame)),
            )
            row = db.execute(
                "SELECT digest,document FROM comparison_frames WHERE " + where, key
            ).fetchone()
    body = json.loads(row["document"])
    if store.digest(body) != row["digest"]:
        raise ValueError("frozen decision frame failed verification")
    return body | {"digest": row["digest"]}


def make_proof(
    fixture,
    cutoff,
    snapshot_digest,
    context,
    training,
    revisions,
    *,
    context_verified=True,
    frame_digest=None,
):
    cutoff = store.iso(cutoff)
    observed = all(
        all(
            f.get(k) and store.iso(f[k]) <= cutoff
            for k in ("published_at", "received_at", "first_recorded_at")
        )
        for f in context.get("facts", [])
    )
    body = {
        "policy": POLICY,
        "fixture_id": fixture["fixture_id"],
        "season": fixture["season"],
        "round_id": fixture["round_id"],
        "kickoff": store.iso(fixture["kickoff"]),
        "input_cutoff": cutoff,
        "snapshot": snapshot_digest,
        "training": training["input_sha256"],
        "last_result": training["latest_result_available_at"],
        "schedule_revisions": revisions,
        "result_grades": training["result_grades"],
        "schedule_grade": training["schedule_grade"],
        "context": store.digest(context),
        "context_timely": observed and context_verified,
        "frame": frame_digest,
    }
    return body | {"sha256": store.digest(body)}


def from_seal(payload):
    """Bind a declared proof to the evidence and clocks in the immutable forecast."""
    proof = payload.get("comparison_proof")
    if not isinstance(proof, dict):
        return None
    evidence = payload.get("evidence", {})
    if (
        proof.get("training") != evidence.get("input_sha256")
        or proof.get("last_result") != evidence.get("latest_result_available_at")
        or proof.get("result_grades") != evidence.get("result_grades")
    ):
        return None
    if (
        proof.get("input_cutoff") != payload.get("input_cutoff")
        or proof.get("fixture_id") != payload.get("fixture_id")
        or proof.get("kickoff") != payload.get("kickoff")
        or proof.get("context") != store.digest(payload.get("context"))
    ):
        return None
    if proof.get("snapshot") != payload.get("input_manifest_sha256"):
        return None
    return proof


def check_replay_cache(payload, proof):
    if not payload.get("comparison_proof") or payload["comparison_proof"] != proof:
        raise SealedInputConflict(
            "A replay for this recipe and decision was already sealed with different inputs; use a new model version to keep both editions."
        )


class PairValidator:
    """Check seal-sourced input proofs against independent local archives once per assessment."""

    def __init__(self, data_dir):
        self.root = data_dir
        self.sources = {}
        self.live_scopes = {}

    def _historical(self, ident):
        from . import historical_inputs

        if ident in self.sources:
            return self.sources[ident]
        self.sources[ident] = {}
        with closing(_open(self.root)) as db:
            row = db.execute(
                "SELECT document FROM comparison_snapshots WHERE digest=?", (ident,)
            ).fetchone()
            if not row:
                return {}
            doc = json.loads(row[0])
            if store.digest(doc) != ident or doc["archive_lag_hours"] is not None:
                return {}
        view = _snapshot_view(self.root, doc, ident)
        if not historical_inputs.inspect(self.root, view)["timing_authenticated"]:
            return {}
        items = football.load_fixtures(
            view["root"], evidence_path=view["evidence_path"]
        )
        raw_outcomes = {
            f.fixture_id: (f.home_goals, f.away_goals)
            for f in football.load_fixtures(view["root"])
        }
        seasons = {}
        for f in items:
            seasons.setdefault(f.season, []).append(f)
        authentic = {}
        complete_years = set()
        for season, group in seasons.items():
            names = {football.team_key(n) for f in group for n in (f.home, f.away)}
            if (
                len(group) != 380
                or len(names) != 20
                or len({f.fixture_id for f in group}) != 380
            ):
                continue
            # The trusted manifest attests the actual EPL Git blobs. Also check the
            # season's complete double round robin and the canonical fixture IDs.
            if any(
                f.fixture_id != football.fixture_id(season, f.home, f.away)
                for f in group
            ):
                continue
            if all(raw_outcomes[f.fixture_id][0] is not None for f in group):
                complete_years.add(football.season_year(season))
            authentic.update(
                {
                    f.fixture_id: f
                    for f in group
                    if f.home_goals is not None
                    and (f.home_goals, f.away_goals) == raw_outcomes[f.fixture_id]
                }
            )
        # The fixed team model uses three complete preceding seasons and the
        # current season up to the decision. Truncating the corpus changes B0/B1.
        authentic = {
            k: f
            for k, f in authentic.items()
            if all(
                football.season_year(f.season) - delta in complete_years
                for delta in (1, 2, 3)
            )
        }
        self.sources[ident] = authentic
        return authentic

    def check(self, c, b, fixture, mode):
        p = c.get("comparison_proof")
        q = b.get("comparison_proof")
        if not isinstance(p, dict) or p != q:
            return "different_or_unproved_inputs"
        required = {
            "fixture_id",
            "season",
            "round_id",
            "kickoff",
            "input_cutoff",
            "last_result",
            "context_timely",
            "result_grades",
            "snapshot",
            "frame",
            "context",
            "training",
            "schedule_grade",
            "schedule_revisions",
        }
        if (
            not required <= p.keys()
            or p.get("policy") != POLICY
            or store.digest({k: v for k, v in p.items() if k != "sha256"})
            != p.get("sha256")
        ):
            return "invalid_input_proof"
        if (
            p["fixture_id"] != fixture["fixture_id"]
            or p["season"] != fixture["season"]
            or p["round_id"] != fixture["round_id"]
            or p["kickoff"] != store.iso(fixture["kickoff"])
        ):
            return "fixture_identity_mismatch"
        cut = store.iso(c["cutoff"])
        if (
            p["input_cutoff"] > cut
            or store.iso(p["last_result"]) > p["input_cutoff"]
            or not p["context_timely"]
            or "lag_assumption" in p["result_grades"]
        ):
            return "unverified_input_timing"
        if c["recipe_digest"] == b["recipe_digest"] and any(
            abs(c["probabilities"][k] - b["probabilities"][k]) > 1e-8
            for k in football.PROB_KEYS
        ):
            return "identical_recipe_disagrees"
        if mode == "replay":
            if p["frame"] is not None or p["input_cutoff"] != cut:
                return "incorrect_historical_cutoff"
            try:
                f = self._historical(p["snapshot"]).get(fixture["fixture_id"])
            except (OSError, KeyError, ValueError, TypeError):
                return "unavailable_or_invalid_snapshot"
            if (
                not f
                or f.kickoff_precision != "minute"
                or store.iso(f.kickoff) != p["kickoff"]
                or f.round != fixture["round_id"]
                or (f.home_goals, f.away_goals)
                != (fixture["home_goals"], fixture["away_goals"])
            ):
                return "unverified_historical_fixture"
            known = [
                entry for entry in f.schedule_versions if store.iso(entry[0]) <= cut
            ]
            if not known or store.iso(known[-1][1]) != p["kickoff"]:
                return "unverified_historical_schedule"
        else:
            with closing(_open(self.root)) as db:
                row = db.execute(
                    "SELECT digest,document FROM comparison_frames WHERE fixture=? AND kickoff=? AND horizon='T-60m' AND mode='formal' AND protocol=?",
                    (fixture["fixture_id"], p["kickoff"], store.PROTOCOL),
                ).fetchone()
            if not row or row["digest"] != p["frame"]:
                return "unverified_live_frame"
            scope = p["snapshot"]
            if scope not in self.live_scopes:
                with closing(_open(self.root)) as db:
                    registered = db.execute(
                        "SELECT manifest FROM football_input_manifests WHERE digest=?",
                        (scope,),
                    ).fetchone()
                    m = json.loads(registered[0]) if registered else {}
                    self.live_scopes[scope] = bool(
                        m
                        and _verified_scope(db, scope, m["files"], m["evidence_sha256"])
                    )
            if not self.live_scopes[scope]:
                return "unverified_live_source"
            doc = json.loads(row["document"])
            if (
                store.digest(doc) != p["frame"]
                or doc["manifest"] != p["snapshot"]
                or doc["cutoff"] != p["input_cutoff"]
                or store.digest(doc["context"]) != p["context"]
            ):
                return "unverified_live_frame"
        return None
