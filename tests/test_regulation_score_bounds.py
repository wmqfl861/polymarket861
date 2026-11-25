import hashlib
import io
import json
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path

import test_independent_results as wikicases
import test_result_admission as sources
from polymarket861 import (
    cli,
    datasets,
    football,
    input_manifest,
    pipeline,
    result_freshness,
    result_pairings,
    store,
)
from polymarket861 import result_admission as admission
from polymarket861 import result_feed as feed
from polymarket861.score_bounds import INVALID_SCORE


class RegulationScores(unittest.TestCase):
    def setUp(self):
        self.case = sources.VerifiedResults()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root

    def test_operator_import_rejects_entire_batch_then_accepts_upper_bound(self):
        self.case.set_time(0)
        file = self.root / "submission.json"
        attachment = self.root / "sheet.txt"
        attachment.write_bytes(b"Operator match sheet for the two registered games")
        matches = [
            {
                "home": row["team1"]["teamName"],
                "away": row["team2"]["teamName"],
                "kickoff": row["matchDateTimeUTC"],
                "home_goals": 1,
                "away_goals": 0,
                "status": "final",
                "period": "regulation",
                "locator": f"fixture {i}",
            }
            for i, row in enumerate(self.case.rows)
        ]
        spec = {
            "season": sources.SEASON,
            "round": 1,
            "rights": "permission",
            "source": "club match sheet",
            "evidence_file": attachment.name,
            "evidence_sha256": hashlib.sha256(attachment.read_bytes()).hexdigest(),
            "matches": matches,
        }
        args = [
            "--data-dir",
            str(self.root),
            "result-feed",
            "import-matches",
            "2026",
            "1",
            str(file),
        ]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            for bad in (91, -1, 51, True, 1.0, "2"):
                with self.subTest(bad=bad):
                    matches[-1]["home_goals"] = bad
                    file.write_text(json.dumps(spec))
                    self.assertEqual(cli.main(args), 2)
                    with closing(admission.database(self.root)) as db:
                        self.assertEqual(
                            db.execute(
                                "SELECT count(*) FROM result_versions"
                            ).fetchone()[0],
                            0,
                        )
                        self.assertEqual(
                            db.execute("SELECT count(*) FROM rf_batches").fetchone()[0],
                            0,
                        )
                    self.assertFalse(
                        result_pairings._final({"status": "final", "score": [0, bad]})
                    )
            matches[-1]["home_goals"] = 50
            file.write_text(json.dumps(spec))
            self.assertEqual(cli.main(args), 0)
            self.assertEqual(cli.main(args), 0)
        self.assertTrue(result_pairings._final({"status": "final", "score": [0, 50]}))
        self.assertEqual(self.case.state(self.case.fids[-1])["home_goals"], 50)

    def test_bad_openliga_result_is_local_and_two_receipts_still_admit_neighbor(self):
        rows = deepcopy(self.case.rows)
        rows[0]["matchResults"][-1]["pointsTeam1"] = 91
        for bad in (91, -1, 51, True, 1.0, "1"):
            row = deepcopy(rows[0])
            row["matchResults"][-1]["pointsTeam1"] = bad
            self.assertEqual(
                next(feed._full_time([row], sources.YEAR))[-2:], (None, INVALID_SCORE)
            )
        self.case.set_time(0)
        self.case.collect(rows, season=True)
        self.case.set_time(43)
        self.case.collect(rows)
        result = self.case.reconcile()
        self.assertEqual(result["available_results"], 1)
        self.assertTrue(
            any(
                r["fixture_id"] == self.case.fids[0] and r["reason"] == INVALID_SCORE
                for r in result["quarantined"]
            )
        )
        self.assertIsNone(admission.verified_regulation(self.root, self.case.fids[0]))
        self.assertEqual(
            admission.verified_regulation(self.root, self.case.fids[1])["home_goals"], 1
        )
        self.case.set_time(85)
        self.case.collect(self.case.rows)
        self.case.set_time(128)
        self.case.collect(self.case.rows)
        self.assertEqual(self.case.reconcile()["available_results"], 2)

    def test_openfootball_quarantines_only_the_bad_source_match(self):
        raw = self.case.root_files / sources.SEASON / "en.1.json"
        data = json.loads(raw.read_bytes())
        for row, goals in zip(data["matches"], ([91, 0], [2, 0])):
            row["score"] = {"ft": goals}
        raw.write_text(json.dumps(data))
        hist = self.case.root_files / "2025-26" / "en.1.json"
        self.case.set_time(0)
        report = datasets.observe(
            self.root,
            self.case.root_files,
            revision="e" * 40,
            expected={
                sources.SEASON: football.blob_sha(raw.read_bytes()),
                "2025-26": football.blob_sha(hist.read_bytes()),
            },
            register_season=sources.SEASON,
        )
        self.assertEqual(
            [(e["fixture_id"], e["reason"]) for e in report["data_conflicts"]],
            [(self.case.fids[0], INVALID_SCORE)],
        )
        actual = {
            r.fixture_id: r
            for r in football.load_fixtures(
                Path(report["fixture_root"]),
                evidence_path=Path(report["evidence_path"]),
            )
        }
        self.assertEqual(actual[self.case.fids[0]].status, "quarantined")
        self.assertEqual(actual[self.case.fids[1]].result_versions[-1].score, (2, 0))

    def test_wikimedia_bounds_are_applied_to_each_attributed_match(self):
        wiki = wikicases.WikiEvidence()
        wiki.setUp()
        self.addCleanup(wiki.doCleanups)
        from polymarket861 import independent_results

        for bad in ("91–0", "100–0", "2.0–0"):
            articles = {
                wiki.page1: wiki.box(score=bad),
                wiki.page2: wiki.box(result="L", score=bad),
            }
            title = independent_results._title(2026, wiki.teams[2])
            other = football.fixture_id("2026-27", wiki.teams[2], wiki.teams[3])
            articles[title] = wiki.box(
                home="Club 2", away="Club 3", mid=8124, score="50–0"
            )
            decoded = independent_results._decode(wiki.raw(articles), wiki.titles, 2026)
            self.assertEqual(
                (decoded[wiki.fixture]["status"], decoded[wiki.fixture].get("error")),
                ("conflict", INVALID_SCORE),
            )
            self.assertIsNone(decoded[wiki.fixture]["score"])
            self.assertEqual(decoded[other]["score"], [50, 0])

    def test_legacy_bad_revision_keeps_history_and_cannot_block_another_forecast(self):
        raw = self.case.root_files / sources.SEASON / "en.1.json"
        content = json.loads(raw.read_bytes())
        future = {
            "round": "Matchday 2",
            "date": "2026-08-29",
            "time": "20:00",
            "team1": "Liverpool FC",
            "team2": "Everton FC",
        }
        content["matches"].append(future)
        raw.write_text(json.dumps(content))
        hist = self.case.root_files / "2025-26" / "en.1.json"
        datasets.observe(
            self.root,
            self.case.root_files,
            revision="e" * 40,
            expected={
                sources.SEASON: football.blob_sha(raw.read_bytes()),
                "2025-26": football.blob_sha(hist.read_bytes()),
            },
            register_season=sources.SEASON,
        )
        self.case.set_time(0)

        def record(fid, goals):
            at = self.case.time
            return store.record_result(
                self.root,
                {
                    "fixture_id": fid,
                    "home_goals": goals[0],
                    "away_goals": goals[1],
                    "source": "club_sheet",
                    "locator": "sheet/" + at,
                    "finalized_at": at,
                    "published_at": at,
                    "received_at": at,
                },
            )

        first, second = self.case.fids
        record(first, (2, 1))
        record(second, (1, 0))
        self.case.set_time(5)
        with self.assertRaisesRegex(ValueError, "0–50"):
            record(first, (91, 0))
        second_version = record(first, (3, 1))
        # Simulate a pre-upgrade database without letting current writers bypass validation.
        with closing(store.connect(self.root)) as db, db:
            db.execute(
                "UPDATE result_versions SET home_goals=91 WHERE fixture_id=? AND version=?",
                (first, second_version["version"]),
            )
        self.assertEqual(self.case.state(first)["result_disposition"], INVALID_SCORE)
        self.assertIsNone(self.case.state(first)["home_goals"])
        self.assertEqual(self.case.state(second)["home_goals"], 1)
        freshness = result_freshness.status(self.root, sources.YEAR, at=self.case.time)
        self.assertIn(first, freshness["latest_round"]["missing_fixture_ids"])
        self.assertEqual(freshness["latest_round"]["received"], 1)

        def scores():
            manifest = input_manifest.verify_observed_inputs(self.root)
            rows = football.load_fixtures(
                Path(manifest["root"]), evidence_path=Path(manifest["evidence_path"])
            )
            return {r.fixture_id: [v.score for v in r.result_versions] for r in rows}

        self.assertEqual(scores()[first], [(2, 1), None])
        self.assertEqual(scores()[second], [(1, 0)])
        fid = football.fixture_id(sources.SEASON, future["team1"], future["team2"])
        self.assertIn(
            "probabilities",
            pipeline.make_forecast(
                self.root, fid, None, mode="technical", horizon="early"
            ),
        )
        self.case.set_time(10)
        record(first, (4, 2))
        self.assertEqual(scores()[first], [(2, 1), None, (4, 2)])
        with closing(store.connect(self.root)) as db:
            self.assertEqual(
                [
                    r[0]
                    for r in db.execute(
                        "SELECT home_goals FROM result_versions WHERE fixture_id=? ORDER BY version",
                        (first,),
                    )
                ],
                [2, 91, 4],
            )
