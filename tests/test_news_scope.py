"""Scope and polarity must survive a real licensed-feed-to-prediction path."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from polymarket861 import news, news_events, news_roster, news_sources, pipeline, store
from polymarket861.market_provider import Reply
from test_news_matchday import AWAY, FID, HOME, KICKOFF, add_club_feed, roster_transport, seed_fixture
from test_paired_proof import at


class FixtureLanguageScopeTests(unittest.TestCase):
    def test_explicit_opponent_scope_in_title_or_quote(self):
        fixture = {"home": HOME, "away": AWAY, "kickoff": KICKOFF}
        for phrase in ("trip to Aston Villa", "visit of Aston Villa", "home game with Aston Villa",
                       "match against Chelsea before a visit to Aston Villa", "travelling to Celtic",
                       "match with aston villa"):
            with self.subTest(phrase=phrase):
                self.assertFalse(news.fixture_relevant("Morgan Smith will miss the " + phrase, "", fixture, HOME))
                self.assertFalse(news.fixture_relevant("Morgan Smith will miss the game.", phrase, fixture, HOME))
        for phrase in ("trip to Chelsea", "visit of Chelsea", "home game with Chelsea",
                       "game against Chelsea FC", "away to Chelsea", "race against the clock"):
            self.assertTrue(news.fixture_relevant("Morgan Smith will miss the " + phrase, "", fixture, HOME))

    def test_different_opponent_and_past_absence_do_not_reduce_current_probabilities(self):
        with tempfile.TemporaryDirectory() as temp:
            root, history = Path(temp) / "app", Path(temp) / "football"
            with at("2026-08-01T10:00Z"):
                seed_fixture(root, history)
                source = add_club_feed(root, HOME)
            roster = roster_transport(HOME)
            stories = [
                "Morgan Smith will miss the trip to Aston Villa.",
                "Morgan Smith will miss the visit of Aston Villa.",
                "Morgan Smith will miss the home game with Aston Villa.",
                "Morgan Smith was ruled out for last weekend but will be back in the squad.",
            ]

            def transport(url):
                self.assertEqual(url, source["url"])
                doc = ElementTree.Element("rss", version="2.0")
                channel = ElementTree.SubElement(doc, "channel")
                for i, story in enumerate(stories):
                    item = ElementTree.SubElement(channel, "item")
                    for key, value in {
                        "guid": "scope-" + str(i), "title": "Team news", "description": story,
                        "link": url.removesuffix("rss") + str(i),
                        "pubDate": "2026-08-12T12:35:00Z" if i == 4 else "2026-08-12T12:00:00Z",
                    }.items():
                        ElementTree.SubElement(item, key).text = value
                now = store.now_utc()
                return Reply(ElementTree.tostring(doc), now, now, {}, url)

            def collect():
                result = news.sync(root, team=HOME, defaults=lambda *_: [], force=True,
                                   roster_refresh=lambda r, t, y: news_roster.refresh(r, t, y, getter=roster))
                self.assertEqual(result["failures"], 0, result)
                return news_events.refresh(root, history_root=history)

            with patch.object(news_sources, "_get", side_effect=transport):
                with at("2026-08-12T12:05Z"):
                    collect()
                    facts = news.snapshot(root, FID, store.now_utc())["facts"]
                    self.assertEqual([(f["event"], f["availability"]) for f in facts], [("return", "available")])
                    initial = news_events.list_updates(root, FID)[0]
                    self.assertFalse(initial["news_pair"]["changed"])
                with at("2026-08-12T12:40Z"):
                    stories.append("Morgan Smith is ruled out for the home game with Chelsea.")
                    self.assertEqual(collect()["published"], 1)
                    latest = news_events.list_updates(root, FID)[0]
                    self.assertEqual(latest["previous_digest"], initial["digest"])
                    self.assertTrue(latest["news_pair"]["changed"])
                with at("2026-08-12T12:59Z"):
                    seal = pipeline.make_forecast(root, FID, history, horizon="T-60m")
                    self.assertEqual([f["quote"] for f in seal["news_pair"]["applied"]], [stories[-1]])
                    self.assertLess(seal["news_pair"]["with_news"]["probabilities"]["home_win"],
                                    seal["news_pair"]["no_news"]["probabilities"]["home_win"])
