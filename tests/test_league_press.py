import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from polymarket861 import league_press as press, market_provider, store


class DomesticPressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.edition = "20261127120000"
        self.received = store.iso("2026-11-27T12:02:00Z")
        self.fixture = {"league": "lal", "season": "2026-27", "home": "Real Madrid CF",
                        "away": "Getafe CF", "kickoff": "2026-11-28T15:00:00Z"}

    def archive(self, edition, org="Real Madrid"):
        fields = [edition + "-1", edition, "1", "example.org", "https://example.org/report",
                  *([""] * 8), org, ""]
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            z.writestr(zipfile.ZipInfo(edition + ".gkg.csv"), "\t".join(fields) + "\n")
        return stream.getvalue()

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
