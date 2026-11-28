import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket861 import season_backfill as back, store
from test_season import NOW, ingest, native


class BackfillSeedConcurrency(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.kick = NOW - timedelta(days=2)
        self.slug = "epl-aa-bb-2026-11-23"
        ingest(self.root, native(self.slug, self.kick), NOW)

    def test_market_writer_can_finish_while_seed_prepares_and_its_receipts_survive(self):
        with closing(back._db(self.root)) as db, db:
            key = store.digest([2026, "trades", "1", 0, 0])
            db.execute(
                "INSERT INTO season_history_jobs VALUES(?,2026,?,'trades','1',0,0,0,'pending','','',NULL)",
                (key, self.slug),
            )
        normalize = back.provider.normalized_markets

        def other_writer(event, *args, **kwargs):
            if event["slug"] == self.slug:
                with closing(sqlite3.connect(self.root / "runtime.sqlite3", timeout=0)) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        "UPDATE season_history_jobs SET state='retry',grain=2,next_at='later',detail='receipt' WHERE key=?",
                        (key,),
                    )
                ingest(self.root, native(self.slug, self.kick, idx=2, aux=True), NOW)
            return normalize(event, *args, **kwargs)

        with patch.object(back.provider, "normalized_markets", side_effect=other_writer):
            back._seed(self.root, 2026, NOW)
        back._seed(self.root, 2026, NOW)
        with closing(back._db(self.root)) as db:
            self.assertEqual(tuple(db.execute(
                "SELECT rowid,state,grain,next_at,detail FROM season_history_jobs WHERE key=?", (key,),
            ).fetchone()), (1, "retry", 2, "later", "receipt"))
            self.assertFalse(db.execute("SELECT 1 FROM season_history_jobs WHERE kind='event'").fetchone())
            self.assertEqual([r[0] for r in db.execute(
                "SELECT selector FROM season_history_jobs WHERE kind='prices' ORDER BY rowid"
            )], ["10", "11", "20", "21"])

    def test_late_seeder_can_add_historical_jobs_without_rewinding_a_newer_marker(self):
        later = NOW + timedelta(minutes=1)
        ingest(self.root, native(self.slug, self.kick, idx=2), later)
        back._seed(self.root, 2026, later)
        back._seed(self.root, 2026, NOW)
        with closing(back._db(self.root)) as db:
            self.assertEqual(db.execute("SELECT source_sha FROM season_history_seed").fetchone()[0],
                             db.execute("SELECT sha FROM ma_events ORDER BY received_at DESC LIMIT 1").fetchone()[0])
            self.assertEqual([r[0] for r in db.execute(
                "SELECT selector FROM season_history_jobs WHERE kind='trades' ORDER BY rowid"
            )], ["2", "1"])
            self.assertEqual(db.execute("SELECT count(*) FROM season_history_jobs WHERE kind='event'").fetchone()[0], 1)

    def test_seed_marker_failure_rolls_back_all_new_jobs_and_retry_can_seed(self):
        with closing(back._db(self.root)) as db:
            db.execute("CREATE TRIGGER reject_seed BEFORE INSERT ON season_history_seed "
                       "BEGIN SELECT RAISE(ABORT,'seed marker failed'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "seed marker failed"):
            back._seed(self.root, 2026, NOW)
        with closing(back._db(self.root)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM season_history_jobs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM season_history_seed").fetchone()[0], 0)
            db.execute("DROP TRIGGER reject_seed")
        back._seed(self.root, 2026, NOW)
        back._seed(self.root, 2026, NOW)
        with closing(back._db(self.root)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM season_history_jobs").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT count(*) FROM season_history_seed").fetchone()[0], 1)
