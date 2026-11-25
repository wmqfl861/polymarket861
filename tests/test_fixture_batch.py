import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import test_opportunity as fixture
from polymarket861 import dashboard, ledger, opportunity, store
from polymarket861 import market_archive as archive


class FixtureWriter(unittest.TestCase):
    def prepare_formal(self):
        case = fixture.Opportunities()
        case.setUp()
        self.addCleanup(case.doCleanups)
        cutoff = fixture.CUT
        seal = case.seal()
        fair = Decimal(str(seal["probabilities"]["total_over_2_5"]))
        when = cutoff - timedelta(seconds=7)
        price = (fair - Decimal(".04")).quantize(Decimal(".01"), rounding=ROUND_FLOOR)
        for token, bid in (("711", price), ("722", Decimal("0.9"))):
            case.book(when, bid, token=token)
        with fixture.at(when):
            ledger.open_account(case.root, "main", "formal")
        return case, price

    def test_failed_batch_retries_from_current_bytes_then_executes_once(self):
        case, price = self.prepare_formal()
        clock = fixture.CUT - timedelta(seconds=5)
        with ledger.connection(case.root) as db:
            sha = db.execute("SELECT sha FROM ma_books WHERE token='711'").fetchone()[0]
        path = store.blob_path(case.root, sha)
        original = path.read_bytes()

        def fail_flush(db, account):
            self.assertEqual(db.execute("SELECT count(*) FROM paper_intents").fetchone()[0], 2)
            raise RuntimeError("interrupted before commit")

        with fixture.at(clock), patch.object(ledger, "_publish_compact", fail_flush):
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                opportunity.process(case.root, fixture.SLUG)
        with ledger.connection(case.root) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM paper_intents").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM paper_depth").fetchone()[0], 0)
        self.assertEqual(opportunity.history(case.root, fixture.SLUG)["events"], [])

        path.write_bytes(b"changed between transactions")
        with fixture.at(clock):
            denied = opportunity.process(case.root, fixture.SLUG)
        self.assertFalse(any(x["state"] == "frozen" for x in denied["decisions"]), denied)
        self.assertTrue(any(x.get("token_id") == "711" for x in denied["gaps"]))
        path.write_bytes(original)
        with fixture.at(clock), patch.object(store, "put_report", wraps=store.put_report) as reports:
            retry = opportunity.process(case.root, fixture.SLUG)
            self.assertEqual({r["state"] for r in retry["decisions"]}, {"skipped", "frozen"})
            opportunity.process(case.root, fixture.SLUG)
            self.assertEqual(sum(c.args[1] == "ledger" for c in reports.call_args_list), 1)
        case.book(fixture.CUT + timedelta(seconds=7), price)
        with fixture.at(fixture.CUT + timedelta(seconds=8)), patch.object(ledger, "_publish_compact", side_effect=RuntimeError("no write")):
            with self.assertRaisesRegex(RuntimeError, "no write"):
                opportunity.process(case.root, fixture.SLUG)
        with ledger.connection(case.root) as db:
            self.assertEqual(db.execute("SELECT cash=initial FROM paper_accounts").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM paper_depth").fetchone()[0], 0)
        with fixture.at(fixture.CUT + timedelta(seconds=8)):
            opportunity.process(case.root, fixture.SLUG)
            opportunity.process(case.root, fixture.SLUG)
        full = ledger.ledger_report(case.root, "main")
        self.assertEqual((len(full["intents"]), full["fills"]), (2, 1))
        self.assertEqual(Decimal(full["cash"]) + Decimal(full["total_spent"]), Decimal(full["initial_cash"]))
        compact = next(r["payload"] for r in store.list_reports(case.root) if r["kind"] == "ledger")
        self.assertNotIn("intents", compact)
        self.assertEqual(compact["cash"], full["cash"])
        view = next(r["payload"] for r in dashboard._report_rows(case.root) if r["kind"] == "ledger")
        self.assertEqual(view, full)

    def test_clock_crosses_after_summary_rolls_back_even_skips_and_log(self):
        case, _ = self.prepare_formal()
        cutoff = fixture.CUT
        current = cutoff - timedelta(seconds=5)
        after = cutoff + timedelta(microseconds=1)
        actual_publish = ledger._publish_compact

        def crossed(db, account):
            nonlocal current
            actual_publish(db, account)
            current = after

        with patch.object(store, "now_utc", side_effect=lambda: store.iso(current)), patch.object(ledger, "_publish_compact", crossed):
            report = opportunity.process(case.root, fixture.SLUG)
        self.assertEqual(report["decisions"], [])
        self.assertIn("deadline", report["gaps"][0]["reason"])
        self.assertEqual(report["observed_at"], store.iso(after))
        with ledger.connection(case.root) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM paper_intents").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM reports WHERE kind='ledger'").fetchone()[0], 0)
        self.assertEqual([r["stage"] for r in opportunity.history(case.root, fixture.SLUG)["events"]], ["gap"])
        with fixture.at(after):
            self.assertEqual(opportunity.process(case.root, fixture.SLUG)["decisions"], [])

    def test_changed_native_bytes_before_commit_invalidate_all_directions(self):
        case, _ = self.prepare_formal()
        with ledger.connection(case.root) as db:
            sha = db.execute("SELECT sha FROM ma_books WHERE token='711'").fetchone()[0]
        path = store.blob_path(case.root, sha)
        correct = path.read_bytes()
        original_publish = ledger._publish_compact

        def damage_after_verification(db, account):
            original_publish(db, account)
            path.write_bytes(b"changed after validation")

        with fixture.at(fixture.CUT), patch.object(ledger, "_publish_compact", damage_after_verification):
            lost = opportunity.process(case.root, fixture.SLUG)
        self.assertEqual(lost["decisions"], [])
        self.assertIn(sha, lost["gaps"][0]["reason"])
        with ledger.connection(case.root) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM paper_intents").fetchone()[0], 0)
        path.write_bytes(correct)
        with fixture.at(fixture.CUT):
            self.assertEqual(len(opportunity.process(case.root, fixture.SLUG)["decisions"]), 2)

    def test_real_sqlite_concurrent_fixtures_share_capital_and_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger.open_account(temp, "cash", "demo", initial_cash="6")
            opportunities = []
            for i in range(5):
                op = ledger.example()
                intent = op["intent"]
                ident = f"order-{i}"
                match = f"fixture-{i}"
                intent.update(intent_id=ident, match_id=match, budget="6")
                intent["contract"]["match_id"] = match
                for item in (intent["contract"], op["first_book"], op["second_book"]):
                    item.update(condition_id=f"condition-{i}", token_id=f"token-{i}")
                opportunities.append(op)
            barrier = Barrier(len(opportunities))

            def run(op):
                barrier.wait(timeout=10)
                with ledger.fixture_batch(temp):
                    intent = op["intent"]
                    ledger.freeze(temp, "cash", intent, op["first_book"])
                    return ledger.evaluate(temp, "cash", intent["intent_id"], op["second_book"])

            with ThreadPoolExecutor(max_workers=5) as pool:
                first = list(pool.map(run, opportunities))
                again = list(pool.map(run, opportunities))
            self.assertEqual(first, again)
            full = ledger.ledger_details(temp, "cash")
            self.assertEqual(len(full["intents"]), 5)
            self.assertGreater(full["fills"], 0)
            self.assertLess(full["fills"], 5)
            self.assertGreaterEqual(Decimal(full["cash"]), 0)
            self.assertEqual(Decimal(full["cash"]) + Decimal(full["total_spent"]), Decimal("6"))

    def test_old_schema_upgrades_once_and_reopens_without_ddl(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with closing(sqlite3.connect(root / "runtime.sqlite3")) as raw:
                raw.executescript("CREATE TABLE ma_contracts(fixture TEXT NOT NULL,event_slug TEXT NOT NULL,market_slug TEXT NOT NULL,received_at TEXT NOT NULL,sha TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(event_slug,market_slug,received_at,sha));")
                raw.execute("INSERT INTO ma_contracts VALUES('f','e','m','t','s','malformed old row')")
                raw.commit()
            with closing(archive.database(root)) as db:
                self.assertEqual(db.execute("SELECT payload FROM ma_contracts").fetchone()[0], "malformed old row")
                self.assertIn("ma_contracts_condition_time", str([tuple(r) for r in db.execute("PRAGMA index_list(ma_contracts)")]))
            with ledger.fixture_batch(root) as db:
                plans = db.execute("EXPLAIN QUERY PLAN SELECT id FROM paper_intents WHERE account='main' AND status='frozen' ORDER BY cutoff,priority,fixture,condition_id,token,id").fetchall()
                self.assertIn("paper_intents_pending", str([tuple(r) for r in plans]))
            statements = []
            real_connect = sqlite3.connect

            def traced(*args, **kwargs):
                db = real_connect(*args, **kwargs)
                db.set_trace_callback(statements.append)
                return db

            with patch.object(sqlite3, "connect", side_effect=traced):
                with ledger.fixture_batch(root):
                    pass
            self.assertFalse(any(s.lstrip().startswith("CREATE ") for s in statements), statements)
