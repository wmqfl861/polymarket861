"""Queue recovery, isolated parallel work, and independently judged paper experiments."""

import argparse
import json
import shutil
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from polymarket861 import evidence, jobs, jobs_research as research, store
from polymarket861.pipeline import register_recipe


class TempStore(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)


class QueueTests(TempStore):
    def test_restored_job_directory_and_dependent_reads_are_portable(self):
        source=self.root/"original"
        restored=self.root/"restored"
        jobs.enqueue_job(source,"first",task="inspect")
        jobs.run_workers(source)
        original=jobs.jobs_status(source)["jobs"][0]
        shutil.copytree(source,restored)
        shutil.rmtree(source)
        status=jobs.jobs_status(restored)
        output=Path(status["jobs"][0]["output"])
        self.assertTrue(output.is_relative_to(restored))
        self.assertEqual(output,Path(status["attempts"][0]["output_dir"]))
        self.assertTrue((output/"result.json").is_file())
        self.assertEqual(status["jobs"][0]["result"]["artifacts"],original["result"]["artifacts"])
        jobs.enqueue_job(restored,"dependent",task="review",dependencies=["first"])
        self.assertEqual(jobs.claim_job(restored,worker="new-host")["prerequisite_results"]["first"]["output_dir"],str(output))

    def test_duplicate_dependency_and_fencing_after_crash(self):
        self.assertTrue(jobs.enqueue_job(self.root,"fixture",task="inspect rosters")["created"])
        self.assertFalse(jobs.enqueue_job(self.root,"fixture",task="inspect rosters")["created"])
        with self.assertRaisesRegex(ValueError,"immutable"):
            jobs.enqueue_job(self.root,"fixture",task="change target")
        jobs.enqueue_job(self.root,"round",task="review",dependencies=["fixture"])
        with patch("polymarket861.jobs.time.time",return_value=100):
            old = jobs.claim_job(self.root,worker="one",lease_seconds=5)
            self.assertIsNone(jobs.claim_job(self.root,worker="two",lease_seconds=5))
        with patch("polymarket861.jobs.time.time",return_value=106):
            fresh = jobs.claim_job(self.root,worker="two",lease_seconds=5)
            self.assertFalse(jobs.renew_lease(self.root,old,5))
            self.assertFalse(jobs.finish_job(self.root,old,result={"bad":True}))
            self.assertNotEqual(old["output_dir"],fresh["output_dir"])
            self.assertTrue(jobs.finish_job(self.root,fresh,result={"sha":"verified"}))
            dep = jobs.claim_job(self.root,worker="two")
            self.assertEqual(dep["prerequisite_results"],{"fixture":{"sha":"verified"}})
            self.assertTrue(jobs.finish_job(self.root,dep,result={"round":1}))
        self.assertCountEqual([a["state"] for a in jobs.jobs_status(self.root)["attempts"]],["expired","done","done"])

    def test_retry_preserves_failed_attempt(self):
        jobs.enqueue_job(self.root,"broken",task="test",max_attempts=1)
        first = jobs.claim_job(self.root,worker="worker")
        self.assertTrue(jobs.finish_job(self.root,first,error="transient"))
        self.assertIsNone(jobs.claim_job(self.root,worker="worker"))
        jobs.retry_job(self.root,"broken")
        self.assertEqual(jobs.run_workers(self.root)["attempts"][0]["attempt"],2)
        self.assertCountEqual([a["state"] for a in jobs.jobs_status(self.root)["attempts"]],["done","failed"])

    def test_transient_failure_retries_automatically_during_drain(self):
        jobs.enqueue_job(self.root,"retryable",task="audit",max_attempts=2)
        normal=jobs._execute
        def temporary(job,binary,timeout):
            if job["attempts"]==1:
                raise OSError("temporarily unavailable")
            return normal(job,binary,timeout)
        with patch.object(jobs,"_execute",side_effect=temporary):
            result=jobs.run_workers(self.root)
        self.assertEqual([r["attempt"] for r in result["attempts"]],[1,2])
        self.assertEqual(jobs.jobs_status(self.root)["jobs"][0]["state"],"done")

    def test_parallel_ready_jobs_and_offline_codex(self):
        for name in ("clubs", "weather"):
            jobs.enqueue_job(self.root,name,task="audit",inputs={"records":[{"at":1}, {}],"required_fields":["at"]})
        jobs.enqueue_job(self.root,"round",task="review",dependencies=["clubs","weather"])
        jobs.enqueue_job(self.root,"proposal",task="draft independently",kind="codex")
        barrier = threading.Barrier(2, timeout=4)
        execute = jobs._execute
        def synchronized(job,binary,timeout):
            if job["key"] in ("clubs","weather"):
                barrier.wait()
            return execute(job,binary,timeout)
        with patch.object(jobs,"_execute",side_effect=synchronized), patch.object(jobs.shutil,"which",return_value=None):
            run = jobs.run_workers(self.root,workers=2,allow_codex=True)
        self.assertEqual(len(run["attempts"]),3)
        self.assertEqual(run["remaining"],1)
        entries = {j["key"]:j for j in jobs.jobs_status(self.root)["jobs"]}
        self.assertEqual(entries["proposal"]["attempts"],0)
        self.assertEqual(entries["clubs"]["result"]["missing_by_field"],{"at":1})
        self.assertNotEqual(entries["clubs"]["output"],entries["weather"]["output"])
        deps = json.loads((Path(entries["round"]["output"])/"input.json").read_text())["dependencies"]
        self.assertEqual(set(deps),{"clubs","weather"})

    def test_real_external_process_uses_attempt_directory_and_sandbox_arguments(self):
        from test_research_disclosure import controlled_cli, note
        from pathlib import Path

        executable=controlled_cli(self.root,note())
        jobs.enqueue_job(self.root,"assist",kind="codex",task="inspect input")
        with patch.object(jobs.shutil,"which",return_value=str(executable)), patch.dict("os.environ",{"GH_TOKEN":"do-not-pass"}):
            result=jobs.run_workers(self.root,allow_codex=True)
        self.assertEqual(result["attempts"][0]["outcome"],"done")
        job=jobs.jobs_status(self.root)["jobs"][0]
        folder=Path(job["output"])
        invocation=json.loads((folder/"codex.jsonl").read_text())
        self.assertFalse(Path(invocation["cwd"]).is_relative_to(self.root))
        self.assertEqual(invocation["topic"],"discovery")
        self.assertEqual(job["result"]["research_isolation"]["os_probe"],"passed")


