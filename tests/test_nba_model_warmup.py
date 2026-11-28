import json
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from polymarket861.nba import data, model, storage, warmup


def season_games(season, rounds=None):
    first = warmup.OPENING[season]
    teams = list(range(30))
    rows = []
    for n in range(rounds or warmup.TEAM_GAMES[season]):
        day = first + timedelta(days=n*2)
        at = day.isoformat() + "T23:30:00Z"
        for i in range(15):
            rows.append({"source": "balldontlie", "game_id": f"{season}-{n}-{i}", "season": season,
                         "game_date": str(day), "stage": "regular", "stage_basis": "disjoint_phase_queries",
                         "status": "final", "home": str(teams[i]), "away": str(teams[-i-1]),
                         "home_score": 112, "away_score": 102, "kickoff": None,
                         "received_at": at, "available_at": at, "evidence_grade": "local_receipt"})
        teams = [teams[0], teams[-1], *teams[1:-1]]
    return rows


class WarmupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.enterContext(closing(storage.connect(self.root)))
        self.db.executescript(data.SCHEMA)
        self.previous = season_games(2025)
        self.current = season_games(2026, rounds=2)
        self.cutoff = "2026-10-23T16:00:00Z"
        self.target = {"game_date": "2026-10-24", "season": 2026}

    def proof(self, start, end, timestamp=None, *, quality=True):
        at = data._iso(timestamp or self.cutoff)
        with self.db:
            sid = self.db.execute("INSERT INTO nba_schedule_scans(start,end,began) VALUES(?,?,?)",
                                  (str(start), str(end), at)).lastrowid
            self.db.execute("INSERT INTO nba_schedule_completions VALUES(?,?,?,?)", (sid, at, "ok", 1))
            q = {"phase_rows": {"regular": 123, "preseason": 0, "ist": 0}, "overlapping_ids": 0,
                 "unidentified_rows": 0, "quarantined_rows": 0, "unknown_stages": 0,
                 "pagination_errors": 0, "phase_mismatches": 0} if quality else {}
            self.db.execute("INSERT INTO nba_schedule_evidence VALUES(?,?,?)",
                            (sid, json.dumps({"quality": q}), "a"*64))

    def check(self, rows):
        return warmup.coverage(self.root, rows, self.target, self.cutoff)

    def test_full_season_per_team_elapsed_sweeps_and_receipts_are_required(self):
        start, stop = warmup._window(2025)
        self.proof(start, stop)
        self.proof("2026-10-20", "2026-10-22", timestamp="2026-10-19T14:00:00Z")
        rows = self.previous + self.current
        self.assertEqual(self.check(rows)["reason"], "current_schedule_coverage_incomplete")
        self.proof("2026-10-20", "2026-10-22")
        self.assertTrue(self.check(rows)["eligible"])
        self.assertEqual(self.check(rows)["current_finals"], 30)
        self.assertEqual(self.check(rows)["through"], "2026-10-22")
        self.assertEqual(self.check(self.previous)["reason"], "current_results_empty")
        self.assertEqual(self.check(rows[1:])["reason"], "previous_season_incomplete")
        self.assertEqual(self.check([dict(rows[0], available_at="2026-11-01T00:00:00Z"), *rows[1:]])[
            "reason"], "previous_season_incomplete")
        same_count_wrong_teams = [dict(rows[0], away="foreign-team"), *rows[1:]]
        self.assertFalse(self.check(same_count_wrong_teams)["eligible"])
        unknown = [dict(rows[-1], stage="unknown"), *rows[:-1]]
        self.assertEqual(self.check(unknown)["reason"], "current_results_unresolved")
        # Legacy completions without provider phase controls cannot certify a day.
        self.proof("2026-10-20", "2026-10-20", quality=False)
        self.assertFalse(self.check(rows)["eligible"])

    def test_impossible_first_receipt_of_an_old_undated_tip_blocks_maturity_after_its_date_passes(self):
        self.proof(*warmup._window(2025))
        self.proof("2026-10-20", "2026-10-22")
        corrupt = dict(self.previous[0], received_at="2025-10-21T03:59:59Z")
        rows = [corrupt, *self.previous[1:], *self.current]
        report = self.check(rows)
        self.assertFalse(report["eligible"])
        self.assertEqual(report["previous_finals"], 1229)
        self.assertEqual(report["previous_unresolved"], 1)
        self.assertEqual(report["reason"], "previous_season_incomplete")
        self.assertTrue(self.check(self.previous + self.current)["eligible"])

    def test_season_opener_needs_the_full_previous_year_even_when_current_has_no_scores(self):
        self.cutoff = "2026-10-20T13:00:00Z"
        self.target["game_date"] = "2026-10-20"
        self.assertFalse(self.check(self.previous)["eligible"])
        self.proof(*warmup._window(2025))
        self.assertTrue(self.check(self.previous)["eligible"])

    def test_backfill_can_run_before_season_opener(self):
        attempts = []
        def sync(root, start, end):
            attempts.append((start, end))
            return {"state": "missing_key", "complete": False}
        args = SimpleNamespace(data_dir=self.root, sync=True, season=None, through=None)
        report = warmup._warmup_command(args, clock=lambda: model.instant("2026-10-18T14:00:00Z"), sync=sync)
        self.assertEqual(report["current_season"], 2026)
        self.assertEqual(report["required_scan_days"], 0)
        self.assertEqual(attempts[0][0], warmup.OPENING[2025])
        self.assertFalse(report["eligible"])

    def test_backfill_stops_on_rate_limit_and_resumes_after_completed_chunk(self):
        now = model.instant(self.cutoff)
        season = 2025
        calls = []

        def sync(root, start, end):
            calls.append((start, end))
            if len(calls) == 2:
                return {"state": "rate_limited", "complete": False, "retry_at": self.cutoff}
            self.proof(start, end)
            # At least one plausible final allows coverage of completed chunks to
            # be retained when resuming while the rest of the season is missing.
            if len(calls) == 1:
                row = self.previous[0]
                at = data._iso(self.cutoff)
                with self.db:
                    self.db.execute("INSERT INTO nba_game_versions VALUES(?,?,?,?,?,?,?)",
                                    ("balldontlie", row["game_id"], 1, at, at, "a"*64, json.dumps(row)))
            return {"state": "ok", "complete": True}

        now_fn = lambda: now
        end = warmup._window(season)[1]
        first = warmup._backfill(self.root, [season], end, sync=sync, clock=now_fn)
        second = warmup._backfill(self.root, [season], end, sync=sync, clock=now_fn)
        self.assertEqual(first[-1]["state"], "rate_limited")
        self.assertTrue(all(a["complete"] for a in second))
        self.assertEqual(calls[1], calls[2])
        self.assertEqual(calls.count(calls[0]), 1)

    def test_keyless_modern_report_contains_coverage_instead_of_a_five_season_score(self):
        args = SimpleNamespace(data_dir=self.root, sync=False)
        result = warmup._modern_command(args, clock=lambda: model.instant("2026-11-28T12:00:00Z"))
        self.assertFalse(result["complete"])
        self.assertNotIn("replays", result)
        counts = result["season_coverage"]
        self.assertEqual([c["expected_finals"] for c in counts], [1080] + [1230]*5)
        self.assertEqual(result["b2b_margin_points_fixed"], 1.5)

    def test_shortened_2020_warmup_and_no_cross_sport_rows(self):
        rows = season_games(2020)
        self.assertTrue(warmup._season_counts(rows, 2020)[0]["complete"])
        rows[0]["source"] = "538"
        self.assertFalse(warmup._season_counts(rows, 2020)[0]["complete"])


if __name__ == "__main__":
    unittest.main()
