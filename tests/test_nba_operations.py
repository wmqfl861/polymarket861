from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from polymarket861.nba import data, markets, model, operations as ops
from polymarket861.nba.operations_report import _difference_interval, report
from polymarket861.store import instant, iso


class OperationsTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory())) / "nba"
        self.tip = instant("2026-11-25T02:00:00Z")  # Nov 24, Eastern
        self.now = self.tip - timedelta(minutes=63)
        self.rows = []
        self.add("scheduled", self.tip - timedelta(days=2))
        stack.enter_context(
            patch.object(
                data,
                "schedule_proof",
                create=True,
                side_effect=lambda root, day, as_of: {
                    "state": "ok",
                    "complete": True,
                    "began": iso(instant(as_of) - timedelta(seconds=30)),
                    "finished": as_of,
                    "start": day,
                    "end": day,
                },
            )
        )
        stack.enter_context(
            patch.object(data, "game_versions", side_effect=self.versions)
        )
        stack.enter_context(patch.object(model, "predict", side_effect=self.predict))
        stack.enter_context(
            patch.object(markets, "benchmark", side_effect=self.benchmark)
        )
        stack.enter_context(
            patch.object(markets, "fixture_conflict", create=True, return_value=None)
        )
        self.families = {
            "win": None,
            "total": 220.5,
            "home_total": 110.5,
            "away_total": 109.5,
        }

    def add(self, status, at, **edits):
        base = (
            dict(self.rows[-1])
            if self.rows
            else {
                "game_id": "bdl:5",
                "kickoff": iso(self.tip),
                "season": 2026,
                "stage": "regular",
                "game_date": "2026-11-24",
                "home": "bdl:1",
                "away": "bdl:2",
                "home_abbr": "BOS",
                "away_abbr": "LAL",
                "home_score": None,
                "away_score": None,
            }
        )
        v = len(self.rows) + 1
        self.rows.append(
            dict(
                base,
                status=status,
                available_at=iso(at),
                received_at=iso(at),
                version=v,
                receipt_sha256=f"{v:064x}",
                **{},
            )
            | edits
        )

    def versions(
        self, root, *, game_id=None, as_of=None, since=None, source="balldontlie"
    ):
        self.assertEqual(source, "balldontlie")
        return [
            r.copy()
            for r in self.rows
            if (game_id is None or r["game_id"] == game_id)
            and r["available_at"] <= as_of
            and (since is None or r["available_at"] >= since)
        ]

    def predict(self, history, game, *, lines, as_of):
        self.assertEqual(game["status"], "scheduled")
        self.assertTrue(
            all(g["stage"] == "regular" and g["available_at"] <= as_of for g in history)
        )
        dist = {"values": [90, 110, 116], "counts": [1, 1, 2]}
        pred = {
            "p_home_win": 0.7,
            "p_away_win": 0.3,
            **{k: {"distribution": dist} for k in ("total", "home", "away")},
        }
        return {
            "models": {name: pred for name in ("N0", "N1", "N2")},
            "scope": "final_including_overtime",
        }

    def benchmark(
        self, root, home, away, kickoff, horizon, *, as_of, source_epoch_at=None
    ):
        self.assertEqual((home, away), ("BOS", "LAL"))
        tip = instant(kickoff)
        cutoff = tip - timedelta(minutes=ops.HORIZONS[horizon])
        obs = iso(cutoff - timedelta(minutes=4))
        if instant(obs) > instant(as_of):
            return {}
        closed = iso(tip - timedelta(seconds=5))
        return {
            key: dict(
                condition_id=key + "-id",
                token_id=key + "-token",
                line=line,
                probability=0.6,
                available_at=obs,
                observed_at=obs,
                close_probability=0.55 if closed <= as_of else None,
                close_at=closed if closed <= as_of else None,
            )
            for key, line in self.families.items()
        }

    def cycle(self):
        return ops.forecast_cycle(self.root, clock=lambda: self.now)

    def windows(self):
        return ops.forecast_show(self.root, "bdl:5")["windows"]

    def test_seal_grade_correction_restore_and_weekly_revisions(self):
        self.assertEqual(self.cycle()["sealed"], 1)
        self.assertEqual(self.cycle()["sealed"], 0)
        w = next(r for r in self.windows() if r["state"] == "sealed")
        body = w["prediction"]
        self.assertEqual(set(body["market_at_prediction"]), set(self.families))
        self.assertFalse(
            any("close_at" in r for r in body["market_at_prediction"].values())
        )
        self.assertEqual(body["input_cutoff"], iso(self.now))
        self.now = self.tip + timedelta(hours=3)
        self.add("final", self.now, home_score=112, away_score=108)
        self.cycle()
        first = report(self.root, "week", "2026-11-23")
        grade = first["report"]["horizons"]["T-60m"]
        self.assertEqual(grade["grades"], {"scored": 1})
        self.assertEqual(grade["readiness"]["cold_or_unverified_sealed_games"], 1)
        self.assertEqual(grade["readiness"]["qualifying_win_quote_games"], 0)
        self.assertEqual(grade["readiness"]["all_seals_win_pairing_rate"], 1)
        self.assertEqual(grade["models"]["N0"]["games"], 1)
        self.assertAlmostEqual(grade["models"]["N2"]["brier_win"], 0.09)
        self.assertEqual(set(grade["market_comparisons"]), set(self.families))
        for family in self.families:
            self.assertEqual(
                grade["market_comparisons"][family]["N2"]["closing_games"], 1
            )
        self.now += timedelta(minutes=3)
        self.add("final", self.now, home_score=112, away_score=118)
        self.cycle()
        second = report(self.root, "week", "2026-11-23")
        self.assertGreater(second["revision"], first["revision"])
        self.assertAlmostEqual(
            second["report"]["horizons"]["T-60m"]["models"]["N2"]["brier_win"], 0.49
        )
        self.assertEqual(
            first, report(self.root, "week", "2026-11-23", first["revision"])
        )
        self.now += timedelta(minutes=2)
        self.add("unknown", self.now, home_score=None, away_score=None)
        self.cycle()
        self.assertEqual(
            report(self.root, "day", "2026-11-24")["report"]["horizons"]["T-60m"][
                "models"
            ],
            {},
        )
        self.now += timedelta(minutes=2)
        self.add("final", self.now, home_score=112, away_score=123)
        self.cycle()
        revised = next(r for r in self.windows() if r["sealed_at"])
        self.assertEqual(
            (revised["sha256"], revised["prediction"]), (w["sha256"], body)
        )
        self.assertEqual(
            [g["grade"]["state"] for g in revised["grade_history"]],
            ["awaiting_result", "scored", "scored", "awaiting_result", "scored"],
        )
        self.assertEqual(self.cycle()["report_versions"], 0)

    def test_late_schedule_and_restart_never_backfill(self):
        self.rows = []
        self.now = self.tip - timedelta(minutes=6)
        self.add("scheduled", self.now)
        self.assertEqual(self.cycle()["sealed"], 0)
        coverage = report(self.root, "day", "2026-11-24")["report"]["horizons"]["T-60m"]
        self.assertEqual(
            coverage["missed_by_reason"], {"schedule_first_observed_after_cutoff": 1}
        )
        for w in self.windows():
            self.assertEqual(
                (w["state"], w["note"]),
                ("missed", "schedule_first_observed_after_cutoff"),
            )
        self.assertEqual(self.cycle()["sealed"], 0)

    def test_market_tick_can_follow_forecast_without_polluting_earlier_inputs(self):
        requested_at = self.now + timedelta(seconds=8)

        def delayed(*args, as_of):
            return (
                self.benchmark(*args, as_of=as_of)
                if instant(as_of) >= requested_at
                else {}
            )

        with patch.object(markets, "benchmark", side_effect=delayed):
            first = self.cycle()
            self.assertEqual((first["sealed"], first["waiting_for_win_quote"]), (0, 1))
            self.assertEqual(
                next(w for w in self.windows() if w["horizon"] == "T-60m")["note"],
                "waiting_for_win_quote",
            )
            self.now += timedelta(minutes=1)
            self.assertEqual(self.cycle()["sealed"], 1)
        self.assertEqual(self.windows()[1]["prediction"]["input_cutoff"], iso(self.now))
        self.assertEqual(
            set(self.windows()[1]["prediction"]["market_at_prediction"]),
            set(self.families),
        )

    def test_last_tick_seals_without_price_and_separate_deadline_receipt_counts_the_miss(
        self,
    ):
        from polymarket861.nba import warmup

        deadline = self.tip - timedelta(minutes=60)
        quote_at = deadline - timedelta(seconds=9)

        def delayed(*args, as_of):
            if instant(as_of) < quote_at:
                return {}
            ref = self.benchmark(*args, as_of=as_of)["win"]
            return {
                "win": dict(ref, available_at=iso(quote_at), observed_at=iso(quote_at))
            }

        with (
            patch.object(markets, "benchmark", side_effect=delayed),
            patch.object(
                warmup, "coverage", return_value={"eligible": True, "policy": "fixture"}
            ),
        ):
            self.now = deadline - timedelta(seconds=70)
            self.assertEqual(self.cycle()["sealed"], 1)
            seal = self.windows()[1]["sha256"]
            self.assertEqual(
                self.windows()[1]["prediction"]["market_at_prediction"], {}
            )
            self.now = deadline + timedelta(seconds=5)
            self.cycle()
            for period, start in (("day", "2026-11-24"), ("week", "2026-11-23")):
                h = report(self.root, period, start)["report"]["horizons"]["T-60m"]
                r = h["readiness"]
                self.assertEqual(
                    (
                        r["due_games"],
                        r["warm_sealed_games"],
                        r["qualifying_win_quote_games"],
                        r["paired_win_games"],
                    ),
                    (1, 1, 1, 0),
                )
                self.assertEqual(r["win_pairing_rate"], 0)
                self.assertEqual(r["fixtures"][0]["game_id"], "bdl:5")
            self.now = self.tip + timedelta(hours=3)
            self.add("final", self.now, home_score=112, away_score=108)
            self.cycle()
            row = self.windows()[1]
            self.assertEqual(row["sha256"], seal)
            self.assertEqual(row["grade_history"][-1]["grade"]["benchmarks"], {})

    def test_quote_must_arrive_by_both_sources_deadlines(self):
        as_of = self.tip - timedelta(minutes=60)
        venue = self.tip - timedelta(minutes=10)
        quote = {
            "available_at": iso(as_of),
            "observed_at": iso(as_of),
            "venue_kickoff": iso(venue),
        }
        self.assertEqual(
            ops._observed_markets({"win": quote}, iso(as_of), iso(self.tip), "T-60m"),
            {},
        )
        quote["available_at"] = quote["observed_at"] = iso(
            venue - timedelta(minutes=60)
        )
        self.assertEqual(
            ops._observed_markets({"win": quote}, iso(as_of), iso(self.tip), "T-60m"),
            {},
        )
        quote["available_at"] = quote["observed_at"] = iso(
            venue - timedelta(minutes=60, microseconds=1)
        )
        self.assertEqual(
            ops._observed_markets({"win": quote}, iso(as_of), iso(self.tip), "T-60m"),
            {},
        )
        # A two-minute difference leaves three shared minutes; a book before
        # the later opening, or before a replacement was received, cannot pair.
        quote["venue_kickoff"] = iso(self.tip - timedelta(minutes=2))
        shared_open = as_of - timedelta(minutes=5)
        quote["observed_at"] = iso(shared_open - timedelta(microseconds=1))
        quote["available_at"] = iso(shared_open)
        self.assertEqual(
            ops._observed_markets({"win": quote}, iso(as_of), iso(self.tip), "T-60m"),
            {},
        )
        quote["observed_at"] = iso(shared_open)
        self.assertIn(
            "win",
            ops._observed_markets({"win": quote}, iso(as_of), iso(self.tip), "T-60m"),
        )
        epoch = iso(shared_open + timedelta(microseconds=1))
        self.assertEqual(
            ops._observed_markets(
                {"win": quote}, iso(as_of), iso(self.tip), "T-60m", epoch
            ),
            {},
        )

    def test_tampered_pairing_assessment_never_enters_readiness(self):
        self.cycle()
        self.now = self.tip - timedelta(minutes=59)
        self.cycle()
        with closing(ops._open(self.root)) as db, db:
            db.execute(
                "UPDATE nba_pairing_outcomes SET win_quote=NULL WHERE horizon='T-60m'"
            )
            ops._dirty_day(db, "2026-11-24")
        with self.assertRaisesRegex(ValueError, "assessment checksum mismatch"):
            self.cycle()

    def test_failed_schedule_is_a_named_miss_in_both_reports(self):
        with patch.object(
            data,
            "schedule_proof",
            return_value={"state": "source_quality", "complete": False},
        ):
            self.assertEqual(self.cycle()["state"], "partial")
            self.now = self.tip - timedelta(minutes=59)
            self.cycle()
        for period, start in (("day", "2026-11-24"), ("week", "2026-11-23")):
            h = report(self.root, period, start)["report"]["horizons"]["T-60m"]
            self.assertEqual(h["missed_by_reason"], {"latest_schedule_sync_failed": 1})

    def test_only_a_large_time_difference_seals_without_pairing(self):
        match = {
            "status": "outside_tolerance",
            "reason": "venue_same_pair_schedule_mismatch",
            "source_kickoff": iso(self.tip),
            "venue_kickoff": iso(self.tip + timedelta(hours=3)),
            "delta_seconds": 10800,
            "venue_event_id": "41",
            "venue_version": 1,
        }
        with (
            patch.object(markets, "schedule_match", return_value=match),
            patch.object(markets, "benchmark", return_value={}),
        ):
            self.assertEqual(self.cycle()["sealed"], 1)
        sealed = self.windows()[1]["prediction"]
        self.assertEqual(sealed["market_schedule"], match)
        self.assertEqual(sealed["market_at_prediction"], {})

    def test_unregistered_game_becomes_due_without_a_source_revision(self):
        self.rows = []
        self.add("scheduled", self.now, kickoff=None)
        self.cycle()
        h = report(self.root, "day", "2026-11-24")["report"]["horizons"]["T-60m"]
        self.assertEqual(h["readiness"]["due_games"], 0)
        self.now += timedelta(days=1)
        self.cycle()
        h = report(self.root, "day", "2026-11-24")["report"]["horizons"]["T-60m"]
        self.assertEqual(
            (h["readiness"]["due_games"], h["readiness"]["sealed_games"]), (1, 0)
        )

    def test_catalogued_games_without_a_regular_schedule_raise_an_alert(self):
        from test_nba_markets import Venue

        venue = Venue()
        self.now = venue.now
        self.rows = []
        with patch.object(markets.api, "get", side_effect=venue.get):
            markets.collect(self.root, now=venue.clock)
        day = self.cycle()
        self.assertEqual(
            (day["state"], day["reason"]),
            ("partial", "market_catalog_without_regular_schedule"),
        )
        self.assertEqual(day["catalogue_coverage"]["catalogued_games"], 1)
        self.add(
            "scheduled",
            self.now,
            game_date="2026-11-27",
            stage="preseason",
            home_abbr="NYK",
            away_abbr="MIA",
        )
        self.assertEqual(self.cycle()["catalogue_coverage"]["catalogued_games"], 0)

    def test_watchdog_can_health_check_market_only(self):
        with patch.object(markets, "collect", return_value={"state": "ok"}):
            ops.run(self.root, "markets", clock=lambda: self.now)
        self.assertEqual(
            ops.status(self.root, clock=lambda: self.now)["state"], "degraded"
        )
        health = ops.status(self.root, component="markets", clock=lambda: self.now)
        self.assertEqual(health["state"], "ok")
        self.assertEqual(list(health["components"]), ["markets"])
        self.now += timedelta(minutes=4)
        self.assertEqual(
            ops.status(self.root, component="markets", clock=lambda: self.now)["state"],
            "degraded",
        )

    def test_crossing_cutoff_while_computing_never_seals(self):
        def slow(*args, **kwargs):
            self.now += timedelta(minutes=5)
            return self.predict(*args, **kwargs)

        with patch.object(model, "predict", side_effect=slow):
            r = self.cycle()
        self.assertEqual(r["sealed"], 0)
        self.assertIn("deadline passed", r["fixture_errors"][0]["error"])
        self.assertEqual(
            next(w for w in self.windows() if w["horizon"] == "T-60m")["state"],
            "missed",
        )

    def test_schedule_A_B_A_produces_separate_epochs(self):
        self.cycle()
        self.now += timedelta(seconds=20)
        self.add("scheduled", self.now, kickoff=iso(self.tip + timedelta(hours=2)))
        self.now += timedelta(seconds=20)
        self.add("scheduled", self.now, kickoff=iso(self.tip))
        epoch_announced = iso(self.now)
        self.assertEqual(self.cycle()["waiting_for_win_quote"], 1)
        self.now = self.tip - timedelta(minutes=61)
        self.assertEqual(self.cycle()["sealed"], 1)
        rows = [r for r in self.windows() if r["horizon"] == "T-60m"]
        self.assertEqual(
            [(r["epoch"], r["active"], r["state"]) for r in rows],
            [(1, 0, "sealed"), (2, 0, "superseded"), (3, 1, "sealed")],
        )
        self.assertEqual(
            rows[0]["grade_history"][-1]["grade"]["state"], "withdrawn_schedule"
        )
        self.assertEqual(rows[2]["prediction"]["source_epoch_at"], epoch_announced)
        self.assertFalse(rows[2]["prediction"]["market_at_prediction"])
        self.now = self.tip - timedelta(minutes=59)
        self.cycle()
        r = report(self.root, "day", "2026-11-24")["report"]["horizons"]["T-60m"]
        self.assertEqual(r["opportunities"], 1)
        self.assertEqual(r["superseded_states"]["sealed"], 1)
        self.assertEqual(r["readiness"]["all_seals_qualifying_win_quote_games"], 0)

    def test_schedule_withdrawn_between_prediction_and_commit(self):
        def cancelled(*args, **kwargs):
            self.add("postponed", self.now)
            return self.predict(*args, **kwargs)

        with patch.object(model, "predict", side_effect=cancelled):
            self.assertEqual(self.cycle()["sealed"], 0)
        self.cycle()
        self.assertTrue(all(not w["active"] for w in self.windows()))

    def test_concurrent_seal_is_idempotent_and_checksum_detects_tampering(self):
        with ThreadPoolExecutor(max_workers=2) as p:
            results = list(p.map(lambda _: self.cycle(), range(2)))
        self.assertEqual(sum(r["sealed"] for r in results), 1)
        with closing(ops._open(self.root)) as db, db:
            db.execute(
                "UPDATE nba_windows SET sealed_at='2025' WHERE sealed_at IS NOT NULL"
            )
        with self.assertRaisesRegex(ValueError, "metadata mismatch"):
            self.windows()

    def test_score_only_observed_game_stays_in_coverage(self):
        self.rows = []
        self.now = self.tip + timedelta(hours=3)
        self.add("final", self.now, home_score=112, away_score=108)
        self.cycle()
        r = report(self.root, "day", "2026-11-24")["report"]
        self.assertEqual((r["schedule_games"], r["without_registered_windows"]), (1, 1))
        self.assertEqual(r["horizons"]["T-60m"]["models"], {})

    def test_new_quotes_after_forecast_do_not_enter_paired_benchmarks(self):
        before = self.now

        def later(root, home, away, kickoff, horizon, *, as_of):
            ref = self.benchmark(root, home, away, kickoff, horizon, as_of=as_of)
            if instant(as_of) == before:
                ref.pop("away_total")
            else:
                ref["win"]["token_id"] = "other-token"
            return ref

        with patch.object(markets, "benchmark", side_effect=later):
            self.cycle()
            self.now = self.tip + timedelta(hours=3)
            self.add("final", self.now, home_score=112, away_score=108)
            self.cycle()
        week = report(self.root, "week", "2026-11-23")["report"]
        h = week["horizons"]["T-60m"]
        self.assertEqual(
            h["market_coverage"]["away_total"]["missing_decision_quote"], 1
        )
        self.assertEqual(h["market_comparisons"]["away_total"], {})
        self.assertEqual(h["market_coverage"]["win"]["decision_without_close"], 1)
        self.assertEqual(h["market_coverage"]["total"]["with_same_token_close"], 1)

    def test_incomplete_or_stale_schedule_proof_does_not_seal(self):
        for proof, reason in [
            (None, "missing"),
            ({"state": "running", "complete": False}, "incomplete"),
            (
                {
                    "state": "ok",
                    "complete": True,
                    "began": iso(self.now - timedelta(hours=1)),
                    "finished": iso(self.now - timedelta(minutes=55)),
                },
                "stale",
            ),
        ]:
            with (
                self.subTest(reason=reason),
                patch.object(data, "schedule_proof", return_value=proof),
            ):
                run = self.cycle()
                self.assertEqual(run["sealed"], 0)
                self.assertIn(reason, run["fixture_errors"][0]["error"])
        self.assertEqual(self.cycle()["sealed"], 1)

    def test_unchanged_feed_uses_incremental_cursor_and_skips_training(self):
        self.cycle()
        self.now += timedelta(seconds=1)
        with (
            patch.object(data, "game_versions", side_effect=self.versions) as reads,
            patch.object(model, "predict") as train,
        ):
            stats = self.cycle()
        self.assertEqual(stats["grade_revisions"], 0)
        self.assertEqual(stats["report_versions"], 0)
        train.assert_not_called()
        self.assertEqual(len(reads.call_args_list), 1)
        self.assertEqual(
            reads.call_args.kwargs["since"], iso(self.now - timedelta(seconds=1))
        )

    def test_difference_interval_resamples_days_and_weights_games(self):
        small = _difference_interval({"day": [0.7, 7]})
        self.assertIsNone(small["interval"])
        self.assertEqual(small["reason"], "too_few_paired_game_days")
        clusters = {"a": [-2, 4], "b": [-1, 2], "c": [-0.5, 1], "d": [-5, 10]}
        self.assertEqual(_difference_interval(clusters)["interval"], [-0.5, -0.5])
        clusters["a"] = [4, 4]
        actual = _difference_interval(clusters)
        self.assertEqual(actual, _difference_interval(dict(reversed(clusters.items()))))
        self.assertEqual(actual["matched_days"], 4)
        self.assertLess(actual["interval"][0], actual["interval"][1])

    def test_market_reports_play_during_computation(self):
        with patch.object(
            markets, "fixture_conflict", side_effect=[None, "venue_observed_start"]
        ):
            r = self.cycle()
        self.assertEqual(r["sealed"], 0)
        self.assertIn("market schedule conflict", r["fixture_errors"][0]["error"])

    def test_candidate_commit_crosses_deadline_and_restart_only_preserves_unadmitted_bytes(
        self,
    ):
        open_real = ops._open

        def open_trace(root):
            db = open_real(root)
            candidate = False

            def trace(sql):
                nonlocal candidate
                if "INSERT OR IGNORE INTO nba_forecast_candidates" in sql:
                    candidate = True
                if candidate and sql == "COMMIT":
                    self.now = self.tip - timedelta(minutes=59)
                    candidate = False

            db.set_trace_callback(trace)
            return db

        with patch.object(ops, "_open", side_effect=open_trace):
            self.assertEqual(self.cycle()["sealed"], 0)
        self.assertEqual(self.cycle()["sealed"], 0)
        self.assertTrue(all(not r["sealed_at"] for r in self.windows()))
        with closing(open_real(self.root)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM nba_forecast_candidates").fetchone()[
                    0
                ],
                1,
            )

    def test_bdl_contradictory_score_then_reschedule_does_not_revive_game(self):
        self.add("unknown", self.now, play_observed=True)
        self.now += timedelta(seconds=10)
        self.add(
            "scheduled",
            self.now,
            kickoff=iso(self.tip + timedelta(seconds=10)),
            play_observed=False,
        )
        result = self.cycle()
        self.assertEqual(result["sealed"], 0)
        current = [r for r in self.windows() if r["active"]]
        self.assertTrue(current and all(r["state"] == "withheld" for r in current))

    def test_services_continue_when_one_source_fails(self):
        self.assertEqual(
            ops.status(self.root)["components"]["schedule"]["state"], "not_started"
        )
        self.assertFalse(self.root.exists())
        with (
            patch.object(markets, "collect", return_value={"state": "ok"}),
            patch.object(data, "sync_bdl", return_value={"state": "missing_key"}),
        ):
            result = ops.run(self.root, clock=lambda: self.now)
        self.assertEqual(result["components"]["schedule"]["state"], "missing_key")
        self.assertEqual(result["components"]["forecast"]["sealed"], 0)
        self.assertEqual(
            ops.status(self.root, clock=lambda: self.now)["state"], "degraded"
        )
        with patch.object(data, "sync_bdl", return_value={"state": "ok"}):
            ops.run(self.root, "schedule", clock=lambda: self.now)
        recovered = ops.run(self.root, "forecast", clock=lambda: self.now)
        self.assertEqual(recovered["components"]["forecast"]["sealed"], 1)
        with closing(ops._open(self.root)) as db, db:
            db.execute(
                "UPDATE nba_cycles SET state='running' WHERE component='forecast'"
            )
        self.assertEqual(ops.status(self.root, clock=lambda: self.now)["state"], "ok")
        self.now += timedelta(minutes=5)
        health = ops.status(self.root, clock=lambda: self.now)
        self.assertEqual(health["components"]["forecast"]["state"], "stale")
        self.assertEqual(health["components"]["schedule"]["state"], "ok")


