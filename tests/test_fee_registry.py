"""Only the test suite installs this fictional policy. Production has no verified paid EPL version."""

import hashlib
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

from market_terms_fixture import CONDITION, SLUG, TOKEN, event, receive

from polymarket861 import (
    fee_registry as fees,
)
from polymarket861 import (
    ledger_rules,
    ledger_terms,
    market_archive,
    market_provider,
    store,
)


class ReviewedFeesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = temp.name
        self.start = store.iso("2025-01-01T00:00:00Z")
        self.received = store.iso("2025-02-01T11:00:00Z")
        self.captured = store.iso("2025-02-01T11:05:00Z")
        self.decision = store.iso("2025-02-01T14:00:00Z")
        self.expiry = store.iso("2025-02-01T14:00:05Z")
        # This is an explicit, synthetic unit-test fixture. It is never shipped in REVIEWED.
        self.body = b"Test fee convention. Cash = shares * .05 * price * (1-price); round each execution price level half-up to .00001."
        self.doc = fees.Document(
            "https://docs.polymarket.com/test-only/fee-policy",
            hashlib.sha256(self.body).hexdigest(),
            self.start,
        )
        self.profile = fees.FeePolicy(
            "test-only-v1",
            "v1",
            ("total",),
            self.start,
            self.expiry,
            (self.doc,),
            "0.05",
            1,
            "symmetric",
            "cash",
            "eip155:137/erc20:0x" + "a" * 40,
            "cash",
            "0.00001",
            "half_up",
            "per_execution_price_level",
            "none",
        )
        data = event("2025-02-01T15:00:00Z", fee=True)
        data["markets"][0]["feeSchedule"] = {
            "rate": 0.05,
            "exponent": 1,
            "takerOnly": True,
        }
        receive(self.root, data, self.received)
        self.market = market_archive.contracts(self.root, SLUG, as_of=self.decision)[0]

    def receive_policy(self, body=None):
        reply = market_provider.Reply(
            self.body if body is None else body,
            self.captured,
            self.captured,
            {},
            self.doc.url,
        )
        with patch.object(fees, "_read_document", return_value=reply):
            return fees.capture(self.root, self.profile.key)

    def at(self, cutoff):
        return ledger_terms.contract_at(self.root, CONDITION, TOKEN, cutoff)

    def test_complete_pinned_profile_and_rate_at_both_books(self):
        self.assertEqual(fees.list_policies()["policies"], [])
        with patch.object(fees, "REVIEWED", (self.profile,)):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                self.at(self.decision)
            # A raw local upload that happens to be byte-identical lacks collector receipts.
            store.archive_bytes(
                self.root,
                "polymarket.com",
                self.body,
                observed_at=self.received,
                metadata={"url": self.doc.url, "operation": "fee_policy"},
            )
            with self.assertRaisesRegex(ValueError, "unavailable"):
                self.at(self.decision)
            capture = self.receive_policy()
            self.assertEqual(capture["receipts"][0]["sha256"], self.doc.sha256)
            c = self.at(self.decision)
            charged = c["fee_schedule"]
            self.assertEqual(charged["source_sha256"], self.doc.sha256)
            self.assertEqual(charged["gamma_source_sha256"], c["rule_source_sha256"])
            self.assertNotEqual(charged["source_sha256"], c["rule_source_sha256"])
            self.assertEqual(charged["received_at"], self.captured)
            self.assertEqual(
                ledger_rules.charge(Decimal(10), Decimal(".50"), charged),
                (Decimal(".12500"), 0),
            )
            with self.assertRaisesRegex(ValueError, "unavailable"):
                self.at(self.received)
            ledger_terms.verify_book_terms(
                c, self.market, data_dir=self.root, at=self.decision
            )
            with self.assertRaisesRegex(ValueError, "reviewed effective"):
                ledger_terms.verify_book_terms(
                    c, self.market, data_dir=self.root, at=self.expiry
                )
            for different in [{"rate": 0.04}, {"exponent": 2}, {"takerOnly": False},
                              {"additionalCashPerShare": 0.03}, {"rebateRate": -1}, {"rebateRate": True}]:
                native = self.market | {
                    "fee_schedule": self.market["fee_schedule"] | different
                }
                with self.subTest(difference=different), self.assertRaises(ValueError):
                    ledger_terms.verify_book_terms(
                        c, native, data_dir=self.root, at=self.decision
                    )
            # The bytes are still intact. Editing the reviewed rounding fields changes the
            # policy digest, and existing collector receipts cannot certify that new mapping.
            for altered in [
                replace(self.profile, rounding="down"),
                replace(self.profile, quantum="1"),
                replace(self.profile, asset="shares", round_in="charged"),
                replace(self.profile, effective_until="2030-01-01T00:00:00Z"),
            ]:
                with (
                    patch.object(fees, "REVIEWED", (altered,)),
                    self.assertRaisesRegex(ValueError, "unavailable"),
                ):
                    self.at(self.decision)
            store.blob_path(self.root, self.doc.sha256).write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "integrity"):
                self.at(self.decision)

    def test_unknown_ambiguous_changed_or_incomplete_profiles_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown"):
            fees.capture(self.root, "self-declared")
        with (
            patch.object(fees, "REVIEWED", (self.profile,)),
            self.assertRaisesRegex(ValueError, "differs"),
        ):
            self.receive_policy(b"not the reviewed fee document")
        with (
            patch.object(
                fees, "REVIEWED", (self.profile, replace(self.profile, key="alternate"))
            ),
            self.assertRaisesRegex(ValueError, "one reviewed"),
        ):
            self.at(self.decision)
        foreign = replace(self.doc, url="https://example.invalid/policy")
        for bad in [
            replace(self.profile, documents=(foreign,)),
            replace(self.profile, documents=()),
            replace(self.profile, aggregation="per_order"),
            replace(self.profile, collateral_asset="USD"),
            replace(self.profile, rounding="guess"),
            replace(self.profile, share_conversion="floor"),
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                fees._checked(bad)
