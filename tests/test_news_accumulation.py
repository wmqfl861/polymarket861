"""News diagnostic denominators, original market witnesses and prospective study eras."""

import json
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import test_opportunity as market
import test_round_comparisons as native
from kickoff_fixture import capture_example, open_release
from polymarket861 import (
    kickoff_proof,
    news_accumulation,
    news_evaluation,
    news_market,
    news_model,
    news_study_plan as plan,
    pipeline,
    store,
)
from test_news_matchday import AWAY, FID, HOME, KICKOFF, SEASON, seed_fixture
from test_news_outcome_admission import admit_final
from test_paired_proof import at


def coverage(home, away, stamp, config="a" * 64):
    return {
        "both_teams_observed": True,
        "observed_teams": 2,
        **{
            side: {
                "team": team,
                "observed": True,
                "sources": [
                    {
                        "status": "ok",
                        "checked_at": store.iso(stamp),
                        "availability_observed": True,
                        "source_observed_at": store.iso(stamp),
                        "extractor_config_sha256": config,
                    }
                ],
            }
            for side, team in (("home", home), ("away", away))
        },
    }


class NativeCloseTests(unittest.TestCase):
    def setUp(self):
        self.case = native.RoundComparisons()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root
        self.game = store.fixture_at(self.root, market.FID)
        self.cut = market.CUT - timedelta(minutes=1)
        self.entry = {"sealed_at": store.iso(self.cut), "mode": "formal"}
        p = self.case.forecast["probabilities"] | {
            "home_win": 0.40,
            "draw": 0.35,
            "away_win": 0.25,
        }
        self.pair = {
            "coverage": coverage(market.HOME, market.AWAY, self.cut),
            "input_cutoff": store.iso(self.cut),
            "no_news": {"probabilities": p},
            "with_news": {"probabilities": p | {"home_win": 0.50, "draw": 0.25}},
        }

    def read(self):
        with closing(store.connect(self.root)) as db:
            return news_market.observe(db, self.root, self.game, self.entry, self.pair)

    def close_status(self):
        with closing(store.connect(self.root)) as db:
            source = news_market.source_capability(db)
        return news_market.summary(
            [
                {
                    "sealed": True,
                    "timely_news_coverage": True,
                    "implementation": "test",
                    "market_close": self.read(),
                }
            ],
            source,
            "formal",
        )

    def test_source_route_fixture_proof_and_native_books_are_distinct(self):
        with (
            patch.dict(kickoff_proof.OPEN_RELEASES, clear=True),
            patch.dict(kickoff_proof.FREE_ESPN_PERMISSIONS, clear=True),
            market.at(market.KICK + timedelta(hours=3)),
        ):
            kickoff_proof.OPEN_RELEASES["unreviewed"] = {}
            unavailable = self.close_status()
            self.assertEqual(unavailable["status"], "actual_kickoff_source_unavailable")
            self.assertEqual(unavailable["source_capability"]["admission_commands"], [])
            key, spec, xml, license_file = open_release(
                self.root, self.game, 78, market.KICK
            )
            kickoff_proof.OPEN_RELEASES[key] = spec
            waiting = self.close_status()
            self.assertEqual(waiting["status"], "awaiting_verified_actual_kickoff")
            self.assertEqual(
                waiting["source_capability"]["reviewed_open_f24_release_keys"], [key]
            )
            self.assertIn(
                "ledger-import-open-kickoff",
                waiting["source_capability"]["admission_commands"][0],
            )
            proof = kickoff_proof.import_open(
                self.root,
                market.FID,
                "78",
                key,
                xml,
                license_file,
                schedule_version=self.game["version"],
            )
            absent = self.close_status()
            self.assertEqual(
                (
                    absent["status"],
                    absent["eligible_seals"],
                    absent["verified_actual_kickoffs"],
                    absent["paired_fixtures"],
                ),
                ("no_matched_market_quotes", 1, 1, 0),
            )
            self.assertTrue(absent["missing_family_reasons"])
            store.blob_path(self.root, proof["source_sha256"]).write_bytes(b"corrupt")
            self.assertEqual(
                self.close_status()["status"], "invalid_actual_kickoff_evidence"
            )

    def test_retained_espn_proof_reverifies_at_original_grant_time(self):
        from polymarket861 import evidence_sources

        received = market.KICK + timedelta(hours=3)
        url = "https://publisher.example.test/event-grant"
        timeline = {
            "header": {
                "id": "72",
                "league": {"slug": "eng.1"},
                "competitions": [
                    {
                        "id": "72",
                        "date": self.game["kickoff"],
                        "status": {"type": {"completed": True}},
                        "competitors": [
                            {
                                "homeAway": side,
                                "team": {"id": side, "displayName": self.game[side]},
                            }
                            for side in ("home", "away")
                        ],
                    }
                ],
            },
            "keyEvents": [
                {
                    "id": "8",
                    "type": {"text": "Kickoff"},
                    "text": "First Half begins.",
                    "wallclock": store.iso(market.KICK),
                }
            ],
        }
        with (
            patch.dict(kickoff_proof.OPEN_RELEASES, clear=True),
            patch.dict(kickoff_proof.FREE_ESPN_PERMISSIONS, clear=True),
        ):
            with market.at(received):
                sha = store.archive_bytes(
                    self.root, "test-grant", b"synthetic issuer grant"
                )["sha256"]
                kickoff_proof.FREE_ESPN_PERMISSIONS[sha] = url
                self.assertEqual(
                    self.close_status()["source_capability"]["status"],
                    "local_permission_required",
                )
                grant = {
                    "source_id": kickoff_proof.SOURCE,
                    "upstream": "ESPN",
                    "free": True,
                    "status": "active",
                    "terms_locator": url,
                    "fields": {
                        "schedule": {
                            "uses": ["trial"],
                            "methods": ["auto"],
                            "allow_free_first_half_event_archive": True,
                            "written_permission_locator": url,
                            "written_permission_sha256": sha,
                        }
                    },
                }
                evidence_sources.register_source(self.root, grant)
                self.assertEqual(
                    self.close_status()["source_capability"]["current_espn_capture"],
                    "capture_authorized",
                )
                with patch.object(
                    kickoff_proof, "_fetch", return_value=json.dumps(timeline).encode()
                ):
                    kickoff_proof.capture(
                        self.root,
                        market.FID,
                        "72",
                        schedule_version=self.game["version"],
                    )
            with market.at(received + timedelta(hours=1)):
                evidence_sources.register_source(
                    self.root, grant | {"status": "disabled"}
                )
                final = self.close_status()
            self.assertEqual(
                final["source_capability"]["status"], "local_permission_required"
            )
            self.assertEqual(final["source_capability"]["admission_commands"], [])
            self.assertEqual(
                (final["verified_actual_kickoffs"], final["status"]),
                (1, "no_matched_market_quotes"),
            )

    def test_matches_frozen_clock_and_original_condition_family_at_actual_close(self):
        self.case.batch(
            market.CUT - timedelta(seconds=2)
        )  # Too late for the sealed inputs.
        self.case.batch(market.KICK - timedelta(seconds=5))
        self.assertEqual(self.read()["reason"], "missing_verified_actual_kickoff")
        actual = market.KICK + timedelta(minutes=1)
        self.case.batch(actual - timedelta(seconds=15), shift=2)
        self.case.batch(actual - timedelta(seconds=5), omit={"21", "42"}, shift=3)
        self.case.batch(actual + timedelta(seconds=1), shift=4)
        with (
            patch.dict(kickoff_proof.OPEN_RELEASES, {}),
            market.at(actual + timedelta(hours=3)),
        ):
            receipt = capture_example(self.root, self.game, 77332111, actual)
            missing = self.read()
            self.assertFalse(missing["endpoints"])
            self.assertIn("decision", missing["missing"]["result"])
            self.case.batch(self.cut - timedelta(seconds=4))
            # All tokens within a family must come from one native observation.
            joined = self.read()
            self.assertEqual(joined["kickoff_proof"], receipt["proof_id"])
            self.assertEqual(
                set(joined["endpoints"]), set(self.pair["no_news"]["probabilities"])
            )
            self.assertEqual(
                joined["receipts"]["result"]["close"]["received_at"],
                store.iso(actual - timedelta(seconds=15)),
            )
            self.assertEqual(
                joined["receipts"]["home"]["close"]["received_at"],
                store.iso(actual - timedelta(seconds=5)),
            )
            self.assertEqual(len(joined["receipts"]["result"]["contracts"]), 3)
            h = joined["endpoints"]["home_win"]
            self.assertAlmostEqual(h["decision"], 0.50 / 1.02)
            self.assertAlmostEqual(h["close"], 0.52 / 1.08)
            self.assertEqual(
                (h["direction"], h["market_drift_agreement"]), ("toward", "away")
            )
            self.assertAlmostEqual(
                h["distance_gain_pp"],
                100 * (abs(0.40 - h["close"]) - abs(0.50 - h["close"])),
            )
            self.assertEqual(
                joined["endpoints"]["total_over_2_5"]["direction"], "unchanged"
            )
            # A changed indexed observation cannot replace its archived original.
            with closing(store.connect(self.root)) as db, db:
                db.execute(
                    "UPDATE ma_books SET payload='{}' WHERE sha=?",
                    (joined["receipts"]["result"]["decision"]["batch"],),
                )
            self.assertFalse(self.read()["endpoints"])

    def test_stale_coverage_and_replay_are_separate_from_market_misses(self):
        self.entry["mode"] = "replay"
        self.assertEqual(self.read()["reason"], "requires_prospective_seal")
        self.entry["mode"] = "formal"
        self.pair["coverage"]["away"]["sources"][0]["source_observed_at"] = store.iso(
            self.cut - timedelta(hours=7)
        )
        self.assertEqual(self.read()["reason"], "news_coverage_incomplete_or_stale")
        metrics = news_market.summary([])["endpoints"]["home_win"]
        self.assertEqual(metrics["pairs"], 0)
        self.assertIsNone(metrics["toward_fraction"])
        self.assertIsNone(metrics["mean_distance_gain_pp"])
        # A step may point toward the close and still overshoot it.
        overshoot = news_market._measure(0.40, 0.75, 0.30, 0.50)
        self.assertEqual(overshoot["direction"], "toward")
        self.assertLess(overshoot["distance_gain_pp"], 0)
        self.assertEqual(
            news_market._measure(0.50, 0.45, 0.60, 0.50)["direction"], "at_reference"
        )


