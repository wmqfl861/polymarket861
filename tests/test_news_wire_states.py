import gzip
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket861 import news, news_sources, operations, store
from polymarket861.market_provider import Reply
from test_news_ingest import registered_squad
from test_news_sources import availability, feed, wire_row


class WireStateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "runtime"
        self.clock = store.instant("2026-11-25T12:00Z")
        clock = patch.object(store, "now_utc", side_effect=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.team = "Chelsea FC"
        self.season = "2026-27"
        self.source = news_sources.default_sources(self.team, self.season)[0]
        for game, opponent, kickoff in (("first", "Forest FC", "2026-11-27T16:00Z"),
                                        ("next", "Villa FC", "2026-11-30T16:00Z")):
            store.register_fixture(self.root, {"fixture_id": game, "home": self.team, "away": opponent,
                "known_at": self.now(), "kickoff": kickoff, "season": self.season, "round_id": 12,
                "source": "calendar", "locator": "schedule:" + game})
        registered_squad(self.root, self.team, self.season)
        self.player = wire_row() | {"display_name": "Morgan Smith", "status": "injured", "chance_of_playing": 25}

    def now(self):
        return store.iso(self.clock)

    def advance(self, **delta):
        self.clock += timedelta(**(delta or {"minutes": 20}))
        return self.now()

    def run_wire(self, rows, *, meta=None, publisher_time=None, legacy=False, labeler=None):
        stamp = self.now()
        data = availability(rows, publisher_time or stamp)
        data["meta"].update(meta or {})
        raw = json.dumps(data).encode()

        def poll(source, team, season, cursor):
            if source["id"] != news_sources.WIRE:
                return {"articles": [], "cursor": {}}
            result = news_sources.poll(source, team, season, cursor, getter=lambda url: Reply(raw, stamp, stamp, {}, url))
            if legacy:
                # 0.4.0 archived a version under each moving provider timestamp.
                result["articles"] = result.pop("wire_observations")
                result.pop("wire_rejections")
            return result

        def no_language_model(*args, **kwargs):
            self.fail("a typed availability row reached a language model")

        out = news.sync(self.root, team=self.team, force=True, defaults=lambda *_: [self.source],
                        poller=poll, roster_refresh=lambda *_: {"status": "cached"},
                        extractor=labeler or no_language_model)
        return next(s for s in out["streams"] if s["source"] == news_sources.WIRE)

    def facts(self, at=None, fixture="first"):
        return news.snapshot(self.root, fixture, at or self.now())["facts"]

    def count(self, table):
        with news._db(self.root) as db:
            return db.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def test_unchanged_receipts_have_no_new_archive_or_claim_and_replay_their_own_cutoff(self):
        old = self.now()
        self.assertEqual(self.run_wire([self.player])["state"], "ok")
        first, = self.facts()
        self.assertEqual((first["event"], first["status"], first["availability"], first["chance_of_playing"]),
                         ("injury", "reported", "doubtful", 25))
        self.assertEqual(first["extractor_backend"], "structured_wire")
        for _ in range(4):
            self.advance()
            check = self.run_wire([self.player])
            self.assertEqual((check["state"], check["articles_added"], check["claims_added"]), ("ok", 0, 0))
        latest, = self.facts()
        self.assertEqual((latest["id"], latest["first_seen_at"], latest["last_seen_at"]), (first["id"], old, self.now()))
        self.assertNotEqual(latest["last_seen_receipt"], first["last_seen_receipt"])
        self.assertEqual(self.facts(old)[0]["last_seen_receipt"], first["last_seen_receipt"])
        for table in ("news_articles", "news_claims", "news_extractions", "news_analysis_runs"):
            self.assertEqual(self.count(table), 1, table)
        self.assertEqual(self.count("news_poll_proofs"), 5)
        self.assertEqual(self.count("news_wire_polls"), 5)
        self.assertEqual(self.count("news_wire_manifests"), 1)
        self.assertEqual(news.article(self.root, first["original_sha256"])["standing_state"]["confirmations"], 5)

    def test_changed_probability_retains_absence_age_but_withdrawal_and_reappearance_reset_it(self):
        anchors = []
        for chance in (25, 75, 25):
            anchors.append(self.now())
            self.assertEqual(self.run_wire([self.player | {"chance_of_playing": chance}])["articles_added"], 1)
            fact, = self.facts()
            self.assertEqual((fact["first_seen_at"], fact["absence_first_seen_at"], fact["chance_of_playing"]),
                             (self.now(), anchors[0], chance))
            self.advance()
        withdrawn = self.now()
        self.assertEqual(self.run_wire([])["articles_added"], 1)
        self.assertEqual(self.facts(), [])
        self.advance()
        self.assertEqual(self.run_wire([self.player])["articles_added"], 1)
        again, = self.facts()
        self.assertEqual((again["first_seen_at"], again["absence_first_seen_at"]), (self.now(), self.now()))
        self.assertEqual(self.facts(withdrawn), [])
        self.assertEqual([self.facts(t)[0]["chance_of_playing"] for t in anchors], [25, 75, 25])

    def test_new_fixture_needs_a_new_witness_and_retains_the_injury_onset(self):
        origin = self.now()
        self.run_wire([self.player])
        self.assertFalse(self.facts(fixture="next"))
        self.clock = store.instant("2026-11-27T18:00Z")
        self.assertEqual(self.run_wire([self.player])["articles_added"], 1)
        later, = self.facts(fixture="next")
        self.assertEqual((later["fixture_id"], later["first_seen_at"], later["last_seen_at"]),
                         ("next", origin, self.now()))
        self.assertEqual(self.facts(origin)[0]["last_seen_at"], origin)

    def test_cache_from_before_one_match_cannot_cover_the_following_fixture(self):
        self.clock = store.instant("2026-11-27T15:45Z")
        prior = self.now()
        self.run_wire([self.player])
        self.advance(minutes=30)
        self.run_wire([self.player], publisher_time=prior)
        snapshot = news.snapshot(self.root, "next", self.now())
        self.assertFalse(snapshot["facts"])
        self.assertFalse(snapshot["coverage"]["home"]["observed"])
        self.assertEqual(snapshot["coverage"]["home"]["sources"][0]["status"], "different_next_fixture")
        self.advance()
        self.assertEqual(self.run_wire([self.player])["articles_added"], 1)
        self.assertTrue(news.snapshot(self.root, "next", self.now())["coverage"]["home"]["observed"])

    def test_unknown_rows_and_names_degrade_coverage_without_quarantining_the_club(self):
        stranger = self.player | {"player_id": 8, "display_name": "Unknown Member"}
        bad = self.player | {"player_id": 9, "status": "resting", "news": "third-party secret"}
        for _ in range(2):
            stream = self.run_wire([self.player, bad, stranger])
            self.assertEqual((stream["state"], stream["rejected_items"], stream["unmatched_players"]), ("partial", 1, 1))
            cover = news.snapshot(self.root, "first", self.now())["coverage"]["home"]
            self.assertFalse(cover["observed"])
            self.assertEqual((cover["sources"][0]["invalid_entries"], cover["sources"][0]["unmatched_players"]), (1, 1))
            self.assertEqual([f["player_name"] for f in self.facts()], ["Morgan Smith"])
            self.advance()
        self.assertEqual(self.count("news_rejected_items"), 1)
        with news._db(self.root) as db:
            sha = db.execute("SELECT original_sha256 FROM news_rejected_items").fetchone()[0]
        quarantined = json.loads(store.read_blob(self.root, sha))
        self.assertEqual(quarantined["source_fields"]["status"], "resting")
        self.assertNotIn(b"third-party secret", store.read_blob(self.root, sha))
        self.assertNotIn("gemini_intel", quarantined["source_fields"])
        self.assertEqual(self.run_wire([self.player])["state"], "ok")
        self.assertTrue(news.snapshot(self.root, "first", self.now())["coverage"]["home"]["observed"])

    def test_another_empty_healthy_feed_cannot_mask_a_fresh_missing_player(self):
        news.add_feed(self.root, feed())
        self.run_wire([self.player, self.player | {"player_id": 2, "display_name": "Unknown Member"}])
        side = news.snapshot(self.root, "first", self.now())["coverage"]["home"]
        self.assertFalse(side["observed"])
        self.assertEqual(side["coverage_gap"], "incomplete_availability_feed")
        by_source = {item["id"]: item for item in side["sources"]}
        self.assertTrue(by_source["club-bulletins"]["availability_observed"])
        self.assertFalse(by_source[news_sources.WIRE]["availability_observed"])

    def test_partial_list_cannot_withdraw_a_missing_identity_and_reused_cache_cannot_restore_freshness(self):
        started = self.now()
        self.run_wire([self.player])
        self.advance()
        self.assertEqual(self.run_wire([{"team": "Chelsea", "status": "undocumented"}])["state"], "partial")
        self.assertEqual(self.count("news_articles"), 1)
        self.assertEqual(self.facts()[0]["last_seen_at"], started)
        news_at = self.advance(hours=7)
        # Use the last admissible provider token, whose underlying snapshot is stale.
        provider_at = store.iso(store.instant(started) + timedelta(minutes=21))
        self.assertEqual(self.run_wire([self.player], publisher_time=provider_at)["articles_added"], 0)
        fact, = self.facts()
        self.assertEqual((fact["last_seen_at"], fact["last_seen_state_as_of"]), (news_at, provider_at))
        self.assertEqual(news.snapshot(self.root, "first", news_at)["coverage"]["home"]["sources"][0]["status"], "stale")
        self.advance()
        self.assertEqual(self.run_wire([])["articles_added"], 1)
        self.assertEqual(self.facts(), [])

    def test_bad_license_or_wrong_club_blocks_the_entire_response(self):
        self.run_wire([self.player])
        self.advance()
        for extra, meta in (([], {"license": "new terms"}), ([self.player | {"team": "Spurs"}], {})):
            result = self.run_wire([self.player | {"chance_of_playing": 75}, *extra], meta=meta)
            self.assertEqual(result["state"], "error")
            self.assertEqual(self.count("news_poll_proofs"), 1)
            self.assertEqual(self.count("news_articles"), 1)
            self.advance()
        self.assertEqual(self.facts()[0]["chance_of_playing"], 25)

    def test_legacy_timestamp_versions_reconstruct_the_first_receipt_without_a_migration_write(self):
        origin = self.now()
        self.run_wire([self.player], legacy=True)
        last_old = self.advance()
        self.run_wire([self.player], legacy=True)
        self.assertEqual(self.count("news_articles"), 2)
        with news._db(self.root, write=True) as db:
            for table in ("news_wire_polls", "news_wire_compactions", "news_wire_manifests", "news_wire_states"):
                db.execute("DROP TABLE " + table)
            db.execute("DELETE FROM runtime_schema_versions WHERE component='team_news'")
        legacy, = self.facts()
        self.assertEqual((legacy["first_seen_at"], legacy["last_seen_at"]), (origin, last_old))
        self.advance()
        self.assertEqual(self.run_wire([self.player])["articles_added"], 0)
        modern, = self.facts()
        self.assertEqual((modern["first_seen_at"], modern["last_seen_at"]), (origin, self.now()))
        self.assertEqual(self.facts(last_old)[0]["last_seen_at"], last_old)

    def test_inline_candidate_membership_and_its_content_hash_remain_replayable(self):
        origin = self.now()
        self.run_wire([self.player])
        self.advance()
        self.run_wire([self.player])
        result = self.facts()[0]
        with news._db(self.root, write=True) as db:
            receipt = result["last_seen_receipt"]
            proof, members, projection = db.execute("""SELECT p.payload,m.articles,m.projection FROM news_wire_polls w
                JOIN news_poll_proofs p ON p.id=w.receipt JOIN news_wire_manifests m ON m.id=w.articles
                WHERE w.receipt=?""", (receipt,)).fetchone()
            old = json.loads(proof)
            old.pop("wire_projection_sha256")
            old["wire_states"] = json.loads(projection)["states"]
            old_receipt = store.digest(old)
            db.execute("INSERT INTO news_poll_proofs VALUES (?,?)", (old_receipt, store.canonical(old)))
            db.execute("UPDATE news_wire_polls SET receipt=?,articles=? WHERE receipt=?", (old_receipt, members, receipt))
        replay, = self.facts()
        self.assertEqual((replay["last_seen_receipt"], replay["last_seen_at"], replay["first_seen_at"]),
                         (old_receipt, self.now(), origin))
        self.advance()
        self.assertEqual(self.run_wire([self.player])["articles_added"], 0)
        self.assertNotEqual(self.facts()[0]["last_seen_receipt"], old_receipt)

    def test_nonfinite_percentage_isolated_with_an_ascii_safe_quarantine(self):
        bad = self.player | {"player_id": 9, "chance_of_playing": float("nan"), "news": "\ud800"}
        result = self.run_wire([bad, self.player])
        self.assertEqual((result["state"], result["rejected_items"], result["claims_added"]), ("partial", 1, 1))
        self.assertEqual(self.facts()[0]["player_name"], "Morgan Smith")

    def test_lossless_retention_only_compacts_superseded_completed_match_originals(self):
        first = self.now()
        self.run_wire([self.player])
        sig = self.facts()[0]["original_sha256"]
        self.advance()
        self.run_wire([self.player | {"chance_of_playing": 0}])
        newest = self.facts()[0]["original_sha256"]
        before = self.facts(first)
        self.clock = store.instant("2026-11-27T18:30Z")
        self.assertEqual(news.retention(self.root, days=1)["superseded_versions"], 0)
        store.record_result(self.root, {"fixture_id": "first", "home_goals": 1, "away_goals": 0,
            "source": "test", "locator": "final:first", **dict.fromkeys(("finalized_at", "published_at", "received_at"), self.now())})
        self.advance(days=3)
        plan = news.retention(self.root, days=1)
        self.assertEqual((plan["superseded_versions"], plan["deleted_versions"]), (1, 0))
        raw = store.blob_path(self.root, sig)
        self.assertNotEqual(raw.suffix, ".gz")
        packed = raw.with_suffix(".gz")
        packed.write_bytes(gzip.compress(b"wrong content"))
        with self.assertRaisesRegex(ValueError, "SHA-256 verification"):
            news.retention(self.root, days=1, compact=True)
        self.assertTrue(raw.is_file())
        self.assertEqual(self.facts(first), before)
        packed.unlink()
        out = news.retention(self.root, days=1, compact=True)
        self.assertEqual(out["compacted_originals"], 1)
        self.assertGreater(out["encoded_bytes_saved"], 0)
        self.assertEqual(store.blob_path(self.root, sig).suffix, ".gz")
        self.assertNotEqual(store.blob_path(self.root, newest).suffix, ".gz")
        self.assertEqual(self.facts(first), before)
        self.assertEqual(news.retention(self.root, days=1, compact=True)["compacted_originals"], 0)
        path = self.root.parent / "cold-copy"
        self.assertTrue(news.retention(self.root, days=1, backup_to=path)["backup"]["ok"])
        restored = self.root.parent / "restored"
        operations.restore_state(path, restored)
        self.assertEqual(news.snapshot(restored, "first", first)["facts"], before)
