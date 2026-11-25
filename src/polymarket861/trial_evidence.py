"""Authenticate the historical denominator and prospective fixture identities for trial admission."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from . import football, historical_inputs, store


def verified_study(data_dir, report):
    seasons = report.get("seasons", [])
    if (
        report.get("kind") != "historical_football_backtest"
        or len(set(seasons)) < 2
        or len(seasons) != len(set(seasons))
        or not historical_inputs.authenticated(data_dir, report)
    ):
        return False
    source = report.get("source", {})
    blobs = source.get("blobs") or {}
    if (
        source.get("repository") != "openfootball/football.json"
        or not source.get("commit")
        or source.get("commit") != report["input_provenance"]["source_revision"]
        or source.get("all_files_verified") is not True
        or not set(seasons) <= blobs.keys()
    ):
        return False
    code = report.get("reproduce", {}).get("source_code_sha256", {})
    for name in ("football.py", "football_model.py", "historical_inputs.py"):
        if (
            code.get(name)
            != hashlib.sha256(
                Path(football.__file__).with_name(name).read_bytes()
            ).hexdigest()
        ):
            return False
    expected = 380 * len(seasons)
    fixtures = report.get("fixtures", {})
    if fixtures.get("unique") != expected or fixtures.get("completed") != expected:
        return False
    for key in ("T-1440", "T-60"):
        by_season = report.get("horizons", {}).get(key, {}).get("by_season", {})
        if set(by_season) != set(seasons) or any(
            by_season[s].get("eligible_completed") != 380 for s in seasons
        ):
            return False
    return True


def scope(report):
    return (
        tuple(sorted(report["seasons"])),
        tuple(sorted(report["source"]["blobs"].items())),
    )


def qualified_seals(data_dir, fixtures):
    """Use the round roster from the exact verified season blob each prospective seal consumed."""
    with closing(store.connect(data_dir)) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='football_input_manifests'"
        ).fetchone():
            return set()

        results = {
            f["fixture_id"]: (f["home_goals"], f["away_goals"]) for f in fixtures
        }

        @lru_cache(maxsize=32)
        def schedule(season, sha, git_sha):
            raw = store.read_blob(data_dir, sha)
            if (
                hashlib.sha256(raw).hexdigest() != sha
                or football.blob_sha(raw) != git_sha
            ):
                return {}
            result = {}
            for item in json.loads(raw)["matches"]:
                record = football._parse_match(season, item, (), None, git_sha)
                if record.status == "quarantined" or record.kickoff_precision == "date":
                    continue
                if record.fixture_id in result:
                    return {}
                result[record.fixture_id] = (
                    record.round,
                    store.iso(record.kickoff),
                    (record.home_goals, record.away_goals),
                )
            return result

        @lru_cache(maxsize=128)
        def registry(identity, season):
            row = db.execute(
                "SELECT manifest,observed_at FROM football_input_manifests WHERE digest=?",
                (identity,),
            ).fetchone()
            if not row:
                return {}, None
            manifest = json.loads(row["manifest"])
            if store.digest(manifest) != identity:
                return {}, None
            for entry in manifest["files"]:
                if entry["season"] != season:
                    continue
                observed = db.execute(
                    "SELECT MIN(observed_at) FROM observations WHERE source='openfootball' "
                    "AND sha256=? AND json_extract(metadata,'$.git_blob')=? AND json_extract(metadata,'$.season')=? "
                    "AND json_extract(metadata,'$.revision')=?",
                    (entry["sha256"], entry["git_blob"], season, manifest["revision"]),
                ).fetchone()[0]
                if observed:
                    try:
                        return schedule(
                            season, entry["sha256"], entry["git_blob"]
                        ), max(observed, row["observed_at"])
                    except (OSError, ValueError, KeyError, TypeError):
                        return {}, None
            return {}, None

        @lru_cache(maxsize=8)
        def final_file(season):
            row = db.execute(
                "SELECT sha256,observed_at,json_extract(metadata,'$.git_blob') AS git_sha "
                "FROM observations WHERE source='openfootball' AND json_extract(metadata,'$.season')=? "
                "ORDER BY observed_at DESC,rowid DESC LIMIT 1",
                (season,),
            ).fetchone()
            if not row:
                return {}, None
            try:
                return schedule(season, row["sha256"], row["git_sha"]), row[
                    "observed_at"
                ]
            except (OSError, ValueError, KeyError, TypeError):
                return {}, None

        observed = set()
        for row in db.execute(
            "SELECT * FROM forecast_seals WHERE mode='technical' AND horizon IN "
            "('T-24h','T-60m') AND sealed_at<=cutoff AND protocol=?",
            (store.PROTOCOL,),
        ):
            data = json.loads(row["data_json"])
            season, home, away = (data.get(k) for k in ("season", "home", "away"))
            if not all((season, home, away)) or store.digest(data) != row["digest"]:
                continue
            fid = football.fixture_id(season, home, away)
            if (
                fid != row["fixture_id"]
                or data.get("fixture_id") != fid
                or data.get("kickoff") != row["kickoff"]
                or data.get("horizon") != row["horizon"]
                or data.get("mode") != "technical"
            ):
                continue
            roster, known = registry(data.get("input_manifest_sha256"), season)
            fixed = roster.get(fid)
            final, received = final_file(season)
            outcome = final.get(fid)
            if (
                known
                and known <= row["sealed_at"]
                and fixed
                and fixed[:2] == (data.get("round_id"), row["kickoff"])
                and outcome
                and outcome[:2] == fixed[:2]
                and received > row["kickoff"]
                and None not in outcome[2]
                and outcome[2] == results.get(fid)
            ):
                observed.add((fid, row["kickoff"], row["horizon"]))
        return observed
