"""Domestic historical protocols must stay separate from the original EPL benchmark."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from itertools import permutations
from pathlib import Path

from polymarket861 import football as fb, league_registry as registry


def match(home, away, day, *, round_number=1, clock="20:00", score=None, **extra):
    row = {"round": f"Matchday {round_number}", "team1": home, "team2": away, "date": day, **extra}
    if clock:
        row["time"] = clock
    if score is not None:
        row["score"] = {"ft": score}
    return row


class DomesticHistoryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def write(self, league, season, rows):
        path = self.root / season / registry.profile(league).source_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"matches": rows}))
        return path

    def test_scoped_archives_registered_season_offsets_and_source_names(self):
        self.write("lal", "2024-25", [match("Athletic Club", "Getafe CF", "2024-08-15", clock="19:00", score=[1, 1])])
        self.write("sea", "2019-20", [match("Sassuolo Calcio", "Udinese Calcio", "2020-08-02", round_number=38, clock="20:45", score=[0, 1])])
        es = fb.load_fixtures(self.root, league="lal")[0]
        it = fb.load_fixtures(self.root, league="sea", archive_lag_hours=24)[0]
        self.assertEqual(es.fixture_id, "lal/2024-25/athletic-club/getafe-cf")
        self.assertEqual(fb.iso(es.kickoff), "2024-08-15T17:00:00Z")
        self.assertEqual(fb.iso(it.kickoff), "2020-08-02T18:45:00Z")
        self.assertEqual(it.fixture_id, "sea/2019-20/us-sassuolo-calcio/udinese-calcio")
        self.assertEqual(it.result_versions[0].available, it.kickoff + timedelta(hours=24))
        with self.assertRaisesRegex(ValueError, "outside selected league"):
            fb.FootballEngine([es, it], league="sea")
        with self.assertRaisesRegex(ValueError, "archive file"):
            fb.load_fixtures(self.root / "2019-20" / "it.1.json", league="lal")
        with self.assertRaisesRegex(ValueError, "archive file"):
            fb.load_fixtures(self.root / "2019-20" / "it.1.json")

    def test_no_false_prematch_or_early_training_on_resumed_game(self):
        self.write("sea", "2024-25", [match("ACF Fiorentina", "FC Internazionale Milano", "2025-02-06", score=[3, 0])])
        f = fb.load_fixtures(self.root, league="sea", archive_lag_hours=24)[0]
        self.assertEqual(f.status, "resumed")
        self.assertEqual(f.home_goals, 3)
        self.assertEqual(fb.iso(f.result_versions[0].available), "2025-02-07T23:00:00Z")
        engine = fb.FootballEngine([f], league="sea")
        for kickoff in (f.kickoff, fb.utc("2024-12-01T17:00:00Z")):
            with self.assertRaisesRegex(ValueError, fb.EXCLUDED_PREMATCH):
                engine.forecast(f.home, f.away, kickoff, kickoff - timedelta(hours=1), season=f.season)
        with self.assertRaisesRegex(ValueError, "no eligible historical results"):
            engine.fit(f.season, fb.utc("2025-02-07T22:59:59Z"), "other")
        self.assertEqual(engine.fit(f.season, f.result_versions[0].available, "other").evidence["training_matches"], 1)

    def test_administrative_and_unrecognized_states_never_become_labels(self):
        self.write("bun", "2021-22", [
            match("VfL Bochum 1848", "Borussia Mönchengladbach", "2022-03-18", score=[0, 2]),
            match("FC Bayern München", "VfL Wolfsburg", "2022-04-01", status="review in progress", score=[0, 2]),
            match("VfL Wolfsburg", "FC Bayern München", "2022-05-14", score=[2, 2]),
        ])
        fixtures = fb.load_fixtures(self.root, league="bun", archive_lag_hours=24)
        self.assertEqual([f.status for f in fixtures], ["awarded", "quarantined", "completed"])
        self.assertEqual([len(f.result_versions) for f in fixtures], [0, 0, 1])
        self.assertEqual([f.home_goals for f in fixtures], [None, None, 2])
        report = fb.backtest(fixtures, "2021-22", "2021-22", league="bun", horizons=(60,),
                             archive_lag_hours=24, bootstrap=0)
        self.assertEqual(len(report["fixtures"]["excluded_fixtures"]), 2)

    def test_strict_replay_requires_both_schedule_and_training_receipts(self):
        self.write("bun", "2024-25", [
            match("Borussia Mönchengladbach", "Bayer 04 Leverkusen", "2024-08-23", score=[0, 2]),
            match("Borussia Dortmund", "Holstein Kiel", "2025-05-17", round_number=34, score=[2, 1]),
        ])
        assumed, target = fb.load_fixtures(self.root, league="bun", archive_lag_hours=24)
        cutoff = target.kickoff - timedelta(hours=1)
        engine = fb.FootballEngine([assumed, target], league="bun", allow_archive_schedule=False)
        with self.assertRaisesRegex(ValueError, "schedule not known"):
            engine.forecast(target.home, target.away, target.kickoff, cutoff)
        known = replace(target, schedule_versions=((cutoff, target.kickoff),), schedule_grades=("observed_schedule",))
        engine = fb.FootballEngine([assumed, known], league="bun", allow_archive_schedule=False)
        with self.assertRaisesRegex(ValueError, "no eligible historical results"):
            engine.forecast(target.home, target.away, target.kickoff, cutoff)
        receipt = replace(assumed, result_versions=(replace(assumed.result_versions[0], grade="observed"),))
        engine = fb.FootballEngine([receipt, known], league="bun", allow_archive_schedule=False)
        forecast = engine.forecast(target.home, target.away, target.kickoff, cutoff, lines=[4.5])
        self.assertIn("4.5", forecast["totals"]["total"])
        self.assertEqual(forecast["evidence"]["schedule_grade"], "observed_schedule")
        report = fb.score_round([known], [forecast], league="bun", season="2024-25", round_number=34)
        self.assertEqual((report["scheduled"], report["scored"], report["unregistered"]), (9, 1, 8))
        self.assertIn("total_4.5", report["additional_lines"])
        implicit = fb.score_round([target], [forecast], league="bun", season="2024-25", round_number=34)
        self.assertEqual(implicit["scored"], 0)
        report = fb.backtest([assumed, target], "2024-25", "2024-25", league="bun", horizons=(60,), bootstrap=0)
        self.assertEqual(report["horizons"]["T-60"]["summary"]["n"], 0)
        details = []
        study = fb.backtest([assumed, target], "2024-25", "2024-25", league="bun", horizons=(60,),
                            archive_lag_hours=24, lines=[4.5], bootstrap=0, details_sink=details.append)
        self.assertEqual(study["protocol"]["research_goal_lines"], [4.5])
        self.assertEqual(study["horizons"]["T-60"]["additional_lines"]["totals"]["total_4.5"]["n"], 1)
        self.assertIn("4.5", details[0]["totals"]["away"])
        original = known.kickoff - timedelta(days=2)
        revised = replace(known, schedule_versions=((original - timedelta(days=1), original),
                                                    (original + timedelta(minutes=1), known.kickoff)),
                          schedule_grades=("observed_schedule", "observed_schedule"))
        with self.assertRaisesRegex(ValueError, fb.UNKNOWN_RECONCILED_KICKOFF):
            fb.FootballEngine([receipt, revised], league="bun").forecast(
                revised.home, revised.away, revised.kickoff, cutoff)

    def test_legacy_epl_replay_cannot_bypass_registered_pre_match_exception(self):
        self.write("epl", "2023-24", [match("AFC Bournemouth", "Luton Town FC", "2024-03-13", score=[4, 3])])
        legacy = fb.load_fixtures(self.root, archive_lag_hours=24)
        with self.assertRaisesRegex(ValueError, "reload history"):
            fb.FootballEngine(legacy, league="epl")
        modern = fb.load_fixtures(self.root, league="epl", archive_lag_hours=24)
        self.assertEqual(legacy[0].status, "completed")
        self.assertEqual(modern[0].status, "replayed")

    def test_missing_times_season_specific_rounds_and_stages(self):
        f = self.write("fl1", "2024-25", [match("Le Havre AC", "Paris Saint-Germain FC", "2024-08-16", clock=None, score=[0, 1])])
        game = fb.load_fixtures(f, league="fl1", archive_lag_hours=24)[0]
        with self.assertRaisesRegex(ValueError, "kickoff_time_unknown"):
            fb.FootballEngine([game], league="fl1").forecast(game.home, game.away, game.kickoff, game.kickoff - timedelta(hours=1))
        for year, count in (("2022-23", 10), ("2023-24", 9)):
            self.assertEqual(fb.score_round([], [], league="fl1", season=year, round_number=1)["scheduled"], count)
        self.write("fl1", "2024-25", [match("Le Havre AC", "Paris Saint-Germain FC", "2024-08-16", round_number=35)])
        with self.assertRaisesRegex(ValueError, "invalid domestic league round"):
            fb.load_fixtures(f, league="fl1")
        self.write("fl1", "2024-25", [match("Le Havre AC", "Paris Saint-Germain FC", "2024-08-16", stage="playoff")])
        with self.assertRaisesRegex(ValueError, "regular"):
            fb.load_fixtures(f, league="fl1")

    def test_b0_complete_season_excludes_awarded_scores_and_never_pools_countries(self):
        season, league = "2022-23", "bun"
        kickoff = fb.utc("2022-09-01T18:00Z")
        result = fb.ResultVersion(kickoff + timedelta(hours=24), (2, 1), "lag_assumption", "test")
        fixtures = [fb.Fixture(fb.fixture_id(season, h, a, league=league), season, 1, kickoff, h, a,
                               2, 1, (result,), status="completed")
                    for h, a in permutations(registry.members(league, season), 2)]
        fixtures[0] = replace(fixtures[0], status="awarded", home_goals=None, away_goals=None,
                              result_versions=(), quarantine_reason="source_awarded")
        unrelated = replace(fixtures[1], season="2019-20", fixture_id=fb.fixture_id("2019-20", fixtures[1].home, fixtures[1].away, league=league))
        cutoff = fb.utc("2023-12-01T18:00Z")
        fit = fb.FootballEngine(fixtures + [unrelated], league=league).fit("2023-24", cutoff, "different")
        self.assertEqual((fit.evidence["b0_matches"], fit.evidence["training_matches"]), (305, 305))
        self.assertEqual(fit.evidence["b0_scope"], "prior_complete_seasons")
        partial = fb.FootballEngine(fixtures[:-1], league=league).fit("2023-24", cutoff, "different")
        self.assertEqual(partial.evidence["b0_scope"], "available_warmup")
