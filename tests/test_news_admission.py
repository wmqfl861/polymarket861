import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket861 import league_registry, news_admission, store


class NewsAdmissionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.directory = self.root / "evidence"
        self.directory.mkdir()
        self.now = store.instant("2026-11-01T12:00Z")
        self.open = self.now + timedelta(days=1)
        mocked = patch.object(store, "now_utc", side_effect=lambda: store.iso(self.now))
        mocked.start()
        self.addCleanup(mocked.stop)

    def proof(self, doc):
        raw = store.canonical(doc).encode()
        ident = hashlib.sha256(raw).hexdigest()
        (self.directory / (ident + ".json")).write_bytes(raw)
        return ident

    def source_plan(self, league="epl", rounds=11):
        names = list(league_registry.members(league, "2026-27"))
        slots = []
        for week in range(1, rounds+1):
            deadline = self.open + timedelta(days=week)
            for i in range(len(names)//2):
                home, away = names[i], names[-i-1]
                key = "/".join([league, "2026-27", *(league_registry.club_key(league,"2026-27",v) for v in (home,away))])
                store.register_fixture(self.root, {"fixture_id":key, "season":"2026-27","round_id":week,
                    "home":home,"away":away,"known_at":store.iso(self.now),"kickoff":store.iso(deadline+timedelta(hours=1)),
                    "source":"calendar","locator":"fixture:"+key})
                slots.append({"id":key, "round":week, "deadline":store.iso(deadline)})
            names = [names[0], names[-1], *names[1:-1]]
        return {"kind":"source_field","league":league,"season":"2026-27","source":"operator-feed",
                "field":"injury","opens_at":store.iso(self.open),"independent_reviewer":"second-source",
                "samples":slots,"horizon_minutes":60}

    def source_review(self, plan, ident, valid):
        entries = []
        scope = {k:plan[k] for k in ("league","season","field","source")}
        matured = [s for s in plan["samples"] if store.instant(s["deadline"]) <= self.now]
        for i,sample in enumerate(matured):
            key, deadline = sample["id"], store.instant(sample["deadline"])
            raw = ("<feed>" + key + "</feed>").encode()
            raw_sha = hashlib.sha256(raw).hexdigest()
            (self.directory/(raw_sha+".xml")).write_bytes(raw)
            native = self.proof(scope | {"id":key,"kind":"source_receipt","received_at":store.iso(deadline),
                                         "response_sha256":raw_sha})
            review = self.proof(scope | {"id":key,"kind":"source_field","registration":ident,
                     "reviewer":"second-source","reviewed_at":store.iso(self.now),"valid":valid(i),"receipt_sha256":native})
            entries.append({"id":key, "evidence_sha256":review})
        semantic = self.proof(scope | {"kind":"source_field_semantics","registration":ident,"reviewer":"second-source",
                       "reviewed_at":store.iso(self.now),"passed":True,"cases":[matured[-1]["id"]]})
        return {"registration":ident, "decisions":entries, "semantic_check":semantic}

    def audit(self, ident, review):
        return news_admission.audit(self.root, ident, review, self.directory)

    def test_source_thresholds_use_entire_frozen_rounds_and_rolling_100(self):
        plan = self.source_plan()
        reg = news_admission.register(self.root, plan)["registration"]
        self.now = self.open + timedelta(days=2)
        review = self.source_review(plan, reg, lambda i:i != 0)
        self.assertEqual(self.audit(reg, review)["status"], "provisional_shadow")
        review["decisions"] = review["decisions"][:-2]
        review.pop("semantic_check")
        report = self.audit(reg, review)
        self.assertEqual((report["status"],report["missing_reviews"],report["window_cases"]), ("shadow_only",2,20))
        self.now = self.open + timedelta(days=10)
        review = self.source_review(plan, reg, lambda i:i < 91)
        self.assertEqual(self.audit(reg, review)["status"], "confirmation_criteria_met")
        review.pop("semantic_check")
        self.assertEqual(self.audit(reg, review)["status"], "shadow_only")
        self.now = self.open + timedelta(days=11)
        review = self.source_review(plan, reg, lambda i:i < 91)
        result = self.audit(reg, review)
        self.assertEqual((result["window_cases"],result["matured"],result["on_time_and_valid"],result["status"]),
                         (100,110,81,"shadow_only"))
        self.assertFalse(result["production_enabled"])

    def test_two_bundesliga_rounds_have_18_cases_and_no_provisional_admission(self):
        plan = self.source_plan("bun", 2)
        reg = news_admission.register(self.root, plan)["registration"]
        self.now = self.open + timedelta(days=2)
        report = self.audit(reg, self.source_review(plan, reg, lambda _:True))
        self.assertEqual((report["matured"],report["status"]), (18,"awaiting_rounds_or_cases"))

    def test_substitution_partial_round_unknown_schedule_and_late_enrollment_rejected(self):
        plan = self.source_plan("fl1", 3)
        with self.assertRaisesRegex(ValueError,"all fixtures"):
            news_admission.register(self.root, plan | {"samples":plan["samples"][:-1]})
        wrong = [s | {"deadline":store.iso(self.open+timedelta(days=30))} for s in plan["samples"]]
        with self.assertRaisesRegex(ValueError,"locally received fixture"):
            news_admission.register(self.root, plan | {"samples":wrong})
        reg = news_admission.register(self.root, plan)["registration"]
        self.now = self.open+timedelta(days=2)
        with self.assertRaisesRegex(ValueError,"preregistration"):
            news_admission.register(self.root, plan | {"field":"lineup"})
        review = self.source_review(plan, reg, lambda _:True)
        ref = review["decisions"][0]["evidence_sha256"]
        (self.directory/(ref+".json")).write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.audit(reg, review)

    def test_source_needs_original_bytes_and_local_receipt_time(self):
        import json
        plan = self.source_plan("fl1", 2)
        reg = news_admission.register(self.root, plan)["registration"]
        self.now = self.open+timedelta(days=2)
        review = self.source_review(plan, reg, lambda _:True)
        entry = review["decisions"][0]
        def read(sha):
            return json.loads((self.directory/(sha+".json")).read_bytes())
        verdict = read(entry["evidence_sha256"])
        receipt = read(verdict["receipt_sha256"])
        original = self.directory/(receipt["response_sha256"]+".xml")
        data = original.read_bytes()
        original.unlink()
        with self.assertRaisesRegex(ValueError, "missing news admission evidence"):
            self.audit(reg, review)
        original.write_bytes(b"substitute")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.audit(reg, review)
        original.write_bytes(data)
        del receipt["received_at"]
        verdict["receipt_sha256"] = self.proof(receipt)
        entry["evidence_sha256"] = self.proof(verdict)
        with self.assertRaisesRegex(ValueError, "received_at"):
            self.audit(reg, review)

    def blind_plan(self):
        scope = {"language":"es","league":"lal","season":"2026-27","model_sha256":"c"*64}
        samples = [{"id":str(i),"confirmed_prediction_sha256":self.proof(scope | {"kind":"system_confirmed_absence",
                   "id":str(i),"predicted_at":store.iso(self.now)})} for i in range(84)]
        plan = scope | {"kind":"confirmed_absence","samples":samples,"reviewers":["a","b","c"],
                        "training_clusters":[],"opens_at":store.iso(self.open)}
        return plan

    def test_blind_reservations_are_atomic_and_retries_use_the_original_plan(self):
        plan = self.blind_plan()
        alternative = plan | {"reviewers": ["a", "b", "another adjudicator"]}
        # Initialize the general database before racing the two admission registrations.
        with closing(store.connect(self.root)):
            pass

        def enroll(candidate):
            try:
                return news_admission.register(self.root, candidate, self.directory)["registration"]
            except ValueError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(enroll, (plan, alternative)))
        self.assertEqual(sum("already reserved" in v for v in outcomes), 1)
        accepted = next(p for p in (plan, alternative) if store.digest(p) in outcomes)
        self.assertEqual(enroll(accepted), store.digest(accepted))
        # A second model cannot claim a previously reserved case, even before first unblinding.
        self.assertIn("already reserved", enroll(accepted | {"model_sha256":"d" * 64}))

    def test_overlapping_legacy_registrations_never_get_a_precision_verdict(self):
        first = self.blind_plan()
        reg = news_admission.register(self.root, first, self.directory)["registration"]
        second = first | {"reviewers": ["a", "b", "replacement"]}
        second_id = store.digest(second)
        with closing(store.connect(self.root)) as db, db:
            db.execute("INSERT INTO news_admission_plans VALUES(?,?,?)",
                       (second_id, store.iso(self.now), store.canonical(second)))
        self.now = self.open + timedelta(hours=1)
        for ident in (reg, second_id):
            with self.assertRaisesRegex(ValueError, "already reserved"):
                self.audit(ident, {"registration": ident, "decisions": []})

    def test_confirmed_absence_requires_all_84_and_adjudication_of_disagreement(self):
        plan = self.blind_plan()
        scope = {k: plan[k] for k in ("language", "league", "season", "model_sha256")}
        with self.assertRaisesRegex(ValueError, "held-out"):
            news_admission.register(self.root, plan | {"training_clusters":["0"]}, self.directory)
        reg = news_admission.register(self.root, plan, self.directory)["registration"]
        self.now = self.open + timedelta(hours=1)
        def decisions(n, errors, disagree=False):
            rows = []
            for i in range(n):
                record = scope | {"registration":reg,"kind":"confirmed_absence","id":str(i),
                                  "labels":{"a":i>=errors,"b":i>=errors},
                                  "reviewed_at":store.iso(self.now)}
                if disagree and i == 0:
                    record["labels"]["a"] = not record["labels"]["b"]
                rows.append({"id":str(i),"evidence_sha256":self.proof(record)})
            return {"registration":reg,"decisions":rows}
        first = self.audit(reg, decisions(83,3))
        self.assertEqual(first["status"], "awaiting_fixed_84")
        self.assertNotIn("correct", first)
        good = self.audit(reg, decisions(84,3))
        self.assertEqual(good["status"], "precision_met")
        self.assertGreaterEqual(good["wilson_lower_95"], 0.90)
        with self.assertRaisesRegex(ValueError, "already sealed"):
            self.audit(reg, decisions(84,4))
        self.assertLess(news_admission._wilson_lower(80,84), 0.90)
        with self.assertRaisesRegex(ValueError, "third registered"):
            self.audit(reg, decisions(84,3,True))
