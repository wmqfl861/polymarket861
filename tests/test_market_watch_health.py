"""A live scheduler tick, restart, and stale scope are visible without writing the evidence DB."""

import contextlib
import errno
import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket861 import big5_market, cli, market_watch_health as health


class WatchHealthTests(unittest.TestCase):
    start = "2026-11-27T00:00:00Z"
    end = "2026-12-01T00:00:00Z"

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
        with contextlib.redirect_stdout(output):
            code = cli.main(
                [
                    "--data-dir",
                    str(self.directory),
                    "market",
                    "league-watchdog",
                    "--start",
                    self.start,
                    "--end",
                    self.end,
                    *args,
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
                (0, 1, {"bun": False}),
            )
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
            self.assertEqual(self.watchdog()[0], 0)
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
        self.assertEqual(self.watchdog("--local-catalog")[1]["state"], "stopped")

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
