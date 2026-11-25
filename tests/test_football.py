"""Football-specific point-in-time, grading and distribution regressions."""

import json
import math
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import numpy as np

from polymarket861 import football as fb
from polymarket861.football_model import _goals_matrix, apply_context, probabilities

SEASON = "2021-22"


def fixture(name, home, away, kickoff, score=None, observed_at=None, **kw):
    goals = score or (None, None)
    versions = (
        (fb.ResultVersion(fb.utc(observed_at), score, "observed", name),)
        if observed_at
        else ()
    )
    return fb.Fixture(
        fb.fixture_id(SEASON, home, away),
        SEASON,
        kw.pop("round", 1),
        fb.utc(kickoff),
        home,
        away,
        *goals,
        versions,
        status="completed" if score else "scheduled",
        **kw,
    )


class FootballTests(unittest.TestCase):
    def test_grade_protocol_direction_and_range(self):
        p = dict(zip(fb.PROB_KEYS, (0.5, 0.3, 0.2, 0.4, 0.6, 0.25)))
        grade = fb.score_probabilities(p, 2, 0)
        self.assertAlmostEqual(grade["score"], 85.6875)
        self.assertAlmostEqual(grade["losses"]["result"], 0.19)
        self.assertGreater(
            fb.score_probabilities(p, 2, 0)["score"],
            fb.score_probabilities(p, 0, 2)["score"],
        )
        perfect = dict(zip(fb.PROB_KEYS, (0, 0, 1, 1, 0, 1)))
        self.assertEqual(fb.score_probabilities(perfect, 0, 3)["score"], 100)
        with self.assertRaises(ValueError):
            fb.score_probabilities(p | {"draw": 0.9}, 1, 1)

    def test_joint_matrix_all_market_families_and_tails(self):
        m, tail = _goals_matrix(3.4, 1.8, -0.05)
        p, by = probabilities(m, [0.5, 1.5, 2.5, 5.5, 9.5, 19.5])
        self.assertAlmostEqual(m.sum(), 1)
        self.assertLess(tail, 1e-12)
        self.assertGreater(by["total"]["9.5"]["over"], 0)
        self.assertGreater(p["home_win"], p["away_win"])
        self.assertAlmostEqual(p["home_win"] + p["draw"] + p["away_win"], 1)
        self.assertAlmostEqual(by["total"]["2.5"]["over"], p["total_over_2_5"])
        for outcomes in by.values():
            overs = [v["over"] for v in outcomes.values()]
            self.assertEqual(overs, sorted(overs, reverse=True))
            for item in outcomes.values():
                self.assertAlmostEqual(sum(item.values()), 1)
        with self.assertRaises(ValueError):
            probabilities(m, [2])
        with self.assertRaises(ValueError):
            _goals_matrix(10, 3, 0.2)

    def test_verified_versions_only_and_man_united_leeds_t24(self):
        # Local 12:30 on August 14 meant 11:30 UTC; Brentford played the
        # previous calendar day at local 20:00, after the T-24h decision.
        earlier = fixture(
            "e", "A", "B", "2021-08-10T13:00Z", (1, 1), "2021-08-10T15:15Z"
        )
        brent = fixture(
            "b",
            "Brentford",
            "Arsenal",
            "2021-08-13T19:00Z",
            (2, 0),
            "2021-08-13T21:20Z",
        )
        target = fixture(
            "t",
            "Manchester United",
            "Leeds United",
            "2021-08-14T11:30Z",
            (5, 1),
            "2021-08-14T14:00Z",
        )
        kickoff = target.kickoff

        def run(fixtures, minutes):
            return fb.predict(
                fixtures,
                target.home,
                target.away,
                kickoff,
                kickoff - timedelta(minutes=minutes),
            )

        p = run([earlier, brent, target], 1440)
        contaminated = replace(
            brent,
            home_goals=9,
            away_goals=0,
            result_versions=(replace(brent.result_versions[0], score=(9, 0)),),
        )
        self.assertEqual(p, run([earlier, contaminated, target], 1440))
        self.assertEqual(p["evidence"]["training_matches"], 1)
        self.assertEqual(
            run([earlier, brent, target], 60)["evidence"]["training_matches"], 2
        )
        self.assertNotEqual(
            run([earlier, contaminated, target], 60)["matrix"],
            run([earlier, brent, target], 60)["matrix"],
        )
        late = replace(
            earlier,
            result_versions=(
                replace(earlier.result_versions[0], available=target.kickoff),
            ),
        )
        with self.assertRaisesRegex(ValueError, "no eligible"):
            run([late], 1440)

    def test_retractions_and_same_time_revisions(self):
        f = fixture("r", "A", "B", "2021-08-10T13:00Z", (2, 1), "2021-08-10T15:15Z")
        old = f.result_versions[0]
        amended = replace(old, score=(1, 1), available=fb.utc("2021-08-12T12:00Z"))
        withdrawn = replace(old, score=None, available=fb.utc("2021-08-14T12:00Z"))
        f = replace(f, result_versions=(old, amended, withdrawn))
        engine = fb.FootballEngine([f])

        def run(day):
            return engine.forecast(
                "C", "D", "2021-08-20T12:00Z", f"2021-08-{day}T12:00Z"
            )["probabilities"]

        self.assertEqual(run(11)["home_win"], 1)
        self.assertEqual(run(12)["draw"], 1)
        with self.assertRaisesRegex(ValueError, "no eligible"):
            run(14)

    def test_reader_observed_upper_bound_dst_and_revision_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            season = root / SEASON
            season.mkdir()
            row = {
                "round": "Matchday 1",
                "date": "2021-08-13",
                "time": "20:00",
                "team1": "A FC",
                "team2": "B",
                "score": {"ft": [2, 0]},
            }
            other = dict(
                row,
                round="Matchday 2",
                team1="C",
                team2="D",
                date="2021-12-02",
                time="20:00",
                score={},
            )
            source = season / "en.1.json"
            source.write_text(json.dumps({"matches": [row, other]}))
            primary = fb.load_fixtures(root)
            self.assertEqual(primary[0].kickoff.hour, 19)
            self.assertEqual(primary[1].kickoff.hour, 20)
            self.assertEqual(primary[0].result_versions, ())
            proxy = fb.load_fixtures(root, archive_lag_hours=24)[0].result_versions[0]
            self.assertEqual(proxy.available, primary[0].kickoff + timedelta(hours=24))
            self.assertEqual(proxy.grade, "lag_assumption")
            seen = "2021-12-01T00:00Z"
            e = {
                "fixture_id": primary[0].fixture_id,
                "score": [2, 0],
                "source": "public-snapshot",
                "finalized_by": seen,
                "published_by": seen,
                "archive_at": seen,
            }
            s = {
                "fixture_id": primary[1].fixture_id,
                "kickoff": "2021-12-02T20:00Z",
                "schedule_observed_at": seen,
            }
            file = root / "proof.ndjson"
            file.write_text("\n".join(map(json.dumps, [e, s])))
            obs = fb.load_fixtures(root, evidence_path=file)
            self.assertEqual(obs[0].result_versions[0].grade, "archived_upper_bound")
            later = fb.predict(
                obs, "C", "D", other["date"] + "T20:00Z", "2021-12-01T10:00Z"
            )
            self.assertEqual(
                later["evidence"]["schedule_grade"], "archived_schedule_upper_bound"
            )
            self.assertEqual(
                later["evidence"]["result_grades"], {"archived_upper_bound": 1}
            )
            with self.assertRaisesRegex(ValueError, "schedule not known"):
                fb.predict(
                    obs, "C", "D", other["date"] + "T20:00Z", "2021-11-30T10:00Z"
                )
            file.write_text("\n".join(map(json.dumps, [e, e | {"score": [9, 9]}])))
            with self.assertRaisesRegex(ValueError, "conflicting versions"):
                fb.load_fixtures(root, evidence_path=file)
            file.write_text(json.dumps(e | {"archive_at": "2021-08-10T12:00Z"}))
            with self.assertRaisesRegex(ValueError, "inconsistent"):
                fb.load_fixtures(root, evidence_path=file)

    def test_promoted_team_fallback_and_genuine_fit(self):
        teams = [f"Team {c}" for c in range(8)]
        rs = []
        start = fb.utc("2021-08-01T15:00Z")
        for i, home in enumerate(teams):
            for j, away in enumerate(teams):
                if i == j:
                    continue
                k = start + timedelta(days=len(rs))
                goals = ((i + 2 * j) % 4, (i + j) % 3)
                rs.append(
                    fixture(str(len(rs)), home, away, k, goals, k + timedelta(hours=3))
                )
        end = start + timedelta(days=len(rs) + 1)
        engine = fb.FootballEngine(rs)
        a = engine.forecast(teams[0], teams[1], end, end - timedelta(hours=1))
        self.assertEqual(a["model"], "B1")
        self.assertTrue(np.isfinite(a["rates"]["rho"]))
        b = engine.forecast(teams[0], "Newcomer", end, end - timedelta(hours=1))
        self.assertEqual((b["model"], b["fallback_reason"]), ("B0", "unseen_team"))
        self.assertAlmostEqual(
            sum(b["probabilities"][k] for k in ("home_win", "draw", "away_win")), 1
        )

    def test_lineup_uncertainty_mixes_scenarios_once(self):
        m, tail = _goals_matrix(1.7, 1.2, -0.03)
        p, t = probabilities(m)
        initial = {
            "model": "B1",
            "rates": {"home": 1.7, "away": 1.2, "rho": -0.03},
            "totals": t,
            "probabilities": p,
            "matrix": m.tolist(),
            "tail_bound": tail,
        }
        impact = {
            "evidence_id": "ab",
            "experimental": True,
            "attack_multiplier": 0.7,
            "defense_multiplier": 1.2,
            "probability": 0.4,
        }
        mixed = apply_context(
            initial, {"teams": {"home": {"impacts": [impact, impact]}}}
        )
        self.assertEqual(mixed["model"], "B2-shadow")
        self.assertEqual(mixed["scenario_count"], 2)
        expected = (
            probabilities(_goals_matrix(1.7 * 0.7, 1.2 * 1.2, -0.03)[0])[0]["home_win"]
            * 0.4
            + p["home_win"] * 0.6
        )
        self.assertAlmostEqual(mixed["probabilities"]["home_win"], expected)
        self.assertEqual(
            apply_context(
                initial,
                {"teams": {"home": {"impacts": [impact | {"experimental": False}]}}},
            )["model"],
            "B1",
        )
        self.assertEqual(
            apply_context(
                initial,
                {
                    "teams": {
                        "home": {"impacts": [impact, impact | {"probability": 0.8}]}
                    }
                },
            )["context_status"],
            "conflicting_impact_evidence",
        )

    def test_shared_weather_scenario_is_one_joint_draw(self):
        grid, _ = _goals_matrix(1.8, 1.4, -0.04)
        p, totals = probabilities(grid)
        base = {
            "model": "B1",
            "totals": totals,
            "probabilities": p,
            "rates": {"home": 1.8, "away": 1.4, "rho": -0.04},
        }
        event = {
            "evidence_id": "weather-a",
            "experimental": True,
            "probability": 0.3,
            "basis": "registered weather study",
            "uncertainty": "finite-sample estimate",
            "home": {"attack_multiplier": 0.9, "defense_multiplier": 0.95},
            "away": {"attack_multiplier": 0.85, "defense_multiplier": 0.97},
        }
        mixed = apply_context(base, {"match_impacts": [event, event]})
        future = probabilities(
            _goals_matrix(1.8 * 0.9 * 0.97, 1.4 * 0.85 * 0.95, -0.04)[0]
        )[0]
        self.assertEqual(mixed["scenario_count"], 2)
        self.assertEqual(mixed["impact_evidence_ids"], ["weather-a"])
        for key in p:
            self.assertAlmostEqual(
                mixed["probabilities"][key], 0.7 * p[key] + 0.3 * future[key]
            )
        missing = apply_context(base, {"match_impacts": [event | {"basis": ""}]})
        self.assertEqual(missing["context_status"], "no_verified_impact")

    def test_missing_postponed_reschedule_and_market_absence(self):
        d = math.sqrt(0.2)
        p = dict(zip(fb.PROB_KEYS, (1 - d, 0, d, d, 1 - d, d)))
        initial = fb.utc("2021-12-10T15:00Z")
        fs = [
            fixture(
                str(i), f"H{i}", f"A{i}", initial, (2, 0), initial + timedelta(hours=3)
            )
            for i in range(10)
        ]
        predicted = {f.fixture_id: p for f in fs[:-1]}
        r = fb.score_round(fs, predicted)
        self.assertEqual(
            (r["scored"], r["completed"], r["missing"], r["status"]),
            (9, 10, 1, "incomplete"),
        )
        self.assertAlmostEqual(r["mean_score"], 80)
        np.testing.assert_allclose(r["score_range"], [72, 82])
        pending = replace(
            fs[-1],
            home_goals=None,
            away_goals=None,
            result_versions=(),
            status="postponed",
        )
        self.assertEqual(
            fb.score_round(fs[:-1] + [pending], predicted)["status"], "preliminary"
        )
        new = initial + timedelta(days=60)
        f = replace(
            fs[-1],
            kickoff=new,
            schedule_versions=(
                (initial - timedelta(days=10), initial),
                (new - timedelta(days=5), new),
            ),
        )
        pred = dict(
            probabilities=p,
            kickoff=fb.iso(initial),
            cutoff=fb.iso(initial - timedelta(hours=1)),
        )
        replay = fb.score_round(fs[:-1] + [f], predicted | {f.fixture_id: pred})
        self.assertEqual(replay["missing"], 1)
        fresh = pred | {
            "kickoff": fb.iso(new),
            "cutoff": fb.iso(new - timedelta(hours=1)),
        }
        good = fb.score_round(fs[:-1] + [f], predicted | {f.fixture_id: fresh})
        self.assertEqual((good["scored"], good["status"]), (10, "final"))
        self.assertEqual(
            fb.score_round([f], {f.fixture_id: fresh | {"sealed_at": fb.iso(new)}})[
                "missing"
            ],
            1,
        )
        with self.assertRaisesRegex(ValueError, "duplicate fixture"):
            fb.score_round([f, f], {})

    def test_rolling_backtest_coverage_details_calibration_and_clusters(self):
        t = fb.utc("2021-08-10T16:00Z")
        fs = [
            fixture(
                str(i),
                f"H{i}",
                f"A{i}",
                t + timedelta(days=i * 5),
                (i % 3, i % 2),
                t + timedelta(days=i * 5, hours=3),
                round=i + 1,
            )
            for i in range(5)
        ]
        details = []
        report = fb.backtest(
            fs,
            SEASON,
            SEASON,
            horizons=(60,),
            bootstrap=25,
            seed=123,
            details_sink=details.append,
        )
        h = report["horizons"]["T-60"]["summary"]
        self.assertEqual((h["n"], h["rounds"], h["coverage"]), (4, 4, 0.8))
        self.assertEqual(h["paired_round_ci95"], [0, 0])
        self.assertEqual(sum(b["n"] for b in h["calibration"]["model"]["home_win"]), 4)
        self.assertEqual(len(details), 4)
        for r in details:
            self.assertLessEqual(
                fb.utc(r["evidence"]["latest_result_available_at"]), fb.utc(r["cutoff"])
            )
        rr = fb.score_round(fs, details, season=SEASON, round_number=2)
        self.assertEqual(rr["scored"], 1)
        self.assertEqual(rr["baseline"]["paired_n"], 1)
        self.assertEqual((rr["unregistered"], rr["status"]), (9, "preliminary"))

    def test_unknown_out_of_season_date_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2019-20" / "en.1.json"
            path.parent.mkdir()
            match = {
                "date": "2019-07-01",
                "round": "Matchday 32",
                "time": "18:00",
                "team1": "A",
                "team2": "B",
                "score": {"ft": [1, 4]},
            }
            path.write_text(json.dumps({"matches": [match]}))
            unknown = fb.load_fixtures(path, archive_lag_hours=24)[0]
            self.assertEqual(unknown.status, "quarantined")
            self.assertIsNone(unknown.home_goals)
            self.assertEqual(unknown.result_versions, ())

    def test_naive_time_and_nonverified_repair_rejected(self):
        with self.assertRaisesRegex(ValueError, "offset"):
            fb.utc("2024-08-01T15:00")
        with self.assertRaisesRegex(ValueError, "unverified"):
            fb.verify_2019_repair([])
        self.assertEqual(
            fb.team_key("Manchester United FC"), fb.team_key("Manchester United")
        )


if __name__ == "__main__":
    unittest.main()
