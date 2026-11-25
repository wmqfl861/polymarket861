import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from polymarket861 import result_feed as feed
from polymarket861.football import fixture_id, load_fixtures
from polymarket861.store import record_result, register_fixture


def match(mid, home, away, score, *, rnd=1):
    return {
        "leagueShortcut": "pl",
        "leagueSeason": 2026,
        "leagueId": 5996,
        "matchID": mid,
        "team1": {"teamName": home},
        "team2": {"teamName": away},
        "group": {"groupOrderID": rnd},
        "matchDateTimeUTC": "2026-08-21T19:00:00Z",
        "matchIsFinished": score is not None,
        "matchResults": []
        if score is None
        else [
            {"resultTypeKind": "HalfTime", "pointsTeam1": 0, "pointsTeam2": 0},
            {
                "resultTypeKind": "After90Minutes",
                "pointsTeam1": score[0],
                "pointsTeam2": score[1],
            },
        ],
    }


class ResultFeedTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.when = "2026-11-25T00:00:00.000000Z"
        self.clock = patch.object(feed, "now_utc", side_effect=lambda: self.when)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.season = "2026-27"
        self.raw = [
            match(1, "FC Arsenal", "Brentford F.C.", [2, 1]),
            match(2, "Chelsea FC", "Fulham FC", [1, 1]),
        ]
        for a, b in [("Arsenal FC", "Brentford FC"), ("Chelsea FC", "Fulham FC")]:
            register_fixture(
                self.root,
                {
                    "fixture_id": fixture_id(self.season, a, b),
                    "season": self.season,
                    "round_id": 1,
                    "home": a,
                    "away": b,
                    "kickoff": "2026-08-21T19:00:00Z",
                    "known_at": "2026-08-01T00:00:00Z",
                    "source": "openfootball",
                    "locator": "published fixture",
                },
            )

    def test_only_final_90_minutes_validate_league_and_duplicates(self):
        row = self.raw[0].copy()
        row["matchResults"] = [
            {"resultTypeKind": "HalfTime", "pointsTeam1": 0, "pointsTeam2": 0},
            {"resultTypeKind": "AfterPenalties", "pointsTeam1": 6, "pointsTeam2": 5},
        ]
        *_, s, err = next(feed._full_time([row], 2026))
        self.assertIsNone(s)
        self.assertEqual(err, "ambiguous_90_minute_final")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            list(feed._full_time([self.raw[0], self.raw[0]], 2026))
        with self.assertRaisesRegex(ValueError, "league id"):
            list(feed._full_time([row | {"leagueId": 8}], 2026))

    def test_conflict_retained_and_candidate_only_available_from_receipt(self):
        record_result(
            self.root,
            {
                "fixture_id": fixture_id(self.season, "Chelsea FC", "Fulham FC"),
                "home_goals": 1,
                "away_goals": 0,
                "archive_at": "2026-08-22T00:00:00Z",
                "finalized_by": "2026-08-22T00:00:00Z",
                "published_by": "2026-08-22T00:00:00Z",
                "source": "openfootball",
                "locator": "evidence://1",
            },
        )
        with patch.object(
            feed, "_get", return_value=json.dumps(self.raw).encode()
        ) as client:
            a = feed.sync(self.root, 2026)
            b = feed.sync(self.root, 2026)
            self.assertEqual(client.call_count, 1)
        self.assertEqual(a["coverage"]["candidate_only"], 1)
        self.assertEqual(a["quarantined"][0]["existing_score"], [1, 0])
        self.assertEqual(b["fetch_status"], "local_cache")
        base = self.root / "base.jsonl"
        base.write_text("")
        out = self.root / "trial.jsonl"
        c = feed.trial_sidecar(self.root, 2026, base, out)
        self.assertEqual(c["appended_candidate_versions"], 1)
        entry = json.loads(out.read_text())
        self.assertEqual(entry["finalized_by"], self.when)
        self.assertIn("ODbL", entry["source"])
        self.assertEqual(base.read_text(), "")
        with self.assertRaisesRegex(ValueError, "must differ"):
            feed.trial_sidecar(self.root, 2026, base, base)
        j = self.root / self.season / "en.1.json"
        j.parent.mkdir()
        j.write_text(
            json.dumps(
                {
                    "matches": [
                        {
                            "round": "Matchday 1",
                            "date": "2026-08-21",
                            "time": "20:00",
                            "team1": "Arsenal FC",
                            "team2": "Brentford FC",
                        }
                    ]
                }
            )
        )
        f = load_fixtures(j, evidence_path=out)[0]
        self.assertEqual(f.result_versions[0].score, (2, 1))
        self.assertEqual(f.result_versions[0].grade, "archived_upper_bound")

    def test_poll_changed_round_only_and_stale_cache(self):
        with patch.object(
            feed, "_get", side_effect=[b'"marker"', json.dumps(self.raw).encode()]
        ):
            feed.sync(self.root, 2026, round_id=1)
        self.when = "2026-11-25T07:00:00.000000Z"
        with patch.object(feed, "_get", return_value=b'"marker"') as client:
            self.assertEqual(
                feed.sync(self.root, 2026, round_id=1)["fetch_status"],
                "source_marker_unchanged",
            )
            self.assertEqual(client.call_count, 1)
        self.when = "2026-11-25T14:00:00.000000Z"
        with patch.object(
            feed, "_get", side_effect=OSError("network unavailable")
        ) as client:
            self.assertIn(
                "stale_cache", feed.sync(self.root, 2026, round_id=1)["fetch_status"]
            )
            self.assertIn(
                "backoff", feed.sync(self.root, 2026, round_id=1)["fetch_status"]
            )
            self.assertEqual(client.call_count, 1)
