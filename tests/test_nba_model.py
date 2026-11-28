import argparse
import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from datetime import date, timedelta

from polymarket861.nba import model, model_replay


def game(i, start=date(2010, 10, 1), season=2010):
    day = (start + timedelta(days=i)).isoformat()
    return {"game_id": f"{season}-{i}", "game_date": day, "season": season, "source": "balldontlie",
            "stage": "regular", "status": "final", "home": "ABCD"[i % 4], "away": "CDAB"[i % 4],
            "neutral": False, "home_score": 102+i % 13, "away_score": 80+i % 9,
            "received_at": day+"T22:00:00Z", "available_at": day+"T22:01:00Z", "kickoff": day+"T18:00:00Z"}


def matchup(i=66):
    return {**game(i), "status": "scheduled", "home_score": None, "away_score": None,
            "received_at": "2010-11-15T12:00:00Z", "available_at": "2010-11-15T12:02:00Z"}


class NBAScoreModelTests(unittest.TestCase):
    def test_same_day_late_unknown_and_cross_source_rows_cannot_change_forecast(self):
        history = [game(i) for i in range(64)]
        fixture = matchup()
        cutoff = fixture["game_date"]+"T17:00:00Z"
        base = model.predict(history, fixture, as_of=cutoff)
        rejected = [
            {**game(64), "available_at": fixture["game_date"]+"T17:00:01Z"},
            {**game(65), "received_at": fixture["game_date"]+"T17:00:01Z"},
            {**game(61), "game_id": "impossible", "kickoff": fixture["kickoff"]},
            {**game(62), "game_id": "withdrawn", "status": "postponed"},
            {**game(63), "game_id": "no-receipt", "available_at": None},
            {**game(64), "game_id": "cross", "source": "538"},
            {**game(65), "game_id": "alpha1-ops-cache", "evidence_grade": "local_receipt"},
            {**game(65), "game_id": "unverified-phase", "evidence_grade": "local_receipt",
             "stage_basis": "query_filter"},
            {**game(65), "game_id": "archived", "evidence_grade": "final_archive_date_only"},
            {**game(64), "game_id": "playoff", "stage": "postseason"},
            {**game(66), "game_id": "same-day", "kickoff": fixture["game_date"]+"T08:00:00Z",
             "received_at": fixture["game_date"]+"T12:00:00Z", "available_at": fixture["game_date"]+"T12:01:00Z"},
            game(67), {**fixture, "status": "final", "home_score": 150, "away_score": 149},
        ]
        self.assertEqual(base, model.predict(reversed(history + rejected), fixture, as_of=cutoff))
        self.assertEqual(base["training_games"], len(history))
        self.assertEqual(base["evidence_grade"], "receipt_filtered")
        for invalid in ({**fixture, "kickoff": None}, {**fixture, "status": "final"},
                        {**fixture, "available_at": fixture["kickoff"]}, {**fixture, "source": "538"}):
            with self.assertRaises(ValueError):
                model.predict(history, invalid, as_of=cutoff)
        with self.assertRaisesRegex(ValueError, "precede"):
            model.predict(history, fixture, as_of=fixture["kickoff"])
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            model.predict(history, fixture, as_of=fixture["game_date"]+"T15:00:00")

    def test_only_received_schedule_changes_b2b_even_when_scores_pending(self):
        hist = [game(i) for i in range(59)]
        fixture = matchup()
        cutoff = fixture["game_date"]+"T12:00:00Z"
        pending = {**game(65), "home": fixture["home"], "away": "external", "status": "scheduled",
                   "away_score": None, "home_score": None}
        base = model.predict(hist, fixture, as_of=cutoff)
        changed = model.predict(hist+[pending], fixture, as_of=cutoff)
        self.assertEqual(base["models"]["N1"], changed["models"]["N1"])
        self.assertEqual(base["training_games"], changed["training_games"])
        self.assertLess(changed["models"]["N2"]["home"]["mean"], base["models"]["N2"]["home"]["mean"])
        self.assertNotEqual(base["training_view_sha256"], changed["training_view_sha256"])

    def test_joint_law_neutral_win_symmetry_and_integer_line_push(self):
        history = [game(i) for i in range(64)]
        fixture = {**matchup(), "neutral": True}
        base = model.predict(history, fixture, lines={"home": 105, "total": 190.5})
        self.assertEqual(base["models"]["N0"]["p_home_win"], 0.5)
        self.assertFalse(base["paper_eligible"])
        json.dumps(base, allow_nan=False)
        for arm in base["models"].values():
            pairs = arm["joint_distribution"]
            samples = [(h, a, n) for h, a, n in zip(pairs["home"], pairs["away"], pairs["counts"], strict=True)]
            n = sum(w for _, _, w in samples)
            self.assertEqual(n, arm["joint_samples"])
            self.assertTrue(all(h != a and min(h, a) >= 0 for h, a, _ in samples))
            self.assertAlmostEqual(arm["p_home_win"], sum(w for h, a, w in samples if h > a)/n)
            self.assertAlmostEqual(arm["total"]["over"], sum(w for h, a, w in samples if h+a > 190.5)/n)
            self.assertAlmostEqual(arm["home"]["equal"], sum(w for h, _, w in samples if h == 105)/n)
            self.assertAlmostEqual(arm["home"]["mean"] + arm["away"]["mean"], arm["total"]["mean"])
        higher = model.predict(history, fixture, lines={"home": 108.5})
        for label in model.MODELS:
            self.assertLessEqual(higher["models"][label]["home"]["over"], base["models"][label]["home"]["over"])

    def test_unknown_venue_uses_listed_home_in_training_and_inference(self):
        history = [dict(game(i), neutral=None) for i in range(50)]
        unknown = model.predict(history, dict(matchup(), neutral=None))
        neutral = model.predict(history, dict(matchup(), neutral=True))
        confirmed = model.predict([dict(g, neutral=False) for g in history], dict(matchup(), neutral=False))
        self.assertEqual(unknown["models"], confirmed["models"])
        self.assertNotEqual(unknown["models"], neutral["models"])
        self.assertEqual(unknown["venue_basis"], "assumed_listed_home")
        self.assertEqual(unknown["training_assumed_home_games"], len(history))
        self.assertEqual(neutral["venue_basis"], "confirmed_neutral")
        self.assertEqual(unknown["training_unknown_site_games"], len(history))

    def test_dated_neutral_exception_has_priority_only_after_publication_and_for_exact_teams(self):
        from polymarket861.nba import venue

        game = dict(matchup(), stage="regular", game_date="2026-11-07",
                    home_abbr="IND", away_abbr="DEN", neutral=False)
        before = model.instant("2026-04-16T23:00:00-04:00")
        after = model.instant("2026-04-17T00:00:00-04:00")
        self.assertEqual(venue.resolve(game, before), (False, "confirmed_non_neutral", None))
        self.assertEqual(venue.resolve(game, after)[:2], (True, "documented_neutral"))
        self.assertIn("pr.nba.com", venue.resolve(game, after)[2])
        self.assertFalse(venue.resolve(dict(game, home_abbr="NYK"), after)[0])
        self.assertFalse(venue.resolve(dict(game, game_date="2026-11-08"), after)[0])
        self.assertEqual(venue.resolve(dict(game, neutral=None), before)[1], "assumed_listed_home")
        self.assertEqual(venue.resolve(dict(game, neutral=True), before)[1], "confirmed_neutral")
        sample = dict(game, status="final", home_score=115, away_score=103, kickoff=None,
                      received_at="2026-11-07T23:40:00-05:00")
        state = model.ScoreState()
        state.learn_day([sample], {})
        self.assertEqual(state.home_edge, 3.0)
        self.assertEqual(state.documented_neutral_games, 1)

    def test_cup_neutral_exceptions_match_official_and_provider_team_aliases(self):
        from polymarket861.nba import venue

        for day, left, right in (("2025-12-13", "ORL", "NY"), ("2025-12-13", "OKC", "SA"),
                                 ("2023-12-07", "LAL", "NO")):
            with self.subTest(date=day, teams=(left, right)):
                bdl = dict(matchup(), neutral=False, game_date=day, home_abbr=left, away_abbr=right)
                archive = dict(matchup(), neutral=None, game_date=day, home=right, away=left)
                self.assertEqual(venue.resolve(bdl)[1], "documented_neutral")
                self.assertEqual(venue.resolve(archive)[1], "documented_neutral")

    def test_no_tip_future_final_cannot_enter_cache_training_rest_or_replay(self):
        # Original BDL shape after normalization and prior to the repaired data reader:
        # a Nov 27 morning ET receipt labelled a Nov 28 game Final without a tip.
        start = date(2026, 10, 21)
        history = [game(i, start, 2026) for i in range(35)]
        future = dict(game(38, start, 2026), kickoff=None, home="A", away="D",
                      stage_basis="provider_fields", evidence_grade="local_receipt",
                      received_at="2026-11-28T02:00:00Z", available_at="2026-11-28T02:01:00Z")
        fixture = dict(game(39, start, 2026), status="scheduled",
                       received_at="2026-11-26T12:00:00Z", available_at="2026-11-26T12:01:00Z")
        for cutoff in ("2026-11-28T02:05:00Z", "2026-11-29T15:00:00Z"):
            reference = model.predict(history, fixture, as_of=cutoff)
            attempt = model.predict(history + [future], fixture, as_of=cutoff)
            self.assertEqual(reference, attempt)
        state = model.ScoreState()
        state.learn_day([future], {})
        rest = {}
        model.update_dates(rest, [future])
        self.assertEqual(state.total_games, 0)
        self.assertFalse(rest)
        self.assertEqual(model.prepare(history + [future]), model.prepare(history))
        valid = dict(future, received_at="2026-11-29T02:00:00Z", available_at="2026-11-29T02:01:00Z")
        self.assertTrue(model.eligible(valid))
        self.assertTrue(model.eligible(dict(future, source="538", evidence_grade="final_archive_date_only",
                                            received_at=None, available_at=None)))

    def test_exact_two_point_crps(self):
        arm = {"p_home_win": .5, **{f: {"distribution": {"values": [0, 2], "counts": [1, 1]}}
                                   for f in model.FAMILIES}}
        self.assertEqual(model.score(arm, 2, 1), {"brier_win": .25, "crps_total": 1.5,
                                                  "crps_home": .5, "crps_away": .5})

    def test_insufficient_duplicate_stale_or_tied_results(self):
        history = [game(i) for i in range(40)]
        with self.assertRaisesRegex(ValueError, "insufficient"):
            model.predict(history[:29], matchup())
        with self.assertRaisesRegex(ValueError, "duplicate"):
            model.predict(history+[history[0]], matchup())
        with self.assertRaisesRegex(ValueError, "unequal"):
            model.predict([{**g, "home_score": g["away_score"]} for g in history], matchup())
        with self.assertRaisesRegex(ValueError, "stale"):
            model.predict(history, {**matchup(), "season": 2013, "game_date": "2013-06-01"})
        with self.assertRaisesRegex(ValueError, "regular season"):
            model.predict(history, {**matchup(), "stage": "playoff"})

    def test_two_season_chronological_replay_is_reproducible(self):
        games = [game(i, date(year, 10, 1), year) for year in (2009, 2010, 2011) for i in range(42)]
        kwargs = {"first_season": 2010, "last_season": 2011, "lag_days": 2, "replicates": 100}
        report = model_replay.backtest(games, **kwargs)
        self.assertEqual(report, model_replay.backtest(reversed(games), **kwargs))
        self.assertEqual(report["pooled"]["games"], 84)
        self.assertEqual(report["per_season"]["2010"]["games"], 42)
        self.assertEqual(report["data_grade"], "technical_final_archive")
        self.assertEqual(report["market_receipts"], 0)
        self.assertFalse(report["paper_eligible"])
        for arm in (*model.MODELS, "observed"):
            self.assertEqual(report["pooled"]["final_score_calibration"][arm]["p_tied_final"], 0.)
        for arm in model.MODELS:
            for k in model_replay.METRICS:
                seasons = report["per_season"].values()
                self.assertAlmostEqual(report["pooled"]["losses"][arm][k],
                                       sum(s["losses"][arm][k] for s in seasons)/2, places=6)
        with self.assertRaisesRegex(ValueError, "continuous coverage"):
            model_replay.backtest(games, first_season=2010, last_season=2012)
        with self.assertRaisesRegex(ValueError, "mixed NBA sources"):
            model_replay.backtest(games+[{**game(43), "source": "538"}], **kwargs)

    def test_predictions_do_not_consume_held_out_game_scores(self):
        training = [game(i, date(2009, 10, 1), 2009) for i in range(60)]
        tests = [game(i) for i in range(3)]
        season = model_replay.backtest(training+tests, first_season=2010, last_season=2010,
                                       lag_days=2, replicates=100)
        altered = model_replay.backtest(training+[{**g, "home_score": 2, "away_score": 149} for g in tests],
                                        first_season=2010, last_season=2010, lag_days=2, replicates=100)
        for arm in model.MODELS:
            self.assertEqual(season["pooled"]["final_score_calibration"][arm],
                             altered["pooled"]["final_score_calibration"][arm])
        self.assertNotEqual(season["pooled"]["losses"], altered["pooled"]["losses"])


