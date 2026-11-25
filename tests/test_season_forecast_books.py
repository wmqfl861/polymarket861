"""Exercise the live season queue, native market receipts and the runner's real input clock."""

import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import wait
from contextlib import closing, redirect_stderr
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import test_opportunity as fx
from test_season import NOW, ingest, native
from polymarket861 import comparisons, market_provider, season, store


class ForecastBooks(unittest.TestCase):
    def timeline(self, horizon, scenario):
        setup = fx.Opportunities()
        setup.setUp()
        self.addCleanup(setup.doCleanups)
        root = setup.root
        inputs = (
            setup.prepare_inputs()
        )  # Both XIs really arrived before the T-60m window.
        cutoff = fx.KICK - season.HORIZONS[horizon]
        opens = cutoff - timedelta(minutes=5)
        freeze = opens + timedelta(
            seconds={"open": 0, "restart": 157, "outage": 152}[scenario]
        )
        with closing(season._db(root)) as db, db:
            db.execute(
                "INSERT INTO season_services(service,next_at) VALUES('forecast',?)",
                (store.iso(freeze),),
            )

        def start():
            agent = season.Coordinator(
                root,
                workers=2,
                once=True,
                football_root=Path(inputs["fixture_root"]),
                evidence=Path(inputs["evidence_path"]),
            )
            agent.services_started = set(season._SERVICES) - {"forecast"}
            return agent

        def advance(agent, at):
            def http(url, params=None, **kwargs):
                if "/events/slug/" in url:
                    return fx.reply(fx.event(url.rsplit("/", 1)[-1]), at, url)
                self.assertEqual(url, market_provider.CLOB + "/books")
                received = at + timedelta(seconds=2)
                books = [
                    {
                        "asset_id": token,
                        "market": fx.CONDITION,
                        "tick_size": ".01",
                        "min_order_size": "1",
                        "timestamp": int(received.timestamp() * 1000),
                        "bids": [{"price": ".49", "size": "10"}],
                        "asks": [{"price": ".51", "size": "10"}],
                    }
                    for token in params["token_ids"]
                ]
                return fx.reply(books, received, url)

            with fx.at(at), patch.object(market_provider, "get", side_effect=http):
                for _ in range(15):
                    agent.tick()
                    if not agent.futures:
                        return
                    self.assertFalse(wait(agent.futures, timeout=10).not_done)
                self.fail("scheduled work did not drain")

        agent = start()
        with redirect_stderr(io.StringIO()):
            try:
                first = opens - timedelta(seconds=9)
                advance(agent, first)
                expected = first + timedelta(seconds=2)
                if scenario != "open":
                    agent.close()
                    agent = start()
                    if scenario == "restart":
                        # The restart's first normal tick lands on a new observable slot.
                        sample = opens + timedelta(seconds=151)
                        advance(agent, sample)
                        expected = sample + timedelta(seconds=2)
                advance(agent, freeze)
            finally:
                agent.close()
        game = store.fixture_at(root, fx.FID)
        with closing(season._db(root)) as db:
            seal = db.execute(
                "SELECT data_json FROM forecast_seals WHERE fixture_id=? AND horizon=?",
                (fx.FID, horizon),
            ).fetchone()
            self.assertIsNotNone(seal)
            body = json.loads(seal[0])
            self.assertEqual(body["input_cutoff"], store.iso(freeze))
            frozen = store.instant(body["input_cutoff"])
            frame = comparisons.frame(
                db, root, game, frozen, frozen, fx.KICK, prospective=True
            )
            batches = [
                r[0]
                for r in db.execute(
                    "SELECT received_at FROM ma_requests WHERE operation='books' AND status='ok' ORDER BY received_at"
                )
            ]
            if scenario == "outage":
                self.assertEqual(
                    batches,
                    [store.iso(expected), store.iso(freeze + timedelta(seconds=2))],
                )
                self.assertEqual(frame["missing"]["total"], "stale_batch")
                missed = db.execute(
                    "SELECT detail FROM season_slots WHERE role='forecast_book' AND "
                    "target BETWEEN ? AND ? AND state='missed'",
                    (store.iso(opens), store.iso(freeze)),
                ).fetchall()
                self.assertTrue(missed)
                self.assertEqual(
                    {json.loads(r[0])["reason"] for r in missed},
                    {"collector_window_missed"},
                )
            else:
                self.assertEqual(frame["received_at"], store.iso(expected), frame)
                self.assertIn("total", frame["items"], frame["missing"])
                self.assertLessEqual(frozen - expected, timedelta(seconds=30))
            # No restart/initial acquisition duplicated a due native sample.
            self.assertEqual(len(batches), len(set(batches)))

    def test_real_runner_seals_with_first_or_later_books_and_rejects_late_receipts(
        self,
    ):
        for horizon in ("T-24h", "T-60m"):
            for scenario in ("open", "restart", "outage"):
                with self.subTest(horizon=horizon, scenario=scenario):
                    self.timeline(horizon, scenario)

    def test_persisted_grid_covers_every_freeze_second_under_receipt_jitter(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            kick = NOW + timedelta(days=2)
            slug = "epl-xx-yy-2026-11-27"
            ingest(root, native(slug, kick=kick), NOW)
            season._plan(root, NOW)
            with closing(season._db(root)) as db:
                for offset in season.HORIZONS.values():
                    cutoff = kick - offset
                    opens = cutoff - timedelta(minutes=5)
                    rows = db.execute(
                        "SELECT role,target,deadline FROM season_slots WHERE fixture=? "
                        "AND stage='book' AND state='pending' AND target BETWEEN ? AND ? ORDER BY target",
                        (
                            slug,
                            store.iso(opens - timedelta(seconds=30)),
                            store.iso(cutoff),
                        ),
                    ).fetchall()
                    self.assertEqual(
                        [r["role"] for r in rows],
                        ["forecast_book"] * 15 + ["decision_book"],
                    )
                    self.assertEqual(
                        rows[-1]["target"], store.iso(cutoff - timedelta(seconds=25))
                    )
                    self.assertLess(rows[-2]["deadline"], rows[-1]["target"])
                    for elapsed in range(301):
                        at = opens + timedelta(seconds=elapsed)
                        # One independent native slot must already have finished
                        # and even its earliest possible receipt must still be fresh.
                        self.assertTrue(
                            any(
                                at - timedelta(seconds=30) <= store.instant(r["target"])
                                and store.instant(r["deadline"]) <= at
                                for r in rows[:-1]
                            ),
                            at,
                        )

    def test_existing_plan_migrates_once_and_labels_pre_upgrade_windows(self):
        with tempfile.TemporaryDirectory() as folder, redirect_stderr(io.StringIO()):
            root = Path(folder)
            kick = NOW + timedelta(days=2)
            slug = "epl-xx-yy-2026-11-27"
            ingest(root, native(slug, kick=kick), NOW)
            season._plan(root, NOW)
            with closing(season._db(root)) as db, db:
                plan = json.loads(
                    db.execute("SELECT plan FROM season_fixtures").fetchone()[0]
                )
                plan.pop("forecast_book_seconds")
                stamp, sha = db.execute(
                    "SELECT received_at,sha FROM ma_events"
                ).fetchone()
                plan["version"] = store.digest(
                    [
                        "critical-windows-v3/hourly-far-books",
                        store.iso(kick),
                        stamp,
                        sha,
                    ]
                )
                db.execute(
                    "UPDATE season_fixtures SET plan=?", (store.canonical(plan),)
                )
                db.execute("DELETE FROM season_slots WHERE role='forecast_book'")
                for minute in range(1, 6):
                    target = kick - timedelta(hours=1, minutes=minute)
                    key = season._add(
                        db,
                        slug,
                        store.iso(kick),
                        target,
                        target + timedelta(seconds=45),
                        "periodic",
                        "book",
                    )
                    if minute == 1:
                        db.execute(
                            "UPDATE season_slots SET state='done',detail='stored receipt' WHERE key=?",
                            (key,),
                        )
            opens = kick - timedelta(hours=24, minutes=5)
            now = opens + timedelta(seconds=28)
            with fx.at(now):
                agent = season.Coordinator(root, once=True)
                agent.services_started = set(season._SERVICES)
                try:
                    self.assertEqual(agent.tick(), 0)
                    agent.next_plan = 0
                    self.assertEqual(agent.tick(), 0)
                finally:
                    agent.close()
            with closing(season._db(root)) as db:
                self.assertEqual(
                    dict(
                        db.execute(
                            "SELECT state,count(*) FROM season_slots WHERE role='forecast_book' GROUP BY state"
                        )
                    ),
                    {"missed": 2, "pending": 28},
                )
                self.assertEqual(
                    {
                        json.loads(row[0])["reason"]
                        for row in db.execute(
                            "SELECT detail FROM season_slots WHERE role='forecast_book' AND state='missed'"
                        )
                    },
                    {"forecast_sampling_added_after_window"},
                )
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM season_slots WHERE role='periodic' AND "
                        "state='superseded' AND detail LIKE '%forecast_window_sampling%'"
                    ).fetchone()[0],
                    4,
                )
                self.assertTrue(
                    db.execute(
                        "SELECT 1 FROM season_slots WHERE state='done' AND detail='stored receipt'"
                    ).fetchone()
                )
                self.assertFalse(
                    db.execute(
                        "SELECT 1 FROM ma_requests WHERE operation='books'"
                    ).fetchone()
                )

    def test_busy_book_pool_reserves_imminent_execution_and_prioritizes_close(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            agent = season.Coordinator(root, once=True, workers=2)
            agent.services_started = set(season._SERVICES)
            released = threading.Event()
            claimed = []
            with closing(season._db(root)) as db, db:
                for fixture, role, delay, budget in (
                    ("diag1", "forecast_book", 0, 8),
                    ("diag2", "forecast_book", 0, 8),
                    ("closing", "closing_book", 0, 12),
                    ("fill", "execution_book", 3, 5),
                ):
                    start = NOW + timedelta(seconds=delay)
                    season._add(
                        db,
                        fixture,
                        store.iso(NOW + timedelta(hours=1)),
                        start,
                        start + timedelta(seconds=budget),
                        role,
                        "book",
                    )

            def sample(root, item):
                claimed.append(item["role"])
                released.wait(3)
                return {"fresh_by_deadline": 1, "books": 1}

            try:
                with patch.object(season, "_slot", side_effect=sample), fx.at(NOW):
                    self.assertEqual(agent.tick(), 1)
                    with fx.at(NOW + timedelta(seconds=3)):
                        self.assertEqual(agent.tick(), 1)
                    released.set()
                    self.assertFalse(wait(agent.futures, timeout=3).not_done)
                    self.assertCountEqual(claimed, ["closing_book", "execution_book"])
            finally:
                released.set()
                agent.close()

    def test_simultaneous_native_outage_and_partial_batch_keep_the_original_failures(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder, redirect_stderr(io.StringIO()):
            root = Path(folder)
            kick = NOW + timedelta(hours=24, seconds=310)
            known = NOW - timedelta(minutes=2)
            for idx, slug in enumerate(
                ("epl-aa-bb-2026-11-26", "epl-cc-dd-2026-11-26"), 1
            ):
                ingest(root, native(slug, kick=kick, idx=idx), known)
            season._plan(root, known)
            simultaneous = threading.Barrier(2)

            def http(url, params, **kwargs):
                self.assertEqual(url, market_provider.CLOB + "/books")
                simultaneous.wait(3)
                if params["token_ids"] == ["10", "11"]:
                    raise OSError("native batch unavailable")
                return fx.reply(
                    [
                        {
                            "asset_id": "20",
                            "market": "0x" + f"{2:064x}",
                            "timestamp": int(NOW.timestamp() * 1000),
                            "tick_size": ".01",
                            "min_order_size": "1",
                            "bids": [{"price": ".49", "size": "10"}],
                            "asks": [{"price": ".51", "size": "10"}],
                        }
                    ],
                    NOW,
                    url,
                )

            agent = season.Coordinator(root, once=True, workers=2)
            agent.services_started = set(season._SERVICES)
            with (
                fx.at(NOW),
                patch.object(market_provider, "get", side_effect=http) as get,
            ):
                try:
                    self.assertEqual(agent.tick(), 2)
                finally:
                    agent.close()
                self.assertEqual(get.call_count, 2)
            with closing(season._db(root)) as db:
                rows = db.execute(
                    "SELECT state,detail FROM season_slots WHERE role='forecast_book' "
                    "AND target=? ORDER BY fixture",
                    (store.iso(NOW),),
                ).fetchall()
                self.assertEqual([r["state"] for r in rows], ["gap", "partial"])
                self.assertEqual(
                    json.loads(rows[0]["detail"])["batch_error"],
                    "native batch unavailable",
                )
                self.assertIn("21", json.loads(rows[1]["detail"])["token_errors"])
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM ma_requests WHERE operation='book'"
                    ).fetchone()[0],
                    0,
                )
