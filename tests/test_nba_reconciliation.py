"""Settlement provenance and market-only operation."""

import json
import os
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket861.nba import markets, reconciliation, storage
from test_nba_review_pipeline import FINAL, GAME, Pipeline


class TestNativePayouts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Pipeline(Path(tmp.name) / "nba")
        self.enterContext(
            patch.dict(os.environ, {"BALLDONTLIE_API_KEY": "offline-replay-key"})
        )
        self.enterContext(patch.object(markets.api, "get", self.p.http))

    def test_market_only_receipts_remain_unpaired_then_acquire_independent_score(self):
        self.p.collect()
        self.p.finish(
            void={"away_total"}, period="VFT", give_score=False, run_forecast=False
        )
        with closing(storage.open_existing(self.p.root)) as db:
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='nba_ops_games'"
                ).fetchone()
            )
        native = markets.status(self.p.root, now=self.p.venue.clock)[
            "payout_reconciliation"
        ]
        self.assertEqual(
            (native["counts"]["unpaired"], native["counts"]["void_50_50"]), (3, 1)
        )
        self.assertEqual(
            (
                native["score_evidence"]["unpaired"],
                native["verified_overtime_total_games"],
            ),
            (4, 0),
        )
        self.p.tick()
        self.assertEqual(
            self.p.period_report()["report"]["payout_reconciliation"], native
        )
        self.p.advance(3600)
        self.p.final_period = 5
        self.p.final = FINAL
        self.p.sync()
        self.p.tick()
        newer = self.p.period_report()["report"]["payout_reconciliation"]
        self.assertEqual(
            (newer["counts"]["match"], newer["verified_overtime_total_games"]), (3, 1)
        )
        self.assertEqual(newer["score_evidence"]["final"], 4)

    def test_mislinked_native_token_receipts_are_flagged_without_changing_scores(self):
        self.p.start()
        self.p.finish()
        ref = self.p.window()["prediction"]["market_at_prediction"]
        cid = ref["win"]["condition_id"]
        with closing(storage.connect(self.p.root)) as db, db:
            original = db.execute(
                "SELECT official_receipt FROM nba_market_settled WHERE condition_id=?",
                (cid,),
            ).fetchone()[0]
            other = db.execute(
                "SELECT official_receipt FROM nba_market_settled WHERE condition_id=?",
                (ref["total"]["condition_id"],),
            ).fetchone()[0]
            db.execute(
                "UPDATE nba_market_settled SET official_receipt=? WHERE condition_id=?",
                (other, cid),
            )
        self.p.advance(60)
        self.p.tick()
        payout = self.p.period_report()["report"]["payout_reconciliation"]
        self.assertEqual(
            (
                payout["counts"]["invalid_native_evidence"],
                payout["comparable_conditions"],
            ),
            (1, 3),
        )
        recent = self.p.window()["grade_history"][-1]["grade"]
        self.assertEqual(recent["state"], "scored")
        self.assertEqual(
            recent["payout_reconciliation"]["win"]["state"], "invalid_native_evidence"
        )
        with closing(storage.connect(self.p.root)) as db, db:
            db.execute(
                "UPDATE nba_market_settled SET official_receipt=? WHERE condition_id=?",
                (original, cid),
            )
        self.p.advance(60)
        self.p.tick()
        with closing(storage.open_existing(self.p.root)) as db:
            states = [
                r[0]
                for r in db.execute(
                    "SELECT state FROM nba_payout_revisions WHERE condition_id=? ORDER BY revision",
                    (cid,),
                )
            ]
            self.assertEqual(states, ["match", "invalid_native_evidence", "match"])
            fresh = reconciliation.by_reference(db, GAME, ref)
            self.assertEqual(fresh["win"]["revision"], 3)
            latest = json.loads(
                db.execute(
                    "SELECT payload FROM nba_payout_revisions WHERE condition_id=? AND revision=3",
                    (cid,),
                ).fetchone()[0]
            )
            self.assertEqual(
                set(latest["token_receipts"]), {latest["token_id"], latest["opposite"]}
            )

    def test_conflicting_periods_cannot_support_an_overtime_precedent(self):
        self.p.start()
        self.p.final_period = 4
        self.p.finish(period="F/OT")
        r = self.p.period_report()["report"]["payout_reconciliation"]
        self.assertEqual(
            (r["counts"]["match"], r["verified_overtime_total_games"]), (4, 0)
        )
        self.assertEqual(
            {i["reason"] for i in r["evidence_issues"]}, {"conflicting_periods"}
        )
        self.p.advance(3600)
        self.p.final_period = 5
        self.p.sync()
        self.p.tick()
        r = self.p.period_report()["report"]["payout_reconciliation"]
        self.assertEqual(
            (r["verified_overtime_total_games"], r["evidence_issues"]), (1, [])
        )
        self.assertEqual(
            r["overtime_total_evidence"][0]["conditions"][0]["overtime"]["basis"],
            "both",
        )

    def test_historical_evidence_regulation_and_final_payouts_recompute(self):
        evidence = (
            Path(__file__).resolve().parents[1]
            / "reports/nba-phase1/v2-overtime-reconciliation.json"
        )
        study = json.loads(evidence.read_text())
        games = study["overtime_games"]
        self.assertEqual(
            len({g["venue_event_id"] for g in games}),
            study["verified_unique_overtime_games"],
        )
        self.assertEqual(len(games), 5)
        decisive = 0
        for g in [*games, study["original_report_fixture"]]:
            with self.subTest(event=g["venue_event_id"]):
                quarters = g["points_by_period"]
                reg = [sum(quarters[side][:4]) for side in ("home", "away")]
                final = [sum(quarters[side]) for side in ("home", "away")]
                self.assertEqual(
                    (reg, final), (g["regulation_home_away"], g["final_home_away"])
                )
                if g in games:
                    self.assertGreater(g["period"]["scoreboard"], 4)
                    self.assertEqual(reg[0], reg[1])
                self.assertEqual([v["side"] for v in g["tokens"]], ["over", "under"])
                self.assertNotEqual(
                    g["tokens"][0]["token_id"], g["tokens"][1]["token_id"]
                )
                self.assertEqual(
                    [v["payout"] for v in g["tokens"]],
                    [
                        int(sum(final) > g["selected_line"]),
                        int(sum(final) < g["selected_line"]),
                    ],
                )
                changed = (sum(reg) > g["selected_line"]) != (
                    sum(final) > g["selected_line"]
                )
                self.assertEqual(changed, g["decisive_for_selected_line"])
                decisive += changed
                for receipt in (
                    g["scoreboard_receipt"],
                    g["gamma_receipt"],
                    *(v["receipt"] for v in g["tokens"]),
                ):
                    self.assertRegex(receipt["sha256"], r"^[a-f0-9]{64}$")
                    self.assertLessEqual(
                        receipt["requested_at"], receipt["received_at"]
                    )
        self.assertEqual((decisive, study["decisive_unique_overtime_games"]), (3, 3))

    def test_changed_source_kickoff_cannot_reuse_a_prior_seals_score_binding(self):
        self.p.start()
        self.p.finish()
        prior = self.p.window()
        self.assertEqual(
            prior["grade_history"][-1]["grade"]["payout_reconciliation"]["win"][
                "state"
            ],
            "match",
        )
        self.p.advance(3600)
        self.p.source_tip += timedelta(minutes=10)
        self.p.sync()
        self.p.tick()
        old = self.p.window()
        self.assertEqual(old["sha256"], prior["sha256"])
        self.assertEqual(
            old["grade_history"][-1]["grade"]["payout_reconciliation"]["win"]["state"],
            "unpaired",
        )
        general = self.p.period_report()["report"]["payout_reconciliation"]
        self.assertEqual(general["counts"]["match"], 4)

    def test_overtime_requires_a_terminal_event_with_explicit_period(self):
        for label, expected in [
            (4, False),
            ("Q5", True),
            ("F/2OT", True),
            ("Final (OT)", True),
            ("Final", None),
            ("Q3", None),
            ("NS", None),
        ]:
            with self.subTest(label=label):
                self.assertIs(
                    reconciliation._period({"ended": True, "period": label})[
                        "overtime"
                    ],
                    expected,
                )
        for event in [
            {"ended": False, "period": 5},
            {"ended": True, "live": True, "period": "OT"},
        ]:
            self.assertIsNone(reconciliation._period(event)["overtime"])
