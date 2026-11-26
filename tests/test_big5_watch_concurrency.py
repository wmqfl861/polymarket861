import errno
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

from polymarket861 import big5_market as big5
from polymarket861 import market_archive as archive
from polymarket861 import market_provider as provider
from polymarket861 import store
from polymarket861.store import iso
import test_big5_market as fixtures
from test_big5_market import BASE, DAY, END, START, gamma, native


NOW = iso(DAY + "T13:00:00Z")
SPORTS = [{"sport": "sea", "series": "10203", "tags": "101962", "primaryTagId": 101962}]


def ranked(fixture):
    now = provider.moment(NOW)
    job = ("periodic", now, now + timedelta(minutes=10), False)
    return 1, job[2], {"fixture": fixture, "kickoff": iso(DAY + "T14:00:00Z")}, job


class Big5WatchConcurrency(unittest.TestCase):
    def test_background_catalog_and_book_sampling_run_on_two_real_workers(self):
        clock = [provider.moment(NOW)]
        overlap = threading.Barrier(2)
        touched = set()

        def advance(_):
            clock[0] += timedelta(minutes=5)

        def upstream(url, params=None, **kwargs):
            if iso(clock[0]) != NOW and url.endswith(("/events", "/books")):
                touched.add(url.rsplit("/", 1)[-1])
                overlap.wait(timeout=5)
            return gamma(url, params, **kwargs)

        def now():
            return iso(clock[0])

        with (
            tempfile.TemporaryDirectory() as data,
            patch.object(big5, "now_utc", now),
            patch.object(archive, "now_utc", now),
            patch.object(store, "now_utc", now),
            patch.object(fixtures, "now_utc", now),
            patch.object(big5.time, "sleep", advance),
            patch.object(provider, "get", upstream),
        ):
            result = big5.watch(
                data, ["sea"], START, END, discover=True, cycles=2, workers=2
            )
            self.assertEqual(touched, {"events", "books"})
            self.assertTrue(result["discovery"]["leagues"]["sea"]["page_walk_complete"])
            self.assertEqual(result["states"], {"complete": 1})
            with closing(big5._db(big5.research_root(data))) as db:
                self.assertEqual(
                    db.execute("SELECT success FROM big5_slots").fetchone()[0], 1
                )
                run = json.loads(
                    db.execute("SELECT report FROM big5_runs").fetchone()[0]
                )
                self.assertEqual(run["received_tokens"], 12)

    def test_reader_with_concurrent_writer_commits_entire_catalog_page(self):
        with tempfile.TemporaryDirectory() as data:
            root = big5.research_root(data)
            ready, committed = threading.Event(), threading.Event()
            open_db, ingest = big5._db, archive._ingest
            reply = gamma(
                provider.GAMMA + "/events",
                {"tag_id": "101962", "offset": 0, "limit": 3},
            )

            def catalog_db(root):
                db = open_db(root)
                # A writer reservation blocks until the other thread commits.
                db.set_trace_callback(
                    lambda sql: ready.set() if sql == "BEGIN IMMEDIATE" else None
                )
                return db

            def ingest_after_other_writer(*args, **kwargs):
                # A deferred savepoint has already read the old snapshot at this point.
                ready.set()
                self.assertTrue(committed.wait(5), "other writer did not commit")
                return ingest(*args, **kwargs)

            with (
                closing(open_db(root)) as writer,
                patch.object(big5, "_db", catalog_db),
                patch.object(archive, "_ingest", ingest_after_other_writer),
                patch.object(
                    archive,
                    "_fetch",
                    return_value=([native(), native(True)], reply, "a" * 64),
                ),
                ThreadPoolExecutor(max_workers=1) as pool,
            ):
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    "INSERT INTO big5_last_dispatch VALUES(?,?,?)",
                    (BASE, "periodic", NOW),
                )
                future = pool.submit(
                    big5._catalog_one, root, "sea", START, END, 1, 3, 0, SPORTS
                )
                try:
                    self.assertTrue(
                        ready.wait(5), "catalogue did not reach its write transaction"
                    )
                    writer.commit()
                finally:
                    writer.rollback()
                    committed.set()
                report = future.result(timeout=5)
                self.assertTrue(report["page_walk_complete"], report["gaps"])
                self.assertEqual(report["fixtures"], [BASE])
                self.assertEqual(report["fixtures_with_both_events"], 1)
                self.assertEqual(
                    writer.execute("SELECT count(*) FROM big5_events").fetchone()[0], 2
                )

    def test_rolled_back_page_reports_no_committed_fixtures(self):
        with tempfile.TemporaryDirectory() as data:
            root = big5.research_root(data)
            record = big5._record

            def fail_second(db, event, *args):
                if event["slug"].endswith("-more-markets"):
                    raise RuntimeError("local contract parser fault")
                return record(db, event, *args)

            with (
                patch.object(provider, "get", gamma),
                patch.object(big5, "_record", fail_second),
            ):
                result = big5.catalog(data, ["sea"], START, END)["leagues"]["sea"]
            self.assertFalse(result["page_walk_complete"])
            self.assertEqual(result["next_offset"], 0)
            self.assertEqual(result["fixtures"], [])
            self.assertEqual(result["gaps"][0]["error"], "local contract parser fault")
            with closing(big5._db(root)) as db:
                self.assertEqual(
                    db.execute("SELECT count(*) FROM big5_events").fetchone()[0], 0
                )

    def test_sample_failure_is_durable_retryable_and_does_not_stop_another_game(self):
        other = "sea-three-four-" + DAY

        def sample(root, fixture, **kwargs):
            if fixture == BASE:
                raise RuntimeError("local parser rejected score")
            return {
                "contracts": {
                    "moneyline.home": {
                        "tokens": {"10": {"observed_at": NOW, "received_at": NOW}}
                    }
                },
                "core_missing": [],
                "received_tokens": 1,
                "requested_tokens": 1,
            }

        with (
            tempfile.TemporaryDirectory() as data,
            patch.object(big5, "now_utc", return_value=NOW),
            patch.object(
                big5, "_ranked_jobs", return_value=[ranked(BASE), ranked(other)]
            ),
            patch.object(big5, "_sample", sample),
        ):
            result = big5.watch(
                data, ["sea"], START, END, discover=False, cycles=1, workers=2
            )
            self.assertEqual(result["states"], {"failed": 1, "complete": 1})
            with closing(big5._db(big5.research_root(data))) as db:
                slots = {
                    r["fixture"]: r for r in db.execute("SELECT * FROM big5_slots")
                }
            self.assertEqual(slots[BASE]["success"], 0)
            self.assertEqual(slots[BASE]["attempts"], 1)
            self.assertEqual(
                slots[BASE]["expires"], iso(provider.moment(NOW) + timedelta(seconds=2))
            )
            self.assertEqual(
                json.loads(slots[BASE]["report"])["error"],
                "local parser rejected score",
            )
            self.assertEqual(slots[other]["success"], 1)
            history = big5.weekly_report(data, START, END)["collector_task_failures"]
            self.assertEqual(history["count"], 1)
            self.assertEqual(history["latest"][0]["fixture"], BASE)
            self.assertEqual(history["latest"][0]["error_type"], "RuntimeError")

    def test_transient_claim_failure_preserves_reason_without_inventing_a_slot(self):
        with (
            tempfile.TemporaryDirectory() as data,
            patch.object(big5, "now_utc", return_value=NOW),
            patch.object(big5, "_ranked_jobs", return_value=[ranked(BASE)]),
            patch.object(
                big5,
                "_claim",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
        ):
            result = big5.watch(data, ["sea"], START, END, discover=False, cycles=1)
            self.assertEqual(result["states"], {"failed": 1})
            with closing(big5._db(big5.research_root(data))) as db:
                self.assertEqual(
                    db.execute("SELECT count(*) FROM big5_slots").fetchone()[0], 0
                )
            history = big5.weekly_report(data, START, END)["collector_task_failures"]
            self.assertEqual(history["latest"][0]["error"], "database is locked")

    def test_discovery_exception_at_startup_or_in_final_future_is_durable(self):
        for failed_at in (1, 2):
            clock, calls = [provider.moment(NOW)], []

            def advance(_seconds):
                clock[0] += timedelta(minutes=6)

            def catalog(*args, **kwargs):
                calls.append(clock[0])
                if len(calls) == failed_at:
                    raise RuntimeError("failed discovery iteration")
                return {"leagues": {"sea": {"gaps": [], "page_walk_complete": True}}}

            with (
                self.subTest(failed_at=failed_at),
                tempfile.TemporaryDirectory() as data,
                patch.object(big5, "now_utc", side_effect=lambda: iso(clock[0])),
                patch.object(big5.time, "sleep", side_effect=advance),
                patch.object(big5, "catalog", catalog),
            ):
                result = big5.watch(data, ["sea"], START, END, cycles=2)
                self.assertEqual(len(calls), 2)
                latest = result["discovery"]["leagues"]["sea"]
                self.assertEqual(latest["page_walk_complete"], failed_at == 1)
                history = big5.weekly_report(data, START, END)[
                    "collector_task_failures"
                ]
                self.assertEqual(history["count"], 1)
                failure = history["latest"][0]["gaps_by_league"]["sea"][0]
                self.assertEqual(failure["error"], "failed discovery iteration")
                self.assertEqual(failure["error_type"], "RuntimeError")

    def test_fatal_errors_and_process_control_propagate(self):
        for exc in (
            sqlite3.DatabaseError("database disk image is malformed"),
            OSError(errno.ENOSPC, "no space left"),
            KeyboardInterrupt(),
            SystemExit(1),
        ):
            for operation in ("catalog", "_sample"):
                with (
                    self.subTest(operation=operation, error=type(exc).__name__),
                    tempfile.TemporaryDirectory() as data,
                    patch.object(big5, "now_utc", return_value=NOW),
                    patch.object(big5, "_ranked_jobs", return_value=[ranked(BASE)]),
                    patch.object(big5, operation, side_effect=exc),
                ):
                    with self.assertRaises(type(exc)):
                        big5.watch(
                            data,
                            ["sea"],
                            START,
                            END,
                            cycles=1,
                            discover=operation == "catalog",
                        )

    def test_cancelled_futures_keep_their_own_context_and_other_results(self):
        clock = [provider.moment(NOW)]
        other = "sea-three-four-" + DAY

        def advance(_):
            clock[0] += timedelta(minutes=6)

        def rank(*_):
            return [] if iso(clock[0]) == NOW else [ranked(BASE), ranked(other)]

        def job(_data, event, window, _workers):
            return {
                "fixture": event["fixture"],
                "phase": window[0],
                "state": "complete",
            }

        class CancelOne(ThreadPoolExecutor):
            def submit(self, fn, *args, **kwargs):
                if fn is big5._watch_catalog or (
                    fn is job and args[1]["fixture"] == BASE
                ):
                    cancelled = Future()
                    cancelled.cancel()
                    return cancelled
                return super().submit(fn, *args, **kwargs)

        with (
            tempfile.TemporaryDirectory() as data,
            patch.object(big5, "now_utc", side_effect=lambda: iso(clock[0])),
            patch.object(big5.time, "sleep", advance),
            patch.object(big5, "_ranked_jobs", rank),
            patch.object(big5, "_job", job),
            patch.object(
                big5,
                "catalog",
                return_value={"leagues": {"sea": {"page_walk_complete": True}}},
            ),
            patch.object(big5, "ThreadPoolExecutor", CancelOne),
        ):
            result = big5.watch(data, ["sea"], START, END, cycles=2)
            self.assertEqual(result["states"], {"failed": 1, "complete": 1})
            latest = result["discovery"]["leagues"]["sea"]
            self.assertFalse(latest["page_walk_complete"])
            self.assertEqual(latest["gaps"][0]["error_type"], "CancelledError")
            failures = big5.weekly_report(data, START, END)["collector_task_failures"]
            self.assertEqual(failures["count"], 2)
            sample = next(
                row for row in failures["latest"] if row["operation"] == "sample"
            )
            self.assertEqual(sample["fixture"], BASE)
            self.assertEqual(sample["error"], "CancelledError")
