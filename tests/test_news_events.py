"""Exercise research event forecasts through ingestion, verified history and SQLite."""

import json
import hashlib
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
    news_roster,
    paired_proof,
    pipeline,
    store,
)
from test_paired_proof import at, season_data
from test_news_roster import Wiki

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
        self.seeded = set()
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

    def squad(self, team, stamp, *, shared_id=None, withdrawn=False):
        # Archive the same revision and linked-identity API shapes used by the
        # operational roster adapter, including explicit missing club templates.
        wire = Wiki(store.instant(stamp))
        person = "Morgan Smith" if team == HOME or shared_id == 123 else "Alex Stone"
        prefix = int(hashlib.sha256(team.encode()).hexdigest()[:6], 16) * 100
        wire.members = [person, *(f"{team} member{i}" for i in range(10))]
        wire.identities = {name: prefix + i for i, name in enumerate(wire.members)}
        wire.identities[person] = shared_id or (123 if team == HOME else prefix)
        wire.revision = int(store.instant(stamp).timestamp())

        def transport(url):
            doc = json.loads(wire(url))
            for row in doc["query"]["pages"]:
                if "revisions" in row:
                    row["revisions"][0]["timestamp"] = "2023-07-01T09:00Z"
                    if withdrawn:
                        del row["revisions"]
                        row["missing"] = True
                elif row["title"] == person:
                    row["categories"] = [
                        {
                            "ns": 14,
                            "title": "Category:Men's association football forwards",
                        }
                    ]
            return json.dumps(doc).encode()

        with at(stamp):
            report = news_roster.refresh(self.root, team, YEAR, getter=transport)
        self.assertEqual(report["player_count"], 0 if withdrawn else 11, report)
        self.seeded.add(team)

    def story(
        self,
        stamp,
        rev,
        text,
        *,
        team=HOME,
        retracted=False,
        secondary=False,
        published=None,
    ):
        if team not in self.seeded:
            self.squad(team, "2023-08-10T08:00Z")
        with at(stamp):
            now = store.now_utc()
            row = dict(
                external_id="club-report",
                revision=rev,
                published_at=published or now,
                updated_at=published or now,
                url="https://club.example/matchday",
                title="Squad availability",
                text=text,
                attribution="Club press",
                retracted=retracted,
            )
            cfg = dict(
                id=("wire-" if secondary else "club-") + football.team_key(team),
                kind="feed",
                license="CC BY 4.0",
                origin="wire.example" if secondary else "club.example",
                authority="secondary" if secondary else "official",
                attribution="Club press",
            )

            def extract(text, players, *, authority, title, team):
                ident = next(
                    (p["player_id"] for p in players if p["name"] in text), None
                )
                if not ident:
                    return []
                return [
                    {
                        "player_id": ident,
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
            )
            self.assertEqual(collected["succeeded"], 1, collected)
            return collected

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
        self.story(FIRST, "r1", "Alex Stone is out.", team="Club 01")
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

    def test_explicit_news_expiry_restores_the_forecast_without_new_articles(self):
        self.observe()
        self.story(FIRST, "r1", "Morgan Smith is out.")
        original_snapshot = news.snapshot
        expires = store.iso("2023-08-12T08:05Z")

        def time_limited(*args):
            snap = original_snapshot(*args)
            snap["facts"] = [fact | {"valid_until": expires} for fact in snap["facts"]]
            return snap

        with patch.object(news, "snapshot", side_effect=time_limited):
            start = self.run_event(FIRST)
            self.assertEqual(start["published"], 1, start)
            self.assertEqual(self.run_event("2023-08-12T08:04Z")["attempted"], 0)
            expired = self.run_event(expires)
            self.assertEqual(expired["published"], 1, expired)
            withdrawn, original = news_events.list_updates(self.root)
            self.assertEqual(withdrawn["previous_digest"], original["digest"])
            self.assertEqual(withdrawn["trigger"], "news_removed")
            self.assertEqual(
                withdrawn["news_pair"]["excluded_reasons"], {"expired_claim": 1}
            )
            self.assertFalse(withdrawn["news_pair"]["changed"])
            self.assertEqual(self.run_event("2023-08-12T09:00Z")["published"], 0)

    def test_kickoff_crossed_during_calculation_cannot_append_event(self):
        self.observe()
        self.story(FIRST, "r1", "Morgan Smith is out.")
        clock = [store.iso(FIRST)]
        arms = pipeline._forecast_arms

        def kickoff(*args, **kwargs):
            result = arms(*args, **kwargs)
            clock[0] = store.iso(KICK)
            return result

        with (
            at(FIRST),
            patch.object(news_events, "_now", side_effect=lambda: clock[0]),
            patch.object(pipeline, "_forecast_arms", side_effect=kickoff),
        ):
            out = news_events.refresh(self.root)
        self.assertEqual(out["deferred"], 1, out)
        self.assertEqual(news_events.list_updates(self.root), [])
        self.assertEqual(store.list_reports(self.root, "news-update"), [])

    def test_roster_withdrawn_during_calculation_cannot_seal_a_stale_event(self):
        self.observe()
        self.story(FIRST, "r1", "Morgan Smith is out.")
        arms = pipeline._forecast_arms

        def withdrawal(*args, **kwargs):
            self.squad(HOME, FIRST, withdrawn=True)
            return arms(*args, **kwargs)

        with at(FIRST), patch.object(pipeline, "_forecast_arms", side_effect=withdrawal):
            out = news_events.refresh(self.root)
        self.assertEqual(out["deferred"], 1, out)
        self.assertEqual(self.run_event(FIRST)["unchanged"], 1)
        self.assertEqual(news_events.list_updates(self.root), [])

    def test_roster_withdrawal_restoration_and_other_club_arrivals_wake_the_fixture(
        self,
    ):
        self.observe()
        self.story(FIRST, "r1", "Morgan Smith is out.")
        first = self.run_event(FIRST)
        self.assertEqual(first["published"], 1, first)
        self.squad(HOME, "2023-08-12T08:01Z", withdrawn=True)
        withdrawn = self.run_event("2023-08-12T08:01Z")
        self.assertEqual(withdrawn["published"], 1, withdrawn)
        self.squad(HOME, "2023-08-12T08:17Z")
        self.assertEqual(self.run_event("2023-08-12T08:17Z")["published"], 1)
        # A separate club changing an unrelated player is harmless. A new
        # registration of our exact player must neutralize his forecast.
        self.squad("Club 07", "2023-08-12T08:18Z")
        unrelated = self.run_event("2023-08-12T08:18Z")
        self.assertEqual(unrelated["published"], 0, unrelated)
        self.squad("Club 08", "2023-08-12T08:19Z", shared_id=123)
        conflict = self.run_event("2023-08-12T08:19Z")
        self.assertEqual(conflict["published"], 1, conflict)
        conflict_doc, restored, removed, original = news_events.list_updates(
            self.root, self.fid
        )
        self.assertEqual(original["digest"], first["items"][0]["digest"])
        self.assertEqual(
            original["news_pair"]["with_news"], restored["news_pair"]["with_news"]
        )
        self.assertEqual(
            [d["trigger"] for d in (removed, conflict_doc)], ["news_removed"] * 2
        )
        self.assertEqual(
            removed["snapshot"]["coverage"]["home"]["roster"]["state"], "withdrawn"
        )
        self.assertFalse(conflict_doc["news_pair"]["changed"])
        self.assertEqual(conflict_doc["previous_digest"], restored["digest"])
        self.assertTrue(news.snapshot(self.root, self.fid, FIRST)["facts"])

    def test_other_club_then_own_roster_expire_at_the_exact_eligibility_boundaries(
        self,
    ):
        self.observe()
        # Two overlapping registrations expire three minutes apart. The source
        # article is retained while identification is ambiguous.
        self.squad("Club 08", "2023-08-05T08:05Z", shared_id=123)
        self.squad(HOME, "2023-08-05T08:08Z")
        story = "Morgan Smith is out."
        self.assertEqual(self.story(FIRST, "r1", story)["claims_added"], 0)
        self.assertEqual(self.run_event(FIRST)["published"], 0)
        self.assertEqual(self.run_event("2023-08-12T08:05:00Z")["attempted"], 0)
        boundary = "2023-08-12T08:05:00.000001Z"
        wake = self.run_event(boundary)
        self.assertEqual((wake["attempted"], wake["published"]), (1, 0), wake)
        rechecked = self.story(boundary, "r1", story, published=FIRST)
        self.assertEqual(
            (rechecked["articles_added"], rechecked["claims_added"]), (0, 1)
        )
        after = self.run_event(boundary)
        self.assertEqual(after["published"], 1, after)
        self.assertEqual(self.run_event("2023-08-12T08:08:00Z")["attempted"], 0)
        expired = self.run_event("2023-08-12T08:08:00.000001Z")
        self.assertEqual(expired["published"], 1, expired)
        current, active = news_events.list_updates(self.root, self.fid)
        self.assertEqual(current["previous_digest"], active["digest"])
        self.assertEqual(
            current["snapshot"]["coverage"]["home"]["roster"]["state"], "expired"
        )
        self.assertFalse(current["news_pair"]["changed"])

    def test_later_secondary_agreement_cannot_emit_an_event_or_downgrade_official_support(
        self,
    ):
        self.observe()
        self.story(FIRST, "club1", "Morgan Smith is out.")
        self.assertEqual(self.run_event(FIRST)["published"], 1)
        self.story("2023-08-12T08:02Z", "wire1", "Morgan Smith is out.", secondary=True)
        agreement = self.run_event("2023-08-12T08:02Z")
        self.assertEqual(
            (agreement["published"], agreement["unchanged"]), (0, 1), agreement
        )
        original = news_events.list_updates(self.root, self.fid)[0]
        self.assertEqual(original["news_pair"]["applied"][0]["prior_weight"], 0.55)
        self.story("2023-08-12T08:03Z", "club2", "", retracted=True)
        downgrade = self.run_event("2023-08-12T08:03Z")
        self.assertEqual(downgrade["published"], 1, downgrade)
        downgraded = news_events.list_updates(self.root, self.fid)[0]
        self.assertEqual(downgraded["news_pair"]["applied"][0]["prior_weight"], 0.45 * 0.55)
        self.assertEqual(downgraded["previous_digest"], original["digest"])
        self.assertNotEqual(
            downgraded["news_pair"]["with_news"], original["news_pair"]["with_news"]
        )
