import os
import tempfile
import unittest
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from polymarket861 import football, football_model, league_player_status as source, store


class PrivatePlayerSourceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.kick = store.instant("2026-11-27T19:30:00Z")
        self.now = self.kick - timedelta(hours=1)
        self.home, self.away = "SV Werder Bremen", "Borussia Mönchengladbach"
        self.fixture = {"fixture_id": football.fixture_id("2026-27", self.home, self.away, league="bun"),
                        "season": "2026-27", "league": "bun", "home": self.home, "away": self.away,
                        "kickoff": store.iso(self.kick)}
        self.match = {"fixture": {"id": 4242, "date": store.iso(self.kick), "timestamp": int(self.kick.timestamp()),
                                  "status": {"short": "NS"}}, "league": {"id": 78, "season": 2026},
                      "teams": {"home": {"id": 8, "name": "Werder Bremen"},
                                "away": {"id": 9, "name": "Borussia Monchengladbach"}}}
        self.injury = {"fixture": self.match["fixture"], "league": self.match["league"],
                       "team": self.match["teams"]["away"],
                       "player": {"id": 123, "name": "Player A", "type": "Questionable", "reason": "Illness"}}
        self.xi = {"team": self.match["teams"]["home"], "startXI": [
            {"player": {"id": n, "name": f"Player {n}", "pos": "M"}} for n in range(10, 21)]}
        self.rows = {"fixtures": [self.match], "injuries": [self.injury], "fixtures/lineups": [self.xi]}
        for target in (patch.dict(os.environ, {"PM861_API_FOOTBALL_KEY": "placeholder-test-key"}),
                       patch.object(source.store, "now_utc", side_effect=lambda: store.iso(self.now))):
            target.start()
            self.addCleanup(target.stop)

    def issue(self, root, name, params):
        return self.rows[name], {"received_at": store.iso(self.now), "provider_http_date": store.iso(self.now)}

    def collect(self):
        # Exercise the provider contract independently of the real free-season and rights gates.
        with patch.object(source, "_provider_state", return_value="available"), patch.object(source, "_issue", side_effect=self.issue):
            return source.collect(self.root, self.fixture)

    def snapshot(self, at=None):
        with patch.object(source, "_provider_state", return_value="available"):
            return source.as_of(self.root, self.fixture, at or self.now)

    def test_provider_outage_is_not_a_withdrawal_or_an_expected_empty_report(self):
        from polymarket861 import big5_scheduler as scheduler

        first = self.now
        self.collect()
        with patch.object(source, "_provider_state", return_value="available"):
            self.assertNotIn("source_errors", scheduler._news(self.root, self.fixture, first))
            for error in ("network_error", "rate_limited"):
                self.now += timedelta(minutes=1)
                with patch.object(source, "_issue", side_effect=source.Unavailable(error)):
                    result = source.collect(self.root, self.fixture)
                self.assertIn(error, scheduler._player_poll_errors(result)[0])
                snapshot = scheduler._news(self.root, self.fixture, self.now)
                self.assertIn(error, snapshot["source_errors"]["player_status"])
                self.assertNotIn("source_errors", scheduler._news(self.root, self.fixture, first))
            self.now += timedelta(minutes=1)
            self.rows["injuries"] = []
            self.rows["fixtures/lineups"] = []
            self.collect()
            corrected = scheduler._news(self.root, self.fixture, self.now)
            self.assertEqual(corrected["player_status"]["injuries"]["state"], "no_published_players")
            self.assertNotIn("source_errors", corrected)
        for reason in ("free_season_unavailable", "rights_unverified", "not_configured"):
            with patch.object(source, "_provider_state", return_value=reason):
                self.assertEqual(scheduler._player_poll_errors(source.collect(self.root, self.fixture)), [])
                self.assertNotIn("source_errors", scheduler._news(self.root, self.fixture, self.now))

    def test_no_key_never_connects_or_creates_source_state(self):
        with patch.dict(os.environ, {"PM861_API_FOOTBALL_KEY": ""}), patch.object(source, "_issue") as network:
            self.assertEqual(source.collect(self.root, self.fixture)["state"], "not_configured")
            self.assertEqual(source.as_of(self.root, self.fixture, self.now)["injuries"]["state"], "not_configured")
            network.assert_not_called()
        self.assertFalse((self.root / "player-status").exists())

    def test_key_alone_never_enables_paid_seasons_or_unlicensed_retention(self):
        with patch.object(source, "_issue") as network:
            for season, state in (("2026-27", "free_season_unavailable"), ("2024-25", "rights_unverified")):
                match = dict(self.fixture, season=season)
                self.assertEqual(source.collect(self.root, match), {"state": state})
                report = source.as_of(self.root, match, self.now)
                self.assertEqual(report["lineup"]["state"], state)
                self.assertIsNone(report["evidence_sha256"])
            network.assert_not_called()
        self.assertFalse((self.root / "player-status").exists())
        # A pre-fix projection is preserved on disk, but cannot start a newly sealed
        # scenario when its local rights/season are outside present permission.
        self.collect()
        scoped = source.as_of(self.root, self.fixture, self.now)
        self.assertEqual(scoped["injuries"]["teams"]["away"]["players"], [])
        self.assertEqual(self.snapshot()["injuries"]["state"], "observed")

    def test_exacts_private_receipt_injuries_and_starting_eleven_have_separate_states(self):
        self.collect()
        self.assertEqual(self.snapshot(self.now - timedelta(seconds=1))["injuries"]["state"], "pending")
        first = self.snapshot()
        self.assertEqual(first["injuries"]["teams"]["away"]["players"][0]["provider_type"], "Questionable")
        self.assertEqual(first["injuries"]["teams"]["home"]["state"], "unknown")
        self.assertEqual(first["lineup"]["teams"]["away"]["state"], "unknown")
        self.assertEqual(len(first["lineup"]["teams"]["home"]["players"]), 11)
        self.assertEqual(first["lineup"]["teams"]["home"]["state"], "published")
        self.now += timedelta(seconds=20)
        self.collect()
        self.assertEqual(first["evidence_sha256"], self.snapshot()["evidence_sha256"])
        self.now += timedelta(seconds=20)
        self.rows["injuries"] = []
        self.rows["fixtures/lineups"] = [dict(self.xi, startXI=self.xi["startXI"][:-1])]
        self.collect()
        updated = self.snapshot()
        self.assertEqual(updated["injuries"]["state"], "no_published_players")
        self.assertEqual(updated["injuries"]["teams"]["away"]["state"], "unknown")
        self.assertEqual(updated["lineup"]["teams"]["home"]["state"], "unknown")
        self.assertNotEqual(first["evidence_sha256"], updated["evidence_sha256"])

    def test_ambiguous_swapped_rescheduled_and_wrong_tournament_are_held(self):
        for fixture, expected in (
            ([self.match, self.match], "fixture_identity_ambiguous"),
            ([dict(self.match, teams={"home": self.match["teams"]["away"], "away": self.match["teams"]["home"]})],
             "fixture_identity_unmatched"),
            ([dict(self.match, fixture=dict(self.match["fixture"], date=store.iso(self.kick + timedelta(hours=1))))],
             "fixture_identity_unmatched"),
            ([dict(self.match, league={"id": 2, "season": 2026})], "fixture_identity_unmatched"),
            ([dict(self.match, fixture=dict(self.match["fixture"], status={"short": "1H"}))], "fixture_not_pregame")):
            self.now += timedelta(seconds=1)
            self.rows["fixtures"] = fixture
            self.assertEqual(self.collect()["state"], expected)
            self.assertEqual(self.snapshot()["lineup"]["state"], expected)
        self.now += timedelta(seconds=1)
        self.rows["fixtures"] = [self.match]
        self.rows["injuries"] = [dict(self.injury, team={"id": 8, "name": "Unregistered"})]
        self.collect()
        self.assertEqual(self.snapshot()["injuries"]["state"], "player_data_identity_mismatch")

    def test_rate_limit_denial_is_shared_across_routes_with_safe_error_and_private_token(self):
        reply = Mock(status_code=429, headers={"Retry-After": "120"})
        with patch.object(source.httpx, "get", return_value=reply) as fetch:
            with self.assertRaisesRegex(source.Unavailable, "^rate_limited$"):
                source._issue(self.root, "fixtures", {})
            with self.assertRaisesRegex(source.Unavailable, "^rate_limited$"):
                source._issue(self.root, "injuries", {})
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs["headers"], {"x-apisports-key": "placeholder-test-key"})
        self.assertNotIn("placeholder-test-key", fetch.call_args.args[0])
        self.assertFalse(fetch.call_args.kwargs["follow_redirects"])

    def test_uncalibrated_scenario_requires_a_match_absence_and_independent_player_identity(self):
        matrix, _ = football_model._goals_matrix(1.35, 1.20, 0.01)
        probs, totals = football_model.probabilities(matrix)
        base = {"model": "B1", "rates": {"home": 1.35, "away": 1.20, "rho": 0.01},
                "kickoff": store.iso(self.kick), "totals": totals, "probabilities": probs}
        name = "Jordan Becker"
        self.injury["player"].update(name=name, type="Missing Fixture")
        self.collect()
        roster = {"players": [{"name": name, "source_position": "FW"}], "ready_at": store.iso(self.now),
                  "evidence": [{"source_sha256": "a" * 64}]}
        snapshot = {"player_status": self.snapshot(), "teams": {"away": {"roster": roster}}}
        original = deepcopy(base)
        calibrated = source.absence_scenario(base, snapshot, cutoff=self.now)
        self.assertTrue(calibrated["changed"])
        self.assertEqual(calibrated["scope"], "uncalibrated_private_research")
        self.assertLess(calibrated["probabilities"]["away_win"], base["probabilities"]["away_win"])
        self.assertEqual(original, base)
        self.assertFalse(source.absence_scenario(base, {"teams": snapshot["teams"], "press": {"title": name}},
                                                cutoff=self.now)["changed"])
        self.assertFalse(source.absence_scenario(base, snapshot, cutoff=self.now - timedelta(minutes=1))["changed"])
        for full_name in ("J. Becker", "Jordan Other"):
            bad = deepcopy(snapshot)
            bad["teams"]["away"]["roster"]["players"][0]["name"] = full_name
            self.assertFalse(source.absence_scenario(base, bad, cutoff=self.now)["changed"])
        for label in ("Questionable", "Injured", ""):
            bad = deepcopy(snapshot)
            bad["player_status"]["injuries"]["teams"]["away"]["players"][0]["provider_type"] = label
            self.assertFalse(source.absence_scenario(base, bad, cutoff=self.now)["changed"])
        bad = deepcopy(snapshot)
        bad["player_status"]["lineup"]["teams"]["away"]["players"] = [{"player_id":123, "name":name}]
        self.assertFalse(source.absence_scenario(base, bad, cutoff=self.now)["changed"])
