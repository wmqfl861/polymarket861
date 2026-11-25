import hashlib
import io
import json
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import test_result_admission as result_case
import test_schedule_consensus as schedule_case
from polymarket861 import (
    cli,
    football,
    football_candidates,
    historical_inputs,
    independent_results,
    input_manifest,
    pipeline,
    result_admission as admission,
    result_feed as feed,
    result_operations as ops,
    schedule_consensus as schedule,
    store,
)


class ResultIdentity(unittest.TestCase):
    def setUp(self):
        self.case = result_case.VerifiedResults()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root
        self.fid = self.case.fids[0]
        self.secondary = {}

        def read(_root, _db, _year, at):
            return {
                fid: active
                for fid, rows in self.secondary.items()
                if (active := [r for r in rows if r["received"] <= at])
            }

        patcher = patch.object(independent_results, "verified", side_effect=read)
        patcher.start()
        self.addCleanup(patcher.stop)

    def evidence(self, kickoff, score, *, status="final", match_id="epl:734"):
        self.secondary.setdefault(self.fid, []).append(
            {
                "source_id": "independent-season-reports",
                "source_match_id": match_id,
                "round": 1,
                "kickoff": store.iso(kickoff),
                "score": score,
                "status": status,
                "started": self.case.time,
                "received": self.case.time,
                "sha256": hashlib.sha256(str((score, status)).encode()).hexdigest(),
                "url": "https://example.test/match/734",
            }
        )

    def test_advanced_result_known_before_placeholder_without_rewriting_schedule_or_seal(
        self,
    ):
        actual = store.instant("2026-08-20T12:00Z")
        early_time = store.iso(actual + timedelta(hours=3))
        self.case.time = early_time
        row = self.case.rows[0] | {"matchDateTimeUTC": store.iso(actual)}
        self.case.collect([row])
        self.evidence(actual, [2, 1])
        report = self.case.reconcile()
        self.assertEqual(len(report["postmatch_pairings"]), 1)
        proof = admission.verified_regulation(self.root, self.fid)
        self.assertEqual(proof["kickoff"], store.iso(actual))
        self.assertNotEqual(proof["kickoff"], proof["registry_kickoff"])
        self.assertEqual(
            store.fixture_at(self.root, self.fid)["kickoff"], proof["registry_kickoff"]
        )
        self.assertIsNone(
            admission.verified_regulation(self.root, self.fid, at=store.iso(actual))
        )
        with closing(store.connect(self.root)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM fixture_versions WHERE fixture_id=?",
                    (self.fid,),
                ).fetchone()[0],
                1,
            )
            old = db.execute(
                "SELECT data_json FROM forecast_seals WHERE digest=?", (self.case.seal,)
            ).fetchone()
            self.assertEqual(store.digest(json.loads(old[0])), self.case.seal)
        view = input_manifest.verify_observed_inputs(self.root)
        matches = football.load_fixtures(
            view["root"], evidence_path=view["evidence_path"]
        )
        f = next(f for f in matches if f.fixture_id == self.fid)
        self.assertEqual(f.result_versions[-1].actual_kickoff, actual)
        self.assertEqual(football.history_kickoff(f, actual), f.kickoff)
        self.assertEqual(football.history_kickoff(f, store.instant(early_time)), actual)
        with self.assertRaisesRegex(ValueError, "already observed"):
            pipeline.make_forecast(
                self.root, self.fid, None, mode="technical", horizon="T-24h"
            )
        self.assertEqual(f.result_versions[-1].available, store.instant(early_time))
        engine = football.FootballEngine(matches)
        self.assertEqual(
            engine.fit("2026-27", store.instant(early_time), None).evidence[
                "training_matches"
            ],
            3,
        )
        self.assertEqual(
            engine.fit("2026-27", actual, None).evidence["training_matches"], 2
        )
        self.assertEqual(
            engine.fit("2026-27", store.instant(early_time), self.fid).evidence[
                "training_matches"
            ],
            2,
        )
        self.case.time = store.iso(actual + timedelta(hours=4))
        self.evidence(actual, [2, 2])
        self.assertIsNone(admission.verified_regulation(self.root, self.fid))
        self.assertEqual(
            self.case.reconcile()["quarantined"][0]["reason"],
            "cross_source_score_conflict",
        )
        self.assertEqual(
            admission.verified_regulation(self.root, self.fid, at=early_time)[
                "home_goals"
            ],
            2,
        )
        updated = input_manifest.verify_observed_inputs(self.root)
        f = next(
            f
            for f in football.load_fixtures(
                updated["root"], evidence_path=updated["evidence_path"]
            )
            if f.fixture_id == self.fid
        )
        self.assertIsNone(f.result_versions[-1].score)

    def test_cli_strict_replay_uses_historical_cutoff_and_requires_its_input_snapshot(
        self,
    ):
        self.case.time = store.iso("2026-08-21T15:00:00Z")
        late = pipeline.make_forecast(
            self.root, self.case.fids[1], None, horizon="early"
        )
        self.case.set_time(0)
        rows = deepcopy(self.case.rows)
        rows[1]["matchDateTimeUTC"] = "2026-08-21T12:00:00Z"
        self.case.collect(rows)
        a, b = self.case.fids
        self.evidence(rows[0]["matchDateTimeUTC"], [2, 1])
        witness = self.secondary[a][0]
        self.secondary[b] = [
            witness
            | {
                "kickoff": store.iso(rows[1]["matchDateTimeUTC"]),
                "score": [1, 0],
                "source_match_id": "epl:735",
            }
        ]
        self.assertEqual(self.case.reconcile()["available_results"], 2)
        view = input_manifest.verify_observed_inputs(self.root)
        self.case.set_time(60)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = cli.main(
                [
                    "--data-dir",
                    str(self.root),
                    "forecast",
                    "replay-round",
                    view["root"],
                    "2026-27",
                    "1",
                    "--evidence",
                    view["evidence_path"],
                ]
            )
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())["round_report"]
        self.assertEqual(report["count"], 1)
        result = store.evaluation_rows(
            self.root, report["model_version"], mode="replay"
        )
        forward = store.evaluation_rows(
            self.root, late["model_version"], horizon="early", mode="technical"
        )
        self.assertNotIn(b, [r["fixture_id"] for r in forward])
        self.assertEqual([r["fixture_id"] for r in result], [a])
        self.assertGreater(result[0]["sealed_at"], result[0]["result_kickoff"])
        self.assertLess(result[0]["cutoff"], result[0]["result_kickoff"])
        with closing(store.connect(self.root)) as db, db:
            row = db.execute(
                "SELECT * FROM forecast_seals WHERE mode='replay' AND fixture_id=?",
                (a,),
            ).fetchone()
            body = json.loads(row["data_json"])
            # A shape-correct generic seal cannot assert an unregistered snapshot.
            body["model_version"] = "generic"
            body["input_manifest_sha256"] = "f" * 64
            proof = body["comparison_proof"]
            proof["snapshot"] = body["input_manifest_sha256"]
            proof["sha256"] = store.digest(
                {k: v for k, v in proof.items() if k != "sha256"}
            )
            fake = dict(row) | {
                "model_version": "generic",
                "data_json": store.canonical(body),
                "digest": store.digest(body),
            }
            db.execute(
                "INSERT INTO forecast_seals VALUES (:fixture_id,:kickoff,:cutoff,:horizon,:model_version,"
                ":protocol,:mode,:sealed_at,:digest,:data_json)",
                fake,
            )
        self.assertEqual(store.evaluation_rows(self.root, "generic", mode="replay"), [])

    def test_football_commands_and_candidate_history_respect_verified_kickoff(self):
        true_start = store.instant("2026-08-20T12:00Z")
        self.case.set_time(0)
        self.case.collect(
            [self.case.rows[0] | {"matchDateTimeUTC": store.iso(true_start)}]
        )
        self.evidence(true_start, [2, 1])
        self.case.reconcile()
        view = input_manifest.verify_observed_inputs(self.root)
        history = football.load_fixtures(
            view["root"], evidence_path=view["evidence_path"]
        )
        game = next(f for f in history if f.fixture_id == self.fid)
        nominal_cutoff = game.kickoff - timedelta(hours=1)
        real_cutoff = true_start - timedelta(hours=1)
        predictions = self.root / "late.json"
        predictions.write_text(
            json.dumps(
                [
                    {
                        "fixture_id": self.fid,
                        "kickoff": store.iso(game.kickoff),
                        "cutoff": store.iso(nominal_cutoff),
                        "sealed_at": store.iso(nominal_cutoff),
                        "probabilities": {k: 0.5 for k in football.PROB_KEYS},
                    }
                ]
            )
        )

        def run(command, extra):
            out, error = io.StringIO(), io.StringIO()
            args = [
                "--data-dir",
                str(self.root),
                "football",
                command,
                view["root"],
                "--evidence",
                view["evidence_path"],
                *extra,
            ]
            with redirect_stdout(out), redirect_stderr(error):
                status = cli.main(args)
            return status, json.loads(
                out.getvalue()
            ) if out.getvalue() else error.getvalue()

        code, error = run(
            "predict",
            [
                "--home",
                game.home,
                "--away",
                game.away,
                "--season",
                game.season,
                "--kickoff",
                store.iso(game.kickoff),
                "--cutoff",
                store.iso(nominal_cutoff),
            ],
        )
        self.assertEqual(code, 2)
        self.assertIn(football.MATCH_STARTED, error)
        code, rnd = run(
            "round",
            [
                "--season",
                game.season,
                "--round",
                "1",
                "--predictions",
                str(predictions),
            ],
        )
        self.assertEqual(
            (code, rnd["completed"], rnd["missing"], rnd["scored"]), (0, 1, 1, 0)
        )
        omission = next(f for f in rnd["fixtures"] if f["fixture_id"] == self.fid)
        self.assertEqual(omission["reason"], football.UNKNOWN_RECONCILED_KICKOFF)
        self.assertEqual(omission["kickoff"], football.iso(true_start))
        code, study = run(
            "backtest",
            [
                "--from-season",
                game.season,
                "--to-season",
                game.season,
                "--horizons",
                "60",
                "--bootstrap",
                "0",
            ],
        )
        self.assertEqual(
            (
                code,
                study["fixtures"]["completed"],
                study["horizons"]["T-60"]["omissions"][
                    football.UNKNOWN_RECONCILED_KICKOFF
                ],
            ),
            (0, 1, 1),
        )
        with patch.object(
            historical_inputs,
            "load_for_study",
            return_value=(history, {"timing_authenticated": True, "reason": None}),
        ):
            candidate = football_candidates.study(
                self.root, view["root"], first=game.season, last=game.season
            )
        self.assertEqual(
            (
                candidate["denominator"],
                candidate["paired_n"],
                candidate["forecast_omissions"][football.UNKNOWN_RECONCILED_KICKOFF],
            ),
            (1, 0, 1),
        )
        # A genuine schedule correction received in time permits the correct historical horizon.
        changed = replace(
            game,
            schedule_versions=game.schedule_versions
            + ((real_cutoff - timedelta(days=1), true_start),),
            schedule_grades=game.schedule_grades + ("archived_schedule_upper_bound",),
        )
        good_history = [
            changed if f.fixture_id == game.fixture_id else f for f in history
        ]
        pred = football.FootballEngine(good_history).forecast(
            game.home, game.away, true_start, real_cutoff, season=game.season
        )
        self.assertEqual(pred["evidence"]["training_matches"], 2)
        graded = football.score_round(
            good_history,
            [pred | {"sealed_at": football.iso(real_cutoff)}],
            season=game.season,
            round_number=1,
        )
        self.assertEqual(graded["scored"], 1)
        unsealed = football.score_round(good_history, [pred], season=game.season, round_number=1)
        self.assertEqual((unsealed["completed"], unsealed["missing"]), (1, 1))
        details = []
        football.backtest(
            good_history,
            game.season,
            game.season,
            horizons=(60,),
            bootstrap=0,
            details_sink=details.append,
        )
        self.assertEqual(
            [(r["fixture_id"], r["kickoff"]) for r in details],
            [(self.fid, football.iso(true_start))],
        )

    def test_operator_withdrawal_persists_until_named_release_and_reverification(self):
        self.case.set_time(0)
        actual = self.case.rows[0]["matchDateTimeUTC"]
        self.case.collect(self.case.rows)
        self.evidence(actual, [2, 1])
        report = self.case.reconcile()
        digest = report["postmatch_pairings"][0]["proof_sha256"]
        self.case.set_time(5)
        with self.assertRaisesRegex(ValueError, "differs"):
            admission.review_pairing(
                self.root, 2026, self.fid, "0" * 64, "withdraw", "Jo", "wrong event"
            )
        admission.review_pairing(
            self.root,
            2026,
            self.fid,
            digest,
            "withdraw",
            "Jo",
            "club disputes match identity",
        )
        self.assertIsNone(admission.verified_regulation(self.root, self.fid))
        self.case.set_time(16)
        self.assertEqual(
            self.case.reconcile()["quarantined"][0]["reason"],
            "operator_withdrew_result_pairing",
        )
        self.assertIsNone(self.case.state(self.fid)["home_goals"])
        admission.review_pairing(
            self.root,
            2026,
            self.fid,
            digest,
            "release",
            "Jo",
            "ordered teams independently confirmed",
        )
        self.assertEqual(
            admission.verified_regulation(self.root, self.fid)["home_goals"], 2
        )

    def test_slow_primary_needs_two_distinct_secondary_receipts_and_tracks_withdrawal(
        self,
    ):
        self.case.set_time(0)
        row = self.case.rows[0] | {"matchIsFinished": False, "matchResults": []}
        actual = row["matchDateTimeUTC"]
        self.case.collect([row])
        self.evidence(actual, [2, 1])
        self.assertEqual(self.case.reconcile()["available_results"], 0)
        self.case.set_time(25)
        self.evidence(actual, [2, 1])
        self.assertEqual(self.case.reconcile()["available_results"], 0)
        self.case.set_time(32)
        self.evidence(actual, [2, 1])
        self.assertEqual(
            self.case.reconcile()["coverage"]["repeated_independent_final"], 1
        )
        self.assertEqual(
            admission.verified_regulation(self.root, self.fid)["home_goals"], 2
        )
        self.case.set_time(33)
        self.evidence(actual, None, status="withdrawn")
        self.assertIsNone(admission.verified_regulation(self.root, self.fid))
        self.assertEqual(self.case.reconcile()["available_results"], 0)
        self.case.set_time(35)
        self.evidence(actual, [2, 1])
        self.assertEqual(self.case.reconcile()["available_results"], 0)

    def test_ambiguous_native_fixture_quarantines_only_the_affected_game(self):
        self.case.set_time(0)
        duplicates = [*self.case.rows, self.case.rows[0] | {"matchID": 450}]
        self.case.collect(duplicates, season=True)
        self.case.set_time(31)
        self.case.collect(duplicates)
        report = self.case.reconcile()
        self.assertEqual(report["available_results"], 1)
        self.assertEqual(
            [(x["fixture_id"], x["reason"]) for x in report["quarantined"]],
            [(self.fid, "ambiguous_source_fixture")],
        )

    def test_manual_decision_survives_the_known_reference_but_a_new_score_reopens_it(
        self,
    ):
        self.case.set_time(0)
        first = self.case.time
        self.case.collect(self.case.rows)
        row = self.case.rows[0]
        attachment = self.root / "matchsheet.txt"
        attachment.write_text("Signed supplement. The final score was 2–1.")
        entry = {
            "home": row["team1"]["teamName"],
            "away": row["team2"]["teamName"],
            "kickoff": row["matchDateTimeUTC"],
            "status": "final",
            "period": "regulation",
            "locator": "signed supplement",
            "home_goals": 2,
            "away_goals": 1,
        }
        batch = {
            "season": "2026-27",
            "round": 1,
            "rights": "permission",
            "reviewer": "Analyst",
            "source": "signed club report",
            "evidence_file": attachment.name,
            "evidence_sha256": hashlib.sha256(attachment.read_bytes()).hexdigest(),
            "matches": [entry],
        }
        file = self.root / "batch.json"
        file.write_text(json.dumps(batch))
        with patch.object(
            admission,
            "_reference",
            side_effect=lambda db, root, season, at: {
                self.fid: [2, 2] if at <= first else [3, 3]
            },
        ):
            with self.assertRaisesRegex(ValueError, "decision_reason"):
                ops.manual(self.root, 2026, 1, file, whole_round=False)
            entry["decision_reason"] = (
                "The signed supplement corrects the initial match sheet."
            )
            file.write_text(json.dumps(batch))
            ops.manual(self.root, 2026, 1, file, whole_round=False)
            self.assertEqual(self.case.reconcile()["coverage"]["manual_reviewed"], 1)
            self.assertIsNone(admission.verified_regulation(self.root, self.fid))
            self.case.set_time(5)
            self.assertEqual(
                self.case.reconcile()["quarantined"][0]["reason"],
                "manual_source_disagreement",
            )
            self.assertIsNone(self.case.state(self.fid)["home_goals"])

    def test_partial_manual_uses_real_kickoff_and_reverses_one_game(self):
        self.case.set_time(0)
        actual = store.iso("2026-08-21T17:00Z")
        row = self.case.rows[0] | {"matchDateTimeUTC": actual}
        self.case.collect([row])
        attachment = self.root / "matchsheet.txt"
        attachment.write_text("Fixture 10, 17:00 UTC, regulation 2–1")
        report = {
            "season": "2026-27",
            "round": 1,
            "rights": "permission",
            "reviewer": "Analyst",
            "source": "club report",
            "evidence_file": attachment.name,
            "evidence_sha256": hashlib.sha256(attachment.read_bytes()).hexdigest(),
            "matches": [
                {
                    "home": row["team1"]["teamName"],
                    "away": row["team2"]["teamName"],
                    "kickoff": actual,
                    "status": "final",
                    "period": "regulation",
                    "locator": "fixture 10",
                    "home_goals": 2,
                    "away_goals": 1,
                    "decision_reason": "confirmed changed time on the signed report",
                }
            ],
        }
        file = self.root / "match.json"
        wrong = deepcopy(report)
        wrong["matches"][0]["kickoff"] = self.case.state(self.fid)["kickoff"]
        file.write_text(json.dumps(wrong))
        with self.assertRaisesRegex(ValueError, "kickoff"):
            ops.manual(self.root, 2026, 1, file, whole_round=False)
        file.write_text(json.dumps(report))
        applied = ops.manual(self.root, 2026, 1, file, whole_round=False)
        self.assertEqual(
            (applied["scope"], applied["items"][0]["actual_kickoff"]),
            ("selected_matches", actual),
        )
        self.assertIsNone(admission.verified_regulation(self.root, self.fid))
        self.assertIsNone(self.case.state(self.case.fids[1])["home_goals"])
        with self.assertRaisesRegex(ValueError, "whole"):
            feed.import_round(self.root, 2026, 1, file)
        self.case.set_time(10)
        self.assertEqual(
            feed.rollback_batch(self.root, applied["batch_id"], "withdrawn report")[
                "count"
            ],
            1,
        )
        self.assertIsNone(self.case.state(self.fid)["home_goals"])


