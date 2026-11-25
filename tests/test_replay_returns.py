import tempfile
import unittest
from contextlib import contextmanager
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import test_opportunity as origin
from kickoff_fixture import capture_example

from polymarket861 import (
    kickoff_proof,
    ledger,
    market_archive,
    market_provider,
    opportunity,
    store,
)


@contextmanager
def missing_file(path):
    raw = path.read_bytes()
    path.unlink()
    try:
        yield
    finally:
        path.write_bytes(raw)


class ReplayReturns(unittest.TestCase):
    def test_only_newly_verified_execution_and_settlement_count(self):
        case = origin.Opportunities()
        case.setUp()
        self.addCleanup(case.doCleanups)
        root = case.root
        f = Decimal(str(case.seal()["probabilities"]["total_over_2_5"]))
        prices = {
            token: (fair - Decimal(".04")).quantize(
                Decimal(".01"), rounding=ROUND_FLOOR
            )
            for token, fair in (("711", f), ("722", 1 - f))
        }
        for token, price in prices.items():
            case.book(origin.CUT - timedelta(seconds=7), price, token=token)
        with origin.at(origin.CUT - timedelta(seconds=5)):
            opportunity.process(root, origin.SLUG)
        execution = {
            token: case.book(origin.CUT + timedelta(seconds=6), price, token=token)
            for token, price in prices.items()
        }
        with origin.at(origin.CUT + timedelta(seconds=9)):
            opportunity.process(root, origin.SLUG)
        case.book(origin.KICK - timedelta(seconds=10), prices["711"] + Decimal(".04"))
        with (
            patch.dict(kickoff_proof.OPEN_RELEASES),
            origin.at(origin.KICK + timedelta(hours=3)),
        ):
            fx = store.fixture_at(root, origin.FID)
            capture_example(root, fx, 98765, origin.KICK)
            opportunity.process(root, origin.SLUG)
            raw = {
                "data": [
                    {
                        "token_id": "711",
                        "condition_id": origin.CONDITION,
                        "resolved": True,
                        "final_price": 1,
                    }
                ]
            }

            def exchange(url, params=None):
                return origin.reply(
                    raw,
                    origin.KICK + timedelta(hours=2),
                    url + "?" + urlencode(params or {}),
                )

            with patch.object(market_provider, "get", side_effect=exchange):
                receipt = ledger.capture_resolution(root, "711")
            ledger.settle(
                root, "main", receipt | {"scenario": "normal", "score": [3, 0]}
            )
            ops = market_archive.replay_inputs(root, [origin.SLUG], account="main")

            def replay():
                return ledger.replay(root, ops)

            valid = replay()
            self.assertEqual(
                [valid["counts"][k] for k in ("A", "filled", "settled")], [2, 2, 1]
            )
            self.assertEqual(
                (
                    valid["clv_covered_fills"],
                    valid["clv_eligible_fills"],
                    valid["clv_coverage"],
                ),
                (1, 2, "0.5"),
            )
            cash = ledger.ledger_report(root, valid["account"])["cash"]
            funding = {
                r["token_id"]: Decimal(r["spent"])
                for r in valid["opportunities"]
                if r["filled"]
            }
            self.assertEqual(Decimal(valid["total_spent"]), sum(funding.values()))
            self.assertEqual(Decimal(valid["settled_cost"]), funding["711"])
            self.assertEqual(Decimal(valid["open_cost"]), funding["722"])
            self.assertEqual(
                Decimal(valid["roi"]), Decimal(valid["realized_pnl"]) / funding["711"]
            )
            # Remove only the winning order's native execution observation.
            with missing_file(store.blob_path(root, execution["711"]["source_sha256"])):
                partial = replay()
                self.assertEqual(
                    [partial["counts"][k] for k in ("A", "filled", "settled")],
                    [1, 1, 0],
                )
                self.assertEqual(
                    (Decimal(partial["total_spent"]), Decimal(partial["open_cost"])),
                    (funding["722"], funding["722"]),
                )
                self.assertEqual(
                    (
                        partial["roi"],
                        partial["realized_pnl"],
                        partial["clv_eligible_fills"],
                        partial["clv_covered_fills"],
                    ),
                    (None, "0", 1, 0),
                )
                self.assertEqual(
                    ledger.ledger_report(root, valid["account"])["cash"], cash
                )
            self.assertEqual(replay()["roi"], valid["roi"])
            # Return the entry evidence but remove the source for the official payout.
            with missing_file(store.blob_path(root, receipt["source_sha256"])):
                pending = replay()
                self.assertEqual(
                    [pending["counts"][k] for k in ("A", "filled", "settled")],
                    [2, 2, 0],
                )
                self.assertEqual(
                    (
                        pending["roi"],
                        pending["settled_proceeds"],
                        pending["settled_cost"],
                    ),
                    (None, "0", "0"),
                )
                self.assertEqual(Decimal(pending["open_cost"]), sum(funding.values()))
                self.assertEqual(pending["clv_covered_fills"], 1)
                winner = next(
                    r for r in pending["opportunities"] if r["token_id"] == "711"
                )
                self.assertEqual((winner["status"], winner["pnl"]), ("filled", None))
                self.assertIn("settlement_error", winner)
            self.assertEqual(replay()["roi"], valid["roi"])
            # Once the opposing token is officially paid zero, revoking just the winner
            # leaves a valid losing cohort with its own denominator and a -100% return.
            raw["data"][0].update(token_id="722", final_price=0)
            with patch.object(market_provider, "get", side_effect=exchange):
                losing_receipt = ledger.capture_resolution(root, "722")
            ledger.settle(
                root, "main", losing_receipt | {"scenario": "normal", "score": [3, 0]}
            )
            final_ops = market_archive.replay_inputs(
                root, [origin.SLUG], account="main"
            )
            complete = ledger.replay(root, final_ops)
            self.assertEqual(complete["counts"]["settled"], 2)
            with missing_file(store.blob_path(root, execution["711"]["source_sha256"])):
                surviving = ledger.replay(root, final_ops)
                self.assertEqual(
                    [surviving["counts"][k] for k in ("A", "filled", "settled")],
                    [1, 1, 1],
                )
                self.assertEqual(Decimal(surviving["settled_cost"]), funding["722"])
                self.assertEqual(Decimal(surviving["settled_proceeds"]), 0)
                self.assertEqual(Decimal(surviving["realized_pnl"]), -funding["722"])
                self.assertEqual(Decimal(surviving["roi"]), -1)
            with self.assertRaisesRegex(ValueError, "conflicting"):
                capture_example(root, fx, 98766, origin.KICK + timedelta(seconds=1))
            invalid = replay()
            self.assertEqual(
                [invalid["counts"][k] for k in ("A", "filled", "settled")], [0, 0, 0]
            )
            self.assertEqual(
                (
                    invalid["roi"],
                    invalid["clv_coverage"],
                    invalid["realized_pnl"],
                    invalid["total_spent"],
                ),
                (None, None, "0", "0"),
            )
            self.assertTrue(all(g["roi"] is None for g in invalid["groups"]))
            audit = ledger.ledger_report(root, valid["account"])
            self.assertEqual(
                (audit["cash"], audit["settled_positions"], audit["fills"]),
                (cash, 1, 2),
            )
            self.assertEqual(audit["realized_pnl"], valid["realized_pnl"])

    def test_cash_and_share_fees_open_stakes_and_zero_payout(self):
        def make(label, fee_asset, terminal):
            op = ledger.example()
            i = op["intent"]
            c = i["contract"]
            i.update(intent_id=label, match_id=label)
            c.update(match_id=label, token_id=label, condition_id=label)
            c["fee_schedule"].update(asset=fee_asset)
            for key in ("first_book", "second_book", "close_book", "settlement"):
                op[key].update(condition_id=label, token_id=label)
            if terminal == "loss":
                op["settlement"].update(scenario="normal", score=[0, 0], payout="0")
            if terminal is None:
                op.pop("settlement")
                op.pop("close_book")
            op["liquidity"] = label
            return op

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ops = [
                make("cash", "cash", "loss"),
                make("shares", "shares", "partial"),
                make("open", "cash", None),
            ]
            r = ledger.replay(root, ops, source="synthetic")
            by = {
                i["intent_id"]: i
                for i in ledger.ledger_report(root, r["account"])["intents"]
            }
            self.assertEqual(
                [r["counts"][k] for k in ("A", "filled", "settled")], [3, 3, 2]
            )
            cash, shares, open_ = (by[k] for k in ("cash", "shares", "open"))
            self.assertGreater(
                Decimal(cash["spent"]),
                Decimal(cash["fill_price"]) * Decimal(cash["gross_shares"]),
            )
            self.assertLess(
                Decimal(shares["net_shares"]), Decimal(shares["gross_shares"])
            )
            fees = sum(Decimal(i["fee_cash_equivalent"]) for i in (cash, shares))
            resolved_cost = sum(Decimal(i["spent"]) for i in (cash, shares))
            proceeds = Decimal(shares["proceeds"])
            self.assertEqual(Decimal(cash["proceeds"]), 0)
            self.assertEqual(Decimal(r["settled_cost"]), resolved_cost)
            self.assertEqual(Decimal(r["open_cost"]), Decimal(open_["spent"]))
            self.assertEqual(Decimal(r["settled_proceeds"]), proceeds)
            self.assertEqual(
                Decimal(r["total_fees_cash_equivalent"]),
                fees + Decimal(open_["fee_cash_equivalent"]),
            )
            self.assertEqual(Decimal(r["settled_fees_cash_equivalent"]), fees)
            self.assertEqual(
                Decimal(r["roi"]), (proceeds - resolved_cost) / resolved_cost
            )
            self.assertEqual((r["clv_covered_fills"], r["clv_eligible_fills"]), (2, 3))
            groups = {g["liquidity"]: g for g in r["groups"]}
            self.assertEqual(Decimal(groups["cash"]["roi"]), Decimal(-1))
            self.assertIsNone(groups["open"]["roi"])
            self.assertEqual(ledger.replay(root, ops, source="synthetic"), r)
