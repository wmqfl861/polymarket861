"""Capture public-domain OpenFootball inputs and record the first actual availability locally."""

from __future__ import annotations

import json
import re
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from . import football, input_manifest
from .store import (
    archive_bytes,
    connect,
    instant,
    iso,
    now_utc,
    register_fixture,
    record_result,
    save_report,
)

BASE = "https://api.github.com/repos/openfootball/football.json"


def _get(url: str) -> bytes:
    request = Request(
        url, headers={"User-Agent": "polymarket861/0.1", "Accept": "application/json"}
    )
    with urlopen(request, timeout=30) as response:
        return response.read()


def _atomic(path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(content)
        temp = Path(handle.name)
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _seasons(first, last):
    lo, hi = football.season_year(first), football.season_year(last)
    if hi < lo or hi - lo > 40:
        raise ValueError("invalid EPL season interval")
    return [f"{yr}-{(yr + 1) % 100:02d}" for yr in range(lo, hi + 1)]


def refresh(data_dir, first, last, *, revision="latest"):
    """Pin once per batch. Verify the Git blob before installing each downloaded file."""
    is_current_head = revision == "latest"
    if is_current_head:
        revision = json.loads(_get(BASE + "/commits/HEAD"))["sha"]
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("expected a full upstream commit")
    root = Path(data_dir) / "sources" / "openfootball" / revision
    expected = {}
    for season in _seasons(first, last):
        known = (
            football.SOURCE_BLOBS.get(season)
            if revision == football.SOURCE_COMMIT
            else None
        )
        if not known:
            metadata = json.loads(
                _get(BASE + f"/contents/{season}/en.1.json?ref=" + quote(revision))
            )
            known = metadata["sha"]
        path = root / season / "en.1.json"
        if not path.exists() or football.blob_sha(path.read_bytes()) != known:
            url = f"https://raw.githubusercontent.com/openfootball/football.json/{revision}/{season}/en.1.json"
            raw = _get(url)
            if football.blob_sha(raw) != known:
                raise ValueError(f"upstream blob mismatch for {season}")
            _atomic(path, raw)
        expected[season] = known
    from .paired_proof import attest_provider_files

    attest_provider_files(data_dir, revision, expected)
    return observe(
        data_dir,
        root,
        revision=revision,
        expected=expected,
        register_season=last,
        current_head=is_current_head,
    )


def observe(
    data_dir,
    root,
    *,
    revision=None,
    expected=None,
    register_season=None,
    current_head=False,
):
    """Archive immutable input bytes, record actual receipt, then publish adjudicated observations."""
    revision = revision or football.SOURCE_COMMIT
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("invalid upstream revision")
    selection = set(expected) if expected is not None else None
    if expected is None:
        if revision != football.SOURCE_COMMIT:
            raise ValueError(
                "use data refresh for revisions without known content hashes"
            )
        expected = football.SOURCE_BLOBS
    root = Path(root)
    files = sorted(
        p
        for p in root.glob("????-??/en.1.json")
        if selection is None or p.parent.name in selection
    )
    if not files:
        raise ValueError("expected an OpenFootball folder with SEASON/en.1.json")
    if selection is not None and {p.parent.name for p in files} != selection:
        raise ValueError("not all selected OpenFootball files were supplied")
    summary = []
    registered = 0
    updated_rounds = set()
    tracked_rounds = set()
    conflicts = []
    fixtures = []
    manifest_files = []
    sidecars = {}
    for file in files:
        season = file.parent.name
        raw = file.read_bytes()
        actual = football.blob_sha(raw)
        if actual != expected.get(season):
            raise ValueError(f"unverified OpenFootball bytes for {season}")
        ref = archive_bytes(
            data_dir,
            "openfootball",
            raw,
            observed_at=now_utc(),
            metadata={"season": season, "revision": revision, "git_blob": actual},
        )
        manifest_files.append(
            {"season": season, "git_blob": actual, "sha256": ref["sha256"]}
        )
        with closing(connect(data_dir)) as db:
            seen = db.execute(
                "SELECT MIN(observed_at) FROM observations WHERE source=? AND sha256=?",
                ("openfootball", ref["sha256"]),
            ).fetchone()[0]
            schedules = {
                r["fixture_id"]: dict(r)
                for r in db.execute(
                    "SELECT * FROM fixture_versions WHERE season=? ORDER BY known_at",
                    (season,),
                )
            }
            scores = {
                (r["fixture_id"], r["kickoff"]): dict(r)
                for r in db.execute(
                    "SELECT r.*,s.kickoff FROM result_versions r JOIN result_schedule_refs s USING(fixture_id,version) ORDER BY r.version"
                )
            }
        locator = f"https://api.github.com/repos/openfootball/football.json/git/blobs/{actual}"
        # Parse the same bytes we hashed. The caller's checkout may be updated
        # concurrently after the original read.
        with tempfile.TemporaryDirectory(prefix="pm861-verified-season-") as scratch:
            captured = Path(scratch) / season / "en.1.json"
            captured.parent.mkdir()
            captured.write_bytes(raw)
            batch = football.load_fixtures(captured)
        fixtures.extend(batch)
        rows = sidecars[season] = []
        completed = quarantined = date_only = past_without_score = 0
        latest = None
        for fixture in batch:
            if fixture.status == "quarantined":
                quarantined += 1
                continue
            fid = fixture.fixture_id
            known = schedules.get(fid)
            kickoff = iso(fixture.kickoff)
            precise = fixture.kickoff_precision == "minute"
            schedule_conflict = False
            if not precise:
                date_only += 1
            elif (
                known
                and known["kickoff"] != kickoff
                and (
                    known["source"] != "openfootball"
                    or not current_head
                    or known["known_at"] >= seen
                )
            ):
                conflicts.append(
                    {
                        "fixture_id": fid,
                        "kind": "schedule",
                        "archive": kickoff,
                        "retained": known["kickoff"],
                    }
                )
                schedule_conflict = True
            if precise and not schedule_conflict:
                rows.append(
                    {
                        "fixture_id": fid,
                        "source": locator,
                        "schedule_observed_at": seen,
                        "kickoff": kickoff,
                    }
                )
                if season == register_season:
                    if not known or (known["kickoff"], known["round_id"]) != (
                        kickoff,
                        fixture.round,
                    ):
                        register_fixture(
                            data_dir,
                            {
                                "fixture_id": fid,
                                "season": season,
                                "round_id": fixture.round,
                                "home": fixture.home,
                                "away": fixture.away,
                                "kickoff": kickoff,
                                "known_at": seen,
                                "source": "openfootball",
                                "locator": locator,
                            },
                        )
                    registered += 1
            if fixture.home_goals is None or instant(seen) <= fixture.kickoff:
                if (instant(seen) - fixture.kickoff).total_seconds() > 86400:
                    past_without_score += 1
                continue
            completed += 1
            latest = max(latest, fixture.kickoff) if latest else fixture.kickoff
            score = [fixture.home_goals, fixture.away_goals]
            result = {
                "fixture_id": fid,
                "source": locator,
                "archive_at": seen,
                "published_by": seen,
                "finalized_by": seen,
            }
            detected = now_utc()
            prior = scores.get((fid, kickoff))
            if schedule_conflict:
                rows.append(
                    result
                    | {
                        "retracted": True,
                        "archive_at": detected,
                        "published_by": detected,
                        "finalized_by": detected,
                    }
                )
                continue
            if prior and [prior["home_goals"], prior["away_goals"]] != score:
                conflicts.append(
                    {
                        "fixture_id": fid,
                        "kind": "result",
                        "archive": score,
                        "retained": [prior["home_goals"], prior["away_goals"]],
                    }
                )
                # The archive cannot override a dispute. Independently adjudicated
                # versions, if present, are merged at their actual local admission time.
                rows.append(
                    result
                    | {
                        "retracted": True,
                        "archive_at": detected,
                        "published_by": detected,
                        "finalized_by": detected,
                    }
                )
                continue
            rows.append(result | {"score": score})
            if season != register_season or not precise:
                continue
            if prior is None:
                record_result(
                    data_dir,
                    {
                        "fixture_id": fid,
                        "home_goals": score[0],
                        "away_goals": score[1],
                        "archive_at": seen,
                        "finalized_by": seen,
                        "published_by": seen,
                        "source": "openfootball",
                        "locator": locator,
                    },
                )
            # Recover a crash after result admission and before the round report.
            # The batch receipt below prevents repeated publication once complete.
            if prior is None or (
                prior["source"] == "openfootball"
                and prior["locator"] == locator
                and prior["received_at"] == seen
            ):
                updated_rounds.add((season, fixture.round))
                if (
                    known
                    and instant(known["known_at"]).timestamp()
                    <= fixture.kickoff.timestamp() - 3600
                ):
                    tracked_rounds.add((season, fixture.round))
        summary.append(
            {
                "season": season,
                "fixtures": len(batch),
                "scores_observed": completed,
                "date_only_fixtures": date_only,
                "quarantined": quarantined,
                "past_scheduled_without_score": past_without_score,
                "newest_scored_kickoff": iso(latest) if latest else None,
                "first_seen": seen,
                "sha256": ref["sha256"],
                "git_blob": actual,
            }
        )
    manifest = input_manifest.create_observed_inputs(
        data_dir, revision, manifest_files, fixtures, sidecars
    )
    from .jobs import review_due_experiments
    from .pipeline import publish_round

    reviews = []
    batch = (manifest["digest"], register_season or "")
    with closing(connect(data_dir)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS football_feed_publications(digest TEXT NOT NULL,register_season TEXT NOT NULL, PRIMARY KEY(digest,register_season))"
        )
        completed_batch = db.execute(
            "SELECT 1 FROM football_feed_publications WHERE digest=? AND register_season=?",
            batch,
        ).fetchone()
    for season, rnd in sorted(updated_rounds if not completed_batch else set()):
        with closing(connect(data_dir)) as db:
            runs = db.execute(
                "SELECT DISTINCT model_version,mode,horizon FROM forecast_seals "
                "WHERE fixture_id IN (SELECT fixture_id FROM fixture_versions WHERE season=? AND round_id=?)",
                (season, rnd),
            ).fetchall()
        defaults = (
            [("B1", "technical", "T-60m")] if (season, rnd) in tracked_rounds else []
        )
        formal_round_complete = False
        for version, mode, horizon in runs or defaults:
            closed = publish_round(
                data_dir, season, rnd, version=version, mode=mode, horizon=horizon
            )
            if (
                mode == "formal"
                and horizon == "T-60m"
                and closed["status"] in {"final", "incomplete"}
            ):
                formal_round_complete = True
        if formal_round_complete:
            reviews.append(review_due_experiments(data_dir, season, rnd))
    with closing(connect(data_dir)) as db, db:
        db.execute(
            "INSERT OR IGNORE INTO football_feed_publications VALUES(?,?)", batch
        )
    if conflicts:
        save_report(
            data_dir,
            "incident",
            "openfootball-fixture-conflicts",
            {"status": "quarantined", "items": conflicts},
        )
    report = {
        "source": "openfootball",
        "revision": revision,
        "observed_at": now_utc(),
        "seasons": summary,
        "registered_fixtures": registered,
        "data_conflicts": conflicts,
        "updated_rounds": len(updated_rounds),
        "experiment_reviews": reviews,
        "evidence_grade": "archived_upper_bound",
        "fixture_root": manifest["root"],
        "evidence_path": manifest["evidence_path"],
        "manifest_digest": manifest["digest"],
        "relative_fixture_root": manifest["relative_root"],
        "relative_evidence_path": manifest["relative_evidence"],
        "evidence_sha256": manifest["evidence_sha256"],
    }
    save_report(data_dir, "source", "openfootball", report)
    return report


def register(subparsers):
    commands = subparsers.add_parser(
        "data", help="固定并观察公共领域赛程与赛果"
    ).add_subparsers(required=True)
    refresh_cmd = commands.add_parser("refresh")
    refresh_cmd.add_argument("--first", default="2021-22")
    refresh_cmd.add_argument("--last", default="2026-27")
    refresh_cmd.add_argument(
        "--revision", default="latest", help="latest 或完整 Git 提交"
    )
    refresh_cmd.set_defaults(
        func=lambda a: refresh(a.data_dir, a.first, a.last, revision=a.revision)
    )
    capture = commands.add_parser("observe")
    capture.add_argument("root", type=Path)
    capture.add_argument("--register-season", help="把该季赛程登记为评分分母")
    capture.set_defaults(
        func=lambda a: observe(a.data_dir, a.root, register_season=a.register_season)
    )