class NBAOriginalReceiptTests(unittest.TestCase):
    def test_raw_future_final_changes_neither_preview_probabilities_nor_training_digest(self):
        from polymarket861.nba import data
        from test_nba_data import TEST_KEY, game as bdl_game, page

        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "nba"
        self.enterContext(patch.dict(os.environ, {"BALLDONTLIE_API_KEY": TEST_KEY}))
        now = model.instant("2026-11-27T12:00:00Z")
        def advance(seconds):
            nonlocal now
            now += timedelta(seconds=seconds)
        def scan(start, end, rows):
            def get(params, _key):
                return page(rows if params["season_type"] == "regular" else [])
            return data.sync_bdl(root, start, end, fetch=get, clock=lambda: now, sleep=advance)
        def final(day, i):
            return bdl_game(day.toordinal(), season=2026, date=str(day), datetime=None, period=4,
                            status="Final", status_state="final",
                            home_team_score=110+i%9, visitor_team_score=97+i%7)
        def preview(tip):
            args = SimpleNamespace(data_dir=root, game_id=f"bdl:{tip.toordinal()}", as_of=data._iso(now),
                                   total_line=None, home_line=None, away_line=None)
            return model._predict_command(args, clock=lambda: now)

        first = date(2026, 10, 21)
        history = [final(first + timedelta(days=i), i) for i in range(35)]
        target, future = date(2026, 11, 29), date(2026, 11, 28)
        fixture = bdl_game(target.toordinal(), season=2026, date=str(target), datetime="2026-11-29T23:00:00Z")
        self.assertTrue(scan(first, target, [*history, fixture])["complete"])
        before = preview(target)
        advance(120)
        result = scan(future, future, [final(future, 30)])
        self.assertFalse(result["complete"])
        self.assertEqual(result["state"], "source_quality")
        after = preview(target)
        self.assertEqual(before["training_games"], 35)
        self.assertEqual(after["training_games"], 35)
        self.assertEqual(before["training_view_sha256"], after["training_view_sha256"])
        self.assertEqual(before["models"], after["models"])
        suspect = data.game_versions(root, game_id=f"bdl:{future.toordinal()}")[-1]
        self.assertEqual(suspect["status"], "unknown")
        self.assertEqual(suspect["timing_problem"], "play_received_before_eastern_game_date")
        self.assertIsNone(suspect["home_score"])


