import hashlib
import json
from unittest import TestCase

from polymarket861 import (
    result_admission,
    result_operations,
    result_training_review,
)


class ExplicitTrainingDispute(TestCase):
    def setUp(self):
        from test_result_identity import ResultIdentity

        self.events = ResultIdentity()
        self.events.setUp()
        self.addCleanup(self.events.doCleanups)
        self.case = self.events.case
        self.root, self.fid = self.case.root, self.events.fid
        self.case.set_time(0)
        self.case.collect(self.case.rows)
        row = self.case.rows[0]
        self.kick = row["matchDateTimeUTC"]
        self.events.evidence(self.kick, [2, 2])
        self.case.reconcile()
        self.case.set_time(1)
        attachment = self.root / "licensed-note.txt"
        attachment.write_text("Test fixture regulation score: 2–2.")
        self.entry = dict(
            home=row["team1"]["teamName"],
            away=row["team2"]["teamName"],
            kickoff=self.kick,
            status="final",
            period="regulation",
            home_goals=2,
            away_goals=2,
            locator="example fixture record",
            decision_reason="Review of the existing 2–1 / 2–2 reports for training.",
        )
        self.entry["training_dispute"] = result_training_review.evidence(
            self.root, 2026, self.fid
        )["training_dispute"]
        self.doc = dict(
            season="2026-27",
            round=1,
            rights="permission",
            reviewer="Example test reviewer",
            source="test match report",
            evidence_file=attachment.name,
            evidence_sha256=hashlib.sha256(attachment.read_bytes()).hexdigest(),
            matches=[self.entry],
        )
        self.path = self.root / "manual.json"

    def import_record(self):
        self.path.write_text(json.dumps(self.doc))
        return result_operations.manual(
            self.root, 2026, 1, self.path, whole_round=False
        )

    def test_existing_controversy_keeps_only_training_and_reopens_on_new_facts(self):
        report = self.import_record()
        self.assertEqual(report["scope"], "selected_matches")
        from datetime import timedelta
        from polymarket861 import store

        earlier = self.events.secondary[self.fid][-1]
        delayed = store.iso(store.instant(earlier["received"]) + timedelta(seconds=30))
        self.events.secondary[self.fid].append(
            earlier | {"received": delayed, "started": delayed, "sha256": "e" * 64}
        )
        self.case.set_time(31)
        self.case.collect(self.case.rows)
        self.events.evidence(self.kick, [2, 2])
        self.events.secondary[self.fid][-1]["sha256"] = "d" * 64
        self.assertEqual(self.case.reconcile()["coverage"]["manual_reviewed"], 1)
        self.assertEqual(self.case.state(self.fid)["away_goals"], 2)
        self.assertIsNone(result_admission.verified_regulation(self.root, self.fid))
        self.case.set_time(64)
        self.events.evidence(self.kick, None, status="withdrawn")
        self.case.set_time(65)
        self.events.evidence(self.kick, [2, 2])
        self.assertIn(
            "reviewed_source_facts_changed",
            [r["reason"] for r in self.case.reconcile()["quarantined"]],
        )
        self.assertIsNone(self.case.state(self.fid)["away_goals"])
        self.assertIsNone(result_admission.verified_regulation(self.root, self.fid))

    def test_fake_sha_missing_reviewer_and_unobserved_score_are_rejected(self):
        blob = self.entry["training_dispute"]["reports"][0]
        previous = blob["sha256"]
        blob["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "exact verified conflicting originals"):
            self.import_record()
        blob["sha256"] = previous
        self.doc["reviewer"] = ""
        with self.assertRaisesRegex(ValueError, "named reviewer"):
            self.import_record()
        self.doc["reviewer"] = "Example test reviewer"
        self.entry["away_goals"] = 15
        with self.assertRaisesRegex(ValueError, "observed score"):
            self.import_record()

    def test_new_native_score_and_damaged_manual_original_revoke_training(self):
        from copy import deepcopy
        from polymarket861 import store

        report = self.import_record()
        attachment = store.blob_path(self.root, report["evidence_sha256"])
        original = attachment.read_bytes()
        attachment.write_bytes(b"test corrupted original")
        self.case.set_time(4)
        self.assertIn(
            "reviewed_source_facts_changed",
            [r["reason"] for r in self.case.reconcile()["quarantined"]],
        )
        self.assertIsNone(self.case.state(self.fid)["away_goals"])
        attachment.write_bytes(original)
        self.case.set_time(5)
        self.doc["reviewer"] = "Second example test reviewer"
        self.import_record()
        rows = deepcopy(self.case.rows)
        next(
            r
            for r in rows[0]["matchResults"]
            if r["resultTypeKind"] == "After90Minutes"
        )["pointsTeam2"] = 3
        self.case.set_time(40)
        self.case.collect(rows)
        self.case.reconcile()
        self.assertIsNone(self.case.state(self.fid)["away_goals"])
        self.assertIsNone(result_admission.verified_regulation(self.root, self.fid))

    def test_new_match_id_holds_and_original_rollback_is_audited(self):
        batch = self.import_record()
        self.case.set_time(31)
        self.events.evidence(self.kick, [2, 2], match_id="different-official-event")
        self.assertTrue(self.case.reconcile()["quarantined"])
        self.assertIsNone(self.case.state(self.fid)["home_goals"])
        self.assertIsNone(result_admission.verified_regulation(self.root, self.fid))
        self.case.set_time(33)
        with self.assertRaisesRegex(ValueError, "later result or schedule change"):
            result_operations.rollback(self.root, batch["batch_id"], "test withdrawal")
        self.assertIsNone(self.case.state(self.fid)["home_goals"])
        self.assertIsNone(result_admission.verified_regulation(self.root, self.fid))
