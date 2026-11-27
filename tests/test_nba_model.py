import argparse
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
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

    def test_unknown_venue_withholds_home_edge_and_keeps_its_own_label(self):
        history = [dict(game(i), neutral=None) for i in range(50)]
        unknown = model.predict(history, dict(matchup(), neutral=None))
        neutral = model.predict(history, dict(matchup(), neutral=True))
        self.assertEqual(unknown["models"], neutral["models"])
        self.assertEqual(unknown["venue_basis"], "unknown_home_advantage_withheld")
        self.assertEqual(neutral["venue_basis"], "confirmed_neutral")
        self.assertEqual(unknown["training_unknown_site_games"], len(history))

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
