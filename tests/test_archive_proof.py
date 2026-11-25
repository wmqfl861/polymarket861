import json
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from archive_capture_fixture import exchange_op, formal_example
from kickoff_fixture import capture_example
from polymarket861 import (
    archive_proof,
    kickoff_proof,
    ledger,
    market_archive,
    market_provider,
    store,
)


class HistoricalArchiveProofTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.op = ledger.example()
        self.op.pop("settlement")
        i = self.op["intent"]
        c = i["contract"]
        fixture = "epl-hom-awa-2025-02-01"
        i.update(match_id=fixture, intent_id=fixture)
        c.update(match_id=fixture, condition_id="0x" + "5" * 64, token_id="77")
        with patch.object(store, "now_utc", return_value="2025-01-02T00:00:00.000000Z"):
            fx = store.register_fixture(
                self.root,
                {
                    "fixture_id": fixture,
                    "round_id": 1,
                    "season": "2024-25",
                    "home": "Home FC",
                    "away": "Away FC",
                    "kickoff": i["kickoff"],
                    "known_at": "2025-01-01T00:00:00Z",
                    "source": "fixture-only schedule",
                    "locator": "fixture://schedule",
                },
            )
        clock = patch.dict(kickoff_proof.OPEN_RELEASES, clear=True)
        clock.start()
        self.addCleanup(clock.stop)
        self.op.update(
            depth_provenance="self_archive", kickoff_source="original fixture release"
        )
        exchange_op(
            self.root, self.op, store.instant(i["kickoff"]), fx["home"], fx["away"]
        )
        formal_example(self.root, self.op)
        capture_example(self.root, fx, 9901, i["kickoff"])

    def test_old_replay_fingerprint_and_direct_unbound_freeze_cannot_enter_new_accounts(
        self,
    ):
        old = ledger.digest(["historical", [self.op], "native-exchange-archive-v1"])
        old_account = "replay-" + old[:16]
        ledger.open_account(self.root, old_account, "historical")
        with ledger.connection(self.root, True) as db:
            db.execute(
                "CREATE TABLE paper_replays(account TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)"
            )
            db.execute("INSERT INTO paper_replays VALUES (?,?)", (old_account, old))
        with self.assertRaisesRegex(ValueError, "different input manifest"):
            ledger.replay(self.root, [self.op], account=old_account)
        new = ledger.replay(self.root, [self.op])
        self.assertNotEqual(new["account"], old_account)
        self.assertEqual(new["counts"]["filled"], 1)
        with self.assertRaisesRegex(ValueError, "registered replay manifest"):
            ledger.freeze(
                self.root, old_account, self.op["intent"], self.op["first_book"]
            )
        without = dict(self.op)
        without.pop("paper_account")
        failed = ledger.replay(self.root, [without])
        self.assertEqual((failed["counts"]["A"], failed["counts"]["filled"]), (0, 0))
        with self.assertRaisesRegex(ValueError, "outside this account"):
            ledger.freeze(
                self.root,
                failed["account"],
                self.op["intent"],
                self.op["first_book"],
                replay_op=self.op,
            )

    def test_source_forecast_receipt_is_checked_again_after_archive(self):
        self.assertEqual(
            ledger._evidence_tier(self.root, self.op, "historical")[0], "A"
        )
        with ledger.connection(self.root, True) as db:
            db.execute(
                "UPDATE forecast_seals SET sealed_at=? WHERE digest=?",
                (self.op["intent"]["kickoff"], self.op["forecast_digest"]),
            )
        self.assertEqual(
            ledger._evidence_tier(self.root, self.op, "historical")[0], "missing"
        )

    def test_native_first_second_and_close_replay(self):
        result = ledger.replay(self.root, [self.op])
        self.assertEqual(
            (
                result["counts"]["A"],
                result["counts"]["filled"],
                result["clv_covered_fills"],
            ),
            (1, 1, 1),
        )
        self.assertIsNone(
            result["roi"]
        )  # This fixture supplies no exchange settlement.

    def test_historical_close_uses_archived_books_even_when_caller_alters_or_omits_close(
        self,
    ):
        baseline = ledger.replay(self.root, [self.op], account="original")
        expected = baseline["opportunities"][0]["clv"]
        self.assertIsNotNone(expected)
        altered = deepcopy(self.op)
        altered["close_book"]["asks"][0]["price"] = "0.45"
        omitted = deepcopy(self.op)
        omitted.pop("close_book")
        for name, op in (("altered", altered), ("omitted", omitted)):
            with self.subTest(name=name):
                result = ledger.replay(self.root, [op], account=name)
                self.assertEqual(result["counts"]["filled"], 1)
                self.assertEqual(result["clv_covered_fills"], 1)
                self.assertEqual(result["opportunities"][0]["clv"], expected)

        with closing(market_archive.database(self.root)) as db, db:
            db.execute(
                "DELETE FROM ma_requests WHERE operation='book' AND sha=?",
                (self.op["close_book"]["source_sha256"],),
            )
        result = ledger.replay(self.root, [altered], account="invalid-only")
        self.assertEqual(result["counts"]["filled"], 1)
        self.assertEqual(result["clv_covered_fills"], 0)
        self.assertIsNone(result["opportunities"][0]["clv"])

    def test_capture_requires_native_evidence_and_accepts_last_valid_observation(self):
        first = self.op["first_book"]
        cutoff = store.instant(self.op["intent"]["decision_at"])
        c = self.op["intent"]["contract"]
        corrupted = dict(first, received_at=store.iso(cutoff))
        unrelated = store.archive_bytes(
            self.root,
            "unrelated-source",
            b"unrelated note",
            observed_at=store.iso(cutoff),
        )
        corrupted["source_sha256"] = unrelated["sha256"]
        with closing(market_archive.database(self.root)) as db, db:
            db.execute(
                "INSERT INTO ma_books VALUES(?,?,?,?)",
                (
                    c["token_id"],
                    corrupted["received_at"],
                    unrelated["sha256"],
                    store.canonical(corrupted),
                ),
            )
            self.assertEqual(
                ledger.eligible_book(
                    db, self.root, c, cutoff, self.op["actual_kickoff"], "first"
                ),
                first,
            )
            db.execute(
                "DELETE FROM ma_requests WHERE operation='book' AND sha=?",
                (first["source_sha256"],),
            )
        got = ledger.replay(self.root, [self.op])
        self.assertEqual(got["counts"]["missing"], 1)
        self.assertEqual(got["counts"]["filled"], 0)

    def test_raw_source_contract_must_match_even_if_operator_changes_both_display_hashes(
        self,
    ):
        with closing(market_archive.database(self.root)) as db, db:
            row = db.execute("SELECT rowid,payload FROM ma_contracts").fetchone()
            fields = json.loads(row["payload"])
            fields["rule_sha256"] = "e" * 64
            db.execute(
                "UPDATE ma_contracts SET payload=? WHERE rowid=?",
                (store.canonical(fields), row["rowid"]),
            )
        op = deepcopy(self.op)
        for k in ("first_book", "second_book", "close_book"):
            op[k]["rule_sha256"] = fields["rule_sha256"]
        op["intent"]["contract"]["rule_sha256"] = fields["rule_sha256"]
        got = ledger.replay(self.root, [op])
        self.assertEqual(got["counts"]["missing"], 1)
        self.assertEqual(got["counts"]["filled"], 0)

    def test_late_whistle_can_explain_pregame_only_with_rechecked_independent_proof(
        self,
    ):
        op = ledger.example()
        i = op["intent"]
        c = i["contract"]
        fixture = "epl-one-two-2025-02-01"
        i["match_id"] = fixture
        c.update(match_id=fixture, condition_id="0x" + "7" * 64, token_id="89")
        planned = store.instant(i["kickoff"])
        actual = planned + timedelta(seconds=42)
        fx = store.register_fixture(
            self.root,
            {
                "fixture_id": fixture,
                "round_id": 1,
                "season": "2024-25",
                "home": "One FC",
                "away": "Two FC",
                "kickoff": i["kickoff"],
                "known_at": "2025-01-01T00:00:00Z",
                "source": "test-only",
                "locator": "fixture://delayed",
            },
        )
        capture_example(self.root, fx, 9902, actual)
        evidence = kickoff_proof.resolve(self.root, fixture, planned, i["decision_at"])
        exchange_op(
            self.root,
            op,
            planned,
            fx["home"],
            fx["away"],
            quotes=[("close_book", 3632, ".38", ".40")],
        )
        self.assertFalse(op["close_book"]["pregame"])
        with closing(market_archive.database(self.root)) as db:
            args = (db, self.root, i["contract"], actual, actual, "close")
            self.assertIsNone(ledger.eligible_book(*args))
            self.assertIsNone(
                ledger.eligible_book(
                    *args, verified_actual=dict(evidence, proof_id="unverified")
                )
            )
            close = ledger.eligible_book(*args, verified_actual=evidence)
            self.assertTrue(close["pregame"])
            self.assertEqual(close["kickoff_proof_id"], evidence["proof_id"])
            self.assertFalse(
                json.loads(
                    db.execute(
                        "SELECT payload FROM ma_books WHERE token='89'"
                    ).fetchone()[0]
                )["pregame"]
            )

    def test_event_with_removed_market_supersedes_an_older_contract_row(self):
        i = self.op["intent"]
        c = i["contract"]
        slug = i["match_id"]
        evidence = c["rule_source_sha256"]
        native = json.loads(store.blob_path(self.root, evidence).read_bytes())
        native["markets"] = []
        later = store.iso(store.instant(i["decision_at"]) - timedelta(seconds=4))
        url = market_provider.GAMMA + "/events/slug/" + slug
        receipt = market_provider.Reply(
            json.dumps(native).encode(), later, later, {}, url
        )
        with patch.object(market_provider, "get", return_value=receipt):
            event, response, sha = market_archive._fetch(self.root, "event", slug, url)
        with closing(market_archive.database(self.root)) as db, db:
            market_archive._ingest(db, event, response, sha)
            early = archive_proof.original_market(
                db, self.root, c["condition_id"], c["rule_received_at"]
            )
            self.assertEqual(early["condition_id"], c["condition_id"])
            with self.assertRaisesRegex(ValueError, "removed or changed"):
                archive_proof.original_market(db, self.root, c["condition_id"], later)
        result = ledger.replay(self.root, [self.op])
        self.assertEqual(result["counts"]["missing"], 1)
        self.assertEqual(result["clv_covered_fills"], 0)
