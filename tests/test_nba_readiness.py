import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from polymarket861.store import digest
from polymarket861.nba import data, markets, operations, readiness, storage
from polymarket861.nba.operations import HORIZONS


class NBAReadiness(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "nba"
        self.now = datetime(2026, 11, 25, 9, tzinfo=timezone.utc)
        self.last = date(2026, 11, 23)

    def test_empty_directory_and_unfinished_date(self):
        result = readiness.readiness(self.root, self.last, now=self.now)
        self.assertFalse(any(result["gates"].values()))
        self.assertIsNone(result["calendar"]["eligible_win_pairing_rate"])
        self.assertFalse(self.root.exists())
        with self.assertRaisesRegex(ValueError, "completed"):
            readiness.readiness(self.root, self.now.date(), now=self.now)

    def build(self, *, ignore=None, late_market=None):
        db = storage.connect(self.root)
        self.addCleanup(db.close)
        db.executescript(data.SCHEMA + markets.SCHEMA + operations._SCHEMA)
        for i in range(15):
            day = self.last - timedelta(days=14 - i)
            when = f"{day}T22:00:00.000000Z"
            if day != ignore:
                db.execute(
                    "INSERT INTO nba_schedule_scans VALUES(?,?,?,?)",
                    (i, str(day), str(day), when),
                )
                db.execute(
                    "INSERT INTO nba_schedule_completions VALUES(?,?,?,1)",
                    (i, when, "ok"),
                )
                db.execute(
                    "INSERT INTO nba_schedule_evidence VALUES(?,?,?)",
                    (
                        i,
                        '{"quality":{"phase_rows":{"regular":1,"ist":0,"preseason":0},"quarantined_rows":0}}',
                        str(i),
                    ),
                )
                db.execute(
                    "INSERT INTO nba_market_runs VALUES(?,?,?,?)",
                    (when, when, "ok", "[]"),
                )
                native_id = i + 1
                sha = f"catalogue-{i}"
                db.execute("INSERT INTO nba_market_payloads VALUES(?,?)", (sha, b"raw"))
                db.execute(
                    "INSERT INTO nba_market_requests(id,sent,url,received,code,sha) VALUES(?,?,?,?,200,?)",
                    (native_id, when, "https://example.test/events/keyset", when, sha),
                )
                db.execute(
                    "INSERT INTO nba_market_catalogues VALUES(?,?,?,?)",
                    (
                        when,
                        f"{day}T00:00:00.000000Z",
                        f"{day + timedelta(days=2)}T00:00:00.000000Z",
                        json.dumps([native_id]),
                    ),
                )
            if (
                day == ignore or i == 4
            ):  # one genuine off-day, or a collector outage if requested
                continue
            event, gid = f"venue:{i}", f"bdl:{i}"
            g = {
                "game_id": gid,
                "game_date": str(day),
                "kickoff": when,
                "stage_basis": "source_field",
                "home": "bdl:1",
                "away": "bdl:2",
                "season": 2026,
                "home_abbr": "NY",
                "away_abbr": "BOS",
                "stage": "regular",
                "status": "scheduled",
            }
            db.execute(
                "INSERT INTO nba_game_versions VALUES('balldontlie',?,1,?,?,?,?)",
                (gid, when, when, str(i), json.dumps(g)),
            )
            db.execute(
                "INSERT INTO nba_market_games VALUES(?,?,?,?,?,?,?,1,?,?,0,NULL)",
                (event, str(i), "NYK", "BOS", "1", "2", when, when, when),
            )
            published = when if day != late_market else str(self.now).replace(" ", "T")
            db.execute(
                "INSERT INTO nba_market_schedule VALUES(?,1,?,?)",
                (event, when, published),
            )
            f = {
                "game_id": gid,
                "home_abbr": "NY",
                "away_abbr": "BOS",
                "due": True,
                "sealed": True,
                "warm_sealed": True,
                "qualifying_win_quote": True,
                "paired_win": True,
                "assessed": True,
            }
            fields = {key: {"readiness": {"fixtures": [f]}} for key in HORIZONS}
            db.execute(
                "INSERT INTO nba_period_reports VALUES('day',?,1,?,?,?)",
                (
                    str(day),
                    when,
                    json.dumps({"horizons": fields}),
                    digest({"horizons": fields}),
                ),
            )
            db.execute(
                "INSERT INTO nba_windows VALUES(?,1,'T-60m',?,?,?,1,'sealed','',?,?,?)",
                (
                    gid,
                    when,
                    str(day),
                    when,
                    when,
                    '{"warmup":{"eligible":true}}',
                    str(i),
                ),
            )
        db.commit()
        return db

    def test_fourteen_actual_game_days_off_day_and_independent_gates(self):
        self.build()
        actual = readiness.readiness(self.root, self.last, now=self.now)
        coverage = actual["calendar"]
        self.assertTrue(coverage["qualified"], coverage["reasons"])
        self.assertEqual(
            (coverage["counted_game_days"], coverage["totals"]["due"]), (14, 14)
        )
        self.assertEqual(coverage["eligible_win_pairing_rate"], 1)
        self.assertEqual(coverage["days"][4]["kind"], "observed_no_regular_games")
        self.assertFalse(actual["gates"]["admitted_player_source"])
        self.assertFalse(actual["phase_two_activated"])

    def test_missing_calendar_day_market_only_and_unknown_history_stay_visible(self):
        db = self.build(ignore=self.last - timedelta(days=10))
        day = str(self.last - timedelta(days=2))
        stamp = f"{day}T23:00:00.000000Z"
        db.execute(
            "INSERT INTO nba_market_games VALUES('more','extra','LAL','UTA','3','4',?,1,?,?,0,NULL)",
            (stamp, stamp, stamp),
        )
        db.execute(
            "INSERT INTO nba_market_schedule VALUES('more',1,?,?)", (stamp, stamp)
        )
        db.execute("DELETE FROM nba_period_reports WHERE period=?", (str(self.last),))
        db.commit()
        cov = readiness.readiness(self.root, self.last, now=self.now)["calendar"]
        self.assertIn("calendar_coverage_incomplete", cov["reasons"])
        self.assertEqual(cov["days"][-3]["horizons"]["T-60m"]["unmatched_listings"], 1)
        self.assertIn(str(self.last), cov["listing_days_without_warm_seal"])
        self.assertEqual(cov["days"][-1]["horizons"]["T-60m"]["due"], 1)
        db.execute("UPDATE nba_windows SET payload='{}'")
        db.commit()
        self.assertEqual(
            readiness.readiness(self.root, self.last, now=self.now)["calendar"][
                "counted_game_days"
            ],
            0,
        )
