"""The event scoreboard belongs to the instant of its original Gamma receipt."""

from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import test_budget_domestic_archive as domestic
import test_big5_receipt_grades as windows
from test_budget_research_fees import PROFILE, refresh
from test_budget_scheduled_entry import BASIS
from polymarket861 import big5_market, big5_market_schedule as state, five_league_budget, store


STAMP = "2030-01-12T12:57:00Z"
PAST = {"finishedTimestamp": "2030-01-12T12:30:00Z"}
LIVE = {"period": "2H", "elapsed": "67", "score": "2-1"}


def outcomes(root, doc, as_of=None):
    return [
        five_league_budget.replay(doc, data_dir=root, as_of=as_of, entry_basis=tier, details=True)["decisions"][0]["outcome"]
        for tier in ("explicit_pregame", BASIS)
    ]


class SoccerNativeState(unittest.TestCase):
    def test_statuses_clocks_scores_and_placeholders(self):
        for event in (PAST, LIVE, {"gameStatus": "InProgress"}, {"status": "Break"}, {"status": "PenaltyShootout"},
                      {"period": "HT"}, {"period": "FT OT"}, {"period": "VFT"}, {"elapsed": "45+2′"},
                      {"elapsed": "00:05"}, {"score": "1–0"}, {"score": "0 : 2"},
                      {"finishedTimestamp": "bad", "period": "2H"}):
            for strong in ({}, windows.STRONG):
                with self.subTest(event=event, strong=strong):
                    self.assertTrue(state.has_started(strong | event, STAMP))
                    self.assertEqual(state.pregame_flags(strong | event, STAMP), "not_pregame")
        for event in ({"period": "NS", "elapsed": "0", "score": "0-0"},
                      {"period": "scheduled", "elapsed": "00:00", "score": "0 – 0"},
                      {"period": "-", "elapsed": "", "score": "-:-"}, {"score": "00:00"},
                      {"period": None, "elapsed": "0’"}, {}):
            with self.subTest(event=event):
                self.assertFalse(state.has_started(event, STAMP))
                self.assertEqual(state.pregame_flags(event, STAMP), "schedule_only")
                self.assertEqual(state.pregame_flags(event | windows.STRONG, STAMP), "explicit_pregame")
        for event in ({"finishedTimestamp": "2030-01-12T18:00:00Z"}, {"finishedTimestamp": "invalid"},
                      {"finishedTimestamp": "2030-01-12T12:30:00"}, {"finishedTimestamp": "0001-01-01T00:00:00Z"},
                      {"period": "unrecognized"}, {"elapsed": "2-1"}, {"score": "home-scores-2"}):
            with self.subTest(event=event):
                self.assertFalse(state.has_started(event, STAMP))
                self.assertEqual(state.pregame_flags(event | windows.STRONG, STAMP), "unclassified")
        self.assertTrue(state.stopped({"status": "Awarded"}, STAMP))
        self.assertFalse(state.has_started({"status": "Awarded"}, STAMP))

    def test_fixed_windows_retain_withdrawn_play_and_upgrade_preexisting_v2_index(self):
        for side in ("main", "aux"):
            for event in (PAST, LIVE, {"status": "InProgress"}):
                with self.subTest(side=side, event=event), TemporaryDirectory() as tmp:
                    runner = windows.TestDomesticReceiptGrades()
                    runner.base = Path(tmp)
                    runner._leg(-20, windows.STRONG | event if side == "main" else windows.STRONG,
                                windows.STRONG | event if side == "aux" else windows.STRONG, watch=False)
                    runner._leg(-10, windows.STRONG, windows.STRONG)
                    runner._leg(6, {}, {})
                    for rebuilt in (False, True):
                        if rebuilt:
                            with closing(big5_market._db(big5_market.research_root(tmp))) as db, db:
                                self.assertTrue(db.execute("SELECT 1 FROM big5_schedule_recovery WHERE version=2").fetchone())
                                db.execute("DELETE FROM big5_play_evidence")
                                db.execute("UPDATE big5_schedule_receipts SET stopped=0")
                                db.execute("DELETE FROM big5_schedule_recovery WHERE version=3")
                        counts = runner._counts()
                        self.assertEqual(counts["fixed_windows_not_pregame"], 2)
                        self.assertEqual(counts["t1_pairs_not_pregame"], 1)
                        self.assertEqual(counts["t1_pairs_schedule_only"] + counts["t1_pairs_explicit_pregame"], 0)

    def test_paper_uses_current_and_previously_withdrawn_state_of_both_parents(self):
        for side in ("main", "aux"):
            for original in (PAST, LIVE):
                for withdrawn in (False, True):
                    with self.subTest(side=side, original=original, withdrawn=withdrawn), TemporaryDirectory() as tmp:
                        root = Path(tmp)
                        doc, (main, aux) = domestic.domestic_entry(root, "sea", explicit=True)
                        decision = store.instant(doc["intents"][0]["decision_at"])
                        changed = (main | original, aux) if side == "main" else (main, aux | original)
                        offset = -200 if withdrawn else 1
                        refresh(root, *changed, decision + timedelta(seconds=offset))
                        self.assertEqual(outcomes(root, doc), ["skipped_unverified_entry"] * 2)

    def test_invalid_future_clock_can_be_corrected_and_later_dated_finish_is_as_of(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc, (main, aux) = domestic.domestic_entry(root, "lal", explicit=True)
            decision = store.instant(doc["intents"][0]["decision_at"])
            kickoff = store.instant(doc["intents"][0]["kickoff"])
            bad_early = {"finishedTimestamp": store.iso(kickoff + timedelta(hours=3))}
            refresh(root, main | bad_early, aux, decision - timedelta(seconds=200))
            self.assertEqual(outcomes(root, doc), ["filled"] * 2)
            # A final received after play cannot revoke an earlier purchase just by existing.
            refresh(root, main, aux | {"ended": True, "finishedTimestamp": store.iso(kickoff + timedelta(hours=2))},
                    kickoff + timedelta(hours=3))
            self.assertEqual(outcomes(root, doc), ["filled"] * 2)
            refresh(root, main, aux | PAST, kickoff + timedelta(hours=4))
            self.assertEqual(outcomes(root, doc, as_of=kickoff + timedelta(hours=3)), ["filled"] * 2)
            self.assertEqual(outcomes(root, doc), ["skipped_unverified_entry"] * 2)

    def test_auxiliary_native_fee_identifiers_cannot_change_and_revert_before_execution(self):
        initial = {"feeType": "sports_fees_v3", "makerBaseFee": 1000, "takerBaseFee": 1000}
        base = domestic.native

        def captured(*args, **kwargs):
            event = base(*args, **kwargs)
            for market in event["markets"]:
                market.update(initial)
            return event

        for changed in ({"feeType": "sports_fees_v4"}, {"takerBaseFee": 2500}, {"makerBaseFee": 1250}):
            with self.subTest(changed=changed), TemporaryDirectory() as tmp, patch.object(domestic, "native", captured):
                root = Path(tmp)
                doc, (main, aux) = domestic.domestic_entry(root, "bun", fee_schedule=PROFILE)
                at = store.instant(doc["intents"][0]["decision_at"])
                other = deepcopy(main)
                other["markets"][0].update(changed)
                refresh(root, other, aux, at + timedelta(seconds=1))
                refresh(root, main, aux, at + timedelta(seconds=3))
                self.assertEqual(outcomes(root, doc), ["skipped_unverified_entry"] * 2)
