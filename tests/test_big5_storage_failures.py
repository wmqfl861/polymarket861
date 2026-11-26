import errno
import io
import sqlite3
import tempfile
import unittest
from contextlib import (
    ExitStack,
    closing,
    contextmanager,
    redirect_stderr,
    redirect_stdout,
)
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import test_big5_market as fixture
from polymarket861 import big5_market as big5
from polymarket861 import (
    cli,
    dbscope,
    market_archive as archive,
    market_provider as provider,
    store,
)


@contextmanager
def collector():
    clock = [store.iso(fixture.DAY + "T12:59:40Z")]

    def get(url, params=None, **kw):
        if url.endswith("/v2/tokens"):
            return provider.Reply(
                b"[]",
                clock[0],
                clock[0],
                {},
                url + "?token_id=" + str(params["token_id"]),
            )
        return fixture.gamma(url, params, **kw)

    with tempfile.TemporaryDirectory() as data, ExitStack() as stack:
        for module in (fixture, store, archive, big5):
            stack.enter_context(
                patch.object(module, "now_utc", side_effect=lambda: clock[0])
            )
        stack.enter_context(patch.object(provider, "get", get))
        yield data, clock, get


def watch(data, *, discover=False):
    return big5.watch(
        data, ["sea"], fixture.START, fixture.END, cycles=1, discover=discover
    )


class Big5StorageFailures(unittest.TestCase):
    def test_transient_network_error_honors_deadline_and_disk_failure_keeps_its_errno(
        self,
    ):
        for local in (False, True):
            with self.subTest(local=local), collector() as (data, clock, _):
                deadline = store.iso(
                    provider.moment(clock[0]) + timedelta(milliseconds=1)
                )
                fault = (
                    OSError(errno.EIO, "archive volume I/O")
                    if local
                    else ConnectionResetError(errno.ECONNRESET, "remote reset")
                )
                source, name = (
                    (archive, "archive_bytes") if local else (provider, "get")
                )
                timer = archive._DEADLINE.set(deadline)
                try:
                    with patch.object(source, name, side_effect=fault) as inject:
                        with self.assertRaises(OSError) as cm:
                            archive.research_fetch(
                                big5.research_root(data),
                                "league_sports",
                                "big5",
                                provider.GAMMA + "/sports",
                            )
                    self.assertEqual(inject.call_count, 1)
                    if local:
                        self.assertIs(cm.exception, fault)
                    else:
                        self.assertIsInstance(cm.exception, archive.DeferredFetch)
                        self.assertIs(cm.exception.__cause__, fault)
                finally:
                    archive._DEADLINE.reset(timer)

    def test_archive_os_faults_exit_cli_once_with_original_errno_at_every_capture_phase(
        self,
    ):
        for code in (errno.ENOSPC, errno.EIO):
            for operation in (
                "league_sports",
                "league_catalog",
                "books",
                "league_event",
                "resolution",
            ):
                with (
                    self.subTest(code=code, operation=operation),
                    collector() as (data, clock, _),
                ):
                    discover = operation in ("league_sports", "league_catalog")
                    if not discover:
                        big5.catalog(data, ["sea"], fixture.START, fixture.END)
                    if operation in ("league_event", "resolution"):
                        clock[0] = store.iso(fixture.DAY + "T17:00:00Z")
                    original, errors = archive.archive_bytes, []

                    def fail_write(*args, **kwargs):
                        if kwargs["metadata"]["operation"] == operation:
                            errors.append(operation)
                            raise OSError(code, "failed to persist original")
                        return original(*args, **kwargs)

                    args = [
                        "--data-dir",
                        data,
                        "market",
                        "league-watch",
                        "--league",
                        "sea",
                        "--start",
                        fixture.START,
                        "--end",
                        fixture.END,
                        "--cycles",
                        "1",
                    ]
                    if not discover:
                        args.append("--local-catalog")
                    out, err = io.StringIO(), io.StringIO()
                    with (
                        patch.object(archive, "archive_bytes", fail_write),
                        redirect_stderr(err),
                        redirect_stdout(out),
                    ):
                        self.assertEqual(cli.main(args), 2)
                    self.assertEqual(errors, [operation])
                    self.assertIn(
                        str(OSError(code, "failed to persist original")), err.getvalue()
                    )
                    self.assertEqual(out.getvalue(), "")

    def test_corrupt_sqlite_stops_discovery(self):
        with collector() as (data, _, _):
            root = big5.research_root(data)
            root.mkdir()
            (root / "runtime.sqlite3").write_bytes(b"corrupt sqlite header" * 100)
            with self.assertRaises(sqlite3.DatabaseError) as cm:
                watch(data, discover=True)
            self.assertEqual(cm.exception.sqlite_errorcode, sqlite3.SQLITE_NOTADB)

    def test_missing_individual_original_is_partial_but_unreadable_volume_fails(self):
        for code in (errno.EIO, errno.EACCES, errno.ENOENT):
            with self.subTest(code=code), collector() as (data, _, _):
                big5.catalog(data, ["sea"], fixture.START, fixture.END)
                read_verified = dbscope.verified_bytes
                reads = []

                def read(root, path, sha, message):
                    reads.append(sha)
                    with patch.object(
                        Path, "open", side_effect=OSError(code, "read fault")
                    ):
                        return read_verified(root, path, sha, message)

                with patch.object(dbscope, "verified_bytes", read):
                    if code == errno.ENOENT:
                        self.assertEqual(watch(data)["states"], {"partial": 1})
                    else:
                        with self.assertRaises(OSError) as cm:
                            watch(data)
                        self.assertEqual(cm.exception.errno, code)
                self.assertTrue(reads)
                with closing(big5._db(big5.research_root(data))) as db:
                    self.assertEqual(
                        db.execute("SELECT sum(success) FROM big5_slots").fetchone()[0],
                        0,
                    )

    def test_real_http_errors_remain_scoped_to_remote_host_and_window(self):
        for status in (401, 403, 404, 429):
            for operation in ("catalog", "books"):
                with (
                    self.subTest(status=status, operation=operation),
                    collector() as (data, _, get),
                ):
                    if operation == "books":
                        big5.catalog(data, ["sea"], fixture.START, fixture.END)
                    suffix = "/sports" if operation == "catalog" else "/books"

                    def remote(url, params=None, **kwargs):
                        if url.endswith(suffix):
                            raise HTTPError(
                                url,
                                status,
                                "upstream response",
                                {"Retry-After": "90"},
                                None,
                            )
                        return get(url, params, **kwargs)

                    with patch.object(provider, "get", remote):
                        result = watch(data, discover=operation == "catalog")
                    if operation == "catalog":
                        report = result["discovery"]["leagues"]["sea"]
                        self.assertFalse(report["page_walk_complete"])
                        self.assertEqual(report["gaps"][0]["http_status"], status)
                    else:
                        self.assertEqual(result["states"], {"partial": 1})
                        self.assertIn(
                            str(status), result["last_jobs"][0]["batch_error"]
                        )
