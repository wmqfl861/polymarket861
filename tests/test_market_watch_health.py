"""A live scheduler tick, restart, and stale scope are visible without writing the evidence DB."""

import contextlib
import errno
import io
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket861 import big5_market, cli, market_watch_health as health


class WatchHealthTests(unittest.TestCase):
    start = "2026-11-27T00:00:00Z"
    end = "2026-12-01T00:00:00Z"

    def test_rolling_cli_watchdog_checks_the_rolling_service_scope(self):
        with (
            health.monitor(
                self.root, ["bun", "fl1", "lal", "sea"], None, None,
                continuous=True, discover=True, rolling=True,
            ) as tick,
            patch.object(health.store, "now_utc", return_value="2026-11-27T12:00:00Z"),
        ):
            tick(1, None)
            code, report = self.watchdog("--rolling")
        self.assertEqual(report["scope"]["start"], "rolling")
        self.assertEqual(report["scope"]["end"], "rolling")
        self.assertTrue(report["rolling"])
        self.assertTrue(report["scheduler_alive"])
        self.assertFalse(report["healthy"])


    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.root = big5_market.research_root(self.directory)
        self.scope = big5_market._args(None, self.start, self.end)
        self.environment = patch.dict(os.environ, {"NOTIFY_SOCKET": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def watchdog(self, *args):
        output = io.StringIO()
        if "--rolling" in args:
            scope_args = ["--rolling"]
        else:
            scope_args = ["--start", self.start, "--end", self.end, *args]
        with contextlib.redirect_stdout(output):
            code = cli.main(
                [
                    "--data-dir",
                    str(self.directory),
                    "market",
                    "league-watchdog",
                    *scope_args,
                ]
            )
        return code, json.loads(output.getvalue())

    def test_stale_future_stopped_and_different_scopes(self):
        self.assertEqual(self.watchdog()[0], 1)
        self.assertFalse(self.root.exists())
        with (
            patch.object(health.store, "now_utc", return_value="2026-11-26T11:30:00Z"),
            health.monitor(
                self.root, *self.scope, continuous=True, discover=True
            ) as tick,
        ):
            self.assertEqual(self.watchdog()[1]["state"], "starting")
            tick(1, {"leagues": {"bun": {"page_walk_complete": False}}})
            code, report = self.watchdog()
            self.assertEqual(
                (code, report["ticks"], report["catalog_complete_by_league"]),
                (1, 1, {"bun": False}),
            )
            self.assertTrue(report["scheduler_alive"])
            self.assertFalse(report["catalog_healthy"])
            self.assertEqual(self.watchdog("--league", "bun")[1]["state"], "missing")
            self.assertEqual(self.watchdog("--local-catalog")[1]["state"], "missing")
            with patch.object(
                health.store, "now_utc", return_value="2026-11-26T11:33:01Z"
            ):
                self.assertEqual(self.watchdog()[1]["state"], "stale")
            with patch.object(
                health.store, "now_utc", return_value="2026-11-26T11:29:59Z"
            ):
                self.assertEqual(self.watchdog()[1]["state"], "clock_conflict")
            with health.monitor(
                self.root, *self.scope, continuous=True, discover=True
            ) as peer:
                peer(2, None)
            code, report = self.watchdog()
            self.assertEqual(
                (code, report["state"], report["ticks"]), (1, "running", 1)
            )
        with patch.object(health.store, "now_utc", return_value="2026-11-26T11:30:01Z"):
            self.assertEqual(self.watchdog()[1]["state"], "stopped")
        self.assertFalse((self.root / "runtime.sqlite3").exists())

    def test_watch_only_reports_actual_continuous_scheduler_ticks(self):
        observed = []

        def interrupt(_):
            observed.append(self.watchdog("--local-catalog"))
            raise KeyboardInterrupt

        with patch.object(big5_market, "_ranked_jobs", return_value=[]):
            big5_market.watch(
                self.directory, None, self.start, self.end, discover=False, cycles=1
            )
            self.assertEqual(self.watchdog("--local-catalog")[1]["state"], "missing")
            with (
                patch.object(big5_market.time, "sleep", side_effect=interrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                big5_market.watch(
                    self.directory, None, self.start, self.end, discover=False, cycles=0
                )
        self.assertEqual((observed[0][0], observed[0][1]["ticks"]), (0, 1))
        self.assertIsNone(observed[0][1]["catalog_by_league"])
        self.assertIsNone(observed[0][1]["catalog_healthy"])
        self.assertEqual(observed[0][1]["catalog_state"], "local_only")
        self.assertEqual(self.watchdog("--local-catalog")[1]["state"], "stopped")

    def successful_walk(self, stamp):
        return {
            "page_walk_complete": True,
            "gaps": [],
            "pages": [{"received_at": stamp, "rows": 0, "http_fresh": True}],
        }

    def test_per_league_native_receipts_survive_rapid_failed_refreshes_and_peers(self):
        all_leagues = self.scope[0]
        t0 = "2026-11-26T12:00:00.000000Z"
        bun = "2026-11-26T11:59:59.000000Z"
        good = {league: self.successful_walk(t0) for league in all_leagues}
        good["bun"] = self.successful_walk(bun)
        with (
            patch.object(health.store, "now_utc", return_value=t0) as clock,
            patch.object(health.time, "monotonic", return_value=0) as monotonic,
            health.monitor(
                self.root, *self.scope, continuous=True, discover=True
            ) as tick,
        ):
            # A completion between throttled heartbeat writes must still be recorded.
            tick(1, None)
            tick(2, {"completed_at": t0, "leagues": good})
            self.assertEqual(self.watchdog()[0], 0)
            tick(
                3,
                {"completed_at": t0, "leagues": {"bun": {"page_walk_complete": False}}},
            )
            code, report = self.watchdog()
            self.assertEqual(
                (code, report["catalog_complete_by_league"]), (0, {"bun": False})
            )
            self.assertEqual(report["catalog_by_league"]["bun"]["last_success_at"], bun)
            with health.monitor(
                self.root, *self.scope, continuous=True, discover=True
            ) as peer:
                peer(
                    1,
                    {
                        "leagues": {
                            league: self.successful_walk(bun) for league in all_leagues
                        }
                    },
                )
            self.assertEqual(
                self.watchdog()[1]["catalog_by_league"]["sea"]["last_success_at"], t0
            )
            clock.return_value = "2026-11-26T12:10:00.000000Z"
            monotonic.return_value = 600
            tick(4, None)
            code, report = self.watchdog()
            self.assertEqual(
                (code, report["state"], report["catalog_state"]),
                (1, "running", "stale"),
            )
            self.assertEqual(
                report["catalog_by_league"]["bun"],
                {
                    "last_success_at": bun,
                    "age_seconds": 601,
                    "state": "stale",
                },
            )
            self.assertEqual(report["catalog_by_league"]["sea"]["state"], "fresh")
            self.assertEqual(self.watchdog("--max-catalog-age", "601")[0], 0)
            self.assertEqual(
                self.watchdog("--league", "bun")[1]["catalog_state"], "missing"
            )
        with patch.object(health.store, "now_utc", return_value="2026-11-26T12:10:01Z"):
            code, report = self.watchdog()
        self.assertEqual((code, report["state"]), (1, "stopped"))
        self.assertEqual(report["catalog_by_league"]["bun"]["last_success_at"], bun)

    def test_partial_or_unverified_walks_do_not_attest_to_success(self):
        t0 = "2026-11-26T12:00:00Z"
        broken = (
            {"page_walk_complete": True, "pages": [], "gaps": []},
            {**self.successful_walk(t0), "gaps": [{"error": "overlap"}]},
            {**self.successful_walk(t0), "page_walk_complete": False},
            self.successful_walk("bad timestamp"),
            self.successful_walk("2026-11-26T12:05:00Z"),
        )
        with (
            patch.object(health.store, "now_utc", return_value=t0) as clock,
            health.monitor(
                self.root, *self.scope, continuous=True, discover=True
            ) as tick,
        ):
            for walk in broken:
                tick(
                    1,
                    {
                        "completed_at": t0,
                        "leagues": {"bun": walk, "sea": self.successful_walk(t0)},
                    },
                )
                code, report = self.watchdog()
                self.assertEqual((code, report["catalog_state"]), (1, "missing"))
                self.assertIsNone(report["catalog_by_league"]["bun"]["last_success_at"])
                self.assertEqual(report["catalog_by_league"]["sea"]["state"], "fresh")
            clock.return_value = "2026-11-26T11:59:59Z"
            self.assertEqual(self.watchdog()[1]["catalog_state"], "clock_conflict")
            tick(
                2,
                {
                    "leagues": {
                        lg: self.successful_walk(clock.return_value)
                        for lg in self.scope[0]
                    }
                },
            )
            self.assertEqual(self.watchdog()[0], 0)

    def test_live_a2_database_is_read_without_promoting_old_completion_flags(self):
        self.root.mkdir(parents=True)
        path = self.root / "watch-health.sqlite3"
        key = health.store.canonical(health._scope(*self.scope, True))
        t0 = "2026-11-26T12:00:00.000000Z"
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE watches(id TEXT PRIMARY KEY,scope,started,updated,state,ticks,catalog)"
            )
            db.execute(
                "INSERT INTO watches VALUES(?,?,?,?,?,?,?)",
                (
                    "legacy",
                    key,
                    t0,
                    t0,
                    "running",
                    7,
                    '{"bun":true}',
                ),
            )
        with patch.object(health.store, "now_utc", return_value=t0):
            code, report = self.watchdog()
            self.assertEqual(
                (code, report["scheduler_alive"], report["catalog_state"]),
                (1, True, "missing"),
            )
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual(
                    [
                        r[0]
                        for r in db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    ],
                    ["watches"],
                )
            with health.monitor(
                self.root, *self.scope, continuous=True, discover=True
            ) as tick:
                tick(
                    1,
                    {"leagues": {lg: self.successful_walk(t0) for lg in self.scope[0]}},
                )
                self.assertEqual(self.watchdog()[0], 0)

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "Linux process start identities"
    )
    def test_reused_local_pid_and_foreign_pid_namespace(self):
        with health.monitor(
            self.root, *self.scope, continuous=True, discover=False
        ) as tick:
            tick(1, None)
            with (
                contextlib.closing(
                    sqlite3.connect(self.root / "watch-health.sqlite3")
                ) as db,
                db,
            ):
                count = db.execute(
                    "UPDATE watch_owners SET start_ticks=start_ticks+1"
                ).rowcount
            if not count:
                self.skipTest("procfs cannot verify process identities")
            self.assertEqual(self.watchdog("--local-catalog")[1]["state"], "exited")
            with patch.object(
                health, "_pid_domain", return_value="a different host or namespace"
            ):
                self.assertEqual(self.watchdog("--local-catalog")[0], 0)

    @unittest.skipUnless(os.name == "posix", "POSIX process signals")
    def test_background_sigint_sigterm_and_stopped_peer_after_crash(self):
        for sig in (signal.SIGKILL, signal.SIGINT, signal.SIGTERM):
            start_floor = health.store.now_utc()
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import signal; signal.signal(signal.SIGINT,signal.SIG_IGN); "
                    "from polymarket861.cli import main; raise SystemExit(main())",
                    "--data-dir",
                    str(self.directory),
                    "market",
                    "league-watch",
                    "--start",
                    self.start,
                    "--end",
                    self.end,
                    "--cycles",
                    "0",
                    "--local-catalog",
                ],
                stdout=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    report = health.status(self.root, *self.scope, discover=False)
                    if report["healthy"] and report["started_at"] >= start_floor:
                        break
                    self.assertIsNone(proc.poll())
                    time.sleep(0.05)
                else:
                    self.fail("background scheduler did not become ready")
                previous = report["run_id"]
                proc.send_signal(sig)
                self.assertEqual(
                    proc.wait(timeout=8), -sig if sig == signal.SIGKILL else 130
                )
                if sig != signal.SIGKILL and health._pid_domain():
                    code, report = self.watchdog("--local-catalog")
                    self.assertEqual(
                        (code, report["state"], report["run_id"]),
                        (1, "stopped", previous),
                    )
                elif sig == signal.SIGKILL and health._pid_domain():
                    self.assertEqual(
                        self.watchdog("--local-catalog")[1]["state"], "exited"
                    )
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

    def test_service_protocol_messages_and_abstract_socket_address(self):
        with (
            patch.dict(os.environ, {"NOTIFY_SOCKET": "@pm861-watch"}),
            patch.object(health.socket, "socket") as transport,
            health.monitor(
                self.root, *self.scope, continuous=True, discover=True
            ) as tick,
        ):
            send = transport.return_value.__enter__.return_value.sendto
            self.assertFalse(send.called)
            with patch.object(health.time, "monotonic", side_effect=[10, 12, 27]):
                for count in (1, 2, 3):
                    tick(count, None)
        self.assertEqual(
            [call.args for call in send.call_args_list],
            [
                (payload, "\x00pm861-watch")
                for payload in (b"READY=1\nWATCHDOG=1", b"WATCHDOG=1", b"STOPPING=1")
            ],
        )

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "systemd uses Unix datagrams")
    def test_notify_reports_readiness_only_after_first_tick(self):
        # AF_UNIX path length is platform-bounded; the test temp directory is short.
        address = str(self.directory / "notify.sock")
        try:
            receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        except PermissionError as exc:
            if exc.errno == errno.EPERM:
                self.skipTest("host prohibits Unix datagram sockets")
            raise
        with (
            receiver as service,
            patch.dict(os.environ, {"NOTIFY_SOCKET": address}),
        ):
            service.bind(address)
            service.settimeout(1)
            with health.monitor(
                self.root, *self.scope, continuous=True, discover=False
            ) as tick:
                with patch.object(health.time, "monotonic", side_effect=[10, 12, 27]):
                    tick(1, None)
                    self.assertEqual(service.recv(128), b"READY=1\nWATCHDOG=1")
                    tick(2, None)
                    tick(3, None)
                    self.assertEqual(service.recv(128), b"WATCHDOG=1")
            self.assertEqual(service.recv(128), b"STOPPING=1")