class PreviewAndSealTests(unittest.TestCase):
    def test_direct_sync_supplies_real_proof_and_N2_equals_same_cutoff_preview(self):
        from test_nba_data import TEST_KEY, game, page

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"BALLDONTLIE_API_KEY": TEST_KEY}),
        ):
            root = Path(tmp) / "nba"
            now = instant("2021-11-10T00:27:00Z")

            def clock():
                return now

            def advance(seconds):
                nonlocal now
                now += timedelta(seconds=seconds)

            games = []
            for i in range(40):
                day = date(2021, 9, 25) + timedelta(days=i)
                games.append(
                    game(
                        100 + i,
                        date=str(day),
                        datetime=str(day) + "T22:00:00Z",
                        status_state="final",
                        status="Final",
                        period=4,
                        home_team_score=101 + i % 15,
                        visitor_team_score=90 + i % 8,
                    )
                )
            games.extend(
                [
                    game(999),
                    game(
                        998,
                        date="2021-11-09",
                        datetime="2021-11-10T02:00:00Z",
                        visitor_team={
                            "id": 4,
                            "abbreviation": "CHI",
                            "full_name": "Chicago Bulls",
                        },
                    ),
                ]
            )
            r = data.sync_bdl(
                root,
                "2021-09-25",
                "2021-11-10",
                fetch=lambda params, key: page(
                    games if params["season_type"] == "regular" else []
                ),
                clock=clock,
                sleep=advance,
            )
            self.assertTrue(r["complete"])
            now = instant("2021-11-10T00:29:00Z")
            sealed = ops.forecast_cycle(root, clock=clock)
            self.assertEqual(sealed["sealed"], 1, sealed)
            record = next(
                r
                for r in ops.forecast_show(root, "bdl:999")["windows"]
                if r["sealed_at"]
            )["prediction"]
            preview = model._predict_command(
                SimpleNamespace(
                    data_dir=root,
                    as_of=iso(now),
                    game_id="bdl:999",
                    total_line=None,
                    home_line=None,
                    away_line=None,
                )
            )
            self.assertEqual(
                {k: preview[k] for k in record["forecast"]}, record["forecast"]
            )
            self.assertEqual(
                preview["preview_eligibility"], "verified_schedule_preview"
            )
            self.assertFalse(preview["sealed"])
            self.assertFalse(preview["forward_scoring_eligible"])
            self.assertEqual(record["schedule_verification"]["scan_id"], r["scan_id"])
