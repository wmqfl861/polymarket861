"""Dual-schedule endpoint admission through native schedule, book, score and first-whistle inputs."""

from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_news_scheduled_market as scheduled_news
import test_opportunity as venue
import test_round_comparisons as books
import test_schedule_consensus as schedule
from kickoff_fixture import capture_example
from test_news_accumulation import coverage
from test_result_feed import match

from polymarket861 import (
    kickoff_proof,
    market_archive,
    market_provider,
    news_endpoint_power,
    news_endpoint_reference,
    news_market,
    result_admission,
    result_feed,
    schedule_consensus,
    store,
)


class DualScheduleEndpoints(unittest.TestCase):
    def setUp(self):
        self.clock = schedule.ProspectiveSchedule()
        self.clock.setUp()
        self.addCleanup(self.clock.doCleanups)
        self.root = self.clock.root
        self.kick = schedule.OLD
        self.slug = "epl-nor-sou-2026-11-28"
        self.cutoffs = [self.kick - timedelta(hours=h) for h in (24, 1)]
        raw = json.dumps(books.catalog())
        for old, new in (
            (venue.SLUG, self.slug),
            (store.iso(venue.KICK), store.iso(self.kick)),
            ("March 1", "November 28"),
            ("11:30 AM ET", "10:00 AM ET"),
        ):
            raw = raw.replace(old, new)
        self.event = json.loads(raw) | {
            "active": True,
            "archived": False,
            "closed": False,
            "tags": [{"slug": "soccer"}, {"slug": "premier-league"}],
        }
        del self.event["live"], self.event["ended"]
        for market in self.event["markets"]:
            market["gameStartTime"] = store.iso(self.kick)
        self.book = books.RoundComparisons()
        self.book.root, self.book.event = self.root, self.event
        # The initial registry fixture and evidence really predate both horizons.
        self.schedules(self.kick - timedelta(hours=26))
        self.assertEqual(
            schedule_consensus.sync(self.root, schedule.YEAR)["matched"], 1
        )
        self.fixture = store.fixture_at(self.root, schedule.FID, prospective=True)
        self.stamps = [cut - timedelta(seconds=3) for cut in self.cutoffs]
        self.close = self.kick - timedelta(seconds=45)
        self.post = self.kick + timedelta(hours=3)

    def gamma(self, when, event):
        self.clock.time = when
        with patch.object(
            market_provider,
            "get",
            side_effect=lambda url, params=None: venue.reply(event, when, url),
        ):
            market_archive.observe_event(self.root, self.slug)

    def schedules(self, when):
        self.clock.time = when
        self.clock.ol(self.kick)
        self.gamma(when, self.event)

    def batch(self, stamp, **kwargs):
        with patch.object(venue, "SLUG", self.slug):
            self.book.batch(stamp, **kwargs)

    def scores(self):
        row = match(902, venue.HOME, venue.AWAY, [2, 1], rnd=schedule.RND) | {
            "matchDateTimeUTC": store.iso(self.kick),
            "leagueName": "Premier League 2026/2027",
            "goals": [],
        }
        row["team1"]["teamId"], row["team2"]["teamId"] = 61, 79
        raw = json.dumps([row]).encode()
        base = result_feed.BASE
        sources = {
            f"{base}/getmatchdata/pl/{schedule.YEAR}": raw,
            f"{base}/getmatchdata/pl/{schedule.YEAR}/{schedule.RND}": raw,
            f"{base}/getlastchangedate/pl/{schedule.YEAR}/{schedule.RND}": b'"final"',
        }
        with patch.object(result_feed, "_get", side_effect=sources.__getitem__):
            self.clock.time = self.kick + timedelta(hours=2)
            result_feed.sync_primary(self.root, schedule.YEAR)
            self.clock.time += timedelta(minutes=41)
            result_feed.sync_primary(self.root, schedule.YEAR, round_id=schedule.RND)
        self.assertEqual(
            result_admission.verified_regulation(self.root, schedule.FID)["home_goals"],
            2,
        )

    def observation(self, cut, as_of=None, *, strict=False, mode="technical"):
        probabilities = {"home_win": 0.44, "total_over_2_5": 0.52}
        entry = {"mode": mode, "sealed_at": store.iso(cut)}
        pair = {
            "coverage": coverage(venue.HOME, venue.AWAY, cut),
            "input_cutoff": store.iso(cut),
            "no_news": {"probabilities": probabilities},
            "with_news": {
                "probabilities": {"home_win": 0.445, "total_over_2_5": 0.524}
            },
        }
        when = as_of or self.post
        with closing(store.connect(self.root)) as db:
            db.execute("BEGIN")
            if strict:
                return news_market.observe(
                    db,
                    self.root,
                    self.fixture,
                    entry,
                    pair,
                    scheduled_proxy=True,
                    as_of=when,
                )
            fixture = next(
                row
                for row in store.eligible_fixture_rows(db)
                if row["fixture_id"] == schedule.FID
            )
            return news_endpoint_reference.observe(
                db, self.root, fixture, entry, pair, as_of=when
            )

    def test_both_horizons_use_native_signatures_missing_flags_and_asof_early_proof(
        self,
    ):
        self.batch(self.stamps[0])
        self.schedules(self.kick - timedelta(hours=2))
        self.batch(self.stamps[1])
        self.batch(self.close, shift=2)
        self.scores()
        for cutoff in self.cutoffs:
            self.assertEqual(
                self.observation(cutoff, self.kick + timedelta(hours=1))["reason"],
                "awaiting_verified_postmatch_result",
            )
            observed = self.observation(cutoff)
            self.assertEqual(
                set(observed.get("endpoints", {})),
                {"home_win", "total_over_2_5"},
                observed,
            )
            self.assertAlmostEqual(
                observed["endpoints"]["home_win"]["decision"], 0.50 / 1.02
            )
            self.assertAlmostEqual(
                observed["endpoints"]["home_win"]["close"], 0.52 / 1.08
            )
            self.assertAlmostEqual(
                observed["endpoints"]["total_over_2_5"]["close"], 0.62
            )
            self.assertFalse(observed["eligible_for_formal_clv"])
            self.assertFalse(self.observation(cutoff, strict=True).get("endpoints"))
            for proof in observed["receipts"].values():
                self.assertNotEqual(proof["decision"]["batch"], proof["close"]["batch"])
                for kind in ("decision", "close"):
                    row = proof[kind]
                    self.assertEqual(
                        set(row["schedule_sources"]), {"openligadb", "gamma"}
                    )
                    self.assertTrue(
                        all(
                            len(src["sha256"]) == 64
                            and src["received_at"] <= row["received_at"]
                            for src in row["schedule_sources"].values()
                        )
                    )
        baseline = self.observation(self.cutoffs[0])
        self.assertEqual(self.observation(self.cutoffs[0], mode="formal"), baseline)
        self.assertEqual(
            self.observation(self.cutoffs[0], mode="replay")["reason"],
            "requires_prospective_seal",
        )
        with closing(store.connect(self.root)) as db:
            flags = db.execute(
                "SELECT DISTINCT json_type(payload,'$.pregame') FROM ma_books"
            ).fetchall()
            self.assertEqual([row[0] for row in flags], ["null"])
        self.clock.time = self.post + timedelta(days=1)
        with patch.dict(kickoff_proof.OPEN_RELEASES, clear=True):
            capture_example(
                self.root, self.fixture, "4477001", self.kick - timedelta(seconds=1)
            )
            self.assertEqual(self.observation(self.cutoffs[0], self.post), baseline)
            self.assertEqual(
                self.observation(self.cutoffs[0], self.clock.time)["reason"],
                "early_actual_kickoff_observed",
            )

    def test_late_schedules_cannot_qualify_an_earlier_book_and_partial_families_do_not_mix(
        self,
    ):
        self.batch(self.stamps[0], omit={"21"})
        # The first schedules are more than 24h old at T-60. The refresh is after that book.
        self.batch(self.stamps[1])
        self.schedules(self.cutoffs[1] + timedelta(minutes=5))
        self.batch(self.close, omit={"42"}, shift=2)
        self.scores()
        late = self.observation(self.cutoffs[1])
        self.assertFalse(late.get("endpoints"), late)
        self.assertRegex(late["missing"]["result"]["decision"], "stale|24h")
        partial = self.observation(self.cutoffs[0])
        self.assertFalse(partial["endpoints"], partial)
        self.assertIn("decision", partial["missing"]["result"])
        self.assertIn("close", partial["missing"]["total"])
        # A subsequent complete close restores only the family complete at the original decision.
        self.batch(self.kick - timedelta(seconds=15), shift=1)
        qualified = self.observation(self.cutoffs[0])
        self.assertEqual(set(qualified["endpoints"]), {"total_over_2_5"}, qualified)
        original = qualified["receipts"]["total"]["decision"]["schedule_sources"][
            "gamma"
        ]["sha256"]
        store.blob_path(self.root, original).write_bytes(b"corrupt native schedule")
        self.assertFalse(self.observation(self.cutoffs[0]).get("endpoints"))

    def test_native_sporting_and_cancelled_statuses_with_missing_or_false_flags(self):
        self.batch(self.stamps[0])
        self.schedules(self.kick - timedelta(hours=2))
        self.batch(self.close)
        self.scores()
        self.assertEqual(len(self.observation(self.cutoffs[0])["endpoints"]), 2)
        controls = {"live": False, "ended": False}
        # Add each independent contrary original at an earlier instant so that
        # it is the first disqualifier, even after a later scheduled retraction.
        cases = [
            ({"gameStatus": "In Progress"}, "exchange_start_or_end_observed"),
            ({"status": "postponed"}, "exchange_schedule_interrupted"),
            (
                controls | {"gameStatus": "SECOND_HALF"},
                "exchange_start_or_end_observed",
            ),
            (controls | {"cancelled": True}, "exchange_schedule_interrupted"),
            ({"status": "unknown provider value"}, "unverified_exchange_state_history"),
            (controls | {"canceled": "yes"}, "unverified_exchange_state_history"),
        ]
        self.gamma(
            self.close - timedelta(seconds=1), self.event | {"status": "Scheduled"}
        )
        for offset, (fields, expected) in enumerate(cases, 3):
            with self.subTest(fields=fields):
                self.gamma(self.close - timedelta(seconds=offset), self.event | fields)
                result = self.observation(self.cutoffs[0])
                self.assertFalse(result.get("endpoints"), result)
                self.assertEqual(result["reason"], expected)


