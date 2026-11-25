import gzip
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError

from polymarket861 import (
    execution_books,
    ledger,
    market_archive,
    market_provider,
    season_backfill,
    shadow,
    store,
)
from test_market_archive import CON, FIX, book, event

TARGET = datetime(2026, 11, 27, 19, 0, 5, tzinfo=UTC)
KICK = TARGET + timedelta(hours=1, seconds=-5)
CUT = KICK - timedelta(hours=1)
SECOND = timedelta(seconds=1)


def receipt(raw, url, requested, received):
    return market_provider.Reply(
        json.dumps(raw).encode(),
        store.iso(requested),
        store.iso(received),
        {"date": format_datetime(received), "age": "0"},
        url,
    )


class ExecutionCollectionTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = patch.object(
            market_archive, "now_utc", return_value=store.iso(TARGET)
        ).start()
        self.addCleanup(patch.stopall)
        self.ingest()
        with closing(market_archive.database(self.root)) as db:
            db.executescript(ledger.SCHEMA + shadow.SCHEMA)

    def ingest(self, kickoff=KICK, at=CUT - SECOND):
        raw = event(home_time=store.iso(kickoff))
        with patch.object(
            market_provider,
            "get",
            return_value=receipt(
                raw, market_provider.GAMMA + "/events/slug/" + FIX, at - SECOND, at
            ),
        ):
            market_archive.observe_event(self.root, FIX)

    def order(
        self,
        token,
        *,
        account="main",
        mode="formal",
        status="frozen",
        kickoff=KICK,
        delay=5,
        slug=FIX,
    ):
        cutoff = kickoff - timedelta(hours=1)
        intent = {
            "market_slug": slug,
            "match_id": "fx-" + slug,
            "decision_at": store.iso(cutoff),
            "kickoff": store.iso(kickoff),
            "contract": {
                "condition_id": CON,
                "token_id": token,
                "seconds_delay": delay,
            },
        }
        plan = {"intent": intent}
        with closing(market_archive.database(self.root)) as db, db:
            if mode == "research":
                db.execute(
                    "INSERT OR IGNORE INTO research_shadow_accounts VALUES(?,?,?)",
                    (account, "1000", "1000"),
                )
                db.execute(
                    "INSERT INTO research_shadow_orders VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        account,
                        account + token,
                        intent["match_id"],
                        slug,
                        store.iso(cutoff),
                        token,
                        CON,
                        status,
                        store.digest(plan),
                        store.canonical(plan),
                        "{}",
                    ),
                )
            else:
                db.execute(
                    "INSERT OR IGNORE INTO paper_accounts VALUES(?,?,?,?)",
                    (account, mode, "1000", "1000"),
                )
                db.execute(
                    "INSERT INTO paper_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        account,
                        account + token,
                        intent["match_id"],
                        CON,
                        token,
                        store.iso(cutoff),
                        0,
                        status,
                        store.digest(plan),
                        store.canonical(plan),
                        "{}",
                    ),
                )

    def run_slot(self, **kwargs):
        return market_archive.capture_execution(
            self.root,
            FIX,
            kickoff=store.iso(KICK),
            target=store.iso(TARGET),
            deadline=store.iso(TARGET + 5 * SECOND),
            discovery={"events": [FIX], "gaps": []},
            **kwargs,
        )

    def test_real_order_scope_is_fixture_kickoff_delay_specific_and_empty_is_no_network(
        self,
    ):
        self.order("22", mode="historical")
        self.order("22", account="after", delay=9)
        self.order("22", account="skip", status="skipped")
        self.order("22", account="other", slug=FIX + "-other")
        self.order("22", account="postponed", kickoff=KICK + timedelta(hours=2))
        with patch.object(market_provider, "get") as get:
            self.assertTrue(self.run_slot()["no_frozen_intents"])
            get.assert_not_called()
            other = market_archive.capture(
                self.root, FIX, tokens=[], discovery={"events": [FIX], "gaps": []}
            )
            self.assertEqual(other["observations"], [])
            get.assert_not_called()
        self.order("11", mode="research")
        self.order("11")
        calls = []

        def get(url, params, *, timeout):
            calls.append((params["token_ids"], timeout))
            return receipt(
                [book(t) for t in params["token_ids"]],
                url,
                TARGET,
                TARGET + 0.25 * SECOND,
            )

        with patch.object(market_provider, "get", side_effect=get):
            first, repeated = self.run_slot(), self.run_slot()
        self.assertEqual(calls, [(["11"], 1.5)])
        self.assertEqual(first["requested_tokens"], ["11"])
        self.assertEqual(first["observations"], repeated["observations"])
        self.assertEqual(
            first["attempt_latency_ms"],
            [{"leg": "primary", "status": "received", "ms": 250.0}],
        )
        self.assertEqual(
            market_archive.stored_books(self.root, "11", before=store.iso(KICK))[0][
                "received_at"
            ],
            store.iso(TARGET + 0.25 * SECOND),
        )

    def test_late_native_cannot_be_used_and_hedge_is_independent_and_bounded(self):
        self.order("11")
        second = threading.Event()
        calls = []

        def get(url, params, *, timeout):
            calls.append(tuple(params["token_ids"]))
            if len(calls) == 1:
                self.assertTrue(second.wait(1))
                return receipt([book("11")], url, TARGET, TARGET + 6 * SECOND)
            second.set()
            return receipt([book("11")], url, TARGET + SECOND, TARGET + 1.25 * SECOND)

        with (
            patch.object(execution_books, "_HEDGE_AFTER", 0.02),
            patch.object(market_provider, "get", side_effect=get),
        ):
            report = self.run_slot()
        self.assertEqual(calls, [("11",), ("11",)])
        self.assertTrue(report["hedge_started"])
        self.assertEqual(
            report["observations"][0]["received_at"], store.iso(TARGET + 1.25 * SECOND)
        )
        self.assertEqual(
            {r["status"] for r in report["attempt_latency_ms"]},
            {"outside_slot", "received"},
        )
        with closing(market_archive.database(self.root)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM ma_books").fetchone()[0], 1
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM ma_requests WHERE operation='books' AND status='ok'"
                ).fetchone()[0],
                2,
            )

    def test_partial_recovery_only_requests_missing_tokens(self):
        self.order("11")
        self.order("22", account="strategy", mode="research")
        ids = []

        def get(url, params, *, timeout):
            ids.append(params["token_ids"])
            at = TARGET + len(ids) * 0.25 * SECOND
            return receipt([book(params["token_ids"][0])], url, at - 0.2 * SECOND, at)

        with patch.object(market_provider, "get", side_effect=get):
            report = self.run_slot()
        self.assertEqual(ids, [["11", "22"], ["22"]])
        self.assertEqual(
            {r["token_id"] for r in report["observations"] if r.get("asks")},
            {"11", "22"},
        )

    def test_first_native_arrival_wins_even_when_primary_leg_is_slower(self):
        self.order("11")
        hedge = threading.Event()
        calls = []

        def get(url, params, *, timeout):
            calls.append(tuple(params["token_ids"]))
            if len(calls) == 1:
                self.assertTrue(hedge.wait(1))
                at = TARGET + 1.4 * SECOND
            else:
                hedge.set()
                at = TARGET + 1.1 * SECOND
            return receipt([book("11")], url, TARGET, at)

        with (
            patch.object(execution_books, "_HEDGE_AFTER", 0.015),
            patch.object(market_provider, "get", side_effect=get),
        ):
            report = self.run_slot()
        self.assertEqual(
            report["observations"][0]["received_at"], store.iso(TARGET + 1.1 * SECOND)
        )
        with closing(market_archive.database(self.root)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM ma_books").fetchone()[0], 1
            )

    def test_interrupted_partial_resumes_from_native_original_without_repeating_primary(
        self,
    ):
        self.order("11")
        self.order("22", mode="research")
        claim = execution_books._claim

        def only_primary(root, key, leg, tokens):
            return leg == "primary" and claim(root, key, leg, tokens)

        calls = []

        def get(url, params, *, timeout):
            calls.append(params["token_ids"])
            return receipt(
                [book(params["token_ids"][0])],
                url,
                TARGET,
                TARGET + len(calls) * 0.25 * SECOND,
            )

        with patch.object(market_provider, "get", side_effect=get):
            with patch.object(execution_books, "_claim", side_effect=only_primary):
                partial = self.run_slot()
            resumed = self.run_slot()
        self.assertEqual(sum("error" in r for r in partial["observations"]), 1)
        self.assertEqual(calls, [["11", "22"], ["22"]])
        self.assertTrue(all(r.get("asks") for r in resumed["observations"]))

    def test_future_exchange_timestamp_cannot_be_reported_fresh(self):
        self.order("11")

        def get(url, params, *, timeout):
            return receipt(
                [
                    book("11")
                    | {"timestamp": int((TARGET + 10 * SECOND).timestamp() * 1000)}
                ],
                url,
                TARGET,
                TARGET + 0.2 * SECOND,
            )

        with patch.object(market_provider, "get", side_effect=get) as calls:
            report = self.run_slot()
        self.assertEqual(calls.call_count, 2)
        self.assertTrue(report["observations"][0].get("error"))
        self.assertTrue(
            all(leg["status"] == "invalid" for leg in report["attempt_latency_ms"])
        )

    def test_research_is_disjoint_and_cannot_create_book_evidence(self):
        self.order("11")

        def get(url, params, *, timeout):
            return receipt(
                [book(t) for t in params["token_ids"]],
                url,
                TARGET,
                TARGET + 0.25 * SECOND,
            )

        with patch.object(market_provider, "get", side_effect=get):
            report = self.run_slot(research=True)
            self.assertTrue(report["research_submitted"])
            self.assertTrue(execution_books._research_capacity.acquire(timeout=2))
            execution_books._research_capacity.release()
        with closing(market_archive.database(self.root)) as db:
            rows = [
                json.loads(r[0])
                for r in db.execute("SELECT metadata FROM observations")
            ]
            self.assertEqual(
                [
                    (r["operation"], r["request_token_ids"])
                    for r in rows
                    if r["operation"] == "books_research"
                ],
                [("books_research", ["22"])],
            )
            self.assertEqual(
                [r[0] for r in db.execute("SELECT token FROM ma_books")], ["11"]
            )

    def test_reschedule_rate_limit_and_long_wait_never_bypass(self):
        self.order("11")
        with patch.object(
            market_provider,
            "get",
            side_effect=HTTPError("x", 429, "slow", {"Retry-After": "3600"}, None),
        ) as get:
            report = self.run_slot()
            self.assertEqual(get.call_count, 1)
            self.assertFalse(report["hedge_started"])
            with self.assertRaises(market_archive.DeferredFetch):
                market_archive._fetch(
                    self.root,
                    "book",
                    "11",
                    market_provider.CLOB + "/book",
                    {"token_id": "11"},
                )
            self.assertEqual(get.call_count, 1)
        with closing(market_archive.database(self.root)) as db:
            detail = json.loads(
                db.execute(
                    "SELECT detail FROM ma_requests WHERE status='long_cooldown'"
                ).fetchone()[0]
            )
            self.assertEqual(
                detail["review_at"], store.iso(TARGET + timedelta(minutes=15))
            )
            self.assertEqual(
                detail["not_before"], store.iso(TARGET + timedelta(hours=1))
            )
        self.ingest(KICK + timedelta(hours=2), TARGET - SECOND)
        with patch.object(market_provider, "get") as get:
            self.assertIn("kickoff changed", self.run_slot()["batch_error"])
            get.assert_not_called()


