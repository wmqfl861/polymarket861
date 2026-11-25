import contextlib
import io
import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from email.message import Message
from email.utils import format_datetime
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode

from polymarket861 import (
    market_archive as ma,
)
from polymarket861 import (
    market_provider as provider,
)
from polymarket861 import (
    season,
    store,
)
from polymarket861 import (
    season_backfill as back,
)

NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)
KICK = NOW + timedelta(hours=1, minutes=15)


def native(slug, kick=KICK, *, idx=1, aux=False):
    home, away = slug.split("-")[1:3]
    return {
        "slug": slug + ("-more-markets" if aux else ""),
        "id": str(idx),
        "title": "Test home vs. Test away",
        "startTime": store.iso(kick),
        "startDate": store.iso(kick - timedelta(hours=1)),
        "closedTime": store.iso(kick + timedelta(hours=2)),
        "live": False,
        "ended": False,
        "teams": [
            {"name": "Test home", "abbreviation": home, "ordering": "home"},
            {"name": "Test away", "abbreviation": away, "ordering": "away"},
        ],
        "markets": [
            {
                "slug": slug + "-total-2pt5",
                "conditionId": "0x" + f"{idx:064x}",
                "sportsMarketType": "totals",
                "version": "v1",
                "outcomes": ["Over", "Under"],
                "clobTokenIds": [str(idx * 10), str(idx * 10 + 1)],
                "line": 2.5,
                "secondsDelay": 5,
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "enableOrderBook": True,
                "description": "test source text",
                "feesEnabled": False,
            }
        ],
    }


def reply(data, url, t):
    return provider.Reply(
        json.dumps(data).encode(),
        store.iso(t),
        store.iso(t),
        {"date": format_datetime(t, usegmt=True), "age": "0"},
        url,
    )


def ingest(root, event, t):
    with patch.object(
        provider,
        "get",
        return_value=reply(event, provider.GAMMA + "/events/slug/" + event["slug"], t),
    ):
        ma.observe_event(root, event["slug"])


class SeasonTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.slug = "epl-aa-bb-2026-11-25"

    def test_decision_and_delayed_close_actual_time(self):
        cutoff = KICK - timedelta(hours=1)
        self.assertEqual(
            ma.next_capture(KICK, cutoff - timedelta(seconds=30)),
            (cutoff - timedelta(seconds=25), "decision_book"),
        )
        for late in (0, 10, 30, 60, 120, 180, 590):
            actual = KICK + timedelta(seconds=late)
            val = ma.next_capture(KICK, actual - timedelta(seconds=31))
            self.assertTrue(actual - timedelta(seconds=30) <= val[0] < actual)
        data = native(self.slug)
        raw = {
            "asset_id": "10",
            "market": data["markets"][0]["conditionId"],
            "asks": [{"price": ".51", "size": "5"}],
            "bids": [{"price": ".49", "size": "5"}],
        }
        probe = KICK + timedelta(seconds=25)
        ingest(self.root, data, probe - timedelta(seconds=3))
        with (
            patch.object(ma, "now_utc", return_value=store.iso(probe)),
            patch.object(
                provider,
                "get",
                return_value=reply(raw, provider.CLOB + "/book?token_id=10", probe),
            ),
        ):
            m = ma.contracts(self.root, self.slug)[0]
            self.assertTrue(ma.snapshot_book(self.root, m, "10")["pregame"])
        after = probe + timedelta(seconds=20)
        with (
            patch.object(ma, "now_utc", return_value=store.iso(after)),
            patch.object(
                provider,
                "get",
                return_value=reply(raw, provider.CLOB + "/book?token_id=10", after),
            ),
        ):
            self.assertIsNone(ma.snapshot_book(self.root, m, "10")["pregame"])
        data["live"] = True
        ingest(self.root, data, after)
        self.assertTrue(ma.market_state(self.root, self.slug))

    def test_retry_after_and_truncated_http(self):
        target = provider.GAMMA + "/events/slug/" + self.slug
        data = native(self.slug)
        headers = Message()
        headers["Retry-After"] = "120"
        problem = HTTPError(target, 429, "busy", headers, None)
        with (
            patch.object(ma, "now_utc", return_value=store.iso(NOW)),
            patch.object(provider, "get", side_effect=problem) as get,
        ):
            with self.assertRaises(ma.DeferredFetch) as first:
                ma.observe_event(self.root, self.slug)
            self.assertEqual(
                first.exception.retry_at, store.iso(NOW + timedelta(seconds=120))
            )
            with self.assertRaises(ma.DeferredFetch):
                ma.observe_event(self.root, self.slug)
            self.assertEqual(get.call_count, 1)
        later = NOW + timedelta(seconds=121)
        with (
            patch.object(ma, "now_utc", return_value=store.iso(later)),
            patch.object(ma.time, "sleep"),
            patch.object(
                provider,
                "get",
                side_effect=[IncompleteRead(b"x", 3), reply(data, target, later)],
            ) as get,
        ):
            self.assertEqual(ma.observe_event(self.root, self.slug), self.slug)
            self.assertEqual(get.call_count, 2)
        with contextlib.closing(ma.database(self.root)) as db:
            self.assertEqual(
                [
                    r[0]
                    for r in db.execute(
                        "SELECT status FROM ma_requests WHERE operation='event' ORDER BY id"
                    )
                ],
                ["error", "error", "ok"],
            )

    def test_simultaneous_fixture_slots_and_restart_marks_past_windows(self):
        for slug, idx in ((self.slug, 1), ("epl-cc-dd-2026-11-25", 2)):
            ingest(self.root, native(slug, idx=idx), NOW)
        with contextlib.redirect_stderr(io.StringIO()):
            season._plan(self.root, NOW)
        due = KICK - timedelta(hours=1, seconds=25)
        with contextlib.closing(season._db(self.root)) as db, db:
            for name in season._SERVICES:
                db.execute(
                    "INSERT OR REPLACE INTO season_services(service,next_at) VALUES(?,?)",
                    (name, store.iso(KICK)),
                )
            db.execute(
                "UPDATE season_slots SET state='done' WHERE role NOT IN ('decision_book','execution_book')"
            )
        barrier = threading.Barrier(2)
        seen = []

        def sample(root, item):
            self.assertEqual(item["role"], "decision_book")
            seen.append(item["fixture"])
            barrier.wait(3)
            return {"fresh_by_deadline": 2, "books": 2}

        with (
            patch.object(store, "now_utc", return_value=store.iso(due)),
            patch.object(season, "_slot", side_effect=sample),
        ):
            c = season.Coordinator(self.root, workers=2)
            self.assertEqual(c.tick(), 2)
            c.close()
        self.assertEqual(set(seen), {self.slug, "epl-cc-dd-2026-11-25"})
        # A later restart audits expired execution slots. It never issues backdated depth.
        after = KICK - timedelta(minutes=59)
        with (
            contextlib.closing(season._db(self.root)) as db,
            db,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            season._expire(db, store.iso(after))
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM season_slots WHERE role='execution_book' AND state='missed' AND target>=?",
                    (store.iso(NOW),),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM season_alerts a JOIN season_slots s ON s.key=a.key WHERE s.role='execution_book' AND s.target>=?",
                    (store.iso(NOW),),
                ).fetchone()[0],
                2,
            )
            self.assertFalse(db.execute("SELECT 1 FROM ma_books").fetchone())

    def test_past_season_backfill_pages_resume_add_child_and_coarser_measured_curve(
        self,
    ):
        kick = NOW - timedelta(days=2)
        data = native("epl-aa-bb-2026-11-23", kick)
        child = native("epl-aa-bb-2026-11-23", kick, idx=2, aux=True)
        calls = []

        def get(url, params=None):
            p = params or {}
            calls.append((url, dict(p)))
            if url.endswith("/sports"):
                body = [{"sport": "epl", "primaryTagId": 82}]
            elif url.endswith("/events"):
                body = [data]
            elif url.endswith("more-markets"):
                body = child
            elif url.endswith("/trades"):
                cursor = p.get("cursor")
                trade = {
                    "token_id": str(int(p["event_id"]) * 10),
                    "condition_id": "0x" + f"{int(p['event_id']):064x}",
                    "timestamp": int(kick.timestamp()),
                    "price": 0.41,
                    "size": 5,
                    "transaction_hash": str(cursor or "first"),
                }
                body = {
                    "data": [trade],
                    "pagination": {
                        "has_more": not bool(cursor),
                        "next_cursor": None if cursor else "page-two",
                    },
                }
            else:
                points = (
                    [
                        {
                            "timestamp": p["start"] + 60,
                            "price": 0.4,
                            "resolution_seconds": 300,
                        }
                    ]
                    if p["bucket_seconds"] == 300
                    else []
                )
                body = {
                    "data": points,
                    "pagination": {"has_more": False, "next_cursor": None},
                }
            return reply(body, url + ("?" + urlencode(p) if params else ""), NOW)

        with (
            patch.object(ma, "now_utc", return_value=store.iso(NOW)),
            patch.object(store, "now_utc", return_value=store.iso(NOW)),
            patch.object(provider, "get", side_effect=get),
        ):
            first = back.backfill(self.root, 2026, max_pages=2, workers=1, page_size=2)
            self.assertEqual(first["page_steps"], 2)
            self.assertTrue(first["catalog_complete"])
            finished = back.backfill(self.root, 2026, workers=2, page_size=5)
            self.assertEqual(finished["jobs"]["trades"]["done"], 2)
            self.assertEqual(finished["jobs"]["prices"]["done"], 4)
            self.assertEqual(finished["outcome_tokens"], 4)
            self.assertEqual(finished["tier_A"], 0)
            self.assertIsNone(finished["execution_roi"])
            before = len(calls)
            again = back.backfill(self.root, 2026, workers=2)
            self.assertEqual(len(calls), before)
            self.assertEqual(again["page_steps"], 0)
        trades = [p for url, p in calls if url.endswith("/trades")]
        self.assertEqual(sum(p.get("cursor") == "page-two" for p in trades), 2)
        first_pair = [p for p in trades if p["event_id"] == trades[0]["event_id"]]
        self.assertEqual(first_pair[0]["limit"], first_pair[-1]["limit"])
        with contextlib.closing(back._db(self.root)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM ma_history WHERE kind='trades'"
                ).fetchone()[0],
                4,
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM season_history_grains WHERE grain=60"
                ).fetchone()[0],
                4,
            )

    def test_retry_fits_original_window_and_restart_preserves_cursor_cycle(self):
        from concurrent.futures import Future

        with contextlib.closing(season._db(self.root)) as db, db:
            season._add(
                db,
                self.slug,
                store.iso(KICK),
                NOW,
                NOW + timedelta(seconds=25),
                "decision_book",
                "book",
            )
            row = dict(db.execute("SELECT * FROM season_slots").fetchone())
        worker = season.Coordinator(self.root)
        fut = Future()
        fut.set_result(
            {
                "fresh_by_deadline": 0,
                "books": 2,
                "retry_at": store.iso(NOW + timedelta(seconds=3)),
            }
        )
        with contextlib.closing(season._db(self.root)) as db, db:
            db.execute(
                "UPDATE season_slots SET state='running',owner=?", (worker.identity,)
            )
        with patch.object(store, "now_utc", return_value=store.iso(NOW)):
            worker._finish(fut, "book", row)
        with contextlib.closing(season._db(self.root)) as db:
            refreshed = db.execute(
                "SELECT state,target,deadline FROM season_slots WHERE key=?",
                (row["key"],),
            ).fetchone()
            self.assertEqual(
                tuple(refreshed), ("pending", row["target"], row["deadline"])
            )
            self.assertEqual(
                db.execute("SELECT not_before FROM season_retries").fetchone()[0],
                store.iso(NOW + timedelta(seconds=3)),
            )
        worker.close()

        def get(url, params):
            nxt = {None: "a", "a": "b", "b": "a"}[params.get("cursor")]
            return reply(
                {"data": [], "pagination": {"has_more": True, "next_cursor": nxt}},
                url,
                NOW,
            )

        with patch.object(provider, "get", side_effect=get):
            for _ in range(2):
                self.assertFalse(
                    ma.history(self.root, "trades", "42", limit=10, pages=1)["complete"]
                )
            with self.assertRaisesRegex(ValueError, "earlier run"):
                ma.history(self.root, "trades", "42", limit=10, pages=1)

    def test_first_discovery_late_accounts_for_fixed_windows_without_requesting_old_books(
        self,
    ):
        at = KICK - timedelta(minutes=50)
        past = native("epl-aa-bb-2026-10-10", kick=KICK - timedelta(days=45), idx=3)
        ingest(self.root, past, at)
        ingest(self.root, native(self.slug), at)
        with contextlib.redirect_stderr(io.StringIO()):
            season._plan(self.root, at)
            season._plan(self.root, at + timedelta(seconds=1))
        with contextlib.closing(season._db(self.root)) as db:
            missed = db.execute(
                "SELECT role,detail,deadline FROM season_slots WHERE state='missed'"
            ).fetchall()
            self.assertEqual(len(missed), 7)
            self.assertEqual(
                {r["role"] for r in missed},
                {"decision_book", "execution_book", "decision", "recover"},
            )
            self.assertEqual(
                {json.loads(r["detail"])["horizon"] for r in missed}, {"T-24h", "T-60m"}
            )
            self.assertTrue(
                all(
                    json.loads(r["detail"])["reason"] == "first_seen_late"
                    and store.instant(r["deadline"]) < at
                    for r in missed
                )
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM season_alerts").fetchone()[0], 7
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM season_slots WHERE fixture=?", (past["slug"],)
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM ma_books").fetchone()[0], 0
            )
        with patch.object(store, "now_utc", return_value=store.iso(at)):
            self.assertEqual(
                sum(
                    r["slots"]
                    for r in season.status(self.root)[
                        "critical_windows_last_seven_days"
                    ]
                ),
                7,
            )

    def test_reinstated_schedules_restore_only_observable_slots(self):
        first = KICK - timedelta(days=2)

        def revise(when, kick):
            ingest(self.root, native(self.slug, kick=kick), when)
            with contextlib.redirect_stderr(io.StringIO()):
                season._plan(self.root, when)

        revise(first, KICK)
        with contextlib.closing(season._db(self.root)) as db, db:
            first_slot = db.execute(
                "SELECT key FROM season_slots WHERE kickoff=? AND role='decision_book' ORDER BY target LIMIT 1",
                (store.iso(KICK),),
            ).fetchone()[0]
            db.execute(
                "UPDATE season_slots SET state='done',detail=? WHERE key=?",
                ('{"receipt":"existing"}', first_slot),
            )
            expected = db.execute(
                "SELECT count(*) FROM season_slots WHERE kickoff=? AND role='execution_book' AND state='pending'",
                (store.iso(KICK),),
            ).fetchone()[0]
        for move in (timedelta(days=16), timedelta(days=3)):
            first += timedelta(minutes=1)
            alt = KICK + move
            revise(first, alt)
            with contextlib.closing(season._db(self.root)) as db:
                self.assertEqual(
                    db.execute("SELECT kickoff FROM season_fixtures").fetchone()[0],
                    store.iso(alt),
                )
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM season_slots WHERE kickoff=? AND role='execution_book' AND state='pending'",
                        (store.iso(KICK),),
                    ).fetchone()[0],
                    0,
                )
            first += timedelta(minutes=1)
            revise(first, KICK)
            with contextlib.closing(season._db(self.root)) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM season_slots WHERE kickoff=? AND role='execution_book' AND state='pending'",
                        (store.iso(KICK),),
                    ).fetchone()[0],
                    expected,
                )
                self.assertEqual(
                    tuple(
                        db.execute(
                            "SELECT state,detail FROM season_slots WHERE key=?",
                            (first_slot,),
                        ).fetchone()
                    ),
                    ("done", '{"receipt":"existing"}'),
                )
                self.assertFalse(
                    db.execute(
                        "SELECT 1 FROM season_alerts WHERE role='execution_book'"
                    ).fetchone()
                )
        # A newly published earlier kickoff gets its own honest, missed deadlines.
        revised_now = first + timedelta(minutes=2)
        revised_kick = revised_now + timedelta(minutes=50)
        revise(revised_now, revised_kick)
        with contextlib.closing(season._db(self.root)) as db:
            reasons = [
                json.loads(r[0])["reason"]
                for r in db.execute(
                    "SELECT detail FROM season_slots WHERE kickoff=? AND state='missed'",
                    (store.iso(revised_kick),),
                )
            ]
            self.assertEqual(set(reasons), {"reschedule_seen_late"})

    def test_archived_early_but_planned_late_and_contiguous_reinstatement(self):
        early = KICK - timedelta(minutes=100)
        late = KICK - timedelta(minutes=50)
        ingest(self.root, native(self.slug), early)
        # Another identical native snapshot cannot erase the first observed kickoff.
        ingest(self.root, native(self.slug), late - timedelta(seconds=1))
        with contextlib.redirect_stderr(io.StringIO()):
            season._plan(self.root, late)
        with contextlib.closing(season._db(self.root)) as db:
            reasons = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT detail FROM season_slots WHERE state='missed'"
                )
            ]
            self.assertEqual(
                sum(r["reason"] == "collector_window_missed" for r in reasons), 5
            )
            self.assertEqual(sum(r["reason"] == "first_seen_late" for r in reasons), 2)
            self.assertTrue(
                all(
                    r["schedule_seen_at"] == store.iso(early)
                    and r["first_planned_at"] == store.iso(late)
                    for r in reasons
                )
            )
        # A sequence fully archived before the first planner tick still has a real revision boundary.
        another = "epl-cc-dd-2026-11-25"
        ingest(self.root, native(another, idx=4), early)
        ingest(
            self.root,
            native(another, KICK + timedelta(days=16), idx=4),
            early + timedelta(minutes=5),
        )
        restored = late - timedelta(minutes=1)
        ingest(self.root, native(another, idx=4), restored)
        with contextlib.redirect_stderr(io.StringIO()):
            season._plan(self.root, late)
        with contextlib.closing(season._db(self.root)) as db:
            restored_rows = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT detail FROM season_slots WHERE fixture=? AND state='missed'",
                    (another,),
                )
            ]
            self.assertEqual(len(restored_rows), 7)
            self.assertTrue(
                all(
                    r["reason"] == "reschedule_seen_late"
                    and r["schedule_seen_at"] == store.iso(restored)
                    for r in restored_rows
                )
            )