class EraTests(unittest.TestCase):
    def test_empty_stock_round_season_and_accumulation_explain_actual_close_capability(
        self,
    ):
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(kickoff_proof.OPEN_RELEASES, clear=True),
            patch.dict(kickoff_proof.FREE_ESPN_PERMISSIONS, clear=True),
        ):
            for report in (
                news_evaluation.evaluate_round(root, SEASON, 1),
                news_evaluation.evaluate_season(root, SEASON),
                news_evaluation.evaluate_accumulation(root, SEASON),
            ):
                close = report["market_close"]
                self.assertEqual(close["status"], "actual_kickoff_source_unavailable")
                self.assertFalse(close["scheduled_clock_eligible"])
                self.assertEqual(
                    (
                        close["eligible_seals"],
                        close["verified_actual_kickoffs"],
                        close["home_win_pairs"],
                    ),
                    (0, 0, 0),
                )
                self.assertEqual(
                    close["source_capability"]["status"], "no_reviewed_free_source"
                )
                self.assertIn("reviewed source release", close["action"])
            with closing(store.connect(root)) as db:
                stored = json.loads(
                    db.execute(
                        "SELECT payload FROM reports WHERE kind='news-accumulation'"
                    ).fetchone()[0]
                )
            self.assertEqual(stored, report)

    def test_ingestion_writers_change_era_when_treatment_or_exposure_changes(self):
        import hashlib
        from polymarket861 import news

        code = Path(news.__file__).read_text()
        changes = (
            ("claims = news_sources.wire_claim(original, aliases)", "claims = []"),
            ("effective_at=effective,", "effective_at=known,"),
            ('first = old["first_seen_at"] if same else received', "first = received"),
            (
                'absence = old["absence_first_seen_at"] if old and kind and old["absence_kind"] == kind else received',
                "absence = received",
            ),
            (
                'proof["provider_state_as_of"] = store.iso(response["provider_state_as_of"])',
                'proof["provider_state_as_of"] = received',
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "news.py"

            def fingerprint(source):
                path.write_text(source)
                return plan.probability_sources(
                    root, {path.name: hashlib.sha256(source.encode()).hexdigest()}
                )

            base = fingerprint(code)
            for old, new in changes:
                with self.subTest(old=old):
                    self.assertEqual(code.count(old), 1)
                    self.assertNotEqual(base, fingerprint(code.replace(old, new)))
            for old, new in (
                (
                    "POLL_INTERVAL = timedelta(minutes=20)",
                    "POLL_INTERVAL = timedelta(minutes=25)",
                ),
                (
                    "CREATE INDEX IF NOT EXISTS news_wire_poll_team",
                    "CREATE INDEX IF NOT EXISTS news_wire_poll_team_v2",
                ),
            ):
                with self.subTest(old=old):
                    self.assertEqual(code.count(old), 1)
                    self.assertEqual(base, fingerprint(code.replace(old, new)))

    def test_wire_mapping_quarantine_and_claimant_change_era_while_transport_does_not(
        self,
    ):
        import hashlib
        from polymarket861 import news_sources

        original = Path(news_sources.__file__).read_text()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "news_sources.py"

            def fingerprint(extra=""):
                raw = (original + "\n" + extra).encode()
                path.write_bytes(raw)
                return plan.probability_sources(
                    root, {path.name: hashlib.sha256(raw).hexdigest()}
                )

            base = fingerprint()
            for name in (
                "wire_claim",
                "wire_fingerprint",
                "wire_labels",
                "_wire_rejection",
                "_wire_row",
            ):
                with self.subTest(name=name):
                    self.assertNotEqual(
                        base, fingerprint(f"def {name}(*args): return None")
                    )
            for name in ("WIRE_MODEL", "WIRE_FIELDS"):
                self.assertNotEqual(base, fingerprint(f"{name} = 'different'"))
            self.assertEqual(base, fingerprint("def _get(url): return None"))

    def test_narrow_probability_source_and_runtime_while_physical_gate_stays_whole(
        self,
    ):
        recipe = pipeline._study_recipe(pipeline._build_recipe("B1", (1.5, 2.5)))
        pair = {
            "protocol": news_model.PROTOCOL,
            "availability_basis": news_model.LOCAL,
            "policy_sha256": news_model.POLICY_SHA256,
        }
        expected = plan.era(recipe, pair, "a" * 64)
        self.assertNotIn("ledger.py", recipe["algorithm_source"])
        self.assertNotIn("ledger.py", recipe["news_probability_source"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "news_minutes.py"

            def identity(text):
                path.write_text(text)
                import hashlib

                return plan.probability_sources(
                    root, {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}
                )

            definition = (
                "RATE=2\ndef parse(x): return x*RATE\ndef latest(x): return parse(x)\n"
            )
            self.assertEqual(
                identity(definition), identity(definition + "def poll(): return 2\n")
            )
            self.assertNotEqual(
                identity(definition), identity(definition.replace("RATE=2", "RATE=3"))
            )
        changed = deepcopy(recipe)
        changed["algorithm_source"]["input_manifest.py"] = "b" * 64
        self.assertNotEqual(store.digest(changed), store.digest(recipe))
        self.assertNotEqual(plan.era(changed, pair, "a" * 64), expected)
        for new_recipe, candidate, extractor in (
            (recipe | {"lines": [2.5]}, pair, "a" * 64),
            (recipe | {"numeric_runtime": {"scipy": "other"}}, pair, "a" * 64),
            (recipe, pair | {"policy_sha256": "changed"}, "a" * 64),
            (recipe, pair, "b" * 64),
        ):
            self.assertNotEqual(plan.era(new_recipe, candidate, extractor), expected)

    def test_only_original_sealed_declaration_counts_and_scores_require_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, history = Path(tmp) / "app", Path(tmp) / "football"
            with at("2026-08-01T10:00Z"):
                seed_fixture(root, history)
            stamp = "2026-08-12T12:59Z"
            snap = {
                "fixture_id": FID,
                "cutoff": store.iso(stamp),
                "facts": [],
                "coverage": coverage(HOME, AWAY, stamp),
            }
            with patch("polymarket861.news.snapshot", return_value=snap), at(stamp):
                seal = pipeline.make_forecast(root, FID, history)
            self.assertEqual(seal["news_study"]["plan_sha256"], plan.PLAN_SHA256)
            era = seal["news_study"]["era_sha256"]
            study = news_evaluation.evaluate_accumulation(root, "2027-28")
            self.assertEqual(study["scheduled_by_season"], {"2026-27": 1, "2027-28": 0})
            early = study["eras"][era]
            self.assertEqual(
                (
                    early["eligible_seals"],
                    early["score"]["pairs"],
                    early["home_close"]["pairs"],
                ),
                (1, 0, 0),
            )
            self.assertIsNone(early["home_close"]["mean"])
            self.assertEqual(
                early["score"]["guardrail"]["status"], "insufficient_independent_rounds"
            )
            self.assertNotIn("practical_effect", study["plan"])
            self.assertIsNone(
                early["scheduled_market_close"]["home_win_regression"]["slope"]
            )
            self.assertEqual(
                news_evaluation.evaluate_accumulation(root, SEASON, mode="formal")[
                    "eras"
                ],
                {},
            )
            self.assertEqual(
                news_evaluation.evaluate_accumulation(root, SEASON, horizon="T-24h")[
                    "eras"
                ],
                {},
            )
            with self.assertRaises(ValueError):
                news_evaluation.evaluate_accumulation(root, SEASON, mode="replay")
            admit_final(root, HOME, AWAY, KICKOFF, [0, 1], "2026-08-13T10:00Z")
            later = news_evaluation.evaluate_accumulation(root, SEASON)["eras"][era]
            self.assertEqual(
                (later["score"]["pairs"], later["score"]["mean"]), (1, 0.0)
            )
            self.assertIsNone(later["score"]["interval"])
            # A valid earlier pair which never declared the new design still
            # grades under its original policy without joining the new cohort.
            with closing(store.connect(root)) as db, db:
                raw = json.loads(
                    db.execute(
                        "SELECT data_json FROM forecast_seals WHERE digest=?",
                        (seal["digest"],),
                    ).fetchone()[0]
                )
                del raw["news_study"]
                raw["news_pair"]["protocol"], raw["news_pair"]["policy_sha256"] = (
                    plan.LEGACY_POLICY
                )
                recipe = plan.sealed_recipe(db, raw)
                del raw["news_recipe_sha256"], recipe["recipe_format"]
                del recipe["news_probability_policy"], recipe["news_probability_source"]
                raw["recipe_digest"] = store.digest(recipe)
                db.execute(
                    "UPDATE model_recipes SET recipe=? WHERE version=?",
                    (store.canonical(recipe), raw["model_version"]),
                )
                db.execute(
                    "UPDATE forecast_seals SET digest=?,data_json=? WHERE digest=?",
                    (store.digest(raw), store.canonical(raw), seal["digest"]),
                )
            after = news_evaluation.evaluate_accumulation(root, SEASON)
            self.assertEqual(
                after["exclusions"], {"no_prospective_study_declaration": 1}
            )
            self.assertEqual(after["eras"], {})
            old = news_evaluation.evaluate_season(root, SEASON, policy="v1")
            self.assertEqual(
                (old["policy_sha256"], old["scored"]), (plan.LEGACY_POLICY[1], 1)
            )

    def test_round_clusters_across_seasons_include_zeros_and_separate_eras(self):
        rows = []
        for season, rnd, score, era in [
            ("2026-27", 12, 1, "first"),
            ("2026-27", 12, 0, "first"),
            ("2027-28", 12, -2, "first"),
            ("2027-28", 13, 4, "second"),
        ]:
            movement = news_market._measure(0.4, 0.4 + score / 100, 0.4, 0.5)
            rows.append(
                {
                    "season": season,
                    "round_id": rnd,
                    "sealed": True,
                    "scored": True,
                    "timely_news_coverage": True,
                    "observed_both": True,
                    "study_status": "declared",
                    "study_plan_sha256": plan.PLAN_SHA256,
                    "implementation": era,
                    "policy_sha256": era,
                    "news_protocol": "test",
                    "extractor_config_sha256": era,
                    "recipe": "physical",
                    "scores": {
                        "no_news": {"score": 50},
                        "with_news": {"score": 50 + score},
                    },
                    "market_close": {
                        "endpoints": {"home_win": movement},
                        "missing": {},
                    },
                }
            )
        rows.append(
            rows[0] | {"study_status": "unknown_or_mixed_extractor_configuration"}
        )
        out = news_accumulation.summarize(
            rows, ["2026-27", "2027-28"], "T-60m", "technical"
        )
        self.assertEqual(out["eligible_seals"], 4)
        self.assertEqual(out["eras"]["second"]["score"]["pairs"], 1)
        first = out["eras"]["first"]
        self.assertEqual(first["seasons"], {"2026-27": 2, "2027-28": 1})
        for key in ("score", "home_close"):
            one = first[key]
            self.assertEqual(
                (one["pairs"], one["rounds"], one["interval"]["family_comparisons"]),
                (3, 2, plan.PLAN["family_comparisons"]),
            )
            self.assertAlmostEqual(one["mean"], -1 / 3)
            self.assertNotIn("power", one)
        primary = first["market_close"]["endpoints"]["home_win"]
        self.assertEqual(
            (primary["toward"], primary["away"], primary["unchanged"]), (1, 1, 1)
        )
        self.assertEqual(primary["toward_fraction"], 0.5)
