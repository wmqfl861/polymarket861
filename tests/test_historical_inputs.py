"""Backtest admission uses the provider bytes and locally witnessed clock for each asserted fact."""

import copy
import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from polymarket861 import (
    datasets,
    football,
    historical_inputs,
    input_manifest,
    paired_proof,
    store,
)


def game(date, home, away, ft=None, time="15:00"):
    row = {"round": "Matchday 1", "date": date, "team1": home, "team2": away}
    if time:
        row["time"] = time
    if ft is not None:
        row["score"] = {"ft": ft}
    return row


class HistoricalQualificationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.data = self.home / "data"
        self.source = self.home / "upstream"
        self.past = "2020-21"
        self.season = "2021-22"
        self.write(self.past, [game("2021-02-02", "Man A", "Man B", [0, 3])])
        self.write(
            self.season,
            [
                game("2021-08-14", "Man A", "Man C"),
                game("2021-08-16", "Man B", "Man D", time=None),
            ],
        )
        self.observe("1" * 40, "2021-08-05T08:00Z")
        self.write(
            self.season,
            [
                game("2021-08-14", "Man A", "Man C", [1, 2]),
                game("2021-08-16", "Man B", "Man D", [3, 1]),
            ],
        )
        self.current = self.observe("2" * 40, "2022-06-01T06:00Z")
        self.rows = list(football.read_json_rows(self.current["evidence_path"]))
        with closing(store.connect(self.data)) as db:
            self.manifest = json.loads(
                db.execute(
                    "SELECT manifest FROM football_input_manifests WHERE digest=?",
                    (self.current["manifest_digest"],),
                ).fetchone()[0]
            )

    def write(self, season, matches):
        p = self.source / season / "en.1.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"matches": matches}))

    def observe(self, revision, observed):
        expected = {
            p.parent.name: football.blob_sha(p.read_bytes())
            for p in self.source.glob("*/en.1.json")
        }
        # Simulated upstream revision metadata and clock; raw archives/receipt handling are the production implementations.
        paired_proof.attest_provider_files(self.data, revision, expected)
        with patch.object(datasets, "now_utc", return_value=store.iso(observed)):
            return datasets.observe(
                self.data, self.source, revision=revision, expected=expected
            )

    def run_study(self, side=None, lag=None):
        return football._cli_backtest(
            Namespace(
                data_dir=self.data,
                path=self.source,
                evidence=side,
                archive_lag_hours=lag,
                from_season=self.season,
                to_season=self.season,
                horizons=[1440, 60],
                bootstrap=1,
                seed=861,
                details=None,
                summary_output=None,
            )
        )

    def admitted_side(self, rows):
        # Strong negative: even a registered derived sidecar cannot assert a fact absent in the original release.
        return input_manifest._save(
            self.data, self.manifest["revision"], self.manifest["files"], rows
        )["evidence_path"]

    def test_registered_archives_independent_originals_and_joint_cutoff_count(self):
        self.assertEqual(len([r for r in self.rows if "score" in r]), 3)
        report = self.run_study(self.current["evidence_path"])
        self.assertTrue(historical_inputs.authenticated(self.data, report))
        self.assertEqual(
            report["input_provenance"]["status"], "verified_local_receipts"
        )
        summary = report["horizons"]["T-60"]["summary"]
        self.assertEqual(
            (summary["eligible_completed"], summary["n"], summary["verified_cutoff_n"]),
            (2, 1, 1),
        )
        # Remove the second schedule from a registered projection: its final schedule can support research only.
        late = football.fixture_id(self.season, "Man B", "Man D")
        subset = [
            r for r in self.rows if not (r["fixture_id"] == late and "kickoff" in r)
        ]
        mixed = self.run_study(self.admitted_side(subset))
        m = mixed["horizons"]["T-60"]["summary"]
        self.assertTrue(historical_inputs.authenticated(self.data, mixed))
        self.assertEqual((m["n"], m["verified_cutoff_n"]), (2, 1))
        fake = copy.deepcopy(report)
        fake["horizons"]["T-60"]["summary"]["verified_cutoff_n"] = 2
        self.assertFalse(historical_inputs.authenticated(self.data, fake))
        # Deleting the earlier release after a genuine run invalidates qualification, even with the complete terminal snapshot.
        with closing(store.connect(self.data)) as db:
            sha = db.execute(
                "SELECT sha256 FROM observations WHERE source='openfootball' AND json_extract(metadata,'$.season')=? AND json_extract(metadata,'$.revision')=?",
                (self.season, "1" * 40),
            ).fetchone()[0]
        store.blob_path(self.data, sha).unlink()
        self.assertFalse(historical_inputs.authenticated(self.data, report))

    def test_registered_fake_points_clocks_and_different_sidecar_hash_refused(self):
        fid = football.fixture_id(self.past, "Man A", "Man B")
        original = next(r for r in self.rows if r["fixture_id"] == fid and "score" in r)
        for change, reason in [
            ({"score": [43, 0]}, "archived_score_or_clock_disagrees"),
            (
                {
                    "archive_at": "2021-08-01T00:00Z",
                    "published_by": "2021-08-01T00:00Z",
                    "finalized_by": "2021-08-01T00:00Z",
                },
                "missing_matching_provider_receipt",
            ),
        ]:
            altered = [r if r is not original else r | change for r in self.rows]
            side = self.admitted_side(altered)
            report = self.run_study(side)
            self.assertEqual(report["input_provenance"]["reason"], reason)
            self.assertFalse(historical_inputs.authenticated(self.data, report))
            self.assertEqual(
                report["horizons"]["T-60"]["summary"]["verified_cutoff_n"], 0
            )
            fixtures, _ = historical_inputs.load_for_study(
                self.data, self.source, side, None
            )
            label = next(f for f in fixtures if f.fixture_id == fid)
            self.assertEqual((label.home_goals, label.away_goals), (0, 3))
        side = self.home / "near-copy.jsonl"
        side.write_bytes(Path(self.current["evidence_path"]).read_bytes() + b"\n")
        report = self.run_study(side)
        self.assertEqual(
            report["input_provenance"]["reason"], "unverified_source_bytes"
        )
        self.assertFalse(historical_inputs.authenticated(self.data, report))

    def test_unbacked_schedule_or_missing_training_files_cannot_reuse_report(self):
        f = football.fixture_id(self.season, "Man A", "Man C")
        original = next(r for r in self.rows if r["fixture_id"] == f and "kickoff" in r)
        changed = [
            r if r is not original else r | {"kickoff": "2021-08-14T15:30Z"}
            for r in self.rows
        ]
        report = self.run_study(self.admitted_side(changed))
        self.assertEqual(
            report["input_provenance"]["reason"], "archived_schedule_disagrees"
        )
        self.assertEqual(report["horizons"]["T-60"]["summary"]["verified_cutoff_n"], 0)
        together = self.run_study(self.current["evidence_path"])
        self.assertTrue(historical_inputs.authenticated(self.data, together))
        for row in self.manifest["files"]:
            if row["season"] == self.past:
                store.blob_path(self.data, row["sha256"]).write_bytes(
                    b"not original EPL bytes"
                )
        self.assertFalse(historical_inputs.authenticated(self.data, together))
