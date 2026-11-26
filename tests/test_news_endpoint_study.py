"""Separate forward registration for home-win and regulation full-match Over 2.5."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from scipy.stats import t

from polymarket861 import news_endpoint_study as trial
from polymarket861 import news_market, news_study_plan, pipeline, store
from test_news_accumulation import coverage
from test_news_matchday import AWAY, FID, HOME, KICKOFF, seed_fixture
from test_paired_proof import at


def fixed_sqlite_clock(stamp):
    # Keep the host clock out of stored fixture tests without weakening the
    # real enrollment trigger or its timestamp checks in production.
    clock = store.iso(stamp)
    trigger = trial._TRIGGER.replace(trial._CLOCK_SQL, f"'{clock}'").replace(
        "julianday('now')", f"julianday('{clock}')")
    return patch.object(trial, "_TRIGGER", trigger)


def raw_seal(db, fixture, now, *, horizon="T-60m", mode="technical", purpose="main", plan=None,
             era="a" * 64, season="2026-27", cutoff=None, input_cutoff=None, sealed_at=None):
    cut = (now + timedelta(seconds=20)) if cutoff is None else cutoff
    # Vary the synthetic fixture id while keeping all binding dimensions explicit.
    kick = cut + trial.HORIZONS[horizon]
    payload = {"purpose": purpose, "season": season, "fixture_id": fixture, "input_cutoff": store.iso(input_cutoff or now),
               "news_study": {"plan_sha256": plan or news_study_plan.PLAN_SHA256, "era_sha256": era}}
    digest = store.digest(payload)
    db.execute("INSERT INTO forecast_seals VALUES(?,?,?,?,?,?,?,?,?,?)",
               (fixture, store.iso(kick), store.iso(cut), horizon, "B1", store.PROTOCOL, mode,
                store.iso(sealed_at or now), digest, store.canonical(payload)))
    return digest


class NewsEndpointStudy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_empty_report_does_not_create_db_and_is_explicit_for_each_clock(self):
        report = trial.report(self.root)
        self.assertEqual(report["state"], "not_registered")
        self.assertEqual(report["family_comparisons"], 10)
        self.assertFalse((self.root / "runtime.sqlite3").exists())
        for horizon in trial.HORIZONS:
            self.assertEqual(report["horizons"][horizon]["score_guardrail"]["pairs"], 0)
            for field in trial.CLOCKS:
                pair = report["horizons"][horizon]["clocks"][field]["full_over_2_5"]
                self.assertEqual(pair["status"], "no_matched_endpoint_pairs")
                self.assertIsNone(pair["slope"])
                self.assertIsNone(pair["interval"])

    def test_trigger_is_atomic_prospective_and_does_not_relabel_existing_seals(self):
        now = store.instant("2026-12-01T12:00:00Z")
        sqlite_clock = fixed_sqlite_clock(now)
        sqlite_clock.start()
        self.addCleanup(sqlite_clock.stop)
        with closing(store.connect(self.root)) as db, db:
            raw_seal(db, "epl/2026-27/previous/away", now - timedelta(seconds=5))
        registered = now - timedelta(seconds=3)
        with patch.object(store, "now_utc", return_value=store.iso(registered)):
            cert = trial.enroll(self.root)
            self.assertEqual(cert, trial.enroll(self.root))
            with self.assertRaisesRegex(ValueError, "immutable"):
                trial.enroll(self.root, mode="formal")
        with closing(store.connect(self.root)) as db, db:
            primary = raw_seal(db, "epl/2026-27/first/away", now)
            raw_seal(db, "epl/2026-27/old/away", now - timedelta(days=1))
            raw_seal(db, "epl/2026-27/replay/away", now, mode="replay")
            raw_seal(db, "epl/2026-27/formal/away", now, mode="formal")
            raw_seal(db, "epl/2026-27/shadow/away", now, purpose="shadow")
            raw_seal(db, "epl/2026-27/wrongplan/away", now, plan="0" * 64)
            raw_seal(db, "lal/2026-27/first/away", now)
            raw_seal(db, "epl/2026-27/oldinput/away", now, input_cutoff=registered - timedelta(seconds=1))
            raw_seal(db, "epl/2026-27/future/away", now, sealed_at=now + timedelta(minutes=1))
            rows = db.execute("SELECT seal,declaration FROM news_endpoint_bindings").fetchall()
            self.assertEqual([tuple(r) for r in rows], [(primary, cert["registration_sha256"])])
            db.execute("DROP TRIGGER news_endpoint_bind_on_insert")
        with self.assertRaisesRegex(ValueError, "trigger"):
            trial.report(self.root)

    def test_registration_refuses_historical_or_expired_start_and_changed_manifest(self):
        now = store.instant("2026-11-26T12:00:00Z")
        with patch.object(store, "now_utc", return_value=store.iso(now)):
            for value in (now - timedelta(seconds=1), store.instant(trial.END)):
                with self.assertRaisesRegex(ValueError, "Start"):
                    trial.enroll(self.root, starts_at=value)
            trial.enroll(self.root, starts_at=now + timedelta(hours=1))
        with closing(store.connect(self.root)) as db, db:
            db.execute("UPDATE news_endpoint_declarations SET mode='formal'")
        with self.assertRaisesRegex(ValueError, "declaration"):
            trial.report(self.root)

    def test_both_slopes_have_distinct_book_families_and_tenfold_simultaneous_intervals(self):
        rows = []
        movements = [(0.04, 0.03), (-0.02, -0.012), (0.01, 0.025), (-0.04, -0.03)]
        for rnd, (x, y) in enumerate(movements, 1):
            outcomes = {"home_win": news_market._measure(0.4, 0.4 + x, 0.4, 0.4 + y),
                        "total_over_2_5": news_market._measure(0.55, 0.55 - x, 0.55, 0.55 + y)}
            rows.append({"season": "2026-27", "round_id": rnd, "implementation": "a",
                         "market_close": {"endpoints": outcomes}, "scheduled_market_close": {"reason": "no_native_batch"}})
        home = trial._cell(rows, "market_close", "home_win", True)
        total = trial._cell(rows, "market_close", "full_over_2_5", True)
        self.assertAlmostEqual(home["slope"], -total["slope"])
        self.assertEqual(total["interval"]["family_comparisons"], 10)
        self.assertAlmostEqual(total["interval"]["critical_t"], t.ppf(1 - .025 / 10, 3))
        self.assertIsNone(trial._cell(rows, "market_close", "full_over_2_5", False)["slope"])
        self.assertEqual(trial._cell(rows, "scheduled_market_close", "full_over_2_5", True)["missing"],
                         {"no_native_batch": 4})
        for row in rows:
            row.update(study_status="declared", study_plan_sha256=news_study_plan.PLAN_SHA256)
        legacy = news_market.regression(rows)
        self.assertEqual(legacy["interval"]["family_comparisons"], 6)
        self.assertAlmostEqual(home["slope"], legacy["slope"])
        self.assertGreater(total["interval"]["critical_t"], legacy["interval"]["critical_t"])

    def test_native_forecast_transaction_binds_and_missing_books_stay_missing(self):
        root, history = self.root / "runtime", self.root / "history"
        kickoff = store.instant(KICKOFF)
        cutoff = kickoff - timedelta(minutes=61)
        snapshot = {"fixture_id": FID, "cutoff": store.iso(cutoff), "facts": [],
                    "coverage": coverage(HOME, AWAY, cutoff)}
        with fixed_sqlite_clock(cutoff):
            with at("2026-08-01T10:00:00Z"):
                seed_fixture(root, history)
                trial.enroll(root)
            with at(cutoff), patch("polymarket861.news.snapshot", return_value=snapshot):
                seal = pipeline.make_forecast(root, FID, history)
            with at(kickoff + timedelta(minutes=1)):
                result = trial.report(root)
            self.assertEqual(result["bound_seals"], 1)
            self.assertEqual(result["initial_bound_probability_era"], seal["news_study"]["era_sha256"])
            h = result["horizons"]["T-60m"]
            self.assertEqual(h["qualified_news_pairs"], 1, h["exclusions"])
            for field in trial.CLOCKS:
                cell = h["clocks"][field]["full_over_2_5"]
                self.assertEqual(cell["matched_books"], 0)
                self.assertEqual(sum(cell["missing"].values()), 1)
                self.assertIsNone(cell["slope"])

    def test_pending_estimates_prior_to_reveal_and_late_first_whistle_or_result(self):
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE football_kickoff_receipts(proof_id,fixture_id,received_at)")
            db.execute("INSERT INTO football_kickoff_receipts VALUES('p','epl/x',?)", (trial.END,))
            current = {"fixture_id": "epl/x", "scored": True, "result_confirmed_at": trial.END,
                       "market_close": {"kickoff_proof": "p", "endpoints": {"home_win": {"close": .5}}},
                       "scheduled_market_close": {"reason": "missing_pregame_source"}}
            gated = trial._receipts_before_end(db, current)
            self.assertFalse(gated["scored"])
            self.assertEqual(gated["market_close"]["reason"], "actual_kickoff_proof_missing_by_study_end")
            self.assertEqual(gated["scheduled_market_close"], current["scheduled_market_close"])
            self.assertTrue(current["scored"])
        values = [{"scored": True, "season": "2026-27", "round_id": r,
                   "scores": {"no_news": {"score": 71}, "with_news": {"score": 69 + .01 * r}}} for r in range(1, 6)]
        self.assertIsNone(trial._guardrail(values, False)["interval"])
        end = trial._guardrail(values, True)
        self.assertEqual(end["interval"]["family_comparisons"], 10)
        self.assertEqual(end["status"], "evidence_of_score_deterioration")
