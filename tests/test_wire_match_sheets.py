import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from polymarket861 import league_dashboard, market_provider, store, wire_match_sheets as sheets


class MatchSheetTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.kick = store.instant("2026-11-27T20:00:00Z")
        self.now = self.kick - timedelta(minutes=74)
        clock = patch.object(store, "now_utc", side_effect=lambda: store.iso(self.now))
        clock.start()
        self.addCleanup(clock.stop)
        self.game = {"fixture_id": "epl/forest-chelsea", "home": "Nottingham Forest", "away": "Chelsea",
                     "known_at": store.iso(self.now), "kickoff": store.iso(self.kick), "season": "2026-27",
                     "round_id": 12, "source": "test", "locator": "test:fixture"}
        store.register_fixture(self.root, self.game)
        self.index = [{"competition": "epl", "status_short": "NS", "is_live": False, "is_finished": False,
                       "kickoff_iso": self.game["kickoff"], "home_team": {"name": "Nott'm Forest"},
                       "away_team": {"name": "Chelsea"}, "api_id": 1234}]
        self.report = {"fixture_id": 1234, "competition": "epl", "date_utc": self.game["kickoff"],
                       "league": {"id": 39, "season": 2026}, "status": {"short": "NS", "elapsed_minutes": None},
                       "teams": {s: {"name": self.game[s]} for s in ("home", "away")},
                       "score": {"current": {"home": None, "away": None}},
                       "lineups": {s: {"team": self.game[s], "starters": [
                           {"id": start+i, "name": "Player " + chr(65+i)} for i in range(11)]}
                           for s, start in (("home", 1), ("away", 12))}}
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        self.assertIn(url, (sheets.INDEX, sheets.MATCH + "1234"))
        stamp = store.iso(self.now)
        meta = {"source": "Touchline Wire", "attribution_url": "https://touchlinewire.com",
                "license": "Free for personal, academic, and open-source use with attribution", "timestamp_iso": stamp}
        data = self.index if url == sheets.INDEX else self.report
        return market_provider.Reply(json.dumps({"meta": meta, "data": data}).encode(), stamp, stamp, {}, url)

    def poll(self):
        return sheets.collect(self.root, getter=self.get)

    def test_fixture_specific_receipt_cutoff_correction_and_no_forecast_mutation(self):
        self.assertEqual(self.poll()["observations"][0]["state"], "reported")
        first = sheets.latest(self.root, self.game, self.now)
        self.assertEqual(len(first["teams"]["home"]), 11)
        self.assertFalse(first["formal_eligible"])
        self.assertIsNone(sheets.latest(self.root, self.game, self.now - timedelta(seconds=1)))
        self.assertEqual(self.poll()["checked"], 0)
        self.assertEqual(len(self.urls), 2)
        self.report["lineups"]["home"]["starters"][0] = {"id": 90, "name": "Corrected Starter"}
        self.now += timedelta(minutes=15)
        self.poll()
        self.assertEqual(sheets.latest(self.root, self.game, self.kick-timedelta(hours=1)), first)
        self.assertEqual(sheets.latest(self.root, self.game, self.now)["teams"]["home"][0]["id"], 90)
        with closing(sqlite3.connect(self.root / "runtime.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wire_sheet_observations").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM forecast_seals").fetchone()[0], 0)

    def test_predicted_players_empty_sheet_cup_live_and_changed_kickoff(self):
        self.report["tactical_intelligence"] = {"home_lineup_forecast": self.report["lineups"]["home"]}
        self.report["lineups"] = {"home": {}, "away": {}}
        self.assertEqual(self.poll()["observations"][0]["state"], "unpublished")
        for change in ("cup", "live", "duplicate"):
            with self.subTest(change=change):
                self.now += timedelta(minutes=4)
                self.report["competition"] = "uefa" if change == "cup" else "epl"
                self.report["status"]["short"] = "1H" if change == "live" else "NS"
                if change == "duplicate":
                    self.index *= 2
                self.assertEqual(self.poll()["observations"][0]["state"], "unavailable")
                self.assertNotIn("teams", sheets.latest(self.root, self.game, self.now))
        moved = self.game | {"kickoff": store.iso(self.kick + timedelta(hours=1))}
        self.assertIsNone(sheets.latest(self.root, moved, self.now))
        self.now = self.kick
        self.assertEqual(self.poll()["checked"], 0)

    def test_read_only_ui_distinguishes_index_metadata_from_article(self):
        items = [{"domain": "paper.example", "url": "https://paper.example/sports", "club_label": "Forest"},
                 {"title": "A & B", "domain": "news.example", "url": "https://news.example/story"}]
        result = league_dashboard._headlines({"teams": {"home": {"items": items}}}, self.game)
        for label in ("待核实", "索引未提供标题", "paper.example", "命中球队名 Forest", "A &amp; B"):
            self.assertIn(label, result)

    def test_schedule_reversion_and_index_clock_are_separate_receipts(self):
        def delayed_index(url):
            reply = self.get(url)
            if url == sheets.INDEX:
                later = store.iso(self.now + timedelta(minutes=2))
                return market_provider.Reply(reply.body, later, later, {}, url)
            return reply
        sheets.collect(self.root, getter=delayed_index)
        self.assertIsNone(sheets.latest(self.root, self.game, self.now))
        self.now += timedelta(minutes=3)
        first = sheets.latest(self.root, self.game, self.now)
        self.assertEqual(first["schedule_version"], 1)
        with closing(store.connect(self.root)) as db, db:
            db.execute("UPDATE wire_sheet_observations SET recorded=?",
                       (store.iso(self.now - timedelta(minutes=8)),))
        with self.assertRaisesRegex(ValueError, "identity, clocks or fingerprint"):
            sheets.latest(self.root, self.game, self.now)
        store.register_fixture(self.root, self.game | {"known_at": store.iso(self.now),
                                                      "kickoff": store.iso(self.kick + timedelta(days=1))})
        self.now += timedelta(minutes=1)
        store.register_fixture(self.root, self.game | {"known_at": store.iso(self.now)})
        self.assertIsNone(sheets.latest(self.root, self.game, self.now))
        self.assertEqual(self.poll()["observations"][0]["state"], "reported")

    def test_provider_backoff_applies_to_another_fixture_entering_the_window(self):
        other = self.game | {"fixture_id": "epl/arsenal-brentford", "home": "Arsenal", "away": "Brentford",
                             "kickoff": store.iso(self.kick + timedelta(minutes=10))}
        store.register_fixture(self.root, other)
        def limited(url):
            self.urls.append(url)
            error = HTTPError(url, 429, "rate limited", {}, None)
            error.retry_after = 1200
            raise error
        sheets.collect(self.root, getter=limited)
        self.now += timedelta(minutes=12)
        self.assertEqual(sheets.collect(self.root, getter=limited)["checked"], 0)
        self.assertEqual(self.urls, [sheets.INDEX])

    def test_mixed_match_phases_duplicate_identity_and_unrelated_broken_schedule(self):
        for fields in ({"status": {"short": "NS", "long": "Match Finished"}},
                       {"score": {"current": {}, "halftime": {"home": 0, "away": 0}}},
                       {"score": {"current": {}, "fulltime": {"away": 2}}},
                       {"teams": {"home": {"winner": True, "score": 2}, "away": {}}},
                       {"events": [{"time": {"elapsed": 54}}]}, {"score": {"current": {}}},
                       {"statistics": {"home": {"total_shots": 13}}}):
            with self.subTest(fields=fields):
                self.assertFalse(sheets._unplayed(self.report | fields))
        foreign = dict(self.index[0], home_team={"name": "Arsenal"}, kickoff_iso="TBD", api_id=987)
        self.assertEqual(sheets._match_id(self.game, [foreign, *self.index]), 1234)
        with self.assertRaises(ValueError):
            sheets._match_id(self.game, [*self.index, self.index[0] | {"status_short": "LIVE"}])
        for conflicting in ({"score": "2-1"}, {"home_penalties": 1}, {"penalty_winner": "home"}):
            with self.assertRaises(ValueError):
                sheets._match_id(self.game, [self.index[0] | conflicting])
        prior = deepcopy(self.report)
        self.poll()
        at = store.iso(self.now)
        self.now += timedelta(minutes=4)
        row = sheets.latest(self.root, self.game, self.now)
        row["teams"]["home"][0]["id"] += 1000
        row["received_at"] = store.iso(self.now)
        self.assertEqual(sheets._save(self.root, self.game, row), "source_conflict")
        self.assertEqual(sheets.latest(self.root, self.game, at)["state"], "reported")
        self.assertNotIn("teams", sheets.latest(self.root, self.game, self.now))
        self.assertEqual(sheets._save(self.root, self.game, row), "source_conflict")
        self.assertEqual(self.report, prior)
