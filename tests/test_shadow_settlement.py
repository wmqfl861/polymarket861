import concurrent.futures
import unittest
from contextlib import closing
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR
from unittest.mock import patch
from urllib.parse import urlencode

import test_opportunity as f
from polymarket861 import (
    evidence,
    ledger,
    ledger_terms,
    market_archive,
    market_provider,
    pipeline,
    settlement,
    shadow,
    store,
)


class ShadowAndSettlement(unittest.TestCase):
    def setUp(self):
        self.case = f.Opportunities()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root

    def native_state(self, when, **settings):
        raw = f.event()
        raw["markets"][0].update(
            feesEnabled=True,
            feeSchedule={
                "rate": 0.03,
                "exponent": 1,
                "takerOnly": True,
                "rebateRate": 0.25,
            },
        )
        raw.pop("ended")
        raw.pop("live")
        raw.update(settings)

        def get(url, params=None):
            return f.reply(raw, when, url)

        with f.at(when), patch.object(market_provider, "get", side_effect=get):
            obj, receipt, sha = market_archive._fetch(
                self.root,
                "event",
                f.SLUG,
                market_provider.GAMMA + "/events/slug/" + f.SLUG,
            )
            with closing(market_archive.database(self.root)) as db, db:
                market_archive._ingest(db, obj, receipt, sha)

    def setup_model(self, *, technical=True):
        if not technical:
            return self.case.seal()
        make = pipeline.make_forecast

        def research(*args, **kwargs):
            kwargs["mode"] = "technical"
            return make(*args, **kwargs)

        with (
            patch.object(evidence, "import_evidence", return_value={}),
            patch.object(pipeline, "make_forecast", side_effect=research),
        ):
            seal = self.case.seal()
        with closing(store.connect(self.root)) as db, db:
            db.execute("DELETE FROM trial_activations")
        return seal

    def open(self, *, technical=True, fee=True, live=None):
        seal = self.setup_model(technical=technical)
        if fee:
            self.native_state(
                f.CUT - timedelta(minutes=4),
                **({"live": live} if live is not None else {}),
            )
        self.price = (
            Decimal(str(seal["probabilities"]["total_over_2_5"])) - Decimal(".05")
        ).quantize(Decimal(".01"), rounding=ROUND_FLOOR)
        self.case.book(f.CUT - timedelta(seconds=6), self.price)
        with f.at(f.CUT - timedelta(seconds=4)):
            if not technical:
                r = f.opportunity.process(self.root, f.SLUG)
                self.assertTrue(any(x["state"] == "frozen" for x in r["decisions"]), r)
            self.frozen = shadow.run(self.root, f.SLUG)
            shadow.run(self.root, f.SLUG)
        self.case.book(f.CUT + timedelta(seconds=6), self.price)
        with f.at(f.CUT + timedelta(seconds=7)):
            if not technical:
                f.opportunity.process(self.root, f.SLUG)
            shadow.run(self.root, f.SLUG)
        return shadow.report(self.root)

    def resolution(self, amount, when=None, **alter):
        when = when or f.KICK + timedelta(hours=3)
        item = {
            "token_id": "711",
            "condition_id": f.CONDITION,
            "outcome_index": 0,
            "outcome": "Over",
            "opposite_token_id": "722",
            "resolved": amount is not None,
            "final_price": amount,
            "market_slug": f.SLUG + "-total-2pt5",
            "event_slug": f.SLUG,
            "closed": amount is not None,
        }

        def get(url, params=None):
            return f.reply(
                {"data": [item | alter]}, when, url + "?" + urlencode(params)
            )

        with f.at(when), patch.object(market_provider, "get", side_effect=get):
            ledger.capture_resolution(self.root, "711")

    def score(self):
        return {
            "fixture_id": f.FID,
            "kickoff": store.iso(f.KICK),
            "home_goals": 2,
            "away_goals": 1,
            "status": "final",
            "period": "regulation",
            "confirmed_at": store.iso(f.KICK + timedelta(hours=2, minutes=50)),
            "source": "integration test for verified_regulation contract",
            "proof": [{"sha256": "e" * 64}],
        }

    def test_first_run_report_has_no_fictitious_balance(self):
        empty = shadow.report(self.root)
        self.assertEqual(empty["account_state"], "unstarted")
        self.assertEqual(empty["orders"], [])
        self.assertIsNone(empty["cash"])
        self.assertIsNone(empty["settled_roi"])
        with shadow.db_for(self.root) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM research_shadow_accounts").fetchone()[0], 0)

    def test_independent_fee_missing_flags_lineups_scheduled_mark_and_formal_isolation(
        self,
    ):
        r = self.open()
        self.assertEqual(r["counts"], {"held": 1}, (r, self.frozen))
        row = r["orders"][0]
        self.assertEqual(row["lineups"], {"home": False, "away": False})
        self.assertEqual(row["pregame_basis"], "scheduled_with_incomplete_flags")
        self.assertEqual(row["fee_basis"], "research_estimate")
        self.assertGreater(Decimal(row["fee_cash_equivalent"]), 0)
        self.assertLess(Decimal(row["net_shares"]), Decimal(row["gross_shares"]))
        self.assertFalse(r["eligible_for_A"])
        self.assertFalse(r["eligible_for_formal"])
        with ledger.connection(self.root) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM paper_intents").fetchone()[0], 0
            )
        with self.assertRaises(ValueError):
            ledger_terms.contract_at(
                self.root, f.CONDITION, "711", f.CUT, match_id=f.FID
            )
        self.case.book(f.KICK - timedelta(seconds=4), self.price + Decimal(".07"))
        with f.at(f.KICK + timedelta(hours=3)):
            shadow.run(self.root, f.SLUG)
            r = shadow.report(self.root)
            self.assertEqual(r["scheduled_close_coverage"], [1, 1])
            self.assertGreater(Decimal(r["mean_scheduled_close_difference"]), 0)
            self.assertNotIn("clv", r["orders"][0])
            self.assertEqual(r["orders"][0]["close_status"], "scheduled_kickoff")
            self.assertTrue(
                all(
                    not op.get("intent")
                    for op in market_archive.replay_inputs(self.root, [f.SLUG])
                )
            )

    def test_live_flags_or_missed_deadline_cannot_create_a_shadow_fill(self):
        self.assertEqual(self.open(live=True)["counts"], {})
        self.native_state(f.CUT + timedelta(seconds=3), live=False)
        with f.at(f.KICK):
            self.assertEqual(shadow.run(self.root, f.SLUG)["actions"], [])

    def test_only_score_plus_final_token_settles_both_ledgers_once_and_conflicts_never_credit_again(
        self,
    ):
        open_report = self.open(technical=False, fee=False)
        self.assertEqual(open_report["counts"], {"held": 1})
        self.resolution(None)
        claimed = f.KICK + timedelta(hours=4)
        with (
            f.at(claimed),
            patch.object(settlement, "_regulation", return_value=self.score()),
        ):
            self.assertEqual(
                settlement.sync(self.root, cached_only=True)["states"], {"pending": 2}
            )
        self.resolution(1, claimed)
        with f.at(claimed), patch.object(settlement, "_regulation", return_value=None):
            self.assertEqual(
                settlement.sync(self.root, cached_only=True)["states"],
                {"manual_review": 2},
            )
        with (
            f.at(claimed),
            patch.object(settlement, "_regulation", return_value=self.score()),
        ):
            a = settlement.sync(self.root, cached_only=True)
            self.assertEqual(a["states"], {"settled": 2}, a)
            cash = (
                ledger.ledger_report(self.root, "main")["cash"],
                shadow.report(self.root)["cash"],
            )
            self.assertTrue(all(Decimal(x) > 10000 for x in cash))
            with concurrent.futures.ThreadPoolExecutor(2) as ex:
                results = list(
                    ex.map(
                        lambda _: settlement.sync(self.root, cached_only=True), range(2)
                    )
                )
            self.assertTrue(all(r["states"] == {"settled": 2} for r in results))
            self.assertEqual(settlement.queue(self.root)["rows"], [])
            self.assertEqual(
                (
                    ledger.ledger_report(self.root, "main")["cash"],
                    shadow.report(self.root)["cash"],
                ),
                cash,
            )
        self.resolution(0.5, claimed + timedelta(minutes=1))
        with (
            f.at(claimed + timedelta(minutes=2)),
            patch.object(settlement, "_regulation", return_value=self.score()),
        ):
            self.assertEqual(
                settlement.sync(self.root, cached_only=True)["states"],
                {"manual_review": 2},
            )
            self.assertEqual(
                (
                    ledger.ledger_report(self.root, "main")["cash"],
                    shadow.report(self.root)["cash"],
                ),
                cash,
            )

    def test_spoofed_outcome_or_special_payout_is_queued_without_spending_again(self):
        before = self.open()["cash"]
        self.resolution(1, outcome="Under")
        with (
            f.at(f.KICK + timedelta(hours=4)),
            patch.object(settlement, "_regulation", return_value=self.score()),
        ):
            x = settlement.sync(self.root, cached_only=True)
            self.assertEqual(x["states"], {"manual_review": 1}, x)
            self.assertIn("identity", x["positions"][0]["reason"])
            self.assertEqual(shadow.report(self.root)["cash"], before)
