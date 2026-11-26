import math
from copy import deepcopy
import tempfile
import unittest
from contextlib import closing

from polymarket861 import news_events, news_model, news_weight_audit, season_health, store
from test_news_public_evidence import replay


def player(ident, team, basis, *, reason=None):
    return {
        "id": "claim-" + ident, "player_id": ident, "player_name": ident, "team": team, "side": "home",
        "role": "forward", "position": "FW", "event": "injury", "status": "reported", "availability": "doubtful",
        "chance_of_playing": 50, "absence_weight": 0.225, "importance_multiplier": 0.55,
        "duration_multiplier": 0.8, "prior_weight": 0.225 * 0.8 * 0.55,
        "duration_basis": "b1_weighted_training_matches",
        "team_training_matches": 10, "matches_in_episode": 2, "weight_before_episode": 8.0,
        "weight_during_episode": 2.0, "player_importance": {"basis": basis, "reason": reason},
        "own_log_shift": -0.04 * 0.225 * 0.8 * 0.55, "opponent_log_shift": 0.005 * 0.225 * 0.8 * 0.55,
    }


class PublishedWeights(unittest.TestCase):
    def test_stored_factors_and_caps_check_the_rate_shift(self):
        row = news_weight_audit.public_claim(player("one", "Arsenal", "neutral", reason="no_minutes"))
        self.assertEqual(row["source_absence_probability"], 0.5)
        self.assertEqual(row["duration_evidence"]["matches_in_episode"], 2)
        policy = deepcopy(news_model.POLICY)
        policy["max_abs_log_adjustment_per_team"] = 0.003
        shift = {"home": -0.003, "away": row["opponent_log_shift"]}
        base = {"lambda_home": 1.4, "lambda_away": 1.0}
        rates = {"lambda_" + s: base["lambda_" + s] * math.exp(v) for s, v in shift.items()}
        entry = {"applied_player_weights": [row], "applied_claims": 1, "log_goal_shifts": shift,
                 "max_abs_log_adjustment_per_team": 0.003,
                 "arms": {"no_news": {"rates": base}, "with_news": {"rates": rates}}}
        replay._verify_player_weights(entry, policy)
        row["role"] = "goalkeeper"
        with self.assertRaisesRegex(ValueError, "role and combined weight"):
            replay._verify_player_weights(entry, policy)
        row["role"] = "forward"
        row["duration_evidence"]["weight_during_episode"] = 3
        with self.assertRaisesRegex(ValueError, "training exposure"):
            replay._verify_player_weights(entry, policy)
        row["duration_evidence"]["weight_during_episode"] = 2
        rates["lambda_away"] = 0.98
        with self.assertRaisesRegex(ValueError, "goal rates"):
            replay._verify_player_weights(entry, policy)

    def test_rolling_counts_choose_one_revision_and_do_not_substitute_missing_legacy_fields(self):
        at = store.iso("2026-11-26T10:00:00Z")
        kickoff = store.iso("2026-11-27T20:00:00Z")
        fixture = {"fixture_id": "test-fixture", "home": "Arsenal", "away": "Chelsea", "kickoff": kickoff}
        players = [player("home", "Arsenal", "neutral", reason="directory_missing"),
                   player("away", "Chelsea", "confirmed_lineup_starts"), {"id": "old", "team": "Arsenal"}]
        with tempfile.TemporaryDirectory() as root, closing(store.connect(root)) as db, db:
            db.executescript(news_events._SCHEMA)
            prior = None
            for seq, items in enumerate([players, players, []], start=1):
                cut = store.iso(f"2026-11-26T0{seq+5}:00:00Z") if seq < 3 else store.iso("2026-11-26T11:00Z")
                doc = {"fixture_id": fixture["fixture_id"], "kickoff": kickoff, "cutoff": cut, "created_at": cut,
                       "state_sha256": "state", "previous_digest": prior, "revision": seq,
                       "news_pair": {"input_cutoff": cut, "policy_sha256": "policy", "applied": items}}
                digest = store.digest(doc)
                db.execute("INSERT INTO news_event_updates VALUES(?,?,?,?,?,?,?,?)",
                           (seq, fixture["fixture_id"], kickoff, seq, prior, digest, "state", store.canonical(doc)))
                prior = digest
            result = news_weight_audit.current_usage(db, [fixture], at)
            self.assertEqual((result["forecasts"],result["applied_claims"],result["weights_with_basis"]), (1,3,2))
            self.assertEqual(result["fallback_reasons"], {"directory_missing": 1})
            self.assertEqual(result["unrecorded_basis"], 1)
            self.assertEqual(result["fallback_share"], 0.5)
            self.assertEqual(result["receipts"][0]["input_cutoff"], "2026-11-26T07:00:00.000000Z")
            self.assertIn("player_weight_fallback", dict(season_health.service_problems("news", {"player_weights": result})))
            away = news_weight_audit.current_usage(db, [fixture], at, "Chelsea")
            self.assertEqual((away["fallback_share"], away["alert"]), (0.0, False))
            later = news_weight_audit.current_usage(db, [fixture], store.iso("2026-11-26T12:00Z"))
            self.assertEqual((later["fallback_share"], later["alert"]), (None, False))
