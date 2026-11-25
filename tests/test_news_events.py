"""Exercise research event forecasts through ingestion, verified history and SQLite."""

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from polymarket861 import (
    datasets,
    football,
    news,
    news_evaluation,
    news_events,
    paired_proof,
    pipeline,
    store,
)
from test_paired_proof import at, season_data

YEAR = "2023-24"
HOME, AWAY = "Club 00", "Club 19"
KICK = "2023-08-12T14:00Z"
FIRST = "2023-08-12T08:00Z"


class EventNewsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "runtime"
        self.history = Path(tmp.name) / "seasons"
        for y in range(2020, 2024):
            f = self.history / f"{y}-{(y + 1) % 100:02d}" / "en.1.json"
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps(season_data(y, y != 2023)))
        self.fid = football.fixture_id(YEAR, HOME, AWAY)
        self.register(HOME, AWAY)

    def register(self, home, away, kickoff=KICK):
        fid = football.fixture_id(YEAR, home, away)
        with at("2023-08-01T10:00Z"):
            store.register_fixture(
                self.root,
                dict(
                    fixture_id=fid,
                    season=YEAR,
                    round_id=1,
                    home=home,
                    away=away,
                    kickoff=kickoff,
                    known_at=store.now_utc(),
                    source="league",
                    locator="schedule/" + fid,
                ),
            )
        return fid

    def observe(self, when="2023-08-03T10:00Z"):
        blobs = {
            p.parent.name: football.blob_sha(p.read_bytes())
            for p in self.history.glob("*/en.1.json")
        }
        with at(when):
            paired_proof.attest_provider_files(self.root, "c" * 40, blobs)
            datasets.observe(self.root, self.history, revision="c" * 40, expected=blobs)

    def story(self, stamp, rev, text, *, team=HOME, retracted=False):
        with at(stamp):
            now = store.now_utc()
            row = dict(
                external_id="club-report",
                revision=rev,
                published_at=now,
                updated_at=now,
                url="https://club.example/matchday",
                title="Squad availability",
                text=text,
                attribution="Club press",
                retracted=retracted,
            )
            cfg = dict(
                id="club-" + football.team_key(team),
                kind="feed",
                license="CC BY 4.0",
                origin="club.example",
                authority="official",
                attribution="Club press",
            )

            def extract(text, players, *, authority, title, team):
                return [
                    {
                        "player_id": players[0]["player_id"],
                        "event": "injury",
                        "availability": "doubtful" if "doubtful" in text else "out",
                        "status": "confirmed",
                        "quote": text,
                        "start": 0,
                        "end": len(text),
                        "model_version": "local-test-model",
                    }
                ]

            collected = news.sync(
                self.root,
                team=team,
                force=True,
                defaults=lambda *args: [cfg],
                poller=lambda *args: {"articles": [row], "cursor": {}},
                extractor=extract,
                roster_refresh=lambda *args: {"status": "cached"},
                roster_reader=lambda *args: [
                    {
                        "player_id": "P1",
                        "name": "Morgan Smith",
                        "team": team,
                        "position": "forward",
                    }
                ],
            )
            self.assertEqual(collected["succeeded"], 1, collected)

    def run_event(self, stamp, **kw):
        with at(stamp):
            return news_events.refresh(self.root, **kw)

    def test_renewals_retractions_chain_and_result_denominator(self):
        self.observe()
        self.story(FIRST, "r1", "Morgan Smith is out.")
        first = self.run_event(FIRST)
        self.assertEqual(first["published"], 1, first)
        ident = first["items"][0]["digest"]
        self.story("2023-08-12T08:20Z", "r2", "Morgan Smith is still out.")
        same = self.run_event("2023-08-12T08:20Z")
        self.assertEqual((same["unchanged"], same["published"]), (1, 0), same)
        self.story("2023-08-12T09:00Z", "r3", "Morgan Smith is doubtful.")
        self.assertEqual(self.run_event("2023-08-12T09:00Z")["published"], 1)
        self.story("2023-08-12T10:00Z", "r4", "Morgan Smith is out again.")
        self.assertEqual(self.run_event("2023-08-12T10:00Z")["published"], 1)
        self.story("2023-08-12T11:00Z", "r5", "", retracted=True)
        self.assertEqual(self.run_event("2023-08-12T11:00Z")["published"], 1)
        docs = news_events.list_updates(self.root, self.fid)
        self.assertEqual([d["revision"] for d in docs], [4, 3, 2, 1])
        self.assertEqual(ident, docs[-1]["digest"])
        self.assertEqual(
            [d["previous_digest"] for d in docs],
            [d["digest"] for d in docs[1:]] + [None],
        )
        self.assertEqual(docs[1]["state_sha256"], docs[3]["state_sha256"])
        self.assertNotEqual(docs[1]["digest"], docs[3]["digest"])
        self.assertEqual(docs[0]["trigger"], "news_removed")
        self.assertEqual(
            docs[0]["news_pair"]["no_news"], docs[0]["news_pair"]["with_news"]
        )
        self.assertTrue(docs[-1]["news_pair"]["changed"])
        self.assertEqual(
            docs[-1]["news_pair"]["facts_sha256"],
            store.digest(docs[-1]["snapshot"]["facts"]),
        )
        self.assertTrue(docs[-1]["snapshot"]["facts"][0]["source_receipt"])
        self.assertEqual(len(store.list_reports(self.root, "news-update")), 4)
        with closing(store.connect(self.root)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM forecast_seals").fetchone()[0], 0
            )
            self.assertEqual(news_evaluation.round_summary(db, YEAR, 1)["sealed"], 0)
        self.assertEqual(self.run_event(KICK)["attempted"], 0)
        self.assertEqual(docs, news_events.list_updates(self.root, limit=4))

    def test_waiting_for_history_is_durable_and_other_fixtures_are_serviced(self):
        self.register("Club 01", "Club 18")
        self.story(FIRST, "r1", "Morgan Smith is out.")
        self.story(FIRST, "r1", "Morgan Smith is out.", team="Club 01")
        first = self.run_event(FIRST, limit=1)
        second = self.run_event(FIRST, limit=1)
        self.assertEqual((first["deferred"], second["deferred"]), (1, 1))
        self.assertNotEqual(
            first["items"][0]["fixture_id"], second["items"][0]["fixture_id"]
        )
        self.assertEqual(self.run_event(FIRST)["state"], "idle")
        self.assertEqual(news_events.list_updates(self.root), [])
        self.observe("2023-08-12T08:01Z")
        recovered = self.run_event("2023-08-12T08:01Z", history_root=self.history)
        self.assertEqual(recovered["published"], 2, recovered)
        self.assertTrue(
            all(
                d["history_observed_at"] <= d["cutoff"]
                for d in news_events.list_updates(self.root)
            )
        )

    def test_arrival_supersedes_inflight_computation_and_a_second_worker_does_not_duplicate(
        self,
    ):
        self.observe()
        self.story(FIRST, "r1", "Morgan Smith is out.")
        entered, release = threading.Event(), threading.Event()
        arms = pipeline._forecast_arms

        def blocked(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(8))
            return arms(*args, **kwargs)

        with (
            at(FIRST),
            patch.object(pipeline, "_forecast_arms", side_effect=blocked),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(news_events.refresh, self.root)
            try:
                self.assertTrue(entered.wait(8))
                self.assertEqual(news_events.refresh(self.root)["attempted"], 0)
                self.story(FIRST, "r2", "Morgan Smith is doubtful.")
            finally:
                release.set()
            raced = future.result(timeout=8)
        self.assertEqual(raced["deferred"], 1, raced)
        self.assertEqual(news_events.list_updates(self.root), [])
        after = self.run_event("2023-08-12T08:01Z")
        self.assertEqual(after["published"], 1, after)
        self.assertEqual(
            news_events.list_updates(self.root)[0]["news_pair"]["applied"][0][
                "availability"
            ],
            "doubtful",
        )
