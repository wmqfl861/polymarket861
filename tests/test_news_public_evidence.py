import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from polymarket861 import football_model, news_ai_benchmark, news_events, store
from scripts import replay_news_probabilities as replay


class PublicProbabilityReplay(unittest.TestCase):
    def test_historical_event_vectors_are_inferred_and_the_separate_seal_has_original_lines(self):
        report = replay.reconstruct(replay.LEGACY)
        checked = replay.verify(report)
        self.assertEqual(checked["events"]["copied_original_probability_values"], 120)
        self.assertEqual(checked["events"]["newly_reconstructed_line_directions"], 480)
        self.assertEqual(checked["separate_early_seal"]["probability_values"], 60)
        row = report["events"][0]
        row["arms"]["with_news"]["totals"]["home"]["0.5"]["under"] += 0.01
        with self.assertRaisesRegex(ValueError, "probability mismatch"):
            replay.verify(report)

    def test_runtime_export_copies_and_verifies_the_saved_rates_and_all_custom_lines(self):
        lines = [0.5, 1.5, 2.5, 4.5]
        pair = {"policy_sha256": "policy", "base_model": "B1", "changed": True,
                "applied": [{"id": "claim"}], "excluded_reasons": {}}
        for name, key, home in (("no_news", "base_rates", 1.4), ("with_news", "candidate_rates", 1.6)):
            matrix, _ = football_model._goals_matrix(home, 1.2, -0.05)
            probabilities, totals = football_model.probabilities(matrix, lines)
            pair[key] = {"home": home, "away": 1.2, "rho": -0.05}
            pair[name] = {"probabilities": probabilities, "totals": totals}
        doc = {"fixture_id": "epl/2026-27/home/away", "season": "2026-27", "round_id": 12,
               "cutoff": "2026-11-25T12:00:00Z", "kickoff": "2026-11-28T15:00:00Z",
               "created_at": "2026-11-25T12:00:01Z", "revision": 1, "state_sha256": "state",
               "recipe_sha256": "recipe", "training_sha256": "training", "previous_digest": None,
               "history_manifest_sha256": "manifest", "lines": lines, "news_pair": pair}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.executescript(news_events._SCHEMA)
                db.execute("INSERT INTO news_event_updates VALUES (1,?,?,?,?,?,?,?)",
                           (doc["fixture_id"], doc["kickoff"], doc["revision"], None, store.digest(doc),
                            doc["state_sha256"], store.canonical(doc)))
                db.commit()
                report = replay.export_runtime(directory, doc["season"], doc["round_id"])
                checked = replay.verify(report)
                self.assertEqual(checked["events"]["probability_values_compared_to_stored_vectors"], 60)
                arm = report["events"][0]["arms"]["with_news"]
                self.assertEqual(arm["rates"]["lambda_home"], 1.6)
                self.assertEqual(arm["totals"], pair["with_news"]["totals"])
                self.assertEqual(arm["adaptive_grid"]["shape"], list(matrix.shape))
                self.assertLess(arm["adaptive_grid"]["joint_tail_union_bound"], replay.TAIL)
                arm["adaptive_grid"]["max_goals_inclusive"] -= 1
                with self.assertRaisesRegex(ValueError, "adaptive grid mismatch"):
                    replay.verify(report)
                db.execute("UPDATE news_event_updates SET document=replace(document, '1.6', '1.8')")
                db.commit()
                with self.assertRaisesRegex(ValueError, "content verification"):
                    replay.export_runtime(directory, doc["season"], doc["round_id"])

    def test_empirical_fallback_stays_visible_with_missing_rates(self):
        arm = replay.replay({"lambda_home": 1.7, "lambda_away": 1.2, "rho": 0.04}, [2.5])
        arm.update(rates=None, adaptive_grid=None)
        event = {"fixture_id": "f", "base_model": "B0", "lines": [2.5], "arms": dict.fromkeys(replay.ARMS, arm)}
        checked = replay._verify_events([event])
        self.assertEqual(checked["empirical_arms_without_rates"], 2)
        self.assertEqual(checked["probability_values"], 0)
        event["base_model"] = "B1"
        with self.assertRaisesRegex(ValueError, "missing rates"):
            replay._verify_events([event])

    def test_early_export_checks_the_original_main_seal_and_input_time(self):
        given = {"home": 1.4, "away": 1.2, "rho": -0.04}
        matrix, _ = football_model._goals_matrix(*given.values())
        probabilities, totals = football_model.probabilities(matrix, [2.5])
        arm = {"probabilities": probabilities, "totals": totals}
        fixture = "epl/2026-27/home/away"
        cutoff = "2026-11-25T10:00:00.000000Z"
        sealed = "2026-11-25T10:00:01.000000Z"
        pair = {"base_rates": given, "candidate_rates": given, "base_model": "B1", "no_news": arm,
                "with_news": arm, "applied": [], "changed": False, "excluded_reasons": {},
                "policy_sha256": "policy", "base_training_sha256": "training", "input_cutoff": cutoff,
                "facts_sha256": store.digest([])}
        doc = {"fixture_id": fixture, "season": "2026-27", "round_id": 12, "model": "B1",
               "model_version": "B1", "horizon": "early", "mode": "technical", "purpose": "main",
               "protocol": store.PROTOCOL, "kickoff": "2026-11-28T15:00:00.000000Z",
               "cutoff": sealed, "sealed_at": sealed, "input_cutoff": cutoff, "news_pair": pair,
               "probabilities": probabilities, "totals": totals, "rates": given,
               "recipe_digest": "recipe", "input_manifest_sha256": "manifest",
               "context": {"player_news": {"fixture_id": fixture, "cutoff": cutoff, "facts": []}},
               "comparison_proof": {"frame": "frame"}, "evidence": {"input_sha256": "training"}}
        columns = ("fixture_id", "kickoff", "cutoff", "horizon", "model_version", "protocol", "mode", "sealed_at")
        with tempfile.TemporaryDirectory() as directory, closing(store.connect(directory)) as db:
            db.execute("INSERT INTO forecast_seals VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (*[doc[k] for k in columns], store.digest(doc), store.canonical(doc)))
            db.commit()
            args = {"early_seals": True, "recipe": "recipe"}
            report = replay.export_runtime(directory, doc["season"], 12, as_of=sealed, **args)
            self.assertEqual(set(replay.verify(report)), {"absolute_tolerance", "technical_early_seals"})
            [projected] = report["technical_early_seals"]
            self.assertEqual((projected["input_cutoff"], projected["sealed_at"]), (cutoff, sealed))
            with self.assertRaisesRegex(ValueError, "no matching"):
                replay.export_runtime(directory, doc["season"], 12, as_of=cutoff, **args)
            doc["rates"] = given | {"home": 1.6}
            db.execute("UPDATE forecast_seals SET digest=?, data_json=?", (store.digest(doc), store.canonical(doc)))
            db.commit()
            with self.assertRaisesRegex(ValueError, "disagree"):
                replay.export_runtime(directory, doc["season"], 12, **args)


class ClubLanguageEvidence(unittest.TestCase):
    def test_real_excerpts_preserve_evidence_offsets_and_explicit_denominators(self):
        path = replay.ROOT / "reports/research/club-prose-0.4.1.json"
        corpus = json.loads(path.read_bytes())
        score = news_ai_benchmark.club_prose_score(path)
        self.assertEqual(score["articles"], 32)
        self.assertEqual(score["publishers"], 16)
        self.assertEqual(score["gold_claims"], 31)
        self.assertEqual(score["negative_articles"], 9)
        self.assertEqual(score["fully_correct_claims"], sum(row["correct"] for row in score["cases"]))
        for row, decision in zip(corpus["passages"], score["cases"], strict=True):
            self.assertEqual(decision["passage_id"], row["id"])
            for claim in decision["emitted_claims"]:
                span = row["text"][claim["start"]:claim["end"]]
                self.assertTrue(span.strip())
                self.assertEqual(hashlib.sha256(span.encode()).hexdigest(), claim["quote_sha256"])
        wire = news_ai_benchmark.published_wire_linkage(replay.ROOT / "reports/news-language-0.4.0.json")
        self.assertEqual(wire["counts"]["records"], 135)
        self.assertEqual(wire["counts"]["unique_exact_names"], 122)
        self.assertEqual(wire["label_agreement_given_unique_name"], 1.0)
