"""Native-format five-league captures with the venue fee switch on."""

from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_budget_domestic_archive import domestic_entry
from test_budget_scheduled_entry import BASIS
from test_capital_native_proof import record_terminal, terminal_row
from market_terms_fixture import reply
from polymarket861 import big5_market, budget_archive_proof, cli, fee_registry, five_league_budget
from polymarket861 import market_archive, market_provider, store


PROFILE = {"rate": 0.05, "exponent": 1, "takerOnly": True, "rebateRate": 0.15}


def command(root, *args):
    out = io.StringIO()
    with redirect_stdout(out):
        status = cli.main(["--data-dir", str(root), "paper-risk", *args])
    if status != 0:
        raise AssertionError(out.getvalue())
    return json.loads(out.getvalue())


def replay(root, doc):
    return five_league_budget.replay(doc, data_dir=root, entry_basis=BASIS, details=True)


def refresh(root, main, aux, at):
    def gamma(url, params=None):
        return reply(aux if url.endswith("-more-markets") else main, store.iso(at), url)

    with patch.object(market_provider, "get", side_effect=gamma):
        result = big5_market._refresh(big5_market.research_root(root), main["slug"])
    if result["gaps"]:
        raise AssertionError(result)


class ResearchFees(unittest.TestCase):
    def test_all_five_collectors_export_cli_terms_and_keep_default_strict(self):
        for code in five_league_budget.CODES:
            with self.subTest(code=code), TemporaryDirectory() as tmp:
                root = Path(tmp)
                doc, _ = domestic_entry(root, code, explicit=True, fee_schedule=PROFILE)
                op = doc["intents"][0]
                inspected = command(root, "verify-contract", op["condition_id"], "--token-id", op["token_id"],
                                    "--as-of", op["decision_at"], "--entry-basis", BASIS)
                self.assertEqual(inspected["fee_basis"], "research_assumption")
                self.assertEqual(inspected["order_books_checked"], 0)
                op.update(inspected["intent_terms"])
                file = root / "input.json"
                file.write_text(json.dumps(doc))
                default = command(root, "replay", str(file))
                self.assertEqual(default["leagues"][code]["outcomes"], {"skipped_unverified_entry": 1})
                report = command(root, "replay", str(file), "--entry-basis", BASIS, "--details")
                trade = report["decisions"][0]
                self.assertEqual(trade["outcome"], "filled", report["proof_gaps"])
                self.assertEqual(trade["entry_evidence"]["basis"], "explicit_pregame")
                self.assertEqual(trade["fee_evidence"]["basis"], "research_assumption")
                self.assertEqual(trade["fee_evidence"]["schedule"]["gamma_fee_schedule"], PROFILE)
                self.assertEqual([b["source_sha256"] for b in trade["fee_evidence"]["book_gamma"]],
                                 [inspected["source_sha256"]] * 2)
                self.assertEqual(Decimal(trade["fill"]["net_shares"]), Decimal("97.5"))
                self.assertEqual(Decimal(trade["cost_including_cash_fees"]), Decimal(50))
                self.assertIsNone(report["venue_realized_pnl"])
                self.assertFalse(report["entry_policy"]["eligible_for_formal_accounts"])

    def test_multilevel_rounding_limited_depth_and_redemption_of_net_shares(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            asks = {"a": [{"price": "0.50", "size": "30"}, {"price": "0.55", "size": "30"}],
                    "b": [{"price": "0.51", "size": "20"}, {"price": "0.55", "size": "40"}]}
            doc, (main, _) = domestic_entry(root, "bun", fee_schedule=PROFILE, asks=asks)
            op = doc["intents"][0]
            op.update(shares="45", limit_price="0.55")
            # The oversized request cannot consume partial depth; after buying 45 only 15 remain.
            doc["intents"] += [dict(op, id="oversize", rank=0, shares="61"), dict(op, id="later", rank=2, shares="16")]
            archive = big5_market.research_root(root)
            market = market_archive.contracts(archive, main["slug"], as_of=op["decision_at"])[0]
            at = store.iso(store.instant(op["kickoff"]) + timedelta(hours=3))
            held = terminal_row(market, 0, 1)
            other = terminal_row(market, 1, 0)
            sha = record_terminal(archive, held["token_id"], json.dumps({"data": [held]}).encode(), at)
            doc["redemptions"] = [dict(id="result", at=at, original_sha256=sha,
                                       **{k: op[k] for k in ("fixture_id", "condition_id", "token_id")})]
            pending = replay(root, doc)
            country = pending["leagues"]["bun"]
            self.assertEqual(country["outcomes"], {"skipped_depth": 2, "filled": 1})
            self.assertEqual(Decimal(country["unredeemed_cost"]), Decimal("23.95"))
            self.assertEqual(Decimal(country["max_fixture_loss_upper_bound"]), Decimal("23.95"))
            self.assertEqual(Decimal(country["cash_from_redemptions"]), 0)
            trade = next(t for t in pending["decisions"] if t["id"] == "paper")
            levels = trade["fill"]["levels"]
            self.assertEqual([(x["size"], x["fee_shares"]) for x in levels], [("20", "0.490197"), ("25", "0.563637")])
            self.assertTrue(all(Decimal(x["fee_cash"]) == 0 for x in levels))
            self.assertEqual(Decimal(trade["fill"]["fee_cash_equivalent"]), Decimal("0.56000082"))
            self.assertEqual(Decimal(trade["fill"]["net_shares"]), Decimal("43.946166"))
            record_terminal(archive, other["token_id"], json.dumps({"data": [other]}).encode(), at)
            country = replay(root, doc)["leagues"]["bun"]
            self.assertEqual(Decimal(country["available_cash"]), Decimal("2019.996166"))
            account = country["settled_paper_pnl_by_fee_basis"]["research_assumption"]
            self.assertEqual((account["filled_positions"], account["redeemed_positions"]), (1, 1))
            self.assertEqual(Decimal(account["settled_paper_pnl"]), Decimal("19.996166"))
            self.assertEqual(Decimal(account["unredeemed_cost"]), 0)

    def test_native_fee_switch_and_parameters_must_be_complete_and_consistent(self):
        cases = [
            (None, PROFILE), (1, PROFILE), ("true", PROFILE), (False, PROFILE), (True, None),
            (True, {}), (True, PROFILE | {"rate": 0}), (True, PROFILE | {"rate": 1.01}),
            (True, PROFILE | {"exponent": True}), (True, PROFILE | {"exponent": 3}),
            (True, PROFILE | {"takerOnly": 1}), (True, PROFILE | {"rebateRate": -0.1}),
            (True, PROFILE | {"otherCharge": 0.1}),
        ]
        for switch, schedule in cases:
            with self.subTest(switch=switch, schedule=schedule), TemporaryDirectory() as tmp:
                root = Path(tmp)
                doc, (main, aux) = domestic_entry(root, "lal", fee_schedule=PROFILE)
                op = doc["intents"][0]
                at = store.instant(op["decision_at"])
                changed = main["markets"][0]
                changed.update(feesEnabled=switch, feeSchedule=schedule)
                refresh(root, main, aux, at - timedelta(seconds=2))
                with self.assertRaises(ValueError):
                    budget_archive_proof.verify_contract(root, op["condition_id"], at, token=op["token_id"], scheduled=True)
                out = replay(root, doc)
                self.assertEqual(out["leagues"]["lal"]["outcomes"], {"skipped_unverified_entry": 1})
                self.assertEqual(Decimal(out["leagues"]["lal"]["available_cash"]), Decimal(2000))

    def test_typed_contract_and_transient_fee_changes_cannot_be_hidden(self):
        for field, value in (("rate", 0.06), ("exponent", True), ("takerOnly", 1)):
            with self.subTest(field=field), TemporaryDirectory() as tmp:
                root = Path(tmp)
                doc, (main, aux) = domestic_entry(root, "fl1", fee_schedule=PROFILE)
                op = doc["intents"][0]
                bad = deepcopy(doc)
                bad["intents"][0]["fee"]["research_assumption"] = 1
                self.assertEqual(replay(root, bad)["leagues"]["fl1"]["outcomes"], {"skipped_unverified_entry": 1})
                at = store.instant(op["decision_at"])
                changed = deepcopy(main)
                changed["markets"][0]["feeSchedule"][field] = value
                refresh(root, changed, aux, at + timedelta(seconds=1))
                refresh(root, main, aux, at + timedelta(seconds=3))
                out = replay(root, doc)
                self.assertEqual(out["leagues"]["fl1"]["outcomes"], {"skipped_unverified_entry": 1})

    def test_reviewed_profile_precedes_estimate_and_cannot_fall_back_when_evidence_fails(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc, _ = domestic_entry(root, "sea", explicit=True, fee_schedule=PROFILE)
            op = doc["intents"][0]
            at = store.instant(op["decision_at"])
            raw = b"Test-only cash taker rule for one price level."
            docref = fee_registry.Document("https://docs.polymarket.com/test-only/paperbuy", hashlib.sha256(raw).hexdigest(),
                                           store.iso(at - timedelta(days=2)))
            policy = fee_registry.FeePolicy(
                "test-only-paperbuy", "v1", ("moneyline",), docref.published_at, store.iso(at + timedelta(hours=1)),
                (docref,), "0.05", 1, "symmetric", "cash", "eip155:137/erc20:0x" + "a" * 40,
                "cash", "0.00001", "half_up", "per_execution_price_level", "none",
            )
            for policies in ((policy,), (replace(policy, rate="0.07"),), (replace(policy, effective_until=store.iso(at)),),
                             (policy, replace(policy, key="also-matches"))):
                with self.subTest(policies=policies), patch.object(fee_registry, "REVIEWED", policies):
                    out = replay(root, doc)
                    self.assertEqual(out["leagues"]["sea"]["outcomes"], {"skipped_unverified_entry": 1})
                    with self.assertRaises(ValueError):
                        budget_archive_proof.verify_contract(root, op["condition_id"], at, token=op["token_id"], scheduled=True)
            received = store.iso(at - timedelta(hours=1))
            response = market_provider.Reply(raw, received, received, {}, docref.url)
            for expires_at_execution in (False, True):
                registered = replace(policy, effective_until=store.iso(at + timedelta(seconds=5))) if expires_at_execution else policy
                with (self.subTest(expires_at_execution=expires_at_execution),
                      patch.object(fee_registry, "REVIEWED", (registered,)),
                      patch.object(fee_registry, "_read_document", return_value=response)):
                    fee_registry.capture(big5_market.research_root(root), registered.key)
                    observed = command(root, "verify-contract", op["condition_id"], "--as-of", op["decision_at"],
                                       "--token-id", op["token_id"], "--entry-basis", BASIS)
                    self.assertEqual(observed["fee_basis"], "authoritative")
                    buy = deepcopy(doc)
                    buy["intents"][0].update(observed["intent_terms"])
                    buy["intents"].append(dict(buy["intents"][0], shares="97", id="smaller", rank=2))
                    country = replay(root, buy)["leagues"]["sea"]
                    if expires_at_execution:
                        self.assertEqual(country["outcomes"], {"skipped_unverified_entry": 2})
                    else:
                        self.assertEqual(country["outcomes"], {"skipped_intent_limit": 1, "filled": 1})
                        account = country["settled_paper_pnl_by_fee_basis"]["authoritative"]
                        self.assertEqual(Decimal(account["buy_cash"]), Decimal("49.7125"))
                        self.assertEqual(Decimal(account["fee_shares"]), 0)
                        self.assertEqual(Decimal(account["fee_cash_equivalent"]), Decimal("1.2125"))
