import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from polymarket861 import market_archive as archive
from polymarket861 import market_provider as provider

T = datetime(2026, 11, 25, 3, 0, tzinfo=UTC)
FIX = "epl-not-che-2026-11-27"
CON = "0x" + "3" * 64


def event(suffix="", home_time="2026-11-27T20:00:00Z"):
    return {
        "id": 2,
        "slug": FIX + suffix,
        "startTime": home_time,
        "live": False,
        "ended": False,
        "teams": [
            {"ordering": "home", "name": "Forest", "alias": "Forest"},
            {"ordering": "away", "name": "Chelsea", "alias": "Chelsea"},
        ],
        "markets": [
            {
                "slug": FIX + "-team-total-home-1pt5",
                "conditionId": CON,
                "sportsMarketType": "soccer_team_totals",
                "version": "v1",
                "clobTokenIds": '["11","22"]',
                "positionIds": ["99", "98"],
                "line": 1.5,
                "outcomes": '["Over","Under"]',
                "groupItemTitle": "Forest O/U 1.5",
                "description": "rules",
                "active": True,
                "acceptingOrders": True,
                "enableOrderBook": True,
                "closed": False,
                "secondsDelay": 3,
                "feesEnabled": True,
                "feeSchedule": {"rate": 0.05, "exponent": 1},
            }
        ],
    }


def reply(obj, t=T, *, age=0):
    from email.utils import format_datetime

    return provider.Reply(
        json.dumps(obj).encode(),
        provider.iso(t - timedelta(seconds=1)),
        provider.iso(t),
        {"date": format_datetime(t), "age": str(age)},
        "https://clob.polymarket.com/book",
    )


def book(token="11"):
    return {
        "asset_id": token,
        "market": CON,
        "timestamp": int(T.timestamp() * 1000) - 100000,
        "min_order_size": "5",
        "tick_size": "0.01",
        "bids": [{"price": "0.35", "size": "10"}, {"price": "0.40", "size": "20"}],
        "asks": [{"price": "0.52", "size": "4"}, {"price": "0.42", "size": "19"}],
    }


class ArchiveTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        clock = patch.object(
            archive, "now_utc", return_value=provider.iso(T + timedelta(minutes=10))
        )
        clock.start()
        self.addCleanup(clock.stop)

    def test_protocol_selection_mapping_and_first_half(self):
        data = event()
        first = provider.normalized_markets(data)[0]
        self.assertEqual(
            (first["family"], first["tokens"][0]["token_id"]), ("home_total", "11")
        )
        data["markets"][0]["version"] = "v2"
        self.assertEqual(
            provider.normalized_markets(data)[0]["tokens"][0]["token_id"], "99"
        )
        data["markets"][0]["groupItemTitle"] = "Chelsea O/U 1.5"
        self.assertEqual(
            provider.normalized_markets(data)[0]["mapping_error"],
            "team_title_unverified",
        )
        data["markets"][0]["sportsMarketType"] = "soccer_first_half_team_totals"
        self.assertIsNone(provider.normalized_markets(data)[0]["family"])

    def test_catalog_normalization_leaves_archive_writer_available(self):
        events = [event(), event("-more-markets")]
        receipt = reply(events)
        normalize = provider.normalized_markets
        seen = []

        def check_writer(item, **kwargs):
            with closing(sqlite3.connect(self.root / "runtime.sqlite3", timeout=0)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self.assertEqual(db.execute("SELECT count(*) FROM ma_events").fetchone()[0], 0)
            seen.append(item["slug"])
            return normalize(item, **kwargs)

        with (
            patch.object(archive, "league_tag", return_value=82),
            patch.object(archive, "_fetch", return_value=(events, receipt, "source")),
            patch.object(provider, "normalized_markets", side_effect=check_writer),
        ):
            result = archive.catalog_window(self.root, "week", T.isoformat(), (T + timedelta(days=1)).isoformat())
        self.assertEqual(seen, [item["slug"] for item in events])
        self.assertEqual(result["fixtures_observed"], [FIX])
        self.assertTrue(result["page_walk_complete"])

    def test_catalog_publication_is_atomic_and_retry_preserves_row_ids(self):
        events = [event(), event("-more-markets")]
        receipt = reply(events)
        tables = ("ma_events", "ma_event_states", "ma_contract_payloads", "ma_contract_receipts")
        with closing(archive.database(self.root)) as db, db:
            db.execute(
                "CREATE TRIGGER reject_last BEFORE INSERT ON ma_contract_receipts "
                "WHEN NEW.event_slug LIKE '%-more-markets' BEGIN SELECT RAISE(ABORT,'publication failed'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "publication failed"):
            archive._index_catalog_page(self.root, events, receipt, "source")
        with closing(archive.database(self.root)) as db, db:
            for table in tables:
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0, table)
            db.execute("DROP TRIGGER reject_last")
        rows = []
        for _ in range(2):
            archive._index_catalog_page(self.root, events, receipt, "source")
            with closing(archive.database(self.root)) as db:
                rows.append([
                    list(map(tuple, db.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")))
                    for table in tables
                ])
        self.assertEqual(rows[0], rows[1])
        self.assertEqual([len(group) for group in rows[0]], [2, 2, 2, 2])

    def test_catalog_rechecks_a_prepared_payload_id_at_publication(self):
        events = [event()]
        archive._index_catalog_page(self.root, events, reply(events), "first")
        with closing(archive.database(self.root)) as db, db:
            db.execute(
                "CREATE TRIGGER change_binding AFTER INSERT ON ma_events WHEN NEW.sha='second' "
                "BEGIN UPDATE ma_contract_payloads SET body='{}' WHERE id=1; END"
            )
        second = reply(events, T + timedelta(minutes=1))
        archive._index_catalog_page(self.root, events, second, "second")
        with closing(archive.database(self.root)) as db:
            payload_id, payload = db.execute(
                "SELECT r.payload_id,c.payload FROM ma_contract_receipts r JOIN ma_contracts c "
                "ON c.rowid=r.id WHERE r.sha='second'"
            ).fetchone()
        self.assertNotEqual(payload_id, 1)
        restored = json.loads(payload)
        self.assertEqual(restored["recorded_at"], second.received_at)
        self.assertEqual(restored["source_sha256"], "second")
        self.assertEqual(restored["condition_id"], CON)

    def test_legacy_moneyline_strict_question(self):
        data = event()
        m = data["markets"][0]
        m.update(
            slug=FIX + "-not",
            sportsMarketType=None,
            groupItemTitle="Forest",
            outcomes='["Yes","No"]',
            question="Will Forest win on 2026-11-27?",
        )
        self.assertEqual(provider.normalized_markets(data)[0]["side"], "home")
        m["question"] = "Will Forest win the first half?"
        self.assertIsNotNone(provider.normalized_markets(data)[0]["mapping_error"])

    def test_book_id_depth_cache_and_fee_coverage(self):
        m = provider.normalized_markets(event())[0]
        b = provider.normalize_book(book(), reply(book()), m, "11", "a" * 64)
        self.assertEqual(b["bids"][0]["price"], "0.40")
        self.assertEqual(b["asks"][0]["price"], "0.42")
        self.assertEqual(b["pregame"], True)
        self.assertLess(b["exchange_at"], b["observed_at"])
        self.assertIsNone(b["fee_effective_at"])
        self.assertIsNone(
            provider.normalize_book(book(), reply(book(), age=34), m, "11", "a" * 64)[
                "observed_at"
            ]
        )
        with self.assertRaisesRegex(ValueError, "different"):
            provider.normalize_book(book("22"), reply(book()), m, "11", "x")
        broken = book()
        broken["bids"][1]["price"] = "0.42"
        with self.assertRaisesRegex(ValueError, "crossed"):
            provider.normalize_book(broken, reply(broken), m, "11", "x")

    def test_heartbeat_and_conflicting_schedule_remain_in_archive(self):
        def get(url, params=None):
            if "more-markets" in url:
                return reply(event("-more-markets", "2026-11-28T20:00:00Z"))
            if url.endswith("/book"):
                return reply(book())
            return reply(event())

        with patch.object(provider, "get", side_effect=get):
            out = archive.capture(self.root, FIX, tokens=["11"])
            self.assertIsNone(out["observations"][0]["pregame"])
            self.assertEqual(
                len(archive.stored_books(self.root, "11")[0]["schedule_conflict"]), 2
            )
            archive.capture(self.root, FIX, tokens=["11"])
        from contextlib import closing

        with closing(archive.database(self.root)) as db:
            self.assertEqual(
                db.execute(
                    "select count(*) from ma_requests where operation='book'"
                ).fetchone()[0],
                2,
            )
        summary = archive.coverage(self.root, fixtures=[FIX, "epl-aa-bb-2026-10-01"])
        group = next(x for x in summary["groups"] if x["family"] == "home_total")
        self.assertEqual(
            (
                group["expected_fixtures"],
                group["listed_fixtures"],
                group["two_sided_book_tokens"],
            ),
            (2, 1, 1),
        )
        self.assertEqual(group["evidence"]["A"], 0)

    def test_history_cursor_resumes_preserving_filters_and_economic_fills(self):
        rows = [
            {
                "transaction_hash": "tx",
                "token_id": "11",
                "condition_id": CON,
                "timestamp": int(T.timestamp()),
                "price": 0.4,
                "size": 5,
                "proxy_wallet": "w",
                "side": "BUY",
            }
        ]
        modified = dict(rows[0], size=8)
        observed = []

        def get(url, params):
            observed.append(params.copy())
            more = "cursor" not in params
            return reply(
                {
                    "data": rows if more else rows + [modified],
                    "pagination": {
                        "has_more": more,
                        "next_cursor": "page-two" if more else None,
                    },
                }
            )

        with patch.object(provider, "get", side_effect=get):
            first = archive.history(self.root, "trades", "2", pages=1, limit=1)
            second = archive.history(self.root, "trades", "2", pages=1, limit=1)
            self.assertFalse(first["complete"])
            self.assertTrue(second["complete"])
            self.assertEqual(first["new_rows"] + second["new_rows"], 2)
            self.assertEqual(
                observed[1], {"limit": 1, "event_id": "2", "cursor": "page-two"}
            )
            self.assertTrue(
                archive.history(self.root, "trades", "2", limit=1)[
                    "resumed_completed_walk"
                ]
            )

    def test_reject_broken_history_and_settlement_price(self):
        with patch.object(
            provider,
            "get",
            return_value=reply({"data": [], "pagination": {"has_more": True}}),
        ), self.assertRaisesRegex(ValueError, "pagination"):
            archive.history(self.root, "trades", "2")
        # Resolution records and post-kickoff prices never stand in for a pregame quote.
        items = [{"timestamp": int(T.timestamp()), "price": 1, "resolution_seconds": 0}]

        def get(url, params=None):
            if url.endswith("/v2/prices-history"):
                return reply(
                    {
                        "data": items,
                        "pagination": {"has_more": False, "next_cursor": None},
                    }
                )
            return reply(event("-more-markets") if "more-markets" in url else event())

        with patch.object(provider, "get", side_effect=get):
            archive.discover(self.root, FIX)
            archive.history(
                self.root, "prices", "11", start=1, end=int(T.timestamp()) + 1
            )
            group = next(
                x
                for x in archive.coverage(self.root)["groups"]
                if x["family"] == "home_total"
            )
            self.assertEqual(group["evidence"], {"A": 0, "B": 0, "C": 0, "missing": 2})

    def test_batch_unordered_rows_bound_to_correct_tokens(self):
        methods = []

        def get(url, params=None):
            methods.append(url)
            if url.endswith("/books"):
                self.assertEqual(params["token_ids"], ["11", "22"])
                a = book("11")
                b = book("22")
                b["asks"][0]["size"] = "17"
                return reply([b, a])
            return reply(event("-more-markets") if "more-markets" in url else event())

        with patch.object(provider, "get", side_effect=get):
            result = archive.capture(self.root, FIX, tokens=["22", "11"])
        self.assertIsNone(result["batch_error"])
        self.assertEqual(len(result["observations"]), 2)
        self.assertEqual(len([m for m in methods if m.endswith("/book")]), 0)
        a = archive.stored_books(self.root, "11")[0]
        b = archive.stored_books(self.root, "22")[0]
        self.assertEqual(a["source_sha256"], b["source_sha256"])
        self.assertEqual(b["asks"][1]["size"], "17")
        self.assertEqual(a["asks"][1]["size"], "4")

    def test_older_short_club_labels_and_reindex_audit(self):
        data = event()
        m = data["markets"][0]
        data["teams"][0].update(
            name="Nottingham Forest FC", alias="Forest", abbreviation="not"
        )
        data["title"] = "Nottingham Forest vs. Chelsea"
        m.update(
            sportsMarketType=None,
            outcomes='["Yes","No"]',
            slug=FIX + "-not",
            question="Will Nottingham Forest win on 2026-11-27?",
            groupItemTitle="Nottingham Forest",
        )
        with patch.object(
            provider,
            "get",
            side_effect=lambda url, params=None: reply(
                data | {"slug": FIX + "-more-markets", "markets": []}
                if "more-markets" in url
                else data
            ),
        ):
            archive.discover(self.root, FIX)
        self.assertEqual(archive.contracts(self.root, FIX)[0]["side"], "home")
        from contextlib import closing

        with closing(archive.database(self.root)) as db, db:
            db.execute(
                "UPDATE ma_contracts SET payload=json_set(payload,'$.mapping_error','old-name-parser')"
            )
        changed = archive.reindex(self.root)
        self.assertEqual(changed["changed_mappings"], 1)
        self.assertEqual(archive.reindex(self.root)["changed_mappings"], 0)
        with closing(archive.database(self.root)) as db:
            old = db.execute("SELECT old_payload FROM ma_mapping_changes").fetchone()[0]
        self.assertEqual(json.loads(old)["mapping_error"], "old-name-parser")
        self.assertIsNone(archive.contracts(self.root, FIX)[0]["mapping_error"])

    def test_sampling_on_grid_and_closing_target(self):
        kickoff = T + timedelta(days=2)
        self.assertEqual(archive.next_capture(kickoff, T)[0], T + timedelta(minutes=15))
        self.assertEqual(
            archive.next_capture(kickoff, kickoff - timedelta(seconds=25))[1],
            "closing_book",
        )
        self.assertEqual(
            archive.next_capture(kickoff, kickoff)[0], kickoff + timedelta(seconds=12)
        )
        self.assertIsNone(
            archive.next_capture(kickoff, kickoff + timedelta(minutes=10))
        )
