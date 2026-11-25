"""Prospective news study scores consume the same authenticated regulation result as settlement."""

import json
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket861 import football, independent_results, news_evaluation, pipeline
from polymarket861 import result_admission, result_feed, round_history, store
from test_news_matchday import AWAY, FID, HOME, KICKOFF, SEASON, YEAR, seed_fixture
from test_paired_proof import at, season_data
from test_result_feed import match


def native_receipt(root, home, away, kickoff, score, stamp):
    year = store.instant(kickoff).year
    body = match(101, home, away, score) | {
        "leagueSeason": year,
        "matchDateTimeUTC": store.iso(kickoff),
    }
    native_documents(root, year, [body], stamp)


def native_documents(root, year, matches, stamp):
    responses = {
        f"{result_feed.BASE}/getlastchangedate/pl/{year}/1": json.dumps(stamp).encode(),
        f"{result_feed.BASE}/getmatchdata/pl/{year}/1": json.dumps(matches).encode(),
    }
    with (
        at(stamp),
        patch.object(result_feed, "now_utc", side_effect=store.now_utc),
        patch.object(result_feed, "_get", side_effect=responses.__getitem__),
    ):
        result_feed.sync(root, year, round_id=1, confirm=True)
        result_admission.reconcile(root, year)


def admit_final(root, home, away, kickoff, score, stamp):
    for delay in (0, 41):
        receipt = store.iso(store.instant(stamp) + timedelta(minutes=delay))
        native_receipt(root, home, away, kickoff, score, receipt)


def independent_receipt(root, score, stamp):
    teams = [HOME, AWAY, *(f"Club {i:02d}" for i in range(1, 19))]
    pages = []
    for i, team in enumerate(teams):
        box = ""
        if team in (HOME, AWAY):
            winner = HOME if score[0] > score[1] else AWAY
            result = "D" if score[0] == score[1] else "W" if team == winner else "L"
            box = (
                "{{football box collapsible\n|round = 1\n|date = 12 August 2026\n"
                f"|time = 15:00 BST\n|team1 = {HOME}\n|team2 = {AWAY}\n"
                f"|score = {score[0]}–{score[1]}\n|result = {result}\n"
                "|report = https://www.premierleague.com/en/match/8123\n}}\n"
            )
        pages.append(
            {
                "title": independent_results._title(YEAR, team),
                "ns": 0,
                "pageid": i + 1,
                "revisions": [
                    {
                        "revid": int(store.instant(stamp).timestamp()) + i,
                        "timestamp": store.iso(stamp),
                        "slots": {
                            "main": {
                                "contentmodel": "wikitext",
                                "content": "===Premier League===\n" + box,
                            }
                        },
                    }
                ],
            }
        )
    raw = json.dumps(
        {"query": {"rightsinfo": {"url": independent_results.LICENSE}, "pages": pages}}
    ).encode()
    with at(stamp), patch.object(independent_results, "_get", return_value=raw):
        return independent_results._receive(root, YEAR, teams)


class NewsOutcomeAdmissionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "app"
        history = Path(tmp.name) / "football"
        with at("2026-08-01T10:00Z"):
            seed_fixture(self.root, history)
        with at("2026-08-12T12:59Z"):
            self.seal = pipeline.make_forecast(
                self.root, FID, history, horizon="T-60m"
            )["digest"]

    def study(self):
        return news_evaluation.evaluate_round(self.root, SEASON, 1)

    def publish(self):
        return round_history.publish_round(self.root, SEASON, 1, mode="technical")

    def assert_pending(self, report):
        self.assertEqual(
            (
                report["scheduled"],
                report["completed"],
                report["sealed"],
                report["scored"],
            ),
            (1, 0, 1, 0),
        )
        self.assertEqual(report["coverage"]["sealed_awaiting_final"], 1)
        self.assertEqual(report["coverage"]["final_without_pair"], 0)
        self.assertEqual(report["coverage"]["paired"], 0)
        self.assertEqual(report["fixtures"][0]["reason"], "awaiting_verified_score")
        self.assertNotIn("goals", report["fixtures"][0])
        self.assertIsNone(report["scores"]["no_news"])
        self.assertIsNone(report["delta"])
        self.assertEqual(report["verdict"], "insufficient_evidence")

    def test_manual_29_to_nil_is_pending_for_both_prospective_modes_and_season(self):
        with at("2026-08-13T10:00Z"):
            stamp = store.now_utc()
            written = pipeline.import_result_and_score(
                self.root,
                {
                    "fixture_id": FID,
                    "home_goals": 29,
                    "away_goals": 0,
                    "source": "uncorroborated operator input",
                    "locator": "manual://final",
                    "finalized_at": stamp,
                    "published_at": stamp,
                    "received_at": stamp,
                },
            )
            # Regular operator entry is still recorded. Neither a source label nor a plausible clock authenticates it.
            self.assertEqual(written["result"]["home_goals"], 29)
            self.assertIsNone(result_admission.verified_regulation(self.root, FID))
            self.assert_pending(written["round_reports"][0]["news_study"])
            self.assert_pending(self.study())
            self.assert_pending(news_evaluation.evaluate_season(self.root, SEASON))
            formal = news_evaluation.evaluate_round(self.root, SEASON, 1, mode="formal")
            self.assertEqual(
                (formal["completed"], formal["sealed"], formal["scored"]), (0, 0, 0)
            )
            self.assertEqual(formal["fixtures"][0]["reason"], "no_primary_seal")
            self.assertEqual(
                formal["fixtures"][0]["result_admission"], "awaiting_verified_score"
            )

    def test_independent_results_conflict_correction_withdrawal_and_previous_edition(
        self,
    ):
        t1, t2 = "2026-08-13T10:00Z", "2026-08-13T10:05Z"
        native_receipt(self.root, HOME, AWAY, KICKOFF, [0, 2], t1)
        self.assert_pending(self.study())
        independent_receipt(self.root, [0, 2], t2)
        with at(t2), patch.object(result_feed, "now_utc", side_effect=store.now_utc):
            result_admission.reconcile(self.root, YEAR)
            proof = result_admission.verified_regulation(self.root, FID)
            self.assertEqual(proof["proof"]["method"], "two_providers")
            first = self.publish()
        study = first["news_study"]
        row = study["fixtures"][0]
        self.assertEqual(
            (study["completed"], study["scored"], row["goals"]), (1, 1, [0, 2])
        )
        self.assertEqual(row["result_proof_sha256"], store.digest(proof["proof"]))
        self.assertEqual(row["result_confirmed_at"], store.iso(t2))
        self.assertEqual(row["seal"], self.seal)
        self.assertEqual(study, self.study())

        conflict = "2026-08-13T10:40Z"
        independent_receipt(self.root, [3, 0], conflict)
        with at(conflict):
            # Cached result_versions still says 0-2 and no disposition has yet been written.
            self.assertEqual(
                store.eligible_fixtures(self.root, season=SEASON)[0]["away_goals"], 2
            )
            self.assert_pending(self.study())
        corrected_at = "2026-08-13T11:20Z"
        native_receipt(self.root, HOME, AWAY, KICKOFF, [3, 0], corrected_at)
        with at(corrected_at):
            corrected = self.publish()
        corrected_row = corrected["news_study"]["fixtures"][0]
        self.assertEqual(
            (
                corrected_row["goals"],
                corrected_row["result_version"],
                corrected_row["seal"],
            ),
            ([3, 0], 2, self.seal),
        )
        self.assertEqual(corrected["news_study"], self.study())
        self.assertNotEqual(
            row["result_proof_sha256"], corrected_row["result_proof_sha256"]
        )
        self.assertNotEqual(row["scores"], corrected_row["scores"])
        self.assertGreater(corrected["report_version"], first["report_version"])
        previous = round_history.round_history(
            self.root, SEASON, 1, mode="technical", edition=first["report_version"]
        )
        self.assertEqual(previous["report"]["news_study"], study)

        t4 = "2026-08-13T11:30Z"
        with at(t4), patch.object(result_feed, "now_utc", side_effect=store.now_utc):
            result_admission.review_pairing(
                self.root,
                YEAR,
                FID,
                corrected_row["result_proof_sha256"],
                "withdraw",
                "operator",
                "bad source revision",
            )
            self.assertIsNone(result_admission.verified_regulation(self.root, FID))
            self.assert_pending(self.study())
            withdrawn = self.publish()
            self.assert_pending(withdrawn["news_study"])
            self.assertGreater(withdrawn["report_version"], corrected["report_version"])
            self.assertEqual(
                withdrawn["news_study"]["fixtures"][0]["result_disposition"],
                "operator_withdrew_result_pairing",
            )

    def test_season_rechecks_original_bytes_and_uses_only_one_snapshot(self):
        home, away = "Club 01", "Club 18"
        other_id = football.fixture_id(SEASON, home, away)
        with at("2026-08-01T10:01Z"):
            store.register_fixture(
                self.root,
                {
                    "fixture_id": other_id,
                    "season": SEASON,
                    "home": home,
                    "away": away,
                    "round_id": 1,
                    "kickoff": KICKOFF,
                    "known_at": store.now_utc(),
                    "source": "fixture schedule",
                    "locator": "synthetic://league/2",
                },
            )
        games = [match(101, HOME, AWAY, [1, 0]), match(102, home, away, [2, 2])]
        for game in games:
            game["matchDateTimeUTC"] = store.iso(KICKOFF)
        for stamp in ("2026-08-13T10:00Z", "2026-08-13T10:41Z"):
            native_documents(self.root, YEAR, games, stamp)
        with at("2026-08-13T11:00Z"):
            with patch.object(
                result_admission, "_raw_rows", wraps=result_admission._raw_rows
            ) as raw:
                study = news_evaluation.evaluate_season(self.root, SEASON)
            self.assertEqual(raw.call_count, 1)
            self.assertEqual((study["completed"], study["scored"]), (2, 1))
            self.assertEqual(study["coverage"]["final_without_pair"], 1)
            unsealed = next(f for f in study["fixtures"] if f["fixture_id"] == other_id)
            self.assertEqual(unsealed["reason"], "no_primary_seal")
            self.assertEqual(unsealed["result_admission"], "verified_regulation")
            with closing(store.connect(self.root)) as db:
                digest = db.execute(
                    "SELECT sha FROM rf_native_requests LIMIT 1"
                ).fetchone()[0]
            store.blob_path(self.root, digest).write_bytes(b"replaced after admission")
            dropped = news_evaluation.evaluate_season(self.root, SEASON)
            self.assertEqual(
                (
                    dropped["scheduled"],
                    dropped["completed"],
                    dropped["sealed"],
                    dropped["scored"],
                ),
                (2, 0, 1, 0),
            )
            self.assertTrue(
                all(
                    r["result_admission"] == "awaiting_verified_score"
                    for r in dropped["fixtures"]
                )
            )
            self.assertIsNone(dropped["delta"])


