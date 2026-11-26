"""Scheduled event-clock evidence and the predeclared v2 market slope."""

from collections import defaultdict
from contextlib import closing
from datetime import timedelta
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scipy.stats import t as student

import test_opportunity as venue
import test_round_comparisons as native
from polymarket861 import (
    kickoff_proof,
    market_archive,
    market_provider,
    news_accumulation,
    news_evaluation,
    news_market,
)
from polymarket861 import news_study_plan as plan
from polymarket861 import pipeline, store
from test_news_accumulation import coverage
from test_news_matchday import AWAY, FID, HOME, SEASON, seed_fixture
from test_paired_proof import at


class ScheduledProxyTests(unittest.TestCase):
    def setUp(self):
        self.case = native.RoundComparisons()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root
        self.game = store.fixture_at(self.root, venue.FID)
        self.cut = venue.CUT - timedelta(minutes=1)
        self.entry = {"sealed_at": store.iso(self.cut), "mode": "technical"}
        p = self.case.forecast["probabilities"]
        self.pair = {
            "coverage": coverage(venue.HOME, venue.AWAY, self.cut),
            "input_cutoff": store.iso(self.cut),
            "no_news": {"probabilities": p},
            "with_news": {"probabilities": p | {"home_win": p["home_win"] + 0.005}},
        }
        self.case.batch(self.cut - timedelta(seconds=3))

    def observe(self, *, proxy=True, when=None):
        with (
            patch.dict(kickoff_proof.OPEN_RELEASES, clear=True),
            patch.dict(kickoff_proof.FREE_ESPN_PERMISSIONS, clear=True),
            venue.at(when or (venue.KICK + timedelta(hours=2))),
            closing(store.connect(self.root)) as db,
        ):
            return news_market.observe(
                db, self.root, self.game, self.entry, self.pair, scheduled_proxy=proxy
            )

    def gamma(self, offset, **event):
        self.case.receive(
            self.case.event | event, venue.KICK + timedelta(seconds=offset)
        )

    def batch(self, offset, **kwargs):
        self.case.batch(venue.KICK + timedelta(seconds=offset), **kwargs)

    def catalog(self, offset, events):
        when = venue.KICK + timedelta(seconds=offset)
        with (
            patch.object(market_archive, "league_tag", return_value="epl"),
            patch.object(
                market_provider,
                "get",
                side_effect=lambda url, params: venue.reply(events, when, url),
            ),
        ):
            market_archive.catalog(self.root, 2025, pages=1)

    def test_stock_separate_from_true_close_and_strict_schedule_boundary(self):
        self.gamma(-23)
        self.batch(-12)
        self.batch(0, shift=2)
        self.batch(9, shift=4)
        self.gamma(11, live=True)
        self.assertEqual(
            self.observe(when=venue.KICK - timedelta(seconds=1))["reason"],
            "awaiting_scheduled_kickoff",
        )
        self.assertEqual(
            self.observe(proxy=False)["reason"], "missing_verified_actual_kickoff"
        )
        row = self.observe()
        self.assertEqual(row["schedule_version"], self.game["version"])
        self.assertEqual(
            set(row["endpoints"]), set(self.case.forecast["probabilities"])
        )
        original = row["receipts"]["result"]["close"]
        self.assertEqual(
            original["received_at"], store.iso(venue.KICK - timedelta(seconds=12))
        )
        self.assertEqual(
            original["explicit_pregame_event"]["received_at"],
            store.iso(venue.KICK - timedelta(seconds=23)),
        )
        self.assertNotIn("kickoff_proof", row)
        # A book can be genuine even on a day with no verified first-half event.
        self.assertGreater(row["endpoints"]["home_win"]["decision"], 0)
        self.assertTrue(row["receipts"]["result"]["contracts"]["home_win"])

    def test_last_eligible_book_retains_earlier_fresh_gamma_and_positive_state_is_monotone(
        self,
    ):
        self.gamma(-43)
        self.batch(-18, shift=1)
        self.batch(-3, shift=2)
        result = self.observe()
        self.assertEqual(
            result["receipts"]["result"]["close"]["received_at"],
            store.iso(venue.KICK - timedelta(seconds=18)),
        )
        self.gamma(-14, live=True)
        self.gamma(-9)
        # Reverting to pregame cannot qualify -3. A positive only after -18
        # does not erase the book genuinely received before it.
        self.assertEqual(
            self.observe()["receipts"]["result"]["close"]["received_at"],
            store.iso(venue.KICK - timedelta(seconds=18)),
        )
        self.gamma(-55, ended=True)
        with closing(store.connect(self.root)) as db, db:
            db.execute("UPDATE ma_event_states SET live=0,ended=0")
        excluded = self.observe()
        self.assertFalse(excluded["endpoints"])
        self.assertEqual(
            excluded["missing"]["result"]["close"], "exchange_start_or_end_observed"
        )

    def test_missing_status_native_original_and_revised_schedule_fail_closed(self):
        self.batch(-12)
        stale = self.observe()
        self.assertFalse(stale["endpoints"])
        self.assertEqual(
            stale["missing"]["result"]["close"], "stale_exchange_pregame_state"
        )
        self.gamma(-19, ended=None)
        self.batch(-5)
        self.assertNotIn("result", self.observe()["receipts"])
        # Amending kickoff and then restoring the old time still changes which
        # revision the forecast described.
        with venue.at(self.cut + timedelta(minutes=10)):
            store.register_fixture(
                self.root,
                {
                    k: self.game[k]
                    for k in ("fixture_id", "season", "round_id", "home", "away")
                }
                | {
                    "kickoff": store.iso(venue.KICK + timedelta(minutes=10)),
                    "known_at": store.now_utc(),
                    "source": "test",
                    "locator": "test://delay",
                },
            )
        with venue.at(self.cut + timedelta(minutes=20)):
            store.register_fixture(
                self.root,
                {
                    k: self.game[k]
                    for k in (
                        "fixture_id",
                        "season",
                        "round_id",
                        "home",
                        "away",
                        "kickoff",
                    )
                }
                | {
                    "known_at": store.now_utc(),
                    "source": "test",
                    "locator": "test://restored",
                },
            )
        self.assertEqual(self.observe()["reason"], "scheduled_fixture_revision_changed")

    def test_original_live_request_survives_missing_event_and_state_indexes(self):
        self.gamma(-25, live=True)
        self.gamma(-15)
        self.batch(-8)
        stamp = store.iso(venue.KICK - timedelta(seconds=25))
        with closing(store.connect(self.root)) as db, db:
            sha = db.execute(
                "SELECT sha FROM ma_requests WHERE operation='event' AND selector=? AND received_at=? AND status='ok'",
                (venue.SLUG, stamp),
            ).fetchone()[0]
            self.assertEqual(
                db.execute(
                    "SELECT live FROM ma_event_states WHERE slug=? AND received_at=? AND sha=?",
                    (venue.SLUG, stamp, sha),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute(
                    "DELETE FROM ma_events WHERE slug=? AND received_at=?",
                    (venue.SLUG, stamp),
                ).rowcount,
                1,
            )
        for surviving_indexes in (1, 0):
            with self.subTest(surviving_indexes=surviving_indexes):
                excluded = self.observe()
                self.assertFalse(excluded["endpoints"])
                self.assertEqual(
                    excluded["missing"]["result"]["close"],
                    "exchange_start_or_end_observed",
                )
            with closing(store.connect(self.root)) as db, db:
                db.execute(
                    "DELETE FROM ma_event_states WHERE slug=? AND received_at=?",
                    (venue.SLUG, stamp),
                )
        # Even with the request subsequently lost, its original observation
        # remains visible as a hole. A later clean state cannot fill it.
        with closing(store.connect(self.root)) as db, db:
            db.execute(
                "DELETE FROM ma_requests WHERE sha=? AND received_at=?", (sha, stamp)
            )
        unknown = self.observe()
        self.assertFalse(unknown["endpoints"])
        self.assertEqual(
            unknown["missing"]["result"]["close"], "unverified_exchange_state_history"
        )

    def test_catalogue_scopes_members_and_reopens_unindexed_auxiliary_original(self):
        unrelated = venue.event("epl-oth-alt-2026-03-01") | {"ended": True}
        self.catalog(-26, [unrelated])
        self.gamma(-15)
        self.batch(-8)
        self.assertEqual(
            set(self.observe()["endpoints"]), set(self.case.forecast["probabilities"])
        )
        aux = venue.SLUG + "-more-markets"
        self.catalog(-25, [unrelated, venue.event(aux) | {"live": True}])
        stamp = store.iso(venue.KICK - timedelta(seconds=25))
        with closing(store.connect(self.root)) as db, db:
            db.execute(
                "DELETE FROM ma_events WHERE slug=? AND received_at=?", (aux, stamp)
            )
            db.execute(
                "DELETE FROM ma_event_states WHERE slug=? AND received_at=?",
                (aux, stamp),
            )
        absent = self.observe()
        self.assertFalse(absent["endpoints"])
        self.assertEqual(
            absent["missing"]["result"]["close"], "exchange_start_or_end_observed"
        )

    def test_positive_state_index_conflict_and_orphan_clean_direct_request_hold(self):
        self.gamma(-25)
        self.gamma(-15)
        self.batch(-8)
        stamp = store.iso(venue.KICK - timedelta(seconds=25))
        with closing(store.connect(self.root)) as db, db:
            db.execute(
                "UPDATE ma_event_states SET ended=1 WHERE slug=? AND received_at=?",
                (venue.SLUG, stamp),
            )
        self.assertEqual(
            self.observe()["missing"]["result"]["close"],
            "unverified_exchange_state_history",
        )
        with closing(store.connect(self.root)) as db, db:
            db.execute(
                "DELETE FROM ma_events WHERE slug=? AND received_at=?",
                (venue.SLUG, stamp),
            )
            db.execute(
                "DELETE FROM ma_event_states WHERE slug=? AND received_at=?",
                (venue.SLUG, stamp),
            )
        self.assertEqual(
            self.observe()["missing"]["result"]["close"],
            "unverified_exchange_state_history",
        )


def regression_row(era, season, rnd, x, y):
    base = 0.4
    data = {"home_win": news_market._measure(base, base + x, base, base + y)}
    return {
        "season": season,
        "round_id": rnd,
        "study_plan_sha256": plan.PLAN_SHA256,
        "study_status": "declared",
        "implementation": era,
        "market_close": {"endpoints": data, "missing": {}},
        "scheduled_market_close": {"endpoints": data, "missing": {}},
    }


class SlopeAndStudyTests(unittest.TestCase):
    def test_zero_information_rounds_do_not_tighten_slope_interval_in_either_fit(self):
        season = "2026-27"
        sparse = [
            regression_row("a", season, 1, 0.02, 0.016),
            regression_row("a", season, 2, -0.02, -0.024),
        ]
        padded = sparse + [
            regression_row("a", season, r, 0, 0.01) for r in range(3, 39)
        ]
        for field in ("market_close", "scheduled_market_close"):
            original = news_market.regression(sparse, field)
            result = news_market.regression(padded, field)
            self.assertEqual(result["rounds"], 38)
            self.assertEqual(result["informative_rounds"], 2)
            self.assertEqual(result["interval"]["degrees_of_freedom"], 1)
            self.assertEqual(result["interval"], original["interval"])
            self.assertLess(result["interval"]["lower"], 0)
            self.assertAlmostEqual(result["slope"], 1)

        # A nonzero raw movement equal to its era mean is zero on the pooled
        # slope axis. Exercise two era intercepts in the same official rounds.
        core, flat = [], []
        for era, center in (("a", 0.05), ("b", -0.03)):
            for rnd, dx, dy in (
                (1, 0.01, 0.02),
                (1, 0.03, 0.01),
                (2, -0.01, -0.01),
                (2, -0.03, -0.02),
            ):
                core.append(regression_row(era, season, rnd, center + dx, center + dy))
            flat.extend(
                regression_row(era, season, r, center, center) for r in range(3, 39)
            )
        original = news_market.regression(core, fixed_effects=True)
        result = news_market.regression(core + flat, fixed_effects=True)
        self.assertEqual(
            (
                result["rounds"],
                result["informative_rounds"],
                result["informative_pairs"],
            ),
            (38, 2, 8),
        )
        self.assertEqual(result["interval"]["degrees_of_freedom"], 1)
        for name in ("lower", "upper", "standard_error"):
            self.assertAlmostEqual(result["interval"][name], original["interval"][name])

    def test_known_slope_and_round_clustered_sandwich(self):
        sample = [
            ("2026-27", 1, -0.02, -0.010),
            ("2026-27", 1, 0.0, 0.008),
            ("2027-28", 1, 0.04, 0.025),
            ("2027-28", 2, -0.01, -0.012),
            ("2027-28", 2, 0.03, 0.022),
            ("2027-28", 3, 0.02, 0.004),
        ]
        rows = [regression_row("a", *i) for i in sample]
        xx = sum(x * x for _, _, x, y in sample)
        expected = sum(x * y for _, _, x, y in sample) / xx
        influence = defaultdict(float)
        for s, r, x, y in sample:
            influence[s, r] += x * (y - expected * x)
        g = len(influence)
        se = math.sqrt(g / (g - 1) * sum(v * v for v in influence.values())) / xx
        beta = news_market.regression(rows, "scheduled_market_close")
        self.assertEqual(
            (beta["pairs"], beta["informative_pairs"], beta["rounds"]), (6, 5, 4)
        )
        self.assertAlmostEqual(beta["slope"], expected)
        self.assertAlmostEqual(beta["interval"]["standard_error"], se)
        self.assertAlmostEqual(
            beta["interval"]["upper"], expected + se * student.ppf(1 - 0.025 / 6, g - 1)
        )
        unregistered = rows[0] | {"study_plan_sha256": plan.ORIGINAL_PLAN_SHA256}
        self.assertEqual(news_market.regression([unregistered] + rows), beta)
        mixed = rows + [regression_row("b", "2028-29", 1, 0.02, 0.1)]
        self.assertEqual(
            news_market.regression(mixed)["status"], "multiple_probability_eras"
        )
        self.assertEqual(
            news_market.regression(mixed, fixed_effects=True)["method"],
            "within_era_fixed_effects",
        )

    def test_half_response_with_zero_distance_gain_zeros_and_degenerate_uncertainty(
        self,
    ):
        rows = [
            regression_row("e", "2026-27", i + 1, x, 0.5 * x)
            for i, x in enumerate([-0.04, -0.02, 0, 0.02, 0.04])
        ]
        pair = news_market.regression(rows)
        self.assertAlmostEqual(pair["slope"], 0.5)
        self.assertIsNone(pair["interval"])
        self.assertEqual(pair["status"], "no_estimable_cluster_variance")
        self.assertAlmostEqual(
            sum(
                r["market_close"]["endpoints"]["home_win"]["distance_gain_pp"]
                for r in rows
            ),
            0,
        )
        self.assertIsNone(news_market.regression([])["slope"])
        zeros = [regression_row("e", "2026-27", i, 0, 0.03) for i in (1, 2)]
        self.assertEqual(
            news_market.regression(zeros)["status"], "no_informative_news_movements"
        )
        same = [regression_row("e", "2026-27", i, 0.02, 0.03) for i in (1, 2)]
        self.assertEqual(
            news_market.regression(same)["status"], "no_variation_in_news_movements"
        )
        self.assertEqual(
            news_market.regression(same + zeros)["status"],
            "no_variation_in_news_movements",
        )
        self.assertEqual(
            news_market.regression(rows[:1])["status"],
            "insufficient_independent_rounds",
        )
        self.assertEqual(
            news_accumulation.guardrail({"interval": {"lower": -2, "upper": -0.02}})[
                "status"
            ],
            "evidence_of_score_deterioration",
        )

    def test_normal_primary_seal_to_stock_scheduled_price_reports_before_the_score(
        self,
    ):
        from test_news_matchday import KICKOFF

        kick = store.instant(KICKOFF)
        cutoff = kick - timedelta(minutes=61)
        slug = "epl-ars-che-2026-08-12"
        # The test publisher uses the same native JSON schema as the exchange.
        raw = json.dumps(native.catalog())
        for original, replacement in (
            (venue.SLUG + "-nor", slug + "-ars"),
            (venue.SLUG + "-sou", slug + "-che"),
            (venue.SLUG, slug),
            (venue.HOME, HOME),
            (venue.AWAY, AWAY),
            ('"nor"', '"ars"'),
            ('"sou"', '"che"'),
            (store.iso(venue.KICK), store.iso(kick)),
            ("March 1", "August 12"),
        ):
            raw = raw.replace(original, replacement)
        with tempfile.TemporaryDirectory() as tmp:
            root, history = Path(tmp) / "runtime", Path(tmp) / "history"
            with at("2026-08-01T10:00Z"):
                seed_fixture(root, history)
            archive = native.RoundComparisons()
            archive.root, archive.event = root, json.loads(raw)
            auxiliary_event = venue.event
            with (
                patch.multiple(venue, SLUG=slug, KICK=kick),
                patch.object(
                    venue,
                    "event",
                    side_effect=lambda tag: auxiliary_event(tag) | {"teams": []},
                ),
            ):
                archive.receive(archive.event, cutoff - timedelta(minutes=3))
                archive.batch(cutoff - timedelta(seconds=3))
                snap = {
                    "fixture_id": FID,
                    "cutoff": store.iso(cutoff),
                    "facts": [],
                    "coverage": coverage(HOME, AWAY, cutoff),
                }
                with (
                    patch("polymarket861.news.snapshot", return_value=snap),
                    at(store.iso(cutoff)),
                ):
                    seal = pipeline.make_forecast(root, FID, history)
                archive.receive(archive.event, kick - timedelta(seconds=22))
                archive.batch(kick - timedelta(seconds=8), shift=2)
                with at(store.iso(kick + timedelta(minutes=1))):
                    rnd = news_evaluation.evaluate_round(root, SEASON, 1)
                    total = news_evaluation.evaluate_accumulation(root, SEASON)
            self.assertEqual((rnd["sealed"], rnd["scored"]), (1, 0))
            for report in (rnd, total, total["eras"][seal["news_study"]["era_sha256"]]):
                self.assertEqual(report["market_close"]["home_win_pairs"], 0)
                self.assertEqual(
                    report["scheduled_market_close"]["home_win_pairs"],
                    1,
                    report["scheduled_market_close"],
                )
                self.assertEqual(
                    report["scheduled_market_close"]["home_win_regression"][
                        "informative_pairs"
                    ],
                    0,
                )
            self.assertTrue(
                rnd["fixtures"][0]["scheduled_market_close"]["receipts"]["result"][
                    "close"
                ]["explicit_pregame_event"]["sha256"]
            )

    def test_original_registered_design_stays_separate_and_cannot_be_relabelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, history = Path(tmp) / "runtime", Path(tmp) / "history"
            with at("2026-08-01T10:00Z"):
                seed_fixture(root, history)
            stamp = "2026-08-12T12:59Z"
            snap = {
                "fixture_id": FID,
                "cutoff": store.iso(stamp),
                "facts": [],
                "coverage": coverage(HOME, AWAY, stamp),
            }
            with patch("polymarket861.news.snapshot", return_value=snap), at(stamp):
                seal = pipeline.make_forecast(root, FID, history)
            with closing(store.connect(root)) as db, db:
                recipe = plan.sealed_recipe(db, seal)
                del seal["news_recipe_sha256"], recipe["recipe_format"]
                del recipe["news_study_plan_sha256"]
                recipe["news_probability_policy"] = list(plan.PREVIOUS_POLICY)
                seal["news_pair"]["protocol"], seal["news_pair"]["policy_sha256"] = (
                    plan.PREVIOUS_POLICY
                )
                seal["recipe_digest"] = store.digest(recipe)
                del seal["digest"]
                original = plan.declaration(recipe, seal["news_pair"], snap)
                seal["news_study"] = original
                db.execute(
                    "UPDATE model_recipes SET recipe=? WHERE version=?",
                    (store.canonical(recipe), seal["model_version"]),
                )
                db.execute(
                    "UPDATE forecast_seals SET digest=?,data_json=?",
                    (store.digest(seal), store.canonical(seal)),
                )
            study = news_evaluation.evaluate_accumulation(root, SEASON)
            self.assertEqual(study["eligible_seals"], 0)
            legacy = study["previous_plans"][plan.ORIGINAL_PLAN_SHA256]
            self.assertEqual(legacy["eligible_seals"], 1)
            self.assertEqual(legacy["plan"]["practical_effect"]["score_points"], 0.10)
            self.assertIn("power", legacy["eras"][original["era_sha256"]]["score"])
            by_policy = news_evaluation.evaluate_season(root, SEASON, policy="v2")
            self.assertEqual(by_policy["policy_sha256"], plan.PREVIOUS_POLICY[1])
            self.assertEqual(
                by_policy["fixtures"][0]["scheduled_market_close"]["reason"],
                "requires_scheduled_proxy_declaration",
            )
            with closing(store.connect(root)) as db, db:
                seal["news_study"] = original | {
                    "plan_version": plan.PLAN["version"],
                    "plan_sha256": plan.PLAN_SHA256,
                }
                db.execute(
                    "UPDATE forecast_seals SET digest=?,data_json=?",
                    (store.digest(seal), store.canonical(seal)),
                )
            invalid = news_evaluation.evaluate_accumulation(root, SEASON)
            self.assertFalse(invalid["previous_plans"])
            self.assertEqual(
                invalid["exclusions"], {"different_or_invalid_study_declaration": 1}
            )
