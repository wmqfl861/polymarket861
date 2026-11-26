"""A terminal binary receipt must agree with all observed exchange evidence at credit."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from threading import Event
import unittest
from unittest.mock import patch

import test_exchange_split_05 as samples
import test_opportunity as venue
from polymarket861 import ledger, settlement, store


class BinaryEvidenceHistoryTests(unittest.TestCase):
    def setUp(self):
        self.case = samples.SplitSettlementTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root, self.stamp = self.case.root, self.case.stamp

    def sync(self, now=None):
        with (
            venue.at(now or self.stamp),
            patch.object(
                settlement, "_regulation", return_value=self.case.case.score()
            ),
        ):
            return settlement.sync(self.root, cached_only=True)

    def prepared_event(self):
        row = next(
            r
            for r in settlement._positions(self.root, None, 50, self.stamp)
            if r["mode"] == "formal"
        )
        return settlement._match(
            self.root,
            row,
            settlement.token_status(self.root, "711", cached_only=True),
            self.case.case.score(),
        )

    def test_valid_pair_matches_and_conflicting_opposite_blocks_manual_replay(self):
        before = self.case.balances()
        self.case.other("0", self.stamp - timedelta(minutes=2))
        self.case.case.resolution("1", self.stamp)
        with venue.at(self.stamp):
            event = self.prepared_event()
        self.assertEqual(self.sync()["states"], {"settled": 2})
        paid = self.case.balances()
        self.assertTrue(all(paid[m] > initial for m, initial in before.items()))
        with venue.at(self.stamp):
            self.assertEqual(ledger.settle(self.root, "main", event), event)
        # SQL insertion order changes; every exchange clock remains equal to the last audit.
        self.case.other("0.5", self.stamp)
        self.assertEqual(self.sync()["states"], {"manual_review": 2})
        with venue.at(self.stamp), self.assertRaisesRegex(ValueError, "conflicting"):
            ledger.settle(self.root, "main", event)
        self.assertEqual(self.case.balances(), paid)

    def test_legacy_posting_without_watch_metadata_reopens_immediately_on_late_opposite(
        self,
    ):
        self.case.case.resolution("1", self.stamp)
        first = self.sync()
        self.assertEqual(first["states"], {"settled": 2})
        self.assertTrue(
            all(p["detail"]["opposite_token_id"] == "722" for p in first["positions"])
        )
        self.assertEqual(self.sync()["examined"], 0)
        paid = self.case.balances()
        with closing(store.connect(self.root)) as db, db:
            db.execute(
                "UPDATE settlement_queue SET details=json_remove(details,'$.opposite_token_id','$.resolution_request_cursor')"
            )
        self.case.other("0.5", self.stamp - timedelta(minutes=1))
        self.assertEqual(self.sync()["states"], {"manual_review": 2})
        self.assertEqual(
            self.sync(self.stamp + timedelta(days=1, minutes=5))["states"],
            {"manual_review": 2},
        )
        self.assertEqual(self.case.balances(), paid)

    def test_contradictions_on_held_or_opposing_token_block_both_atomic_cash_writes(
        self,
    ):
        before = self.case.balances()
        self.case.case.resolution("1", self.stamp)
        with venue.at(self.stamp):
            event = self.prepared_event()
            positions = settlement._positions(self.root, None, 50, self.stamp)
            shadow_row = next(r for r in positions if r["mode"] == "research_shadow")
            committed = Event()

            def cash_call(call):
                self.assertTrue(committed.wait(timeout=5))
                with self.assertRaisesRegex(ValueError, "conflicting"):
                    call()

            def inject():
                try:
                    self.case.other("0.5", self.stamp)
                finally:
                    committed.set()

            with ThreadPoolExecutor(max_workers=3) as workers:
                formal = workers.submit(
                    cash_call, lambda: ledger.settle(self.root, "main", event)
                )
                shadow = workers.submit(
                    cash_call,
                    lambda: settlement._credit_shadow(self.root, shadow_row, event),
                )
                workers.submit(inject).result()
                formal.result()
                shadow.result()
            self.assertEqual(self.sync()["states"], {"manual_review": 2})
            self.case.case.resolution("0.5", self.stamp - timedelta(seconds=10))
            with self.assertRaisesRegex(ValueError, "conflicting"):
                ledger.settle(self.root, "main", event)
            self.assertEqual(self.case.balances(), before)

    def test_receipt_committed_between_payment_and_queue_save_vetoes_settled_label(
        self,
    ):
        before = self.case.balances()
        self.case.case.resolution("1", self.stamp)
        original = settlement._record
        calls = []

        def record(root, position, state, reason, detail):
            if not calls:
                calls.append(position["mode"])
                self.case.other("0.5", self.stamp)
            return original(root, position, state, reason, detail)

        with patch.object(settlement, "_record", side_effect=record):
            observed = self.sync()
        self.assertEqual(observed["states"], {"manual_review": 2}, observed)
        paid = self.case.balances()
        self.assertGreater(paid[calls[0]], before[calls[0]])
        self.assertTrue(
            all(paid[mode] == cash for mode, cash in before.items() if mode != calls[0])
        )
        self.sync()
        self.assertEqual(self.case.balances(), paid)
