import json
import tempfile
import unittest
from concurrent.futures import wait
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import test_schedule_consensus as native
from polymarket861 import (
    datasets,
    football,
    news,
    news_events,
    pipeline,
    runner,
    season,
    season_readiness,
    store,
)
from polymarket861 import schedule_consensus as schedules
from release_fixture import approved
from test_recipe_upgrade import old_build
from test_runner import clock


class SchedulePrerequisites(unittest.TestCase):
    def setUp(self):
        self.case = native.ProspectiveSchedule()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root
        self.poller = Mock(side_effect=ConnectionError("test publisher unavailable"))

    def collect(self):
        return news.sync(
            self.root,
            season=native.SEASON,
            poller=self.poller,
            defaults=lambda team, season: [{"id": "test-feed", "kind": "feed"}],
            roster_refresh=lambda *args: {"status": "cached"},
            roster_reader=lambda *args: [],
            extractor=lambda text, roster: [],
        )

    def test_news_waits_for_both_archives_and_the_applied_kickoff(self):
        initial = self.collect()
        self.assertEqual(initial["state"], "awaiting_schedule")
        self.assertEqual(
            initial["schedule_readiness"]["blocked_fixtures"], [native.FID]
        )
        self.case.ol()
        self.assertEqual(self.collect()["state"], "awaiting_schedule")
        self.case.gamma()
        agreed = self.collect()["schedule_readiness"]["fixtures"][0]
        self.assertEqual(agreed["kickoff"], store.iso(native.OLD))
        self.assertEqual(agreed["openligadb"]["kickoff"], store.iso(native.NEW))
        self.assertIn("not been applied", agreed["reason"])
        self.poller.assert_not_called()
        self.assertEqual(schedules.sync(self.root, native.YEAR)["corrected"], 1)
        collected = self.collect()
        ready = collected["schedule_readiness"]
        self.assertEqual((ready["confirmed"], ready["blocked_fixtures"]), (1, []))
        current = ready["fixtures"][0]
        self.assertEqual(current["kickoff"], store.iso(native.NEW))
        self.assertEqual(
            current["openligadb"]["kickoff"], current["gamma"][0]["kickoff"]
        )
        self.assertEqual(collected["attempted"], 2)
        self.assertEqual(self.poller.call_count, 2)

    def test_unchanged_openfootball_time_also_requires_originals(self):
        self.case.ol(native.OLD)
        self.case.gamma(native.OLD)
        report = self.collect()["schedule_readiness"]
        self.assertEqual(report["fixtures"][0]["source"], "openfootball")
        self.assertEqual(report["fixtures"][0]["status"], "confirmed")
        self.assertEqual(self.case.counts(), 1)
        self.case.time += timedelta(hours=25)
        self.assertEqual(self.collect()["state"], "awaiting_schedule")

    def test_direct_event_refresh_cannot_publish_legacy_placeholder_work(self):
        with news_events._writer(self.root) as db:
            db.execute(
                "INSERT INTO news_event_work(fixture_id,kickoff,ticket) VALUES(?,?,1)",
                (native.FID, store.iso(native.OLD)),
            )
        updates = news_events.refresh(self.root)
        self.assertEqual(updates["schedule_blocked_fixtures"], [native.FID])
        self.assertEqual(updates["attempted"], 0)
        self.assertEqual(news_events.list_updates(self.root), [])

    def prepare_event(self):
        self.case.ol(native.OLD)
        self.case.gamma(native.OLD)
        with news_events._writer(self.root) as db:
            db.execute(
                "INSERT INTO news_event_work(fixture_id,kickoff,ticket) VALUES(?,?,1)",
                (native.FID, store.iso(native.OLD)),
            )

    def calculated_event(self, _root, task, _previous, _cache, _history):
        return {
            "fixture_id": task["fixture_id"],
            "kickoff": task["kickoff"],
            "cutoff": store.now_utc(),
            "schedule_version": task["version"],
            "state_sha256": store.digest(["new player report"]),
        }, store.iso(self.case.time + timedelta(hours=2))

    def test_new_native_disagreement_during_event_does_not_publish_or_consume_the_retry(
        self,
    ):
        self.prepare_event()
        self.case.time += timedelta(minutes=31)  # The next native round request is due.
        finished_fit = False
        open_schedule_db = schedules._db

        def interleave(*args):
            nonlocal finished_fit
            event = self.calculated_event(*args)
            self.case.time += timedelta(seconds=1)
            self.case.ol(native.OLD + timedelta(hours=1))
            finished_fit = True
            return event

        def no_second_writer(root):
            if finished_fit:
                self.fail("final news schedule validation opened a second database")
            return open_schedule_db(root)

        with (
            patch.object(news_events, "_report", side_effect=interleave),
            patch.object(schedules, "_db", side_effect=no_second_writer),
        ):
            blocked = news_events.refresh(self.root)
        self.assertEqual((blocked["published"], blocked["deferred"]), (0, 1))
        self.assertEqual(blocked["schedule_blocked_fixtures"], [native.FID])
        self.assertIn("disagree", blocked["items"][0]["reason"])
        self.assertEqual(self.case.counts(), 1)
        self.assertEqual(news_events.list_updates(self.root), [])
        with closing(store.connect(self.root)) as db:
            retry = db.execute(
                "SELECT retry_at FROM news_event_work WHERE fixture_id=?", (native.FID,)
            ).fetchone()[0]
        self.assertEqual(retry, store.now_utc())
        self.case.time += timedelta(minutes=31)
        self.case.ol(native.OLD)
        with patch.object(news_events, "_report", side_effect=self.calculated_event):
            recovered = news_events.refresh(self.root)
        self.assertEqual((recovered["published"], recovered["deferred"]), (1, 0))
        self.assertEqual(news_events.list_updates(self.root)[0]["schedule_version"], 1)

    def test_event_requires_missing_or_expired_confirmation_even_for_an_ordinary_b1_fixture(
        self,
    ):
        self.prepare_event()

        def remove_source(*args):
            event = self.calculated_event(*args)
            with closing(schedules._db(self.root)) as db, db:
                db.execute("DELETE FROM rf_native_requests")
            return event

        with patch.object(news_events, "_report", side_effect=remove_source):
            blocked = news_events.refresh(self.root)
        self.assertIn("missing native", blocked["items"][0]["reason"])
        state = schedules.readiness(self.root, fixture_ids={native.FID})["fixtures"][0]
        self.assertIsNone(state["fixed_b1_block_reason"])
        self.assertFalse(state["news_eligible"])
        self.case.time += timedelta(seconds=1)
        self.case.ol(native.OLD)
        recheck = news_events.dbscope.recheck_originals

        def expire_before_commit(originals):
            recheck(originals)
            self.case.time += timedelta(hours=25)

        with (
            patch.object(news_events, "_report", side_effect=self.calculated_event),
            patch.object(
                news_events.dbscope,
                "recheck_originals",
                side_effect=expire_before_commit,
            ),
        ):
            expired = news_events.refresh(self.root)
        self.assertIn("expired during publication", expired["items"][0]["reason"])
        self.assertEqual(news_events.list_updates(self.root), [])

    def test_fixed_b1_waits_for_bulk_placeholders_and_seals_only_the_corroborated_game(
        self,
    ):
        for n in range(9):
            home, away = f"Other Home {n}", f"Other Away {n}"
            store.register_fixture(
                self.root,
                {
                    "fixture_id": football.fixture_id(native.SEASON, home, away),
                    "season": native.SEASON,
                    "round_id": native.RND,
                    "home": home,
                    "away": away,
                    "kickoff": store.iso(native.OLD),
                    "known_at": store.now_utc(),
                    "source": "openfootball",
                    "locator": "test://season-file",
                },
            )
        # Early technical observations remain accessible.
        self.assertEqual(
            pipeline.make_forecast(self.root, native.FID, None, horizon="early")[
                "digest"
            ],
            self.case.previous["digest"],
        )
        self.case.time = native.OLD - timedelta(hours=24, minutes=2)
        with patch.object(
            schedules, "readiness", wraps=schedules.readiness
        ) as preflight:
            blocked = runner.run_due(self.root)
        self.assertEqual(preflight.call_count, 1)
        self.assertEqual(blocked["counts"], {"waiting_schedule": 10})
        self.assertTrue(
            all("bulk placeholder" in job["error"] for job in blocked["jobs"])
        )
        with self.assertRaisesRegex(ValueError, "bulk placeholder"):
            pipeline.make_forecast(self.root, native.FID, None, horizon="T-24h")
        self.case.time += timedelta(seconds=20)
        self.case.ol(native.OLD)
        self.case.gamma(native.OLD)
        with patch.object(
            schedules, "readiness", wraps=schedules.readiness
        ) as preflight:
            recovered = runner.run_due(self.root)
        # The seal validates the batch's evidence revision and original files.
        # An unchanged batch needs no second native archive scan under a writer.
        self.assertEqual(preflight.call_count, 1)
        self.assertIn(native.FID, preflight.call_args.kwargs["fixture_ids"])
        self.assertIsNone(preflight.call_args.kwargs["_connection"])
        self.assertEqual(recovered["counts"], {"done": 1, "waiting_schedule": 9})
        with closing(store.connect(self.root)) as db:
            fixed = db.execute(
                "SELECT fixture_id FROM forecast_seals WHERE horizon='T-24h'"
            ).fetchall()
            self.assertEqual([r[0] for r in fixed], [native.FID])
        self.case.time = native.OLD - timedelta(hours=24) + timedelta(seconds=1)
        missed = runner.run_due(self.root)
        self.assertTrue(
            all("bulk placeholder" in job["error"] for job in missed["jobs"])
        )
        self.assertEqual(missed["counts"]["missed"], 9)

    def test_ordinary_file_registration_is_usable_but_new_conflict_during_t60_fit_blocks_commit(
        self,
    ):
        self.case.time = native.OLD - timedelta(hours=1, seconds=80)
        ordinary = self.collect()["schedule_readiness"]["fixtures"][0]
        self.assertIsNone(ordinary["fixed_b1_block_reason"])
        self.assertFalse(ordinary["bulk_placeholder"])
        forecast = pipeline._forecast_arms
        open_db = schedules._db
        fitting_finished = False

        def no_second_connection(root):
            if fitting_finished:
                self.fail(
                    "revalidation inside the sealing transaction opened another connection"
                )
            return open_db(root)

        def late_disagreement(*args, **kwargs):
            nonlocal fitting_finished
            self.case.ol(native.OLD)
            self.case.gamma(native.NEW)
            result = forecast(*args, **kwargs)
            fitting_finished = True
            return result

        with (
            patch.object(pipeline, "_forecast_arms", side_effect=late_disagreement),
            patch.object(schedules, "_db", side_effect=no_second_connection),
        ):
            with self.assertRaisesRegex(ValueError, "native kickoff conflicts"):
                pipeline.make_forecast(self.root, native.FID, None, horizon="T-60m")
        blocked = runner.run_due(self.root)
        self.assertEqual(blocked["counts"]["waiting_schedule"], 1)
        with closing(store.connect(self.root)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM forecast_seals WHERE horizon='T-60m'"
                ).fetchone()[0],
                0,
            )

    def test_named_approval_and_withdrawal_take_effect_without_another_sync(self):
        far = native.OLD + timedelta(days=3)
        self.case.ol(far)
        self.case.gamma(far)
        self.assertEqual(
            self.collect()["schedule_readiness"]["blocked_fixtures"], [native.FID]
        )
        schedules.review_schedule(
            self.root, native.YEAR, native.FID, far, "test reviewer", "move confirmed"
        )
        ready = self.collect()["schedule_readiness"]["fixtures"][0]
        self.assertTrue(ready["news_eligible"])
        self.assertEqual(ready["review"]["reviewer"], "test reviewer")
        self.case.time += timedelta(minutes=1)
        schedules.review_schedule(
            self.root,
            native.YEAR,
            native.FID,
            far,
            "test reviewer",
            "withdrawn",
            withdraw=True,
        )
        held = self.collect()["schedule_readiness"]["fixtures"][0]
        self.assertFalse(held["news_eligible"])
        self.assertEqual(held["review"]["action"], "withdrawn")


