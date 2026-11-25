import json
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from polymarket861 import news_roster as roster, store


class Wiki:
    """Two separate, provenance-bearing API responses following the recorded Wikimedia shape."""

    def __init__(self, now):
        self.now = now
        self.revision = 81
        self.members = [
            "André Test",
            "Alex Moore (footballer, born 2000)",
            "Alex Moore (footballer, born 2001)",
        ] + [f"First Last{n}" for n in range(8)]
        self.identities = {}
        self.calls = 0
        self.club_header = None

    def __call__(self, url):
        self.calls += 1
        args = parse_qs(urlsplit(url).query)
        self.now += timedelta(seconds=2)
        if args["prop"] == ["revisions"]:
            (title,) = args["titles"]
            club = self.club_header or title.removeprefix("Template:").removesuffix(
                " squad"
            )
            rows = "\n".join(
                f"{{{{football squad2 player|no={i + 1}|name=[[{n}]]}}}}"
                for i, n in enumerate(self.members)
            )
            body = (
                f"{{{{football squad|teamname={club}|list=<div>\n"
                "{{!-- ignore --}}\n" + rows + "\n"
                "<!--{{football squad2 player|no=98|name=[[On Loan]]}}-->\n"
                "{{football squad manager|name=[[Old Coach]]}}\n</div>}}"
            )
            revision = {
                "revid": self.revision,
                "timestamp": "2026-07-17T09:00:00Z",
                "slots": {"main": {"contentmodel": "wikitext", "content": body}},
            }
            pages = [{"ns": 10, "title": title, "revisions": [revision]}]
            result = {
                "query": {"rightsinfo": {"url": roster.wiki.LICENSE}, "pages": pages}
            }
        else:
            pages = []
            for title in args["titles"][0].split("|"):
                pageid = self.identities.setdefault(title, len(self.identities) + 100)
                pages.append(
                    {
                        "title": title,
                        "ns": 0,
                        "pageid": pageid,
                        "pageprops": {"wikibase_item": f"Q{pageid}"},
                    }
                )
            result = {"query": {"pages": pages}}
        return json.dumps(result).encode()


