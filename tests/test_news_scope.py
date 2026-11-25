"""Scope and polarity must survive a real licensed-feed-to-prediction path."""

import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from polymarket861 import news, news_ai, news_events, news_roster, news_sources, pipeline, store
from polymarket861.market_provider import Reply
from test_news_matchday import AWAY, FID, HOME, KICKOFF, add_club_feed, roster_transport, seed_fixture
from test_paired_proof import at


class FixtureLanguageScopeTests(unittest.TestCase):
    def test_distinguishes_injury_scene_from_target_of_absence(self):
        roster = [{"player_id": "m", "name": "Morgan Smith"}]
        fixture = {"home": HOME, "away": AWAY, "kickoff": KICKOFF}
        for text in (
            "Morgan Smith will miss the match against Chelsea after injuring his ankle in the FA Cup against Villa.",
            "After suffering an ankle injury against Villa in the League Cup, Morgan Smith will miss the game against Chelsea.",
            "Morgan Smith suffered a knee injury on international duty and will miss the Premier League fixture against Chelsea.",
        ):
            with self.subTest(text=text):
                records = news_ai.extract(text, roster, authority="official", team=HOME)
                self.assertEqual([(r["event"], r["availability"]) for r in records], [("injury", "out")])
                self.assertTrue(news.fixture_relevant(records[0]["quote"], "Team news", fixture, HOME))
        league = "Morgan Smith will miss the Premier League match against Chelsea."
        self.assertTrue(news.fixture_relevant(league, "Knee injury in League Cup against Villa", fixture, HOME))
        for text in (
            "Morgan Smith will miss the FA Cup tie against Chelsea after suffering an injury in the Premier League.",
            "Morgan Smith will miss the game against Villa after a League Cup injury against Chelsea.",
            "Morgan Smith suffered a knee injury in a friendly against Chelsea.",
            "Morgan Smith will miss England's international fixture after his injury.",
        ):
            self.assertFalse(news.fixture_relevant(text, "", fixture, HOME), text)
        self.assertFalse(news.fixture_relevant("He will miss the game.", "", fixture, HOME,
                                               antecedent="Morgan Smith suffered a knee injury in the FA Cup against Villa."))

    def test_pronouns_have_a_unique_nearby_subject_and_verbatim_evidence(self):
        roster = [{"player_id": "m", "name": "Morgan Smith"}, {"player_id": "a", "name": "Alex Jones"}]
        named = "Morgan Smith suffered a knee injury in a friendly against Villa."
        follow = "It was later confirmed that he had suffered a ligament injury and would miss the upcoming season."
        doc = named + " The manager described the injury as a setback. " + follow
        rows = news_ai.extract(doc, roster, authority="secondary", team=HOME)
        out, = [r for r in rows if r["availability"] == "out"]
        self.assertEqual((out["player_id"], out["quote"], out["status"]), ("m", follow, "reported"))
        self.assertEqual(doc[out["start"]:out["end"]], follow)
        anchor = out["antecedent"]
        self.assertEqual(doc[anchor["start"]:anchor["end"]], named)
        for prefix in (named + "\n", "Morgan Smith and Alex Jones are injured. ",
                       named + " The team then travelled. ", "Morgan Smith and Jack Carter are injured. ",
                       "Rumours say Morgan Smith was injured. ", named + " " + "Another person spoke. " * 2):
            self.assertFalse(any(r.get("antecedent") for r in news_ai.extract(prefix + follow, roster)), prefix)

    def test_inline_quotation_templates_keep_words_and_exclude_citation_fields(self):
        body = ("==Team news==\nOn 12 August, the manager described the injury as "
                '{{quote|text=a [[setback|major setback]]|author=private author}} and "a long recovery".'
                "<ref>{{cite news|title=external citation|date=12 August}}</ref>")
        para, = news_sources.wiki_prose(body).values()
        self.assertIn("“a major setback”", para)
        self.assertIn('"a long recovery"', para)
        self.assertNotIn("private author", para)
        self.assertNotIn("external citation", para)

    def test_old_wikipedia_paragraph_keys_survive_quote_projection_upgrade(self):
        source = news_sources.default_sources(HOME, "2026-27")[2]
        stamp = "2026-08-12T12:00:00Z"
        line = 'On 12 August, the coach described the injury as {{quote|text=a setback}}.'
        old = store.digest(news_sources._wiki_line(line, False))
        cursor = {"revid": 101, "revision_time": store.iso(stamp), "paragraphs": [old], "active": [old],
                  "active_versions": {old: 101}}

        def run(text, revision, previous):
            raw = json.dumps({"title": source["page_title"], "content_model": "wikitext",
                              "license": {"url": source["license_url"]}, "latest": {"id": revision, "timestamp": stamp},
                              "source": "==Team news==\n" + text}).encode()
            return news_sources._wiki(source, raw, stamp, previous)

        items, migrated, report = run(line, 101, cursor)
        self.assertEqual((items, report["revision_conflicts"], migrated["active"]), ([], [], [old]))
        items, same, _ = run(line, 102, migrated)
        self.assertEqual((items, same["active"]), ([], [old]))
        items, _, _ = run(line.replace("a setback", "a substantial setback"), 103, same)
        self.assertEqual([r["external_id"] for r in items if r.get("retracted")], [old])
        self.assertIn("“a substantial setback”", next(r["text"] for r in items if not r.get("retracted")))

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
