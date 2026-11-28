"""NBA review regressions. Only HTTP responses and time are controlled."""

import json
import os
import random
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from polymarket861.nba import data, markets, operations, reconciliation, storage
from polymarket861.nba.markets_contracts import stamp
from polymarket861.nba.operations_report import report
from test_nba_markets import Venue

GAME = "bdl:9001"
TEAMS = {
    abbr: {"id": num, "abbreviation": abbr, "full_name": name}
    for abbr, num, name in [
        ("NYK", 20, "New York Knicks"),
        ("MIA", 16, "Miami Heat"),
        ("BOS", 2, "Boston Celtics"),
        ("CHI", 5, "Chicago Bulls"),
        ("DEN", 8, "Denver Nuggets"),
        ("LAL", 14, "Los Angeles Lakers"),
    ]
}
FAMILIES = {"win", "total", "home_total", "away_total"}
FINAL = (118, 109)


def score_row(gid, tip, home, away, final=None):
    return {
        "id": gid,
        "date": tip.astimezone(markets.EAST).date().isoformat(),
        "season": 2026,
        "datetime": stamp(tip),
        "status": "Final" if final else stamp(tip),
        "period": 4 if final else 0,
        "time": "Final" if final else None,
        "postseason": False,
        "home_team_score": final[0] if final else 0,
        "visitor_team_score": final[1] if final else 0,
        "home_team": TEAMS[home],
        "visitor_team": TEAMS[away],
    }


class Pipeline:
    def __init__(self, root):
        self.root, self.venue = root, Venue()
        self.tip = self.venue.tip
        self.day = self.tip.astimezone(markets.EAST).date()
        self.source_tip = self.tip
        self.ignore_filter, self.missing_time = False, False
        self.neutral, self.final = None, None
        self.final_period = 4
        self.payouts, self.history = {}, []
        rng = random.Random(861)
        for days in range(20, 0, -1):
            teams = list(TEAMS)
            rng.shuffle(teams)
            for h, a in zip(teams[::2], teams[1::2], strict=True):
                hs, vs = rng.randrange(101, 128), rng.randrange(97, 125)
                self.history.append(
                    score_row(
                        1000 + len(self.history),
                        self.tip - timedelta(days=days),
                        h,
                        a,
                        (hs + int(hs == vs), vs),
                    )
                )

    def bdl(self, params, key):
        # SDK-shaped rows omit explicit season_type/neutral_site; separate request results are evidence.
        current = score_row(9001, self.source_tip, "NYK", "MIA", self.final)
        if self.final:
            current["period"] = self.final_period
        if self.missing_time:
            del current["datetime"]
        rows = self.history + [current]
        if self.neutral is not None:
            rows = [dict(row, neutral_site=self.neutral) for row in rows]
        if not self.ignore_filter and params["season_type"] != "regular":
            rows = []
        rows = [
            r for r in rows if params["start_date"] <= r["date"] <= params["end_date"]
        ]
        return httpx.Response(200, json={"data": rows, "meta": {"next_cursor": None}})

    def http(self, url, params, *, timeout):
        if url != markets.api.DATA + "/v2/tokens":
            return self.venue.get(url, params, timeout=timeout)
        token = str(params["token_id"])
        condition, amount = self.payouts[token]
        body = {
            "data": [
                {
                    "token_id": token,
                    "condition_id": condition,
                    "resolved": True,
                    "final_price": amount,
                }
            ]
        }
        now = self.venue.now
        return markets.api.Reply(
            json.dumps(body).encode(),
            stamp(now),
            stamp(now),
            {"date": format_datetime(now, usegmt=True), "age": "0"},
            url,
        )

    def advance(self, seconds):
        self.venue.now += timedelta(seconds=seconds)

    def sync(self):
        return data.sync_bdl(
            self.root,
            self.day - timedelta(days=21),
            self.day + timedelta(days=1),
            fetch=self.bdl,
            clock=self.venue.clock,
            sleep=self.advance,
        )

    def collect(self):
        return markets.collect(self.root, now=self.venue.clock)

    def tick(self):
        return operations.forecast_cycle(self.root, clock=self.venue.clock)

    def window(self, horizon="T-60m"):
        return next(
            (
                w
                for w in operations.forecast_show(self.root, GAME)["windows"]
                if w["horizon"] == horizon
            ),
            {},
        )

    def start(self, forecast_first=False):
        self.venue.now = min(self.tip, self.source_tip) - timedelta(minutes=75)
        initial = self.sync()
        venue_window = self.tip - timedelta(minutes=63)
        score_window = self.source_tip - timedelta(minutes=63)
        first = None
        if forecast_first:
            self.venue.now = score_window
            first = self.tick()
            self.advance(8)
            self.collect()
            self.advance(52)
        else:
            self.venue.now = venue_window
            self.collect()
            self.venue.now = max(self.venue.now + timedelta(seconds=2), score_window)
        return initial, first, self.tick()

    def finish(
        self,
        *,
        wrong_win=False,
        void=(),
        period="FT",
        give_score=True,
        run_forecast=True,
    ):
        self.venue.now = self.tip - timedelta(minutes=2)
        self.collect()
        self.venue.now = self.tip + timedelta(hours=3)
        home, away = FINAL
        for m in self.venue.event["markets"]:
            kind = m["sportsMarketType"]
            if kind == "moneyline":
                family, yes = "win", (home > away) != wrong_win
            else:
                side = m["marketMetadata"]["line_family"]
                family, value = {
                    "full.total": ("total", home + away),
                    "full.team_total.home": ("home_total", home),
                    "full.team_total.away": ("away_total", away),
                }[side]
                yes = value > m["line"]
            pair = (0.5, 0.5) if family in void else (int(not yes), int(yes))
            for token, amount in zip(json.loads(m["clobTokenIds"]), pair, strict=True):
                self.payouts[token] = (m["conditionId"], amount)
            m.update(
                closed=True,
                umaResolutionStatus="resolved",
                outcomePrices=json.dumps(pair),
            )
        self.venue.event.update(ended=True, period=period)
        if give_score:
            self.final = FINAL
            self.sync()
        self.collect()
        return self.tick() if run_forecast else None

    def period_report(self, kind="day", revision=None):
        day = (
            self.day if kind == "day" else self.day - timedelta(days=self.day.weekday())
        )
        return report(self.root, kind, day.isoformat(), revision)


