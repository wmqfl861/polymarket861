"""Pre-verifier paper accounts remain auditable after upgrade and cannot fund new decisions."""

import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import test_archive_proof as archive_fixture
from polymarket861 import cli, ledger, qualification


class HistoricalAccountUpgrade(unittest.TestCase):
    def setUp(self):
        sample = archive_fixture.HistoricalArchiveProofTests()
        sample.setUp()
        self.addCleanup(sample.doCleanups)
        self.op, self.root = sample.op, sample.root

    def legacy(self, name, status):
        ledger.open_account(self.root, name, "historical")
        with ledger.connection(self.root, True) as db:
            source = db.execute(
                "SELECT * FROM paper_intents WHERE account=?",
                (self.op["paper_account"],),
            ).fetchone()
            original, result = (
                json.loads(source["original"]),
                json.loads(source["result"]),
            )
            original["intent"]["priority"] = 9
            result.update(status=status, decision_sha256=ledger.digest(original))
            if status == "filled":
                db.execute(
                    "UPDATE paper_accounts SET cash=? WHERE account=?",
                    (result["cash_after_fill"], name),
                )
            else:
                result["spent"] = result["proceeds"] = result["pnl"] = None
            db.execute(
                "INSERT INTO paper_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    name,
                    source["id"],
                    source["fixture"],
                    source["condition_id"],
                    source["token"],
                    source["cutoff"],
                    9,
                    status,
                    ledger.digest(original),
                    ledger.packed(original),
                    ledger.packed(result),
                ),
            )
        return original

    def test_legacy_cash_and_orders_are_readable_and_all_write_paths_reject(self):
        op = self.op
        held = self.legacy("old-filled", "filled")
        self.legacy("old-pending", "frozen")
        before = ledger.ledger_report(self.root, "old-filled")
        self.assertTrue(before["legacy_read_only"])
        self.assertIsNone(before["replay_verifier"])
        self.assertEqual(before["fills"], 1)
        self.assertLess(
            ledger.number(before["cash"]), ledger.number(before["initial_cash"])
        )
        ident = held["intent"]["intent_id"]
        token = held["intent"]["contract"]
        event = {
            "condition_id": token["condition_id"],
            "token_id": token["token_id"],
            "resolution_id": "historical-close",
            "source": "test",
            "resolved_at": op["actual_kickoff"],
            "scenario": "normal",
            "status": "resolved",
            "payout": "1",
        }
        for act in (
            lambda: ledger.freeze(
                self.root, "old-pending", held["intent"], held["book"]
            ),
            lambda: ledger.freeze(
                self.root, "old-pending", held["intent"], held["book"], replay_op=op
            ),
            lambda: ledger.evaluate(self.root, "old-pending", ident, op["second_book"]),
            lambda: ledger.recover(self.root, "old-pending"),
            lambda: ledger.record_close(self.root, "old-filled", ident),
            lambda: ledger.settle(self.root, "old-filled", event),
        ):
            with self.assertRaisesRegex(ValueError, "read-only"):
                act()
        path = Path(self.root) / "legacy-request.json"
        path.write_text(json.dumps({"book": op["second_book"]}))
        err = StringIO()
        with redirect_stderr(err), redirect_stdout(StringIO()):
            code = cli.main(
                [
                    "--data-dir",
                    str(self.root),
                    "ledger-evaluate",
                    "--account",
                    "old-pending",
                    "--intent-id",
                    ident,
                    "--input",
                    str(path),
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("read-only", err.getvalue())
        self.assertEqual(ledger.ledger_report(self.root, "old-filled"), before)
        self.assertEqual(
            ledger.ledger_report(self.root, "old-pending")["intents"][0]["status"],
            "frozen",
        )
        self.assertFalse(
            ledger.audit_risk(self.root, "B1", account="old-filled")["passed"]
        )
        self.assertFalse(
            qualification._current_market_study(
                self.root, {"account": "old-filled", "counts": {"A": 1}}
            )
        )
        with self.assertRaisesRegex(ValueError, "outside this replay manifest"):
            ledger.replay(self.root, [op], account="old-pending")
        current = ledger.replay(self.root, [op])
        self.assertEqual(current["counts"]["filled"], 1)
        self.assertFalse(
            ledger.ledger_report(self.root, current["account"])["legacy_read_only"]
        )
        self.assertTrue(qualification._current_market_study(self.root, current))

    def test_partial_market_replay_manifest_cannot_reserve_cash_by_direct_freeze(self):
        incomplete = dict(self.op)
        incomplete.pop("second_book")
        account = ledger.replay(self.root, [incomplete])["account"]
        with self.assertRaisesRegex(ValueError, "current A"):
            ledger.freeze(
                self.root, account, incomplete["intent"], incomplete["first_book"], replay_op=incomplete
            )
        report = ledger.ledger_report(self.root, account)
        self.assertEqual(report["intents"], [])
        self.assertEqual(ledger.number(report["reserved_cash"]), 0)

    def test_current_account_rechecks_source_before_direct_execution_retries_and_marks(
        self,
    ):
        r = ledger.replay(self.root, [self.op])
        a, op = r["account"], self.op
        ident = op["intent"]["intent_id"]
        self.assertEqual(
            ledger.evaluate(self.root, a, ident, op["second_book"])["status"], "filled"
        )
        with self.assertRaisesRegex(ValueError, "execution book"):
            ledger.evaluate(self.root, a, ident, op["first_book"])
        self.assertIsNotNone(ledger.record_close(self.root, a, ident)["clv"])
        self.assertEqual(ledger.recover(self.root, a)["recovered"], [])
        with ledger.connection(self.root, True) as db:
            db.execute(
                "DELETE FROM forecast_seals WHERE digest=?", (op["forecast_digest"],)
            )
        with self.assertRaisesRegex(ValueError, "current A"):
            ledger.evaluate(self.root, a, ident, op["second_book"])
        with self.assertRaisesRegex(ValueError, "current A"):
            ledger.record_close(self.root, a, ident)
