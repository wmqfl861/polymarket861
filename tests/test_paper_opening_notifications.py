import copy
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from polymarket861.nba import data


if sys.platform != "linux":
    raise unittest.SkipTest("the deployed Weixin timer uses Linux/systemd")

SOURCE = (Path(__file__).resolve().parents[1] / "deploy/paper-automation"
          / "five-automation/weixin-recommendations/notify.py")
spec = importlib.util.spec_from_file_location("paper_opening_notifications", SOURCE)
notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notify)


class PaperOpeningNotifications(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.paper = Path(directory.name)
        self.db_file = self.paper / "notifications.sqlite3"
        self.db = notify.database(self.db_file)
        self.addCleanup(lambda: self.db.close())
        self.start = datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)
        self.now = self.start + timedelta(minutes=1)
        self.report = {"state": "ok", "schema": "five-league-paper-risk-v1",
                       "evidence_mode": "local_receipts", "as_of": self.now.isoformat(),
                       "positions": [], "decisions": []}
        self.sender = Mock(return_value=("accepted", "test-receipt", None))

    def add_position(self, key, at):
        decision = {"id": key, "at": at.isoformat(), "outcome": "filled",
                    "cost_including_cash_fees": "12.50", "fill": {"net_shares": "23.5"}}
        position = {**copy.deepcopy(decision), "display": "主队 vs 客队 · 主胜", "league": "epl",
                    "weighted_fill_price": "0.50", "redemption_cash": None,
                    "kickoff": (self.now + timedelta(hours=2)).isoformat()}
        self.report["positions"].append(position)
        self.report["decisions"].append(decision)
        return position, decision

    def check(self, at):
        (self.paper / "account-status.json").write_text(json.dumps(self.report))
        return notify.run(self.db, at, sender=self.sender, paper=self.paper)

    def test_existing_positions_and_unbooked_candidates_are_silent(self):
        for i in range(6):
            self.add_position(f"old-{i}", self.start - timedelta(hours=1))
        (self.paper / "feed.json").write_text('{"recommendations":[{"reference_stake":"20"}]}')
        self.assertEqual(self.check(self.start), 0)
        self.report["positions"][0]["weighted_fill_price"] = "0.49"
        self.assertEqual(self.check(self.now), 0)
        self.sender.assert_not_called()
        self.assertEqual(self.db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)

    def test_matched_new_fill_is_sent_once_across_restart(self):
        self.assertEqual(self.check(self.start), 0)
        position, decision = self.add_position("new-entry", self.now)
        decision["outcome"] = "skipped_unverified_entry"
        self.assertEqual(self.check(self.now), 0)
        decision.update(outcome="filled", cost_including_cash_fees="20.00")
        self.assertEqual(self.check(self.now), 0)
        self.sender.assert_not_called()
        decision["cost_including_cash_fees"] = position["cost_including_cash_fees"]
        self.assertEqual(self.check(self.now), 0)
        self.sender.assert_called_once()
        body = self.sender.call_args.args[0]
        for field in ("模拟账户新建仓", position["display"], "12.50", "50¢"):
            self.assertIn(field, body)
        self.db.close()
        self.db = notify.database(self.db_file)
        position["weighted_fill_price"] = "0.51"
        self.assertEqual(self.check(self.now + timedelta(minutes=5)), 0)
        self.sender.assert_called_once()

    def test_nba_research_receipt_and_restart_leave_football_notifications_untouched(self):
        (self.paper / "feed.json").write_text('{"recommendations":[]}')
        self.report["cash"] = "10000.00"
        self.assertEqual(self.check(self.start), 0)

        def football_hashes():
            return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.paper.iterdir()}

        before = football_hashes()
        game = {"id": 7, "season": 2022, "date": "2023-01-02", "status": "Final", "period": 4,
                "home_team_score": 110, "visitor_team_score": 105,
                "home_team": {"id": 1, "abbreviation": "BOS", "full_name": "Boston Celtics"},
                "visitor_team": {"id": 2, "abbreviation": "MIL", "full_name": "Milwaukee Bucks"}}
        response = httpx.Response(200, json={"data": [game], "meta": {"next_cursor": None}})
        with tempfile.TemporaryDirectory() as other, patch.dict(os.environ, {"BALLDONTLIE_API_KEY": "fixture-key"}):
            nba = Path(other) / "nba"
            for index in range(2):
                result = data.sync_bdl(nba, game["date"], game["date"],
                                       fetch=Mock(return_value=response),
                                       clock=lambda: self.now + timedelta(minutes=index))
                self.assertEqual(result["new_versions"], 1 if index == 0 else 0)
                self.assertEqual(result["state"], "ok")
                self.assertEqual(self.check(self.now), 0)
            self.assertEqual(len(data.games_as_of(nba)), 1)
        self.sender.assert_not_called()
        self.assertEqual(football_hashes(), before)
        self.add_position("genuine-football-fill", self.now)
        for _ in range(2):
            self.assertEqual(self.check(self.now), 0)
        self.sender.assert_called_once()

    def test_failed_opening_retries_after_delay(self):
        self.check(self.start)
        self.add_position("retry", self.now)
        self.sender.side_effect = [("failed", None, "session"), ("accepted", "test-receipt", None)]
        self.assertEqual(self.check(self.now), 1)
        self.assertEqual(self.check(self.now + timedelta(minutes=1)), 1)
        self.assertEqual(self.check(self.now + timedelta(minutes=5)), 0)
        self.assertEqual(self.check(self.now + timedelta(minutes=10)), 0)
        self.assertEqual(self.sender.call_count, 2)

    def test_ambiguous_delivery_checks_hermes_before_sending_again(self):
        self.check(self.start)
        self.add_position("uncertain", self.now)
        self.sender.return_value = ("uncertain", None, "timeout")
        with patch.object(notify, "mirrored", side_effect=[False, None, True]):
            self.assertEqual(self.check(self.now), 1)
            self.assertEqual(self.check(self.now + timedelta(minutes=31)), 1)
            self.assertEqual(self.check(self.now + timedelta(minutes=32)), 0)
        self.assertEqual(self.check(self.now + timedelta(minutes=33)), 0)
        self.sender.assert_called_once()


    def test_expired_failed_opening_records_non_delivery_once(self):
        self.check(self.start)
        position, _ = self.add_position("failed-expired", self.now)
        self.sender.return_value = ("failed", None, "session")
        self.assertEqual(self.check(self.now), 1)
        kickoff = notify.instant(position["kickoff"])
        self.assertEqual(self.check(kickoff), 0)
        self.assertEqual(self.check(kickoff + timedelta(hours=1)), 0)
        self.sender.assert_called_once()
        self.assertEqual([r[0] for r in self.db.execute("SELECT status FROM attempts ORDER BY id")],
                         ["failed", "expired_undelivered"])

    def test_ambiguous_expiry_can_reconcile_late_receipt_without_resend(self):
        self.check(self.start)
        position, _ = self.add_position("unknown-expired", self.now)
        self.sender.return_value = ("uncertain", None, "timeout")
        with patch.object(notify, "mirrored", side_effect=[None, False, True]):
            self.assertEqual(self.check(self.now), 1)
            kickoff = notify.instant(position["kickoff"])
            self.assertEqual(self.check(kickoff), 0)
            self.assertEqual(self.db.execute("SELECT status FROM attempts").fetchone()[0], "expired_unconfirmed")
            self.assertEqual(self.check(kickoff + timedelta(minutes=5)), 0)
        self.sender.assert_called_once()
        self.assertEqual(self.db.execute("SELECT status FROM attempts").fetchone()[0], "accepted")
