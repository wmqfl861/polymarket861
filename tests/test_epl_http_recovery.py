import importlib.util
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/paper-automation/epl_http_recovery.py"
spec = importlib.util.spec_from_file_location("epl_http_recovery", SCRIPT)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class EplHttpRecovery(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "runtime.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE ma_requests(id INTEGER PRIMARY KEY, operation, received_at, status, detail);
            CREATE TABLE ma_cooldown(endpoint, not_before);
        """)
        self.now = datetime.now(timezone.utc)

    def add(self, op, minutes, status="error", message="public HTTP total deadline elapsed"):
        with self.db:
            self.db.execute("INSERT INTO ma_requests VALUES(NULL,?,?,?,?)",
                            (op, recovery.stamp(self.now - timedelta(minutes=minutes)), status, message))

    def test_both_hosts_must_fail_for_a_sustained_and_current_period(self):
        self.add("catalog", 11)
        self.assertFalse(recovery.stalled(self.db, self.now))
        self.add("books", 6)
        self.assertFalse(recovery.stalled(self.db, self.now))
        self.add("book", 1, message="capture request window has already elapsed")
        self.assertFalse(recovery.stalled(self.db, self.now))
        self.add("books", 1)
        self.assertTrue(recovery.stalled(self.db, self.now))
        self.add("book", 0, status="ok", message=None)
        self.assertFalse(recovery.stalled(self.db, self.now))

    def test_upstream_http_response_or_cooldown_prevents_restart(self):
        self.add("catalog", 12)
        self.add("books", 1)
        with self.db:
            self.db.execute("INSERT INTO ma_cooldown VALUES('clob',?)",
                            (recovery.stamp(self.now + timedelta(minutes=1)),))
        self.assertFalse(recovery.stalled(self.db, self.now))
        self.db.execute("DELETE FROM ma_cooldown")
        self.add("books", 1, message="HTTP Error 429: Too Many Requests")
        self.assertFalse(recovery.stalled(self.db, self.now))

    def test_data_api_history_does_not_count_as_a_clob_failure(self):
        self.add("catalog", 12)
        self.add("prices", 2)
        self.add("trades", 1)
        self.assertFalse(recovery.stalled(self.db, self.now))

    def test_late_writes_cannot_hide_a_recent_receipt_before_the_scan(self):
        self.add("book", 2, status="ok", message=None)
        old = recovery.stamp(self.now - timedelta(hours=1))
        self.db.executemany("INSERT INTO ma_requests VALUES(NULL,'book',?,'error',NULL)",
                            ((old,) for _ in range(8192)))
        self.add("catalog", 12)
        self.add("books", 1)
        self.assertFalse(recovery.stalled(self.db, self.now))

    def test_fresh_probe_and_same_process_are_required(self):
        self.add("event", 12)
        self.add("books", 1)
        with patch.object(recovery, "active_process", return_value=123):
            with patch.object(recovery, "upstream_reachable", return_value=False):
                self.assertEqual(recovery.main(self.path), 1)
            with patch.object(recovery, "upstream_reachable", return_value=True):
                self.assertEqual(recovery.main(self.path), 0)
        with patch.object(recovery, "active_process", side_effect=[123, 124]):
            with patch.object(recovery, "upstream_reachable", return_value=True):
                self.assertEqual(recovery.main(self.path), 1)

    def test_a_success_arriving_during_probe_prevents_restart(self):
        self.add("event", 12)
        self.add("books", 1)
        def recovered(_db):
            self.add("books", 0, status="ok", message=None)
            return True
        with patch.object(recovery, "active_process", return_value=123):
            with patch.object(recovery, "upstream_reachable", side_effect=recovered):
                self.assertEqual(recovery.main(self.path), 1)
