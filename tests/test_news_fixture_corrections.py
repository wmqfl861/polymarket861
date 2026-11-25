"""Schedule revisions re-evaluate captured news without recreating a past decision."""

import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from polymarket861 import news, news_events, news_roster, news_sources, pipeline, store
from polymarket861.market_provider import Reply
from test_news_matchday import AWAY, FID, HOME, KICKOFF, PUBLISHED, SEASON, add_club_feed, roster_transport, seed_fixture
from test_paired_proof import at


class NewsFixtureCorrections(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root, self.history = Path(tmp.name) / "runtime", Path(tmp.name) / "football"
        with at("2026-08-01T10:00Z"):
            seed_fixture(self.root, self.history)
            self.feeds = {team: add_club_feed(self.root, team) for team in (HOME, AWAY)}
        self.rosters = {team: roster_transport(team) for team in self.feeds}

    def collect(self, team, rows, *, extractor=None):
        source = self.feeds[team]
        doc = ET.Element(news_sources.ATOM + "feed")
        for ident, revision, message in rows:
            if message is None:
                ET.SubElement(doc, news_sources.TOMBSTONE + "deleted-entry", ref=ident, when=revision)
                continue
            entry = ET.SubElement(doc, news_sources.ATOM + "entry")
            ET.SubElement(entry, news_sources.ATOM + "link", href=source["url"].removesuffix("rss") + ident)
            for k, v in {"id": ident, "published": PUBLISHED, "updated": revision,
                         "title": "Squad news", "summary": message}.items():
                ET.SubElement(entry, news_sources.ATOM + k).text = v
        raw = ET.tostring(doc)

        def poll(config, name, season, cursor):
            now = store.now_utc()
            return news_sources.poll(config, name, season, cursor, getter=lambda url: Reply(raw, now, now, {}, url))

        result = news.sync(self.root, team=team, force=True, defaults=lambda *_: [], poller=poll, extractor=extractor,
                           roster_refresh=lambda r, t, s: news_roster.refresh(r, t, s, getter=self.rosters[t]))
        self.assertEqual(result["succeeded"], 1, result)

    def register(self, fid, kickoff, known_at, *, away=AWAY):
        return store.register_fixture(self.root, {
            "fixture_id": fid, "season": SEASON, "round_id": 1, "home": HOME, "away": away,
            "kickoff": kickoff, "known_at": known_at, "source": "revised timetable", "locator": "test/" + fid,
        })

    def view(self, when):
        return news.snapshot(self.root, FID, when)

    def test_corrected_kickoff_restores_unrepeated_story_with_proof_and_new_event(self):
        first, sealing, correction = "2026-08-12T12:25Z", "2026-08-12T12:59Z", "2026-08-12T13:05Z"
        revised = "2026-08-13T14:00Z"
        thursday = "Morgan Smith is ruled out of Thursday’s game against Chelsea after injuring his knee on international duty."
        with at(first):
            self.collect(HOME, [("dated", PUBLISHED, thursday)])
            self.collect(AWAY, [("original", PUBLISHED, "Leo James is doubtful for the game against Arsenal.")])
            self.assertEqual(news_events.refresh(self.root, history_root=self.history)["published"], 1)
            old_event = news_events.list_updates(self.root, FID)[0]
        with at(sealing):
            sealed = pipeline.make_forecast(self.root, FID, self.history, horizon="T-60m")
        old = self.view(sealing)
        self.assertEqual([f["external_id"] for f in old["facts"]], ["original"])
        with at(correction):
            # The publisher supplies an earlier announcement time. It cannot
            # affect the sealed snapshot before we actually saw its schedule.
            self.register(FID, revised, "2026-08-12T12:30Z")
            now = self.view(correction)
            self.assertTrue(now["coverage"]["both_teams_observed"])
            self.assertEqual(now["coverage"]["home"]["sources"][0]["recorded_kickoff"], store.iso(KICKOFF))
            self.assertEqual(now["schedule"]["recorded_at"], store.iso(correction))
            self.assertEqual({f["external_id"] for f in now["facts"]}, {"original", "dated"})
            dated = next(f for f in now["facts"] if f["external_id"] == "dated")
            self.assertEqual((dated["quote"], dated["recorded_kickoff"], dated["kickoff"]),
                             (thursday, store.iso(KICKOFF), store.iso(revised)))
            self.assertEqual((dated["received_at"], dated["known_at"]), (store.iso(first), store.iso(first)))
            older = next(f for f in now["facts"] if f["external_id"] == "original")
            self.assertEqual(older["id"], old["facts"][0]["id"])
            self.assertEqual(news_events.refresh(self.root, history_root=self.history)["published"], 1)
            changed, original = news_events.list_updates(self.root, FID)
            self.assertEqual((changed["trigger"], changed["previous_digest"], changed["cutoff"]),
                             ("schedule_changed", old_event["digest"], store.iso(correction)))
            self.assertEqual(original, old_event)
            self.assertEqual({a["id"] for a in changed["news_pair"]["applied"]}, {dated["id"], older["id"]})
        # A replacement and a tombstone have earlier publisher dates than their
        # actual arrival. A later reread never resurrects the first revision.
        edit, delete = "2026-08-12T13:15Z", "2026-08-12T13:25Z"
        with at(edit):
            self.collect(HOME, [("dated", "2026-08-12T13:06Z", "Morgan Smith is available again for the game against Chelsea.")])
        self.assertEqual(next(f["availability"] for f in self.view(edit)["facts"] if f["external_id"] == "dated"), "available")
        with at(delete):
            self.collect(HOME, [("dated", "2026-08-12T13:16Z", None)])
        with at("2026-08-12T13:35Z"):
            self.collect(HOME, [("dated", PUBLISHED, thursday)])
        self.assertEqual([f["external_id"] for f in self.view(delete)["facts"]], ["original"])
        self.assertEqual([f["external_id"] for f in self.view("2026-08-12T13:35Z")["facts"]], ["original"])
        self.assertEqual(self.view(correction), now)
        self.assertEqual(self.view(sealing), old)
        self.assertEqual(old, sealed["context"]["player_news"])
        self.assertEqual(pipeline.read_forecast(self.root, sealed["digest"]), sealed)

    def test_upgrade_reanalyses_a_previously_discarded_candidate_at_the_actual_retry_time(self):
        first, correction, retry = "2026-08-12T12:25Z", "2026-08-12T12:26Z", "2026-08-12T12:27Z"
        text = "Morgan Smith will miss Thursday’s match against Chelsea."
        with at(first):
            self.collect(HOME, [("legacy", PUBLISHED, text)], extractor=lambda *_a, **_k: [])
        # Old installations retained the original, its zero-claim analysis,
        # and the number dropped by their one-time scope check.
        with news._db(self.root, write=True) as db:
            db.execute("""UPDATE news_analysis_runs SET metadata=json_set(json_remove(metadata,'$.fixture_scope'),
                          '$.fixture_scope_excluded',1)""")
        with at(correction):
            self.register(FID, "2026-08-13T14:00Z", store.now_utc())
        pending = self.view(correction)
        self.assertEqual(pending["facts"], [])
        self.assertEqual(pending["coverage"]["home"]["sources"][0]["status"], "scope_reanalysis_pending")
        with at(retry):
            # The publisher no longer lists the story. The original on disk
            # is sufficient to run the new extractor inside the remote cache.
            self.collect(HOME, [])
        fact, = self.view(retry)["facts"]
        self.assertEqual((fact["quote"], fact["received_at"], fact["known_at"]), (text, store.iso(first), store.iso(retry)))
        self.assertEqual(self.view(correction), pending)

    def test_changed_order_and_long_postponement_do_not_roll_forward_evidence(self):
        first = "2026-08-12T12:15Z"
        with at(first):
            self.collect(HOME, [("original", PUBLISHED, "Morgan Smith is ruled out.")])
        self.assertEqual(len(self.view(first)["facts"]), 1)
        with at("2026-08-12T12:20Z"):
            self.register("other", "2026-08-12T13:30Z", store.now_utc(), away="Everton FC")
            now = self.view(store.now_utc())
            self.assertEqual(now["facts"], [])
            self.assertEqual(now["coverage"]["home"]["sources"][0]["status"], "different_next_fixture")
            self.assertEqual(news.snapshot(self.root, "other", store.now_utc())["facts"], [])
        with at("2026-08-12T12:30Z"):
            self.register("other", "2026-08-29T14:00Z", store.now_utc(), away="Everton FC")
            self.register(FID, "2026-08-26T14:00Z", store.now_utc())
            self.assertEqual(self.view(store.now_utc())["facts"], [])
        stale = self.view("2026-08-12T19:30Z")
        self.assertFalse(stale["coverage"]["home"]["observed"])
        self.assertEqual(stale["coverage"]["home"]["sources"][0]["status"], "stale")
        self.assertEqual(len(self.view(first)["facts"]), 1)

    def test_old_date_only_news_is_removed_and_restored_as_linked_prospective_events(self):
        with at("2026-08-12T12:15Z"):
            self.collect(HOME, [("dated", PUBLISHED, "Morgan Smith will miss Wednesday’s match against Chelsea.")])
            self.assertEqual(news_events.refresh(self.root, history_root=self.history)["published"], 1)
        for when, kickoff in (("2026-08-12T12:16Z", "2026-08-13T14:00Z"), ("2026-08-12T12:17Z", KICKOFF)):
            with at(when):
                self.register(FID, kickoff, store.now_utc())
                self.assertEqual(news_events.refresh(self.root, history_root=self.history)["published"], 1)
                self.assertEqual(news_events.refresh(self.root, history_root=self.history)["published"], 0)
        restored, removed, original = news_events.list_updates(self.root, FID)
        self.assertEqual((restored["revision"], removed["revision"], original["revision"]), (3, 2, 1))
        self.assertEqual([restored["previous_digest"], removed["previous_digest"]], [removed["digest"], original["digest"]])
        self.assertEqual(removed["snapshot"]["facts"], [])
        self.assertFalse(removed["news_pair"]["changed"])
        self.assertEqual([restored["trigger"], removed["trigger"]], ["schedule_changed", "schedule_changed"])
        self.assertEqual(restored["news_pair"]["with_news"], original["news_pair"]["with_news"])