class TestNBAReviewPipeline(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Pipeline(Path(tmp.name) / "nba")
        self.enterContext(
            patch.dict(os.environ, {"BALLDONTLIE_API_KEY": "offline-replay-key"})
        )
        self.enterContext(patch.object(markets.api, "get", self.p.http))

    def assert_paired(self, grade=True):
        w = self.p.window()
        self.assertEqual(w.get("state"), "sealed", w.get("note"))
        pred = w["prediction"]
        self.assertFalse(pred["warmup"]["eligible"])
        self.assertEqual(set(pred["market_at_prediction"]), FAMILIES)
        for ref in pred["market_at_prediction"].values():
            self.assertEqual(ref["venue_event_id"], self.p.venue.event["id"])
            self.assertLessEqual(ref["available_at"], pred["input_cutoff"])
        if grade:
            g = w["grade_history"][-1]["grade"]
            self.assertEqual((g["state"], g["final"]), ("scored", list(FINAL)))
            self.assertEqual(set(g["benchmarks"]), FAMILIES)
            self.assertEqual(set(g["payout_reconciliation"]), FAMILIES)
            self.assertEqual(
                self.p.period_report()["report"]["horizons"]["T-60m"]["grades"],
                {"scored": 1},
            )
        return pred

    def test_market_first_unknown_venue_uses_listed_home_and_scores_all_four(self):
        sync, _, _ = self.p.start()
        self.assertTrue(sync["complete"])
        self.p.finish()
        pred = self.assert_paired()
        self.assertEqual(pred["forecast"]["venue_basis"], "assumed_listed_home")
        self.assertGreater(pred["forecast"]["models"]["N0"]["p_home_win"], 0.5)
        totals = self.p.period_report()["report"]["payout_reconciliation"]
        self.assertEqual(
            (totals["native_conditions"], totals["counts"]["match"]), (4, 4)
        )
        self.assertEqual(
            totals["verified_overtime_total_games"], 0
        )  # "FT" is ambiguous.

    def test_forecast_first_waits_for_next_tick_and_freezes_all_quotes(self):
        _, first, second = self.p.start(forecast_first=True)
        self.assertEqual(first["sealed"], 0)
        self.assertEqual(second["sealed"], 1)
        self.p.finish()
        self.assert_paired()

    def test_same_day_ten_minute_difference_preserves_both_schedules(self):
        self.p.source_tip += timedelta(minutes=10)
        self.p.start()
        self.p.finish()
        window = self.p.window()
        self.assertEqual(window["state"], "sealed")
        pred = window["prediction"]
        self.assertEqual(pred["market_at_prediction"], {})
        self.assertEqual(pred["decision_quote_state"], "no_shared_decision_window")
        self.assertEqual(window["grade_history"][-1]["grade"]["benchmarks"], {})
        match = pred["market_schedule"]
        self.assertEqual(match["status"], "within_tolerance")
        self.assertEqual(abs(match["delta_seconds"]), 600)
        self.assertEqual(match["source_kickoff"], stamp(self.p.source_tip))
        self.assertEqual(match["venue_kickoff"], stamp(self.p.tip))
        self.assertEqual(
            self.p.period_report()["report"]["payout_reconciliation"]["counts"][
                "match"
            ],
            4,
        )

    def test_native_snapshot_before_overlap_and_fresh_paired_snapshot_inside_it(self):
        p = self.p
        p.source_tip += timedelta(minutes=2)
        p.venue.now = p.tip - timedelta(minutes=75)
        self.assertTrue(p.sync()["complete"])
        p.venue.now = p.tip - timedelta(minutes=64)
        p.collect()
        original_at = stamp(p.venue.now)
        p.venue.now = p.tip - timedelta(minutes=62, seconds=55)
        self.assertEqual(p.tick()["waiting_for_win_quote"], 1)
        for market in p.venue.event["markets"]:
            if market["sportsMarketType"] == "moneyline":
                market["test_cents"] = 74
        p.venue.now = p.tip - timedelta(minutes=62)
        p.collect()
        p.advance(2)
        self.assertEqual(p.tick()["sealed"], 1)
        ref = p.window()["prediction"]["market_at_prediction"]["win"]
        self.assertEqual(ref["probability"], 0.74)
        self.assertGreater(ref["observed_at"], original_at)
        p.finish()
        g = p.window()["grade_history"][-1]["grade"]
        self.assertEqual(g["benchmarks"]["win"]["market_probability"], 0.74)
        h = p.period_report()["report"]["horizons"]["T-60m"]
        self.assertEqual(h["readiness"]["all_seals_win_pairing_rate"], 1.0)

    def test_live_score_under_another_id_with_no_tip_blocks_forecast_without_market_catalogue(
        self,
    ):
        p = self.p
        other = score_row(9002, p.source_tip, "NYK", "MIA")
        other.update(datetime=None, status="Q1", period=1, home_team_score=4)
        p.history.append(other)
        p.venue.now = p.tip - timedelta(minutes=70)
        p.sync()
        p.venue.now = p.tip - timedelta(minutes=61)
        result = p.tick()
        self.assertEqual(result["sealed"], 0)
        self.assertIn("ambiguous_score_source_fixture", str(result["fixture_errors"]))

    def test_ignored_phase_filter_surfaces_source_failure_and_blocks_sealing(self):
        self.p.ignore_filter = True
        sync, _, forecast = self.p.start()
        self.assertEqual(sync["state"], "source_quality")
        self.assertFalse(sync["complete"])
        self.assertGreater(sync["quality"]["overlapping_ids"], 0)
        self.assertNotEqual(forecast["state"], "ok")
        self.assertFalse(self.p.window().get("sealed_at"))
        self.p.finish()
        summary = operations.status(self.p.root, clock=self.p.venue.clock)[
            "payout_reconciliation"
        ]
        self.assertEqual(summary["comparable_conditions"], 0)

    def test_explicit_non_neutral_has_documented_basis_and_seals(self):
        self.p.neutral = False
        self.p.start()
        self.p.finish()
        self.assertEqual(
            self.assert_paired()["forecast"]["venue_basis"], "confirmed_non_neutral"
        )

    def test_missing_datetime_cannot_seal_or_turn_a_native_payout_into_a_score(self):
        self.p.missing_time = True
        sync, _, forecast = self.p.start()
        self.assertEqual(sync["state"], "source_quality")
        self.assertGreater(sync["quality"]["scheduled_without_kickoff"], 0)
        self.assertNotEqual(forecast["state"], "ok")
        self.assertFalse(self.p.window().get("sealed_at"))
        self.p.finish()
        native = self.p.period_report()["report"]["payout_reconciliation"]
        self.assertEqual(
            (native["native_conditions"], native["counts"]["unpaired"]), (4, 4)
        )

    def test_payout_disagrees_is_visible_in_grades_both_reports_and_health(self):
        self.p.start()
        self.p.finish(wrong_win=True)
        self.assert_paired()
        w = self.p.window()
        g = w["grade_history"][-1]["grade"]["payout_reconciliation"]
        self.assertEqual(g["win"]["state"], "disagrees")
        self.assertEqual(
            (g["win"]["native_primary"], g["win"]["score_implied_primary"]), (0, 1)
        )
        for native in [
            self.p.period_report(kind)["report"]["payout_reconciliation"]
            for kind in ("day", "week")
        ] + [
            operations.status(self.p.root, clock=self.p.venue.clock)[
                "payout_reconciliation"
            ]
        ]:
            self.assertEqual(
                (native["counts"]["disagrees"], native["counts"]["match"]),
                (1, 3),
            )
            self.assertEqual(
                [i["game_id"] for i in native["disagreement_games"]], [GAME]
            )
            self.assertEqual(
                native["disagreement_games"][0]["conditions"][0]["condition_id"],
                g["win"]["condition_id"],
            )

    def test_late_score_corrections_withdrawals_and_split_payouts_are_versioned(self):
        self.p.start()
        # A second horizon on the same event must not double the OT case count.
        self.p.venue.now = self.p.tip - timedelta(minutes=18)
        self.p.sync()
        self.p.collect()
        self.p.tick()
        self.assertEqual(self.p.window("T-15m").get("state"), "sealed")
        self.p.final_period = 5
        self.p.finish(void={"away_total"}, period="F/OT", give_score=False)
        old = self.p.period_report()
        self.assertEqual(
            old["report"]["payout_reconciliation"]["counts"]["awaiting_score"], 3
        )
        self.assertEqual(
            old["report"]["payout_reconciliation"]["verified_overtime_total_games"], 0
        )
        for final, expect_win in [
            (FINAL, "match"),
            ((107, 111), "disagrees"),
            (None, "awaiting_score"),
            (FINAL, "match"),
        ]:
            self.p.advance(3600)
            self.p.final = final
            self.p.sync()
            self.p.tick()
            grade = self.p.window()["grade_history"][-1]["grade"]
            self.assertEqual(grade["payout_reconciliation"]["win"]["state"], expect_win)
            self.assertEqual(
                grade["payout_reconciliation"]["away_total"]["state"], "void_50_50"
            )
        week = self.p.period_report("week")["report"]["payout_reconciliation"]
        self.assertEqual(
            (
                week["native_conditions"],
                week["verified_overtime_total_games"],
                len(week["void_games"]),
            ),
            (4, 1, 1),
        )
        evidence = week["overtime_total_evidence"][0]["conditions"][0]
        self.assertEqual(evidence["period"]["value"], "F/OT")
        self.assertTrue(evidence["event_sha256"] and evidence["result_receipt_sha256"])
        self.assertEqual(
            self.p.period_report(revision=old["revision"])["sha256"], old["sha256"]
        )
        revision = self.p.period_report()["revision"]
        self.p.tick()
        self.assertEqual(self.p.period_report()["revision"], revision)
        with closing(storage.open_existing(self.p.root)) as db:
            refs = self.p.window()["prediction"]["market_at_prediction"]
            others = dict(
                refs, win=dict(refs["win"], token_id=refs["total"]["token_id"])
            )
            self.assertEqual(
                reconciliation.by_reference(db, GAME, others)["win"]["state"],
                "unpaired",
            )
            self.assertEqual(
                reconciliation.by_reference(db, "bdl:9002", refs)["win"]["state"],
                "unpaired",
            )