class PreviewCommandTests(unittest.TestCase):
    def setUp(self):
        from polymarket861.nba import data, markets, storage
        from polymarket861.store import iso

        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.enterContext(closing(storage.connect(self.root)))
        self.db.executescript(data.SCHEMA + markets.SCHEMA)
        fixture = self.fixture = dict(matchup(), home_abbr="BOS", away_abbr="LAL")
        self.cutoff = iso(fixture["game_date"]+"T17:00:00Z")
        for row in [game(i) for i in range(50)]+[fixture]:
            self.insert(row, 1, row["available_at"])
        parser = argparse.ArgumentParser()
        model.register(parser.add_subparsers())
        self.args = parser.parse_args(["model", "predict", fixture["game_id"], "--as-of", self.cutoff])
        self.args.data_dir = self.root

    def insert(self, row, version, timestamp):
        from polymarket861.store import iso

        with self.db:
            self.db.execute("INSERT INTO nba_game_versions VALUES(?,?,?,?,?,?,?)",
                            ("balldontlie", row["game_id"], version, iso(timestamp), iso(timestamp), "a"*64,
                             json.dumps(row)))

    def preview(self):
        return self.args.func(self.args, clock=lambda: self.cutoff)

    def test_stale_manual_preview_explicitly_unscored_and_verified_scan_distinct(self):
        from polymarket861.store import iso

        first = self.preview()
        self.assertEqual(first["preview_eligibility"], "exploratory_unverified_schedule")
        self.assertEqual(first["schedule_verification"]["state"], "unverified")
        self.assertFalse(first["sealed"] or first["forward_scoring_eligible"] or first["paper_eligible"])
        self.assertEqual(iso(first["as_of"]), self.cutoff)
        day = self.fixture["game_date"]
        with self.db:
            self.db.execute("INSERT INTO nba_schedule_scans VALUES(1,?,?,?)", (day, day, iso(day+"T16:55:00Z")))
            self.db.execute("INSERT INTO nba_schedule_completions VALUES(1,?,'ok',1)", (iso(day+"T16:56:00Z"),))
            from hashlib import sha256

            quality = {"phase_rows": {"regular": 1, "preseason": 0, "ist": 0},
                       "overlapping_ids": 0, "phase_mismatches": 0, "quarantined_rows": 0,
                       "unknown_stages": 0, "unidentified_rows": 0, "pagination_errors": 0}
            manifest = json.dumps({"scan_id": 1, "start": day, "end": day, "complete": True, "quality": quality})
            self.db.execute("INSERT INTO nba_schedule_evidence VALUES(1,?,?)",
                            (manifest, sha256(manifest.encode()).hexdigest()))
        fresh = self.preview()
        self.assertEqual(fresh["preview_eligibility"], "verified_schedule_preview")
        self.assertEqual(fresh["schedule_epoch"], 1)
        self.assertFalse(fresh["forward_scoring_eligible"])
        self.assertFalse(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='nba_cycles'").fetchone())

    def test_known_market_live_conflict_blocks_even_an_unverified_snapshot(self):
        from polymarket861.store import iso

        observed = self.fixture["game_date"]+"T16:30:00Z"
        tip = iso(self.fixture["kickoff"])
        with self.db:
            self.db.execute("INSERT INTO nba_market_games VALUES('g','1','BOS','LAL','1','2',?,1,?,?,1,NULL)",
                            (tip, iso(observed), iso(observed)))
            self.db.execute("INSERT INTO nba_market_schedule VALUES('g',1,?,?)", (tip, iso(observed)))
            self.db.execute("INSERT INTO nba_market_play VALUES('g',?,'venue_event_live')", (iso(observed),))
        with self.assertRaisesRegex(ValueError, "market schedule conflict"):
            self.preview()
        self.args.as_of = self.fixture["game_date"]+"T16:29:59Z"
        self.assertEqual(self.preview()["preview_eligibility"], "exploratory_unverified_schedule")

    def test_provider_cancellation_and_started_or_identity_rollback_block_manual_prediction(self):
        f, day = self.fixture, self.fixture["game_date"]
        self.insert(dict(f, status="postponed"), 2, day+"T16:03:00Z")
        with self.assertRaisesRegex(ValueError, "no active"):
            self.preview()
        self.insert(dict(f, home="new-team"), 3, day+"T16:04:00Z")
        self.insert(f, 4, day+"T16:05:00Z")
        with self.assertRaisesRegex(ValueError, "ambiguous identity"):
            self.preview()
        with self.db:
            self.db.execute("DELETE FROM nba_game_versions WHERE game_id=? AND version>1", (f["game_id"],))
        self.insert(dict(f, status="unknown", play_observed=True), 2, day+"T16:03:00Z")
        self.insert(f, 3, day+"T16:04:00Z")
        with self.assertRaisesRegex(ValueError, "observed play"):
            self.preview()

    def test_future_cutoff_rejected(self):
        self.args.as_of = self.fixture["game_date"]+"T17:00:01Z"
        with self.assertRaisesRegex(ValueError, "future"):
            self.preview()


if __name__ == "__main__":
    unittest.main()
