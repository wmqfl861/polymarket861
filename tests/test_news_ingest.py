import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket861 import news, store


class NewsIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"
        self.clock = store.instant("2026-11-25T12:00:00Z")
        clock = patch.object(
            store, "now_utc", side_effect=lambda: store.iso(self.clock)
        )
        clock.start()
        self.addCleanup(clock.stop)
        self.fixture("previous", "2026-11-24T16:00:00Z", "United FC", "Forest FC")
        self.fixture("next", "2026-11-28T16:00:00Z", "Forest FC", "Chelsea FC")
        self.fixture("later", "2026-12-03T16:00:00Z", "Forest FC", "Villa FC")
        self.configs = [
            {
                "id": "club-feed",
                "kind": "feed",
                "license": "CC BY 4.0",
                "origin": "club.example",
                "authority": "official",
                "attribution": "Club media",
            }
        ]
        self.body = "Morgan Smith is ruled out."
        self.rows = [self.story()]
        self.poll_cursors = []
        self.rostered = True

    def fixture(self, ident, kickoff, home, away):
        return store.register_fixture(
            self.root,
            {
                "fixture_id": ident,
                "season": "2026-27",
                "round_id": 12,
                "home": home,
                "away": away,
                "known_at": store.iso(self.clock),
                "kickoff": kickoff,
                "source": "club-feed",
                "locator": "test:" + ident,
            },
        )

    def advance(self, sec=60):
        self.clock += timedelta(seconds=sec)
        return store.iso(self.clock)

    def story(self, *, text=None, revision="a", stamp="2026-11-25T10:00:00Z", **extra):
        return {
            "external_id": "story",
            "revision": revision,
            "published_at": stamp,
            "url": "https://club.example/news/morgan",
            "title": "Team update",
            "text": self.body if text is None else text,
            "attribution": "The club",
            **extra,
        }

    def poll(self, config, team, season, cursor):
        self.poll_cursors.append(cursor)
        return {"articles": self.rows, "cursor": {"version": len(self.poll_cursors)}}

    def players(self, _root, team, _season, _at):
        if not self.rostered or team != "Forest FC":
            return []
        return [
            {
                "player_id": "Q123",
                "name": "Morgan Smith",
                "aliases": ["M. Smith"],
                "position": "forward",
                "source_url": "https://club.example/squad",
            }
        ]

    def extract(self, text, players, *, authority, title, team):
        if "nothing" in text:
            return []
        active = "returns" in text
        return [
            {
                "player_id": players[0]["player_id"],
                "event": "return" if active else "injury",
                "availability": "available" if active else "out",
                "status": "rumour" if "rumour" in text else "confirmed",
                "quote": text,
                "start": 0,
                "end": len(text),
                "confidence": 0.9,
                "model_version": "fixture-extraction",
            }
        ]

    def run_sync(self, **options):
        args = dict(
            team="Forest",
            force=True,
            defaults=lambda t, s: self.configs,
            poller=self.poll,
            extractor=self.extract,
            roster_reader=self.players,
            roster_refresh=lambda *_: {"status": "cached"},
        )
        return news.sync(self.root, **(args | options))

    def snap(self, cutoff=None):
        return news.snapshot(self.root, "next", cutoff or store.iso(self.clock))

    def test_immutable_receipt_revision_quarantine_and_withdrawal(self):
        before = store.iso(self.clock)
        self.advance()
        zero = self.snap(before)
        self.assertEqual(zero["facts"], [])
        self.assertFalse(zero["coverage"]["both_teams_observed"])
        self.assertEqual(self.run_sync()["claims_added"], 1)
        first = store.iso(self.clock)
        f = self.snap(first)["facts"][0]
        self.assertEqual(f["position"], "forward")
        self.assertEqual(
            f["quote"], news.article(self.root, f["original_sha256"])["text"]
        )
        self.assertTrue(self.snap(first)["coverage"]["home"]["observed"])
        self.assertFalse(self.snap(first)["coverage"]["away"]["observed"])
        self.assertFalse(self.snap(before)["facts"])
        self.assertEqual(self.run_sync()["articles_added"], 0)
        conflict = self.advance()
        self.rows = [self.story(text="Morgan Smith returns.")]
        self.assertEqual(self.run_sync()["failures"], 1)
        self.assertFalse(self.snap(conflict)["facts"])
        self.assertEqual(self.snap(first)["facts"][0]["id"], f["id"])
        self.advance()
        self.rows = [
            self.story(
                text="Morgan Smith returns.",
                revision="b",
                updated_at=store.iso(self.clock),
            )
        ]
        self.assertEqual(self.run_sync()["failures"], 0)
        fixed = store.iso(self.clock)
        self.assertEqual(self.snap(fixed)["facts"][0]["availability"], "available")
        # A previously known old revision cannot revive the original absence.
        self.advance()
        self.rows = [self.story()]
        self.assertEqual(self.run_sync()["articles_added"], 0)
        self.assertEqual(self.snap()["facts"][0]["availability"], "available")
        self.advance()
        self.rows = [
            self.story(
                text="", revision="c", retracted=True, updated_at=store.iso(self.clock)
            )
        ]
        self.run_sync()
        self.assertEqual(self.snap()["facts"], [])
        self.assertEqual(self.snap(fixed)["facts"][0]["availability"], "available")

    def test_no_roster_leaves_raw_for_retry_and_cutoff_is_after_ai(self):
        received = self.advance()
        self.rostered = False
        first = self.run_sync()
        self.assertEqual(first["streams"][0]["state"], "no_roster")
        self.assertEqual(first["articles_added"], 1)
        self.assertFalse(self.snap(received)["coverage"]["home"]["observed"])
        self.assertEqual(self.poll_cursors, [None])
        self.advance()
        self.rows = []
        self.rostered = True

        def delayed(*args, **kwargs):
            result = self.extract(*args, **kwargs)
            self.advance(20)
            return result

        after_receipt = store.iso(self.clock)
        self.assertEqual(self.run_sync(extractor=delayed)["claims_added"], 1)
        fact = self.snap()["facts"][0]
        self.assertEqual(fact["received_at"], received)
        self.assertEqual(fact["known_at"], store.iso(self.clock))
        self.assertEqual(self.snap(after_receipt)["facts"], [])
        self.assertEqual(self.poll_cursors, [None, {"version": 1}])
        report = store.list_reports(self.root, "news-coverage")[0]["payload"]
        self.assertEqual(report["last_states"], {"ok": 1})
        self.assertNotIn(self.body, store.canonical(report))

    def test_wrong_fixture_cup_dates_recap_and_old_bulletins(self):
        self.advance()
        variants = [
            self.story(
                external_id="before-previous-game", stamp="2026-11-23T12:00:00Z"
            ),
            self.story(
                external_id="cup", text="Morgan Smith is ruled out of the FA Cup tie."
            ),
            self.story(
                external_id="other", text="Morgan Smith is ruled out against Villa."
            ),
            self.story(
                external_id="weekday",
                text="Morgan Smith is ruled out of Tuesday's match.",
            ),
            self.story(
                external_id="dated",
                text="Morgan Smith is ruled out of the game on 5 December.",
            ),
            self.story(
                external_id="recap",
                title="Match report",
                text="Morgan Smith missed the game.",
            ),
            self.story(external_id="future", stamp="2026-11-26T12:00:00Z"),
            self.story(
                external_id="accurate",
                text="Morgan Smith is ruled out of Saturday's match.",
            ),
        ]
        self.rows = variants
        self.assertEqual(self.run_sync()["failures"], 1)
        facts = self.snap()["facts"]
        self.assertEqual([f["external_id"] for f in facts], ["accurate"])
        self.assertEqual(news.as_of(self.root, "later", store.iso(self.clock)), [])
        self.advance()
        self.fixture("next", "2026-11-29T16:00:00Z", "Forest FC", "Chelsea FC")
        self.assertFalse(self.snap()["facts"])
        self.assertFalse(self.snap()["coverage"]["home"]["observed"])

    def test_independent_rumours_and_covered_zero_are_distinct(self):
        self.configs.append(
            self.configs[0]
            | {"id": "paper", "authority": "secondary", "origin": "wire.example"}
        )
        self.advance()

        def selected(cfg, *args):
            rows = (
                [self.story(text="Morgan Smith rumour of an absence.")]
                if cfg["id"] == "paper"
                else []
            )
            return {"articles": rows, "cursor": {}}

        self.assertEqual(self.run_sync(poller=selected)["succeeded"], 2)
        snapshot = self.snap()
        self.assertEqual(snapshot["facts"][0]["status"], "rumour")
        self.assertTrue(snapshot["coverage"]["home"]["observed"])
        until = self.advance(7 * 3600)
        statuses = {
            s["status"] for s in self.snap(until)["coverage"]["home"]["sources"]
        }
        self.assertEqual(statuses, {"stale"})
        self.assertFalse(self.snap(until)["coverage"]["home"]["observed"])

    def test_bad_credits_and_spans_isolated_by_stream(self):
        self.configs.append(
            self.configs[0]
            | {"id": "broken", "authority": "secondary", "origin": "wire.example"}
        )
        self.advance()

        def selected(cfg, *args):
            if cfg["id"] == "broken":
                raise OSError("fixture source unavailable")
            return self.poll(cfg, *args)

        self.assertEqual(self.run_sync(poller=selected)["state"], "partial")
        self.assertEqual(len(self.snap()["facts"]), 1)
        self.advance()
        self.rows = [self.story(revision="a2", updated_at=store.iso(self.clock))]

        def invent(*args, **kwargs):
            row = self.extract(*args, **kwargs)[0]
            return [row | {"quote": "unsupported words"}]

        self.assertEqual(self.run_sync(extractor=invent)["succeeded"], 0)
        self.assertEqual(self.snap()["facts"], [])

    def test_empty_snapshot_is_read_only_and_force_respects_leases_and_remote_cache(
        self,
    ):
        missing = self.root / "not-created"
        self.assertFalse(
            news.snapshot(missing, "missing", store.iso(self.clock))["facts"]
        )
        self.assertFalse(missing.exists())
        entered = threading.Event()
        release = threading.Event()
        self.advance()

        def slow(*args):
            entered.set()
            release.wait(timeout=4)
            return self.poll(*args) | {
                "next_poll_after": news._plus(self.clock, timedelta(minutes=5))
            }

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            active = pool.submit(self.run_sync, poller=slow)
            self.assertTrue(entered.wait(timeout=4))
            try:
                self.assertEqual(self.run_sync()["attempted"], 0)
                with news._db(self.root, write=True):
                    pass
            finally:
                release.set()
            self.assertEqual(active.result()["succeeded"], 1)
        self.assertEqual(self.run_sync(force=True)["attempted"], 0)
        self.advance(301)
        self.assertEqual(self.run_sync(force=True)["attempted"], 1)

    def test_syndication_conflict_and_official_recovery(self):
        self.configs.append(
            self.configs[0]
            | {"id": "paper", "authority": "secondary", "origin": "wire.example"}
        )
        first_at = self.advance()

        def selected(cfg, *args):
            stamp = first_at if cfg["id"] == "paper" else store.iso(self.clock)
            text = (
                self.body
                if stamp == first_at or not self.rows
                else "Morgan Smith returns."
            )
            return {
                "articles": [self.story(text=text, revision=stamp, stamp=stamp)],
                "cursor": {},
            }

        self.run_sync(poller=selected)
        self.assertEqual(
            {f["status"] for f in self.snap(first_at)["facts"]},
            {"reported", "confirmed"},
        )
        self.advance()
        self.run_sync(poller=selected)
        active = self.snap()["facts"]
        self.assertEqual([x["availability"] for x in active], ["available"])
        self.assertFalse(active[0]["conflict"])
        self.assertEqual(news.status(self.root)["archive_counts"]["news_articles"], 3)

    def test_source_failure_fairness(self):
        self.configs.append(self.configs[0] | {"id": "z-feed"})
        self.advance()
        first = self.run_sync(limit=1)
        self.advance()
        second = self.run_sync(limit=1)
        self.assertNotEqual(
            first["streams"][0]["source"], second["streams"][0]["source"]
        )
        self.assertEqual(self.run_sync(force=False)["state"], "idle")

    def test_model_switch_and_zero_extraction_are_point_in_time_even_with_long_source_cache(
        self,
    ):
        from polymarket861 import news_ai, news_local_ai

        self.advance()

        def cached(*args):
            return self.poll(*args) | {
                "next_poll_after": news._plus(self.clock, timedelta(days=1))
            }

        self.run_sync(poller=cached)
        first = store.iso(self.clock)
        original = self.snap(first)["facts"][0]
        with (
            patch.object(
                news_local_ai,
                "status",
                return_value={
                    "enabled": True,
                    "state": "configured",
                    "model_id": "local-test",
                    "endpoint_sha256": "test",
                },
            ),
            patch.object(news_local_ai, "extract", return_value=[]) as backend,
            patch.object(
                news_local_ai,
                "last_decision",
                return_value={
                    "backend": "local_llm_plus_nb",
                    "model_version": "local-test",
                    "local_status": "ok",
                    "local_requested": True,
                    "local_accepted": 0,
                    "local_added": 0,
                    "policy_version": news_local_ai.PROTOCOL,
                },
            ),
        ):
            self.advance()
            # Switch policy while HTTP is inside a 24-hour TTL. The previously
            # archived story is re-extracted, without a fake new source receipt.
            old_polls = len(self.poll_cursors)
            s = self.run_sync(poller=cached, extractor=None, force=False)["streams"][0]
            self.assertFalse(s["polled"])
            self.assertEqual(s["zero_claim_analyses"], 1)
            self.assertEqual(s["source_observed_at"], first)
            self.assertEqual(len(self.poll_cursors), old_polls)
            backend.assert_called_once()
            self.assertEqual(self.snap()["facts"], [])
            self.assertEqual(self.snap(first)["facts"][0]["id"], original["id"])
            receipt = news.article(self.root, original["original_sha256"])
            self.assertEqual(receipt["analyses"][-1]["claim_count"], 0)
            self.assertEqual(
                receipt["analyses"][-1]["metadata"]["model_version"], "local-test"
            )
            self.assertNotEqual(news_ai.MODEL_VERSION, "local-test")
        self.advance(6 * 3600 + 1)
        self.assertFalse(self.snap()["coverage"]["home"]["observed"])

    def test_roster_retry_inside_remote_cache_and_sourced_outlet_label(self):
        self.advance()
        self.rostered = False
        self.rows = [
            self.story(origin="Daily Test Sports", source_fields={"status": "doubtful"})
        ]

        def cached(*args):
            return self.poll(*args) | {
                "next_poll_after": news._plus(self.clock, timedelta(days=1)),
                "response_sha256": "a" * 64,
                "request_url": "https://news.example/articles",
                "requested_at": store.iso(self.clock),
                "received_at": store.iso(self.clock),
            }

        initial = self.run_sync(poller=cached)
        received = store.iso(self.clock)
        self.assertEqual(initial["streams"][0]["state"], "no_roster")
        self.advance(300)
        self.rostered = True
        second = self.run_sync(poller=cached, force=False)
        self.assertEqual(second["streams"][0]["state"], "ok")
        self.assertFalse(second["streams"][0]["polled"])
        self.assertEqual(len(self.poll_cursors), 1)
        fact = self.snap()["facts"][0]
        self.assertEqual(fact["received_at"], received)
        self.assertEqual(fact["origin"], "daily test sports")
        self.assertEqual(fact["status"], "reported")
        original = news.article(self.root, fact["original_sha256"])
        self.assertEqual(original["source_fields"], {"status": "doubtful"})
        self.assertEqual(
            original["collection_receipts"][0]["response_sha256"], "a" * 64
        )

    def test_unexamined_wiki_baseline_and_transfer_only_do_not_prove_coverage(self):
        self.advance()
        self.configs = [
            dict(self.configs[0], id=s)
            for s in ("touchline-rumours", "wikipedia-season-news")
        ]

        def poll(cfg, *args):
            return {
                "articles": [],
                "cursor": {},
                "baseline_only": cfg["id"] == "wikipedia-season-news",
            }

        report = self.run_sync(poller=poll)
        self.assertEqual(report["succeeded"], 2)
        side = self.snap()["coverage"]["home"]
        self.assertFalse(side["observed"])
        self.assertTrue(all(s["status"] == "ok" for s in side["sources"]))

    def test_configured_feed_deactivation_excludes_current_facts_only(self):
        self.advance()
        cfg = {
            "id": "official-bulletins",
            "kind": "feed",
            "url": "https://www.chelsea.example/news/rss",
            "team": "Forest FC",
            "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "attribution": "Publisher",
            "authority": "secondary",
            "free": True,
            "rights": ["fetch", "store", "extract", "model"],
        }
        news.add_feed(self.root, cfg)
        self.configs = []
        self.run_sync()
        first = store.iso(self.clock)
        self.assertEqual(self.snap(first)["facts"][0]["source"], "official-bulletins")
        self.advance()
        news.disable_feed(self.root, cfg["id"])
        self.assertEqual(self.snap()["facts"], [])
        self.assertTrue(self.snap(first)["facts"])
        self.assertEqual(self.run_sync()["attempted"], 0)

    def test_substantive_correction_beats_intervening_wire_but_cosmetic_edit_does_not(
        self,
    ):
        self.configs.append(
            self.configs[0]
            | {"id": "wire", "authority": "secondary", "origin": "wire.example"}
        )
        release = self.advance()

        def poll(config, *_):
            if config["id"] == "wire":
                return {"articles": list(wire), "cursor": {}}
            return {"articles": list(club), "cursor": {}}

        club = [self.story(stamp=release)]
        wire = []
        self.run_sync(poller=poll)
        middle = self.advance()
        wire.append(self.story(text="Morgan Smith returns.", stamp=middle))
        self.run_sync(poller=poll)
        self.assertTrue(all(f["conflict"] for f in self.snap()["facts"]))
        cosmetic = self.advance()
        club[:] = [
            self.story(
                text=self.body + " An unchanged team update.",
                stamp=release,
                revision="b",
                updated_at=cosmetic,
            )
        ]
        self.run_sync(poller=poll)
        self.assertTrue(all(f["conflict"] for f in self.snap()["facts"]))
        self.assertEqual(
            next(f for f in self.snap()["facts"] if f["availability"] == "out")[
                "effective_at"
            ],
            release,
        )
        corrected = self.advance()
        club[:] = [
            self.story(
                text="Morgan Smith returns.",
                stamp=release,
                revision="c",
                updated_at=corrected,
            )
        ]
        self.run_sync(poller=poll)
        final = self.snap()["facts"]
        self.assertEqual(
            [(f["availability"], f["effective_at"], f["conflict"]) for f in final],
            [("available", corrected, False)],
        )
