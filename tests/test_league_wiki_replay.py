import hashlib
import itertools
import json
import os
import sqlite3
import tempfile
import unittest
import zlib
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from polymarket861 import football, league_live_inputs as live, league_registry as registry, league_wiki_cache as cache


LEAGUE, SEASON = "bun", "2026-27"


def at(time):
    return f"2026-11-20T{time}:00.000000Z"


def revision(number, published, score="1–0", league=LEAGUE):
    clubs = registry.members(league, SEASON)
    names = [f"|name_T{i}=[[{club}]]" for i, club in enumerate(clubs)]
    scores = [f"| match_T{h}_T{a} = " + (score if (h, a) == (0, 1) else "")
              for h, a in itertools.permutations(range(len(clubs)), 2)]
    rev = {"revid": number, "timestamp": published, "slots": {"main": {"content": "\n".join(names + scores)}}}
    page = {"ns": 0 if league == "sea" else 10, "title": live._wiki_title(league, SEASON), "revisions": [rev]}
    return json.dumps({"query": {"pages": [page], "rightsinfo": {"url": live.wiki.LICENSE}}}).encode()


class VerifiedWikiReplayTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.path = self.state / "sources" / "domestic-live-inputs" / "receipts.sqlite3"
        self.path.parent.mkdir(parents=True)
        self.db = self.connect()
        self.addCleanup(self.db.close)
        self.db.executescript(live._SCHEMA)
        home, away = registry.members(LEAGUE, SEASON)[:2]
        self.fid = football.fixture_id(SEASON, home, away, league=LEAGUE)
        self.match = football.Fixture(self.fid, SEASON, 1, football.utc(at("10:00")), home, away, 1, 0)
        parse = patch.object(live, "_wiki_scores", wraps=live._wiki_scores)
        self.parse = parse.start()
        self.addCleanup(parse.stop)
        self.addCleanup(cache._cache.cache_clear)

    def connect(self):
        db = cache.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def insert(self, n, seen, published=None, score="1–0", league=LEAGUE):
        published = published or seen
        body = revision(n, published, score, league)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO wiki_receipts VALUES(?,?,?,?,?,?,?,?)",
                            (league, SEASON, n, published, seen, live._wiki_title(league, SEASON),
                             hashlib.sha256(body).hexdigest(), zlib.compress(body)))
        return body

    def history(self, until="19:00", db=None, league=LEAGUE):
        return live._wiki_history(db or self.db, league, SEASON, at(until))

    def test_cold_warm_increment_rewind_and_parser_identity(self):
        self.insert(1, at("10:00"))
        self.insert(2, at("11:00"), score="2–0")
        self.assertEqual(len(self.history("10:30")[0]), 1)
        self.assertEqual(self.parse.call_count, 1)
        # Production load_inputs opens a fresh connection each tick.
        with closing(self.connect()) as db:
            self.assertEqual([r[-1][self.fid] for r in self.history(db=db)[0]], [(1, 0), (2, 0)])
        self.assertEqual(self.parse.call_count, 2)
        with patch.object(cache.zlib, "decompress", side_effect=AssertionError("redecoded the archive")):
            self.assertEqual(len(self.history("10:30")[1]), 1)
            self.assertEqual([m["revision"] for m in self.history()[1]], [1, 2])
        self.insert(3, at("13:00"), score="")
        history, metadata = self.history()
        self.assertEqual(self.parse.call_count, 3)
        self.assertIsNone(history[-1][-1][self.fid])
        self.assertEqual(metadata[-1]["observed_at"], at("13:00"))
        with self.assertRaises(TypeError):
            history[-1][-1][self.fid] = (8, 8)
        self.insert(1, at("10:00"), league="sea")
        self.assertEqual(self.history(league="sea")[1][0]["title"], live._wiki_title("sea", SEASON))
        self.assertEqual(self.parse.call_count, 4)
        with patch.object(live, "_wiki_scores", wraps=self.parse) as replacement:
            self.assertEqual(self.history()[1], metadata)
            self.assertEqual(replacement.call_count, 3)

    def test_changed_bytes_metadata_and_uncommitted_snapshot(self):
        original = self.insert(1, at("10:00"))
        self.insert(2, at("13:00"), score="2–2")
        self.db.execute("PRAGMA journal_mode=WAL")
        previous = self.history()[1]
        with closing(self.connect()) as old:
            old.execute("BEGIN")
            self.assertEqual(self.history(db=old)[1], previous)
            with self.db:
                self.db.execute("UPDATE wiki_receipts SET body=? WHERE revision=2", (zlib.compress(b"wrong"),))
            self.assertEqual(self.history(db=old)[1], previous)
            self.assertEqual(len(self.history("12:00")[0]), 1)
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                self.history()
        with self.db:
            self.db.execute("DELETE FROM wiki_receipts WHERE revision=2")
        self.db.execute("UPDATE wiki_receipts SET body=? WHERE revision=1", (b"broken",))
        with self.assertRaisesRegex(ValueError, "compressed Wikimedia receipt"):
            self.history()
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(self.history()[1], previous[:1])
        with self.db:
            self.db.execute("UPDATE wiki_receipts SET body=?,published=? WHERE revision=1",
                            (zlib.compress(original, 0), at("10:01")))
        with self.assertRaisesRegex(ValueError, "inconsistent revision or time"):
            self.history()

    def test_steady_reads_skip_blobs_and_an_offline_file_edit_is_rechecked(self):
        raw = self.insert(1, at("10:00"))
        with closing(self.connect()) as db:
            first = self.history(db=db)[1]
        with closing(self.connect()) as db:
            def headers_only(action, table, column, *_):
                if action == sqlite3.SQLITE_READ and (table, column) == ("wiki_receipts", "body"):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(headers_only)
            self.assertEqual(self.history(db=db)[1], first)
        # Replace the file outside SQLite without changing its receipt headers,
        # generation marker or original compressed-byte length.
        archive = bytearray(self.path.read_bytes())
        blob = zlib.compress(raw)
        pos = archive.index(blob)
        archive[pos + len(blob) - 1] ^= 1
        replacement = self.path.with_suffix(".replacement")
        replacement.write_bytes(archive)
        if os.name == "nt":
            # Windows does not permit replacing an open SQLite file.
            self.db.close()
            replacement.replace(self.path)
        else:
            with closing(self.connect()) as old:
                replacement.replace(self.path)
                self.assertEqual(self.history(db=old)[1], first)
        with closing(self.connect()) as fresh, self.assertRaisesRegex(ValueError, "compressed Wikimedia receipt"):
            self.history(db=fresh)

    def test_polled_page_is_verified_once_across_fresh_reader_connections(self):
        body = revision(44, at("10:00"))
        with patch.object(live.wiki, "_get", return_value=body), patch.object(live.store, "now_utc", return_value=at("13:00")):
            live._poll_wiki(self.db, LEAGUE, SEASON)
        self.assertEqual(self.parse.call_count, 1)
        with closing(self.connect()) as db:
            history, meta = self.history(db=db)
        self.assertEqual((len(history), meta[0]["revision"], meta[0]["observed_at"]), (1, 44, at("13:00")))
        self.assertEqual(self.parse.call_count, 1)

    def test_live_input_entry_reconstructs_corrections_and_source_receipts(self):
        root = self.state / "football"
        path = root / SEASON / registry.profile(LEAGUE).source_file
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"matches": [{"round": "Matchday 1", "date": "2026-11-18",
                                                 "team1": self.match.home, "team2": self.match.away}]}))
        args = {"league": LEAGUE, "football_root": root, "market_state_dir": self.state / "markets"}
        with (patch.object(registry, "source_blob", return_value=registry.git_blob(path.read_bytes())),
              patch.object(live.store, "now_utc", return_value=at("09:30")),
              patch.object(live.league_openfootball_refresh, "refresh", return_value={"state": "ok"}),
              patch.object(live, "_poll_wiki")):
            live.load_inputs(self.state, **args)
            for i, (time, score) in enumerate([("11:00", "1–0"), ("11:10", "1–0"),
                                               ("12:00", ""), ("12:30", "2–1")], start=1):
                self.insert(i, at(time), score=score)
            for cutoff, scores, revisions in [("12:30", [(1, 0), None, (2, 1)], [1, 2, 3, 4]),
                                              ("12:00", [(1, 0), None], [1, 2, 3]),
                                              ("11:10", [(1, 0)], [1, 2])]:
                output = live.load_inputs(self.state, as_of=at(cutoff), **args)
                match, = output.fixtures
                self.assertEqual([v.score for v in match.result_versions], scores)
                self.assertEqual([m["revision"] for m in output.provenance["wikimedia_revisions"]], revisions)
                self.assertEqual(output.provenance["current_season_scores"], int(scores[-1] is not None))

    def test_projection_matches_full_replay_at_each_cutoff_with_reschedules_and_withdrawals(self):
        for n, (time, published, score) in enumerate([
            ("11:00", "11:00", "1–0"), ("11:30", "11:30", "1–0"), ("11:31", "11:31", "1–0"),
            ("12:10", "12:10", "1–0"), ("13:30", "12:05", "1–0"), ("13:31", "13:30", "1–0"),
            ("14:10", "14:10", ""), ("14:20", "14:20", "2–1"),
        ], start=1):
            self.insert(n, at(time), at(published), score)
        schedule = ((football.utc(at("12:00")), football.utc(at("12:00"))),)
        for pin, clocks in [(at("09:30"), schedule), (at("12:15"), schedule), (at("09:30"), ())]:
            original = (at("12:05"), "openfootball", "local", at("12:00"), self.match)
            for cutoff in ("11:30", "14:11", "19:00", "12:30", "19:00"):
                with self.subTest(pin=pin, clocks=len(clocks), cutoff=cutoff):
                    history, _ = self.history(cutoff)
                    direct = [(max(pin, seen), source, locator, pub, scores[self.fid])
                              for seen, source, locator, pub, scores in history]
                    changes = cache.score_observations(LEAGUE, SEASON, live._wiki_scores, history,
                                                       {self.fid: self.match}, {self.fid: clocks}, pin)
                    other = [original] if original[0] <= at(cutoff) else []
                    def result(rows):
                        return live._score_versions(self.match, sorted(other + list(rows), key=lambda r: r[0]), clocks)
                    self.assertEqual(result(changes.get(self.fid, ())), result(direct))
                    self.assertLessEqual(len(changes.get(self.fid, ())), len(direct))

    def test_first_equal_or_empty_receipt_after_schedule_change_survives_incremental_replay(self):
        # The change and its reversal fall between two wiki receipts. Both
        # receipts refer to the same kickoff, but intervening editions no
        # longer belong to one continuous schedule epoch.
        schedule = tuple((football.utc(at(when)), football.utc(at(kick)))
                         for when, kick in (("11:05", "12:00"), ("11:06", "10:00")))
        for value in (None, (2, 1)):
            with self.subTest(value=value):
                rows = [(at(when), "wikipedia", when + str(value), at(when), {self.fid: value})
                        for when in ("10:30", "11:30", "11:45")]
                def project(history):
                    return cache.score_observations(LEAGUE, SEASON, live._wiki_scores, history,
                                                    {self.fid: self.match}, {self.fid: schedule}, at("09:30"))
                self.assertEqual(len(project(rows[:1])[self.fid]), 1)
                changes = project(rows)[self.fid]
                self.assertEqual([row[0] for row in changes], [at("10:30"), at("11:30")])
                self.assertEqual(changes[-1][-1], value)
                direct = [(s, src, loc, pub, scores[self.fid]) for s, src, loc, pub, scores in rows]
                self.assertEqual(live._score_versions(self.match, changes, schedule),
                                 live._score_versions(self.match, direct, schedule))