def season_games(year, rounds):
    start = datetime(year,8,20,15,tzinfo=timezone.utc)
    season = f"{year}-{(year+1)%100:02}"
    games = []
    for rnd in range(1,rounds+1):
        for j in range(10):
            kick = start + timedelta(weeks=rnd-1,minutes=j)
            games.append({"fixture_id":f"{season}/{rnd}/{j}","season":season,"round_id":rnd,"home":f"H{j}","away":f"A{j}",
                          "kickoff":store.iso(kick),"received_at":store.iso(kick+timedelta(hours=2)),
                          "home_goals":2,"away_goals":1,"result_version":1})
    return games


from polymarket861 import paired_proof

class ResearchTests(TempStore):
    registered = "2030-07-01T09:00:00.000000Z"
    judged = "2031-05-25T09:00:00.000000Z"

    def register_model(self, key="candidate", **overrides):
        spec = {"kind":"model","version":"B2-trial","baseline":"B1","window":"2030-31/1", "hypothesis":"availability improves the four scores"}
        with patch.object(store,"now_utc",return_value=self.registered):
            register_recipe(self.root,overrides.get("version",spec["version"]),"B2")
            return jobs.register_candidate(self.root,key,**(spec | overrides))

    def test_registration_is_immutable_and_shadow_without_trusted_evidence(self):
        claim = {"score":100,"risk_passed":True,"forward_matches":999}
        self.register_model(manifest=claim)
        self.assertEqual(self.register_model(manifest=claim)["state"],"shadow")
        with self.assertRaisesRegex(ValueError,"cannot be edited"):
            self.register_model(manifest={"score":99})
        result = jobs.evaluate_candidate(self.root,"candidate")
        self.assertEqual((result["state"],result["assessment"]["reason"]),("shadow","incomplete_official_schedule"))
        self.assertIsNone(jobs.active_version(self.root))
        self.register_model("second",version="X2")
        self.register_model("third",version="X3")
        with self.assertRaisesRegex(ValueError,"three"):
            self.register_model("fourth",version="X4")
        with self.assertRaisesRegex(ValueError,"38 official rounds"):
            self.register_model("shift",version="Shift",window="2030-31/2")
        with self.assertRaisesRegex(ValueError,"deadline"):
            with patch.object(store,"eligible_fixtures",return_value=season_games(2024,12)):
                self.register_model("retro",version="Retro",window="2024-25/1")

    def predictions(self, games, version, mode, *, missing=0, weak=False):
        p = [1.,0.,0.,1.,1.,0.] if version != "B1" and not weak else [.34,.33,.33,.5,.5,.5]
        keys = ("home_win","draw","away_win","total_over_2_5","home_over_1_5","away_over_1_5")
        with closing(store.connect(self.root)) as db:
            checksum=store.digest(json.loads(db.execute("SELECT recipe FROM model_recipes WHERE version=?",(version,)).fetchone()[0]))
        rows=[]
        for f in games[missing:]:
            cutoff=store.iso(store.instant(f["kickoff"])-timedelta(hours=1))
            rows.append(f | {"model_version":version,"mode":mode,"recipe_digest":checksum,"cutoff":cutoff,
                             "sealed_at":cutoff,"comparison_proof":{"snapshot":"one synthetic assessment batch"},"probabilities":dict(zip(keys,p)),"digest":store.digest([version,f["fixture_id"],p])})
        return rows

    # These tests isolate the statistical gate using synthetic scorer rows.
    # Source authentication is exercised through real seals in test_paired_proof.
    @patch.object(paired_proof.PairValidator,"check",new=lambda *_:None)
    def test_real_window_gate_coverage_simultaneous_reveal_and_rollback(self):
        history=season_games(2028,38)+season_games(2029,38)
        forward=season_games(2030,38)
        schedule=history+forward
        def rows(_,version,*,mode,**kwargs):
            return self.predictions(history if mode=="replay" else forward,version,mode,weak=version=="tie")
        audit={"passed":True,"checked_intents":2,"checked_fills":1,"accounts":[{"account":"B2-trial","mode":"formal","role":"candidate",
               "candidate_key":"candidate","candidate_version":"B2-trial"}]}
        with patch.object(store,"eligible_fixtures",return_value=schedule), patch.object(store,"evaluation_rows",side_effect=rows), \
                patch.object(store,"require_formal",return_value={}), patch.object(research,"_paper_audit",return_value=audit):
            registered=self.register_model()
            audit["accounts"][0]["registration_digest"]=registered["digest"]
            self.register_model("equal",version="tie")
            with patch.object(store,"now_utc",return_value="2030-08-21T15:00:00.000000Z"):
                pending=jobs.evaluate_candidate(self.root,"candidate")
                self.assertEqual(pending["assessment"]["reason"],"awaiting_official_results")
                self.assertNotIn("forward",pending["assessment"])
            with patch.object(store,"now_utc",return_value=self.judged):
                audit["accounts"][0]["mode"]="historical"
                held=jobs.evaluate_candidate(self.root,"candidate")
                self.assertEqual(held["assessment"]["reason"],"paper_risk_unverified")
                self.assertEqual(held["state"],"shadow")
                audit["accounts"][0]["mode"]="formal"
                tie=jobs.evaluate_candidate(self.root,"equal")
                self.assertEqual(tie["state"],"shadow")
                good=jobs.evaluate_candidate(self.root,"candidate")
                self.assertEqual(good["state"],"recommended")
                self.assertIsNone(jobs.active_version(self.root))
                with self.assertRaisesRegex(ValueError,"named reviewer"):
                    jobs.approve_candidate(self.root,"candidate",reviewer=" ",reason="examined both arms")
                audit["accounts"][0]["mode"]="historical"
                self.assertEqual(jobs.approve_candidate(self.root,"candidate",reviewer="league analyst",reason="review")["state"],"shadow")
                self.assertIsNone(jobs.active_version(self.root))
                audit["accounts"][0]["mode"]="formal"
                self.assertEqual(jobs.evaluate_candidate(self.root,"candidate")["state"],"recommended")
                good=jobs.approve_candidate(self.root,"candidate",reviewer="league analyst",reason="checked the registered comparison")
                self.assertEqual(good["state"],"promoted")
                self.assertEqual(good["assessment"]["approved_by"]["reviewer"],"league analyst")
                self.assertEqual(good["assessment"]["forward"]["paired_count"],380)
                self.assertAlmostEqual(good["assessment"]["forward"]["inference"]["individual_confidence"],.9975)
                self.assertEqual(jobs.active_candidate(self.root)["key"],"candidate")
                self.assertIsNone(jobs.active_version(self.root,at=self.registered))
            with patch.object(store,"now_utc",return_value="2031-05-26T09:00:00.000000Z"):
                jobs.rollback_candidate(self.root,"candidate",reason="fixture mapping revised",reviewer="league analyst")
                self.assertEqual(jobs.active_version(self.root),"B1")
                self.assertEqual(jobs.active_version(self.root,at=self.judged),"B2-trial")
                self.assertEqual(jobs.evaluate_candidate(self.root,"candidate")["state"],"rolled_back")
        self.assertEqual([e["action"] for e in jobs.experiment_status(self.root)["events"]].count("promoted"),1)

    # These tests isolate the statistical gate using synthetic scorer rows.
    # Source authentication is exercised through real seals in test_paired_proof.
    @patch.object(paired_proof.PairValidator,"check",new=lambda *_:None)
    def test_both_versions_missing_after_round_end_is_measured_against_schedule(self):
        history=season_games(2028,38)+season_games(2029,38)
        forward=season_games(2030,38)
        def rows(_,version,*,mode,**kwargs):
            return self.predictions(history if mode=="replay" else forward,version,mode,missing=0 if mode=="replay" else 21)
        with patch.object(store,"eligible_fixtures",return_value=history+forward), patch.object(store,"evaluation_rows",side_effect=rows), \
                patch.object(store,"require_formal",return_value={}), patch.object(store,"now_utc",return_value=self.judged):
            self.register_model()
            observed=jobs.evaluate_candidate(self.root,"candidate")
        forward=observed["assessment"]["forward"]
        self.assertEqual((forward["denominator"],forward["paired_count"]),(380,359))
        self.assertEqual(observed["state"],"shadow")
        self.assertFalse(observed["assessment"]["revealed"])

    def test_field_source_promoted_with_paired_independent_real_facts(self):
        future=season_games(2030,2)
        with patch.object(store,"eligible_fixtures",return_value=future), patch.object(store,"now_utc",return_value=self.registered):
            jobs.register_candidate(self.root,"new-lineups",kind="source",version="new",baseline="old",window="2030-31/1",
                                    hypothesis="the directly licensed feed has more confirmed lineups at T-60",
                                    manifest={"field":"lineup","rounds":2,"reference_source":"judge"})
        for src in ("new","old","judge"):
            evidence.register_source(self.root,{"source_id":src,"status":"active","free":True,"upstream":src,
                                     "terms_locator":f"fixture://terms/{src}","effective_at":self.registered,
                                     "fields":{"lineup":{"uses":["trial","prediction"],"methods":["manual"]}}})
        for rnd in (1,2):
            records=[]
            games=[f for f in future if f["round_id"]==rnd]
            before=store.iso(store.instant(games[0]["kickoff"])-timedelta(hours=2))
            for f in games:
                for side in ("home","away"):
                    for src in ("new","judge","old"):
                        if src=="old" and side=="away":
                            continue
                        team=f[side]
                        records.append({"source_id":src,"fixture_id":f["fixture_id"],"field":"lineup","team":team,
                                        "method":"manual","published_at":before,"received_at":before,"locator":f"fixture://{src}/{team}",
                                        "value":{"confirmed":True,"starters":[f"{team}-{n}" for n in range(11)]}})
            with patch.object(evidence,"now_utc",return_value=before):
                evidence.import_evidence(self.root,records)
        with patch.object(store,"eligible_fixtures",return_value=future), patch.object(store,"now_utc",return_value=self.judged), \
                patch.object(evidence,"now_utc",return_value=self.judged):
            result=jobs.evaluate_candidate(self.root,"new-lineups")
        self.assertEqual(result["state"],"promoted")
        comparison=result["assessment"]["comparison"]
        self.assertEqual((comparison["fixture_count"],comparison["denominator"]),(20,40))
        self.assertEqual(comparison["by_source"]["old"]["correct"],20)
        self.assertEqual(jobs.active_version(self.root,"source","lineup"),"new")

    def test_cli_status_registration(self):
        parser=argparse.ArgumentParser()
        parser.add_argument("--data-dir",type=Path,default=self.root)
        jobs.register(parser.add_subparsers(dest="command"))
        for argv,field in ((["jobs","status"],"jobs"),(["experiment","status"],"candidates")):
            args=parser.parse_args(argv)
            self.assertEqual(args.func(args)[field],[])

    def test_auto_review_waits_for_postponement_and_deduplicates_labels(self):
        games=season_games(2030,38)
        self.register_model()
        incomplete=[f | {"home_goals":None,"away_goals":None} if f["round_id"]==2 and f["home"]=="H0" else f for f in games]
        with patch.object(store,"now_utc",return_value=self.judged),patch.object(research,"evaluate_candidate",wraps=research.evaluate_candidate) as judge:
            with patch.object(store,"eligible_fixtures",return_value=incomplete):
                early=jobs.review_due_experiments(self.root,"2030-31",12)
                self.assertEqual(early["windows"][0]["state"],"awaiting_window")
                judge.assert_not_called()
            with patch.object(store,"eligible_fixtures",return_value=games):
                first=jobs.review_due_experiments(self.root,"2030-31",2)
                again=jobs.review_due_experiments(self.root,"2030-31",2)
            self.assertEqual(first["windows"][0]["state"],"checked")
            self.assertEqual(again["windows"][0]["state"],"already_checked")
            self.assertEqual(judge.call_count,1)


if __name__ == "__main__":
    unittest.main()