class StrongerClockStatus(unittest.TestCase):
    def test_false_booleans_cannot_hide_a_sporting_status_from_either_stronger_clock(
        self,
    ):
        case = scheduled_news.ScheduledProxyTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.gamma(-20)
        case.batch(-6)
        after = venue.KICK + timedelta(hours=3)
        with patch.dict(kickoff_proof.OPEN_RELEASES, clear=True), venue.at(after):
            capture_example(case.root, case.game, "4477002", venue.KICK)

            def result(explicit):
                with closing(store.connect(case.root)) as db:
                    return news_market.observe(
                        db,
                        case.root,
                        case.game,
                        case.entry,
                        case.pair,
                        scheduled_proxy=explicit,
                        as_of=after,
                    )

            for explicit in (True, False):
                control = result(explicit)
                self.assertIn("home_win", control["endpoints"], control)
                self.assertEqual(
                    "explicit_pregame_event" in control["receipts"]["result"]["close"],
                    explicit,
                )
            case.gamma(-4, gameStatus="In Progress")
            for explicit in (True, False):
                before_notice = result(explicit)
                self.assertEqual(
                    before_notice["receipts"]["result"]["close"]["received_at"],
                    store.iso(venue.KICK - timedelta(seconds=6)),
                )
            case.gamma(-10, status="IN-PLAY")
            case.gamma(-8, gameStatus="scheduled")
            for explicit in (True, False):
                observed = result(explicit)
                self.assertFalse(observed["endpoints"])
                self.assertEqual(
                    observed["missing"]["result"]["close"],
                    "exchange_start_or_end_observed",
                )


class RegisteredPower(unittest.TestCase):
    def test_packaged_plan_matches_the_reproducible_report_and_each_design_cell(self):
        plan = news_endpoint_power.planning()
        report = (
            Path(__file__).resolve().parents[1]
            / "reports/five-leagues-0.5.0a2/news-endpoint-power-v2.json"
        )
        self.assertEqual(plan, json.loads(report.read_bytes()))
        self.assertEqual(
            (
                plan["family_comparisons"],
                plan["replications_per_cell"],
                plan["source"]["fixtures"],
            ),
            (14, 2000, 10),
        )
        coordinates = {
            (
                r["endpoint"],
                r["layout"],
                r["matches"],
                r["assumed_gap_sd_pp"],
                r["assumed_slope"],
            )
            for r in plan["cells"]
        }
        self.assertEqual(len(coordinates), len(plan["cells"]))
        self.assertEqual(len(coordinates), 2 * 2 * 3 * 3 * 3)
        self.assertTrue(
            all(
                0 <= r["lower_bound_above_zero"] <= r["interval_defined"] <= 1
                for r in plan["cells"]
            )
        )
        plan["cells"].clear()
        self.assertEqual(len(news_endpoint_power.planning()["cells"]), 108)
