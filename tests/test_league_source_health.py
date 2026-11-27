import argparse
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from polymarket861 import context_feeds, league_catalog, league_press, league_rosters, news_roster, store
from polymarket861.blob_storage import blob_path


SEASON = "2026-27"
NOW = "2026-11-27T12:00:00.000000Z"
FIRST = "2026-11-25T10:20:00.000000Z"
SECOND = "2026-11-26T10:20:00.000000Z"
LAST = "2026-11-27T10:20:00.000000Z"
GDELT = context_feeds.GKG
ROSTER = league_rosters._ROSTER_SOURCE
EARLY_EDITION = "20261125100000"
LAST_EDITION = "20261126100000"
LINK = "https://example.org/bayern-squad"


class LeagueSourceHealth(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "five"
        self.legacy = Path(temp.name) / "legacy"
        self.roster = self.root / "five-league-rosters"
        self.press = self.root / "team-press"
        clock = patch.object(store, "now_utc", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def health(self, leagues=None):
        return league_catalog.local_source_health(self.root, SEASON, leagues, epl_data_dir=self.legacy)

    def database(self, directory, name, schema):
        directory.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(directory / name)
        conn.executescript(schema)
        return conn

    def test_empty_partial_and_unrecognized_stores(self):
        result = self.health()
        self.assertEqual(result["state"], "no_local_database")
        self.assertEqual(result["registered_teams"], 96)
        self.assertEqual(result["teams_without_checks"], 96)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.legacy.exists())
        with closing(self.database(self.root, "runtime.sqlite3", "CREATE TABLE other(x)")):
            pass
        self.assertEqual(self.health()["state"], "no_news_collection_schema")
        with closing(self.database(self.press, "index.sqlite3", league_press.SCHEMA)) as db, db:
            db.execute("DROP TABLE press_clubs")
            db.execute("INSERT INTO press_releases VALUES(?,?,?,?,?,?)",
                       (LAST_EDITION, "checksum", SECOND, SEASON, '["bun"]', 10))
        result = self.health()
        self.assertEqual(result["teams_with_local_checks"], 18)
        self.assertEqual(result["teams_with_observations"], 0)
        row = result["clubs"]["bun/bayern-munchen"][GDELT]
        self.assertEqual(row["collector_state"], "link_table_unavailable")
        self.assertIsNone(row["observed_links"])
        self.assertEqual(result["clubs"]["lal/barcelona"], {})
        self.assertFalse(self.roster.exists())

    def test_only_matching_batches_and_distinct_club_urls_are_observations(self):
        with closing(self.database(self.press, "index.sqlite3", league_press.SCHEMA)) as db, db:
            for edition, received, season, leagues in (
                    (EARLY_EDITION, LAST, SEASON, ["bun", "lal"]),
                    (LAST_EDITION, SECOND, SEASON, ["bun"]),
                    ("20261124100000", FIRST, "2025-26", ["bun", "lal"])):
                db.execute("INSERT INTO press_releases VALUES(?,?,?,?,?,?)",
                           (edition, "checksum", received, season, json.dumps(leagues), 42))
            for edition, league, raw, url in (
                    (EARLY_EDITION, "bun", "FC Bayern München", LINK),
                    (LAST_EDITION, "bun", "FC Bayern München", LINK),
                    (EARLY_EDITION, "bun", "FC Bayern München", LINK + "-2"),
                    (LAST_EDITION, "lal", "FC Barcelona", LINK + "-not-scanned-in-this-edition"),
                    ("20261124100000", "lal", "FC Barcelona", LINK + "-wrong-season"),
                    (EARLY_EDITION, "bun", "FC Barcelona", LINK + "-wrong-membership")):
                db.execute("INSERT INTO press_clubs VALUES(?,?,?,?,?)",
                           (edition, league, raw, url, store.canonical({"url": url})))
        result = self.health()
        self.assertEqual(result["news"], {"teams_with_local_checks": 38, "teams_with_observations": 1,
                                         "teams_checked_without_observations": 37, "teams_without_checks": 58})
        batch = result["latest_shared_gdelt_batch_by_league"]["bun"]
        self.assertEqual((batch["edition"], batch["received_at"], batch["last_scan_utc"]), (LAST_EDITION, SECOND, LAST))
        bayern = result["clubs"]["bun/bayern-munchen"][GDELT]
        self.assertEqual((bayern["last_check_utc"], bayern["observed_links"]), (LAST, 2))
        self.assertEqual(bayern["latest_link"]["url"], LINK)
        barca = result["clubs"]["lal/barcelona"][GDELT]
        self.assertEqual((barca["collector_state"], barca["observed_links"]), ("checked_no_links", 0))
        self.assertIsNone(barca["latest_link"])
        self.assertFalse(barca["has_observation"])

    def test_roster_receipt_survives_later_failed_check_and_requires_archived_original(self):
        original = b"an archived first-team page"
        sha = hashlib.sha256(original).hexdigest()
        archive = blob_path(self.roster, sha)
        archive.parent.mkdir(parents=True)
        archive.write_bytes(original)
        projection = {"parser_version": "test", "reason": None, "players": [{"name": f"Player {i}"} for i in range(11)],
                      "evidence": [{"source_sha256": sha, "published_at": FIRST, "received_at": SECOND}]}
        digest = store.digest(projection)
        with closing(self.database(self.roster, "runtime.sqlite3", league_rosters._ROSTER_SCHEMA)) as db, db:
            db.execute("INSERT INTO local_roster_projections VALUES(?,?)", (digest, store.canonical(projection)))
            db.execute("INSERT INTO local_roster_receipts VALUES(?,?,?,?,?)", ("lal", SEASON, "barcelona", SECOND, digest))
            for league, team in (("lal", "barcelona"), ("lal", "athletic-club"), ("epl", "arsenal")):
                db.execute("INSERT INTO local_roster_polls VALUES(?,?,?,?,?,?,?)",
                           (league, SEASON, team, LAST, NOW, "failed", "publisher unavailable"))
        before = sorted((p, p.stat().st_mtime_ns, p.read_bytes()) for p in self.root.rglob("*") if p.is_file())
        result = self.health()
        self.assertEqual(result["teams_with_local_checks"], 2)
        self.assertEqual(result["teams_with_observations"], 1)
        self.assertEqual(result["news"]["teams_with_local_checks"], 0)
        row = result["clubs"]["lal/barcelona"][ROSTER]
        self.assertEqual((row["collector_state"], row["last_check_utc"]), ("failed", LAST))
        self.assertEqual((row["roster_players"], row["latest_receipt_utc"], row["roster_state"]), (11, SECOND, "available"))
        self.assertTrue(row["has_roster"])
        self.assertFalse(result["clubs"]["lal/athletic-club"][ROSTER]["has_roster"])
        self.assertEqual(result["clubs"]["epl/arsenal"], {})
        self.assertEqual(before, sorted((p, p.stat().st_mtime_ns, p.read_bytes()) for p in self.root.rglob("*") if p.is_file()))
        with patch.object(store, "now_utc", return_value="2026-12-12T00:00:00.000000Z"):
            expired = self.health(["lal"])["clubs"]["lal/barcelona"][ROSTER]
        self.assertTrue(expired["has_roster"])
        self.assertEqual(expired["roster_state"], "expired")
        archive.write_bytes(b"damage")
        row = self.health(["lal"])["clubs"]["lal/barcelona"][ROSTER]
        self.assertFalse(row["has_observation"])
        self.assertEqual(row["roster_state"], "roster_original_unavailable")

    def test_legacy_directory_merge_uses_real_receipts_and_preserves_latest_check(self):
        history = """CREATE TABLE news_checks(id INTEGER,season,team_key,source,fixture_id,checked_at,state,detail);
                     CREATE VIEW news_check_history AS SELECT * FROM news_checks;"""
        with closing(self.database(self.legacy, "runtime.sqlite3", history + news_roster._SCHEMA)) as db, db:
            for index, team, checked, status, detail in (
                    (1, "arsenal", FIRST, "ok", {"articles_added": 1}), (2, "arsenal", LAST, "failed", {}),
                    (3, "chelsea", LAST, "ok", {"articles_added": 0, "roster_players": 30, "response_sha256": "abcd"})):
                db.execute("INSERT INTO news_checks VALUES(?,?,?,?,?,?,?,?)",
                           (index, SEASON, team, "sample", f"epl/{SEASON}/arsenal/chelsea", checked, status, json.dumps(detail)))
            db.execute("INSERT INTO news_checks VALUES(4,?,?,?,?,?,?,?)",
                       (SEASON, "barcelona", "sample", f"lal/{SEASON}/barcelona/getafe-cf", LAST, "ok", '{"articles_added": 2}'))
            document = {"source": news_roster.SOURCE, "season": SEASON, "team": "Arsenal FC", "players": [{"player_id": 1}]}
            digest = store.digest(document)
            db.execute("INSERT INTO news_roster_content VALUES(?,?)", (digest, store.canonical(document)))
            db.execute("INSERT INTO news_roster_receipts VALUES(?,?,?,?)", (SEASON, "arsenal", SECOND, digest))
            db.execute("INSERT INTO news_roster_checks VALUES(?,?,?,?,?,?)",
                       (SEASON, "arsenal", "claim", NOW, "error", '{"status":"error","error":"temporarily unavailable"}'))
        with closing(self.database(self.roster, "runtime.sqlite3", league_rosters._ROSTER_SCHEMA)) as db, db:
            db.execute("INSERT INTO local_roster_polls VALUES(?,?,?,?,?,?,?)",
                       ("lal", SEASON, "barcelona", LAST, NOW, "unknown", "empty_roster"))
        parser = argparse.ArgumentParser()
        parser.add_argument("--data-dir", type=Path)
        league_catalog.register(parser.add_subparsers())
        args = parser.parse_args(["--data-dir", str(self.root), "leagues", "health", "--epl-data-dir", str(self.legacy)])
        result = args.func(args)
        self.assertEqual((result["registered_teams"], result["teams_with_local_checks"], result["teams_with_observations"]),
                         (96, 3, 1))
        self.assertEqual(result["teams_checked_without_observations"], 2)
        sample = result["clubs"]["epl/arsenal"]["sample"]
        self.assertEqual((sample["collector_state"], sample["last_check_utc"], sample["latest_observation_utc"]),
                         ("failed", LAST, FIRST))
        self.assertFalse(result["clubs"]["epl/chelsea"]["sample"]["has_observation"])
        self.assertNotIn("sample", result["clubs"]["lal/barcelona"])
        squad = result["clubs"]["epl/arsenal"][news_roster.SOURCE]
        self.assertEqual((squad["has_roster"], squad["latest_receipt_utc"], squad["collector_state"]), (True, SECOND, "error"))
        self.assertIsNone(squad["last_check_utc"])
        self.assertEqual(self.health(["lal"])["registered_teams"], 20)
        self.assertFalse((self.root / "runtime.sqlite3").exists())

    def test_legacy_article_scope_revisions_and_recording_time(self):
        schema = """CREATE TABLE news_checks(id,season,team_key,source,fixture_id,checked_at,state);
                    CREATE TABLE news_articles(seq INTEGER PRIMARY KEY,season,team_key,source,external_id,
                                               updated_at,recorded_at,fixture_id,retracted,url);"""
        source = "touchline-rumours"
        fixture = f"epl/{SEASON}/chelsea/arsenal"
        with closing(self.database(self.legacy, "runtime.sqlite3", schema)) as db, db:
            db.execute("INSERT INTO news_checks VALUES(?,?,?,?,?,?,?)", (1, SEASON, "chelsea", source, fixture, LAST, "ok"))
            rows = [
                (1, source, "retained", FIRST, FIRST, None, False),
                (2, source, "withdrawn", FIRST, FIRST, fixture, False),
                (3, source, "withdrawn", SECOND, SECOND, fixture, True),
                (4, source, "future", NOW, "2026-12-01T00:00:00.000000Z", fixture, False),
                (5, "other", "unscoped", LAST, LAST, None, False),
                (6, source, "foreign", LAST, LAST, f"lal/{SEASON}/chelsea/arsenal", False),
            ]
            for seq, src, external, updated, recorded, scope, withdrawn in rows:
                db.execute("INSERT INTO news_articles VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (seq, SEASON, "chelsea", src, external, updated, recorded, scope, withdrawn,
                            f"https://example.org/{external}"))
        result = self.health()
        club = result["clubs"]["epl/chelsea"]
        self.assertEqual(set(club), {source})
        self.assertEqual(club[source]["article_count"], 1)
        self.assertEqual(club[source]["latest_article"], {"recorded_at": FIRST, "url": "https://example.org/retained"})
        self.assertTrue(club[source]["has_observation"])
        self.assertEqual((result["news"]["teams_with_observations"], result["news"]["teams_without_checks"]), (1, 95))

    def test_reader_sees_committed_wal_and_cannot_write(self):
        with closing(self.database(self.press, "index.sqlite3", league_press.SCHEMA)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            writer.execute("INSERT INTO press_releases VALUES(?,?,?,?,?,?)", (LAST_EDITION, "sum", LAST, SEASON, '["bun"]', 8))
            writer.commit()
            dbpath = self.press / "index.sqlite3"
            with league_catalog._health_database(dbpath) as (reader, tables):
                self.assertIn("press_releases", tables)
                with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                    reader.execute("DELETE FROM press_releases")
            self.assertEqual(self.health(["bun"])["teams_checked_without_observations"], 18)
            self.assertEqual(writer.execute("SELECT count(*) FROM press_releases").fetchone()[0], 1)
