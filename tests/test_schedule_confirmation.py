"""Fixed EPL kickoff evidence persists by registration, while news checks stay current."""

import json
import unittest
from concurrent.futures import Future
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

import test_schedule_consensus as native
from polymarket861 import (
    football,
    market_archive,
    market_provider,
    pipeline,
    runner,
    schedule_consensus as schedules,
    season,
    season_readiness,
    store,
)


class ConfirmedKickoffTests(unittest.TestCase):
    def setUp(self):
        self.feed = native.ProspectiveSchedule()
        self.feed.setUp()
        self.addCleanup(self.feed.doCleanups)
        self.root = self.feed.root
        for n in range(9):
            home, away = f"Home {n}", f"Away {n}"
            store.register_fixture(
                self.root,
                {
                    "fixture_id": football.fixture_id(native.SEASON, home, away),
                    "season": native.SEASON,
                    "home": home,
                    "away": away,
                    "round_id": native.RND,
                    "kickoff": store.iso(native.OLD),
                    "known_at": store.now_utc(),
                    "source": "openfootball",
                    "locator": "test://schedule-file",
                },
            )

    def inspect(self, at=None):
        return schedules.readiness(self.root, fixture_ids={native.FID}, at=at)[
            "fixtures"
        ][0]

    def record_agreement(self):
        self.feed.ol(native.OLD)
        self.feed.gamma(native.OLD)
        row = self.inspect()
        self.assertTrue(row["bulk_placeholder"])
        self.assertEqual(row["fixed_confirmation"]["accepted_at"], store.now_utc())
        return row["fixed_confirmation"]

    def test_confirms_once_and_survives_silence_but_not_one_sided_disagreement_or_withdrawal(
        self,
    ):
        original_count = self.feed.counts()
        self.feed.ol(native.OLD)
        self.assertIsNone(self.inspect()["fixed_confirmation"])
        self.feed.time += timedelta(seconds=2)
        self.feed.gamma(native.OLD)
        prior = store.now_utc()
        self.feed.time += timedelta(seconds=2)
        self.assertIsNone(self.inspect(prior)["fixed_confirmation"])
        accepted = self.inspect()["fixed_confirmation"]
        self.assertIsNone(self.inspect(prior)["fixed_confirmation"])
        self.feed.time += timedelta(hours=25)
        schedules.sync(self.root, native.YEAR)
        current = self.inspect()
        self.assertEqual(current["fixed_confirmation"], accepted)
        self.assertIsNone(current["fixed_b1_block_reason"])
        self.assertFalse(current["news_eligible"])
        self.feed.ol(native.OLD + timedelta(minutes=30))
        self.assertIn("disagree", self.inspect()["fixed_b1_block_reason"])
        schedules.sync(self.root, native.YEAR)
        self.feed.time += timedelta(hours=25)
        self.assertTrue(self.inspect()["fixed_b1_block_reason"])
        self.feed.ol(native.OLD)
        self.feed.gamma(native.OLD)
        schedules.sync(self.root, native.YEAR)
        self.assertIsNone(self.inspect()["fixed_b1_block_reason"])
        schedules.review_schedule(
            self.root,
            native.YEAR,
            native.FID,
            native.OLD,
            "operator",
            "fixture under review",
            withdraw=True,
        )
        self.assertIn("withdrawn", self.inspect()["fixed_b1_block_reason"])
        self.feed.time += timedelta(seconds=1)
        schedules.review_schedule(
            self.root,
            native.YEAR,
            native.FID,
            native.OLD,
            "operator",
            "two current originals verified",
        )
        self.assertIsNone(self.inspect()["fixed_b1_block_reason"])
        self.assertEqual(self.feed.counts(), original_count)
        with closing(schedules._db(self.root)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM epl_schedule_proofs WHERE fixture=?",
                    (native.FID,),
                ).fetchone()[0],
                1,
            )

    def test_original_tampering_and_later_ineligible_gamma_block_at_the_decision(self):
        confirmation = self.record_agreement()
        self.feed.time += timedelta(hours=25)
        self.feed.gamma(native.OLD, period=False)
        self.assertIn("Gamma", self.inspect()["fixed_b1_block_reason"])
        self.feed.time += timedelta(seconds=1)
        self.feed.gamma(native.OLD)
        self.assertIsNone(self.inspect()["fixed_b1_block_reason"])
        original = store.blob_path(self.root, confirmation["openligadb_sha256"])
        original.write_bytes(b"changed")
        self.assertIn("archive", self.inspect()["fixed_b1_block_reason"])

    def test_superseding_registration_needs_its_own_confirmation(self):
        accepted = self.record_agreement()
        self.feed.time += timedelta(minutes=31)
        self.feed.ol(native.NEW)
        self.feed.gamma(native.NEW)
        self.assertTrue(self.inspect()["fixed_b1_block_reason"])
        # The old event remains in the catalogue; the competing day cannot be silently admitted.
        self.assertEqual(schedules.sync(self.root, native.YEAR)["corrected"], 0)
        old = self.inspect(accepted["accepted_at"])
        self.assertEqual(old["fixed_confirmation"], accepted)
        self.assertIsNone(old["fixed_b1_block_reason"])
        self.feed.time += timedelta(seconds=1)
        store.register_fixture(
            self.root,
            {
                "fixture_id": native.FID,
                "season": native.SEASON,
                "round_id": native.RND,
                "home": old["home"],
                "away": old["away"],
                "kickoff": store.iso(native.NEW),
                "known_at": store.now_utc(),
                "source": "operator",
                "locator": "test://new-registration",
            },
        )
        newer = self.inspect()
        self.assertEqual(newer["version"], old["version"] + 1)
        self.assertIsNone(newer["fixed_confirmation"])
        self.assertTrue(newer["fixed_b1_block_reason"])
        self.assertEqual(
            self.inspect(accepted["accepted_at"])["fixed_confirmation"], accepted
        )

    def test_contradiction_invalidates_a_precomputed_fixed_batch_under_the_writer_lock(
        self,
    ):
        self.record_agreement()
        self.feed.time += timedelta(hours=25)
        checked = schedules.fixed_schedules(self.root, {native.FID}, store.now_utc())
        self.assertIsNone(checked.fixtures[native.FID]["fixed_b1_block_reason"])
        self.feed.time += timedelta(seconds=1)
        self.feed.ol(native.OLD + timedelta(minutes=30))
        with closing(schedules._db(self.root)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "disagree"):
                schedules.require_fixed(
                    self.root,
                    checked.fixtures[native.FID],
                    store.now_utc(),
                    checked,
                    db=db,
                )

    def test_locked_seal_reopens_both_receipts_and_origin_when_preflight_index_has_not_changed(
        self,
    ):
        proof = self.record_agreement()
        self.feed.time += timedelta(hours=25)
        at = store.now_utc()
        queries = {
            "missing gamma request": (
                "DELETE FROM ma_requests WHERE sha=?",
                proof["gamma_sha256"],
            ),
            "edited gamma request": (
                "UPDATE ma_requests SET status='partial' WHERE sha=?",
                proof["gamma_sha256"],
            ),
            "edited gamma observation": (
                "UPDATE observations SET metadata='{}' WHERE sha256=? AND source='polymarket.com'",
                proof["gamma_sha256"],
            ),
            "missing OpenLigaDB observation": (
                "DELETE FROM observations WHERE source='openligadb-results' AND sha256=?",
                proof["openligadb_sha256"],
            ),
            "edited OpenLigaDB origin": (
                "UPDATE rf_native_requests SET url='test://wrong-matchweek' WHERE sha=?",
                proof["openligadb_sha256"],
            ),
            "missing registration receipt": (
                "DELETE FROM schedule_receipts WHERE fixture_id=?",
                native.FID,
            ),
            "edited registration precision": (
                "UPDATE schedule_receipts SET precision='date' WHERE fixture_id=?",
                native.FID,
            ),
            "edited file registration": (
                "UPDATE fixture_versions SET locator='test://unverified' WHERE fixture_id=?",
                native.FID,
            ),
        }
        for example, (sql, value) in queries.items():
            with self.subTest(example=example), closing(schedules._db(self.root)) as db:
                batch = schedules.fixed_schedules(self.root, {native.FID}, at)
                fixture = batch.fixtures[native.FID]
                self.assertIsNone(fixture["fixed_b1_block_reason"])
                db.execute("BEGIN IMMEDIATE")
                self.assertGreater(db.execute(sql, (value,)).rowcount, 0)
                self.assertEqual(schedules._frontier(db), batch.frontier)
                with self.assertRaises(ValueError):
                    schedules.require_fixed(self.root, fixture, at, batch, db=db)
                db.rollback()

    def test_physical_original_lost_during_fixed_seal_rolls_back_report_and_forecast(
        self,
    ):
        proof = self.record_agreement()
        self.feed.time = native.OLD - timedelta(hours=24, minutes=2)
        put = pipeline.put_report

        def swap_archive(db, kind, key, data):
            put(db, kind, key, data)
            if kind == "match" and data["horizon"] == "T-24h":
                store.blob_path(self.root, proof["gamma_sha256"]).write_bytes(
                    b"changed during seal"
                )

        with patch.object(pipeline, "put_report", side_effect=swap_archive):
            with self.assertRaisesRegex(ValueError, "original.*changed"):
                pipeline.make_forecast(self.root, native.FID, None, horizon="T-24h")
        with closing(store.connect(self.root)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM forecast_seals WHERE fixture_id=? AND horizon='T-24h'",
                    (native.FID,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM reports WHERE kind='match' AND json_extract(payload,'$.horizon')='T-24h'"
                ).fetchone()[0],
                0,
            )

    def test_unindexed_newer_direct_gamma_or_orphan_original_blocks_the_old_time(self):
        proof = self.record_agreement()
        self.feed.time += timedelta(minutes=31)
        slug = self.feed.gamma(native.OLD + timedelta(minutes=30))
        self.assertTrue(self.inspect()["fixed_b1_block_reason"])
        with closing(schedules._db(self.root)) as db, db:
            self.assertEqual(
                db.execute(
                    "DELETE FROM ma_events WHERE slug=? AND received_at=?",
                    (slug, store.now_utc()),
                ).rowcount,
                1,
            )
        self.assertTrue(self.inspect()["fixed_b1_block_reason"])
        with closing(schedules._db(self.root)) as db, db:
            self.assertEqual(
                db.execute(
                    "DELETE FROM ma_requests WHERE operation='event' AND selector=? AND received_at=?",
                    (slug, store.now_utc()),
                ).rowcount,
                1,
            )
        self.assertTrue(self.inspect()["fixed_b1_block_reason"])
        self.assertIsNone(self.inspect(proof["accepted_at"])["fixed_b1_block_reason"])
        self.feed.time += timedelta(hours=25)
        self.assertTrue(self.inspect()["fixed_b1_block_reason"])
        self.feed.gamma(native.OLD)
        self.assertIsNone(self.inspect()["fixed_b1_block_reason"])

    def test_unindexed_catalogue_member_and_alternate_date_do_not_resurrect_independent_proof(
        self,
    ):
        proof = self.record_agreement()
        original = json.loads(store.read_blob(self.root, proof["gamma_sha256"]))
        self.feed.time += timedelta(minutes=31)

        def capture_page(entries):
            url = market_provider.GAMMA + "/events"
            with patch.object(
                market_provider,
                "get",
                return_value=native.ex.reply(entries, self.feed.time, url),
            ):
                # A crash after this normal collector entry point and before
                # _ingest leaves an original+request pair without a member index.
                _, _, sha = market_archive._fetch(
                    self.root, "catalog", "fixture-test-page", url
                )
                return sha

        capture_page([{"slug": "an-unrelated-event"}])
        self.assertIsNone(self.inspect()["fixed_b1_block_reason"])
        self.feed.time += timedelta(seconds=1)
        data = dict(original, startTime=store.iso(native.OLD + timedelta(minutes=30)))
        sha = capture_page([{"slug": "a-different-unrelated-event"}, data])
        self.assertTrue(self.inspect()["fixed_b1_block_reason"])
        with closing(schedules._db(self.root)) as db, db:
            self.assertEqual(
                db.execute(
                    "DELETE FROM ma_requests WHERE operation='catalog' AND sha=?",
                    (sha,),
                ).rowcount,
                1,
            )
        self.assertTrue(self.inspect()["fixed_b1_block_reason"])
        self.feed.time += timedelta(seconds=1)
        self.feed.gamma(native.OLD)
        self.assertIsNone(self.inspect()["fixed_b1_block_reason"])
        self.feed.time += timedelta(seconds=1)
        later = native.OLD + timedelta(days=1)
        capture_page(
            [
                dict(
                    original,
                    slug=original["slug"][:-10] + store.iso(later)[:10],
                    startTime=store.iso(later),
                )
            ]
        )
        self.assertTrue(self.inspect()["fixed_b1_block_reason"])
        self.assertIsNone(self.inspect(proof["accepted_at"])["fixed_b1_block_reason"])

    def test_initial_confirmation_requires_latest_native_catalogue_to_be_reconciled(
        self,
    ):
        self.feed.ol(native.OLD)
        slug = self.feed.gamma(native.OLD)
        self.feed.time += timedelta(seconds=1)
        self.feed.gamma(native.OLD + timedelta(hours=1))
        with closing(schedules._db(self.root)) as db, db:
            db.execute(
                "DELETE FROM ma_events WHERE slug=? AND received_at=?",
                (slug, store.now_utc()),
            )
        row = self.inspect()
        self.assertIsNone(row["fixed_confirmation"])
        self.assertTrue(row["fixed_b1_block_reason"])
        with closing(schedules._db(self.root)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM epl_schedule_proofs WHERE fixture=?",
                    (native.FID,),
                ).fetchone()[0],
                0,
            )

    def test_preflight_reports_before_open_dedupes_after_restart_and_clears_when_confirmed(
        self,
    ):
        cutoff = native.OLD - timedelta(hours=24)
        self.feed.time = cutoff - timedelta(hours=15)
        self.assertEqual(runner.run_due(self.root)["schedule_preflight"]["blocked"], [])
        self.feed.time = cutoff - timedelta(hours=7)
        state = runner.run_due(self.root)
        warnings = state["schedule_preflight"]["blocked"]
        self.assertEqual(len(warnings), 10)
        first = next(s for s in warnings if s["fixture_id"] == native.FID)
        self.assertEqual(first["opens"], store.iso(cutoff - timedelta(minutes=5)))
        self.assertEqual(
            first["readiness_command"], "season readiness --year 2026 --round 12"
        )
        self.assertTrue(first["schedule_reason"])
        self.assertFalse(state["jobs"])
        for _ in range(2):
            coordinator = season.Coordinator(self.root)
            try:
                with closing(season._db(self.root)) as db, db:
                    db.execute(
                        "INSERT OR REPLACE INTO season_services(service,owner,next_at) VALUES('forecast',?,?)",
                        (coordinator.identity, store.now_utc()),
                    )
                ready = Future()
                ready.set_result(state)
                coordinator._finish(ready, "service", {"service": "forecast"})
            finally:
                coordinator.close()
        with closing(season._db(self.root)) as db:
            saved = db.execute(
                "SELECT reason FROM season_alerts WHERE fixture=? AND role='forecast_schedule_preflight'",
                (native.FID,),
            ).fetchall()
            self.assertEqual(len(saved), 1)
            self.assertIn("season readiness", saved[0][0])
            self.assertEqual(
                db.execute("SELECT count(*) FROM forecast_dispatch").fetchone()[0], 0
            )
            self.assertEqual(
                len(
                    json.loads(
                        db.execute(
                            "SELECT result FROM season_services WHERE service='forecast'"
                        ).fetchone()[0]
                    )["summary"]["schedule_preflight"]["blocked"]
                ),
                10,
            )
        self.record_agreement()
        self.assertNotIn(
            native.FID,
            {
                w["fixture_id"]
                for w in runner.run_due(self.root)["schedule_preflight"]["blocked"]
            },
        )

    def test_operator_readiness_reports_next_actionable_clock_and_local_heartbeat(self):
        self.record_agreement()
        self.feed.time = native.OLD - timedelta(hours=23)
        report = season_readiness.report(self.root, native.YEAR, round_id=native.RND)
        self.assertLess(report["first_registered_window"]["cutoff"], report["as_of"])
        self.assertEqual(report["next_registered_window"]["horizon"], "T-60m")
        self.assertEqual(report["coordinator"]["state"], "unobserved")
        with closing(season._db(self.root)) as db, db:
            db.execute(
                "INSERT INTO season_services(service,next_at,updated) VALUES('heartbeat',?,?)",
                (store.now_utc(), store.now_utc()),
            )
        self.assertEqual(
            season_readiness.report(self.root, native.YEAR, native.RND)["coordinator"][
                "state"
            ],
            "current",
        )
        self.feed.time += timedelta(seconds=91)
        self.assertEqual(
            season_readiness.report(self.root, native.YEAR, native.RND)["coordinator"][
                "state"
            ],
            "stale",
        )
