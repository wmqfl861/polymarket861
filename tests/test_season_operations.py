import io
import json
import shlex
import sys
import tempfile
import threading
import unittest
from concurrent.futures import Future, wait
from contextlib import closing, redirect_stderr
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import OpenerDirector

import test_opportunity as football
from test_season import KICK, NOW, ingest, native

from polymarket861 import (
    ledger,
    market_provider,
    season,
    season_alerts,
    season_health,
    shadow,
    store,
)


class SeasonOperations(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_long_service_renews_once_per_lease_and_yields_to_a_new_owner(self):
        first = season.Coordinator(self.root)
        second = season.Coordinator(self.root, once=True)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first_job, second_job = Future(), Future()
        self.addCleanup(first_job.cancel)
        self.addCleanup(second_job.cancel)

        def stamp(minutes):
            return store.iso(NOW + timedelta(minutes=minutes))

        def tick(coordinator, minutes):
            with patch.object(store, "now_utc", return_value=stamp(minutes)):
                return coordinator.tick()

        def lease():
            with closing(season._db(self.root)) as db:
                return tuple(db.execute(
                    "SELECT owner,until,updated FROM season_services WHERE service='forecast'"
                ).fetchone())

        with (
            patch.dict(season._SERVICES, {"forecast": 30}, clear=True),
            patch.object(season, "_plan"),
            patch.object(season.season_readiness, "release", return_value={"ready": True}),
            patch.object(first.pools["service"], "submit", return_value=first_job) as first_submit,
            patch.object(second.pools["service"], "submit", return_value=second_job) as second_submit,
        ):
            self.assertEqual(tick(first, 0), 1)
            self.assertEqual(tick(first, 1), 0)
            self.assertEqual(lease(), (first.identity, stamp(5), None))
            self.assertEqual(tick(first, 4), 0)
            self.assertEqual(lease(), (first.identity, stamp(9), None))
            self.assertEqual(tick(second, 6), 0)

            # Even a delayed heartbeat cannot queue another local copy.
            self.assertEqual(tick(first, 10), 0)
            self.assertEqual(lease(), (first.identity, stamp(15), None))
            self.assertEqual(tick(second, 16), 1)
            self.assertEqual(tick(first, 20), 0)
            self.assertEqual(lease(), (second.identity, stamp(21), None))
            self.assertEqual(tick(second, 20), 0)
            self.assertEqual(lease(), (second.identity, stamp(25), None))

            first_job.set_result({})
            self.assertEqual(tick(first, 21), 0)
            self.assertEqual(lease(), (second.identity, stamp(25), None))
            second_job.set_result({})
            self.assertEqual(tick(second, 22), 0)
            self.assertEqual(lease(), (None, None, stamp(22)))
            self.assertEqual(tick(second, 23), 0)
            first_submit.assert_called_once()
            second_submit.assert_called_once()

    def test_no_value_partial_and_missing_decisions_are_distinct_and_rate_limited(self):
        acct = lambda acts, gaps: season_health.account_summary(
            "main",
            {"decisions": acts, "gaps": [{"reason": s} for s in gaps]},
            "decisions",
            store.iso(NOW),
        )
        skip = {"state": "skipped", "reason": "market disagreement exceeds 0.08"}
        healthy = acct([skip], ["no verified listing at decision"])
        missing = acct([], ["sealed forecast missing", "sealed forecast missing"])
        broken = {"state": "error", "error": "bad input", "error_type": "ValueError"}
        self.assertEqual(healthy["state"], "no_value")
        self.assertEqual(missing["operational_reasons"], {"sealed forecast missing": 2})
        row = {"role": "decision", "kickoff": store.iso(KICK), "fixture": "test"}
        with closing(season._db(self.root)) as db, db, redirect_stderr(io.StringIO()):
            for minutes in (0, 59, 61):
                stamp = store.iso(NOW + timedelta(minutes=minutes))
                with patch.object(store, "now_utc", return_value=stamp):
                    self.assertEqual(
                        season_health.paper_state(
                            db, {"formal": healthy, "shadow": healthy}, row, stamp
                        ),
                        "done",
                    )
                    self.assertEqual(
                        season_health.paper_state(
                            db, {"formal": healthy, "shadow": missing}, row, stamp
                        ),
                        "partial",
                    )
                    self.assertEqual(
                        season_health.paper_state(
                            db, {"formal": broken, "shadow": missing}, row, stamp
                        ),
                        "error",
                    )
            self.assertEqual(
                dict(
                    db.execute("SELECT role,count(*) FROM season_alerts GROUP BY role")
                ),
                {"paper_formal": 2, "paper_shadow": 2},
            )

    def test_returned_errors_are_counted_even_when_service_succeeds(self):
        result = {
            "status": "degraded",
            "fetches": [{"scope": 10, "status": "unavailable", "error": "slow"}],
            "overdue": [{"fixture_id": "f1"}],
            "quarantined": [{"fixture_id": "f2"}],
        }
        problems = dict(season_health.service_problems("results", result))
        self.assertEqual(set(problems), {"source:OpenLigaDB", "overdue", "quarantined"})
        self.assertIn("f1", problems["overdue"])
        self.assertEqual(
            dict(
                season_health.service_problems(
                    "history", {"catalog_errors": [{"op": "catalog", "error": "429"}]}
                )
            ),
            {"catalog": "429"},
        )
        self.assertFalse(
            list(
                season_health.service_problems(
                    "results", {"status": "ok", "overdue": []}
                )
            )
        )

    def test_finished_services_persist_each_distinct_cause_once(self):
        coordinator = season.Coordinator(self.root)
        self.addCleanup(coordinator.close)
        with redirect_stderr(io.StringIO()):
            for minutes in (0, 5):
                stamp = store.iso(NOW + timedelta(minutes=minutes))
                for name, report in (
                    (
                        "results",
                        {
                            "status": "degraded",
                            "overdue": [{"fixture_id": "f1"}],
                            "fetches": [
                                {
                                    "scope": 10,
                                    "status": "unavailable",
                                    "error": f"retry {minutes}",
                                }
                            ],
                        },
                    ),
                    (
                        "history",
                        {
                            "catalog_errors": [
                                {"op": "catalog", "error": f"http {minutes}"}
                            ]
                        },
                    ),
                ):
                    with closing(season._db(self.root)) as db, db:
                        db.execute(
                            "INSERT OR REPLACE INTO season_services(service,next_at,owner) VALUES(?,?,?)",
                            (name, stamp, coordinator.identity),
                        )
                    future = Future()
                    future.set_result(report)
                    with patch.object(store, "now_utc", return_value=stamp):
                        coordinator._finish(future, "service", {"service": name})
        with closing(season._db(self.root)) as db:
            self.assertEqual(
                dict(
                    db.execute("SELECT role,count(*) FROM season_alerts GROUP BY role")
                ),
                {"history": 1, "results": 2},
            )
            self.assertEqual(
                [
                    json.loads(row["result"])["health"]
                    for row in db.execute(
                        "SELECT result FROM season_services ORDER BY service"
                    )
                ],
                ["degraded", "degraded"],
            )

    def test_independent_watchdog_routes_json_and_coalesces_across_hour_boundary(self):
        outfile = self.root / "alerts"
        # Literal punctuation is part of an argv element. Event text travels on stdin.
        arg = "$(touch should-never-run)"
        program = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.stdin.read() + sys.argv[2])"
        command = shlex.join([sys.executable, "-c", program, str(outfile), arg])
        t = NOW.replace(minute=59)
        with (
            patch.object(store, "now_utc", return_value=store.iso(t)),
            redirect_stderr(io.StringIO()),
        ):
            self.assertTrue(season_alerts.heartbeat(self.root)["heartbeat_stale"])
            self.assertFalse(outfile.exists())
            one = season_alerts.heartbeat(self.root, alert_command=command)
            self.assertIsNone(one["delivery"]["error"])
        raw = outfile.read_text().removesuffix(arg)
        self.assertEqual(json.loads(raw)["role"], "watchdog")
        with patch.object(
            store, "now_utc", return_value=store.iso(t + timedelta(minutes=3))
        ):
            self.assertIsNone(
                season_alerts.heartbeat(self.root, alert_command=command)["delivery"]
            )
        with closing(season._db(self.root)) as db, db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM season_alerts").fetchone()[0], 1
            )
            db.execute(
                "INSERT INTO season_services(service,next_at,updated) VALUES('heartbeat','',?)",
                (store.iso(t),),
            )
        with patch.object(
            store, "now_utc", return_value=store.iso(t + timedelta(seconds=90))
        ):
            self.assertFalse(season_alerts.heartbeat(self.root)["heartbeat_stale"])
        slow = shlex.join([sys.executable, "-c", "import time; time.sleep(10)"])
        with (
            patch.object(
                store, "now_utc", return_value=store.iso(t + timedelta(minutes=61))
            ),
            patch.object(season_alerts, "TIMEOUT", 0.05),
            redirect_stderr(io.StringIO()),
        ):
            failed = season_alerts.heartbeat(self.root, alert_command=slow)
            self.assertEqual(failed["delivery"]["error"], "TimeoutExpired")
            self.assertIsNone(
                season_alerts.heartbeat(self.root, alert_command=command)["delivery"]
            )

    def test_far_cadence_migrates_pending_books_and_preserves_other_observations(self):
        slug = "epl-nor-sou-2026-12-01"
        kick = NOW + timedelta(days=6)
        ingest(self.root, native(slug, kick=kick), NOW)
        season._plan(self.root, NOW)
        with closing(season._db(self.root)) as db, db:

            def count(stage, offset):
                return db.execute(
                    "SELECT count(*) FROM season_slots WHERE stage=? AND target=? AND state='pending'",
                    (stage, store.iso(kick - offset)),
                ).fetchone()[0]

            self.assertEqual(count("book", timedelta(hours=52, minutes=15)), 0)
            self.assertEqual(count("metadata", timedelta(hours=52, minutes=15)), 1)
            self.assertEqual(count("book", timedelta(hours=47, minutes=45)), 1)
            self.assertEqual(count("book", timedelta(hours=52)), 1)
            plan = json.loads(
                db.execute(
                    "SELECT plan FROM season_fixtures WHERE fixture=?", (slug,)
                ).fetchone()[0]
            )
            plan.pop("far_book_minutes")
            plan["version"] = "old-plan"
            db.execute(
                "UPDATE season_fixtures SET plan=? WHERE fixture=?",
                (store.canonical(plan), slug),
            )

            def old(minutes):
                target = kick - timedelta(hours=52, minutes=minutes)
                return season._add(
                    db,
                    slug,
                    store.iso(kick),
                    target,
                    target + timedelta(seconds=45),
                    "periodic",
                    "book",
                )

            pending, done = old(15), old(30)
            db.execute(
                "UPDATE season_slots SET state='done',detail=? WHERE key=?",
                ('{"receipt":"original"}', done),
            )
        season._plan(self.root, NOW)
        with closing(season._db(self.root)) as db:
            self.assertEqual(
                tuple(
                    db.execute(
                        "SELECT state,detail FROM season_slots WHERE key=?", (pending,)
                    ).fetchone()
                ),
                ("superseded", '{"reason":"far_book_hourly"}'),
            )
            self.assertEqual(
                tuple(
                    db.execute(
                        "SELECT state,detail FROM season_slots WHERE key=?", (done,)
                    ).fetchone()
                ),
                ("done", '{"receipt":"original"}'),
            )

    def test_pending_handler_does_not_hold_loop_or_sqlite_writer(self):
        entered, release = threading.Event(), threading.Event()

        def handler(*args, **kwargs):
            self.assertFalse(kwargs["shell"])
            entered.set()
            self.assertTrue(release.wait(3))
            return SimpleNamespace(returncode=0)

        with closing(season._db(self.root)) as db, db, redirect_stderr(io.StringIO()):
            season._alert(db, "test", None, "results", "source unavailable")
        coordinator = season.Coordinator(
            self.root, once=True, alert_command="/operator/bin/receiver"
        )
        coordinator.services_started = set(season._SERVICES)
        try:
            with patch.object(season_alerts.subprocess, "run", side_effect=handler):
                coordinator.tick()
                self.assertTrue(entered.wait(2))
                coordinator.tick()
                with closing(season._db(self.root)) as db:
                    self.assertIsNotNone(
                        db.execute(
                            "SELECT updated FROM season_services WHERE service='heartbeat'"
                        ).fetchone()
                    )
                self.assertFalse(coordinator.delivery.done())
                release.set()
                coordinator.delivery.result(timeout=3)
        finally:
            release.set()
            coordinator.close()

    def test_full_matchday_from_forecast_service_through_execution_and_close(self):
        fixture = football.Opportunities()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.root
        inputs = fixture.prepare_inputs()
        when = football.CUT - timedelta(minutes=3)
        price = Decimal(".50")

        def http(url, params=None, **kwargs):
            src = url
            if params and url != market_provider.CLOB + "/books":
                src += "?" + urlencode(params)

            def event(slug):
                body = football.event(slug)
                body["live"] = when >= football.KICK
                return body

            def book(token):
                ask = price if token == "711" else Decimal(".98")
                return {
                    "asset_id": token,
                    "market": football.CONDITION,
                    "hash": store.digest([token, store.iso(when)]),
                    "tick_size": ".01",
                    "min_order_size": "1",
                    "timestamp": int(when.timestamp() * 1000),
                    "bids": [{"price": str(ask - Decimal(".01")), "size": "40"}],
                    "asks": [{"price": str(ask), "size": "40"}],
                }

            if url.endswith("/sports"):
                data = [{"sport": "epl", "primaryTagId": 82}]
            elif "/events/slug/" in url:
                data = event(url.rsplit("/", 1)[-1])
            elif url.endswith("/events"):
                data = [event(football.SLUG), event(football.SLUG + "-more-markets")]
            elif url.endswith("/books"):
                data = [book(token) for token in params["token_ids"]]
            elif url.endswith("/book"):
                data = book(params["token_id"])
            else:
                raise OSError("offline public endpoint " + url)
            return football.reply(data, when, src)

        options = {
            "football_root": Path(inputs["fixture_root"]),
            "evidence": Path(inputs["evidence_path"]),
        }
        with (
            patch.object(market_provider, "get", side_effect=http),
            patch.object(
                OpenerDirector, "open", side_effect=OSError("offline football sources")
            ),
            redirect_stderr(io.StringIO()),
        ):
            # Run the first collector tick with concurrent catalog, result and news work.
            with football.at(when):
                start = season.run(root, workers=2, **options)
            self.assertEqual(start["data_dir"], str(root.resolve()))
            self.assertEqual(
                {s["service"] for s in start["services"]}, set(season._SERVICES)
            )
            # A first-use catalog can commit new schedule evidence during each
            # verification attempt. Exercise the next scheduled poll inside the window.
            forecast_poll = next(s for s in start["services"] if s["service"] == "forecast")
            when = store.instant(forecast_poll["next_at"])
            self.assertLess(when, football.CUT)
            with closing(season._db(root)) as db:
                first = db.execute(
                    "SELECT state,error,available,digest FROM forecast_dispatch WHERE fixture_id=? "
                    "AND horizon='T-60m' AND mode='formal' AND current_schedule=1",
                    (football.FID,),
                ).fetchone()
            self.assertIsNotNone(first, forecast_poll)
            if first["state"] != "done":
                self.assertEqual(first["state"], "error", dict(first))
                self.assertEqual(
                    first["error"],
                    "ValueError: schedule evidence changed repeatedly before the fixed B1 decision",
                )
                self.assertIsNone(first["digest"])
                self.assertLessEqual(store.instant(first["available"]), when)
            with football.at(when):
                next_tick = season.run(root, workers=2, **options)
            with closing(season._db(root)) as db:
                seals = db.execute(
                    "SELECT data_json,digest,sealed_at FROM forecast_seals WHERE fixture_id=? AND horizon='T-60m' "
                    "AND mode='formal'",
                    (football.FID,),
                ).fetchall()
                self.assertEqual(len(seals), 1, next_tick["services"])
                seal = seals[0]
                payload = json.loads(seal["data_json"])
                self.assertLessEqual(store.instant(seal["sealed_at"]), football.CUT)
                self.assertEqual(seal["digest"], store.digest(payload))
                if first["state"] == "done":
                    self.assertEqual(seal["digest"], first["digest"])
                fair = payload["probabilities"]["total_over_2_5"]
                price = (Decimal(str(fair)) - Decimal(".05")).quantize(
                    Decimal(".01"), rounding=ROUND_FLOOR
                )
                self.assertTrue(
                    db.execute(
                        "SELECT 1 FROM ma_requests WHERE operation='catalog' AND selector='epl-week' "
                        "AND status='ok'"
                    ).fetchone()
                )

            coordinator = season.Coordinator(root, workers=2, **options)

            def advance(now):
                nonlocal when
                when = now
                with football.at(when):
                    for _ in range(30):
                        new = coordinator.tick()
                        if not coordinator.futures and not new:
                            return
                        _, pending = wait(list(coordinator.futures), timeout=10)
                        self.assertFalse(pending, "matchday task stalled")
                self.fail("matchday tasks did not drain")

            try:
                advance(football.CUT - timedelta(seconds=24))
                with closing(season._db(root)) as db:
                    summary = json.loads(
                        db.execute(
                            "SELECT detail FROM season_slots WHERE role='after_decision_book' "
                            "AND state='done'"
                        ).fetchone()[0]
                    )
                for account in ("formal", "shadow"):
                    self.assertEqual(summary[account]["frozen"], 1, summary)
                    self.assertEqual(summary[account]["as_of"], store.iso(when))
                advance(football.CUT - timedelta(seconds=3))
                advance(football.CUT + timedelta(seconds=6))
                advance(football.KICK - timedelta(seconds=9))
                advance(football.KICK + timedelta(seconds=1))
            finally:
                with football.at(when):
                    coordinator.close()
        with closing(season._db(root)) as db:
            result = db.execute(
                "SELECT state,detail FROM season_slots WHERE role='execution_book' AND target=?",
                (store.iso(football.CUT + timedelta(seconds=5)),),
            ).fetchone()
            self.assertEqual(result["state"], "done", result["detail"])
            self.assertEqual(json.loads(result["detail"])["requested_tokens"], ["711"])
            self.assertTrue(
                db.execute(
                    "SELECT 1 FROM ma_books WHERE token='711' AND received_at=?",
                    (store.iso(football.KICK - timedelta(seconds=9)),),
                ).fetchone()
            )
            self.assertTrue(
                db.execute(
                    "SELECT 1 FROM season_alerts WHERE role='results'"
                ).fetchone()
            )
            self.assertEqual(
                [
                    tuple(r)
                    for r in db.execute(
                        "SELECT role,reason,occurred FROM season_alerts WHERE role IN ('paper_formal','paper_shadow') "
                        "AND occurred<=?",
                        (store.iso(football.CUT + timedelta(seconds=6)),),
                    )
                ],
                [],
            )
            self.assertTrue(
                db.execute(
                    "SELECT 1 FROM season_alerts WHERE role='paper_formal' AND occurred>=?",
                    (store.iso(football.KICK),),
                ).fetchone()
            )
        with ledger.connection(root) as db:
            held = json.loads(
                db.execute(
                    "SELECT result FROM paper_intents WHERE account='main' AND status='filled'"
                ).fetchone()[0]
            )
            self.assertEqual(
                held["filled_at"], store.iso(football.CUT + timedelta(seconds=6))
            )
            # Only a scheduled kickoff is available in this fixture. Formal CLV needs independent proof.
            self.assertIsNone(held.get("close_mid"))
        with shadow.db_for(root) as db:
            research = json.loads(
                db.execute(
                    "SELECT result FROM research_shadow_orders WHERE status='held'"
                ).fetchone()[0]
            )
            self.assertEqual(research["filled_at"], held["filled_at"])
            self.assertEqual(research["close_status"], "scheduled_kickoff")
