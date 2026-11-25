import json
import tempfile
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from polymarket861 import independent_results as wiki
from polymarket861 import result_feed, result_freshness, store
from polymarket861.football import fixture_id


class WikiEvidence(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.teams = [f"Club {n} FC" for n in range(20)]
        self.titles = tuple(sorted(wiki._title(2026, team) for team in self.teams))
        self.start = "2026-11-21T18:00:00Z"
        self.seen = store.iso(self.start)
        self.page1, self.page2 = (wiki._title(2026, team) for team in self.teams[:2])
        self.fixture = fixture_id("2026-27", *self.teams[:2])

    def box(
        self,
        result="W",
        score="2–0",
        *,
        time="15:00 [[Greenwich Mean Time|GMT]]",
        mid=8123,
        home="Club 0",
        away="Club 1",
        round=11,
    ):
        return (
            "{{football box collapsible\n"
            + f"|round = {round}\n|date = 21 November 2026\n"
            + f"|time = {time}\n|team1 = {home}\n|team2 = {away}\n|score = {score}\n|result = {result}\n"
            + f"|report = https://www.premierleague.com/en/match/{mid}\n"
            + "}}\n"
        )

    def raw(self, articles, *, licensed=True, published=None):
        pages = []
        for i, title in enumerate(self.titles):
            pages.append(
                {
                    "title": title,
                    "pageid": i + 1,
                    "ns": 0,
                    "revisions": [
                        {
                            "revid": 100 + i,
                            "timestamp": published or self.start,
                            "slots": {
                                "main": {
                                    "contentmodel": "wikitext",
                                    "content": "===Premier League===\n"
                                    + articles.get(title, "")
                                    + "\n===FA Cup===\n"
                                    + self.box(score="8–0"),
                                }
                            },
                        }
                    ],
                }
            )
        return json.dumps(
            {
                "query": {
                    "rightsinfo": {"url": wiki.LICENSE if licensed else "unknown"},
                    "pages": pages,
                }
            }
        ).encode()

    def receive(self, articles):
        with (
            patch.object(wiki, "_get", return_value=self.raw(articles)),
            patch.object(store, "now_utc", return_value=self.seen),
        ):
            return wiki._receive(self.root, 2026, self.teams)

    def history(self, at=None):
        with closing(store.connect(self.root)) as db:
            return wiki.verified(self.root, db, 2026, at or self.seen).get(
                self.fixture, []
            )

    def test_native_receipts_cutoff_withdrawal_conflict_and_corruption(self):
        pair = {self.page1: self.box(), self.page2: self.box("L")}
        first = self.receive(pair)
        self.assertEqual(self.history("2026-11-21T17:59:59Z"), [])
        self.assertEqual(self.history()[-1]["score"], [2, 0])
        self.assertEqual(len(self.history()[-1]["supporting_articles"]), 2)
        first_at = self.seen
        self.seen = store.iso(store.instant(self.seen) + timedelta(minutes=31))
        correction = self.receive(
            {self.page1: self.box(score="3–0"), self.page2: self.box("L")}
        )
        self.assertNotEqual(correction["sha256"], first["sha256"])
        self.assertEqual(self.history()[-1]["status"], "conflict")
        self.assertEqual(self.history(first_at)[-1]["score"], [2, 0])
        self.seen = store.iso(store.instant(self.seen) + timedelta(minutes=31))
        self.receive({})
        self.assertEqual(self.history()[-1]["status"], "withdrawn")
        store.blob_path(self.root, correction["sha256"]).write_bytes(b"damaged")
        self.assertEqual(self.history()[-2]["status"], "conflict")

    def test_competition_time_licence_and_ambiguous_identity(self):
        def decode(x, **kwargs):
            return wiki._decode(self.raw(x, **kwargs), self.titles, 2026)

        self.assertEqual(decode({}), {})  # FA Cup boxes are ignored
        self.assertEqual(
            decode({self.page1: self.box(time="15:00 BST")})[self.fixture]["status"],
            "unverified",
        )
        self.assertEqual(
            decode({self.page1: self.box(result="")})[self.fixture]["status"],
            "unfinished",
        )
        self.assertNotEqual(
            decode({self.page1: self.box()}, published="2026-11-20T08:00:00Z")[
                self.fixture
            ]["status"],
            "final",
        )
        with self.assertRaises(ValueError):
            decode({self.page1: self.box()}, licensed=False)
        next_match = self.box(
            home="Club 0", away="Club 2", round=12, score="", result=""
        )
        before = {self.page1: self.box() + next_match, self.page2: self.box("L")}
        self.assertEqual(decode(before)[self.fixture]["score"], [2, 0])
        duplicate_final = self.box(home="Club 0", away="Club 2", round=12)
        self.assertEqual(
            decode({self.page1: self.box() + duplicate_final})[self.fixture]["status"],
            "conflict",
        )

    def test_recent_unfinished_rechecks_marker_after_30_minutes(self):
        import test_result_feed

        match = test_result_feed.match(321, "Club 0", "Club 1", [2, 0])
        match["matchIsFinished"] = False
        self.seen = store.iso("2026-11-22T01:00:00Z")
        with (
            patch.object(result_feed, "now_utc", side_effect=lambda: self.seen),
            patch.object(
                result_feed,
                "_get",
                side_effect=[b'"abc"', json.dumps([match]).encode(), b'"abc"'],
            ) as get,
        ):
            result_feed.sync(self.root, 2026, round_id=1, recent=True)
            self.seen = store.iso(store.instant(self.seen) + timedelta(minutes=29))
            self.assertEqual(
                result_feed.sync(self.root, 2026, round_id=1, recent=True)[
                    "fetch_status"
                ],
                "local_cache",
            )
            self.seen = store.iso(store.instant(self.seen) + timedelta(minutes=1))
            self.assertEqual(
                result_feed.sync(self.root, 2026, round_id=1, recent=True)[
                    "fetch_status"
                ],
                "source_marker_unchanged",
            )
            self.assertEqual(get.call_count, 3)

    def test_next_round_uses_receipt_time_and_last_two_due_results(self):
        known = store.iso("2026-11-01T12:00:00Z")
        fixtures = []
        with closing(store.connect(self.root)) as db, db:
            for n, day in enumerate((5, 12, 28), start=1):
                home, away = self.teams[0], self.teams[n]
                fid = fixture_id("2026-27", home, away)
                kickoff = store.iso(f"2026-11-{day:02d}T15:00:00Z")
                fixtures.append(fid)
                store.register_fixture_db(
                    db,
                    dict(
                        fixture_id=fid,
                        season="2026-27",
                        round_id=n,
                        home=home,
                        away=away,
                        kickoff=kickoff,
                        known_at=known,
                        source="fixture-test",
                        locator=str(n),
                        precision="minute",
                    ),
                    recorded_at=known,
                )
            at = store.iso("2026-11-22T02:00:00Z")
            store.record_result_db(
                db,
                dict(
                    fixture_id=fixtures[1],
                    home_goals=2,
                    away_goals=0,
                    finalized_at=at,
                    published_at=at,
                    received_at=at,
                    source="fixture-test",
                    locator="native-test",
                ),
                recorded_at=at,
            )
        a = result_freshness.status(self.root, 2026, "2026-11-21T22:00:00Z")
        b = result_freshness.status(self.root, 2026, "2026-11-22T03:00:00Z")
        self.assertEqual(a["next_round_blocked_fixtures"], [fixtures[-1]])
        self.assertEqual(
            a["next_round_teams"][self.teams[0]]["fixture_ids"], fixtures[:2]
        )
        self.assertEqual(b["next_round_blocked_fixtures"], [])
        self.assertEqual(a["latest_round"]["received"], 0)
        self.assertEqual(b["latest_round"]["received"], 1)
        self.assertEqual(b["oldest_missing"]["fixture_id"], fixtures[0])
        self.assertEqual(
            b["next_round_teams"][self.teams[0]]["players"]["state"], "unavailable"
        )
