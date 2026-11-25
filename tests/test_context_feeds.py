"""Provider adapters are exercised with recorded-shape responses, without network access."""

import json
import hashlib
import io
import tempfile
import unittest
import zipfile
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode

from polymarket861 import context_feeds as feeds
from polymarket861 import evidence, store
from polymarket861.market_provider import Reply


class FeedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        now = store.instant(store.now_utc())
        self.run = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        self.start = (now + timedelta(days=2)).replace(hour=15, minute=30, second=0, microsecond=0)
        self.venue = {"latitude": 52.94, "longitude": -1.13, "venue":"City Ground",
                      "venue_locator":"owned://test-venue-coordinate"}
        store.register_fixture(self.root, {"fixture_id": "F", "home":"Forest", "away":"Chelsea",
            "season":"test-season", "round_id":2, "kickoff": store.iso(self.start),
            "known_at":store.iso(now - timedelta(days=1)), "source":"test", "locator":"owned://fixture"})

    def getter(self, payload):
        def fetch(endpoint, params, *, timeout):
            self.assertGreater(timeout, 0)
            return Reply(json.dumps(payload).encode(), store.now_utc(), store.now_utc(), {},
                         endpoint + "?" + urlencode(params))
        return fetch

    def weather_payload(self):
        date = self.start.date().isoformat()
        return {"latitude":53, "longitude":-1.125, "elevation":67, "utc_offset_seconds":0,
            "vendor_secret": "excluded",
            "hourly_units": {"time":"iso8601", **feeds.VARIABLES, "extra_units":"excluded"},
            "hourly":{"time":[date + "T15:00",date + "T16:00"],"temperature_2m":[9.2,10.0],
                      "precipitation":[0,1.5], "wind_speed_10m":[6.0,7.2],"wind_gusts_10m":[11,12],
                      "extra_series":["excluded","excluded"]}}

    def weather(self, payload, run=None):
        return feeds.collect_weather(self.root, "F", **self.venue, run_at=store.iso(run or self.run),
                                     getter=self.getter(payload))

    def test_open_meteo_permission_first_receipt_units_and_revisions(self):
        with self.assertRaisesRegex(ValueError, "noncommercial"):
            feeds.enable(self.root, feeds.WEATHER)
        with self.assertRaisesRegex(ValueError, "grant"):
            self.weather(self.weather_payload())
        feeds.enable(self.root, feeds.WEATHER, noncommercial=True)
        raw = self.weather_payload()
        receipt = self.weather(raw)
        self.assertEqual(receipt["status"], "ok")
        original, = receipt["evidence_ids"]
        fact = evidence.get_evidence(self.root, original)
        self.assertEqual(fact["value"]["forecast_for"],store.iso(self.start))
        self.assertEqual(fact["published_at"],evidence.utc(receipt["received_at"]))
        self.assertNotEqual(fact["published_at"],fact["value"]["run_at"])
        self.assertEqual(fact["value"]["issue_time_basis"],"first_local_response_upper_bound")
        self.assertEqual(len(fact["value"]["forecast_hours"]),2)
        self.assertEqual(fact["value"]["units"]["wind_speed_10m"],"m/s")
        archive = json.loads(store.blob_path(self.root,receipt["snapshot_sha256"]).read_bytes())
        self.assertNotIn("extra_series",archive["hourly"])
        self.assertNotIn("vendor_secret",archive)
        self.assertEqual(self.weather(raw)["evidence_ids"],[original])
        raw["hourly"]["temperature_2m"][0] = 12.0
        fixed, = self.weather(raw)["evidence_ids"]
        older, = self.weather(raw, self.run-timedelta(hours=6))["evidence_ids"]
        now = store.now_utc()
        ctx = evidence.as_of_context(self.root,"F","Forest","Chelsea",now,purpose="trial")
        self.assertEqual(ctx["evidence_ids"],[fixed])
        self.assertIn(older,ctx["corrections"][0]["superseded_ids"])
        self.assertEqual(evidence.get_evidence(self.root,original)["value"]["forecast_hours"][0]["temperature_2m"],9.2)
        self.assertEqual(evidence.as_of_context(self.root,"F","Forest","Chelsea",now)["evidence_ids"],[])

    def test_approved_weather_can_reach_context_after_its_real_first_receipt(self):
        grant = feeds.providers()[feeds.WEATHER]
        grant["fields"]["weather"]["uses"] = ["trial", "prediction"]
        evidence.register_source(self.root, dict(grant, status="active",free=True,noncommercial_use_confirmed=True))
        before = store.now_utc()
        receipt = self.weather(self.weather_payload())
        self.assertEqual(evidence.as_of_context(self.root,"F","Forest","Chelsea",before,prospective=True)["facts"],[])
        ctx = evidence.as_of_context(self.root,"F","Forest","Chelsea",store.now_utc(),prospective=True)
        self.assertEqual(ctx["evidence_ids"],receipt["evidence_ids"])
        self.assertFalse(evidence.as_of_context(self.root,"F","Forest","Chelsea",store.now_utc(),purpose="training")["facts"])

    def test_gdelt_metadata_index_time_syndication_and_raw_projection(self):
        feeds.enable(self.root, feeds.GDELT)
        yesterday = (store.instant(store.now_utc())-timedelta(days=1)).strftime("%Y%m%dT%H%M%SZ")
        article = {"url":"https://news.example/story?utm_campaign=ad", "title":"A sports headline",
                   "seendate":yesterday, "domain":"news.example", "language":"English", "sourcecountry":"UK",
                   "socialimage":"do-not-store", "body":"do-not-store article text"}
        raw = {"articles":[article, dict(article,url="https://news.example/story"),
                           dict(article,url="https://wire.example/dispatch",domain="wire.example"),
                           dict(article,url="https://news.example/no-time",seendate=None),
                           dict(article,url="https://news.example/future",seendate="20990701T010000Z")]}
        fetched = feeds.collect_news(self.root,"F","Chelsea",getter=self.getter(raw))
        self.assertEqual(fetched["rows_skipped"]["duplicate_url"],1)
        self.assertEqual(fetched["rows_skipped"]["invalid_or_missing_time"],2)
        self.assertEqual(len(fetched["evidence_ids"]),2)
        facts = [evidence.get_evidence(self.root,ident) for ident in fetched["evidence_ids"]]
        self.assertEqual(len({f["value"]["headline_fingerprint"] for f in facts}),1)
        self.assertIsNone(facts[0]["value"]["publisher_published_at"])
        self.assertEqual(facts[0]["value"]["time_basis"],"index_observation_only")
        raw_on_disk = store.blob_path(self.root,fetched["snapshot_sha256"]).read_text()
        self.assertNotIn("do-not-store",raw_on_disk)
        self.assertNotIn("A sports headline",raw_on_disk)
        self.assertEqual(evidence.as_of_context(self.root,"F","Forest","Chelsea",store.now_utc())["facts"],[])
        with self.assertRaisesRegex(ValueError, "scheduled team"):
            feeds.collect_news(self.root,"F","Unrelated",getter=self.getter(raw))

    def test_provider_errors_and_invalid_forecast_preserve_health_diagnosis(self):
        feeds.enable(self.root,feeds.GDELT)
        def unavailable(endpoint, params, *, timeout):
            raise HTTPError(endpoint,503,"Service Unavailable",{},None)
        result = feeds.collect_news(self.root,"F","Forest",getter=unavailable)
        self.assertEqual((result["status"],result["http_status"],result["evidence_ids"]),("error",503,[]))
        self.assertEqual(evidence.source_report(self.root,feeds.GDELT)["health"]["news"]["status"],"error")
        feeds.enable(self.root,feeds.WEATHER,noncommercial=True)
        raw = self.weather_payload()
        raw["hourly_units"]["wind_speed_10m"] = "km/h"
        result = self.weather(raw)
        self.assertEqual(result["status"],"invalid")
        self.assertIn("units",result["detail"])
        self.assertFalse(result["evidence_ids"])

    def test_gkg_bulk_fallback_and_same_original_across_gdelt_products(self):
        for sid in (feeds.GDELT, feeds.GKG):
            feeds.enable(self.root,sid)
        yesterday = (store.instant(store.now_utc())-timedelta(days=1)).replace(minute=0,second=0,microsecond=0)
        timestamp = yesterday.strftime("%Y%m%d%H%M%S")
        url = "https://club.example/football/chelsea-update"
        fields = [""]*27
        fields[0:5] = [timestamp+"-9",timestamp,"1","club.example",url]
        fields[13] = "Chelsea Football Club"
        fields[26] = "DO-NOT-ARCHIVE"
        compressed = io.BytesIO()
        with zipfile.ZipFile(compressed,"w") as archive:
            archive.writestr(timestamp+".gkg.csv", "\t".join(fields))
        body = compressed.getvalue()
        release = feeds.GKG_BASE + timestamp + ".gkg.csv.zip"
        checksum = hashlib.md5(body,usedforsecurity=False).hexdigest()
        manifest = f"{len(body)} {checksum} {release}".encode()
        def serve(endpoint,params,*,timeout):
            if endpoint == feeds.NEWS_URL:
                raise HTTPError(endpoint,503,"service unavailable",{},None)
            payload = manifest if endpoint.endswith("lastupdate.txt") else body
            return Reply(payload,store.now_utc(),store.now_utc(),{},endpoint)
        result = feeds.collect_news(self.root,"F","Chelsea",getter=serve)
        self.assertEqual(result["source_id"],feeds.GKG)
        self.assertEqual(result["primary_attempt"]["http_status"],503)
        self.assertEqual(len(result["evidence_ids"]),1)
        projection = store.blob_path(self.root,result["snapshot_sha256"]).read_text()
        self.assertNotIn("DO-NOT-ARCHIVE",projection)
        self.assertEqual(json.loads(projection)["release_rows_scanned"],1)
        extra = {"articles":[{"url":url,"title":"An index title","domain":"club.example",
                 "seendate":yesterday.strftime("%Y%m%dT%H%M%SZ")}]}
        feeds.collect_news(self.root,"F","Chelsea",getter=self.getter(extra))
        ctx = evidence.as_of_context(self.root,"F","Forest","Chelsea",store.now_utc(),purpose="trial")
        self.assertEqual(len(ctx["facts"]),1)
        self.assertEqual(len(ctx["evidence_ids"]),2)


if __name__ == "__main__":
    unittest.main()
