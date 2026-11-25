"""A released binary can read its predecessor's seals and can run the next fixture."""

import json
from itertools import count
import unittest
from contextlib import closing, contextmanager
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal
from unittest.mock import patch

import test_opportunity as matchday
import test_runner as scheduled
from release_fixture import approved
from polymarket861 import jobs, opportunity, pipeline, round_history, runner, store


@contextmanager
def old_build():
    current = pipeline._build_recipe

    def prior(family, lines):
        record = current(family, lines)
        return record | {
            "algorithm_source": {k: "a" * 64 for k in record["algorithm_source"]}
        }

    with patch.object(pipeline, "_build_recipe", side_effect=prior):
        yield


class RecipeUpgrade(unittest.TestCase):
    def test_retrieve_hash_and_identity_before_executability_and_keep_explicit_versions(
        self,
    ):
        setup = scheduled.RunnerTests()
        setup.setUp()
        self.addCleanup(setup.doCleanups)
        first, second = setup.fixtures(2)
        data = setup.data

        def early(fx, version=None):
            return pipeline.make_forecast(
                data, fx["fixture_id"], None, horizon="early", version=version
            )

        with scheduled.clock("2030-08-15T08:00Z"), old_build():
            stored = early(first)
            custom = pipeline.register_recipe(data, "pinned-study", "B1")
        with scheduled.clock("2030-08-16T08:00Z"):
            self.assertEqual(early(first), stored)
            with self.assertRaisesRegex(ValueError, "release-compare"):
                early(second)
            self.assertEqual(pipeline.prepare_builtin(data), "B1")
            approved(data)
            ticks = count()
            start = store.instant("2030-08-16T08:00Z")

            def ticking():
                return store.iso(start + timedelta(microseconds=next(ticks)))

            with (
                patch.object(store, "now_utc", side_effect=ticking),
                patch.object(pipeline, "now_utc", side_effect=ticking),
            ):
                new = early(second)
            successor = new["model_version"]
            self.assertEqual(successor, "B1@" + new["recipe_digest"])
            self.assertEqual(new["purpose"], "main")
            self.assertEqual(early(first), stored)
            self.assertEqual(early(first, "B1"), stored)
            with scheduled.clock("2030-08-16T08:05Z"):
                self.assertEqual(early(second), new)
            self.assertEqual(
                jobs.active_version(
                    data, "model", "epl", default="B1", at="2030-08-15T08:00Z"
                ),
                "B1",
            )
            with self.assertRaisesRegex(ValueError, "different recipe"):
                early(second, "B1")
            with self.assertRaisesRegex(ValueError, "different recipe"):
                early(first, "pinned-study")
            with closing(store.connect(data)) as db, db:
                self.assertEqual(
                    store.digest(
                        json.loads(
                            db.execute(
                                "SELECT recipe FROM model_recipes WHERE version='pinned-study'"
                            ).fetchone()[0]
                        )
                    ),
                    custom["recipe_digest"],
                )
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM builtin_model_releases"
                    ).fetchone()[0],
                    1,
                )
                body = dict(stored)
                body.pop("digest")
                body.update(fixture_id=second["fixture_id"])
                db.execute(
                    "UPDATE forecast_seals SET data_json=?,digest=? WHERE digest=?",
                    (store.canonical(body), store.digest(body), stored["digest"]),
                )
            with self.assertRaisesRegex(ValueError, "identity"):
                early(first, "B1")

    def test_rollout_after_timely_seal_does_not_replace_formal_decision_or_main_order(
        self,
    ):
        case = matchday.Opportunities()
        case.setUp()
        self.addCleanup(case.doCleanups)
        with old_build():
            original = case.seal()
        with matchday.at(matchday.CUT - timedelta(seconds=45)):
            successor = approved(case.root)
            self.assertNotEqual(successor, original["model_version"])
            resumed = runner.run_due(case.root)
            slot = next(
                r
                for r in resumed["jobs"]
                if r["fixture_id"] == matchday.FID and r["horizon"] == "T-60m"
            )
            self.assertEqual(
                (slot["version"], slot["digest"]), ("B1", original["digest"])
            )
            self.assertEqual(
                pipeline.make_forecast(case.root, matchday.FID, None, mode="formal")[
                    "digest"
                ],
                original["digest"],
            )
        fair = Decimal(str(original["probabilities"]["total_over_2_5"]))
        case.book(
            matchday.CUT - timedelta(seconds=7),
            (fair - Decimal(".04")).quantize(Decimal(".01"), rounding=ROUND_FLOOR),
        )
        with matchday.at(matchday.CUT - timedelta(seconds=5)):
            placed = opportunity.process(case.root, matchday.SLUG)
            self.assertIn(
                "frozen", [d.get("state") for d in placed["decisions"]], placed
            )

    def test_new_t60_dispatch_main_ledger_and_corrected_round_use_physical_release(
        self,
    ):
        case = matchday.Opportunities()
        case.setUp()
        self.addCleanup(case.doCleanups)
        root = case.root
        with matchday.at(case.early), old_build():
            baseline = pipeline.register_recipe(root, "B1", "B1")
        # The helper prepares the normal registered schedule, training archive, two confirmed lineups and a formal account window.
        # The scheduler, rather than the setup helper, produces the first forecast in this test.
        with patch.object(pipeline, "make_forecast", return_value=None):
            case.seal()
        with matchday.at(matchday.CUT - timedelta(minutes=1)):
            approved(root)
            dispatched = runner.run_due(root)
            slot = next(
                r
                for r in dispatched["jobs"]
                if r["fixture_id"] == matchday.FID and r["horizon"] == "T-60m"
            )
            self.assertEqual(slot["state"], "done")
            new = pipeline.make_forecast(root, matchday.FID, None, mode="formal")
            version = new["model_version"]
            self.assertEqual(version, slot["version"])
            self.assertNotEqual(new["recipe_digest"], baseline["recipe_digest"])
            self.assertEqual(runner.run_due(root)["counts"]["already_done"], 2)
        fair = Decimal(str(new["probabilities"]["total_over_2_5"]))
        price = (fair - Decimal(".04")).quantize(Decimal(".01"), rounding=ROUND_FLOOR)
        case.book(matchday.CUT - timedelta(seconds=7), price)
        with matchday.at(matchday.CUT - timedelta(seconds=5)):
            placed = opportunity.process(root, matchday.SLUG)
            self.assertIn(
                "frozen", [d.get("state") for d in placed["decisions"]], placed
            )
        for i, goals in enumerate([(2, 0), (1, 0)]):
            when = matchday.KICK + timedelta(hours=3 + i)
            with matchday.at(when):
                result = pipeline.import_result_and_score(
                    root,
                    {
                        "fixture_id": matchday.FID,
                        "home_goals": goals[0],
                        "away_goals": goals[1],
                        "source": "test-official",
                        "locator": "test://score/" + str(i),
                        "finalized_at": store.iso(when),
                        "published_at": store.iso(when),
                        "received_at": store.iso(when),
                    },
                )
                self.assertEqual(result["round_reports"][0]["model_version"], version)
                self.assertEqual(
                    pipeline.publish_round(root, matchday.CHAMP, 26)["model_version"],
                    version,
                )
        history = round_history.round_history(root, matchday.CHAMP, 26)
        self.assertEqual(history["count"], 2)
        self.assertTrue(history["round_key"].endswith("/" + version))
        self.assertEqual(
            round_history.round_history(root, matchday.CHAMP, 26, version="B1")[
                "count"
            ],
            0,
        )
