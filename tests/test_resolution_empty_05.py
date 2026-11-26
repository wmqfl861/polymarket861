"""An empty authenticated token lookup is retryable only before its first terminal state."""

from datetime import timedelta
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import test_exchange_split_05 as samples
import test_opportunity as fixture
from polymarket861 import ledger, market_provider, settlement


class EmptyResolutionTests(unittest.TestCase):
    def setUp(self):
        self.s = samples.SplitSettlementTests()
        self.s.setUp()
        self.addCleanup(self.s.doCleanups)
        self.root, self.stamp = self.s.root, self.s.stamp

    @staticmethod
    def native(token, amount):
        first = token == "711"
        return {
            "condition_id": fixture.CONDITION,
            "token_id": token,
            "opposite_token_id": "722" if first else "711",
            "outcome_index": 0 if first else 1,
            "outcome": "Over" if first else "Under",
            "resolved": amount is not None,
            "closed": amount is not None,
            "final_price": amount,
            "event_slug": fixture.SLUG,
            "market_slug": fixture.SLUG + "-total-2pt5",
        }

    @staticmethod
    def response(when, payload):
        return lambda url, params=None: fixture.reply(
            payload(params["token_id"]), when, url + "?" + urlencode(params)
        )

    def capture(self, token, payload, when):
        with (
            fixture.at(when),
            patch.object(
                market_provider,
                "get",
                side_effect=self.response(when, lambda _: payload),
            ),
        ):
            return ledger.capture_resolution(self.root, token)

    def test_empty_returns_pending_then_retries_at_15_minutes_and_rechecks_after_one_day(
        self,
    ):
        before = self.s.balances()
        for token in ("711", "722"):
            receipt = self.capture(token, {"data": []}, self.stamp)
            self.assertIsNone(receipt["payout"])
            self.assertEqual(receipt["status"], "pending")
        with (
            fixture.at(self.stamp + timedelta(minutes=14)),
            patch.object(market_provider, "get") as route,
        ):
            self.assertEqual(settlement.sync(self.root)["states"], {"pending": 2})
            route.assert_not_called()
        due = self.stamp + timedelta(minutes=15)
        terminal = lambda token: {"data": [self.native(token, "0.5")]}
        with (
            fixture.at(due),
            patch.object(
                market_provider, "get", side_effect=self.response(due, terminal)
            ) as route,
        ):
            first = settlement.sync(self.root)
            self.assertEqual(first["states"], {"settled": 2}, first)
            self.assertEqual(
                sorted(c.args[1]["token_id"] for c in route.call_args_list),
                ["711", "722"],
            )
        paid = self.s.balances()
        self.assertTrue(all(paid[m] > v for m, v in before.items()))
        next_audit = due + timedelta(days=1)
        with (
            fixture.at(next_audit),
            patch.object(
                market_provider, "get", side_effect=self.response(next_audit, terminal)
            ) as route,
        ):
            self.assertEqual(settlement.sync(self.root)["states"], {"settled": 2})
            self.assertEqual(
                sorted(c.args[1]["token_id"] for c in route.call_args_list),
                ["711", "722"],
            )
        self.assertEqual(paid, self.s.balances())

    def test_initial_empty_opposite_does_not_veto_binary_but_terminal_withdrawal_stays_disputed(
        self,
    ):
        self.s.case.resolution("1", self.stamp)
        self.capture("722", {"data": []}, self.stamp)
        with (
            fixture.at(self.stamp),
            patch.object(settlement, "_regulation", return_value=self.s.case.score()),
        ):
            self.assertEqual(
                settlement.sync(self.root, cached_only=True)["states"], {"settled": 2}
            )
        paid = self.s.balances()
        later = self.stamp + timedelta(minutes=1)
        with self.assertRaisesRegex(ValueError, "withdrew"):
            self.capture("711", {"data": []}, later)
        self.s.case.resolution("1", later + timedelta(seconds=1))
        with (
            fixture.at(later + timedelta(seconds=2)),
            patch.object(settlement, "_regulation", return_value=self.s.case.score()),
        ):
            self.assertEqual(
                settlement.sync(self.root, cached_only=True)["states"],
                {"manual_review": 2},
            )
        self.assertEqual(paid, self.s.balances())

    def test_wrong_condition_in_native_pending_cannot_be_laundered_by_a_later_terminal(
        self,
    ):
        before = self.s.balances()
        wrong = self.native("711", None) | {"condition_id": "wrong-condition"}
        self.capture("711", {"data": [wrong]}, self.stamp)
        self.assertEqual(self.s.reconcile()["states"], {"manual_review": 2})
        later = self.stamp + timedelta(seconds=1)
        self.s.case.resolution("0.5", later)
        self.s.other("0.5", later)
        self.assertEqual(self.s.reconcile(later)["states"], {"manual_review": 2})
        self.assertEqual(before, self.s.balances())

    def test_malformed_empty_and_wrong_identity_are_never_retryable_absence(self):
        wrong = self.native("722", "0.5")
        for body in (
            {},
            {"data": None},
            {"data": [], "error": "unavailable"},
            {"data": [wrong]},
            {"data": [self.native("711", "0.5"), wrong]},
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                market_provider.resolution_row(body, "711")
        before = self.s.balances()
        with self.assertRaisesRegex(ValueError, "wrong or ambiguous"):
            self.capture("711", {"data": [wrong]}, self.stamp)
        self.s.case.resolution("0.5", self.stamp + timedelta(minutes=1))
        self.s.other("0.5", self.stamp + timedelta(minutes=1))
        with fixture.at(self.stamp + timedelta(minutes=2)):
            self.assertEqual(
                settlement.sync(self.root, cached_only=True)["states"],
                {"manual_review": 2},
            )
        self.assertEqual(self.s.balances(), before)
