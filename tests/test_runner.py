"""Real forecast entry point driven by a controlled clock and synthetic EPL fixtures."""

import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, closing, contextmanager, redirect_stderr
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket861 import datasets, football, input_manifest, pipeline, runner, schedule_consensus, store


@contextmanager
def clock(value):
    with ExitStack() as stack:
        for module in (store,pipeline,datasets,input_manifest):
            stack.enter_context(patch.object(module,"now_utc",return_value=store.iso(value)))
        yield


class RunnerTests(unittest.TestCase):
    kickoff = "2030-08-20T15:00:00Z"
    known = "2030-08-01T12:00:00Z"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)/"data"
        self.root = Path(temp.name)/"source"
        (self.root/"2030-31").mkdir(parents=True)

    def fixtures(self, count=1):
        games = [{"round":"Matchday 1","date":"2030-08-10","time":"16:00","team1":"Historic H","team2":"Historic A","score":{"ft":[1,1]}}]
        registered = []
        for n in range(count):
            home, away = f"Home {n}", f"Away {n}"
            games.append({"round":"Matchday 1","date":"2030-08-20","time":"16:00","team1":home,"team2":away,"score":{}})
            with clock(self.known):
                registered.append(store.register_fixture(self.data,{"fixture_id":football.fixture_id("2030-31",home,away),
                    "season":"2030-31","round_id":1,"home":home,"away":away,"kickoff":self.kickoff,"known_at":self.known,
                    "source":"local fixture","locator":f"test://schedule/{n}"}))
        path=self.root/"2030-31"/"en.1.json"
        path.write_text(json.dumps({"matches":games}))
        # Exercise the ordinary verified local observation path before either decision cutoff.
        with clock("2030-08-11T09:00Z"):
            captured=datasets.observe(self.data,self.root,revision="a"*40,expected={"2030-31":football.blob_sha(path.read_bytes())})
        self.root=Path(captured["fixture_root"])
        self.evidence=Path(captured["evidence_path"])
        return registered

    def dispatch(self, **kwargs):
        return runner.run_due(self.data,self.root,evidence_path=self.evidence,**kwargs)

    def test_evidence_churn_retains_two_retry_levels_and_seal_margin(self):
        self.fixtures()
        original = schedule_consensus.fixed_schedules

        def invalidate_after_read(*args, **kwargs):
            checks = original(*args, **kwargs)
            with closing(store.connect(self.data)) as other_writer, other_writer:
                other_writer.execute("UPDATE epl_schedule_epoch SET revision=revision+1 WHERE id=1")
            time.sleep(.012)
            return checks

        log = io.StringIO()
        with clock("2030-08-19T14:58Z"), redirect_stderr(log), patch.object(
            schedule_consensus, "fixed_schedules", side_effect=invalidate_after_read
        ):
            failed = self.dispatch()["jobs"][0]
        self.assertEqual(failed["state"], "error")
        timing = failed["schedule_verification"]
        self.assertEqual((timing["dispatch_retries"], timing["invalidated_snapshots"], timing["reverifications"]), (0, 3, 2))
        retried = [row for row in timing["checks"] if row["phase"] == "reverification"]
        self.assertTrue(all(row["elapsed_ms"] >= 10 for row in retried))
        self.assertEqual(timing["finish_margin_seconds"], 120)
        self.assertIsNone(timing["seal_margin_seconds"])
        self.assertEqual(timing["next_retry_at"], store.iso("2030-08-19T14:58:02Z"))
        with clock(timing["next_retry_at"]), redirect_stderr(log):
            success = self.dispatch()["jobs"][0]
        self.assertEqual(success["state"], "done")
        self.assertEqual(success["schedule_verification"]["dispatch_retries"], 1)
        self.assertEqual(success["schedule_verification"]["seal_margin_seconds"], 118)
        self.assertIsNone(success["schedule_verification"]["next_retry_at"])
        journal = [json.loads(line) for line in log.getvalue().splitlines()]
        self.assertEqual([row["event"] for row in journal], ["epl_fixed_schedule_attempt"] * 2)
        self.assertEqual([row["state"] for row in journal], ["error", "done"])
        status = runner.runner_status(self.data)
        self.assertEqual(status["attempts"][1]["schedule_verification"], timing)
        self.assertEqual(status["last_schedule_preflight"]["fixture_count"], 1)
        with closing(store.connect(self.data)) as db:
            sealed = db.execute("SELECT digest,data_json FROM forecast_seals").fetchall()
        self.assertEqual(len(sealed), 1)
        self.assertEqual(store.digest(json.loads(sealed[0]["data_json"])), success["digest"])
        self.assertNotIn("schedule_verification", json.loads(sealed[0]["data_json"]))

    def test_expired_window_keeps_last_error_and_late_worker_diagnosis(self):
        games = self.fixtures(2)
        slots = [runner._slot(g, "T-24h", "technical", "B1") for g in games]
        error = "schedule evidence changed repeatedly before the fixed B1 decision"
        with clock("2030-08-19T14:59:55Z"):
            claims = [runner._claim(self.data, s) for s in slots]
            self.assertEqual(runner._finish(self.data, slots[0], claims[0], None, error)["state"], "error")
        with clock("2030-08-19T15:00:02Z"), redirect_stderr(io.StringIO()):
            missed = runner._claim(self.data, slots[0])
            self.assertIn(error, missed["error"])
            late = runner._finish(
                self.data, slots[1], claims[1], None, "calculation finished after its sealing deadline",
                {"shared_preflight": None, "checks": []},
            )
            self.assertEqual((late["state"], late["schedule_verification"]["finish_margin_seconds"]), ("missed", -2))
            self.assertEqual(runner._claim(self.data, slots[1])["state"], "already_missed")
        with closing(store.connect(self.data)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM forecast_seals").fetchone()[0], 0)

    def test_shared_preflight_error_survives_failed_dispatch(self):
        game = self.fixtures()[0]
        log = io.StringIO()
        with clock("2030-08-19T14:58Z"), redirect_stderr(log), patch.object(
            schedule_consensus, "fixed_schedules", side_effect=OSError("raw receipt is unavailable")
        ), self.assertRaisesRegex(OSError, "raw receipt"):
            self.dispatch()
        last = runner.runner_status(self.data)["last_schedule_preflight"]
        self.assertEqual(last["fixture_ids"], [game["fixture_id"]])
        self.assertEqual(last["checks"][-1]["outcome"], "error")
        self.assertIn("raw receipt", last["error"])
        self.assertEqual(json.loads(log.getvalue())["event"], "epl_fixed_schedule_preflight_failed")
        with clock("2030-08-19T15:00:01Z"):
            missed = self.dispatch()["jobs"][0]
        self.assertEqual(missed["state"], "missed")
        self.assertIn("schedule preflight failed: OSError: raw receipt", missed["error"])

    def test_late_obsolete_attempt_cannot_replace_another_workers_lease(self):
        slot = runner._slot(self.fixtures()[0], "T-24h", "technical", "B1")
        with clock("2030-08-19T14:58Z"):
            first = runner._claim(self.data, slot)
        with clock("2030-08-19T14:59Z"):
            second = runner._claim(self.data, slot)
        self.assertEqual(second["attempt"], 2)
        with clock("2030-08-19T15:00:01Z"), redirect_stderr(io.StringIO()):
            previous = runner._finish(self.data, slot, first, None, "first worker finished late",
                                      {"shared_preflight": None, "checks": []})
            self.assertEqual(previous["state"], "stale_attempt")
            current = runner.runner_status(self.data)
            self.assertEqual(current["jobs"][0]["state"], "running")
            self.assertEqual(current["attempts"][1]["state"], "expired")
            self.assertEqual(current["attempts"][1]["schedule_verification"]["state"], "stale_attempt")
            finished = runner._finish(self.data, slot, second, None, "second worker finished late")
            self.assertEqual((finished["state"], finished["error"]), ("missed", "second worker finished late"))

    def test_actual_technical_seal_each_horizon_idempotent_and_late_miss(self):
        self.fixtures()
        with clock("2030-08-19T14:54Z"):
            early=self.dispatch()
            self.assertEqual(early["counts"],{})
            self.assertEqual(early["next_window_opens"],store.iso("2030-08-19T14:55Z"))
        with clock("2030-08-19T14:58Z"):
            run=self.dispatch()
            self.assertEqual(run["counts"],{"done":1})
            self.assertEqual(run["jobs"][0]["mode"],"technical")
            self.assertEqual(self.dispatch()["counts"],{"already_done":1})
        with clock("2030-08-20T14:00:01Z"), patch.object(pipeline,"make_forecast",side_effect=AssertionError("late dispatch")):
            missed=self.dispatch()
            self.assertEqual(missed["counts"],{"already_done":1,"missed":1})
            self.assertEqual(self.dispatch()["counts"],{"already_done":1,"already_missed":1})
        with closing(store.connect(self.data)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM forecast_seals").fetchone()[0],1)
        self.assertEqual(len(runner.runner_status(self.data)["attempts"]),1)

    def test_superseded_schedule_keeps_old_seal_and_new_time_gets_its_own(self):
        game=self.fixtures()[0]
        with clock("2030-08-19T14:58Z"):
            old=self.dispatch()["jobs"][0]
        revised=store.iso(store.instant(self.kickoff)+timedelta(weeks=1))
        with clock("2030-08-20T09:00Z"):
            store.register_fixture(self.data,game|{"kickoff":revised,"known_at":store.now_utc(),"locator":"test://rescheduled"})
            observed=self.dispatch()
            self.assertEqual(observed["superseded"],[old["key"]])
            self.assertEqual(observed["counts"],{})
        with clock("2030-08-26T14:59Z"):
            new=self.dispatch()["jobs"][0]
        self.assertEqual(new["state"],"done")
        self.assertNotEqual(old["digest"],new["digest"])
        states={r["key"]:r for r in runner.runner_status(self.data)["jobs"]}
        self.assertEqual(states[old["key"]]["current_schedule"],0)
        self.assertEqual(states[new["key"]]["current_schedule"],1)

    def test_parallel_same_time_isolates_error_and_retries_inside_window(self):
        games=self.fixtures(2)
        normal=pipeline.make_forecast
        barrier=threading.Barrier(2,timeout=4)
        def once(data,fixture,root,**kwargs):
            barrier.wait()
            if fixture==games[0]["fixture_id"]:
                raise OSError("fixture source temporarily unavailable")
            return normal(data,fixture,root,**kwargs)
        with clock("2030-08-20T13:58:30Z"),patch.object(pipeline,"make_forecast",side_effect=once):
            first=self.dispatch(workers=2)
            self.assertEqual(first["counts"],{"done":1,"error":1,"missed":2})
        with clock("2030-08-20T13:59Z"):
            retry=self.dispatch(workers=2)
        self.assertEqual(retry["counts"]["done"],1)
        self.assertEqual(retry["counts"]["already_done"],1)
        attempts=runner.runner_status(self.data)["attempts"]
        self.assertCountEqual([r["state"] for r in attempts],["done","done","error"])

    def test_recovery_and_current_worker_reject_a_mismatched_seal_clock(self):
        game = self.fixtures()[0]
        with clock("2030-08-19T14:58Z"):
            slot = runner._slot(game, "T-24h", "technical", "B1")
            claim = runner._claim(self.data, slot)
            seal = pipeline.make_forecast(self.data, game["fixture_id"], self.root, version="B1",
                                          mode="technical", horizon="T-24h", evidence_path=self.evidence)
            with closing(store.connect(self.data)) as db, db:
                db.execute("UPDATE forecast_seals SET sealed_at=? WHERE digest=?",
                           (store.iso("2030-08-19T15:01Z"), seal["digest"]))
            with self.assertRaisesRegex(ValueError, "identity or clock mismatch"):
                runner._finish(self.data, slot, claim, seal["digest"], None, {"checks": [], "shared_preflight": None})
            with self.assertRaisesRegex(ValueError, "identity or clock mismatch"):
                runner._claim(self.data, slot)

    def test_reconcile_existing_seal_after_crash_and_select_formal_after_activation(self):
        game=self.fixtures()[0]
        active_at=store.iso("2030-08-20T13:00Z")
        with closing(store.connect(self.data)) as db, db:
            db.execute("INSERT INTO trial_activations VALUES (?,?,?,?)",(store.PROTOCOL,active_at,active_at,"{}"))
        with clock("2030-08-20T13:59Z"):
            sealed=pipeline.make_forecast(self.data,game["fixture_id"],self.root,version="B1",mode="formal",evidence_path=self.evidence)
        with clock("2030-08-20T14:01Z"):
            report=self.dispatch()
        resolved=next(x for x in report["jobs"] if x["horizon"]=="T-60m")
        self.assertEqual((resolved["mode"],resolved["state"],resolved["digest"]),("formal","reconciled",sealed["digest"]))
        missed=next(x for x in report["jobs"] if x["horizon"]=="T-24h")
        self.assertEqual((missed["mode"],missed["state"]),("technical","missed"))

    def test_concurrent_local_schedulers_claim_one_decision(self):
        self.fixtures()
        normal=pipeline.make_forecast
        entered=threading.Event()
        release=threading.Event()
        def delayed(*args,**kwargs):
            entered.set()
            self.assertTrue(release.wait(4))
            return normal(*args,**kwargs)
        with clock("2030-08-19T14:59Z"),patch.object(pipeline,"make_forecast",side_effect=delayed) as predict:
            with runner.ThreadPoolExecutor(2) as pool:
                first=pool.submit(self.dispatch)
                self.assertTrue(entered.wait(4))
                try:
                    second=self.dispatch()
                    self.assertEqual(second["counts"],{"in_progress":1})
                finally:
                    release.set()
                self.assertEqual(first.result()["counts"],{"done":1})
            self.assertEqual(predict.call_count,1)

    def test_date_only_waits_and_latest_locally_verified_manifest_is_default(self):
        game=self.fixtures()[0]
        with clock(self.known):
            store.register_fixture(self.data,game|{"fixture_id":"unknown-time","home":"New H","away":"New A","precision":"date"})
        with clock("2030-08-19T14:57Z"):
            run=runner.run_due(self.data)
            self.assertEqual(run["awaiting_exact_kickoff"],["unknown-time"])
            self.assertEqual(run["counts"],{"done":1})


if __name__=="__main__":
    unittest.main()
