"""Join prospective news, frozen forecasts, exchange books and regulation results."""

from datetime import timedelta
import json
import unittest
from unittest.mock import patch

from polymarket861 import (
    datasets,
    football,
    news,
    news_roster,
    news_sources,
    paired_proof,
    pipeline,
    store,
)
from polymarket861.market_provider import Reply
import test_news_endpoint_reference_v2 as close
from test_news_endpoints_v2 import capture_clock, cli
import test_news_matchday as clubs
import test_opportunity as market
from test_paired_proof import season_data
import test_schedule_consensus as schedule


def attach_training_history(root):
    """Preserve the admitted current schedule and supply native past seasons for B1."""
    history = root / "verified_history"
    blobs = {}
    for year in range(2023, 2027):
        label = f"{year}-{(year + 1) % 100:02d}"
        source = history / label / "en.1.json"
        source.parent.mkdir(parents=True)
        if year == 2026:
            source.write_bytes((root / "fixtures" / label / "en.1.json").read_bytes())
        else:
            body = season_data(year, finished=True)
            for game in body["matches"]:
                for side in ("team1", "team2"):
                    game[side] = {"Club 00": market.HOME, "Club 19": market.AWAY}.get(
                        game[side], game[side]
                    )
            source.write_text(json.dumps(body))
        blobs[label] = football.blob_sha(source.read_bytes())
    revision = "4" * 40
    paired_proof.attest_provider_files(root, revision, blobs)
    datasets.observe(root, history, revision=revision, expected=blobs)
    pipeline.prepare_builtin(root)
    return history


class NewsEndpointsPublicFlow(unittest.TestCase):
    def test_native_news_prediction_and_books_reach_only_the_eligible_clock_at_both_decisions(
        self,
    ):
        sample = close.DualScheduleEndpoints()
        sample.setUp()
        self.addCleanup(sample.doCleanups)
        root, kickoff = sample.root, sample.kick
        sample.clock.time = kickoff - timedelta(hours=27)
        history = attach_training_history(root)
        feeds = [clubs.add_club_feed(root, team) for team in (market.HOME, market.AWAY)]
        by_url = {f["url"]: f for f in feeds}

        def fetch(url):
            return Reply(
                clubs.rss(by_url[url]), store.now_utc(), store.now_utc(), {}, url
            )

        with capture_clock():
            cli(root, "register", "--version", "v2")
            for horizon, cutoff in zip(("T-24h", "T-60m"), sample.cutoffs, strict=True):
                sample.schedules(cutoff - timedelta(minutes=45))
                sample.clock.time = cutoff - timedelta(minutes=5)
                # Separate fictitious roster identities and real licensed empty RSS bytes.
                with (
                    patch.object(news_sources, "_get", side_effect=fetch),
                    patch.object(clubs, "HOME", market.HOME),
                ):
                    for team in (market.HOME, market.AWAY):
                        news.sync(
                            root,
                            team=team,
                            season=schedule.SEASON,
                            force=True,
                            workers=1,
                            defaults=lambda *_: [],
                            roster_refresh=lambda r, t, s: news_roster.refresh(
                                r, t, s, getter=clubs.roster_transport(t)
                            ),
                        )
                sample.batch(cutoff - timedelta(seconds=3))
                sample.clock.time = cutoff
                seal = pipeline.make_forecast(
                    root, schedule.FID, history, horizon=horizon
                )
                self.assertTrue(seal["news_pair"]["coverage"]["both_teams_observed"])
            sample.batch(sample.close, shift=2)
            sample.scores()
            sample.clock.time = sample.post
            interim = cli(root, "report")
            sample.clock.time = store.instant("2028-07-01T00:00Z")
            final = cli(root, "report")
            self.assertEqual(
                (interim["state"], final["state"], final["bound_seals"]),
                ("prospective_collection", "window_ended", 2),
            )
            for horizon, record in final["horizons"].items():
                self.assertEqual(
                    (
                        record["qualified_news_pairs"],
                        record["score_guardrail"]["pairs"],
                    ),
                    (1, 1),
                )
                for endpoint in ("home_win", "full_over_2_5"):
                    counts = {
                        field: row[endpoint]["matched_books"]
                        for field, row in record["clocks"].items()
                    }
                    self.assertEqual(
                        counts,
                        {
                            "market_close": 0,
                            "scheduled_market_close": 0,
                            "dual_schedule_close": 1,
                        },
                    )
                    before = interim["horizons"][horizon]["clocks"][
                        "dual_schedule_close"
                    ][endpoint]
                    self.assertEqual(before["status"], "sealed_until_fixed_end")
                    self.assertIsNone(before["slope"])
                    self.assertEqual(
                        record["clocks"]["dual_schedule_close"][endpoint]["status"],
                        "no_informative_news_movements",
                    )
