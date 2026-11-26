import argparse
import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from polymarket861 import league_catalog as catalog
from polymarket861 import league_registry as registry
from polymarket861 import league_source_rights as rights


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports" / "five-leagues-0.5"


class FiveLeagueRegistry(unittest.TestCase):
    def test_explicit_alias_pairs_and_historical_23_splits(self):
        audit_years = set(catalog._years("2018-19", "2024-25"))
        expected = {"epl": (0, 0), "lal": (10, 9), "bun": (6, 4), "sea": (6, 6), "fl1": (5, 4)}
        for league, (pair_count, split_count) in expected.items():
            splits = 0
            aliases = registry.alias_rows(league)
            self.assertEqual(len(aliases), pair_count)
            for entry in aliases:
                with self.subTest(league=league, alias=entry["raw"]):
                    seasons = entry["effective_seasons_by_spelling"]
                    old, new = entry["raw"], entry["canonical"]
                    self.assertTrue(seasons[old] and seasons[new])
                    self.assertFalse(set(seasons[old]) & set(seasons[new]))
                    self.assertEqual(registry.club_key(league, seasons[old][0], old),
                                     registry.club_key(league, seasons[new][0], new))
                    self.assertIn(registry.SOURCE_COMMIT, entry["sources_by_season"][seasons[old][0]])
                    splits += int(all(audit_years.intersection(observed) for observed in seasons.values()))
            self.assertEqual(splits, split_count)

    def test_frozen_input_rejects_unseen_spelling_including_equivalent_slugs(self):
        registry.validate_members("lal", "2025-26", ["Real Madrid CF"])
        for name in ("Real Madrid", "REAL MADRID CF", "Real  Madrid CF", "Real Madrid CF B"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unknown"):
                registry.validate_members("lal", "2025-26", [name])
        with self.assertRaisesRegex(ValueError, "missing"):
            registry.validate_members("lal", "2025-26", ["Real Madrid CF"], require_complete=True)
        with self.assertRaisesRegex(ValueError, "Unregistered"):
            registry.members("lal", "2030-31")
        # The Italian alias is not an EPL alias.
        self.assertNotEqual(registry.club_key("sea", "2018-19", "Inter"),
                            registry.club_key("epl", "2018-19", "Inter"))

    def test_all_76_frozen_seasons_are_bijective_and_link_source_blobs(self):
        evidence = json.loads((ROOT / "docs/five-leagues-0.5/evidence/history.json").read_text())
        count = 0
        for code, p in registry.PROFILES.items():
            expected = evidence["inventory_2010_11_to_2026_27"][p.archive_code]["files_git_blob_sha1"]
            self.assertEqual(registry.source_blobs(code), expected)
            for season in expected:
                names = registry.members(code, season)
                registry.validate_members(code, season, names, require_complete=True)
                self.assertEqual(len(names), registry.team_count(code, season))
                count += 1
        self.assertEqual(count, 76)

    def test_french_restructure_distinguishes_departures_and_spelling(self):
        self.assertEqual(registry.fixture_count("fl1", "2022-23"), 380)
        self.assertEqual(registry.round_count("fl1", "2023-24"), 34)
        self.assertEqual(registry.fixture_count("fl1", "2023-24"), 306)
        delta = catalog.season_members("fl1", "2023-24")
        self.assertEqual(len(delta["left_league"]), 4)
        self.assertEqual(sum(r["change"] == "entered_league" for r in delta["clubs"]), 2)
        legacy = [(r, c) for c in registry.PROFILES for s in registry.source_blobs(c)
                  for r in catalog.season_members(c, s)["clubs"]
                  if r["change"] == "retained_source_spelling_changed" and r["source_name"] == "Real Madrid CF"]
        self.assertTrue(legacy)
        self.assertEqual(legacy[0][0]["previous_source_name"], "Real Madrid")

    def test_source_venue_time_and_extended_season(self):
        when = datetime(2024, 8, 29, 21, 30, tzinfo=ZoneInfo(registry.source_timezone("lal")))
        venue = ZoneInfo(registry.venue_timezone("lal", "UD Las Palmas"))
        self.assertEqual(when.astimezone(venue).hour, 20)
        self.assertEqual(when.astimezone(UTC).hour, 19)
        with self.assertRaisesRegex(ValueError, "Unregistered source clock"):
            registry.source_timezone("lal", "unreviewed-provider")
        self.assertIsNone(registry.venue_timezone("fl1", "OGC Nice", neutral=True))
        self.assertEqual(registry.season_window("sea", "2019-20")[1], date(2020, 8, 3))
        for invalid in ("202-23", "2022-24", "2022/23"):
            with self.assertRaises(ValueError):
                registry.team_count("fl1", invalid)

    def test_exception_direction_stage_lifecycle_and_independent_completion(self):
        case = registry.fixture_exception("sea", "2023-24", "Udinese Calcio", "AS Roma")
        self.assertEqual((case.kind, case.first_played_on, case.completion_date),
                         ("resumed", "2024-04-14", "2024-04-25"))
        self.assertTrue(case.train_regulation)
        self.assertFalse(case.predict_prematch)
        self.assertIsNone(case.first_kickoff_utc)
        self.assertTrue(case.sources)
        for c, h, a, stage in (("sea", "AS Roma", "Udinese Calcio", "regular"),
                               ("sea", "Udinese Calcio", "AS Roma", "cup"),
                               ("bun", "Udinese Calcio", "AS Roma", "regular")):
            self.assertIsNone(registry.fixture_exception(c, "2023-24", h, a, stage=stage))
        bournemouth = registry.fixture_exception("epl", "2023-24", "AFC Bournemouth", "Luton Town FC")
        self.assertEqual((bournemouth.kind, bournemouth.completion_date), ("replayed", "2024-03-13"))
        pending = registry.fixture_exception("lal", "2026-27", "Rayo Vallecano de Madrid", "Athletic Club")
        self.assertFalse(pending.train_regulation)
        self.assertFalse(pending.predict_prematch)
        verona = registry.fixture_exception("sea", "2020-21", "Hellas Verona FC", "AS Roma")
        self.assertEqual(verona.kind, "awarded")
        self.assertFalse(verona.train_regulation)
        self.assertFalse(verona.predict_prematch)
        # One specific playoff between these clubs took place on 11 June. The
        # 5 March regular-season fixture remains an ordinary and distinct row.
        self.assertIsNone(registry.fixture_exception("sea", "2022-23", "Spezia Calcio", "Hellas Verona FC"))
        all_covid = [c for (lg, s, _, _), c in registry._exceptions().items() if (lg, s) == ("fl1", "2019-20")]
        self.assertEqual(len(all_covid), 101)
        self.assertTrue(all(c.kind == "cancelled" and not c.train_regulation for c in all_covid))

    def test_verified_8_year_inventory_quarantines_flagged_and_unmarked_rows(self):
        inventory = json.loads((REPORTS / "history-inventory.json").read_text())
        self.assertEqual((inventory["verified_files"], inventory["regular_rows"]), (40, 14386))
        for row in inventory["files"]:
            self.assertEqual(row["git_blob_sha1"], registry.source_blob(row["league"], row["season"]))
            self.assertEqual(row["ordered_pairs"], registry.fixture_count(row["league"], row["season"]))
            self.assertEqual(sum(row["dispositions"].values()), row["regular_rows"])
            self.assertNotIn("unverified_source_status", row["dispositions"])
        dispositions = inventory["dispositions"]
        self.assertGreaterEqual(dispositions["regulation_label_after_verified_completion_date_only"], 7)
        self.assertGreaterEqual(dispositions["quarantined_exception"], 106)
        self.assertEqual(dispositions["missing_or_invalid_regulation_score"], 20)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "2024-25").mkdir()
            (path / "2024-25/de.1.json").write_text('{"matches":[]}')
            with self.assertRaisesRegex(ValueError, "Git blob"):
                catalog.verify_season(path, "bun", "2024-25")

    def test_rights_are_explicit_for_all_clubs_and_discovery_cannot_train(self):
        data = rights.rights_register()
        self.assertEqual(Counter(c["league"] for c in data["clubs"]),
                         {"epl": 20, "lal": 20, "bun": 18, "sea": 20, "fl1": 18})
        self.assertEqual(len({c["club_id"] for c in data["clubs"]}), 96)
        self.assertEqual(json.loads((REPORTS / "club-source-rights.json").read_text()),
                         json.loads(json.dumps(data)))
        for c in data["clubs"]:
            p = data["league_rights"][c["rights_profile"]]
            self.assertEqual("touchline_directory" in c, c["league"] == "epl")
            self.assertIn("team_article_fulltext", p["uncovered_fields"])
            self.assertNotIn("clausefoot", p["fields_to_candidate_sources"]["player_availability"])
        self.assertIn("clausefoot", data["league_rights"]["fl1"]["metadata_discovery_only"])
        self.assertIn("openligadb-bl1", data["league_rights"]["bun"]["fields_to_candidate_sources"]["schedule_and_result"])
        self.assertRaises(ValueError, rights.rights_register, season="2025-26")

    def test_local_health_preserves_unknown_failed_and_cross_league_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = catalog.local_source_health(root, "2026-27", ["epl"])
            self.assertEqual(status["teams_with_local_checks"], 0)
            self.assertFalse((root / "runtime.sqlite3").exists())
            with sqlite3.connect(root / "runtime.sqlite3") as db:
                db.execute("CREATE TABLE news_checks(id INTEGER,season,team_key,source,fixture_id,checked_at,state)")
                for index, league, state in ((1, "epl", "ok"), (2, "epl", "failed"), (3, "lal", "ok")):
                    db.execute("INSERT INTO news_checks VALUES(?,?,?,?,?,?,?)",
                               (index, "2026-27", "arsenal", "sample", f"{league}/2026-27/arsenal/chelsea",
                                "2026-08-22T12:00:00Z", state))
            status = catalog.local_source_health(root, "2026-27")
            self.assertEqual(status["teams_with_local_checks"], 1)
            self.assertEqual(status["clubs"]["epl/arsenal"]["sample"]["collector_state"], "failed")

    def test_catalog_registration_uses_normal_command_arguments(self):
        parser = argparse.ArgumentParser()
        catalog.register(parser.add_subparsers())
        args = parser.parse_args(["leagues", "clubs", "--season", "2026-27", "--league", "fl1"])
        self.assertEqual(len(args.func(args)[0]["clubs"]), 18)
        args = parser.parse_args(["leagues", "list", "--season", "2022-23"])
        self.assertEqual({r["code"]: r["fixtures"] for r in args.func(args)}["fl1"], 380)