class SeasonAdmissionGuardTests(unittest.TestCase):
    def test_single_unverified_result_keeps_379_paired_fixture_season_pending(self):
        games = season_data(YEAR, finished=False)["matches"]
        arm = {"score": 65.0}
        for key in ("losses", "log_losses", "item_scores"):
            arm[key] = {k: 0.5 for k in football.ITEM_KEYS}
        rows = []
        for game in games:
            rows.append(
                {
                    "fixture_id": football.fixture_id(
                        SEASON, game["team1"], game["team2"]
                    ),
                    "season": SEASON,
                    "round_id": int(game["round"].split()[1]),
                    "home": game["team1"],
                    "away": game["team2"],
                    "sealed": True,
                    "scored": True,
                    "completed": True,
                    "observed_both": True,
                    "changed": False,
                    "recipe": "r",
                    "implementation": "build",
                    "news_slice": "no_applied_news",
                    "lines": {},
                    "scores": {"no_news": arm, "with_news": arm},
                }
            )
        self.assertTrue(news_evaluation._complete_calendar(rows))
        # The unsealed fixture would lift the completeness guard if its raw, manually recorded
        # score counted. Both versions of this test have 379 valid predictions in all 38 rounds.
        fixture = rows[-1] | {
            "home_goals": 29,
            "away_goals": 0,
            "result_version": 1,
            "schedule_version": 1,
            "kickoff": store.iso(KICKOFF),
        }
        rows[-1] = news_evaluation._score_one(
            fixture, None, None, "no_primary_seal", None, {}, "technical", None
        )
        pending = news_evaluation._summary(rows, SEASON, None, "T-60m", "technical")
        self.assertEqual(
            (pending["completed"], pending["scored"], pending["observed_both"]),
            (379, 379, 379),
        )
        self.assertEqual(pending["verdict"], "insufficient_evidence")
        proof = {
            "fixture_id": fixture["fixture_id"],
            "status": "final",
            "period": "regulation",
            "result_version": 1,
            "kickoff": fixture["kickoff"],
            "home_goals": 29,
            "away_goals": 0,
            "proof": {},
            "confirmed_at": "2026-08-13T10:00Z",
            "source": "unit-test witness",
        }
        rows[-1] = news_evaluation._score_one(
            fixture, None, None, "no_primary_seal", None, {}, "technical", proof
        )
        completed = news_evaluation._summary(rows, SEASON, None, "T-60m", "technical")
        self.assertEqual(
            (
                completed["completed"],
                completed["scored"],
                completed["coverage"]["final_without_pair"],
            ),
            (380, 379, 1),
        )
        self.assertEqual(completed["verdict"], "no_observed_changes")