class NetworkAndHistoryTests(TestCase):
    def test_connection_reuse_gzip_trickle_total_deadline_and_concurrent_independence(
        self,
    ):
        peers, headers = [], []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                peers.append(self.client_address)
                headers.append(dict(self.headers))
                payload = gzip.compress(b'{"ok":true}')
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                try:
                    for byte in payload:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        if self.path == "/trickle":
                            time.sleep(0.05)
                except OSError:
                    pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
            threading.Thread(target=http.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{http.server_port}"
            try:
                self.assertEqual(market_provider.get(url).body, b'{"ok":true}')
                self.assertEqual(market_provider.get(url).body, b'{"ok":true}')
                self.assertEqual(peers[0], peers[1])
                self.assertEqual(headers[0]["Accept-Encoding"], "gzip")
                with ThreadPoolExecutor(2) as pool:
                    t = time.monotonic()
                    slow = pool.submit(
                        market_provider.get, url + "/trickle", timeout=0.2
                    )
                    self.assertEqual(
                        market_provider.get(url, timeout=1).body, b'{"ok":true}'
                    )
                    with self.assertRaises(TimeoutError):
                        slow.result(timeout=0.9)
                    self.assertLess(time.monotonic() - t, 0.85)
            finally:
                http.shutdown()

    def test_503_server_wait_covers_sibling_paths_and_keeps_original_status(self):
        with (
            TemporaryDirectory() as root,
            patch.object(market_archive, "now_utc", return_value=store.iso(TARGET)),
        ):
            err = HTTPError(
                "url",
                503,
                "timeout",
                {"Retry-After": format_datetime(TARGET + 90 * SECOND)},
                None,
            )
            with patch.object(market_provider, "get", side_effect=err) as calls:
                for kind in ("trades", "prices-history"):
                    with self.assertRaises(market_archive.DeferredFetch) as raised:
                        market_archive._fetch(
                            root, kind, "11", market_provider.DATA + "/v2/" + kind
                        )
                    self.assertEqual(raised.exception.code, 503)
                    self.assertEqual(
                        raised.exception.retry_at, store.iso(TARGET + 90 * SECOND)
                    )
            self.assertEqual(calls.call_count, 1)
            self.assertEqual(
                market_archive._retry_at("9" * 500, TARGET),
                "9999-12-31T23:59:59.999999Z",
            )

    def test_earliest_expiring_tier_and_host_cooldown(self):
        with TemporaryDirectory() as root, closing(season_backfill._db(root)) as db, db:
            now = datetime(2026, 11, 25, tzinfo=UTC)
            for key, days in (
                ("fresh", 1),
                ("5min", 59),
                ("30min", 89.5),
                ("old", 150),
            ):
                end = int((now - timedelta(days=days)).timestamp())
                db.execute(
                    "INSERT INTO season_history_jobs VALUES(?,2026,?,'prices',?,?,?,0,'pending','','',NULL)",
                    (key, key, key, end - 1800, end),
                )
            rows = season_backfill._next_jobs(db, 2026, now, 4)
            self.assertEqual(
                [r["key"] for r in rows], ["30min", "5min", "fresh", "old"]
            )
            db.execute(
                "INSERT INTO ma_cooldown VALUES(?,?)",
                ("data-api.polymarket.com", store.iso(now + SECOND)),
            )
            self.assertFalse(season_backfill._next_jobs(db, 2026, now, 4))
