"""The strategy may select fixtures but cannot drop a losing or unpriced sibling from a selected one."""

import unittest
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR
from unittest.mock import patch

import test_opportunity as native
import test_strategy as single
from kickoff_fixture import capture_example
from polymarket861 import (
    kickoff_proof,
    ledger,
    market_archive,
    opportunity,
    store,
    strategy,
)


class StrategyScopeTests(unittest.TestCase):
    def start(self, root, name, inputs, season, source="main"):
        history = "historical-" + name
        ledger.replay(root, inputs, account=history)
        return strategy.register_trial(
            root,
            name,
            {"max_orders": 1},
            source_account=source,
            history_account=history,
            history_inputs=inputs,
            season=season,
            first_round=1,
            budget=17,
        )

    def test_two_held_tokens_one_close_and_omitted_sibling_on_registration_or_old_score(
        self,
    ):
        case = native.Opportunities()
        case.setUp()
        self.addCleanup(case.doCleanups)
        root = case.root
        with patch.dict(kickoff_proof.OPEN_RELEASES):
            prob = Decimal(str(case.seal()["probabilities"]["total_over_2_5"]))
            quotes = {
                k: (v - Decimal(".04")).quantize(Decimal(".01"), rounding=ROUND_FLOOR)
                for k, v in (("711", prob), ("722", 1 - prob))
            }
            for token, price in quotes.items():
                case.book(native.CUT - timedelta(seconds=7), price, token=token)
            with native.at(native.CUT - timedelta(seconds=5)):
                opportunity.process(root, native.SLUG)
            for token, price in quotes.items():
                case.book(native.CUT + timedelta(seconds=6), price, token=token)
            with native.at(native.CUT + timedelta(seconds=9)):
                opportunity.process(root, native.SLUG)
            case.book(
                native.KICK - timedelta(seconds=10),
                quotes["711"] + Decimal(".04"),
                token="711",
            )
            with native.at(native.KICK + timedelta(hours=3)):
                capture_example(
                    root, store.fixture_at(root, native.FID), 12345, native.KICK
                )
                full = market_archive.replay_inputs(root, [native.SLUG], account="main")
                self.start(root, "full", full, "2026-27")
                strategy.history(root, "full")
                h = strategy.score(root, "full")["history"]
                self.assertEqual(
                    (
                        h["counts"]["opportunities"],
                        h["joint_coverage"],
                        h["counts"].get("paired_fixtures", 0),
                    ),
                    (2, 0.5, 0),
                )
                sample = [x for x in full if x.get("token_id") != "722"]
                with self.assertRaisesRegex(
                    ValueError, "missing verified formal T-60 parents"
                ):
                    self.start(root, "subset", sample, "2027-28")
                self.assertEqual(
                    ledger.ledger_report(root, "historical-subset")["fills"], 1
                )
                dangling = [x for x in full if not x.get("intent")]
                with self.assertRaisesRegex(
                    ValueError, "incomplete historical selection pool"
                ):
                    self.start(root, "only-placeholders", dangling, "2029-30")
                sibling = next(
                    x for x in full if x.get("intent") and x["token_id"] == "722"
                )
                raw = store.blob_path(root, sibling["second_book"]["source_sha256"])
                archive = raw.read_bytes()
                raw.unlink()
                self.start(root, "old-subset", sample, "2028-29")
                strategy.history(root, "old-subset")
                self.assertEqual(
                    strategy.score(root, "old-subset")["history"]["counts"][
                        "paired_fixtures"
                    ],
                    1,
                )
                # Persist the exact pre-scope registration shape. Existing signed trial metadata is read-only on upgrade.
                with strategy.database(root, True) as db:
                    spec = strategy._trial(db, "old-subset")["spec"]
                    spec.pop("history_scope")
                    db.execute(
                        "UPDATE selection_trials SET spec=?,digest=? WHERE key=?",
                        (ledger.packed(spec), ledger.digest(spec), "old-subset"),
                    )
                raw.write_bytes(archive)
                with self.assertRaisesRegex(
                    ValueError, "missing verified formal T-60 parents"
                ):
                    strategy.history(root, "old-subset")
                reviewed = strategy.promote(root, "old-subset")
                h = reviewed["history"]
                self.assertEqual(reviewed["decision"], "remain_shadow")
                self.assertFalse(h["qualified"])
                self.assertEqual(
                    (
                        h["counts"]["opportunities"],
                        h["counts"].get("paired_fixtures", 0),
                    ),
                    (2, 0),
                )
                self.assertEqual(h["missing"]["incomplete_historical_scope"], 1)
                self.assertEqual(
                    h["parent_scope"]["missing_parents"][0][2], sibling["intent_id"]
                )

    def test_choosing_whole_fixtures_does_not_require_out_of_scope_games(self):
        case = single.StrategyTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        a = case.op(11, "2024-25", case.t - timedelta(days=100))
        b = case.op(12, "2024-25", case.t - timedelta(days=99))
        self.start(case.root, "one-fixture", [a], "2026-27", source="future-main")
        strategy.history(case.root, "one-fixture")
        part = strategy.score(case.root, "one-fixture")["history"]
        self.assertEqual(
            (
                part["parent_scope"]["fixture_count"],
                part["counts"]["opportunities"],
                part["counts"]["paired_fixtures"],
            ),
            (1, 1, 1),
        )
        self.start(case.root, "both-fixtures", [a, b], "2027-28", source="future-main")
        strategy.history(case.root, "both-fixtures")
        part = strategy.score(case.root, "both-fixtures")["history"]
        self.assertEqual(
            (
                part["parent_scope"]["fixture_count"],
                part["counts"]["opportunities"],
                part["counts"]["paired_fixtures"],
            ),
            (2, 2, 2),
        )