class LargeScheduleReview(unittest.TestCase):
    def test_operator_can_verify_postponement_after_old_slot_but_before_new_deadline(
        self,
    ):
        c = schedule_case.ProspectiveSchedule()
        c.setUp()
        self.addCleanup(c.doCleanups)
        c.time = schedule_case.OLD + timedelta(days=3)
        late = c.time + timedelta(days=5)
        c.ol(late)
        c.gamma(late)
        report = schedule.sync(c.root, schedule_case.YEAR)
        self.assertEqual(report["corrected"], 0)
        self.assertTrue(
            any(r["fixture_id"] == schedule_case.FID for r in report["late_skipped"])
        )
        new = schedule.review_schedule(
            c.root,
            schedule_case.YEAR,
            schedule_case.FID,
            late,
            "fixture coordinator",
            "the two published sources agree on the future fixture",
        )
        self.assertEqual(new["known_at"], store.iso(c.time))
        old = store.fixture_at(
            c.root, schedule_case.FID, at=store.iso(schedule_case.OLD), prospective=True
        )
        self.assertEqual(old["kickoff"], store.iso(schedule_case.OLD))
        c.time = late - timedelta(minutes=29)
        proposed = late - timedelta(minutes=5)
        c.ol(proposed)
        c.gamma(proposed)
        with self.assertRaises(ValueError):
            schedule.review_schedule(
                c.root,
                schedule_case.YEAR,
                schedule_case.FID,
                proposed,
                "reviewer",
                "late change",
            )

    def test_large_review_checks_current_sources_and_keeps_audited_withdrawal(self):
        c = schedule_case.ProspectiveSchedule()
        c.setUp()
        self.addCleanup(c.doCleanups)
        year, fid = schedule_case.YEAR, schedule_case.FID
        far = schedule_case.OLD + timedelta(days=19)
        c.ol(far)
        c.gamma(far)
        rejected = schedule.sync(c.root, year)["items"][0]
        self.assertIn("36-hour", rejected["reason"])
        self.assertEqual(rejected["openligadb_kickoff"], rejected["gamma_kickoffs"][0])
        with self.assertRaisesRegex(ValueError, "differs"):
            schedule.review_schedule(
                c.root, year, fid, schedule_case.OLD, "operator", "native agreement"
            )
        accepted = schedule.review_schedule(
            c.root,
            year,
            fid,
            far,
            "operator",
            "two native sources support postponement",
        )
        self.assertEqual(accepted["status"], "corrected")
        approved_at = store.now_utc()
        with closing(store.connect(c.root)) as db:
            proof = json.loads(
                db.execute("SELECT proof FROM epl_schedule_proofs").fetchone()[0]
            )
            self.assertEqual(proof["approval"]["reviewer"], "operator")
        c.time += timedelta(minutes=5)
        withdrawn = schedule.review_schedule(
            c.root,
            year,
            fid,
            far,
            "operator",
            "event called into question",
            withdraw=True,
        )
        self.assertEqual(withdrawn["action"], "withdrawn")
        with self.assertRaisesRegex(ValueError, "withdrawn"):
            store.fixture_at(c.root, fid, prospective=True)
        self.assertEqual(
            store.fixture_at(c.root, fid, at=approved_at, prospective=True)["kickoff"],
            store.iso(far),
        )
        c.time += timedelta(minutes=1)
        schedule.sync(c.root, year)
        with self.assertRaisesRegex(ValueError, "withdrawn"):
            store.fixture_at(c.root, fid, prospective=True)
        self.assertEqual(c.counts(), 2)
