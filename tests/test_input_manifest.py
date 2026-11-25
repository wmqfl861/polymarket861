"""Integration checks for prospective evidence authentication, revision, precision and restore."""

import json
import shutil
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from polymarket861 import datasets, football, input_manifest, operations, store


class InputManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.runtime = self.base / "data"
        self.source = self.base / "external"
        self.season = "2024-25"
        self.path = self.source / self.season / "en.1.json"
        self.path.parent.mkdir(parents=True)
        self.rows = [
            {
                "round": "Matchday 1",
                "date": "2024-08-16",
                "time": "20:00",
                "team1": "A FC",
                "team2": "B FC",
                "score": {"ft": [1, 1]},
            },
            {
                "round": "Matchday 2",
                "date": "2024-08-20",
                "team1": "B FC",
                "team2": "C FC",
                "score": {},
            },
        ]

    def observe(
        self,
        rows=None,
        *,
        clock="2024-08-17T11:00:00.000000Z",
        revision="1" * 40,
        current=False,
    ):
        self.path.write_text(json.dumps({"matches": rows or self.rows}))
        with (
            patch.object(datasets, "now_utc", return_value=clock),
            patch.object(store, "now_utc", return_value=clock),
        ):
            return datasets.observe(
                self.runtime,
                self.source,
                revision=revision,
                expected={self.season: football.blob_sha(self.path.read_bytes())},
                register_season=self.season,
                current_head=current,
            )

    def manifest(self, report):
        return input_manifest.verify_observed_inputs(
            self.runtime, report["fixture_root"], report["evidence_path"]
        )

    def test_verified_input_rejects_unobserved_sidecar_and_changed_source(self):
        r = self.observe()
        self.assertEqual(r["seasons"][0]["date_only_fixtures"], 1)
        fixtures = store.eligible_fixtures(self.runtime)
        self.assertEqual(len(fixtures), 1)
        verified = self.manifest(r)
        self.assertEqual(verified["digest"], r["manifest_digest"])
        self.assertTrue(
            (
                self.runtime
                / "sources"
                / "openfootball"
                / ("1" * 40)
                / self.season
                / "en.1.json"
            ).is_file()
        )
        external = self.base / "sidecar.jsonl"
        shutil.copyfile(r["evidence_path"], external)
        self.assertEqual(
            input_manifest.verify_observed_inputs(self.runtime, self.source, external)[
                "digest"
            ],
            verified["digest"],
        )
        external.write_text("{}\n")
        with self.assertRaisesRegex(ValueError, "no locally observed"):
            input_manifest.verify_observed_inputs(self.runtime, self.source, external)
        self.path.write_text(self.path.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "no locally observed"):
            input_manifest.verify_observed_inputs(
                self.runtime, self.source, r["evidence_path"]
            )
        rows = football.load_fixtures(
            verified["root"], evidence_path=verified["evidence_path"]
        )
        self.assertEqual(rows[1].schedule_versions, ())
        self.assertEqual(len(rows[0].result_versions), 1)

    def test_new_independent_result_supersedes_rejected_upstream_and_older_manifest(
        self,
    ):
        r = self.observe()
        fid = football.fixture_id(self.season, "A FC", "B FC")
        clock = "2024-08-18T18:00:00.000000Z"
        with patch.object(store, "now_utc", return_value=clock):
            store.record_result(
                self.runtime,
                {
                    "fixture_id": fid,
                    "home_goals": 0,
                    "away_goals": 1,
                    "source": "official scorer",
                    "locator": "official league bulletin",
                    "finalized_at": "2024-08-16T21:15:00Z",
                    "published_at": "2024-08-18T16:00:00Z",
                    "received_at": "2024-08-18T17:00:00Z",
                },
            )
        new = self.manifest(r)
        self.assertNotEqual(new["digest"], r["manifest_digest"])
        history = football.load_fixtures(
            new["root"], evidence_path=new["evidence_path"]
        )[0]
        self.assertEqual(
            [(v.available, v.score) for v in history.result_versions],
            [
                (football.utc("2024-08-17T11:00Z"), (1, 1)),
                (football.utc(clock), (0, 1)),
            ],
        )
        rerun = self.observe(clock="2024-08-19T12:00:00.000000Z")
        self.assertEqual(rerun["data_conflicts"][0]["kind"], "result")
        clean = football.load_fixtures(
            rerun["fixture_root"], evidence_path=rerun["evidence_path"]
        )[0]
        self.assertEqual(clean.result_versions[-1].score, (0, 1))
        self.assertEqual(rerun["manifest_digest"], new["digest"])

    def test_equal_priority_score_conflict_freezes_until_adjudicated(self):
        r = self.observe()
        changed = deepcopy(self.rows)
        changed[0]["score"]["ft"] = [4, 0]
        observed = self.observe(
            changed,
            clock="2024-08-19T12:00:00.000000Z",
            revision="2" * 40,
            current=True,
        )
        self.assertEqual(observed["data_conflicts"][0]["kind"], "result")
        history = football.load_fixtures(
            observed["fixture_root"], evidence_path=observed["evidence_path"]
        )[0]
        self.assertEqual([v.score for v in history.result_versions], [(1, 1), None])
        # Even an explicitly selected older manifest must respect the quarantine.
        old = self.manifest(r)
        hist = football.load_fixtures(old["root"], evidence_path=old["evidence_path"])[
            0
        ]
        self.assertIsNone(hist.result_versions[-1].score)

    def test_late_archived_schedule_cannot_overwrite_current_head(self):
        rows = deepcopy(self.rows)
        rows[1]["time"] = "15:00"
        self.observe(rows)
        newer = deepcopy(rows)
        newer[1]["date"] = "2024-08-23"
        current = self.observe(
            newer, clock="2024-08-18T11:00:00.000000Z", revision="2" * 40, current=True
        )
        old = deepcopy(rows)
        old[0]["status"] = "complete"
        late = self.observe(old, clock="2024-08-19T11:00:00.000000Z", revision="3" * 40)
        self.assertTrue(any(c["kind"] == "schedule" for c in late["data_conflicts"]))
        fid = football.fixture_id(self.season, "B FC", "C FC")
        self.assertEqual(
            store.fixture_at(self.runtime, fid)["kickoff"],
            store.iso("2024-08-23T14:00Z"),
        )
        hist = football.load_fixtures(
            current["fixture_root"], evidence_path=current["evidence_path"]
        )[1]
        self.assertEqual(len(hist.schedule_versions), 2)

    def test_offline_restore_reconstructs_derived_paths_from_archived_bytes(self):
        r = self.observe()
        backup = self.base / "backup"
        destination = self.base / "moved"
        operations.backup_state(self.runtime, backup)
        operations.restore_state(backup, destination)
        # No path from the original machine is available in this check.
        shutil.rmtree(self.runtime)
        shutil.rmtree(self.source)
        for derived in ("sources", "datasets"):
            if (destination / derived).exists():
                shutil.rmtree(destination / derived)
        restored = input_manifest.verify_observed_inputs(
            destination, r["fixture_root"], r["evidence_path"]
        )
        self.assertEqual(restored["digest"], r["manifest_digest"])
        self.assertTrue(Path(restored["root"]).is_relative_to(destination))
        self.assertTrue(Path(restored["evidence_path"]).is_relative_to(destination))
        records = football.load_fixtures(
            restored["root"], evidence_path=restored["evidence_path"]
        )
        self.assertEqual(records[0].result_versions[-1].score, (1, 1))

    def test_formal_close_recovers_after_interruption_and_reviews_once(self):
        from unittest.mock import Mock

        fid = football.fixture_id(self.season, "A FC", "B FC")
        when = store.iso("2024-08-14T10:00Z")
        kick = store.iso("2024-08-16T19:00Z")
        with patch.object(store, "now_utc", return_value=when):
            store.register_fixture(
                self.runtime,
                {
                    "fixture_id": fid,
                    "season": self.season,
                    "round_id": 1,
                    "home": "A FC",
                    "away": "B FC",
                    "known_at": when,
                    "kickoff": kick,
                    "source": "schedule",
                    "locator": "fixture schedule",
                },
            )
        # Only routing is exercised here; sealed-payload scoring is tested independently.
        with closing(store.connect(self.runtime)) as db, db:
            for version in ("B1", "candidate"):
                db.execute(
                    "INSERT INTO forecast_seals VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        fid,
                        kick,
                        when,
                        "T-60m",
                        version,
                        store.PROTOCOL,
                        "formal",
                        when,
                        "test-" + version,
                        "{}",
                    ),
                )
        with patch(
            "polymarket861.pipeline.publish_round",
            side_effect=RuntimeError("worker stopped"),
        ):
            with self.assertRaisesRegex(RuntimeError, "worker stopped"):
                self.observe()
        review = Mock(return_value={"reviewed": []})
        with (
            patch(
                "polymarket861.pipeline.publish_round",
                return_value={
                    "status": "final",
                    "mode": "formal",
                    "horizon": "T-60m",
                    "expected": 10,
                    "completed": 10,
                },
            ) as publish,
            patch("polymarket861.jobs.review_due_experiments", review),
        ):
            self.observe()
            self.observe()
        self.assertEqual(publish.call_count, 2)
        review.assert_called_once_with(self.runtime, self.season, 1)

    def test_openfootball_complete_technical_round_routes_review_once(self):
        fixtures = [
            self.rows[0] | {"team1": f"Home {i} FC", "team2": f"Away {i} FC"} for i in range(10)
        ]
        self.observe(
            [f | {"score": {}} for f in fixtures],
            clock="2024-08-14T10:00:00Z",
            revision="1" * 40,
        )
        partial = self.observe(
            fixtures[:-1] + [fixtures[-1] | {"score": {}}], revision="2" * 40
        )
        self.assertEqual(partial["experiment_reviews"], [])
        final = self.observe(fixtures, clock="2024-08-17T14:00:00Z", revision="3" * 40)
        self.assertEqual(
            final["experiment_reviews"], [{"season": self.season, "round_id": 1, "windows": []}]
        )
        again = self.observe(fixtures, clock="2024-08-17T14:00:00Z", revision="3" * 40)
        self.assertEqual(again["experiment_reviews"], [])

    def test_managed_file_tamper_detected_before_training(self):
        r = self.observe()
        e = Path(r["evidence_path"])
        good = e.read_bytes()
        e.write_bytes(good + b" ")
        with self.assertRaisesRegex(ValueError, "verified digest"):
            input_manifest.verify_observed_inputs(self.runtime)
        e.unlink()
        fixed = input_manifest.verify_observed_inputs(self.runtime)
        self.assertEqual(Path(fixed["evidence_path"]).read_bytes(), good)