class NewsRoster(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.wiki = Wiki(store.instant("2026-11-20T08:00:00Z"))
        self.clock = patch.object(
            store, "now_utc", side_effect=lambda: store.iso(self.wiki.now)
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def refresh(self, team="Chelsea FC"):
        return roster.refresh(self.root, team, "2026-27", getter=self.wiki)

    def current(self, team="Chelsea FC", at=None):
        return roster.as_of(self.root, team, "2026-27", at or self.wiki.now)

    def test_real_receipt_scopes_first_team_and_stable_player_identity(self):
        first = self.wiki.now
        report = self.refresh()
        self.assertEqual(report["status"], "updated")
        self.assertEqual(self.current(at=first), [])
        rows = self.current()
        self.assertEqual(len(rows), 11)
        self.assertTrue(all(r["seen_at"] >= report["published_at"] for r in rows))
        self.assertTrue(all(r["license"] == roster.wiki.LICENSE for r in rows))
        self.assertEqual(
            set([r["name"] for r in rows]) & {"Old Coach", "On Loan"}, set()
        )
        same_names = [p for p in rows if p["name"] == "Alex Moore"]
        self.assertEqual(len({p["player_id"] for p in same_names}), 2)
        andre = next(p for p in rows if p["name"] == "André Test")
        self.assertIn("Andre Test", andre["aliases"])
        self.assertEqual(
            andre["wikidata_id"], andre["player_id"].replace("enwiki:", "Q")
        )
        for digest in (report["squad_sha256"], *report["identity_sha256"]):
            self.assertTrue(json.loads(store.read_blob(self.root, digest)))
        self.assertEqual(self.refresh()["status"], "cached")
        self.assertEqual(self.wiki.calls, 2)
        self.assertEqual(
            roster.refresh(self.root, "Chelsea FC", "2025-26", getter=self.wiki)[
                "status"
            ],
            "out_of_season",
        )
        self.assertEqual(self.wiki.calls, 2)

    def test_transfer_and_conflicting_club_identity_are_time_versioned(self):
        old = self.refresh()["seen_at"]
        player = self.wiki.members[0]
        original_club = list(self.wiki.members)
        self.wiki.members = [player, *[f"Other Member{n}" for n in range(10)]]
        overlap = self.refresh("Arsenal FC")["seen_at"]
        self.assertNotIn(player, {p["name"] for p in self.current()})
        self.assertNotIn(player, {p["name"] for p in self.current("Arsenal FC")})
        self.assertIn(player, {p["name"] for p in self.current(at=old)})
        self.wiki.now += timedelta(hours=7)
        self.wiki.revision += 1
        self.wiki.members = original_club[1:] + ["New Player"]
        self.refresh()
        self.assertIn(player, {p["name"] for p in self.current("Arsenal FC")})
        self.assertNotIn(
            player, {p["name"] for p in self.current("Arsenal FC", at=overlap)}
        )
        self.assertNotIn(player, {p["name"] for p in self.current()})
        self.assertIn("New Player", {p["name"] for p in self.current()})

    def test_network_failure_backoff_staleness_wrong_team_and_identity_ambiguity(self):
        first = self.refresh()
        self.wiki.now += timedelta(hours=7)
        self.wiki.club_header = "Arsenal F.C."
        result = self.refresh()
        self.assertEqual(result["status"], "error")
        self.assertIn("club", result["error"])
        count = self.wiki.calls
        self.assertEqual(self.refresh()["status"], "error")
        self.assertEqual(self.wiki.calls, count)
        self.assertEqual(len(self.current(at=first["seen_at"])), 11)
        self.wiki.now += timedelta(days=7)
        self.assertFalse(self.current())

        body = json.dumps(
            {
                "query": {
                    "redirects": [{"from": "Alias", "to": "Person"}],
                    "pages": [
                        {
                            "title": "Person",
                            "ns": 0,
                            "pageid": 12,
                            "pageprops": {"disambiguation": ""},
                        }
                    ],
                }
            }
        ).encode()
        self.assertEqual(roster._people(body, {"Alias": {"A"}}), ({}, ["Alias"]))
        with self.assertRaisesRegex(ValueError, "omitted"):
            roster._people(body, {"X": {"X"}})

    def test_mutated_revision_is_withheld_then_fixed_without_backdating(self):
        initial = self.refresh()
        self.wiki.now += timedelta(hours=7)
        self.wiki.members[-1] = "New Arrival"
        bad = self.refresh()
        self.assertEqual(bad["status"], "error")
        self.assertIn("inconsistent", bad["error"])
        self.assertEqual(self.current(), [])
        self.assertEqual(len(self.current(at=initial["seen_at"])), 11)
        self.wiki.now += timedelta(minutes=16)
        self.wiki.revision += 1
        self.assertEqual(self.refresh()["status"], "updated")
        self.assertIn("New Arrival", {p["name"] for p in self.current()})
        self.assertEqual(self.current(at=bad["seen_at"]), [])

    def test_deleted_template_immediately_withdraws_membership(self):
        initial = self.refresh()
        self.wiki.now += timedelta(hours=7)

        def deleted(url):
            (title,) = parse_qs(urlsplit(url).query)["titles"]
            return json.dumps(
                {
                    "query": {
                        "rightsinfo": {"url": roster.wiki.LICENSE},
                        "pages": [{"ns": 10, "title": title, "missing": True}],
                    }
                }
            ).encode()

        report = roster.refresh(self.root, "Chelsea FC", "2026-27", getter=deleted)
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["player_count"], 0)
        self.assertEqual(self.current(), [])
        self.assertEqual(len(self.current(at=initial["seen_at"])), 11)

    def test_position_uses_observed_unambiguous_category_only(self):
        def evidence(title, categories):
            return {
                "title": title,
                "pageid": len(title),
                "ns": 0,
                "categories": [
                    {"ns": 14, "title": "Category:Men's association football " + c}
                    for c in categories
                ],
            }

        pages = [
            evidence("Keeper Name", ["goalkeepers"]),
            evidence("Known Hybrid", ["midfielders", "wingers"]),
            evidence("No Role Claimed", ["coaches"]),
        ]
        raw = json.dumps({"query": {"pages": pages}}).encode()
        rows, missing = roster._people(raw, {p["title"]: set() for p in pages})
        by_name = {p["name"]: p for p in rows.values()}
        self.assertEqual(missing, [])
        self.assertEqual(by_name["Keeper Name"]["position"], "goalkeeper")
        self.assertNotIn("position", by_name["Known Hybrid"])
        self.assertEqual(len(by_name["Known Hybrid"]["position_categories"]), 2)
        self.assertNotIn("position", by_name["No Role Claimed"])

    def test_slow_network_lease_commits_before_fetch(self):
        concurrent = []

        def slow_fetch(url):
            if not concurrent:
                concurrent.append(self.refresh())
            return self.wiki(url)

        result = roster.refresh(self.root, "Chelsea FC", "2026-27", getter=slow_fetch)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(concurrent[0]["status"], "busy")
        self.assertEqual(self.wiki.calls, 2)


if __name__ == "__main__":
    unittest.main()
