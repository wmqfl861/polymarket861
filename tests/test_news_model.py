import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import numpy as np

from polymarket861 import (
    datasets,
    football,
    football_model,
    news_evaluation,
    news_model,
    paired_proof,
    pipeline,
    store,
)
from test_paired_proof import at, season_data


HOME, AWAY = "Club 00", "Club 19"
CUT = "2023-08-12T12:59:00Z"
LINES = (0.5, 1.5, 2.5, 3.5)


def model():
    matrix, tail = football_model._goals_matrix(1.55, 1.32, -0.04)
    probs, totals = football_model.probabilities(matrix, LINES)
    return {
        "model": "B1",
        "probabilities": probs,
        "totals": totals,
        "rates": {"home": 1.55, "away": 1.32, "rho": -0.04},
        "matrix": matrix.tolist(),
        "tail_bound": tail,
        "cutoff": CUT,
        "evidence": {"input_sha256": "same-training"},
    }


def claim(**changes):
    return {
        "id": "event1",
        "player_id": "club:player1",
        "player_name": "Player One",
        "team": HOME,
        "status": "confirmed",
        "event": "injury",
        "availability": "out",
        "position": "forward",
        "origin": "club press office",
        "authority": "official",
        "url": "https://example.org/original",
        "quote": "Player One will miss this match with an ankle injury.",
        "published_at": "2023-08-12T08:00:00Z",
        "received_at": "2023-08-12T09:00:00Z",
        "known_at": "2023-08-12T09:00:01Z",
        **changes,
    }


class NewsModelTests(unittest.TestCase):
    def pair(self, *facts, **kw):
        return news_model.paired_prediction(
            model(), list(facts), home=HOME, away=AWAY, **kw
        )

    def test_one_joint_distribution_all_markets_and_fixed_role_priors(self):
        original = model()
        before = copy.deepcopy(original)
        candidate, pair = news_model.paired_prediction(
            original, [claim()], home=HOME, away=AWAY, lines=LINES
        )
        self.assertEqual(original, before)
        self.assertTrue(pair["changed"])
        self.assertLess(
            pair["with_news"]["probabilities"]["home_win"],
            pair["no_news"]["probabilities"]["home_win"],
        )
        self.assertLess(candidate["rates"]["home"], original["rates"]["home"])
        self.assertEqual(
            pair["with_news"]["probabilities"],
            football_model.probabilities(candidate["matrix"], LINES)[0],
        )
        self.assertEqual(
            pair["with_news"]["totals"],
            football_model.probabilities(candidate["matrix"], LINES)[1],
        )
        self.assertAlmostEqual(np.sum(candidate["matrix"]), 1)
        self.assertEqual(pair["applied"][0]["role"], "forward")
        keeper, pk = self.pair(claim(position="GK", team=AWAY))
        self.assertGreater(keeper["rates"]["home"], original["rates"]["home"])
        self.assertEqual(keeper["rates"]["away"], original["rates"]["away"])
        self.assertEqual(pk["policy_sha256"], pair["policy_sha256"])
        _, rumour = self.pair(claim(status="rumour"))
        self.assertEqual(rumour["no_news"], rumour["with_news"])
        self.assertFalse(rumour["changed"])
        _, reported = self.pair(claim(status="reported", authority="secondary"))
        self.assertLess(
            abs(reported["log_goal_shifts"]["home"]),
            abs(pair["log_goal_shifts"]["home"]),
        )
        _, bound = self.pair(*(claim(id=f"c{i}", player_id=f"p{i}") for i in range(23)))
        self.assertAlmostEqual(
            bound["log_goal_shifts"]["home"],
            -news_model.POLICY["max_abs_log_adjustment_per_team"],
        )

    def test_duplicate_conflict_return_and_time_boundaries(self):
        _, one = self.pair(claim())
        _, two = self.pair(claim(), claim(id="syndicated", origin="wire agency"))
        self.assertEqual(one["with_news"], two["with_news"])
        self.assertEqual(len(two["applied"]), 1)
        _, disputed = self.pair(claim(), claim(id="other", availability="doubtful"))
        self.assertFalse(disputed["changed"])
        self.assertTrue(
            disputed["excluded_reasons"]["conflicting_current_availability"]
        )
        _, restored = self.pair(
            claim(),
            claim(
                id="returned",
                availability="available",
                event="return",
                published_at="2023-08-12T09:00:00Z",
            ),
        )
        self.assertFalse(restored["changed"])
        _, withdrawn = self.pair(
            claim(cluster_id="story"),
            claim(id="tombstone", cluster_id="story", status="retracted"),
        )
        self.assertFalse(withdrawn["changed"])
        for bad in [
            claim(team="Elsewhere"),
            claim(known_at="2023-08-12T13:00Z"),
            claim(known_at=None),
            claim(quote=""),
            claim(authority="secondary"),
            claim(status="denied"),
            claim(event={}),
            claim(fixture_id="some-other-game"),
            claim(roster_seen_at="2023-08-16T08:00Z"),
            claim(published_at="2023-07-01T00:00Z"),
        ]:
            with self.subTest(bad=bad):
                self.assertEqual(self.pair(bad)[1]["with_news"], one["no_news"])
        _, both = self.pair(claim(), claim(id="transfer", team=AWAY))
        self.assertFalse(both["changed"])
        _, unrelated = self.pair(
            claim(), claim(id="transfer", event="transfer", conflict=True)
        )
        self.assertEqual(one["with_news"], unrelated["with_news"])
        fallback = model() | {"model": "B0", "rates": None}
        result, p = news_model.paired_prediction(
            fallback, [claim()], home=HOME, away=AWAY
        )
        self.assertEqual(p["status"], "no_team_rates")
        self.assertEqual(result["matrix"], fallback["matrix"])

    def test_an_agreeing_reprint_preserves_the_latest_still_supported_confirmation(
        self,
    ):
        club = claim(cluster_id="official-story")
        wire = claim(
            id="wire",
            origin="independent paper",
            authority="secondary",
            status="reported",
            published_at="2023-08-12T08:05Z",
        )
        expected = self.pair(club)[1]
        for source in (wire, wire | {"origin": club["origin"]}):
            both = self.pair(source, club)[1]
            self.assertEqual(both["with_news"], expected["with_news"])
            self.assertEqual(both["applied"][0]["prior_weight"], 1)
        contrary = wire | {"availability": "doubtful"}
        self.assertFalse(self.pair(club, contrary)[1]["changed"])
        correction = club | {
            "id": "new-club",
            "published_at": "2023-08-12T08:10Z",
            "availability": "doubtful",
        }
        reconciled = self.pair(club, contrary, correction)[1]
        self.assertEqual(reconciled["applied"][0]["id"], "new-club")
        self.assertEqual(reconciled["with_news"], self.pair(correction)[1]["with_news"])
        withdrawn = self.pair(
            club,
            wire,
            claim(id="remove", cluster_id="official-story", status="withdrawn"),
        )[1]
        self.assertEqual(withdrawn["with_news"], self.pair(wire)[1]["with_news"])

    def test_public_archive_research_never_backdates_a_local_receipt(self):
        fact = claim(
            received_at="2026-11-25T09:00Z",
            known_at="2026-11-25T09:01Z",
            historical_revision={
                "id": "654321",
                "url": "https://en.wikipedia.org/w/index.php?oldid=654321",
                "sha256": hashlib.sha256(b"original").hexdigest(),
            },
        )
        _, strict = self.pair(fact)
        _, sensitivity = self.pair(fact, availability_basis=news_model.HISTORICAL)
        self.assertFalse(strict["changed"])
        self.assertTrue(sensitivity["changed"])
        self.assertEqual(
            sensitivity["applied"][0]["received_at"], store.iso(fact["received_at"])
        )
        _, unproved = self.pair(
            fact | {"historical_revision": {}}, availability_basis=news_model.HISTORICAL
        )
        self.assertFalse(unproved["changed"])
        _, late_correction = self.pair(
            fact | {"effective_at": "2023-08-13T08:00Z"},
            availability_basis=news_model.HISTORICAL,
        )
        self.assertFalse(late_correction["changed"])
        _, late_republication = self.pair(
            fact
            | {
                "published_at": "2023-08-13T08:00Z",
                "effective_at": fact["published_at"],
            },
            availability_basis=news_model.HISTORICAL,
        )
        self.assertFalse(late_republication["changed"])


