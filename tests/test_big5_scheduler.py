import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from polymarket861 import big5_scheduler as scheduler
from polymarket861 import football, store


class DomesticSchedulerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.clock = store.instant("2026-11-27T00:00:00Z")
        self.kickoff = store.instant("2026-11-28T00:03:00Z")
        self.fixture = self.match("FC Bayern München", "SV Werder Bremen", self.kickoff)
        past = self.match("SV Werder Bremen", "FC Bayern München", self.clock - timedelta(days=40))
        self.previous = replace(past, status="completed", result_versions=(
            football.ResultVersion(past.kickoff + timedelta(hours=3), (2, 1), "observed", "archive:past"),))
        self.upcoming = True
        self.pregame = True
        self.fingerprint = "first"
        for method, mock in (("_clock", lambda: self.clock), ("_load", self.inputs),
                             ("_pregame", lambda *args: self.pregame), ("_news", self.news)):
            patcher = patch.object(scheduler, method, side_effect=mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    def match(self, home, away, kick):
        fid = football.fixture_id("2026-27", home, away, league="bun")
        observed = self.clock - timedelta(days=3)
        return football.Fixture(fid, "2026-27", 12, kick, home, away,
                                schedule_known_at=observed, schedule_versions=((observed, kick),),
                                schedule_grades=("observed_schedule",))

    def inputs(self, root, code, history, market_root):
        if code != "bun":
            raise ValueError("result input temporarily unavailable")
        fixture = self.fixture
        binding = {"slug": "bun-bay-svw", "event_id": "11", "home_id": "8", "away_id": "9",
                   "kickoff": store.iso(fixture.kickoff), "receipt": store.iso(fixture.schedule_known_at),
                   "sha256": "a" * 64}
        return SimpleNamespace(fixtures=(self.previous, fixture),
                               upcoming=(fixture,) if self.upcoming else (),
                               provenance={"market_fixtures": {fixture.fixture_id: binding}})

    def news(self, root, fixture, at):
        return {"facts": [], "evidence_sha256": self.fingerprint, "teams": {}}

    def tick(self, *leagues):
        return scheduler.tick(self.root, leagues=leagues or ["bun"],
                              market_state_dir=self.root / "market", collect_news=False)

    def details(self):
        return scheduler.status(self.root, leagues=["bun"], fixture_id=self.fixture.fixture_id)["leagues"]["bun"]

    def test_preview_fixed_windows_are_prospective_and_retries_deduplicate(self):
        initial = self.tick("bun", "lal")["leagues"]
        self.assertEqual(initial["bun"]["state"], "ok")
        self.assertEqual(initial["lal"]["state"], "retry")
        self.tick()
        report = self.details()
        self.assertEqual(report["dispatch"]["T-24h"], {"sealed": 1})
        self.assertEqual(report["dispatch"]["preview"], {"sealed": 1})
        self.assertEqual(report["dispatch"]["T-60m"], {"pending": 1})
        seals = [row for row in report["fixture_dispatch"] if row["sealed"]]
        self.assertEqual({r["attempts"] for r in seals}, {1})
        for row in seals:
            self.assertLess(row["sealed"], row["deadline"])
            self.assertEqual(store.digest(row["payload"]), row["digest"])
            self.assertEqual(row["payload"]["baseline"]["evidence"]["training_matches"], 1)
            self.assertFalse(row["payload"]["formal_eligible"])
        self.clock = self.kickoff - timedelta(minutes=62)
        self.pregame = False
        self.tick()
        self.assertEqual(self.details()["dispatch"]["T-60m"], {"retry": 1})
        self.clock = self.kickoff - timedelta(minutes=61)
        self.pregame = True
        self.tick()
        at_sixty = next(row for row in self.details()["fixture_dispatch"] if row["horizon"] == "T-60m")
        self.assertEqual((at_sixty["state"], at_sixty["attempts"]), ("sealed", 2))

    def test_missed_and_rescheduled_fixtures_never_backdate(self):
        self.clock = self.kickoff - timedelta(hours=8)
        self.tick()
        report = self.details()
        self.assertEqual(report["dispatch"]["T-24h"], {"missed": 1})
        old_digest = report["eligible_upcoming"][0]["latest_seal"]["digest"]
        later = self.kickoff + timedelta(days=1)
        self.fixture = replace(self.fixture, kickoff=later, schedule_known_at=self.clock,
                               schedule_versions=(*self.fixture.schedule_versions, (self.clock, later)),
                               schedule_grades=("observed_schedule", "observed_schedule"))
        self.tick()
        report = self.details()
        self.assertEqual(report["dispatch"]["T-60m"], {"pending": 1, "withdrawn": 1})
        self.assertEqual(report["dispatch"]["preview"], {"sealed": 2})
        self.assertIn(old_digest, [r["digest"] for r in report["fixture_dispatch"]])
        self.assertEqual([m["kickoff"] for m in report["eligible_upcoming"]], [store.iso(later)])

    def test_receipt_changes_score_corrections_and_withdrawals(self):
        self.tick()
        self.fingerprint = "second"
        self.clock += timedelta(minutes=1)
        self.tick()
        self.tick()
        report = self.details()
        self.assertEqual(report["dispatch"]["news_update"], {"sealed": 1})
        news = next(r for r in report["fixture_dispatch"] if r["horizon"] == "news_update")
        self.assertFalse(news["payload"]["pair"]["changed"])
        self.clock = self.kickoff + timedelta(hours=3)
        self.upcoming = False
        self.fixture = replace(self.fixture, status="completed", result_versions=(
            football.ResultVersion(self.clock, (0, 2), "observed", "archive:a"),))
        self.tick()
        self.tick()
        self.assertEqual(self.details()["score_revisions"], 3)
        self.clock += timedelta(hours=1)
        self.fixture = replace(self.fixture, result_versions=(*self.fixture.result_versions,
            football.ResultVersion(self.clock, (2, 0), "observed", "archive:b")))
        self.tick()
        self.clock += timedelta(hours=1)
        self.fixture = replace(self.fixture, status="withdrawn", quarantine_reason="awarded by league",
                               result_versions=(*self.fixture.result_versions,
            football.ResultVersion(self.clock, None, "observed", "archive:c")))
        self.tick()
        scores = self.details()["fixture_results"]
        self.assertEqual(len(scores), 9)
        self.assertEqual(len([r for r in scores if "scores" not in r and r["score"] is None]), 3)

    def test_two_workers_seal_once_and_readonly_status(self):
        self.assertEqual(scheduler.status(self.root, leagues=["bun"])["leagues"]["bun"]["state"], "not_started")
        self.assertFalse((self.root / "forecast-scheduler").exists())
        with ThreadPoolExecutor(2) as pool:
            results = [f.result() for f in [pool.submit(self.tick), pool.submit(self.tick)]]
        self.assertTrue(all(r["leagues"]["bun"]["state"] in {"ok", "newer_cycle_in_progress"} for r in results))
        with closing(scheduler._database(self.root, "bun")) as db:
            rows = list(db.execute("SELECT attempts,payload FROM domestic_jobs WHERE sealed IS NOT NULL"))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r[0] == 1 for r in rows))
        self.assertTrue(all(json.loads(r[1])["scope"] == scheduler.SCOPE for r in rows))
        with self.assertRaisesRegex(ValueError, "select at least"):
            self.tick("epl")
