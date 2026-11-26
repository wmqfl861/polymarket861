import copy
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from polymarket861 import five_league_budget as risk, store


def one_fixture():
    full = risk.stress_scenario()
    op = next(row for row in full["intents"] if row["cohort"] == "prior_round" and row["id"].endswith("-1"))
    books = [r for r in full["books"] if r["token_id"] == op["token_id"]]
    event = next(r for r in full["redemptions"] if r["token_id"] == op["token_id"])
    sha = event["original_sha256"]
    return {"schema": risk.SCHEMA, "evidence_mode": "synthetic", "books": books,
            "intents": [op], "originals": {sha: full["originals"][sha]}, "redemptions": [event]}


class CapitalReplay(unittest.TestCase):
    def test_48_matchweek_measures_carry_in_and_locked_capital(self):
        report = risk.stress_report()
        self.assertEqual((report["target_fixtures"], report["carry_in_fixtures"]), (48, 24))
        before, after = report["weekend_before_redemption"], report["after_delayed_receipts"]
        self.assertEqual(Decimal(before["total_unredeemed_cost"]), 9950)
        self.assertEqual(Decimal(after["total_unredeemed_cost"]), 250)
        self.assertEqual(Decimal(after["total_available_cash"]), 9750)
        self.assertEqual(after["redemption_events"]["venue_still_pending"], 5)
        for code, b in before["leagues"].items():
            self.assertGreater(b["cohorts"]["stress_round"]["outcomes"]["skipped_depth"], 0)
            self.assertLessEqual(Decimal(b["max_fixture_loss_upper_bound"]), 150)
            self.assertLessEqual(Decimal(b["peak_unredeemed_cost"]), 2000)
            self.assertGreaterEqual(Decimal(b["minimum_cash"]), 0)
            self.assertEqual(Decimal(after["leagues"][code]["unredeemed_cost"]), 50)
        self.assertGreater(before["leagues"]["epl"]["outcomes"]["skipped_cash"], 0)
        self.assertGreater(Decimal(before["leagues"]["fl1"]["available_cash"]), 0)

    def test_deterministic_priority_and_consumption_of_the_same_observed_depth(self):
        doc = one_fixture()
        op = doc["intents"][0]
        clone = {**op, "rank": op["rank"] + 1, "id": "clone"}
        # The same exchange observation can have a second local identifier.
        second = {**doc["books"][1], "id": "alias"}
        doc["books"].append(second)
        clone["execution_book"] = "alias"
        doc["intents"] = [clone, op, copy.deepcopy(op)]
        event = doc["redemptions"][0]
        doc["redemptions"].extend([copy.deepcopy(event), {**event, "id": "repeat-terminal"}])
        result = risk.replay(doc, details=True)
        self.assertEqual([r["outcome"] for r in result["decisions"]], ["filled", "skipped_depth"])
        self.assertEqual(result["redemption_events"]["positions_redeemed"], 1)
        self.assertEqual(result["redemption_events"]["duplicate_terminal"], 1)
        doc["intents"].reverse()
        doc["redemptions"].reverse()
        doc["books"].reverse()
        self.assertEqual(risk.replay(doc, details=True), result)
        doc["intents"].append({**op, "shares": "11"})
        with self.assertRaisesRegex(ValueError, "conflicting retry"):
            risk.replay(doc)

    def test_no_original_no_cash_and_wrong_token_or_digest_is_rejected(self):
        doc = one_fixture()
        redemption = doc["redemptions"][0]
        sha = redemption["original_sha256"]
        original = doc["originals"].pop(sha)
        redemption["payout"] = "1"
        report = risk.replay(doc)
        self.assertEqual(Decimal(report["leagues"]["epl"]["available_cash"]), 1950)
        self.assertEqual(report["redemption_events"], {"missing_original": 1})
        doc["originals"][sha] = dict(original, received_at="2040-01-01T00:00:00Z")
        self.assertEqual(risk.replay(doc)["redemption_events"], {"original_not_yet_received": 1})
        doc["originals"][sha] = dict(original, body=original["body"] + " ")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            risk.replay(doc)
        redemption["original_sha256"] = hashlib.sha256(b'{"data":[]}').hexdigest()
        doc["originals"][redemption["original_sha256"]] = dict(original, body='{"data":[]}')
        with self.assertRaisesRegex(ValueError, "mismatched redemption"):
            risk.replay(doc)

    def test_bare_local_file_does_not_prove_a_fill_or_release_cash(self):
        doc = one_fixture()
        event = doc["redemptions"][0]
        original = doc["originals"][event["original_sha256"]]
        raw = original.pop("body").encode()
        url = f"{risk.EXCHANGE_URL}?token_id={event['token_id']}"
        doc["evidence_mode"] = "local_receipts"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for archive in (False, True):
                if archive:
                    store.archive_bytes(root, "polymarket.com", raw, observed_at=original["received_at"],
                                        metadata={"url": url})
                report = risk.replay(doc, data_dir=root)
                self.assertEqual(report["redemption_events"], {"missing_verified_entry": 1})
                self.assertEqual(report["leagues"]["epl"]["outcomes"], {"skipped_unverified_entry":1})
                self.assertEqual(Decimal(report["leagues"]["epl"]["available_cash"]), 2000)
            self.assertEqual(risk.replay(doc)["leagues"]["epl"]["outcomes"], {"skipped_unverified_entry":1})

    def test_timing_fee_and_scope_cannot_bypass_limits(self):
        doc = one_fixture()
        op = doc["intents"][0]
        doc["books"][1]["received_at"] = op["kickoff"]
        with self.assertRaisesRegex(ValueError, "out of order"):
            risk.replay(doc)
        doc = one_fixture()
        op = doc["intents"][0]
        op["fee"]["rate"] = "0.08"
        self.assertEqual(risk.replay(doc)["leagues"]["epl"]["outcomes"], {"skipped_intent_limit": 1})
        op["fee"]["asset"] = "shares"
        report = risk.replay(doc)["leagues"]["epl"]
        self.assertEqual(report["outcomes"], {"filled": 1})
        self.assertEqual(Decimal(report["buy_cash_including_fees"]), 49)
        self.assertLess(Decimal(report["cash_from_redemptions"]), 50)
        doc["redemptions"][0]["fixture_id"] = op["fixture_id"].replace("epl/", "lal/")
        with self.assertRaisesRegex(ValueError, "outside"):
            risk.replay(doc)

    def test_conflicting_terminal_restatement_cannot_credit_twice(self):
        doc = one_fixture()
        event = doc["redemptions"][0]
        record = doc["originals"][event["original_sha256"]]
        raw = json.loads(record["body"])
        raw["data"][0]["final_price"] = "1"
        raw["data"][1]["final_price"] = "0"
        body = json.dumps(raw)
        sha = hashlib.sha256(body.encode()).hexdigest()
        doc["originals"][sha] = dict(record, body=body)
        doc["redemptions"].append({**event, "original_sha256": sha, "id": "different-payout"})
        with self.assertRaisesRegex(ValueError, "conflicting terminal"):
            risk.replay(doc)
