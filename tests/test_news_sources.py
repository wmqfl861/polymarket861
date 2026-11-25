import json
import unittest
from datetime import timedelta

from polymarket861 import news_sources as news, store
from polymarket861.market_provider import Reply

CLOCK = "2026-11-25T12:00:00.000000Z"
CLUB = "Chelsea FC"
SEASON = "2026-27"


def feed(**options):
    return news.validate_source({
        "id": "club-bulletins", "kind": "rss", "url": "https://chelsea.example/club.xml",
        "team": CLUB, "free": True, "authority": "official", "license": "operator-grant",
        "license_url": "https://chelsea.example/grant", "attribution": "Club staff",
        "rights": list(news.RIGHTS), **options,
    })


def run(source, data, cursor=None, *, at=CLOCK):
    def getter(url):
        raw = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        return Reply(raw, at, at, {"Cache-Control": "max-age=180, s-maxage=360"}, url)

    return news.poll(source, source["team"], SEASON, cursor, getter=getter)


def availability(rows, at=CLOCK):
    return {
        "meta": {"source": "Touchline Wire Tactical API", "timestamp_iso": at, "attribution_url": news.WIRE_HOST,
                 "count": len(rows), "license": "Free for personal, academic, and open-source use with attribution"},
        "data": rows,
    }


def wire_row():
    return {"player_id": 4, "display_name": "Joe Example", "team": "Chelsea", "status": "doubtful",
            "category": "injury", "chance_of_playing": 25, "news": "Other publisher's prose",
            "gemini_intel": {"manager_quote": "Unverified manager quote"}}


