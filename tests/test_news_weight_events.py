"""The event worker wakes on a change to a player's effective probability adjustment."""

import json
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket861 import evidence, evidence_sources, news, news_events, news_minutes, news_model, news_roster, news_sources, store
from polymarket861.market_provider import Reply
from test_news_importance import envelope, reply, usage_rows
from test_news_matchday import FID, HOME, SEASON, roster_transport, seed_fixture
from test_paired_proof import at


class WeightEvents(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root, self.history = Path(tmp.name) / "runtime", Path(tmp.name) / "football"
        with at("2026-08-01T10:00Z"):
            seed_fixture(self.root, self.history)
        self.source = news_sources.default_sources(HOME, SEASON)[0]

    def squad(self):
        news_roster.refresh(self.root, HOME, SEASON, getter=roster_transport(HOME))

    def wire(self, chance):
        now = store.now_utc()
        rows = [] if chance is None else [{"display_name": "Morgan Smith", "player_id": 1, "team": "Arsenal",
                                           "status": "injured", "category": "injury", "chance_of_playing": chance}]
        raw = json.dumps(envelope(rows, now)).encode()

        def poll(config, team, season, cursor):
            return news_sources.poll(config, team, season, cursor, getter=lambda url: Reply(raw, now, now, {}, url))

        result = news.sync(self.root, team=HOME, force=True, defaults=lambda *_: [self.source],
                           poller=poll, roster_refresh=lambda *_: {"status": "cached"})
        self.assertEqual(result["succeeded"], 1, result)

    def event(self, published):
        outcome = news_events.refresh(self.root, history_root=self.history)
        self.assertEqual(outcome["published"], published, outcome)
        return news_events.list_updates(self.root, FID)[0]

    def prior_fixture(self, i, when):
        return store.register_fixture(self.root, {"fixture_id": f"previous-{i}", "season": SEASON,
            "home": HOME, "away": f"Visitors {i}", "round_id": 1, "kickoff": when,
            "known_at": store.now_utc(), "source": "league", "locator": f"league/{i}"})

    def test_same_label_probability_changes_then_expires_at_the_source_boundary(self):
        with at("2026-08-11T08:00Z"):
            self.squad()
            self.wire(25)
            first = self.event(1)
        with at("2026-08-11T08:10Z"):
            self.wire(75)
            change = self.event(1)
        self.assertEqual(change["previous_digest"], first["digest"])
        for doc, chance, weight in ((first, 25, 0.3375), (change, 75, 0.1125)):
            self.assertEqual((doc["state"][0]["availability"], doc["state"][0]["chance_of_playing"]), ("doubtful", chance))
            self.assertAlmostEqual(doc["news_pair"]["applied"][0]["prior_weight"], weight)
        self.assertNotEqual(first["news_pair"]["with_news"], change["news_pair"]["with_news"])
        with at("2026-08-11T08:20Z"):
            self.wire(75)
            self.assertEqual(self.event(0), change)
        with at("2026-08-11T14:19:59Z"):
            self.event(0)
        with at("2026-08-11T14:20:00Z"):
            self.assertEqual(news_events.refresh(self.root, history_root=self.history)["attempted"], 0)
        with at("2026-08-11T14:20:00.000001Z"):
            expired = self.event(1)
            self.assertFalse(expired["news_pair"]["changed"])
            self.assertEqual(expired["news_pair"]["excluded_reasons"], {"stale_wire_observation": 1})
        with at("2026-08-11T14:30Z"):
            self.wire(75)
            self.assertEqual(self.event(1)["previous_digest"], expired["digest"])

    def test_continuous_episode_crosses_exact_duration_boundary_and_reset_restores_effect(self):
        with at("2026-08-01T10:05Z"):
            self.prior_fixture(1, "2026-08-08T15:00Z")
        with at("2026-08-05T09:00Z"):
            self.squad()
            self.wire(25)
        with at("2026-08-12T08:55Z"):
            self.squad()
            self.wire(25)
            first = self.event(1)
        with at("2026-08-12T08:59:59Z"):
            self.assertEqual(news_events.refresh(self.root, history_root=self.history)["attempted"], 0)
        with at("2026-08-12T09:00Z"):
            decayed = self.event(1)
        self.assertEqual(decayed["state"][0]["duration_multiplier"], 0.75)
        self.assertAlmostEqual(decayed["news_pair"]["applied"][0]["prior_weight"], 0.75 * first["news_pair"]["applied"][0]["prior_weight"])
        with at("2026-08-12T09:10Z"):
            self.wire(None)
            self.event(1)
        with at("2026-08-12T09:20Z"):
            self.wire(25)
            restored = self.event(1)
        self.assertEqual(restored["news_pair"]["with_news"], first["news_pair"]["with_news"])
        self.assertEqual(restored["state"], first["state"])
        onset = store.instant("2026-07-01T00:00Z")
        for days, _ in news_model.POLICY["duration_steps_days"][1:]:
            deadline = onset + timedelta(days=days)
            snap = {"cutoff": store.iso(deadline - timedelta(minutes=1)), "facts": [{
                "source": news_sources.WIRE, "time_basis": "provider_snapshot",
                "received_at": store.iso(onset), "published_at": store.iso(onset),
                "absence_first_seen_at": store.iso(onset), "last_seen_at": store.iso(deadline - timedelta(minutes=2)),
                "last_seen_state_as_of": store.iso(deadline - timedelta(minutes=2)),
            }]}
            self.assertEqual(news_events._review(snap, None), store.iso(deadline))

    def test_independently_received_lineups_and_minutes_wake_a_fixture_inside_its_review_interval(self):
        kicks = [f"2026-08-{i:02}T15:00Z" for i in (4, 6, 8)]
        with at("2026-08-01T10:05Z"):
            for i, kick in enumerate(kicks):
                self.prior_fixture(i, kick)
        with at("2026-08-10T12:00Z"):
            for i, kick in enumerate(kicks):
                end = store.iso(store.instant(kick) + timedelta(hours=2))
                store.record_result(self.root, {"fixture_id": f"previous-{i}", "home_goals": 1, "away_goals": 0,
                    "source": "league", "locator": f"score/{i}", "finalized_at": end, "published_at": end, "received_at": end})
        with at("2026-08-11T08:00Z"):
            self.squad()
            self.wire(25)
            first = self.event(1)
        stamp = "2026-08-11T08:01Z"
        with at(stamp), patch.object(evidence_sources, "now_utc", return_value=stamp), patch.object(evidence, "now_utc", return_value=stamp):
            evidence.register_source(self.root, {"source_id": "test-lineups", "free": True, "status": "active",
                "upstream": "club", "terms_locator": "https://club.example/terms",
                "fields": {"lineup": {"methods": ["manual"], "uses": ["prediction"]}}})
            evidence.import_evidence(self.root, [{"source_id": "test-lineups", "method": "manual", "field": "lineup",
                "fixture_id": f"previous-{i}", "team": HOME, "locator": f"squad/{i}", "revision": 1,
                "published_at": store.iso(store.instant(kick) - timedelta(hours=1)), "received_at": stamp, "valid_until": kick,
                "value": {"confirmed": True, "starters": [f"Club Player {j}" for j in range(11)], "bench": ["Morgan Smith"]}}
                for i, kick in enumerate(kicks)])
            bench = self.event(1)
        self.assertEqual(bench["previous_digest"], first["digest"])
        self.assertEqual(bench["state"][0]["importance_multiplier"], 0.25)
        with at("2026-08-11T08:02Z"):
            rows = [row | {"club": "Arsenal"} for row in usage_rows()]
            capture = news_minutes.refresh(self.root, HOME, SEASON, getter=reply(envelope(rows, store.now_utc())))
            self.assertEqual(capture["state"], "captured", capture)
            minutes = self.event(1)
            self.assertEqual(self.event(0), minutes)
        self.assertEqual(minutes["previous_digest"], bench["digest"])
        self.assertGreater(minutes["state"][0]["importance_multiplier"], 0.25)
        self.assertEqual(minutes["news_pair"]["applied"][0]["player_importance"]["basis"], "epl_season_minutes_and_starts")
        with closing(store.connect(self.root)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM news_wire_polls").fetchone()[0], 1)
