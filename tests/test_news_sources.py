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
        vanished = run(src, availability([]), first["cursor"])
        self.assertEqual([(c["external_id"], c["revision"]) for c in vanished["revision_conflicts"]],
                         [(row["external_id"], row["revision"])])
        self.assertEqual(vanished["articles"], [])
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

    def test_provider_revisions_quarantine_precise_player_and_recover(self):
        src = news.default_sources(CLUB, SEASON)[0]
        out = wire_row() | {"status": "injured", "chance_of_playing": 0}
        other = wire_row() | {"player_id": 5, "display_name": "Rob Example"}
        base = run(src, availability([out, other]))
        victim = base["articles"][0]
        contradicted = run(src, availability([wire_row(), other]), base["cursor"])
        hold, = contradicted["revision_conflicts"]
        self.assertEqual((hold["external_id"], hold["revision"]), (victim["external_id"], victim["revision"]))
        self.assertEqual(contradicted["articles"], [])
        repeated = run(src, availability([other, out]), contradicted["cursor"])
        self.assertEqual(repeated["revision_conflicts"], [hold])
        later = "2026-11-25T12:30:00.000000Z"
        restored = run(src, availability([wire_row(), other], later), repeated["cursor"], at=later)
        self.assertFalse(restored["revision_conflicts"])
        self.assertEqual([(r["external_id"], r["revision"]) for r in restored["articles"]],
                         [(victim["external_id"], later)])
        self.assertEqual(len(restored["wire_observations"]), 2)

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

    def test_coarse_rumour_date_does_not_mask_a_contradiction_in_one_precise_snapshot(self):
        src = news.default_sources(CLUB, SEASON)[1]
        def state(at, club):
            story = {"id": "rumour-1", "rumour_type": "transfer", "subject_name": "Joe Example",
                     "current_club": "Chelsea", "target_club_names": [club], "status": "active",
                     "published_at": "2026-11-23T12:43:00Z", "last_updated": "2026-11-25"}
            return availability([], at) | {"data": {"club_filter": "CHE", "season": "2026/27", "count": 1,
                                                    "rumours": [story]}}
        first = run(src, state(CLOCK, "Spurs"))
        row, = first["articles"]
        second = run(src, state(CLOCK, "Arsenal"), first["cursor"])
        self.assertIn((row["external_id"], row["revision"]),
                      [(c["external_id"], c["revision"]) for c in second["revision_conflicts"]])
        self.assertFalse(second["articles"])
        later = "2026-11-25T12:30:00.000000Z"
        third = run(src, state(later, "Arsenal"), second["cursor"], at=later)
        restored, = third["articles"]
        self.assertNotEqual(restored["revision"], row["revision"])
        self.assertIn("join Arsenal", restored["text"])

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

    def test_complete_feed_retains_oldest_before_window_rotates_and_refuses_oversized_feed(self):
        src = feed()
        def text(i):
            return (f'<item><guid>{i}</guid><link>https://chelsea.example/n/{i}</link>'
                    f'<title>Story {i}</title><pubDate>Tue, 24 Nov 2026 10:05:00 GMT</pubDate></item>')
        def window(ids):
            return '<rss><channel>' + ''.join(text(i) for i in ids) + '</channel></rss>'
        initial = run(src, window(range(101)))
        self.assertEqual(initial["pending_entries"], 0)
        self.assertEqual({r["external_id"] for r in initial["articles"]}, {str(i) for i in range(101)})
        restored_cursor = json.loads(json.dumps(initial["cursor"]))
        later = run(src, window(range(1, 102)), restored_cursor)
        self.assertEqual([r["external_id"] for r in later["articles"]], ["101"])
        self.assertFalse(later["articles"][0].get("retracted"))
        self.assertEqual(len(run(src, window(range(news.MAX_ITEMS)))["articles"]), news.MAX_ITEMS)
        with self.assertRaisesRegex(ValueError, "limit exceeded"):
            run(src, window(range(news.MAX_ITEMS + 1)), restored_cursor)
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

    def test_same_atom_update_and_id_conflict_across_polls_and_in_one_response(self):
        src = feed()
        def entry(ident, body, clock="10:00"):
            return (f'<entry><id>{ident}</id><updated>2026-11-24T{clock}:00Z</updated>'
                    f'<link href="https://chelsea.example/{ident}"/><content>{body}</content></entry>')
        def pack(*rows):
            return '<feed xmlns="http://www.w3.org/2005/Atom">' + ''.join(rows) + '</feed>'
        absence, available = entry("A", "Player is out"), entry("A", "Player is available")
        stable, new = entry("B", "Another player is absent"), entry("C", "Team update")
        first = run(src, pack(absence, stable))
        expected = next(a for a in first["articles"] if a["external_id"] == "A")
        result = run(src, pack(available, stable, new), first["cursor"])
        conflict, = result["revision_conflicts"]
        self.assertEqual((conflict["external_id"], conflict["revision"], conflict["native_revision_at"]),
                         ("A", expected["revision"], expected["updated_at"]))
        self.assertEqual([r["external_id"] for r in result["articles"]], ["C"])
        repeated = run(src, pack(absence, stable, new), result["cursor"])
        self.assertFalse(repeated["articles"])
        self.assertEqual(repeated["revision_conflicts"], [conflict])
        same_batch = run(src, pack(absence, available, stable))
        self.assertEqual([r["external_id"] for r in same_batch["articles"]], ["B"])
        self.assertEqual(same_batch["revision_conflicts"], [conflict])
        later = run(src, pack(entry("A", "Player is available", "11:00"), stable, new), repeated["cursor"])
        self.assertFalse(later["revision_conflicts"])
        self.assertGreater(later["articles"][0]["revision"], expected["revision"])

        # Previously deployed cursors only knew the content-derived archive ID. A
        # matching observation upgrades them in place without introducing new news.
        legacy = {"binding": first["cursor"]["binding"], "rows": {store.digest("A"): expected["legacy_revision"]}}
        upgraded = run(src, pack(absence), legacy)
        self.assertEqual(upgraded["articles"], [])
        collision = run(src, pack(available), upgraded["cursor"])
        self.assertEqual({c["revision"] for c in collision["revision_conflicts"]},
                         {expected["revision"], expected["legacy_revision"]})

    def test_wiki_same_revision_isolates_affected_paragraph_even_if_introduced_earlier(self):
        source = news.default_sources(CLUB, SEASON)[2]
        def page(revid, body):
            return {"title": source["page_title"], "content_model": "wikitext", "source": "==Team news==\n" + body,
                    "latest": {"id": revid, "timestamp": "2026-11-24T11:00:00Z"},
                    "license": {"url": news.WIKI_LICENSE}}
        joe = "On 24 November, Joe Example was ruled out after an injury during the training session."
        rob = "On 24 November, Rob Example was suspended and will miss the match after his red card."
        body = joe + "\n\n" + rob
        base = run(source, page(99, ""))
        fresh = run(source, page(100, body), base["cursor"])
        self.assertEqual(len(fresh["articles"]), 2)
        target = next(a for a in fresh["articles"] if a["text"] == joe)
        unchanged = run(source, page(101, body), fresh["cursor"])
        collision = run(source, page(101, rob), unchanged["cursor"])
        isolated, = collision["revision_conflicts"]
        self.assertEqual((isolated["external_id"], isolated["revision"]), (target["external_id"], "100"))
        legacy_cursor = {k: v for k, v in unchanged["cursor"].items() if k != "active_versions"}
        old, = run(source, page(101, rob), legacy_cursor)["revision_conflicts"]
        self.assertEqual((old["external_id"], old["revision_through"]), (target["external_id"], 101))
        recovered = run(source, page(102, body), collision["cursor"])
        reissued, = recovered["articles"]
        self.assertEqual((reissued["external_id"], reissued["revision"]), (target["external_id"], "102"))


if __name__ == "__main__":
    unittest.main()
