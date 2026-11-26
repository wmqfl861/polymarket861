from contextlib import closing
from datetime import timedelta
import unittest
from unittest.mock import patch

import test_opportunity as venue
import test_round_comparisons as comparisons_fixture
import test_schedule_consensus as schedule_fixture
from polymarket861 import (
    kickoff_proof,
    news_market,
    result_admission,
    schedule_consensus,
    store,
)


class ResearchScheduleCloseTests(unittest.TestCase):
    def setUp(self):
        self.case = comparisons_fixture.RoundComparisons()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root
        self.post = venue.KICK + timedelta(hours=3)
        self.final = {
            "fixture_id": venue.FID,
            "kickoff": store.iso(venue.KICK),
            "status": "final",
            "period": "regulation",
            "confirmed_at": store.iso(self.post),
            "proof": [{"sha256": "e" * 64}],
        }
        self.evidence = {
            "openligadb": {"sha256": "a" * 64},
            "gamma": {"sha256": "b" * 64},
        }
        self.case.receive(
            self.case.event | {"live": None, "ended": None},
            venue.KICK - timedelta(minutes=3),
        )

    def report(self, final=True):
        with (
            venue.at(self.post),
            patch.object(
                result_admission,
                "verified_season_regulation",
                return_value={venue.FID: self.final} if final else {},
            ),
            patch.object(
                news_market,
                "_schedule_pair_at_book",
                return_value=(venue.SLUG, self.evidence),
            ),
        ):
            return news_market.close_reference(self.root, venue.FID)

    def test_true_window_borders_native_books_missing_flags_and_scope_isolation(self):
        for seconds in (121, 9):
            self.case.batch(venue.KICK - timedelta(seconds=seconds))
        t = self.report()
        self.assertTrue(t["research_only"])
        self.assertFalse(t["eligible_for_formal_clv"])
        self.assertFalse(t["eligible_for_promotion"])
        self.assertTrue(
            all(not v["qualified_families"] for v in t["tiers"].values()), t
        )
        for seconds in (120, 10):
            self.case.batch(venue.KICK - timedelta(seconds=seconds))
            out = self.report()["tiers"]
            qualified = out["two_source_schedule"]
            self.assertEqual(qualified["qualified_families"], 4, qualified)
            self.assertEqual(
                qualified["family_receipts"]["total"]["received_at"],
                store.iso(venue.KICK - timedelta(seconds=seconds)),
            )
            self.assertIn(
                "openligadb", qualified["family_receipts"]["total"]["schedule_sources"]
            )
            self.assertEqual(out["actual_first_whistle"]["qualified_families"], 0)
            self.assertEqual(out["explicit_exchange_pregame"]["qualified_families"], 0)
        self.assertEqual(
            self.report(final=False)["tiers"]["two_source_schedule"][
                "qualified_families"
            ],
            0,
        )
        with patch.object(
            kickoff_proof,
            "resolve",
            return_value={
                "actual_kickoff": store.iso(venue.KICK - timedelta(seconds=1)),
                "proof_id": "early",
            },
        ):
            row = self.report()["tiers"]["two_source_schedule"]
            self.assertIn("early_actual_kickoff_observed", row["missing"].values())
        self.case.receive(
            self.case.event | {"live": True}, venue.KICK - timedelta(seconds=1)
        )
        row = self.report()["tiers"]["two_source_schedule"]
        self.assertEqual(row["qualified_families"], 0)
        self.assertIn("exchange_start_or_end_observed", row["missing"].values())

    def test_postmatch_exchange_revision_rejects_previously_eligible_reference(self):
        self.case.batch(venue.KICK - timedelta(seconds=60))
        self.assertEqual(
            self.report()["tiers"]["two_source_schedule"]["qualified_families"], 4
        )
        self.case.receive(
            self.case.event
            | {"startTime": store.iso(venue.KICK - timedelta(minutes=1))},
            venue.KICK + timedelta(minutes=90),
        )
        row = self.report()["tiers"]["two_source_schedule"]
        self.assertEqual(row["qualified_families"], 0)
        self.assertIn(
            "retrospective_exchange_schedule_revision", row["missing"].values()
        )

    def test_failed_native_consensus_and_earlier_clock_in_later_score_are_held(self):
        self.case.batch(venue.KICK - timedelta(seconds=70))
        self.final["kickoff"] = store.iso(venue.KICK - timedelta(minutes=2))
        row = self.report()["tiers"]["two_source_schedule"]
        self.assertIn("result_source_reports_early_kickoff", row["missing"].values())
        self.final["kickoff"] = store.iso(venue.KICK)
        with (
            venue.at(self.post),
            patch.object(
                result_admission,
                "verified_season_regulation",
                return_value={venue.FID: self.final},
            ),
        ):
            row = news_market.close_reference(self.root, venue.FID)["tiers"][
                "two_source_schedule"
            ]
            self.assertEqual(row["qualified_families"], 0)
            self.assertIn("schedule confirmation", row["missing"]["result"])


class ScheduleWitnessTests(unittest.TestCase):
    def test_authenticates_both_original_collectors_and_rejects_stale_pair(self):
        s = schedule_fixture.ProspectiveSchedule()
        s.setUp()
        self.addCleanup(s.doCleanups)
        scheduled = schedule_fixture.OLD
        book = store.iso(scheduled - timedelta(seconds=110))
        s.time = scheduled - timedelta(hours=25)
        s.ol(scheduled)
        slug = s.gamma(scheduled)
        schedule_consensus.sync(s.root, schedule_fixture.YEAR)
        with closing(store.connect(s.root)) as db:
            f = schedule_consensus._registration(db, schedule_fixture.FID, 1)
            with self.assertRaisesRegex(ValueError, "stale|24h"):
                news_market._schedule_pair_at_book(db, s.root, f, 1, book, scheduled)
        s.time = scheduled - timedelta(hours=2)
        s.ol(scheduled)
        s.gamma(scheduled)
        with closing(store.connect(s.root)) as db:
            f = schedule_consensus._registration(db, schedule_fixture.FID, 1)
            actual_slug, proof = news_market._schedule_pair_at_book(
                db, s.root, f, 1, book, scheduled
            )
            self.assertEqual(actual_slug, slug)
            self.assertEqual(set(proof), {"openligadb", "gamma"})
            self.assertNotEqual(proof["openligadb"]["sha256"], proof["gamma"]["sha256"])
            self.assertTrue(
                all(row["received_at"] == store.iso(s.time) for row in proof.values())
            )
        s.time = scheduled - timedelta(minutes=30)
        s.ol(scheduled - timedelta(minutes=1))
        with closing(store.connect(s.root)) as db:
            with self.assertRaises(ValueError):
                news_market._schedule_pair_at_book(db, s.root, f, 1, book, scheduled)
