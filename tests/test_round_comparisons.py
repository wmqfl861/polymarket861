"""Normal round publication from native multi-token test receipts and immutable football seals."""

import unittest
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

import test_opportunity as fx
from kickoff_fixture import capture_example

from polymarket861 import (
    comparisons,
    dashboard,
    football,
    kickoff_proof,
    market_archive,
    market_provider,
    pipeline,
    round_history,
    store,
)


def catalog():
    data = fx.event()
    template = data["markets"][0]

    def market(index, suffix, kind, labels, title, rules, line=None):
        return template | {
            "slug": fx.SLUG + "-" + suffix,
            "conditionId": "0x" + format(index, "064x"),
            "outcomes": labels,
            "clobTokenIds": [str(index * 10 + 1), str(index * 10 + 2)],
            "sportsMarketType": kind,
            "groupItemTitle": title,
            "description": rules,
            "line": line,
            "feesEnabled": True,
        }

    markets = []
    header = "In the upcoming game, scheduled for March 1, 2026 "
    regular = "within the first 90 minutes of regular play plus stoppage time"
    for n, (suffix, title, predicate, pay) in enumerate(
        [
            ("nor", fx.HOME, fx.HOME + " wins", '"No"'),
            ("draw", "Draw", "the game ends in a draw", 'to "Yes"'),
            ("sou", fx.AWAY, fx.AWAY + " wins", '"No"'),
        ],
        1,
    ):
        rules = (
            header
            + f'If {predicate}, this market will resolve to "Yes". Otherwise, this market will resolve to "No". '
            "If the game is postponed, this market will remain open until the game has been completed. "
            f"If the game is canceled entirely, with no make-up game, this market will resolve {pay}. "
            f"This market refers only to the outcome {regular}."
        )
        markets.append(market(n, suffix, "moneyline", ["Yes", "No"], title, rules))
    markets.append(
        market(
            4,
            "total-2pt5",
            "totals",
            ["Over", "Under"],
            "O/U 2.5",
            template["description"],
            2.5,
        )
    )
    for n, side, club in [(5, "home", fx.HOME), (6, "away", fx.AWAY)]:
        body = (
            header
            + f'This market will resolve to "Over" if {club} score 2 or more goals {regular}. '
            f'If {club} score less than 2 goals {regular}, this market will resolve to "Under". '
            f"This market is based solely on {club}'s score, not the combined score of both teams. "
            "If the game is postponed, this market will remain open until the game has been completed. "
            "If the game is canceled entirely, with no make-up game, this market will resolve 50-50. "
            f"This market refers only to the outcome {regular}. Extra time and penalty shoot-outs are excluded. "
            "The primary resolution source for this market is the official statistics of the event as recognized by the governing body or event organizers. "
            "However, if the governing body or event organizers have not published final match statistics within 2 hours after the event's conclusion, a consensus of credible reporting may be used instead. "
            "All markets will settle based on the official final result as recognized by the governing body or event organizers. "
            "Revisions to officially declared final scores made after market resolution will not be accounted for in determining the outcome."
        )
        markets.append(
            market(
                n,
                "team-total-" + side + "-1pt5",
                "soccer_team_totals",
                ["Over", "Under"],
                club + " O/U 1.5",
                body,
                1.5,
            )
        )
    return data | {"markets": markets}