class NewsEvidenceTests(unittest.TestCase):
    def test_coverage_uses_documented_availability_and_both_local_clocks(self):
        source = {
            "status": "degraded",
            "availability_observed": True,
            "checked_at": CUT,
            "source_observed_at": CUT,
        }
        fixture = {"home": HOME, "away": AWAY}
        coverage = {
            k: {"team": name, "observed": True, "sources": [source.copy()]}
            for k, name in fixture.items()
        }
        self.assertTrue(
            news_evaluation._both_observed(coverage, fixture, store.iso(CUT))
        )
        for change in (
            {"availability_observed": False},
            {"checked_at": "2023-08-13T08:00Z"},
            {"source_observed_at": "2023-08-13T08:00Z"},
            {"status": "stale"},
        ):
            with self.subTest(change=change):
                coverage["away"]["sources"] = [source | change]
                self.assertFalse(
                    news_evaluation._both_observed(coverage, fixture, store.iso(CUT))
                )

    def test_real_seal_early_vs_late_arrival_readmission_and_result_revision(self):
        season = "2023-24"
        kickoff = "2023-08-12T14:00Z"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "app"
            history = Path(temp) / "sources"
            for yr in (2020, 2021, 2022, 2023):
                path = history / f"{yr}-{(yr + 1) % 100:02d}" / "en.1.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(season_data(yr, yr != 2023)))
            fid = football.fixture_id(season, HOME, AWAY)
            rev = "b" * 40
            blobs = {
                p.parent.name: football.blob_sha(p.read_bytes())
                for p in history.glob("*/en.1.json")
            }
            with at("2023-08-01T10:00Z"):
                paired_proof.attest_provider_files(root, rev, blobs)
                datasets.observe(root, history, revision=rev, expected=blobs)
                store.register_fixture(
                    root,
                    dict(
                        fixture_id=fid,
                        home=HOME,
                        away=AWAY,
                        season=season,
                        round_id=1,
                        kickoff=kickoff,
                        source="league schedule",
                        locator="schedule/01",
                        known_at=store.now_utc(),
                    ),
                )
                pipeline.prepare_builtin(root)
            coverage = {
                "both_teams_observed": True,
                "observed_teams": 2,
                "home": {
                    "team": HOME,
                    "observed": True,
                    "sources": [
                        {
                            "id": "club-home",
                            "status": "ok",
                            "checked_at": "2023-08-11T12:00Z",
                        }
                    ],
                },
                "away": {
                    "team": AWAY,
                    "observed": True,
                    "sources": [
                        {
                            "id": "club-away",
                            "status": "ok",
                            "checked_at": "2023-08-11T12:00Z",
                        }
                    ],
                },
            }

            def snapshot(_root, fixture_id, cutoff):
                f = claim()
                return {
                    "fixture_id": fixture_id,
                    "cutoff": cutoff,
                    "facts": [f]
                    if store.instant(f["known_at"]) <= store.instant(cutoff)
                    else [],
                    "coverage": coverage,
                }

            with patch("polymarket861.news.snapshot", side_effect=snapshot):
                with at("2023-08-11T13:59Z"):
                    early = pipeline.make_forecast(root, fid, history, horizon="T-24h")
                with at(CUT):
                    sealed = pipeline.make_forecast(root, fid, history, horizon="T-60m")
            self.assertFalse(early["news_pair"]["changed"])
            self.assertTrue(sealed["news_pair"]["changed"])
            pending = news_evaluation.evaluate_round(root, season, 1)
            self.assertEqual(
                (pending["sealed"], pending["scored"], pending["expected"]), (1, 0, 10)
            )

            def finish(h, a, stamp):
                from test_news_outcome_admission import admit_final

                admit_final(root, HOME, AWAY, kickoff, [h, a], stamp)

            finish(0, 2, "2023-08-12T18:00Z")
            first = news_evaluation.evaluate_round(root, season, 1)
            self.assertEqual(1, first["scored"])
            self.assertEqual(1, first["changed"])
            self.assertEqual(1, first["observed_both"])
            self.assertEqual(1, first["affected"]["count"])
            self.assertGreater(first["delta"], 0)
            self.assertEqual(
                first["by_news_status"]["confirmed_only"]["delta"], first["delta"]
            )
            self.assertEqual(12, len(first["lines"]))
            self.assertEqual(first, news_evaluation.evaluate_round(root, season, 1))
            self.assertEqual(first["verdict"], "insufficient_evidence")
            self.assertEqual(
                0.0,
                news_evaluation.evaluate_round(root, season, 1, horizon="T-24h")[
                    "delta"
                ],
            )
            research = news_evaluation.evaluate_season(root, season)
            self.assertEqual((research["scored"], research["expected"]), (1, 380))
            finish(3, 0, "2023-08-13T10:00Z")
            corrected = news_evaluation.evaluate_round(root, season, 1)
            self.assertLess(corrected["delta"], 0)
            self.assertEqual(2, corrected["fixtures"][0]["result_version"])
            self.assertEqual(sealed["digest"], corrected["fixtures"][0]["seal"])
            with closing(store.connect(root)) as db, db:
                db.execute(
                    "INSERT INTO result_event_times VALUES(?,?,?,?)",
                    (
                        fid,
                        2,
                        store.iso("2023-08-12T11:00Z"),
                        "verified different kickoff",
                    ),
                )
            blocked = news_evaluation.evaluate_round(root, season, 1)
            self.assertEqual(0, blocked["scored"])
            self.assertEqual(1, blocked["exclusions"]["changed_actual_kickoff"])

    def test_season_interval_is_clustered_and_early_sample_remains_inconclusive(self):
        left = football.score_probabilities(model()["probabilities"], 0, 2)
        left["score"] = 50.0
        right = dict(left, score=52.0)
        rows = [
            dict(
                fixture_id=f"f{i}",
                season="2023-24",
                round_id=i + 1,
                scored=True,
                sealed=True,
                completed=True,
                observed_both=True,
                changed=True,
                recipe="r",
                implementation="one build",
                news_slice="reported_only",
                scores={"no_news": left, "with_news": right},
                lines={},
            )
            for i in range(4)
        ]
        out = news_evaluation._summary(rows, "2023-24", None, "T-60m", "technical")
        self.assertEqual(out["delta"], 2.0)
        self.assertEqual(out["interval"]["rounds"], 4)
        self.assertEqual(out["interval"]["family_comparisons"], 10)
        self.assertEqual(out["verdict"], "insufficient_evidence")
