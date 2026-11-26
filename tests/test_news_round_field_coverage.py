"""Daily reports retain the pre-match player/field denominators seen by the model."""

from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from polymarket861 import (
    news,
    news_evaluation,
    news_field_coverage,
    news_sources,
    pipeline,
    round_history,
    store,
)
from test_news_ingest import registered_squad
from test_news_matchday import AWAY, FID, HOME, SEASON, seed_fixture
from test_news_sources import availability, run, wire_row
from test_paired_proof import at


class NewsRoundFieldTests(unittest.TestCase):
    def test_two_primary_horizons_and_later_correction_in_round_and_news_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            root, history = Path(temp) / "app", Path(temp) / "history"
            with at("2026-08-01T10:00Z"):
                seed_fixture(root, history)

            def sync(team, rows):
                source = news_sources.default_sources(team, SEASON)[0]
                stamp = store.now_utc()
                news.sync(
                    root,
                    team=team,
                    force=True,
                    defaults=lambda *_: [source],
                    poller=lambda src, *_: run(
                        src, availability(rows, stamp), at=stamp
                    ),
                    roster_refresh=lambda *_: {"status": "cached"},
                )

            def report(horizon):
                return news_evaluation.evaluate_round(root, SEASON, 1, horizon=horizon)[
                    "availability_fields"
                ]

            with at("2026-08-11T13:20Z"):
                no_seal = round_history.publish_round(root, SEASON, 1, mode="technical")
                self.assertIsNone(
                    no_seal["news_availability_fields"]["suspensions"]["scope_excluded"]
                )
                self.assertEqual(
                    no_seal["news_availability_fields"]["unobserved_team_fixtures"], 20
                )
                registered_squad(root, HOME, SEASON)
                registered_squad(
                    root, AWAY, SEASON, person="Leo James", shared_id=124, prefix=2000
                )
                sync(HOME, [])
            with at("2026-08-11T13:59Z"):
                pipeline.make_forecast(root, FID, history, horizon="T-24h")
                earlier = report("T-24h")
                self.assertEqual(
                    (
                        earlier["observed_team_fixtures"],
                        earlier["unobserved_team_fixtures"],
                    ),
                    (1, 19),
                )
                self.assertEqual(earlier["suspensions"]["observed"], 0)
            ban = wire_row() | {
                "team": "Arsenal",
                "display_name": "Morgan Smith",
                "category": "suspension",
                "status": "suspended",
                "chance_of_playing": 0,
            }
            injury = wire_row() | {
                "player_id": 6,
                "team": "Chelsea",
                "display_name": "Leo James",
                "injury_date": "2026-08-05",
            }
            for stamp in ("2026-08-12T12:15Z", "2026-08-12T12:35Z"):
                with at(stamp):
                    sync(HOME, [ban])
                    sync(AWAY, [injury])
            with at("2026-08-12T12:59Z"):
                entry = pipeline.make_forecast(root, FID, history, horizon="T-60m")
                later = report("T-60m")
                self.assertEqual(
                    (
                        later["observed_team_fixtures"],
                        later["unobserved_team_fixtures"],
                    ),
                    (2, 18),
                )
                self.assertEqual(
                    later["suspensions"]["scope_excluded_without_named_competition"], 1
                )
                self.assertEqual(
                    (
                        later["suspensions"]["applied"],
                        later["injuries"]["with_injury_date_only"],
                    ),
                    (0, 1),
                )
                self.assertEqual(later["injuries"]["observed"], 1)
                base = next(p for p in later["players"] if p["event"] == "suspension")
                self.assertEqual(
                    (base["player"], base["status"], base["source"]),
                    ("Morgan Smith", "reported", news_sources.WIRE),
                )
                self.assertEqual(
                    (base["known_at"], base["observed_at"], base["input_cutoff"]),
                    (
                        store.iso("2026-08-12T12:15Z"),
                        store.iso("2026-08-12T12:35Z"),
                        entry["news_pair"]["input_cutoff"],
                    ),
                )
            with at("2026-08-12T13:12Z"):
                sync(HOME, [ban | {"suspension_competition": "Premier League"}])
                changed = news.snapshot(root, FID, store.now_utc())["facts"]
                self.assertTrue(
                    any(
                        row.get("suspension_competition") == "Premier League"
                        for row in changed
                    )
                )
                published = round_history.publish_round(
                    root, SEASON, 1, mode="technical"
                )
                self.assertEqual(published["news_study"]["availability_fields"], later)
                self.assertEqual(report("T-24h"), earlier)
                season = news_evaluation.evaluate_season(root, SEASON)[
                    "availability_fields"
                ]
                self.assertEqual(season["expected_team_fixtures"], 760)
                self.assertEqual(season["suspensions"], later["suspensions"])

    def test_partial_legacy_and_foreign_scope_are_separate_from_absent_and_applied(
        self,
    ):
        record = {
            "fixture_id": FID,
            "season": SEASON,
            "home": HOME,
            "away": AWAY,
            "kickoff": "2026-08-12T14:00:00.000000Z",
            "input_cutoff": store.iso("2026-08-12T13:00Z"),
            "sealed": True,
            "seal": "test",
        }
        stamp = store.iso("2026-08-12T12:35Z")
        base = {
            "fixture_id": FID,
            "kickoff": record["kickoff"],
            "team": AWAY,
            "time_basis": "provider_snapshot",
            "known_at": stamp,
            "last_seen_at": stamp,
            "last_seen_receipt": "retained",
            "source": news_sources.WIRE,
            "model_version": news_sources.WIRE_MODEL,
            "status": "reported",
        }
        source = {
            "id": news_sources.WIRE,
            "status": "partial",
            "recorded_kickoff": record["kickoff"],
            "checked_at": stamp,
            "source_observed_at": stamp,
            "provider_state_as_of": stamp,
            "coverage_evidence": news_sources.news_scope.wire_coverage(AWAY, SEASON),
        }
        coverage = {"away": {"team": AWAY, "sources": [source]}}

        def summarize(facts, applied=(), excluded=(), *, sources=coverage):
            doc = {
                "context": {"player_news": {"coverage": sources, "facts": facts}},
                "news_pair": {
                    "applied": [{"id": x} for x in applied],
                    "excluded": [
                        {"id": x, "reason": "suspension_competition_unverified"}
                        for x in excluded
                    ],
                },
            }
            with (
                tempfile.TemporaryDirectory() as temp,
                closing(store.connect(temp)) as db,
            ):
                db.execute(
                    "INSERT INTO forecast_seals VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        FID,
                        record["kickoff"],
                        record["input_cutoff"],
                        "T-60m",
                        "B1",
                        store.PROTOCOL,
                        "technical",
                        stamp,
                        "test",
                        store.canonical(doc),
                    ),
                )
                return news_field_coverage.summarize(db, [record], 10)

        partial_empty = summarize([])
        self.assertEqual(partial_empty["partial_team_fixtures"], 1)
        self.assertIsNone(partial_empty["suspensions"]["observed"])
        cup = base | {
            "id": "cup",
            "player_id": "p1",
            "event": "suspension",
            "suspension_competition": "FA Cup",
        }
        league = cup | {
            "id": "league",
            "player_id": "p2",
            "suspension_competition": "Premier League",
        }
        unknown = base | {"id": "unknown", "player_id": "p3", "event": "suspension"}
        exact = base | {
            "id": "exact",
            "player_id": "p4",
            "event": "injury",
            "injury_started_at": "2026-08-01T09:00:00Z",
        }
        undated = base | {"id": "undated", "player_id": "p5", "event": "injury"}
        future = base | {
            "id": "future",
            "player_id": "p6",
            "event": "injury",
            "known_at": store.iso("2026-08-12T13:30Z"),
        }
        result = summarize(
            [cup, cup, league, unknown, exact, undated, future],
            ["league", "exact"],
            ["cup", "unknown"],
        )
        self.assertEqual(
            (
                result["suspensions"]["observed"],
                result["suspensions"]["scope_excluded"],
                result["suspensions"]["applied"],
            ),
            (3, 2, 1),
        )
        self.assertEqual(
            result["suspensions"][
                "scope_excluded_with_other_or_unrecognized_competition"
            ],
            1,
        )
        self.assertEqual(
            (
                result["injuries"]["observed"],
                result["injuries"]["with_injury_started_at"],
                result["injuries"]["no_native_onset"],
            ),
            (2, 1, 1),
        )
        self.assertEqual(
            summarize([unknown], excluded=["unknown"], sources={})["suspensions"][
                "scope_excluded"
            ],
            1,
        )
        legacy = {"model_version": "touchline-availability-fields-v1"}
        old = summarize(
            [unknown | legacy, undated | legacy], ["unknown", "undated"], sources={}
        )
        self.assertEqual(old["status"], "legacy_fields_unverified")
        self.assertEqual(
            (
                old["suspensions"]["applied"],
                old["injuries"]["field_support_unverified"],
            ),
            (1, 1),
        )
        self.assertIsNone(old["suspensions"]["scope_excluded"])
        self.assertIsNone(old["injuries"]["with_native_onset"])
