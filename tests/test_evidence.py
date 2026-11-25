"""Permissions, time travel, independent source adjudication and reversible data repair."""

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket861 import evidence as ev
from polymarket861.store import list_reports


def time_at(hour, minute=0):
    return f"2025-07-01T{hour:02}:{minute:02}:00Z"


def starters(prefix):
    return [f"{prefix}{n}" for n in range(1, 12)]


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def source(self, ident="desk", *, fields=("lineup",), uses=("trial", "prediction"),
               status="active", effective_at=None, upstream=None, impacts=False):
        return ev.register_source(self.root, {
            "source_id": ident, "name": ident, "free": True, "status": status,
            "effective_at": effective_at or time_at(0), "upstream": upstream or ident,
            "terms_locator": "owned://author-grant/1",
            "fields": {f: {"uses": list(uses), "methods": ["manual"], "allow_impacts": impacts}
                       for f in fields},
        })

    def fact(self, *, source="desk", field="lineup", team="Home", value=None, **overrides):
        record = {
            "source_id": source, "fixture_id": "F", "team": team, "field": field,
            "method": "manual", "published_at": time_at(9), "received_at": time_at(9, 2),
            "locator": f"owned://{source}/event-F",
            "value": {"confirmed": False, "starters": starters(team)} if value is None else value,
        }
        return dict(record, **overrides)

    def ingest(self, *args):
        return ev.import_evidence(self.root, list(args))["evidence_ids"]

    def context(self, hour, **options):
        return ev.as_of_context(self.root, "F", "Home", "Away", time_at(hour), **options)

    def test_field_grants_manual_candidate_and_training_revocation(self):
        self.source(status="candidate", uses=("trial", "prediction", "training"))
        trial, = self.ingest(self.fact())
        self.assertEqual(self.context(10, purpose="trial")["evidence_ids"], [trial])
        self.assertEqual(self.context(10)["evidence_ids"], [])
        self.source(effective_at=time_at(11), uses=("trial", "prediction", "training"))
        self.assertEqual(self.context(12, purpose="training")["evidence_ids"], [])
        with self.assertRaisesRegex(ValueError, "permission"):
            self.ingest(self.fact(method="auto"))
        with self.assertRaisesRegex(ValueError, "permission"):
            self.ingest(self.fact(field="news"))
        live, = self.ingest(self.fact(revision=2, received_at=time_at(12), published_at=time_at(12)))
        self.assertEqual(self.context(13, purpose="training")["evidence_ids"], [live])
        self.source(effective_at=time_at(14))
        self.assertEqual(self.context(13, purpose="training")["evidence_ids"], [])
        self.assertEqual(self.context(13)["evidence_ids"], [live])
        self.assertEqual(ev.source_report(self.root, "desk")["trial"]["eligible_training_count"], 0)

    def test_independent_teams_predicted_and_confirmed_knowability(self):
        self.source()
        guess, home, away = self.ingest(
            self.fact(),
            self.fact(value={"confirmed": True, "starters": starters("H")}, revision=2,
                      published_at=time_at(10), received_at=time_at(12)),
            self.fact(team="Away", value={"confirmed": True, "starters": starters("A")},
                      published_at=time_at(11), received_at=time_at(10)),
        )
        before = self.context(10)
        self.assertEqual(before["evidence_ids"], [guess])
        self.assertEqual(before["confirmed"], {"home": False, "away": False})
        at_eleven = self.context(11)
        self.assertEqual(at_eleven["teams"]["home"]["lineup"]["evidence_id"], guess)
        self.assertEqual(at_eleven["teams"]["away"]["lineup"]["evidence_id"], away)
        after = self.context(12)
        self.assertEqual(after["confirmed"], {"home": True, "away": True})
        self.assertEqual(after["teams"]["home"]["lineup"]["evidence_id"], home)
        self.assertEqual(after["corrections"][0]["superseded_ids"], [guess])
        self.assertEqual(after["teams"]["home"]["impacts"], [])
        with self.assertRaisesRegex(ValueError, "11 distinct"):
            self.ingest(self.fact(revision=3, value={"confirmed": True, "starters": ["P"] * 11}))

    def test_latest_revision_withdrawal_expiry_and_atomic_retry(self):
        self.source(fields=("injury",))
        def record(revision, received, **kwargs):
            return self.fact(field="injury", subject="P", revision=revision,
                             received_at=time_at(received), **kwargs)
        new = record(2, 10, value={"status": "out"}, valid_until=time_at(13))
        new_id, old_id = self.ingest(new, record(1, 11, value={"status": "in"}))
        self.assertEqual(self.context(12)["evidence_ids"], [new_id])
        self.assertEqual(self.context(13)["evidence_ids"], [])
        self.assertEqual(self.ingest(dict(new, received_at=time_at(14))), [new_id])
        self.assertEqual(ev.get_evidence(self.root, new_id)["received_at"], ev.utc(time_at(10)))
        with self.assertRaisesRegex(ValueError, "new revision"):
            self.ingest(record(3, 14, value={"status": "doubtful"}), dict(new, value={"status": "fit"}))
        self.assertEqual(ev.source_report(self.root, "desk")["trial"]["sample_count"], 2)
        retract, = self.ingest(record(4, 15, state="withdrawn", value={}))
        context = self.context(16)
        self.assertFalse(context["facts"])
        self.assertEqual(context["corrections"][0]["evidence_ids"], [retract])
        self.assertEqual(set(context["corrections"][0]["superseded_ids"]), {old_id, new_id})

    def test_syndication_distinct_sources_and_conflicting_lineups(self):
        self.source(fields=("lineup", "news"))
        self.source("mirror", fields=("lineup", "news"))
        for source in ("desk", "mirror"):
            self.ingest(self.fact(source=source, field="news", claim_key="story-37", origin_id="wire",
                                  value={"event": "travel delay"}))
        self.ingest(self.fact(source="desk", value={"confirmed": True, "starters": starters("A")}),
                    self.fact(source="mirror", value={"confirmed": True, "starters": starters("B")}))
        self.ingest(self.fact(source="desk", field="news", team="Else", revision=2, claim_key="outside",
                              value={"event": "unrelated"}, state="active"))
        context = self.context(12)
        news = [f for f in context["facts"] if f["field"] == "news"]
        self.assertEqual(len(news), 1)
        self.assertEqual(len(news[0]["corroborating_ids"]), 1)
        self.assertFalse(context["teams"]["home"]["confirmed"])
        self.assertEqual(context["conflicts"][0]["reason"], "conflicting_confirmed_lineups")
        self.assertEqual(context["corrections"], [])
        self.ingest(self.fact(source="desk", field="news", origin_id="wire", revision=2,
                              claim_key="story-37", state="withdrawn", value={}))
        self.assertFalse(any(f["field"] == "news" for f in self.context(12)["facts"]))

    def test_domain_and_evidence_backed_impact_validation(self):
        self.source(fields=("weather", "injury", "card", "suspension", "travel"), impacts=True)
        for raw in [
            self.fact(field="injury", value={"status": "out"}),
            self.fact(field="suspension", subject="P", value={"matches": 1}),
            self.fact(field="weather", value={"run_at": time_at(10), "forecast_for": time_at(15)}),
        ]:
            with self.assertRaises(ValueError):
                self.ingest(raw)
        self.ingest(self.fact(field="weather", team="", value={"run_at": time_at(8),
                           "forecast_for": time_at(15), "wind_kph": 20}))
        synthetic = {"attack_multiplier": 0.9, "defense_multiplier": 1.1, "probability": 0.7,
                     "uncertainty": "synthetic holdout interval", "basis": "owned://test-calibration/1"}
        self.ingest(self.fact(field="injury", subject="P", value={"status": "doubtful", "impact": synthetic}))
        impact, = self.context(12)["teams"]["home"]["impacts"]
        self.assertTrue(impact["experimental"])
        self.assertEqual(impact["basis"], synthetic["basis"])
        self.assertEqual(ev.get_evidence(self.root, impact["evidence_id"])["subject"], "P")
        with self.assertRaisesRegex(ValueError, "probability"):
            self.ingest(self.fact(field="injury", subject="Q", value={"impact": dict(synthetic, probability=1.1)}))
        self.source("plain", fields=("injury",))
        with self.assertRaisesRegex(ValueError, "permission for impact"):
            self.ingest(self.fact(source="plain", field="injury", subject="P", value={"impact": synthetic}))

    def test_true_prospective_receipts_and_independent_comparison_denominator(self):
        with patch("polymarket861.evidence_sources.now_utc", return_value=time_at(0)):
            for sid in ("base", "candidate", "ref"):
                self.source(sid)
        confirmed = {"confirmed": True, "starters": starters("P")}
        with patch.object(ev, "now_utc", return_value=time_at(9, 30)):
            self.ingest(self.fact(source="base", value=confirmed))
        with patch.object(ev, "now_utc", return_value=time_at(14)):
            late, = self.ingest(self.fact(source="candidate", value=confirmed))
            self.ingest(self.fact(source="ref", value=dict(confirmed, starters=list(reversed(starters("P"))))))
        fact = ev.source_reference(self.root, "ref", "F", "Home", "lineup", cutoff=time_at(15))
        self.assertEqual(fact["first_recorded_at"], ev.utc(time_at(14)))
        self.assertIsNone(ev.source_reference(self.root, "candidate", "F", "Home", "lineup",
                                               cutoff=time_at(12), prospective=True))
        common = {"team": "Home", "round": "1", "cutoff":time_at(12), "prospective": True,
                  "expected_value":fact["value"], "reference_origin":fact["origin_id"],
                  "reference_locator":fact["locator"]}
        observations = [dict(common, fixture_id="F"), dict(common, fixture_id="MISSING", round="2")]
        compare = ev.source_comparison(self.root, ["base","candidate"], "lineup", observations)
        self.assertEqual((compare["denominator"],compare["fixture_count"],compare["rounds"]), (2,2,["1","2"]))
        self.assertEqual(compare["by_source"]["base"]["verified_correct_rate"], 0.5)
        self.assertEqual(compare["by_source"]["candidate"]["missing"], 2)
        replay = ev.source_comparison(self.root, ["candidate"], "lineup",
                                      [dict(common,fixture_id="F",prospective=False,reference_origin="candidate")])
        self.assertEqual(replay["by_source"]["candidate"]["evidence_ids"], [late])
        self.assertEqual(replay["by_source"]["candidate"]["verified"], 0)
        self.assertIsNone(ev.source_comparison(self.root, ["base"], "lineup")["by_source"]["base"]["on_time_rate"])

    def test_quarantine_versioned_repair_and_transitive_impact(self):
        self.source(fields=("injury",))
        raw = self.fact(field="injury", subject="P", value={"status":"out"})
        old, = self.ingest(raw)
        parent = old
        chain = ["feature", "training_set", "model", "prediction", "sim_order"]
        for kind in chain:
            ev.link_lineage(self.root, kind, kind + "-v1", [parent])
            parent = {"kind":kind, "key":kind + "-v1"}
        report = ev.open_incident(self.root, {"incident_id":"I", "symptom":"wrong injury flag",
                  "affected_evidence_ids":[old], "opened_at":time_at(11)})
        self.assertEqual({r["kind"] for r in report["impacted"]}, {"evidence", *chain})
        self.assertEqual(next(r for r in report["impacted"] if r["kind"]=="model")["action"], "retrain_or_rollback")
        self.assertEqual(self.context(10)["evidence_ids"], [old])
        self.assertEqual(self.context(11)["evidence_ids"], [])
        new, = self.ingest(dict(raw, revision=2, value={"status":"fit"}, received_at=time_at(12)))
        self.assertEqual(self.context(13)["evidence_ids"], [new])
        with self.assertRaisesRegex(ValueError, "lineage is immutable"):
            ev.link_lineage(self.root, "feature", "feature-v1", [new])
        remedy = {"incident_id":"I", "root_cause":"missing opponent key", "remedy":"validate identity",
                  "regression_case":"fixture.test_swapped_player", "fix_version":"parser-v2",
                  "correction_ids":[new], "closed_at":time_at(13)}
        with self.assertRaisesRegex(ValueError, "requires successful"):
            ev.resolve_incident(self.root, remedy)
        checks = dict(bad_sample_rejected=True, good_sample_accepted=True, affected_range_replayed=True)
        result = ev.resolve_incident(self.root, dict(remedy, verification=checks))
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(ev.get_evidence(self.root, old)["value"]["status"], "out")
        self.assertEqual(list_reports(self.root, "incident")[0]["payload"]["fix_version"], "parser-v2")

    def test_frozen_source_recovery_and_cli(self):
        self.source(fields=("card",))
        def check(hour, latest, sha):
            return ev.observe_source(self.root, {"source_id":"desk","field":"card","outcome":"ok",
                    "checked_at":time_at(hour),"expected_data_at":time_at(10),
                    "latest_data_at":time_at(latest),"content_hash":sha})
        self.assertEqual(check(10,9,"old")["status"], "late")
        self.assertEqual(check(11,9,"old")["status"], "frozen")
        self.assertEqual(check(12,11,"new")["status"], "ok")
        parser = argparse.ArgumentParser()
        parser.add_argument("--data-dir", type=Path)
        ev.register(parser.add_subparsers(required=True))
        trial_file = self.root / "sample.json"
        trial_file.write_text(json.dumps(self.fact(field="card",subject="P",value={"yellow":1})))
        argv = parser.parse_args(["--data-dir", str(self.root), "source", "trial", "desk", "--file", str(trial_file)])
        report = argv.handler(argv)
        self.assertEqual(report["trial"]["eligible_prediction_count"],0)
        self.assertEqual(report["health"]["card"]["status"],"ok")
        self.assertEqual(len(list_reports(self.root, "source")),1)

    def test_root_cause_guard_replays_bad_good_and_blocks_recurrence(self):
        self.source(fields=("minutes",))
        raw = self.fact(field="minutes", subject="P", value={"minutes": 930})
        bad, = self.ingest(raw)
        ev.open_incident(self.root, {"incident_id":"I", "affected_evidence_ids":[bad],
            "symptom":"adapter ingested tenths of a minute as minutes", "opened_at":time_at(10)})
        corrected, = self.ingest(dict(raw, revision=2, value={"minutes":93},received_at=time_at(11)))
        wrong, = self.ingest(dict(raw, subject="Q"))
        report = {"incident_id":"I", "closed_at":time_at(12), "root_cause":"tenths passed as minutes",
            "remedy":"repair unit conversion", "regression_case":"minutes-unit-1", "fix_version":"adapter2",
            "guard":{"source_id":"desk","field":"minutes","path":"minutes","min":0,"max":140},
            "correction_ids":[corrected], "verification":{"bad_sample_rejected":True,"good_sample_accepted":True,
                                                         "affected_range_replayed":True}}
        with self.assertRaisesRegex(ValueError, "later revision"):
            ev.resolve_incident(self.root, dict(report,correction_ids=[wrong]))
        with self.assertRaisesRegex(ValueError, "reject the affected"):
            ev.resolve_incident(self.root, dict(report,guard=dict(report["guard"],max=1000)))
        self.assertEqual(ev.resolve_incident(self.root,report)["verification_mode"],"enforced_guard")
        with self.assertRaisesRegex(ValueError, "regression guard"):
            self.ingest(dict(raw,subject="R"))
        self.assertEqual(len(self.ingest(dict(raw,subject="R",value={"minutes":84}))),1)

    def test_discovered_catalog_cannot_elevate_or_demote_a_source(self):
        self.source(fields=("news",))
        manifest = {"source_id":"new", "upstream":"publisher", "free":True, "status":"active",
            "terms_locator":"owned://documentation", "discovered_from":"owned://catalog/version-1",
            "fields":{"news":{"uses":["trial","prediction"],"methods":["manual"],"unit":"story"}}}
        found = ev.discover_sources(self.root,[manifest,dict(manifest,source_id="desk"),dict(manifest,source_id="paid",free=False)])
        self.assertEqual(found["registered"],["new"])
        self.assertEqual(found["already_known"],["desk"])
        self.assertEqual(found["rejected"][0]["source_id"],"paid")
        self.assertEqual(ev.source_report(self.root,"desk")["status"],"active")
        self.assertEqual(ev.source_report(self.root,"new")["status"],"candidate")
        coverage = ev.coverage_gaps(self.root,"news")["fields"]["news"]
        self.assertEqual(coverage["prediction_sources"],["desk"])
        self.assertEqual(coverage["candidate_sources"],["new"])
        self.assertIn("no_independent_backup",coverage["gaps"])

    def test_neutral_weather_requires_traceable_matching_holdout_for_one_joint_scenario(self):
        self.source("judge",fields=("impact_validation",))
        manifest = {"source_id":"weather", "status":"active", "free":True, "effective_at":time_at(0),
            "upstream":"forecast-office", "terms_locator":"owned://weather",
            "fields":{"weather":{"uses":["prediction","trial"],"methods":["manual"],"allow_match_impacts":True}}}
        ev.register_source(self.root,manifest)
        estimate = {"home":{"attack_multiplier":0.9,"defense_multiplier":1.04},
                    "away":{"attack_multiplier":0.84,"defense_multiplier":0.97},"probability":0.3}
        certificate = self.fact(source="judge",field="impact_validation",fixture_id="calibration-v1",team="",
            value={"applies_to":"weather","holdout_games":10,"evaluation_locator":"owned://validation/report#12",
                   "estimate":estimate}, published_at=time_at(10),received_at=time_at(11))
        proof, = self.ingest(certificate)
        base = self.fact(source="weather",field="weather",team="",value={"run_at":time_at(8),"forecast_for":time_at(15)})
        no_effect, = self.ingest(base)
        self.assertEqual(self.context(10)["match_impacts"],[])
        shared = dict(estimate, uncertainty="synthetic holdout range",basis="owned://coefficient/v1",
                      scenario_id="storm",validation_evidence_id=proof)
        derived, = self.ingest(dict(base,revision=2,value=dict(base["value"],match_impact=shared)))
        self.assertEqual(self.context(10)["match_impacts"],[])
        got = self.context(12)
        self.assertEqual(got["evidence_ids"],sorted([derived,proof]))
        self.assertNotIn(no_effect,got["evidence_ids"])
        item, = got["match_impacts"]
        self.assertTrue(item["experimental"])
        self.assertEqual(item["validation_evidence_id"],proof)
        self.assertEqual(item["away"],estimate["away"])
        changed = dict(shared, probability=0.8)
        self.ingest(dict(base,revision=3,value=dict(base["value"],match_impact=changed)))
        self.assertEqual(self.context(12)["match_impacts"],[])
        self.assertEqual(self.context(12)["impact_exclusions"][0]["reason"],
                         "independent_match_estimate_validation_unavailable")


if __name__ == "__main__":
    unittest.main()
