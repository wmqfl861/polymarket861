import gzip
import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from polymarket861 import store
from scripts import news_history_archive as archive, reproduce_news_history as history


class NewsArchiveEvidence(unittest.TestCase):
    def setUp(self):
        self.cut = store.instant("2025-08-16T13:00:00Z")
        self.start = self.cut - timedelta(days=14)
        self.phrase = "James Barton sustained a knee injury during pre-season."
        self.page = {
            "title": "James Barton",
            "id": 51,
            "parentid": 50,
            "published_at": "2025-08-07T10:00:00Z",
            "text": "==Club career==\n====2025–26====\n"
            + self.phrase
            + "<ref>{{cite news |date=3 August 2025 |access-date=7 August 2025}}</ref>",
        }

    def test_first_appearance_follows_parent_for_unrelated_changing_revision(self):
        self.page["text"] = "==News==\n" + self.phrase
        initial = self.page | {
            "id": 50,
            "parentid": 49,
            "published_at": "2025-08-03T10:00:00Z",
        }
        before = initial | {"id": 49, "parentid": 48, "text": "==News==\nNo update."}
        reader = Mock()
        reader.get.side_effect = lambda title, oldid: {50: initial, 49: before}[oldid]
        first, parent = archive.story_origin(
            reader, self.page, self.phrase, "club", self.start
        )
        self.assertEqual((first["id"], parent["id"]), (50, 49))
        reader.get.side_effect = lambda title, oldid: (
            initial | {"published_at": "2025-07-20T00:00:00Z"}
        )
        self.assertIsNone(
            archive.story_origin(reader, self.page, self.phrase, "club", self.start)
        )

    def test_biography_citation_receipt_access_date_cannot_renew_old_report(self):
        result = archive._calendar_evidence(
            self.page, self.phrase, "biography", self.start, self.cut
        )
        self.assertEqual(result["dates"], ["2025-08-03"])
        for old in (
            self.page["text"].replace("date=3 August 2025", "date=3 August 2019"),
            self.page["text"].replace(
                "|date=3 August 2025", "|access-date=3 August 2025"
            ),
            self.page["text"].replace("2025–26", "2019–20"),
        ):
            self.assertIsNone(
                archive._calendar_evidence(
                    self.page | {"text": old},
                    self.phrase,
                    "biography",
                    self.start,
                    self.cut,
                )
            )

    def test_future_publication_parent_and_bad_sentence_remain_out(self):
        kickoff = self.cut + timedelta(hours=1)
        f = SimpleNamespace(
            fixture_id="fixture", home="Club", away="Others", kickoff=kickoff
        )
        proof = {
            "id": 3,
            "title": "Club",
            "url": "https://en.wikipedia.org/w/index.php?oldid=3",
            "parentid": 0,
            "sha256": "a" * 64,
            "published_at": "2025-08-17T00:00:00Z",
            "received_at": "2026-11-25T00:00:00Z",
        }
        with self.assertRaisesRegex(ValueError, "proof"):
            history._proof({"3": proof}, 3, store.iso(self.cut))
        proof["published_at"] = "2025-08-15T00:00:00Z"
        panel = {
            "fixture_id": "fixture",
            "team": "Club",
            "cutoff": store.iso(self.cut),
            "roster": [],
            "roster_revision": None,
            "sources": [{"kind": "club", "title": "Club", "closing": 3}],
            "stories": [
                {
                    "known_from": "club",
                    "origin": 3,
                    "previous": 0,
                    "text": self.phrase,
                    "sentence_sha256": "b" * 64,
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "ancestry"):
            history.extract_side(panel, {"3": proof}, f, "2026-11-25T00:00:00Z")

    def test_published_cohort_keeps_all_thirty_and_linked_historical_claims(self):
        panel = json.loads(gzip.decompress(history.DEFAULT_INPUTS.read_bytes()))
        self.assertEqual(store.digest(panel), history.PUBLISHED_INPUTS)
        self.assertEqual(len(panel["snapshot"]), 60)
        self.assertEqual(len({s["fixture_id"] for s in panel["snapshot"]}), 30)
        self.assertTrue(
            all(
                store.instant(p["received_at"]).year > 2025
                for p in panel["revisions"].values()
            )
        )
        side = next(
            s
            for s in panel["snapshot"]
            if s["fixture_id"].endswith("/tottenham-hotspur/burnley")
            and "Tottenham" in s["team"]
        )
        f = SimpleNamespace(
            fixture_id=side["fixture_id"],
            home=side["team"],
            away="Burnley FC",
            kickoff=store.instant(side["cutoff"]) + timedelta(hours=1),
        )
        claims = history.extract_side(
            side, panel["revisions"], f, "2027-01-01T00:00:00Z"
        )
        links = [c for c in claims if c["player_name"] == "James Maddison"]
        self.assertTrue(links)
        self.assertTrue(
            all(
                c["status"] == "reported" and c["availability"] == "unknown"
                for c in links
            )
        )
        self.assertTrue(
            all(
                store.instant(c["published_at"])
                < self.cut
                < store.instant(c["received_at"])
                for c in links
            )
        )
