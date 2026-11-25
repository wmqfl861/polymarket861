import tempfile
import threading
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket861 import news, news_roster, store
from test_news_roster import Wiki


def registered_squad(
    root, club, season, *, person="Morgan Smith", shared_id=123, prefix=1000
):
    wiki = Wiki(store.instant(store.now_utc()))
    wiki.members = [person, *[f"Squad member {prefix + i}" for i in range(10)]]
    wiki.identities = {name: prefix + i for i, name in enumerate(wiki.members)}
    wiki.identities[person] = shared_id
    return news_roster.refresh(root, club, season, getter=wiki)


def replay_feed_tick(root, stamp, raw):
    from polymarket861 import news_sources
    from polymarket861.market_provider import Reply

    def poll(source, team, season, cursor):
        return news_sources.poll(source, team, season, cursor,
            getter=lambda url: Reply(raw, stamp, stamp, {"cache-control": "max-age=300"}, url))

    with patch.object(store, "now_utc", return_value=stamp):
        return news.sync(root, team="Forest FC", force=False, defaults=lambda *_: [],
            roster_refresh=lambda *_: {"status": "cached"}, poller=poll)


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
        registered_squad(self.root, "Forest FC", "2026-27")

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
                "player_id": "enwiki:123",
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

    def test_registry_withdrawal_cross_club_ambiguity_and_original_cutoff(self):
        self.advance()
        self.run_sync()
        first = store.iso(self.clock)
        initial = self.snap(first)
        self.assertEqual(len(initial["facts"]), 1)
        self.assertTrue(initial["coverage"]["home"]["observed"])
        self.advance()
        registered_squad(self.root, "Villa FC", "2026-27", prefix=2000)
        ambiguous = self.snap()
        self.assertEqual(ambiguous["facts"], [])
        self.assertIn(
            "enwiki:123",
            ambiguous["coverage"]["home"]["roster"]["ambiguous_player_ids"],
        )
        self.assertFalse(ambiguous["coverage"]["home"]["observed"])
        self.assertEqual(self.snap(first), initial)
        # A genuine, licensed missing-template response withdraws club membership.
        import json
        from urllib.parse import parse_qs, urlsplit

        def removed(url):
            title = parse_qs(urlsplit(url).query)["titles"][0]
            return json.dumps(
                {
                    "query": {
                        "rightsinfo": {"url": news_roster.wiki.LICENSE},
                        "pages": [{"ns": 10, "title": title, "missing": True}],
                    }
                }
            ).encode()

        self.advance(7 * 3600)
        news_roster.refresh(self.root, "Forest FC", "2026-27", getter=removed)
        withheld = self.snap()
        self.assertEqual(withheld["facts"], [])
        self.assertEqual(withheld["coverage"]["home"]["roster"]["state"], "withdrawn")
        self.assertFalse(withheld["coverage"]["home"]["observed"])
        self.assertEqual(self.snap(first), initial)

    def test_expired_or_absent_authoritative_membership_never_falls_back_to_injected_players(
        self,
    ):
        import sqlite3

        with news._db(self.root, write=True) as db:
            db.execute("DELETE FROM news_roster_receipts")
        self.run_sync()  # Technical test adapter supplies a player but no source receipt.
        at = store.iso(self.clock)
        self.assertEqual(self.snap(at)["facts"], [])
        self.assertEqual(
            self.snap(at)["coverage"]["home"]["roster"]["state"], "never_observed"
        )
        self.assertFalse(self.snap(at)["coverage"]["home"]["observed"])
        self.assertEqual(news.status(self.root)["archive_counts"]["news_claims"], 1)
        self.advance()
        with news._db(self.root, write=True) as db:
            db.execute("DELETE FROM news_roster_checks")
        # Make the most recent authoritative observation genuinely seven days old,
        # with a new fixture still up ahead when it expires.
        old = self.clock
        self.clock -= timedelta(days=7, seconds=2)
        registered_squad(self.root, "Forest FC", "2026-27")
        self.clock = old
        db_path = self.root / "runtime.sqlite3"
        with closing(sqlite3.connect(db_path)) as check:
            before = check.execute("PRAGMA data_version").fetchone()[0]
            snap = self.snap()
            self.assertFalse(snap["facts"])
            self.assertEqual(snap["coverage"]["home"]["roster"]["state"], "expired")
            self.assertEqual(check.execute("PRAGMA data_version").fetchone()[0], before)

    def test_rss_backlog_prevents_false_zero_until_last_page_is_drained(self):
        import hashlib
        import json
        import os
        import subprocess
        import sys
        from xml.etree import ElementTree as ET
        import email.utils

        start = self.advance()
        cfg = news.add_feed(
            self.root,
            {
                "kind": "feed",
                "id": "test-rss",
                "url": "https://forest.example/rss",
                "team": "Forest FC",
                "license": "CC BY 4.0",
                "license_url": "https://forest.example/reuse",
                "authority": "official",
                "attribution": "test publisher",
                "free": True,
                "rights": ["fetch", "store", "extract", "model"],
            },
        )
        self.configs = []
        root = ET.Element("rss", version="2.0")
        channel = ET.SubElement(root, "channel")
        for index in range(101):
            item = ET.SubElement(channel, "item")
            for name, value in {
                "guid": str(index),
                "title": "Availability",
                "link": f"https://forest.example/notes/{index}",
                "description": self.body if index == 0 else "nothing new",
                "pubDate": email.utils.format_datetime(
                    store.instant(start) - timedelta(minutes=400 - index)
                ),
            }.items():
                ET.SubElement(item, name).text = value
        raw = ET.tostring(root)
        original_hash = hashlib.sha256(raw).hexdigest()
        first = replay_feed_tick(self.root, start, raw)
        initial = first["streams"][0]
        self.assertEqual((initial["state"], initial["provider_pending"], initial["inbox_pending"],
                          initial["fetch_complete"]), ("pending", 0, 1, False), initial)
        self.assertEqual(first["pending_entries"], 1)
        self.assertEqual(news.status(self.root)["pending_entries"], 1)
        self.assertEqual(self.snap(start)["facts"], [])
        self.assertFalse(self.snap(start)["coverage"]["home"]["observed"])
        self.advance(30)
        self.assertEqual(replay_feed_tick(self.root, store.iso(self.clock), raw)["attempted"], 0)
        channel.remove(channel.findall("item")[0])  # The publisher evicted the decisive oldest item.
        stamp = self.advance(600)
        rotated = ET.tostring(root)
        # A new interpreter sees only this rotated HTTP page plus the durable runtime.
        script = ("import json,sys; from test_news_ingest import replay_feed_tick; "
                  "print(json.dumps(replay_feed_tick(sys.argv[1],sys.argv[2],sys.stdin.buffer.read())))")
        cmd = subprocess.run([sys.executable, "-c", script, str(self.root), stamp],
                              input=rotated, capture_output=True, check=True,
                              env=os.environ | {"PYTHONPATH": os.pathsep.join(sys.path)})
        last = json.loads(cmd.stdout)
        self.assertEqual((last["state"], last["pending_entries"]), ("ok", 0), last)
        self.assertEqual(last["streams"][0]["response_sha256"], hashlib.sha256(rotated).hexdigest())
        current = self.snap()
        fact = current["facts"][0]
        self.assertEqual(fact["external_id"], "0")
        self.assertEqual(fact["received_at"], start)
        self.assertEqual(fact["known_at"], stamp)
        self.assertEqual(fact["response_sha256"], original_hash)
        self.assertTrue(current["coverage"]["home"]["observed"])
        self.assertFalse(self.snap(start)["coverage"]["home"]["observed"])
        self.assertEqual(news.status(self.root)["pending_entries"], 0)
        self.assertEqual(
            news.article(self.root, current["facts"][0]["original_sha256"])["source"],
            cfg["id"],
        )

    def test_newer_wire_attributing_same_official_origin_keeps_original_witness(self):
        self.configs.append(self.configs[0] | {"id": "paper", "authority": "secondary"})
        stamp = self.advance()
        wires = []

        def feed(cfg, *_):
            return {
                "articles": [self.story(stamp=stamp)]
                if cfg["authority"] == "official"
                else wires,
                "cursor": {},
            }

        self.run_sync(poller=feed)
        later = self.advance()
        wires.append(self.story(stamp=later, external_id="syndicated"))
        self.run_sync(poller=feed)
        facts = self.snap()["facts"]
        self.assertEqual({f["status"] for f in facts}, {"confirmed", "reported"})
        self.assertFalse(any(f["conflict"] for f in facts))

    def test_chronic_source_errors_trigger_one_private_research_job_then_clear(self):
        from polymarket861 import maintenance, research_workspace

        diagnostic = "https://private-feed.test/hidden-player?private_score=42"

        def down(*args):
            raise OSError(diagnostic)

        def source_report():
            return next(
                (
                    r
                    for r in store.list_reports(self.root, "source")
                    if r["key"] == news.RESEARCH_SOURCE
                ),
                None,
            )

        def queued():
            return [
                r
                for r in maintenance.tick(self.root)["new_jobs"]
                if r.startswith("research-source-")
            ]

        self.run_sync(poller=down)
        self.assertIsNone(source_report())
        self.assertFalse(queued())
        self.advance()
        self.run_sync(poller=down)
        started = source_report()
        self.assertEqual(
            started["payload"]["health"],
            {
                "publisher_feed.collector_error": {
                    "status": "error",
                    "affected_streams": "1",
                }
            },
        )
        jobs = queued()
        self.assertEqual(len(jobs), 1)
        self.advance()
        self.run_sync(poller=down)
        self.assertEqual(source_report(), started)
        self.assertFalse(queued())
        with news._db(self.root) as db:
            ref = db.execute(
                "SELECT ref FROM research_maintenance_subjects WHERE topic='source' AND subject=?",
                (news.RESEARCH_SOURCE,),
            ).fetchone()[0]
            profile = research_workspace._collection_health(
                db, "source", {"source_ref": ref}
            )
        self.assertEqual(
            profile, {"news_collection_health": started["payload"]["health"]}
        )
        self.assertNotIn(diagnostic, str(profile))
        self.assertNotIn("Forest", str(profile))
        self.assertNotIn("club-feed", str(profile))
        self.advance()
        self.run_sync()
        self.assertEqual(source_report()["payload"]["health"], {})
        self.assertFalse(queued())

    def test_only_stalled_repeated_publisher_backlog_creates_research_signal(self):
        backlog = 4

        def delayed(*args):
            return {"articles": [], "cursor": {}, "pending_entries": backlog}

        def signal():
            return next(
                (
                    r["payload"]["health"]
                    for r in store.list_reports(self.root, "source")
                    if r["key"] == news.RESEARCH_SOURCE
                ),
                {},
            )

        self.run_sync(poller=delayed)
        self.assertFalse(signal())
        self.advance()
        backlog = 2
        self.run_sync(poller=delayed)
        self.assertFalse(signal())
        self.advance()
        self.run_sync(poller=delayed)
        self.assertEqual(
            signal(),
            {
                "publisher_feed.provider_backlog": {
                    "status": "late",
                    "affected_streams": "1",
                }
            },
        )
        self.advance()
        backlog = 0
        self.run_sync(poller=delayed)
        self.assertFalse(signal())

    def test_native_atom_contradiction_withdraws_event_then_newer_revision_recovers(self):
        import hashlib
        from test_news_events import EventNewsTests, FIRST, HOME, YEAR
        from test_paired_proof import at
        from polymarket861 import news_events, news_sources
        from polymarket861.market_provider import Reply

        fixture = EventNewsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.observe()
        fixture.squad(HOME, '2023-08-10T08:00Z')
        with at('2023-08-11T08:00Z'):
            news.add_feed(fixture.root, dict(id='club-bulletins', kind='feed', team=HOME,
                url='https://club.example/atom', license='CC BY 4.0', license_url='https://club.example/terms',
                authority='official', attribution='Club communications',
                free=True, rights=['fetch', 'store', 'extract', 'model']))

        def tick(stamp, updated, text):
            raw = (f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>player-news</id>'
                   f'<link href="https://club.example/squad"/><title>Squad news</title>'
                   f'<published>{FIRST}</published><updated>{updated}</updated>'
                   f'<content>{text}</content></entry></feed>').encode()
            def feed(cfg, team, season, cursor):
                return news_sources.poll(cfg, team, season, cursor, getter=lambda url: Reply(raw, stamp, stamp, {}, url))
            with at(stamp):
                out = news.sync(fixture.root, team=HOME, season=YEAR, force=True,
                    defaults=lambda *_: [], roster_refresh=lambda *_: {'status': 'cached'}, poller=feed)
                snap = news.snapshot(fixture.root, fixture.fid, stamp)
                event = news_events.refresh(fixture.root, history_root=fixture.history)
            return out, snap, event, hashlib.sha256(raw).hexdigest()

        opened, before, event, _ = tick(FIRST, FIRST, 'Morgan Smith is ruled out.')
        self.assertEqual(opened['succeeded'], 1, opened)
        self.assertEqual([f['availability'] for f in before['facts']], ['out'], before)
        self.assertEqual(event['published'], 1, event)
        held, now, event, digest = tick('2023-08-12T08:10Z', FIRST, 'Morgan Smith is available.')
        self.assertGreaterEqual(held['streams'][0]['revision_conflicts'], 1, held)
        self.assertEqual(now['facts'], [])
        self.assertFalse(now['coverage']['home']['observed'])
        earlier = news.snapshot(fixture.root, fixture.fid, FIRST)
        self.assertEqual(earlier, before)
        self.assertEqual(event['published'], 1, event)
        change = news_events.list_updates(fixture.root, fixture.fid)[0]
        self.assertEqual(change['trigger'], 'news_removed')
        self.assertEqual(change['news_pair']['with_news'], change['news_pair']['no_news'])
        conflicts = news.article(fixture.root, before['facts'][0]['original_sha256'])['revision_conflicts']
        self.assertEqual({x['response_sha256'] for x in conflicts}, {digest})
        self.assertEqual({x['observed_at'] for x in conflicts}, {store.iso('2023-08-12T08:10Z')})
        corrected = '2023-08-12T08:22Z'
        ok, current, event, _ = tick(corrected, corrected, 'Morgan Smith is ruled out.')
        self.assertEqual(ok['succeeded'], 1, ok)
        self.assertTrue(current['coverage']['home']['observed'])
        self.assertEqual(current['facts'][0]['availability'], 'out')
        self.assertEqual(event['published'], 1, event)
        self.assertEqual(news.snapshot(fixture.root, fixture.fid, FIRST), before)
