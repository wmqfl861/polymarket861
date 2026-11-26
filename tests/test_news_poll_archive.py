import json
import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import patch

from polymarket861 import news, news_poll_archive, news_sources, operations, store
from polymarket861.market_provider import Reply
import test_news_ingest as prose_fixture
import test_news_wire_states as wire_fixture


class PollArchiveTests(unittest.TestCase):
    def wire(self):
        case = wire_fixture.WireStateTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_each_historical_cutoff_preserves_exact_observation_with_irregular_clocks(
        self,
    ):
        case = self.wire()
        expected = []
        for index in range(20):
            # This is the recorded supplier time. The later retrieval really
            # happened; it cannot make the supplier's clock more recent.
            published = store.iso(store.instant(case.now()) - news.POLL_INTERVAL)
            if index in (11, 12):
                case.advance(seconds=79)
            checked = case.run_wire([case.player], publisher_time=published)
            self.assertTrue(checked["fetch_complete"])
            snap = news.snapshot(case.root, "first", case.now())
            proof_id = snap["facts"][0]["last_seen_receipt"]
            with news._db(case.root) as db:
                proof = news_poll_archive.receipt(db, proof_id)
            expected.append((snap, checked, proof_id, proof))
            case.advance(minutes=20, seconds=7 + index)

        with news._db(case.root) as db:
            inventory = news_poll_archive.inventory(db)
            self.assertEqual(inventory["unchanged_check_runs"], 1)
            self.assertGreater(inventory["individually_replayable_checks_in_runs"], 8)
            self.assertEqual(
                db.execute("SELECT count(*) FROM news_check_history").fetchone()[0],
                len(expected),
            )
            span = db.execute("SELECT * FROM news_poll_runs").fetchone()
            samples = list(
                db.execute("SELECT * FROM news_poll_ticks ORDER BY checked_at")
            )
            self.assertEqual(
                (
                    span["first_checked_at"],
                    span["last_checked_at"],
                    span["observations"],
                ),
                (samples[0]["checked_at"], samples[-1]["checked_at"], len(samples)),
            )
            for snap, checked, proof_id, proof in expected:
                with self.subTest(time=snap["cutoff"]):
                    check = db.execute(
                        "SELECT detail FROM news_check_history WHERE checked_at=?",
                        (snap["cutoff"],),
                    ).fetchone()
                    self.assertEqual(checked, json.loads(check[0]))
                    self.assertEqual(news_poll_archive.receipt(db, proof_id), proof)
                    self.assertEqual(
                        news.snapshot(case.root, "first", snap["cutoff"]), snap
                    )

            earlier, later = expected[10][0], expected[11][0]
            between = store.iso(store.instant(earlier["cutoff"]) + news.POLL_INTERVAL)
            self.assertLess(between, later["cutoff"])
            before = news.snapshot(case.root, "first", between)
            self.assertEqual(before["facts"], earlier["facts"])
            self.assertEqual(before["coverage"], earlier["coverage"])
            with self.assertRaisesRegex(ValueError, "unknown news poll"):
                news_poll_archive.receipt(db, "1" * 64)
        shown = news.article(case.root, expected[0][0]["facts"][0]["original_sha256"])
        self.assertEqual(shown["standing_state"]["confirmations"], len(expected))
        self.assertEqual(
            news.status(case.root)["archive_counts"]["news_poll_proofs"], len(expected)
        )

        snap, _, ident, original = expected[11]
        backup = case.root.parent / "polls-backup"
        restored = case.root.parent / "polls-restored"
        operations.backup_state(case.root, backup, compress=True)
        operations.restore_state(backup, restored)
        with news._db(restored) as db:
            self.assertEqual(news_poll_archive.receipt(db, ident), original)
        self.assertEqual(news.snapshot(restored, "first", snap["cutoff"]), snap)

    def test_corrupt_folded_receipt_cannot_certify_coverage_with_or_without_players(self):
        for with_player in (True, False):
            with self.subTest(with_player=with_player):
                case = self.wire()
                cutoffs = []
                for _ in range(34):
                    case.run_wire([case.player] if with_player else [])
                    cutoffs.append(case.now())
                    case.advance()
                at = cutoffs[31]
                self.assertTrue(news.snapshot(case.root, "first", at)["coverage"]["home"]["study_qualified"])
                with news._db(case.root, write=True) as db:
                    tick = db.execute("SELECT id,receipt FROM news_poll_ticks WHERE checked_at=?", (at,)).fetchone()
                    db.execute("UPDATE news_poll_ticks SET response=zeroblob(32) WHERE id=?", (tick["id"],))
                    with self.assertRaisesRegex(ValueError, "SHA-256"):
                        news_poll_archive.receipt(db, tick["receipt"])
                side = news.snapshot(case.root, "first", at)["coverage"]["home"]
                self.assertFalse(side["study_qualified"])
                self.assertFalse(side["observed"])
                source = next(s for s in side["sources"] if s["id"] == news_sources.WIRE)
                self.assertEqual(source["status"], "invalid_poll_evidence")
                self.assertIsNone(source["source_observed_at"])
                self.assertFalse(source["fetch_complete"])
                for other in (cutoffs[30], cutoffs[32], cutoffs[33]):
                    self.assertTrue(news.snapshot(case.root, "first", other)["coverage"]["home"]["study_qualified"])

    def test_folded_wire_membership_must_match_the_response_states_and_article_editions(self):
        case = self.wire()
        for _ in range(15):
            case.run_wire([case.player])
            case.advance()
        with news._db(case.root, write=True) as db:
            tick = db.execute("SELECT run,receipt,checked_at FROM news_poll_ticks ORDER BY id LIMIT 1").fetchone()
            header = db.execute("SELECT members FROM news_poll_runs WHERE id=?", (tick["run"],)).fetchone()
            projection = db.execute("SELECT projection FROM news_wire_manifests WHERE id=?", header).fetchone()[0]
            # The projection is still genuine and the alternate membership pointer has a valid digest.
            # Silently omitting the article would otherwise turn a real injury into an empty observation.
            partial = store.digest([[], json.loads(projection)])
            db.execute("INSERT INTO news_wire_manifests VALUES (?,?,?)", (partial, "[]", projection))
            db.execute("UPDATE news_poll_runs SET members=? WHERE id=?", (partial, tick["run"]))
            self.assertEqual(news_poll_archive.receipt(db, tick["receipt"])["id"], tick["receipt"])
        side = news.snapshot(case.root, "first", tick["checked_at"])["coverage"]["home"]
        self.assertFalse(side["study_qualified"])
        self.assertFalse(side["observed"])

    def test_seal_kept_standalone_outage_and_return_start_separate_run(self):
        case = self.wire()
        for _ in range(12):
            case.run_wire([case.player])
            case.advance()
        # This check is captured by a fixed forecast before the next poll.
        case.run_wire([case.player])
        pinned_at = case.now()
        pinned = case.facts()[0]["last_seen_receipt"]
        with news._db(case.root, write=True) as db:
            db.execute(
                "INSERT INTO forecast_seals VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "first",
                    "2026-11-27T16:00Z",
                    pinned_at,
                    "T-60m",
                    "unit",
                    "unit",
                    "technical",
                    pinned_at,
                    "unit-test-seal",
                    store.canonical({"news_pair": {"input_cutoff": pinned_at}}),
                ),
            )
        for _ in range(2):
            case.advance()
            case.run_wire([case.player])
        previous = case.now()
        case.advance(hours=8)
        self.assertFalse(
            news.snapshot(case.root, "first", case.now())["coverage"]["home"][
                "observed"
            ]
        )
        self.assertEqual(case.facts()[0]["last_seen_at"], previous)
        for _ in range(10):
            case.run_wire([case.player])
            case.advance()
        with news._db(case.root) as db:
            self.assertEqual(news_poll_archive.inventory(db)["unchanged_check_runs"], 2)
            self.assertIsNotNone(
                db.execute(
                    "SELECT 1 FROM news_wire_polls WHERE receipt=?", (pinned,)
                ).fetchone()
            )
            self.assertIsNotNone(
                db.execute(
                    "SELECT 1 FROM news_checks WHERE checked_at=?", (pinned_at,)
                ).fetchone()
            )
            self.assertIsNotNone(
                db.execute(
                    "SELECT 1 FROM news_poll_proofs WHERE id=?", (pinned,)
                ).fetchone()
            )
        for _ in range(2):
            case.run_wire([case.player], meta={"license": "unsupported"})
            case.advance()
        self.assertFalse(
            news.snapshot(case.root, "first", case.now())["coverage"]["home"][
                "observed"
            ]
        )
        for _ in range(10):
            case.run_wire([case.player | {"chance_of_playing": 75}])
            case.advance()
        with news._db(case.root) as db:
            self.assertEqual(news_poll_archive.inventory(db)["unchanged_check_runs"], 3)
        self.assertEqual(case.facts(pinned_at)[0]["chance_of_playing"], 25)
        self.assertEqual(case.facts()[0]["chance_of_playing"], 75)

    def test_repeat_of_archived_exact_receipt_keeps_first_wire_clock_and_unique_proof(
        self,
    ):
        case = self.wire()
        for _ in range(15):
            case.run_wire([case.player])
            case.advance()
        with news._db(case.root) as db:
            tick = db.execute(
                "SELECT receipt,wire_order,checked_at FROM news_poll_ticks ORDER BY id LIMIT 1"
            ).fetchone()
        at = tick["checked_at"]
        original = case.facts(at)
        case.clock = store.instant(at)
        # Force the normal writer to see the identical request, response and
        # observation again in this test of a clock restore.
        with news._db(case.root, write=True) as db:
            db.execute("UPDATE news_streams SET next_at='',provider_after=''")
        case.run_wire([case.player])
        self.assertEqual(case.facts(at), original)
        with news._db(case.root) as db:
            rows = db.execute(
                "SELECT rowid,recorded_at FROM news_wire_history WHERE receipt=?",
                (tick["receipt"],),
            ).fetchall()
            self.assertEqual([tuple(r) for r in rows], [(tick["wire_order"], at)])
            self.assertEqual(
                db.execute("SELECT count(*) FROM news_receipt_history").fetchone()[0],
                15,
            )
        self.assertEqual(
            news.status(case.root)["archive_counts"]["news_poll_proofs"], 15
        )

    def test_non_wire_empty_bulletins_persist_their_individual_proofs_and_earlier_coverage(
        self,
    ):
        case = prose_fixture.NewsIngestTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.rows = []
        original = []
        for index in range(13):
            self.assertEqual(case.run_sync()["streams"][0]["state"], "ok")
            with news._db(case.root) as db:
                (ident,) = db.execute(
                    "SELECT last_receipt FROM news_poll_heads"
                ).fetchone()
                original.append((case.snap(), news_poll_archive.receipt(db, ident)))
            case.advance(sec=1200 + index)
        with news._db(case.root) as db:
            totals = news_poll_archive.inventory(db)
            self.assertGreater(totals["individually_replayable_checks_in_runs"], 0)
            self.assertEqual(totals["wire_confirmations_in_runs"], 0)
            for snap, proof in original:
                self.assertEqual(news_poll_archive.receipt(db, proof["id"]), proof)
                self.assertEqual(case.snap(snap["cutoff"]), snap)
            sample = db.execute("SELECT id,checked_at FROM news_poll_ticks LIMIT 1").fetchone()
            db.execute("UPDATE news_poll_ticks SET response=zeroblob(32) WHERE id=?", (sample["id"],))
        side = case.snap(sample["checked_at"])["coverage"]["home"]
        self.assertFalse(side["study_qualified"])
        self.assertFalse(side["observed"])

    def test_atomic_rollback_leaves_old_witnesses_readable_and_resumes_after_restart(
        self,
    ):
        case = self.wire()
        for _ in range(9):
            case.run_wire([case.player])
            case.advance()
        original = case.facts()
        with news._db(case.root) as db:
            count = db.execute("SELECT count(*) FROM news_poll_ticks").fetchone()[0]
            raw = db.execute(
                "SELECT * FROM news_checks ORDER BY id DESC LIMIT 1"
            ).fetchone()
            (ident,) = db.execute("SELECT last_receipt FROM news_poll_heads").fetchone()
            db.execute("""CREATE TRIGGER interrupt_test_compaction BEFORE DELETE ON news_checks BEGIN
                     SELECT RAISE(ABORT, 'test interruption'); END""")
        outcome = case.run_wire([case.player])
        self.assertEqual(outcome["state"], "error")
        self.assertIn("test interruption", outcome["issues"])
        with news._db(case.root) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM news_poll_ticks").fetchone()[0], count
            )
            self.assertEqual(
                dict(
                    db.execute(
                        "SELECT * FROM news_checks WHERE id=?", (raw["id"],)
                    ).fetchone()
                ),
                dict(raw),
            )
            self.assertEqual(news_poll_archive.receipt(db, ident)["id"], ident)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            db.execute("DROP TRIGGER interrupt_test_compaction")
        self.assertEqual(
            case.facts(raw["checked_at"])[0]["last_seen_receipt"],
            original[0]["last_seen_receipt"],
        )
        case.advance(hours=1)
        self.assertEqual(case.run_wire([case.player])["state"], "ok")
        self.assertEqual(case.facts()[0]["last_seen_at"], case.now())

    def test_older_raw_rows_reopen_without_writing_and_survive_first_fold_after_upgrade(
        self,
    ):
        case = self.wire()
        with patch.object(news_poll_archive, "track"):
            for _ in range(32):
                case.run_wire([case.player])
                case.advance()
        before = case.facts()
        with news._db(case.root) as db:
            for view in [
                "news_receipt_history",
                "news_wire_history",
                "news_check_history",
                "news_folded_observations",
            ]:
                db.execute("DROP VIEW " + view)
            for table in [
                "news_poll_heads",
                "news_poll_upgrade_heads",
                "news_poll_upgrade",
                "news_poll_ticks",
                "news_poll_runs",
            ]:
                db.execute("DROP TABLE " + table)
            db.execute(
                "DELETE FROM runtime_schema_versions WHERE component='team_news'"
            )
        self.assertEqual(case.facts(), before)
        with sqlite3.connect(
            f"file:{case.root}/runtime.sqlite3?mode=ro", uri=True
        ) as db:
            self.assertFalse(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='news_poll_runs'"
                ).fetchone()
            )
        # Old checks carried the body hash and observation time, but not always
        # the receipt ID. Competing original receipts must leave that row untouched.
        with news._db(case.root, write=True) as db:
            ident, payload = db.execute(
                "SELECT id,payload FROM news_poll_proofs ORDER BY rowid LIMIT 1 OFFSET 16"
            ).fetchone()
            other = json.loads(payload)
            other["requested_at"] = store.iso(
                store.instant(other["requested_at"]) - timedelta(microseconds=57)
            )
            db.execute(
                "INSERT INTO news_poll_proofs VALUES (?,?)",
                (store.digest(other), store.canonical(other)),
            )
        reports = []
        while not reports or not reports[-1]["complete"]:
            reports.append(
                news.retention(case.root, compact=True, limit=6)["pre_upgrade_checks"]
            )
        self.assertEqual(sum(r["examined"] for r in reports), 32)
        self.assertGreater(sum(r["folded"] for r in reports), 5)
        self.assertEqual(sum(r["unlinked_or_ambiguous"] for r in reports), 1)
        self.assertEqual(
            news.retention(case.root, compact=True, limit=6)["pre_upgrade_checks"][
                "examined"
            ],
            0,
        )
        with news._db(case.root) as db:
            self.assertIsNotNone(
                db.execute(
                    "SELECT 1 FROM news_poll_proofs WHERE id=?", (ident,)
                ).fetchone()
            )
        for _ in range(12):
            case.run_wire([case.player])
            case.advance()
        self.assertEqual(case.facts(before[0]["last_seen_at"]), before)
        with news._db(case.root) as db:
            self.assertGreater(
                news_poll_archive.inventory(db)[
                    "individually_replayable_checks_in_runs"
                ],
                0,
            )
            self.assertEqual(
                news_poll_archive.receipt(db, before[0]["last_seen_receipt"])[
                    "observed_at"
                ],
                before[0]["last_seen_at"],
            )

    def test_distinct_provider_request_response_and_local_clocks_roundtrip(self):
        case = self.wire()
        witnesses = []
        original_extract = news._extract_pending

        def finish_later(*args):
            case.clock += timedelta(milliseconds=73)
            return original_extract(*args)

        def poll(source, team, season, cursor):
            requested = case.now()
            case.clock += timedelta(milliseconds=212)
            received = case.now()
            provider = store.iso(store.instant(requested) - timedelta(seconds=31))
            raw = json.dumps(
                wire_fixture.availability([case.player], provider)
            ).encode()
            case.clock += timedelta(milliseconds=108)
            return news_sources.poll(
                source,
                team,
                season,
                cursor,
                getter=lambda url: Reply(raw, requested, received, {}, url),
            )

        with patch.object(news, "_extract_pending", side_effect=finish_later):
            for _ in range(15):
                out = news.sync(
                    case.root,
                    team=case.team,
                    defaults=lambda *_: [case.source],
                    poller=poll,
                    roster_refresh=lambda *_: {"status": "cached"},
                )
                check = out["streams"][0]
                self.assertEqual(check["state"], "ok")
                with news._db(case.root) as db:
                    row = db.execute(
                        "SELECT receipt,observed_at,provider_at,recorded_at FROM news_wire_polls ORDER BY rowid DESC LIMIT 1"
                    ).fetchone()
                    proof = news_poll_archive.receipt(db, row["receipt"])
                self.assertLess(proof["provider_state_as_of"], proof["requested_at"])
                self.assertLess(proof["requested_at"], proof["received_at"])
                self.assertLess(proof["received_at"], proof["observed_at"])
                self.assertLess(proof["observed_at"], check["checked_at"])
                witnesses.append((check, dict(row), proof))
                case.advance()
        with news._db(case.root) as db:
            self.assertGreater(
                news_poll_archive.inventory(db)[
                    "individually_replayable_checks_in_runs"
                ],
                3,
            )
            for check, sample, proof in witnesses:
                self.assertEqual(
                    dict(
                        db.execute(
                            "SELECT receipt,observed_at,provider_at,recorded_at FROM news_wire_history WHERE receipt=?",
                            (sample["receipt"],),
                        ).fetchone()
                    ),
                    sample,
                )
                self.assertEqual(
                    news_poll_archive.receipt(db, sample["receipt"]), proof
                )
                self.assertEqual(
                    json.loads(
                        db.execute(
                            "SELECT detail FROM news_check_history WHERE checked_at=?",
                            (check["checked_at"],),
                        ).fetchone()[0]
                    ),
                    check,
                )