class SeasonReadiness(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_first_use_has_no_implicit_release_or_ready_empty_season(self):
        report = season_readiness.report(self.root, 2026)
        self.assertEqual(report["state"], "attention")
        self.assertEqual(report["schedule"]["state"], "unavailable")
        self.assertEqual(report["results"]["state"], "unavailable")
        self.assertIsNone(report["training"]["training_matches"])
        self.assertEqual(report["release"]["state"], "first_use")
        with closing(store.connect(self.root)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM model_recipes").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM builtin_model_releases").fetchone()[0],
                0,
            )

    def test_upgrade_alert_without_fixtures_and_independent_collection_after_failure(
        self,
    ):
        with old_build():
            pipeline.register_recipe(self.root, "B1", "B1")
        summary = runner.run_due(self.root)
        self.assertEqual(summary["jobs"], [])
        self.assertEqual(summary["release_readiness"]["state"], "approval_required")
        sequence = []

        def service(_root, name, _now, _options):
            sequence.append(name)
            if name == "catalog":
                raise OSError("test upstream unavailable")
            return {}

        coordinator = season.Coordinator(self.root, once=True)
        coordinator.services_started = {"forecast", "history", "settlements"}
        with (
            patch.object(season, "_service", side_effect=service),
            patch.object(season, "_plan"),
        ):
            try:
                for _ in range(5):
                    coordinator.tick()
                    if coordinator.futures:
                        wait(list(coordinator.futures), timeout=3)
            finally:
                coordinator.close()
        self.assertEqual(sequence, ["catalog", "results", "news"])
        with closing(season._db(self.root)) as db:
            warnings = [
                r[0]
                for r in db.execute(
                    "SELECT reason FROM season_alerts WHERE role='forecast'"
                )
            ]
            self.assertEqual(len(warnings), 1)
            self.assertIn("forecast release-baseline", warnings[0])
            self.assertIn("forecast release-approve", warnings[0])
            self.assertEqual(
                db.execute("SELECT count(*) FROM model_release_decisions").fetchone()[
                    0
                ],
                0,
            )
        version = approved(self.root)
        current = season_readiness.release(self.root)
        self.assertTrue(current["ready"])
        self.assertEqual(current["active_version"], version)
        self.assertEqual(current["approval"]["reviewer"], "release steward")

    def test_round_gaps_blocked_team_and_real_training_input_share_a_runtime(self):
        source = self.root / "input" / native.SEASON / "en.1.json"
        source.parent.mkdir(parents=True)
        games = [
            {
                "round": f"Matchday {i}",
                "date": day,
                "time": "15:00",
                "team1": "Home FC",
                "team2": f"Away {i}",
                "score": {"ft": [2, 0]} if i == 1 else {},
            }
            for i, day in enumerate(
                ("2026-10-01", "2026-10-08", "2026-10-15", "2026-11-27"), 1
            )
        ]
        source.write_text(json.dumps({"matches": games}))
        with clock("2026-11-25T10:00Z"):
            inputs = datasets.observe(
                self.root,
                source.parent.parent,
                revision="a" * 40,
                expected={native.SEASON: football.blob_sha(source.read_bytes())},
                register_season=native.SEASON,
            )
            report = season_readiness.report(self.root, native.YEAR)
        results = report["results"]
        self.assertEqual(
            [(r["round"], r["due"], r["received"]) for r in results["rounds"]],
            [(1, 1, 1), (2, 1, 0), (3, 1, 0), (4, 0, 0)],
        )
        upcoming = football.fixture_id(native.SEASON, "Home FC", "Away 4")
        self.assertEqual(results["next_round_blocked_fixtures"], [upcoming])
        self.assertEqual(results["next_round_teams"]["Home FC"]["overdue_count"], 2)
        self.assertEqual(report["training"]["training_matches"], 1)
        self.assertEqual(
            report["training"]["manifest_sha256"], inputs["manifest_digest"]
        )
        self.assertEqual(report["training"]["matches_by_season"], {native.SEASON: 1})
        self.assertEqual(report["schedule"]["blocked_fixtures"], [upcoming])
