import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from polymarket861 import league_press as press, league_press_labels, league_registry, market_provider, store


class DomesticPressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.edition = "20261127120000"
        self.received = store.iso("2026-11-27T12:02:00Z")
        self.fixture = {"league": "lal", "season": "2026-27", "home": "Real Madrid CF",
                        "away": "Getafe CF", "kickoff": "2026-11-28T15:00:00Z"}

    def archive(self, edition, org="Real Madrid", *, stories=None):
        if stories is None:
            stories = [{"url": "https://example.org/report", "org": org, "theme": "TAX_FNCACT_FOOTBALLER,82;"}]
        rows = []
        for i, story in enumerate(stories):
            fields = [""] * 27
            fields[:5] = [f"{edition}-{i}", edition, "1", "example.org", story["url"]]
            fields[8] = story.get("theme", "")
            fields[13] = story.get("org", "")
            fields[14] = story.get("v2org", "")
            fields[26] = story.get("extras", "")
            rows.append("\t".join(fields))
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            z.writestr(zipfile.ZipInfo(edition + ".gkg.csv"), "\n".join(rows) + "\n")
        return stream.getvalue()

    def links(self, *stories):
        scanned, rows = press._parse(self.archive(self.edition, stories=stories), self.edition,
                                     self.received, press._clubs("2026-27"))
        self.assertEqual(scanned, len(stories))
        return rows

    def getter(self, *, head=None, wrong_checksum=False, org="Real Madrid"):
        latest = head or self.edition
        body = self.archive(latest, org)
        checksum = "0" * 32 if wrong_checksum else hashlib.md5(body, usedforsecurity=False).hexdigest()
        self.urls = []

        def get(url, *, timeout):
            self.urls.append(url)
            if url.endswith("lastupdate.txt"):
                data = f"{len(body)} {checksum} {press.BASE}{latest}.gkg.csv.zip".encode()
            else:
                data = self.archive(url.rsplit("/", 1)[-1][:14], org)
            return market_provider.Reply(data, self.received, self.received, {}, url)

        return get

    def test_shared_edition_cutoff_team_identity_and_duplicate_poll(self):
        get = self.getter()
        with patch.object(store, "now_utc", return_value=self.received):
            first = press.collect(self.root, quarters=1, get=get)
            second = press.collect(self.root, quarters=1, get=get)
        self.assertEqual((first["team_links"]["lal"], second["cached_editions"], len(self.urls)), (1, 1, 3))
        before = press.fixture_news_as_of(self.root, self.fixture, "2026-11-27T12:01:00Z")
        after = press.fixture_news_as_of(self.root, self.fixture, self.received)
        self.assertIsNone(before["evidence_sha256"])
        home, = after["teams"]["home"]["items"]
        self.assertEqual(home["matched_via"], "indexed_organization")
        self.assertEqual(home["received_at"], self.received)
        self.assertIsNone(home["publisher_published_at"])
        self.assertEqual(after["teams"]["away"]["items"], [])

    def test_future_manifest_defers_only_future_edition(self):
        with patch.object(store, "now_utc", return_value=self.received):
            out = press.collect(self.root, quarters=1, get=self.getter(head="20261127121500"))
        self.assertEqual(out["received_editions"], 1)
        self.assertTrue(self.urls[-1].endswith(self.edition + ".gkg.csv.zip"))

    def test_bad_checksum_and_city_name_do_not_create_club_evidence(self):
        with patch.object(store, "now_utc", return_value=self.received):
            invalid = press.collect(self.root, quarters=1, get=self.getter(wrong_checksum=True))
            empty = press.collect(self.root, quarters=1, get=self.getter(org="Madrid city council"))
        self.assertEqual(invalid["state"], "unavailable")
        self.assertEqual(sum(empty["team_links"].values()), 0)
        self.assertIsNone(press.fixture_news_as_of(self.root, self.fixture, self.received)["evidence_sha256"])
        json.dumps(empty)

    def test_each_current_club_has_reviewed_shorthand_and_only_its_seasons_membership(self):
        labels = press._clubs("2026-27")
        clubs = [(league, team) for league in league_registry.PROFILES
                 for team in league_registry.members(league, "2026-27")]
        self.assertEqual(len(clubs), 96)
        for league, team in clubs:
            self.assertIn(team, league_press_labels.ALIASES)
            self.assertTrue(any(name != press._words(team) and club == (league, team)
                                for name, club in labels.items()), team)
        self.assertEqual(labels["le mans"], ("fl1", "Le Mans FC"))
        self.assertNotIn("le mans", press._clubs("2023-24"))

    def test_title_only_record_uses_gkg_extras_and_preserves_provenance(self):
        url = "https://publisher.example/829"
        rows = self.links({"url": url, "extras": "<SOCIALIMAGE>Barcelona</SOCIALIMAGE>"
                           "<PAGE_TITLE>Fußball: Köln &amp; Stuttgart &#xE9;lan</PAGE_TITLE>"})
        self.assertEqual({key[1] for key in rows}, {"1. FC Köln", "VfB Stuttgart"})
        for doc in rows.values():
            self.assertEqual(doc["title"], "Fußball: Köln & Stuttgart élan")
            self.assertEqual((doc["title_source"], doc["matched_via"], doc["football_context"]),
                             ("gdelt_page_title", "indexed_title", "indexed_title:fussball"))
            self.assertEqual(doc["match_relevance"], "unverified")
            self.assertEqual(doc["received_at"], self.received)
            self.assertIsNone(doc["publisher_published_at"])

    def test_missing_title_is_null_and_ambiguous_names_need_football_context(self):
        url = "https://publisher.example/123"
        rows = self.links({"url": url, "v2org": "Real Madrid,112", "theme": "TAX_FNCACT_FOOTBALLER,2;"})
        doc = rows["lal", "Real Madrid CF", url]
        self.assertEqual(doc["football_context"], "indexed_theme:TAX_FNCACT_FOOTBALLER")
        self.assertIsNone(doc["title"])
        self.assertIsNone(doc["title_source"])
        self.assertFalse(self.links({"url": url, "org": "Real Madrid;AS Monaco;AS Roma;Le Mans;Paris FC",
                                     "theme": "TAX_WORLDFISH_FOOTBALLFISH;SPORTS;"}))
        self.assertFalse(self.links({"url": "https://travel.example/lyon-vs-lille"}))
        self.assertFalse(self.links({"url": "https://laliga.com.unrelated.example/no-title", "org": "Sevilla"}))

    def test_scope_and_local_false_positives_do_not_create_mens_club_rows(self):
        urls = ("motorsport/24-hours-of-le-mans-entry-list", "travel/weekend-in-le-mans-old-town",
                "finance/tax-havens-such-as-monaco", "minority-known-as-roma",
                "basketball/euroleague-real-madrid", "womens-football/manchester-united-women-wsl",
                "fußball/dortmund-ii-aufstieg", "futbol/barcelona-femeni-vs-madrid",
                "calcio/primavera/roma-fiorentina", "football/as-monaco-u-19s",
                "football/inter-miami-vs-newcastle-jets", "soccer/barcelona-sc-vs-emelec",
                "news/resort-le-havre-normandie", "soccer/como-juega-el-mallorca",
                "football/a-nice-lens-for-camera", "football/juventusville")
        for item in urls:
            with self.subTest(item=item):
                self.assertFalse(self.links({"url": "https://example.org/" + item}))
        self.assertFalse(self.links({"url": "https://site.example/football/man-utd", "org": "Manchester United Women"}))

    def test_competitive_match_paths_link_only_the_named_clubs(self):
        cases = (
            ("https://example.org/sport/barcelona-vs-sevilla-team-news", {"FC Barcelona", "Sevilla FC"}),
            ("https://example.org/football/juventus-napoli-preview-serie-a", {"Juventus FC", "SSC Napoli"}),
            ("https://example.org/bundesliga/werder-bremen-stuttgart-lineups", {"SV Werder Bremen", "VfB Stuttgart"}),
            ("https://ligue1.com/fr/articles/l1_article_6071-ou-et-quand-regarder-lille-lyon-j11-ligue-1-2026-2027",
             {"Lille OSC", "Olympique Lyonnais"}),
            ("https://example.org/football/inter-milan-signs-midfielder", {"FC Internazionale Milano"}),
            ("https://example.org/business/manchester-united-minority-investment", {"Manchester United FC"}),
            ("https://www.laliga.com/en/athletic-club-visit", {"Athletic Club"}),
            ("https://example.org/laliga/rcd-espanyol-de-barcelona-vs-sevilla-fc-4", {"RCD Espanyol de Barcelona", "Sevilla FC"}),
        )
        for url, teams in cases:
            self.assertEqual({key[1] for key in self.links({"url": url})}, teams, url)

    def test_legacy_projection_rechecks_old_links_without_changing_archived_rows(self):
        folder = self.root / "team-press"
        folder.mkdir()
        fixture = dict(self.fixture, league="fl1", home="Le Mans FC", away="Lille OSC")
        urls = ("https://example.org/football/le-mans-vs-lorient", "https://example.org/cars/ford-le-mans-hypercar")
        with closing(sqlite3.connect(folder / "index.sqlite3")) as db, db:
            db.executescript(press.SCHEMA)
            db.execute("INSERT INTO press_releases VALUES(?,?,?,?,?,?)",
                       (self.edition, "sum", self.received, "2026-27", '["fl1"]', 2))
            for url in urls:
                doc = {"url": url, "matched_via": "article_url", "index_seen_at": self.received, "club_label": "le mans"}
                db.execute("INSERT INTO press_clubs VALUES(?,?,?,?,?)",
                           (self.edition, "fl1", "Le Mans FC", url, store.canonical(doc)))
        at = press.fixture_news_as_of(self.root, fixture, self.received)
        item, = at["teams"]["home"]["items"]
        self.assertEqual(item["url"], urls[0])
        self.assertTrue(item["legacy_rechecked"])
        self.assertIsNone(item["title"])
        with closing(sqlite3.connect(folder / "index.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM press_clubs").fetchone()[0], 2)

    def test_duplicate_missing_title_and_wrong_sport_theme(self):
        url = "https://example.org/football/juventus-news"
        titled = {"url": url, "extras": "<PAGE_TITLE>Juventus team news</PAGE_TITLE>"}
        for rows in (self.links(titled, {"url": url}), self.links({"url": url}, titled)):
            self.assertEqual(rows["sea", "Juventus FC", url]["title"], "Juventus team news")
        self.assertFalse(self.links({"url": "https://example.org/headline", "org": "Real Madrid CF",
                                     "theme": "TAX_FNCACT_BASKETBALL_COACH,22;"}))

    def test_a_repeated_url_keeps_only_a_title_already_known_at_the_query_time(self):
        directory = self.root / "team-press"
        directory.mkdir()
        url = "https://example.org/football/real-madrid-vs-getafe-team-news"
        with closing(sqlite3.connect(directory / "index.sqlite3")) as db, db:
            db.executescript(press.SCHEMA)
            for edition, received, title in (
                ("20261127120000", "2026-11-27T12:02:00Z", ""),
                ("20261127121500", "2026-11-27T12:17:00Z", "<PAGE_TITLE>Club team news</PAGE_TITLE>"),
                ("20261127123000", "2026-11-27T12:32:00Z", ""),
            ):
                stamp = store.iso(received)
                scanned, docs = press._parse(self.archive(edition, stories=[{"url": url, "extras": title}]),
                                             edition, stamp, press._clubs("2026-27"))
                db.execute("INSERT INTO press_releases VALUES(?,?,?,?,?,?)",
                           (edition, "sum", stamp, "2026-27", '["lal"]', scanned))
                db.executemany("INSERT INTO press_clubs VALUES(?,?,?,?,?)",
                               ((edition, *key, store.canonical(v)) for key, v in docs.items()))
        historical = press.fixture_news_as_of(self.root, self.fixture, self.received)
        self.assertIsNone(historical["teams"]["home"]["items"][0]["title"])
        with_title = press.fixture_news_as_of(self.root, self.fixture, "2026-11-27T12:18:00Z")
        repeated = press.fixture_news_as_of(self.root, self.fixture, "2026-11-27T12:33:00Z")
        article = repeated["teams"]["home"]["items"][0]
        self.assertEqual((article["title"], article["title_received_at"], article["title_record_id"]),
                         ("Club team news", store.iso("2026-11-27T12:17:00Z"), "20261127121500-0"))
        self.assertEqual(repeated["evidence_sha256"], with_title["evidence_sha256"])
