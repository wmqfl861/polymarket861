"""Local, hash-addressed OpenFootball inputs with portable, independently archived provenance."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from .score_bounds import INVALID_SCORE, valid_score
from . import dbscope
from . import football
from .store import archive_bytes, read_blob, canonical, connect, digest, iso, now_utc

_SCHEMA = """
CREATE TABLE IF NOT EXISTS football_input_manifests(
    digest TEXT PRIMARY KEY, observed_at TEXT NOT NULL, manifest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS football_input_history(
    fixture_id TEXT NOT NULL, season TEXT NOT NULL, kind TEXT NOT NULL, known_at TEXT NOT NULL,
    value TEXT NOT NULL, record TEXT NOT NULL,
    PRIMARY KEY(fixture_id,kind,known_at)
);
"""


def _db(data_dir):
    db = connect(data_dir)
    dbscope.prepare(db, "input-manifest", _SCHEMA)
    return db


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _install(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and not path.is_symlink():
        if _hash(path.read_bytes()) != _hash(data):
            raise ValueError(
                f"stored football input differs from its verified digest: {path}"
            )
        return
    if path.exists() or path.is_symlink():
        raise ValueError(f"invalid football input path: {path}")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def remember_rows(db, season, rows):
    """Retain first sight of a value and subsequent changes, including withdrawals."""
    for row in rows:
        fid = row["fixture_id"]
        kind = "schedule" if "schedule_observed_at" in row else "score"
        at = row["schedule_observed_at"] if kind == "schedule" else row["archive_at"]
        value = canonical(row["kickoff"] if kind == "schedule" else row.get("score"))
        existing = db.execute(
            "SELECT value FROM football_input_history WHERE fixture_id=? AND kind=? AND known_at=?",
            (fid, kind, at),
        ).fetchone()
        if existing:
            if existing[0] != value:
                raise ValueError(
                    "conflicting football evidence at the same observed time"
                )
            continue
        previous = db.execute(
            "SELECT value FROM football_input_history WHERE fixture_id=? AND kind=? AND known_at<? "
            "ORDER BY known_at DESC LIMIT 1",
            (fid, kind, at),
        ).fetchone()
        if previous and previous[0] == value:
            continue
        db.execute(
            "INSERT INTO football_input_history VALUES(?,?,?,?,?,?)",
            (fid, season, kind, at, value, canonical(row)),
        )


def _effective_rows(db, fixtures):
    """Apply locally observed adjudications to the historical version stream."""
    ids = {f.fixture_id: f for f in fixtures if f.status != "quarantined"}
    streams = defaultdict(dict)
    for fid, kind, ts, record in db.execute(
        "SELECT fixture_id,kind,known_at,record FROM football_input_history"
    ):
        if fid in ids:
            streams[fid, kind][ts] = json.loads(record)
    authoritative_schedules = {}
    for row in db.execute(
        "SELECT f.*,s.recorded_at FROM fixture_versions f JOIN schedule_receipts s "
        "USING(fixture_id,version) WHERE s.precision='minute' AND f.source!='openfootball' "
        "ORDER BY f.known_at,f.version"
    ):
        fid = row["fixture_id"]
        if fid not in ids:
            continue
        at = max(row["known_at"], row["recorded_at"])
        versions = streams[fid, "schedule"]
        for k in list(versions):
            if k >= at:
                del versions[k]
        versions[at] = {
            "fixture_id": fid,
            "kickoff": row["kickoff"],
            "schedule_observed_at": at,
            "source": f"{row['source']}:{row['locator']}; schedule v{row['version']}",
        }
        authoritative_schedules[fid] = row["kickoff"]
    # A correction's local admission time also bounds its use. An operator can
    # record accurately cited older publication times without backdating live evidence.
    from .schedule_consensus import result_links

    proven_schedules, later_checks = result_links(db)
    reviewed = defaultdict(list)
    bound_results = set()
    invalid_openfootball = set()
    adjudicated = set()
    for r in db.execute(
        "SELECT r.*,s.kickoff,t.actual_kickoff FROM result_versions r JOIN result_schedule_refs s "
        "USING(fixture_id,version) LEFT JOIN result_event_times t USING(fixture_id,version) ORDER BY recorded_at,version"
    ):
        fid = r["fixture_id"]
        if fid not in ids:
            continue
        key = (fid, r["kickoff"])
        if r["source"] == "openfootball":
            # The ordinary archive uses its own receipt stream. Replay legacy
            # corrupt rows and their corrections without overwriting an adjudication.
            if not valid_score((r["home_goals"], r["away_goals"])):
                invalid_openfootball.add(key)
            elif key not in invalid_openfootball or key in adjudicated:
                continue
        else:
            adjudicated.add(key)
        at = max(r["recorded_at"], r["received_at"])
        raw_kickoff = iso(ids[fid].kickoff)
        adjusted = proven_schedules.get((fid, r["kickoff"]))
        if r["source"] == "verified-result-pairing" and r["actual_kickoff"]:
            bound_results.add((fid, r["kickoff"]))
        elif r["kickoff"] != raw_kickoff:
            if (
                not adjusted
                or adjusted[0] > at
                or authoritative_schedules.get(fid) != r["kickoff"]
            ):
                continue
            if at <= raw_kickoff:
                at = later_checks.get((fid, r["version"]), at)
        if at <= (r["actual_kickoff"] or raw_kickoff):
            continue
        reviewed[fid].append((at, r))
    for fid, changes in reviewed.items():
        versions = streams[fid, "score"]
        first = changes[0][0]
        # After a locally recorded adjudication, a later contradictory archive
        # cannot displace it. Before that point preserve the actual input history.
        for at in list(versions):
            if at >= first:
                del versions[at]
        for at, r in changes:
            versions[at] = {
                "fixture_id": fid,
                "score": [r["home_goals"], r["away_goals"]],
                "finalized_by": at,
                "published_by": at,
                "archive_at": at,
                "source": f"{r['source']}:{r['locator']}; result v{r['version']}",
            }
            if r["actual_kickoff"]:
                versions[at]["result_kickoff"] = r["actual_kickoff"]
            if r["source"] == "verified-result-pairing":
                versions[at]["result_pairing"] = r["locator"]
            adjusted = proven_schedules.get((fid, r["kickoff"]))
            if adjusted and r["kickoff"] != iso(ids[fid].kickoff):
                versions[at]["schedule_proof"] = adjusted[1]
                versions[at]["original_result_receipt"] = max(
                    r["received_at"], r["recorded_at"]
                )
    states = defaultdict(list)
    for change in db.execute("SELECT * FROM result_dispositions ORDER BY at"):
        fid = change["fixture_id"]
        if fid in ids and (
            change["kickoff"] == iso(ids[fid].kickoff)
            or (fid, change["kickoff"]) in proven_schedules
            or (fid, change["kickoff"]) in bound_results
        ):
            states[fid].append(change)
    for fid, changes in states.items():
        versions = streams[fid, "score"]
        original = dict(versions)
        for idx, change in enumerate(changes):
            at = change["at"]
            if change["held"]:
                until = changes[idx + 1]["at"] if idx + 1 < len(changes) else None
                for stamp in list(versions):
                    if at <= stamp and (until is None or stamp < until):
                        del versions[stamp]
                versions[at] = {
                    "fixture_id": fid,
                    "score": None,
                    "retracted": True,
                    "archive_at": at,
                    "published_by": at,
                    "finalized_by": at,
                    "source": change["reason"],
                }
                before = [stamp for stamp in original if stamp <= at]
                if before and original[max(before)].get("result_kickoff"):
                    versions[at]["result_kickoff"] = original[max(before)]["result_kickoff"]
                    if original[max(before)].get("result_pairing"):
                        versions[at]["result_pairing"] = original[max(before)]["result_pairing"]
                adjusted = proven_schedules.get((fid, change["kickoff"]))
                if adjusted:
                    versions[at]["schedule_proof"] = adjusted[1]
            elif at not in versions:
                prior = [stamp for stamp in original if stamp <= at]
                if prior:
                    versions[at] = original[max(prior)] | {
                        "archive_at": at,
                        "published_by": at,
                        "finalized_by": at,
                    }
    for (fid, kind), versions in streams.items():
        if kind == "schedule" and versions:
            authoritative_schedules[fid] = versions[max(versions)]["kickoff"]
    output = []
    for key, versions in sorted(streams.items()):
        for at, row in sorted(versions.items()):
            if key[1] == "score" and at < (row.get("result_kickoff") or iso(ids[key[0]].kickoff)):
                continue
            if key[1] == "score":
                if not row.get("retracted") and not valid_score(row.get("score")):
                    # Keep the original version and its knowledge time. A corrupt
                    # revision withdraws the prior score until a valid correction.
                    row = row | {"score": None, "retracted": True, "source": INVALID_SCORE}
                effective = authoritative_schedules.get(
                    key[0], iso(ids[key[0]].kickoff)
                )
                if effective != iso(ids[key[0]].kickoff) and not row.get("result_pairing"):
                    approved = proven_schedules.get((key[0], effective))
                    if not approved or row.get("schedule_proof") != approved[1]:
                        continue
            output.append(row)
    return output


def _materialize(data_dir, manifest, identity):
    data_dir = Path(data_dir).resolve()
    base = data_dir / "sources" / "openfootball" / manifest["revision"]
    scoped = data_dir / "datasets" / "football-inputs" / identity
    # A revision folder can contain seasons captured by a different request.
    # Use a per-manifest view for training so only attested files are consumed.
    src = scoped / "sources"
    for item in manifest["files"]:
        raw = read_blob(data_dir, item["sha256"])
        if _hash(raw) != item["sha256"] or football.blob_sha(raw) != item["git_blob"]:
            raise ValueError("football source archive failed its content hash")
        for folder in (base, src):
            _install(folder / item["season"] / "en.1.json", raw)
    seasons = {item["season"] for item in manifest["files"]}
    if {p.parent.name for p in src.glob("????-??/en.1.json")} != seasons:
        raise ValueError("unexpected season in the verified football input directory")
    aux = read_blob(data_dir, manifest["evidence_sha256"])
    if _hash(aux) != manifest["evidence_sha256"]:
        raise ValueError("football sidecar failed its archive hash")
    sidecar = scoped / "observations.jsonl"
    _install(sidecar, aux)
    return {
        "digest": identity,
        "root": str(src),
        "evidence_path": str(sidecar),
        "archive_root": str(base),
        "relative_root": src.relative_to(data_dir).as_posix(),
        "relative_evidence": sidecar.relative_to(data_dir).as_posix(),
        "evidence_sha256": manifest["evidence_sha256"],
    }


def _save(data_dir, revision, files, rows):
    data_dir = Path(data_dir)
    raw = b"".join((canonical(r) + "\n").encode() for r in rows)
    sha = _hash(raw)
    with closing(_db(data_dir)) as db:
        existing = db.execute(
            "SELECT 1 FROM observations WHERE source='openfootball-sidecar' AND sha256=? LIMIT 1",
            (sha,),
        ).fetchone()
    if not existing:
        archive_bytes(
            data_dir,
            "openfootball-sidecar",
            raw,
            media_type="application/x-ndjson",
            metadata={"revision": revision},
        )
    manifest = {
        "format": 1,
        "revision": revision,
        "files": sorted(files, key=lambda r: r["season"]),
        "evidence_sha256": sha,
    }
    identity = digest(manifest)
    with closing(_db(data_dir)) as db, db:
        db.execute(
            "INSERT OR IGNORE INTO football_input_manifests VALUES(?,?,?)",
            (identity, now_utc(), canonical(manifest)),
        )
    return _materialize(data_dir, manifest, identity)


def create_observed_inputs(data_dir, revision, files, fixtures, rows_by_season):
    if not re.fullmatch("[0-9a-f]{40}", revision):
        raise ValueError("invalid source revision")
    with closing(_db(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        for season, rows in rows_by_season.items():
            remember_rows(db, season, rows)
        effective = _effective_rows(db, fixtures)
    return _save(data_dir, revision, files, effective)


def verify_observed_inputs(data_dir, root=None, evidence_path=None):
    """Validate the actual bytes and return the portable, adjudicated training view.

    An explicit external path must match a previously observed manifest. With no
    paths, the most recent observation is used, including after an offline restore.
    """
    data_dir = Path(data_dir).resolve()
    path = Path(root).expanduser().resolve() if root is not None else None
    explicit = (
        Path(evidence_path).expanduser().resolve()
        if evidence_path is not None
        else None
    )

    # A restored database may still contain a displayed path from its old host.
    # Only a valid, locally registered content address can rename that path.
    def moved(old, kind):
        if old is None or old.exists() or len(old.parts) < 4:
            return old
        suffix = old.parts[-4:]
        if (
            suffix[:2] != ("datasets", "football-inputs")
            or suffix[3] != kind
            or not re.fullmatch("[0-9a-f]{64}", suffix[2])
        ):
            return old
        with closing(_db(data_dir)) as db:
            found = db.execute(
                "SELECT manifest FROM football_input_manifests WHERE digest=?",
                (suffix[2],),
            ).fetchone()
        if not found:
            return old
        manifest = json.loads(found[0])
        if digest(manifest) != suffix[2]:
            raise ValueError("invalid relocated football manifest")
        view = _materialize(data_dir, manifest, suffix[2])
        return Path(view["root"] if kind == "sources" else view["evidence_path"])

    path = moved(path, "sources")
    explicit = moved(explicit, "observations.jsonl")
    if explicit is not None and not explicit.is_file():
        raise ValueError("the supplied football evidence file is missing")
    evidence_sha = _hash(explicit.read_bytes()) if explicit is not None else None
    if path and not path.exists():
        raise ValueError(
            "football source path is missing; omit it to reopen the local manifest"
        )
    paths = (
        ([path] if path and path.is_file() else sorted(path.glob("????-??/en.1.json")))
        if path
        else []
    )
    if path and not paths:
        raise ValueError("no football season inputs found at the supplied path")
    supplied = {p.parent.name: _hash(p.read_bytes()) for p in paths}
    with closing(_db(data_dir)) as db:
        candidates = db.execute(
            "SELECT digest,manifest FROM football_input_manifests ORDER BY observed_at DESC,rowid DESC"
        ).fetchall()
        for row in candidates:
            m = json.loads(row["manifest"])
            if digest(m) != row["digest"]:
                raise ValueError("football input manifest digest mismatch")
            expected = {f["season"]: f["sha256"] for f in m["files"]}
            if evidence_sha is not None and evidence_sha != m["evidence_sha256"]:
                continue
            if path is not None and expected != supplied:
                managed = data_dir / "sources" / "openfootball" / m["revision"]
                if path != managed or any(
                    supplied.get(k) != v for k, v in expected.items()
                ):
                    continue
            break
        else:
            raise ValueError(
                "no locally observed football manifest matches the supplied source and evidence bytes"
            )
        result = _materialize(data_dir, m, row["digest"])
        fixtures = football.load_fixtures(Path(result["root"]))
        effective = _effective_rows(db, fixtures)
    return _save(data_dir, m["revision"], m["files"], effective)
