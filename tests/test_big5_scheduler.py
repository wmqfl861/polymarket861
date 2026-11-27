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
from polymarket861 import domestic_editions, football, league_live_inputs, store


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
        self.assertNotIn("news_update", report["dispatch"])
        self.assertNotIn("forecast_update", report["dispatch"])
        self.assertEqual(report["evidence_changes"], 1)
        change, = report["fixture_evidence_changes"]
        self.assertEqual((change["before_sha"], change["after_sha"], change["disposition"]),
                         ("first", "second", "evidence_only"))
        self.assertEqual(change["changed_outputs"], [])
        self.clock = self.kickoff + timedelta(hours=3)
        self.upcoming = False
        self.fixture = replace(self.fixture, status="completed", result_versions=(
            football.ResultVersion(self.clock, (0, 2), "observed", "archive:a"),))
        self.tick()
        self.tick()
        self.assertEqual(self.details()["score_revisions"], 2)
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
        self.assertEqual(len(scores), 6)
        self.assertEqual(len([r for r in scores if "scores" not in r and r["score"] is None]), 2)
        self.assertEqual(self.details()["fixture_evaluation"], [])

    def test_equal_receipt_clock_withholds_conflicting_finals_until_agreement(self):
        self.tick()
        self.upcoming = False
        self.clock = self.kickoff + timedelta(hours=3)
        first = store.iso(self.clock)
        initial = (first, "wikipedia", "wiki:a", first, (2, 1))
        disputed = (first, "openfootball", "open:b", first,
                    replace(self.fixture, status="completed", home_goals=0, away_goals=3))
        observations = [initial]
        versions, _ = league_live_inputs._score_versions(self.fixture, observations, self.fixture.schedule_versions)
        self.fixture = replace(self.fixture, status="completed", result_versions=versions)
        self.tick()
        self.assertEqual(self.details()["scored_samples_by_horizon"], {"preview": 1, "T-24h": 1})

        observations.append(disputed)
        for ordering in (observations, list(reversed(observations))):
            versions, conflict = league_live_inputs._score_versions(self.fixture, ordering, self.fixture.schedule_versions)
            self.assertTrue(conflict)
            self.assertIsNone(football.result_as_of(replace(self.fixture, result_versions=versions), self.clock).score)
        self.fixture = replace(self.fixture, result_versions=versions)
        self.tick()
        self.tick()
        report = self.details()
        self.assertEqual((report["scored_samples_by_horizon"], report["fixture_evaluation"]), ({}, []))
        self.assertEqual(report["score_revisions"], 4)

        self.clock += timedelta(microseconds=1)
        correction = store.iso(self.clock)
        observations.append((correction, "wikipedia", "wiki:c", correction, (0, 3)))
        versions, conflict = league_live_inputs._score_versions(self.fixture, observations, self.fixture.schedule_versions)
        self.assertFalse(conflict)
        self.fixture = replace(self.fixture, result_versions=versions)
        self.assertIsNone(football._result_at(self.fixture, store.instant(first)))
        self.assertEqual(football._result_at(self.fixture, self.clock), (0, 3))
        self.tick()
        self.tick()
        report = self.details()
        self.assertEqual([r["score"] for r in report["fixture_evaluation"]], [[0, 3], [0, 3]])
        self.assertEqual(report["score_revisions"], 6)

    def test_retraction_after_rescheduling_keeps_the_original_kickoff(self):
        at = self.kickoff + timedelta(hours=2)
        moved = self.kickoff + timedelta(days=2)
        schedule = ((self.clock, self.kickoff), (at + timedelta(minutes=1), moved))
        seen = store.iso(at)
        withdrawn = store.iso(at + timedelta(minutes=2))
        confirmed = store.iso(moved + timedelta(hours=2))
        observations = [(seen, "openfootball", "open:a", seen,
                         replace(self.fixture, status="completed", home_goals=2, away_goals=0)),
                        (withdrawn, "wikipedia", "wiki:b", withdrawn, None)]
        versions, conflict = league_live_inputs._score_versions(self.fixture, observations, schedule)
        self.assertFalse(conflict)
        self.assertEqual([(v.actual_kickoff, v.score) for v in versions],
                         [(self.kickoff, (2, 0)), (self.kickoff, None)])
        revised = replace(self.fixture, kickoff=moved, schedule_versions=schedule,
                          schedule_grades=("observed_schedule",) * 2, result_versions=versions)
        healthy = self.match("Borussia Dortmund", "1. FC Union Berlin", moved + timedelta(days=1))
        engine = football.FootballEngine((self.previous, healthy, revised), league="bun", allow_archive_schedule=False)
        result = engine.forecast(healthy.home, healthy.away, healthy.kickoff, at + timedelta(hours=1),
                                 season=healthy.season)
        self.assertEqual(result["evidence"]["training_matches"], 1)
        observations.append((confirmed, "wikipedia", "wiki:c", confirmed, (2, 0)))
        versions, _ = league_live_inputs._score_versions(self.fixture, observations, schedule)
        self.assertEqual((versions[-1].actual_kickoff, versions[-1].score), (moved, (2, 0)))

    def test_score_corrected_training_and_scenario_revisions_once_per_horizon(self):
        self.tick()
        with self.subTest("rolling evidence, retry, rollback"):
            committed = [r["digest"] for r in self.details()["fixture_dispatch"] if r["sealed"]]
            self.clock += timedelta(minutes=1)
            self.fingerprint = "second"
            with ThreadPoolExecutor(2) as pool:
                workers = [pool.submit(self.tick) for _ in range(2)]
                for worker in workers:
                    worker.result()
            self.clock += timedelta(minutes=1)
            self.fingerprint = "first"
            self.tick()
            self.tick()
            self.assertEqual(self.details()["evidence_only_changes"], 2)
            self.assertEqual(committed, [r["digest"] for r in self.details()["fixture_dispatch"] if r["sealed"]])
        with self.subTest("same news and a new observed result affect B0"):
            self.clock += timedelta(minutes=1)
            self.pregame = False
            self.previous = replace(self.previous, result_versions=(*self.previous.result_versions,
                football.ResultVersion(self.clock, (1, 2), "observed", "archive:training-correction")))
            self.tick()
            self.assertEqual(self.details()["dispatch"]["forecast_update"], {"retry": 1})
            self.pregame = True
            self.clock += timedelta(minutes=1)
            with ThreadPoolExecutor(2) as pool:
                workers = [pool.submit(self.tick) for _ in range(2)]
                for worker in workers:
                    worker.result()
            self.tick()
            self.previous = replace(self.previous, result_versions=(*self.previous.result_versions,
                football.ResultVersion(self.clock, (4, 0), "observed", "archive:training-correction-2")))
            self.tick()
            updates = [r for r in self.details()["fixture_dispatch"] if r["horizon"] == "forecast_update"]
            self.assertEqual(len(updates), 2)
            self.assertTrue(all(r["state"] == "sealed" and r["payload"]["changed_outputs"] == ["baseline"]
                                for r in updates))
            self.assertEqual(self.details()["evidence_only_changes"], 2)
        self.clock = self.kickoff + timedelta(hours=3)
        self.upcoming = False
        self.fixture = replace(self.fixture, status="completed", result_versions=(
            football.ResultVersion(self.clock, (2, 1), "observed", "archive:actual"),))
        self.tick()
        report = self.details()
        self.assertEqual(report["score_revisions"], 3)
        self.assertEqual(report["scored_samples_by_horizon"], {"preview": 1, "T-24h": 1, "forecast_update": 1})
        current = next(r for r in report["fixture_evaluation"] if r["horizon"] == "forecast_update")
        self.assertEqual(current["forecast_sha256"], updates[-1]["digest"])
        self.assertEqual(report["match_weighted_scores"]["forecast_update"]["mean_no_news"],
                         current["scores"]["no_news"]["score"])

    def test_fixed_forecast_owns_a_coincident_change_and_other_total_lines_are_compared(self):
        self.tick()
        self.clock = self.kickoff - timedelta(minutes=62)
        self.previous = replace(self.previous, result_versions=(*self.previous.result_versions,
            football.ResultVersion(self.clock, (0, 2), "observed", "archive:new-label")))
        self.tick()
        report = self.details()
        self.assertEqual(report["dispatch"]["T-60m"], {"sealed": 1})
        self.assertEqual(report["dispatch"]["forecast_update"], {"superseded": 1})
        fixed = next(r["payload"] for r in report["fixture_dispatch"] if r["horizon"] == "T-60m")
        original = domestic_editions.signatures(fixed["pair"], fixed["player_scenario"])
        arm = fixed["pair"]["no_news"]
        totals = json.loads(json.dumps(arm["totals"]))
        line = next(iter(totals["away"]))
        totals["away"][line]["over"] += 1e-15
        candidate = {**fixed["pair"], "changed": True, "with_news": {**arm, "totals": totals}}
        self.assertEqual(domestic_editions.signatures(candidate, fixed["player_scenario"]), original)
        totals["away"][line]["over"] -= 0.01
        candidate_signatures = domestic_editions.signatures(candidate, fixed["player_scenario"])
        self.assertNotEqual(candidate_signatures["paired_news"], original["paired_news"])
        self.assertEqual(candidate_signatures["baseline"], original["baseline"])

    def test_player_scenario_can_change_and_expire_without_changing_the_pair(self):
        self.tick()
        cutoff = self.clock + timedelta(minutes=2)

        def news(root, fixture, at):
            return {"facts": [], "evidence_sha256": "status", "source_digests": {"player_status": "injury"},
                    "player_status": {"injuries": {"state": "observed", "teams": {"home": {"players": [1]}}}}}

        def scenario(base, snapshot, *, cutoff, engine=None):
            active = "player_status" in snapshot and cutoff < expiry
            return {"changed": active, "probabilities": dict(base["probabilities"], draw=0.1) if active
                    else base["probabilities"], "status": "test-scenario", "applied": []}

        expiry = cutoff
        with patch.object(scheduler, "_news", side_effect=news), patch.object(
                scheduler.league_player_status, "absence_scenario", side_effect=scenario):
            self.clock += timedelta(minutes=1)
            self.tick()
            self.clock += timedelta(seconds=30)
            with patch.object(scheduler, "_news", return_value={
                "evidence_sha256": None, "source_errors": {"player_status": "unreadable observation"},
            }):
                self.tick()
            self.assertEqual(self.details()["dispatch"]["forecast_update"], {"sealed": 1})
            self.clock = cutoff
            self.tick()
            self.clock += timedelta(minutes=1)
            self.tick()
        report = self.details()
        updates = [r["payload"] for r in report["fixture_dispatch"] if r["horizon"] == "forecast_update"]
        self.assertEqual([u["changed_outputs"] for u in updates], [["player_scenario"], ["player_scenario"]])
        self.assertEqual([u["player_scenario"]["changed"] for u in updates], [True, False])
        self.assertTrue(all(u["pair"]["changed"] is False for u in updates))
        self.assertEqual(report["evidence_changes"], 1)
        self.assertEqual(report["evidence_only_changes"], 0)
        event, = report["fixture_evidence_changes"]
        self.assertEqual(event["changed_sources"], ["player_status"])

    def test_failed_score_verification_does_not_expose_superseded_result(self):
        self.tick()
        self.clock = self.kickoff + timedelta(hours=3)
        self.upcoming = False
        self.fixture = replace(self.fixture, status="completed", result_versions=(
            football.ResultVersion(self.clock, (2, 1), "observed", "archive:a"),))
        self.tick()
        with closing(scheduler._database(self.root, "bun")) as db, db:
            original, = db.execute("SELECT payload FROM domestic_jobs WHERE horizon='preview'").fetchone()
            db.execute("UPDATE domestic_jobs SET payload='{}' WHERE horizon='preview'")
        self.clock += timedelta(hours=1)
        self.fixture = replace(self.fixture, result_versions=(*self.fixture.result_versions,
            football.ResultVersion(self.clock, (1, 2), "observed", "archive:b")))
        detail = self.tick()["leagues"]["bun"]
        self.assertEqual((detail["state"], detail["fixture_errors"][0]["stage"]), ("partial", "score"))
        self.assertEqual(self.details()["scored_samples_by_horizon"], {})
        self.assertEqual(self.details()["score_revisions"], 2)
        with closing(scheduler._database(self.root, "bun")) as db, db:
            db.execute("UPDATE domestic_jobs SET payload=? WHERE horizon='preview'", (original,))
        self.tick()
        self.assertEqual(self.details()["score_revisions"], 4)
        self.assertEqual([r["score"] for r in self.details()["fixture_evaluation"]], [[1, 2], [1, 2]])

    def test_legacy_scores_are_retained_without_weighting_a_current_sample(self):
        self.tick()
        self.clock = self.kickoff + timedelta(hours=3)
        self.upcoming = False
        self.fixture = replace(self.fixture, status="completed", result_versions=(
            football.ResultVersion(self.clock, (2, 1), "observed", "archive:actual"),))
        self.tick()
        with closing(scheduler._database(self.root, "bun")) as db, db:
            preview = db.execute("SELECT * FROM domestic_jobs WHERE horizon='preview'").fetchone()
            result = db.execute("SELECT * FROM domestic_scores WHERE horizon='preview'").fetchone()
            for i in range(3):
                db.execute("INSERT INTO domestic_jobs VALUES(?,?,'news_update',?,?,?,?,?,?,?,?,?,?)",
                    (preview["fixture"], preview["kickoff"], str(i), preview["opens"], preview["deadline"],
                     "sealed", 1, preview["attempted"], preview["error"], preview["sealed"],
                     preview["digest"], preview["payload"]))
                db.execute("INSERT INTO domestic_scores VALUES(?,?,'news_update',?,?,?,?)",
                    (result["fixture"], result["kickoff"], str(i), result["result_at"],
                     result["result_sha"], result["payload"]))
            # Simulate a read-only deployed database before the release first opens it.
            for table in ("domestic_evidence_changes", "domestic_change_cursor", "domestic_result_heads"):
                db.execute("DROP TABLE " + table)
            db.execute("DELETE FROM runtime_schema_versions WHERE component='domestic-forecast-editions-v1'")
        report = self.details()
        self.assertEqual(report["score_revisions"], 5)
        self.assertEqual(report["scored_samples_by_horizon"], {"preview": 1, "T-24h": 1})
        self.assertEqual(len(report["fixture_results"]), 5)
        self.assertEqual(report["evidence_changes"], 0)
        self.tick()
        self.assertEqual(self.details()["fixture_evaluation"], report["fixture_evaluation"])
        self.assertEqual(self.details()["score_revisions"], 5)

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

    def test_fixture_binding_and_input_failure_do_not_block_healthy_fixed_window(self):
        bad = self.match("Borussia Dortmund", "1. FC Union Berlin", self.kickoff)
        gateway = self.inputs(self.root, "bun", None, None)
        gateway.upcoming = (bad, self.fixture)
        gateway.fixtures += (bad,)
        bindings = gateway.provenance["market_fixtures"]
        binding = bindings[self.fixture.fixture_id]
        with patch.object(scheduler, "_load", return_value=gateway):
            partial = self.tick()["leagues"]["bun"]
            self.assertEqual(partial["state"], "partial")
            self.assertEqual(partial["fixture_errors"][0]["stage"], "schedule")
            self.assertEqual(self.details()["dispatch"]["T-24h"], {"sealed": 1})
            old_digest = self.details()["eligible_upcoming"][0]["latest_seal"]["digest"]
            bindings[bad.fixture_id] = dict(binding, slug="bun-bvb-fcu", event_id="22", home_id="12", away_id="13")
            def faulty_snapshot(root, data, now):
                if data["fixture_id"] == bad.fixture_id:
                    raise TypeError("broken identity for one fixture")
                return self.news(root, data, now)
            with patch.object(scheduler, "_news", side_effect=faulty_snapshot):
                partial = self.tick()["leagues"]["bun"]
            self.assertEqual(partial["fixture_errors"][0]["stage"], "inputs")
            self.assertEqual(self.tick()["leagues"]["bun"]["state"], "ok")
            report = self.details()
        self.assertEqual(report["dispatch"]["T-24h"], {"sealed": 2})
        self.assertIn(old_digest, [f["latest_seal"]["digest"] for f in report["eligible_upcoming"]])

    def test_unexpected_roster_failure_is_visible_and_does_not_block_player_collection(self):
        from polymarket861 import league_rosters, league_player_status
        self.tick()
        with patch.object(league_rosters, "collect_fixture_rosters", side_effect=TypeError("unknown structure")), \
             patch.object(league_player_status, "collect") as other, self.assertLogs(scheduler.__name__, "WARNING"):
            count, error = scheduler._poll_batch(self.root, "bun")
        self.assertEqual(count, 1)
        self.assertIn("rosters: TypeError: unknown structure", error)
        other.assert_called_once()
        report = self.details()
        self.assertEqual(report["news_polls"], {"retry": 1})
        self.assertEqual(report["news_poll_failures"][0]["fixture"], self.fixture.fixture_id)
        self.assertEqual(report["news_poll_failures"][0]["due"], store.iso(self.clock + timedelta(minutes=5)))
        self.clock += timedelta(minutes=5)
        with patch.object(league_rosters, "collect_fixture_rosters"), patch.object(league_player_status, "collect"):
            self.assertEqual(scheduler._poll_batch(self.root, "bun"), (1, None))
        self.assertEqual(self.details()["news_poll_failures"], [])

    def test_shared_input_outage_closes_known_window_without_a_late_seal(self):
        self.tick()
        self.clock = self.kickoff - timedelta(minutes=63)
        with patch.object(scheduler, "_load", side_effect=OSError("shared archive unavailable")):
            self.assertEqual(self.tick()["leagues"]["bun"]["state"], "retry")
            self.clock = self.kickoff - timedelta(minutes=60)
            self.tick()
        self.assertEqual(self.details()["dispatch"]["T-60m"], {"missed": 1})
        self.tick()
        rows = [r for r in self.details()["fixture_dispatch"] if r["horizon"] == "T-60m"]
        self.assertEqual([(r["state"], r["sealed"]) for r in rows], [("missed", None)])
