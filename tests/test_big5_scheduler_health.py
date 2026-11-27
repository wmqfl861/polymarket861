"""Health and exit codes belong to an advancing service, including independent league failures."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stdout
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from polymarket861 import big5_scheduler as scheduler, cli, market_watch_health as watch, store


class ForecastHealthTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = store.instant(store.now_utc())

    def command(self, action="watchdog", code="bun"):
        out = io.StringIO()
        with redirect_stdout(out):
            status = cli.main(["--data-dir", str(self.root), "big5-scheduler", action, "--league", code])
        return status, json.loads(out.getvalue())["leagues"][code]

    def complete(self, code="bun", state="ok", **details):
        with closing(scheduler._database(self.root, code)) as db:
            scheduler._record_cycle(db, store.iso(self.now), state, details)

    def monitor(self, code="bun"):
        return watch.monitor(scheduler._directory(self.root, code), [code], None, None, continuous=True,
                             discover=False, notify_systemd=False)

    def test_legacy_one_shot_and_two_minute_boundary_are_readonly(self):
        result, row = self.command("status")
        self.assertEqual((result, row["state"]), (1, "not_started"))
        self.assertFalse(list(self.root.iterdir()))
        # A deployed fiveauto.1 database has no per-process monitor or checkpoint tables.
        directory = scheduler._directory(self.root, "bun")
        directory.mkdir(parents=True)
        with closing(sqlite3.connect(directory / "runtime.sqlite3")) as db, db:
            db.executescript(scheduler.SCHEMA)
            db.execute("INSERT INTO domestic_run VALUES(1,?,?,'ok','{}')", (store.iso(self.now),) * 2)
        with patch.object(store, "now_utc", side_effect=lambda: store.iso(self.now)):
            self.assertEqual((self.command()[0], self.command()[1]["state"]), (1, "unmonitored"))
            with self.monitor() as heartbeat:
                heartbeat(1, None)
                self.assertEqual(self.command()[0], 0)
                self.now += timedelta(seconds=120)
                self.assertEqual(self.command()[0], 0)
                self.now += timedelta(microseconds=1)
                for action in ("status", "watchdog"):
                    result, row = self.command(action)
                    self.assertEqual((result, row["state"], row["last_cycle_age_seconds"]), (1, "stale", 120.000001))
                self.now -= timedelta(seconds=200)
                self.assertEqual(self.command()[1]["state"], "clock_conflict")
        with closing(sqlite3.connect(directory / "runtime.sqlite3")) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='domestic_cycle_checkpoint'").fetchone())

    def test_process_stop_peer_selection_and_last_success(self):
        with patch.object(store, "now_utc", side_effect=lambda: store.iso(self.now)), self.monitor() as first:
            self.complete(eligible_upcoming=4)
            first(1, None)
            with self.monitor() as second:
                second(1, None)
                self.assertEqual(self.command()[0], 0)
            self.assertEqual(self.command()[0], 0)
            self.now += timedelta(seconds=1)
            self.complete(state="retry", error="missing input")
            result, row = self.command()
            self.assertEqual((result, row["state"], row["last_cycle"]["error"]), (1, "retry", "missing input"))
            self.assertEqual(row["consecutive_incomplete_cycles"], 1)
            self.assertEqual(row["last_successful_cycle"]["eligible_upcoming"], 4)
            self.now += timedelta(seconds=1)
            self.complete()
            self.assertEqual(self.command()[1]["consecutive_incomplete_cycles"], 0)
        with patch.object(store, "now_utc", return_value=store.iso(self.now)):
            self.assertEqual((self.command()[0], self.command()[1]["service"]["state"]), (1, "stopped"))

    @unittest.skipUnless(watch._pid_domain(), "Linux PID start ticks identify an exited process immediately")
    def test_killed_foreground_daemon_is_detected_before_heartbeat_expires(self):
        script = """
import sys
from pathlib import Path
from polymarket861 import big5_scheduler as scheduler, market_watch_health as watch, store
root = Path(sys.argv[1])
with scheduler._database(root, 'bun') as db:
    scheduler._record_cycle(db, store.now_utc(), 'ok', {})
with watch.monitor(scheduler._directory(root, 'bun'), ['bun'], None, None, continuous=True, discover=False,
                   notify_systemd=False) as heartbeat:
    heartbeat(1, None)
    print('ready', flush=True)
    sys.stdin.read()
"""
        env = dict(os.environ, PYTHONPATH=str(Path(scheduler.__file__).resolve().parents[1]))
        with subprocess.Popen([sys.executable, "-c", script, str(self.root)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, env=env, text=True) as process:
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                self.assertEqual(self.command()[0], 0)
            finally:
                process.kill()
                process.wait(timeout=10)
        result, row = self.command()
        self.assertEqual((result, row["state"]), (1, "exited"))
        self.assertLess(row["last_cycle_age_seconds"], scheduler.HEALTH_MAX_AGE)

    def test_one_hung_league_does_not_hold_its_neighbour_or_feed_systemd(self):
        release = threading.Event()
        counts = {"bun": 0, "sea": 0}
        notifications = []
        class Done(BaseException):
            pass

        def cycle(root, code, history, market):
            if code == "sea":
                release.wait(8)
            counts[code] += 1
            self.complete(code)
            return {"state": "ok"}

        def advance(_seconds):
            if counts["bun"] >= 2:
                try:
                    self.assertEqual(counts["sea"], 0)
                    self.assertEqual(self.command()[0], 0)
                    self.assertEqual(self.command(code="sea")[0], 1)
                    self.assertEqual(notifications, [])
                finally:
                    release.set()
                raise Done

        with patch.object(scheduler, "_league_tick", side_effect=cycle), \
             patch.object(scheduler, "time", SimpleNamespace(monotonic=lambda: 0, sleep=advance)), \
             patch.object(watch, "_notify", side_effect=notifications.append), self.assertRaises(Done):
            scheduler.run(self.root, leagues=["bun", "sea"], market_state_dir=self.root / "market",
                          repeat=True, interval=1)
        self.assertEqual(notifications, ["STOPPING=1"])

    def test_worker_failure_outside_any_fixture_is_durable_and_recovers(self):
        with self.monitor() as heartbeat, patch.object(scheduler, "_poll", side_effect=RuntimeError("bad batch")):
            self.complete()
            heartbeat(1, None)
            with self.assertLogs(scheduler.__name__, level="ERROR"):
                scheduler._poll_batch(self.root, "bun")
            result, row = self.command()
            self.assertEqual((result, row["state"], row["news_poll_batch"]["error"]),
                             (1, "partial", "RuntimeError: bad batch"))
            with patch.object(scheduler, "_poll", return_value=0):
                self.assertEqual(scheduler._poll_batch(self.root, "bun"), (0, None))
            self.assertEqual(self.command()[0], 0)

    def test_one_corrupt_database_does_not_obscure_other_league_health(self):
        with self.monitor() as heartbeat:
            self.complete()
            heartbeat(1, None)
            path = scheduler._directory(self.root, "sea")
            path.mkdir(parents=True)
            (path / "runtime.sqlite3").write_bytes(b"invalid sqlite file")
            report = scheduler.status(self.root, leagues=["sea", "bun"], health_only=True)
            self.assertFalse(report["healthy"])
            self.assertEqual(report["leagues"]["sea"]["state"], "unavailable")
            self.assertIn("DatabaseError", report["leagues"]["sea"]["error"])
            self.assertTrue(report["leagues"]["bun"]["healthy"])