class RoundComparisons(unittest.TestCase):
    def setUp(self):
        self.case = fx.Opportunities()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root
        self.forecast = self.case.seal()
        self.event = catalog()
        self.receive(self.event, fx.CUT - timedelta(minutes=10))
        when = fx.KICK + timedelta(hours=3)
        with fx.at(when):
            store.record_result(
                self.root,
                {
                    "fixture_id": fx.FID,
                    "home_goals": 2,
                    "away_goals": 0,
                    "source": "test scorer",
                    "locator": "test://score",
                    "finalized_at": store.iso(when),
                    "published_at": store.iso(when),
                    "received_at": store.iso(when),
                },
            )

    def receive(self, event, at):
        def get(url, params=None):
            data = (
                fx.event(url.rsplit("/", 1)[-1])
                if url.endswith("-more-markets")
                else event
            )
            return fx.reply(data, at, url)

        with patch.object(market_provider, "get", side_effect=get):
            market_archive.discover(self.root, fx.SLUG)

    def batch(self, when, *, omit=(), change=None, shift=0):
        # 1X2 affirmative prices sum to 1.02; the six binary books use actual complementary tokens.
        cent = {1: 50, 2: 31, 3: 21, 4: 60, 5: 40, 6: 30}
        values = []
        for m in self.event["markets"]:
            for index, t in enumerate(m["clobTokenIds"]):
                mid = (
                    cent[int(t) // 10] + shift
                    if index == 0
                    else 100 - cent[int(t) // 10] - shift
                )
                if t not in omit:
                    values.append(
                        {
                            "asset_id": t,
                            "market": m["conditionId"],
                            "timestamp": str(int(when.timestamp() * 1000)),
                            "bids": [{"price": f"{(mid - 1) / 100:.2f}", "size": "10"}],
                            "asks": [{"price": f"{(mid + 1) / 100:.2f}", "size": "10"}],
                            "tick_size": "0.01",
                            "min_order_size": "1",
                        }
                    )
        if change:
            change(values)

        def get(url, params=None):
            self.assertTrue(url.endswith("/books"))
            return fx.reply(values, when, url)

        with fx.at(when), patch.object(market_provider, "get", side_effect=get):
            market_archive.capture(
                self.root, fx.SLUG, discovery={"events": [fx.SLUG], "gaps": []}
            )

    def report(self):
        return pipeline.publish_round(self.root, fx.CHAMP, 26)

    def row(self, report):
        return next(
            r["comparison"] for r in report["fixtures"] if r["fixture_id"] == fx.FID
        )

    def test_full_join_normalization_pairing_two_clocks_and_immutable_history(self):
        before = self.report()
        no_quotes = self.row(before)
        self.assertEqual(
            no_quotes["baseline"]["input_proof_sha256"],
            self.forecast["comparison_proof"]["sha256"],
        )
        self.assertEqual(no_quotes["baseline"]["score"], no_quotes["model"]["score"])
        self.assertIsNone(no_quotes["market"]["score"])
        self.batch(fx.CUT - timedelta(seconds=7))
        self.batch(fx.KICK - timedelta(seconds=4), shift=2)
        graded = self.report()
        c = self.row(graded)
        m = c["market"]
        self.assertFalse(m["missing"], m["missing"])
        self.assertAlmostEqual(m["moneyline_sum"], 1.02)
        self.assertAlmostEqual(m["probabilities"]["home_win"], 0.5 / 1.02)
        self.assertEqual(m["probabilities"]["total_over_2_5"], 0.6)
        self.assertEqual(m["probabilities"]["home_over_1_5"], 0.4)
        self.assertAlmostEqual(
            m["score"], football.score_probabilities(m["probabilities"], 2, 0)["score"]
        )
        self.assertIsNone(c["frozen_market"]["score"])
        self.assertNotEqual(c["input_cutoff"], c["decision_at"])
        pairs = graded["comparisons"]["pairs"]
        self.assertEqual(
            (pairs["market"]["paired"], pairs["market"]["expected"]), (1, 10)
        )
        self.assertAlmostEqual(
            pairs["market"]["delta"], c["model"]["score"] - m["score"]
        )
        self.assertEqual(c["close"]["basis"], "shadow_planned")
        self.assertEqual(len(c["close"]["items"]["home"]["tokens"]), 2)
        self.assertEqual(
            graded["comparisons"]["closing_distance"]["verified_actual"]["home"][
                "paired"
            ],
            0,
        )
        self.assertIsNone(
            graded["comparisons"]["closing_distance"]["verified_actual"]["home"]["mse"]
        )
        old = round_history.round_history(
            self.root, fx.CHAMP, 26, edition=before["report_version"]
        )
        self.assertIsNone(self.row(old["report"])["market"]["score"])
        self.assertEqual(old["report_sha256"], before["report_sha256"])
        self.assertEqual(self.report()["report_sha256"], graded["report_sha256"])

    def test_incomplete_latest_batch_cannot_borrow_tokens_or_grades(self):
        self.batch(fx.CUT - timedelta(seconds=31))
        self.batch(fx.CUT + timedelta(seconds=1))
        self.batch(fx.KICK + timedelta(seconds=3))
        self.assertIsNone(self.row(self.report())["market"]["score"])
        self.batch(fx.CUT - timedelta(seconds=8))
        self.batch(fx.CUT - timedelta(seconds=3), omit={"21", "52"})
        c = self.row(self.report())
        self.assertEqual(set(c["market"]["item_scores"]), {"total", "away"})
        self.assertIsNone(c["market"]["score"])
        self.assertFalse(c["close"]["items"])
        self.assertEqual(
            self.report()["comparisons"]["pairs"]["market"]["by_family"]["home"][
                "missing"
            ],
            10,
        )
        self.batch(
            fx.CUT - timedelta(seconds=1),
            change=lambda rows: rows[0].update(market="0x" + "f" * 64),
        )
        self.assertIn("result", self.row(self.report())["market"]["missing"])

    def test_actual_closing_proof_splits_shadow_and_exact_token_fair_value(self):
        self.batch(fx.CUT - timedelta(seconds=2))
        self.batch(fx.KICK - timedelta(seconds=5))
        early = self.row(self.report())
        self.assertEqual(early["close"]["basis"], "shadow_planned")
        actual = fx.KICK + timedelta(seconds=60)
        self.batch(actual - timedelta(seconds=5), shift=2)
        with (
            patch.dict(kickoff_proof.OPEN_RELEASES, {}),
            fx.at(actual + timedelta(hours=3)),
        ):
            fixture = store.fixture_at(self.root, fx.FID)
            receipt = capture_example(self.root, fixture, "77332211", actual)
            report = self.report()
            close = self.row(report)["close"]
            self.assertEqual(
                (close["basis"], close["proof_id"]),
                ("verified_actual", receipt["proof_id"]),
            )
            q = close["items"]["total"]["tokens"][0]
            self.assertAlmostEqual(q["mid"], 0.62)
            self.assertAlmostEqual(q["squared_distance"], (q["fair_low"] - 0.62) ** 2)
            self.assertEqual(
                report["comparisons"]["closing_distance"]["shadow_planned"]["total"][
                    "paired"
                ],
                0,
            )

    def test_last_eligible_close_keeps_each_family_together_and_editions_on_dashboard(
        self,
    ):
        old = self.report()
        self.batch(fx.KICK - timedelta(seconds=20), shift=1)
        self.batch(fx.KICK - timedelta(seconds=4), omit={"21", "42"}, shift=2)
        fresh = self.report()
        close = self.row(fresh)["close"]
        self.assertIsNone(close["batch"])
        receipts = close["family_receipts"]
        self.assertEqual(
            receipts["result"]["received_at"],
            store.iso(fx.KICK - timedelta(seconds=20)),
        )
        self.assertEqual(
            receipts["home"]["received_at"], store.iso(fx.KICK - timedelta(seconds=4))
        )
        self.assertEqual(close["items"]["total"]["tokens"][0]["mid"], 0.61)
        rows = dashboard._report_rows(self.root)
        archived = next(row for row in rows if row["kind"] == "round-edition")
        self.assertEqual(archived["payload"]["report_sha256"], old["report_sha256"])
        self.assertFalse(self.row(archived["payload"])["close"]["items"])
        self.assertTrue(
            self.row(next(row["payload"] for row in rows if row["kind"] == "round"))[
                "close"
            ]["items"]
        )

    def test_wide_book_rules_mutation_and_same_kickoff_other_fixture(self):
        self.batch(
            fx.CUT - timedelta(seconds=8),
            change=lambda rows: rows[6].update(asks=[{"price": ".99", "size": "10"}]),
        )
        self.assertIn("total", self.row(self.report())["market"]["missing"])

        def inspect_swap():
            wrong = store.fixture_at(self.root, "unrelated")
            with closing(store.connect(self.root)) as db:
                return comparisons.frame(db, self.root, wrong, fx.CUT, fx.CUT, fx.KICK)

        self.assertFalse(inspect_swap()["items"])
        changed = deepcopy(self.event)
        changed["markets"][2]["description"] += " Altered settlement."
        self.receive(changed, fx.CUT - timedelta(seconds=2))
        self.assertIn("result", self.row(self.report())["market"]["missing"])
