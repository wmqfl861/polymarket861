"""Real forecast entry point driven by a controlled clock and synthetic EPL fixtures."""

import json
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing, contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket861 import datasets, football, input_manifest, pipeline, runner, store


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
        with clock("2030-08-20T13:56Z"),patch.object(pipeline,"make_forecast",side_effect=once):
            first=self.dispatch(workers=2)
            self.assertEqual(first["counts"],{"done":1,"error":1,"missed":2})
        with clock("2030-08-20T13:57Z"):
            retry=self.dispatch(workers=2)
        self.assertEqual(retry["counts"]["done"],1)
        self.assertEqual(retry["counts"]["already_done"],1)
        attempts=runner.runner_status(self.data)["attempts"]
        self.assertCountEqual([r["state"] for r in attempts],["done","done","error"])

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
