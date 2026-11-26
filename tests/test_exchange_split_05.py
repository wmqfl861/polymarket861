import base64
from contextlib import closing
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
import gzip
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import test_opportunity as venue
import test_shadow_settlement as existing
from polymarket861 import (
    ledger,
    ledger_terms,
    market_provider,
    settlement,
    shadow,
    store,
)


class SplitSettlementTests(unittest.TestCase):
    def setUp(self):
        self.case = existing.ShadowAndSettlement()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root
        self.case.open(technical=False, fee=False)
        self.stamp = venue.KICK + timedelta(hours=3)

    def other(self, payout, when=None, **change):
        when = when or self.stamp
        native = {
            "condition_id": venue.CONDITION,
            "token_id": "722",
            "opposite_token_id": "711",
            "outcome": "Under",
            "outcome_index": 1,
            "resolved": payout is not None,
            "closed": payout is not None,
            "final_price": payout,
            "event_slug": venue.SLUG,
            "market_slug": venue.SLUG + "-total-2pt5",
        } | change

        def get(url, params=None):
            return venue.reply({"data": [native]}, when, url + "?" + urlencode(params))

        with venue.at(when), patch.object(market_provider, "get", side_effect=get):
            ledger.capture_resolution(self.root, "722")

    def balances(self):
        return {
            "formal": Decimal(ledger.ledger_report(self.root, "main")["cash"]),
            "research_shadow": Decimal(shadow.report(self.root)["cash"]),
        }

    def reconcile(self, at=None):
        with (
            venue.at(at or self.stamp),
            patch.object(settlement, "_regulation", side_effect=AssertionError),
        ):
            return settlement.sync(self.root, cached_only=True)

    def test_two_terminal_receipts_credit_both_books_exactly_once_and_reopen_on_opposite_correction(
        self,
    ):
        before = self.balances()
        self.case.resolution("0.5", self.stamp)
        self.assertEqual(self.reconcile()["states"], {"manual_review": 2})
        self.assertEqual(self.balances(), before)
        self.other(None)
        self.assertEqual(self.reconcile()["states"], {"manual_review": 2})
        self.other("0.5", self.stamp + timedelta(seconds=2))
        now = self.stamp + timedelta(seconds=3)
        result = self.reconcile(now)
        self.assertEqual(result["states"], {"settled": 2}, result)
        with closing(store.connect(self.root)) as db:
            event = json.loads(
                db.execute(
                    "SELECT payload FROM paper_resolutions WHERE terminal"
                ).fetchone()[0]
            )
            for table, mode in (
                ("paper_intents", "formal"),
                ("research_shadow_orders", "research_shadow"),
            ):
                row = json.loads(
                    db.execute(
                        "SELECT result FROM " + table + " WHERE status='settled'"
                    ).fetchone()[0]
                )
                credit = (Decimal(row["net_shares"]) * Decimal("0.5")).quantize(
                    ledger.UNIT, rounding=ROUND_DOWN
                )
                self.assertEqual(self.balances()[mode], before[mode] + credit)
            self.assertEqual(event["scenario"], "exchange_split")
            self.assertIsNone(event["score"])
            self.assertEqual(
                [t["outcome"] for t in event["payout_vector"]], ["Over", "Under"]
            )
            self.assertTrue(event["rule_profile"]["template_sha256"])
            self.assertEqual(
                len(set(t["source_sha256"] for t in event["payout_vector"])), 2
            )
        after = self.balances()
        with venue.at(now):
            self.assertEqual(ledger.settle(self.root, "main", event), event)
            with self.assertRaises(ValueError):
                ledger.settle(self.root, "main", event | {"rule_sha256": "0" * 64})
            with self.assertRaises(ValueError):
                ledger.settle(self.root, "main", event | {"scenario": "canceled"})
        self.assertEqual(self.reconcile(now)["examined"], 0)
        self.other("1", now + timedelta(minutes=1))
        self.assertEqual(
            self.reconcile(now + timedelta(minutes=2))["states"], {"manual_review": 2}
        )
        self.assertEqual(self.balances(), after)

    def test_reversed_labels_pending_wrong_condition_or_noncomplementary_values_never_credit(
        self,
    ):
        before = self.balances()
        self.case.resolution("0.5", self.stamp)
        for change in (
            {"outcome": "Over"},
            {"outcome_index": 0},
            {"condition_id": "wrong"},
            {"opposite_token_id": "909"},
            {"closed": False},
            {"market_slug": "different"},
            {"event_slug": "different"},
            {"final_price": "0.25"},
        ):
            with self.subTest(change=change):
                self.other("0.5", **change)
                self.assertEqual(self.reconcile()["states"], {"manual_review": 2})
                self.assertEqual(self.balances(), before)
                with closing(store.connect(self.root)) as db, db:
                    db.execute(
                        "DELETE FROM ma_requests WHERE operation='resolution' AND selector='722'"
                    )

    def test_nantes_readonly_native_regression_and_exact_historical_wording(self):
        report = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "reports/five-leagues-0.5/settlement-observation.json"
            ).read_text()
        )
        native = {}
        for name, receipt in report["receipts"].items():
            raw = gzip.decompress(base64.b64decode(receipt["body"], validate=True))
            self.assertEqual(hashlib.sha256(raw).hexdigest(), receipt["sha256"])
            native[name] = json.loads(raw)
        g, clob = native["gamma"], native["clob"]
        contract = {
            "fixture": report["fixture_slug"],
            "condition_id": report["condition_id"],
            "slug": g["slug"],
            "family": "total",
            "side": None,
            "line": "2.5",
            "rules": g["description"],
            "rule_sha256": report["rule"]["utf8_sha256"],
            "event_teams": [
                {"ordering": "home", "name": "FC Nantes"},
                {"ordering": "away", "name": "Toulouse FC"},
            ],
            "tokens": [
                {"token_id": token, "outcome": label}
                for token, label in zip(
                    json.loads(g["clobTokenIds"]),
                    json.loads(g["outcomes"]),
                    strict=True,
                )
            ],
        }
        self.assertEqual(g["description"], clob["description"])
        self.assertEqual(g["umaResolutionStatus"], "resolved")
        profile = ledger_terms.rule_profile(contract)
        self.assertEqual(
            profile["template_sha256"], report["rule"]["normalized_template_sha256"]
        )
        self.assertEqual(
            ledger_terms._interpret(contract, "over")[0]["canceled"], "0.5"
        )
        for i, t in enumerate(contract["tokens"]):
            row = native["data-" + str(i)]["data"][0]
            self.assertEqual(row, row | report["verified_terminal_tokens"][i])
            settlement._verify_token(
                contract, {"row": row, "status": "resolved"}, t["token_id"]
            )
            self.assertEqual(row["final_price"], i)
            self.assertEqual(str(i), json.loads(g["outcomePrices"])[i])
            self.assertEqual(clob["tokens"][i]["winner"], bool(i))
        different = contract["rules"].replace("50–50", "75–25")
        with self.assertRaisesRegex(ValueError, "unreviewed"):
            ledger_terms.rule_profile(
                contract
                | {
                    "rules": different,
                    "rule_sha256": hashlib.sha256(different.encode()).hexdigest(),
                }
            )