class NewsSources(unittest.TestCase):
    def test_availability_scope_retries_tombstone_and_revision(self):
        src = news.default_sources(CLUB, SEASON)[0]
        first = run(src, availability([wire_row()]))
        row, = first["articles"]
        self.assertIn("Joe Example is doubtful", row["text"])
        self.assertEqual(row["time_basis"], "provider_snapshot")
        self.assertEqual(row["observed_state_as_of"], CLOCK)
        self.assertNotIn("manager quote", json.dumps(row))
        self.assertNotIn("publisher's prose", json.dumps(row))
        self.assertEqual(first["next_poll_after"], store.iso(store.instant(CLOCK) + timedelta(minutes=6)))
        self.assertFalse(run(src, availability([wire_row()]), first["cursor"])["articles"])
        with self.assertRaisesRegex(ValueError, "contradictory"):
            run(src, availability([]), first["cursor"])
        with self.assertRaisesRegex(ValueError, "club filter"):
            run(src, availability([wire_row() | {"team": "Spurs"}]), first["cursor"])
        later = "2026-11-25T12:40:00.000000Z"
        updated = run(src, availability([], later), first["cursor"], at=later)
        removed, = updated["articles"]
        self.assertTrue(removed["retracted"])
        self.assertEqual(removed["external_id"], row["external_id"])
        self.assertNotIn("recovered", removed["text"])
        with self.assertRaisesRegex(ValueError, "backwards"):
            run(src, availability([wire_row()]), updated["cursor"])

    def test_wire_requires_current_license_season_and_source_clock(self):
        src = news.default_sources(CLUB, SEASON)[0]
        data = availability([])
        data["meta"]["license"] = "paid"
        with self.assertRaisesRegex(ValueError, "policy"):
            run(src, data)
        with self.assertRaisesRegex(ValueError, "after local"):
            run(src, availability([], "2026-11-26T00:00:00Z"))
        with self.assertRaisesRegex(ValueError, "stale"):
            run(src, availability([], "2026-11-23T00:00:00Z"))

    def test_rumour_scope_dates_and_canonical_club_code(self):
        source = news.default_sources("Nottingham Forest FC", SEASON)[1]
        self.assertTrue(source["url"].endswith("=NFO"))
        base = {
            "id": "rumour-1", "rumour_type": "transfer", "subject_name": "Joe Example",
            "current_club": "Nott'm Forest", "target_club_names": ["Spurs"], "status": "active",
            "published_at": "2026-11-23T12:43:00Z", "last_updated": "2026-11-25",
            "source_outlet": "A publisher", "source_headline": "Third-party text",
        }
        blob = availability([])
        blob["data"] = {"club_filter": "NFO", "season": "2026/27", "count": 2,
                        "rumours": [base, base | {"id": "inbound", "current_club": "Spurs"}]}
        initial = run(source, blob)
        row, = initial["articles"]
        self.assertEqual(row["published_at"], store.iso(base["published_at"]))
        self.assertEqual(row["updated_at"], CLOCK)
        self.assertIn("could leave Nottingham Forest FC to join Spurs", row["text"])
        self.assertNotIn("Third-party text", json.dumps(row))
        self.assertFalse(run(source, blob, initial["cursor"])["articles"])
        blob["data"]["club_filter"] = None
        with self.assertRaisesRegex(ValueError, "unscoped"):
            run(source, blob)

    def test_wiki_initial_snapshot_and_unrelated_table_edits_are_not_news(self):
        source = news.default_sources(CLUB, SEASON)[2]
        prior = "==Season summary==\nOn 8 August, Joe Example was injured in pre-season.\n"
        def revision(text, n, when):
            return {"title": source["page_title"], "content_model": "wikitext",
                    "source": text, "latest": {"id": n, "timestamp": when},
                    "license": {"url": news.WIKI_LICENSE + "deed.en"}}
        first = run(source, revision(prior, 10, "2026-11-23T01:00:00Z"))
        self.assertFalse(first["articles"])
        text = prior + "\nOn 24 November, [[Joe Example]] returned to training after injury.<ref>other text</ref>"
        second = run(source, revision(text, 11, "2026-11-24T06:00:00Z"), first["cursor"])
        row, = second["articles"]
        self.assertNotIn("[[", row["text"])
        self.assertNotIn("other text", row["text"])
        self.assertEqual(row["published_at"], store.iso("2026-11-24T06:00:00Z"))
        self.assertIn("oldid=11", row["url"])
        unrelated = text + "\n{|\n| 24 November Bob Example injured\n|}\n==Matches==\nOn 25 November X returned to training."
        third = run(source, revision(unrelated, 12, "2026-11-25T06:00:00Z"), second["cursor"])
        self.assertFalse(third["articles"])
        quiet = run(source, revision(prior, 13, "2026-11-25T07:00:00Z"), third["cursor"])
        self.assertEqual(quiet["articles"][0]["external_id"], row["external_id"])
        self.assertTrue(quiet["articles"][0]["retracted"])
        old_event = prior + "\nOn 15 July, Bob Example was injured during training."
        self.assertFalse(run(source, revision(old_event, 14, CLOCK), quiet["cursor"])["articles"])
        with self.assertRaisesRegex(ValueError, "regressing"):
            run(source, revision(prior, 12, CLOCK), quiet["cursor"])

    def test_rss_news_requires_explicit_rights_and_preserves_embedded_text(self):
        for opts in ({"free": False}, {"rights": ["fetch"]}, {"url": "http://local/feed"},
                     {"url": "https://127.0.0.1/feed"}, {"license_url": "not-a-url"}):
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                feed(**opts)
        src = feed()
        raw = """<rss><channel>
          <item><guid>a</guid><link>https://chelsea.example/u</link><title>Team news</title>
          <pubDate>Tue, 24 Nov 2026 10:05:00 GMT</pubDate>
          <description><![CDATA[<p>Joe Example is out.</p><script>ignore me</script>]]></description></item>
          <item><guid>b</guid><link>https://unrelated.example/f</link><title>Other outlet</title>
          <pubDate>Tue, 24 Nov 2026 10:05:00 GMT</pubDate></item>
          <item><guid>f</guid><link>https://chelsea.example/q</link><title>Future</title>
          <pubDate>Thu, 26 Nov 2026 10:05:00 GMT</pubDate></item>
          </channel></rss>"""
        result = run(src, raw)
        article, = result["articles"]
        self.assertIn("Joe Example is out.", article["text"])
        self.assertNotIn("ignore me", article["text"])
        self.assertEqual(result["invalid_entries"], 2)
        self.assertFalse(run(src, raw, result["cursor"])["articles"])
        self.assertEqual(article["published_at"], "2026-11-24T10:05:00.000000Z")
        empty = run(src, "<rss><channel /></rss>", result["cursor"])
        self.assertFalse(empty["articles"])
        with self.assertRaisesRegex(ValueError, "document type"):
            run(src, "<!DOCTYPE root><rss><channel /></rss>")

    def test_large_feed_finishes_in_bounded_polls_and_malformed_feed_is_visible(self):
        src = feed()
        def text(i):
            return (f'<item><guid>{i}</guid><link>https://chelsea.example/n/{i}</link>'
                    f'<title>Story {i}</title><pubDate>Tue, 24 Nov 2026 10:05:00 GMT</pubDate></item>')
        raw = '<rss><channel>' + ''.join(text(i) for i in range(140)) + '</channel></rss>'
        initial = run(src, raw)
        rest = run(src, raw, initial["cursor"])
        self.assertEqual(initial["pending_entries"], 40)
        self.assertEqual(rest["pending_entries"], 0)
        self.assertEqual(len({r["external_id"] for r in initial["articles"] + rest["articles"]}), 140)
        self.assertFalse(run(src, raw, rest["cursor"])["articles"])
        with self.assertRaisesRegex(ValueError, "all 1 publisher"):
            run(src, '<rss><channel><item><guid>broken</guid></item></channel></rss>')

    def test_atom_explicit_withdrawal_cursor_scope_and_source_seal(self):
        src = feed()
        raw = """<feed xmlns="http://www.w3.org/2005/Atom" xmlns:at="http://purl.org/atompub/tombstones/1.0">
        <entry><id>urn:note:1</id><link href="https://chelsea.example/press"/>
        <title>Availability</title><content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Joe will miss out.</p></div></content>
        <published>2026-11-23T10:00:00Z</published><updated>2026-11-23T11:00:00Z</updated></entry></feed>"""
        base = run(src, raw)
        self.assertIn("Joe will miss out.", base["articles"][0]["text"])
        tomb = raw[:raw.index("<entry>")] + '<at:deleted-entry ref="urn:note:1" when="2026-11-24T10:00:00Z"/></feed>'
        res = run(src, tomb, base["cursor"])
        self.assertTrue(res["articles"][0]["retracted"])
        self.assertEqual(res["articles"][0]["external_id"], base["articles"][0]["external_id"])
        with self.assertRaisesRegex(ValueError, "another feed"):
            run(feed(url="https://chelsea.example/v2"), raw, base["cursor"])
        built = news.default_sources(CLUB, SEASON)[0]
        with self.assertRaisesRegex(ValueError, "built-in"):
            news.validate_source(built | {"authority": "official"})
        def redirected(url):
            return Reply(b"", CLOCK, CLOCK, {}, "https://unrelated.example/payload")
        with self.assertRaisesRegex(ValueError, "does not match"):
            news.poll(src, CLUB, SEASON, getter=redirected)


if __name__ == "__main__":
    unittest.main()
