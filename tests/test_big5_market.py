import copy
import json
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from polymarket861 import (
    big5_market as big5,
    market_archive as archive,
    market_provider as provider,
)
from polymarket861.store import iso, now_utc


DAY = "2030-01-12"
BASE = "sea-one-two-" + DAY
START = DAY + "T00:00:00Z"
END = "2030-01-13T00:00:00Z"


def native(aux=False, *, league="sea"):
    series, tag = big5.MARKETS[league]
    base = BASE.replace("sea-", league + "-")
    teams = [
        {
            "name": "One FC",
            "ordering": "home",
            "abbreviation": "one",
            "id": 21,
            "league": league,
        },
        {
            "name": "Two FC",
            "ordering": "away",
            "abbreviation": "two",
            "id": 22,
            "league": league,
        },
    ]
    if aux:
        lines = (
            (4, "total-2pt5", "totals", "O/U 2.5", 2.5),
            (5, "team-total-home-1pt5", "soccer_team_totals", "One FC O/U 1.5", 1.5),
            (6, "team-total-away-1pt5", "soccer_team_totals", "Two FC O/U 1.5", 1.5),
            (7, "total-3pt5", "totals", "O/U 3.5", 3.5),
            (8, "total-3", "totals", "O/U 3", 3),
        )
    else:
        lines = (
            (1, "one", "moneyline", "One FC", None),
            (2, "draw", "moneyline", "Draw", None),
            (3, "two", "moneyline", "Two FC", None),
        )
    markets = []
    for i, suffix, kind, title, line in lines:
        markets.append(
            {
                "slug": base + "-" + suffix,
                "sportsMarketType": kind,
                "groupItemTitle": title,
                "conditionId": "0x" + format(i, "064x"),
                "clobTokenIds": [str(i * 10), str(i * 10 + 1)],
                "version": "v1",
                "outcomes": ["Yes", "No"] if not aux else ["Over", "Under"],
                "line": line,
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "enableOrderBook": True,
                "description": "unreviewed fixture rules",
                "feesEnabled": True,
                "feeSchedule": {"rate": 0.03},
                "secondsDelay": 1,
                "gameStartTime": DAY + "T14:00:00Z",
            }
        )
    return {
        "id": "11" if aux else "10",
        "slug": base + ("-more-markets" if aux else ""),
        "startTime": DAY + "T14:00:00Z",
        "title": "One FC vs. Two FC",
        "sport": {"sport": league, "series": series},
        "series": [{"id": series}],
        "tags": [{"id": tag}],
        "teams": teams,
        "markets": markets,
        "live": False,
        "ended": False,
    }


def gamma(url, params=None, **kwargs):
    params = params or {}
    now = now_utc()
    if url.endswith("/sports"):
        value = [
            {
                "sport": "sea",
                "series": "10203",
                "tags": "101962,100618",
                "primaryTagId": 100618,
            }
        ]
    elif url.endswith("/events"):
        assert str(params["tag_id"]) == "101962"
        value = [native(), native(True)][
            params["offset"] : params["offset"] + params["limit"]
        ]
    elif "/events/slug/" in url:
        value = native(url.endswith("-more-markets"))
    elif url.endswith("/books"):
        value = [
            {
                "asset_id": token,
                "market": "0x" + format(int(token) // 10, "064x"),
                "bids": [{"price": "0.3", "size": "10"}],
                "asks": [
                    {"price": "0.4", "size": "50"},
                    {"price": "0.6", "size": "50"},
                ],
                "timestamp": str(int(provider.moment(now).timestamp() * 1000)),
            }
            for token in params["token_ids"]
        ]
    else:
        raise AssertionError(url)
    return provider.Reply(
        json.dumps(value).encode(),
        now,
        now,
        {"date": format_datetime(provider.moment(now))},
        url
        + ("?" + urlencode(params) if params and not url.endswith("/books") else ""),
    )


class TestDomesticMarket(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_all_five_domestic_identity_and_epl_default(self):
        for league in big5.MARKETS:
            event = native(league=league)
            self.assertEqual(big5._identity(event, league)[2], league)
            self.assertEqual(bool(provider.normalized_markets(event)), league == "epl")
            self.assertEqual(len(provider.normalized_markets(event, sport=league)), 3)
        sea = native()
        for key, value in (
            ("series", [{"id": "999"}]),
            ("tags", [{"id": "100618"}]),
            ("sport", {"sport": "lal", "series": "10203"}),
        ):
            with self.assertRaises(ValueError):
                big5._identity(sea | {key: value}, "sea")
        for mutation in (
            {"league": "bun"},
            {"ordering": "away"},
            {"id": 22},
            {"abbreviation": "other"},
        ):
            bad = copy.deepcopy(sea)
            bad["teams"][0].update(mutation)
            with self.assertRaises(ValueError):
                big5._identity(bad, "sea")
        with closing(archive.database(self.root)) as db:
            self.assertFalse(archive._ingest(db, sea, None, ""))
        with self.assertRaises(ValueError):
            archive.discover(self.root, BASE)

    @patch.object(provider, "get", side_effect=gamma)
    def test_real_entry_points_both_sides_source_hash_and_denominator(self, _):
        result = big5.collect(self.root, ["sea"], START, END, mode="core", page_size=2)
        league = result["leagues"]["sea"]
        self.assertTrue(league["page_walk_complete"])
        self.assertEqual([p["rows"] for p in league["pages"]], [2, 0])
        self.assertEqual(league["primary_tag"], "100618")
        self.assertEqual(league["tag"], "101962")
        self.assertFalse((self.root / "runtime.sqlite3").exists())
        (sample,) = league["observations"]
        self.assertEqual(sample["core_missing"], [])
        self.assertEqual(set(sample["contracts"]), set(big5.CORE))
        self.assertEqual(
            [
                sample[k]
                for k in (
                    "requested_tokens",
                    "received_tokens",
                    "two_sided_tokens",
                    "fresh_tokens",
                )
            ],
            [12] * 4,
        )
        self.assertEqual(sample["excluded_contracts"]["non_half_goal_line"], 1)
        home = sample["contracts"]["home_total.1.5"]
        self.assertEqual(set(home["tokens"]), {"Over", "Under"})
        self.assertEqual(home["fee_schedule"]["rate"], 0.03)
        self.assertIn("unreviewed", home["rule_profile"])
        over = home["tokens"]["Over"]
        self.assertEqual(
            over["depth_50"], {"levels": 2, "vwap": 0.5, "above_best_ask": 0.1}
        )
        self.assertEqual(len(home["rule_sha256"]), 64)
        self.assertEqual(len(over["source_sha256"]), 64)
        summary = big5.weekly_report(
            self.root, "2000-01-01T00:00:00Z", "2099-01-01T00:00:00Z"
        )
        self.assertEqual(
            summary["by_league_family"]["sea"]["moneyline"][
                "expected_token_observations"
            ],
            6,
        )
        self.assertGreater(summary["raw_unique_on_disk_bytes"], 0)
        full = big5.collect(self.root, ["sea"], START, END, mode="all", page_size=2)
        self.assertEqual(
            full["leagues"]["sea"]["observations"][0]["received_tokens"], 14
        )

    @patch.object(provider, "get", side_effect=gamma)
    def test_incomplete_pagination_metadata_and_missing_side(self, _):
        partial = big5.catalog(
            self.root, ["sea", "lal"], START, END, pages=1, page_size=1
        )
        self.assertEqual(partial["leagues"]["sea"]["next_offset"], 1)
        self.assertFalse(partial["leagues"]["sea"]["page_walk_complete"])
        tail = big5.catalog(
            self.root, ["sea"], START, END, pages=1, page_size=20, offset=1
        )["leagues"]["sea"]
        self.assertTrue(tail["reached_end"])
        self.assertFalse(tail["page_walk_complete"])
        self.assertIsNone(tail["next_offset"])
        self.assertEqual(partial["leagues"]["lal"]["fixtures"], [])
        self.assertTrue(partial["leagues"]["lal"]["gaps"])

        def missing(url, params=None, **kwargs):
            reply = gamma(url, params, **kwargs)
            if url.endswith("/books"):
                reply = provider.Reply(
                    json.dumps(json.loads(reply.body)[1:]).encode(),
                    reply.requested_at,
                    reply.received_at,
                    reply.headers,
                    reply.url,
                )
            return reply

        with patch.object(provider, "get", side_effect=missing):
            item = big5.collect(self.root, ["sea"], START, END, mode="core")["leagues"][
                "sea"
            ]["observations"][0]
        self.assertEqual(item["requested_tokens"], 12)
        self.assertEqual(item["received_tokens"], 11)
        self.assertEqual(
            sum(
                "error" in v
                for c in item["contracts"].values()
                for v in c["tokens"].values()
            ),
            1,
        )

    def test_distinct_conditions_and_tokens_or_reject_ambiguous_slots(self):
        events = native(), native(True)
        contracts = sum(
            (provider.normalized_markets(event, sport="sea") for event in events), []
        )
        eligible, errors = big5._selection(contracts, True)
        self.assertEqual(len(eligible), 7)
        self.assertEqual(errors["non_half_goal_line"], 1)
        conflicting = copy.deepcopy(contracts)
        conflicting[0]["tokens"][0]["token_id"] = conflicting[-1]["tokens"][0][
            "token_id"
        ]
        conflicting[1]["game_times"].append(END)
        valid, issues = big5._selection(conflicting, False)
        self.assertNotIn("moneyline.home", valid)
        self.assertNotIn("moneyline.draw", valid)
        self.assertEqual(issues["ambiguous_contract_kickoff"], 1)
        contracts[0]["tokens"][0]["token_id"] = contracts[1]["tokens"][0]["token_id"]
        eligible, errors = big5._selection(contracts, False)
        self.assertNotIn("moneyline.home", eligible)
        self.assertNotIn("moneyline.draw", eligible)
        self.assertEqual(errors["ambiguous_slot_or_identifier"], 2)
        self.assertEqual(
            big5.depth_50([{"price": ".31", "size": "15"}]),
            {"levels": None, "available_usd": 4.65},
        )

    def test_live_slots_expiry_and_concurrent_lease(self):
        kick = provider.moment(DAY + "T14:00:00Z")
        d = kick - timedelta(hours=24)
        self.assertEqual(big5._slot(kick, d - timedelta(seconds=25))[0], "t24_decision")
        self.assertIsNone(big5._slot(kick, d))
        self.assertEqual(big5._slot(kick, d + timedelta(seconds=5))[0], "t24_execution")
        self.assertEqual(
            big5._slot(kick, d + timedelta(seconds=8), (8,))[0], "t24_execution_8s"
        )
        self.assertEqual(
            big5._slot(kick, kick - timedelta(minutes=2))[0::3], ("closing", True)
        )
        self.assertFalse(big5._slot(kick, kick - timedelta(seconds=100))[-1])
        self.assertIsNone(big5._slot(kick, kick + timedelta(seconds=600)))
        root = big5.research_root(self.root)
        end = d + timedelta(seconds=10)
        args = (root, BASE, iso(kick), "decision")
        self.assertEqual(big5._claim(*args, end, d), 1)
        self.assertIsNone(big5._claim(*args, end, d))
        big5._finish(*args, 1, False, iso(d), {"state": "partial"})
        self.assertEqual(big5._claim(*args, end, d), 2)
        big5._finish(*args, 1, True, iso(d), {"state": "obsolete"})
        with closing(big5._db(root)) as db:
            self.assertFalse(db.execute("SELECT success FROM big5_slots").fetchone()[0])
        big5._finish(*args, 2, True, iso(d), {"state": "complete"})
        self.assertIsNone(big5._claim(*args, end, d))

    @patch.object(provider, "get", side_effect=gamma)
    def test_stale_catalog_and_identity_changes_are_quarantined(self, _):
        big5.catalog(self.root, ["sea"], START, END)
        root = big5.research_root(self.root)
        with closing(big5._db(root)) as db, db:
            db.execute("UPDATE big5_events SET receipt='2000-01-01T00:00:00Z'")
            replaced = native() | {"id": "111"}
            with self.assertRaisesRegex(ValueError, "changed"):
                big5._record(db, replaced, big5._identity(replaced, "sea"), None, "")
        report = big5._sample(
            root, BASE, refresh=False, phase="periodic", all_lines=False
        )
        self.assertEqual(report["requested_tokens"], 0)
        self.assertEqual(len(report["event_gaps"]), 2)
        with self.assertRaisesRegex(ValueError, "future"):
            big5.terminal(self.root, BASE)

    def test_foreground_restart_and_unobserved_decisions_in_weekly_denominator(self):
        with patch.object(provider, "get", side_effect=gamma):
            big5.catalog(self.root, ["sea"], START, END)
            first = big5.watch(
                self.root, ["sea"], START, END, discover=False, workers=1
            )
            second = big5.watch(
                self.root, ["sea"], START, END, discover=False, workers=1
            )
        self.assertEqual(first["states"], {"complete": 1})
        self.assertEqual(second["states"], {})
        with patch.object(big5, "now_utc", return_value="2030-01-13T04:00:00.000000Z"):
            week = big5.weekly_report(self.root, "2030-01-11T00:00:00Z", END)
        self.assertEqual(week["by_league"]["sea"]["fixed_windows_due"], 4)
        self.assertEqual(week["by_league"]["sea"]["fixed_windows_complete"], 0)
        self.assertEqual(week["by_league"]["sea"]["t24_pairs_due"], 1)
        self.assertEqual(week["by_league"]["sea"]["t1_pairs_complete"], 0)
        self.assertEqual(week["by_league"]["sea"]["closing_slots_due"], 60)
        self.assertEqual(week["by_league"]["sea"]["closing_slots_complete"], 0)
        self.assertEqual(week["observed_http_status_errors"]["429"], 0)

    def test_real_token_pair_terminal_and_wrong_identity_remains_pending(self):
        past_day = "2020-01-12"
        past_fixture = BASE.replace(DAY, past_day)
        past_events = [
            json.loads(json.dumps(native(aux)).replace(DAY, past_day))
            for aux in (False, True)
        ]
        by_token = {
            t: (c, i)
            for event in past_events
            for c in provider.normalized_markets(event, sport="sea")
            for i, t in enumerate(t["token_id"] for t in c["tokens"])
        }

        def serve(url, params=None, **kwargs):
            if url.startswith(provider.DATA):
                token = params["token_id"]
                contract, index = by_token[token]
                # One officially pending side, plus a different condition incorrectly claiming a winner.
                pending = token == "11"
                condition = (
                    contract["condition_id"] if token != "21" else "0x" + "a" * 64
                )
                row = {
                    "token_id": token,
                    "final_price": None if pending else "0.5",
                    "resolved": not pending,
                    "closed": not pending,
                    "condition_id": condition,
                    "outcome_index": index,
                    "market_slug": contract["slug"],
                    "event_slug": contract["event_slug"],
                    "opposite_token_id": contract["tokens"][1 - index]["token_id"],
                    "outcome": contract["tokens"][index]["outcome"],
                }
                now = now_utc()
                return provider.Reply(
                    json.dumps({"data": [row]}).encode(),
                    now,
                    now,
                    {"date": format_datetime(provider.moment(now))},
                    url + "?" + urlencode(params),
                )
            r = gamma(url, params, **kwargs)
            return provider.Reply(
                r.body.replace(DAY.encode(), past_day.encode()),
                r.requested_at,
                r.received_at,
                r.headers,
                r.url,
            )

        with patch.object(provider, "get", side_effect=serve):
            big5.catalog(
                self.root,
                ["sea"],
                START.replace("2030", "2020"),
                END.replace("2030", "2020"),
            )
            report = big5.terminal(self.root, past_fixture)
        self.assertFalse(report["terminal_complete"])
        self.assertEqual(report["contracts"]["moneyline.home"]["status"], "pending")
        self.assertEqual(report["contracts"]["moneyline.draw"]["status"], "pending")
        total = report["contracts"]["total.2.5"]
        self.assertEqual(total["status"], "verified_exchange_vector")
        self.assertEqual(
            [provider.number(t["payout"]) for t in total["tokens"].values()], [0.5, 0.5]
        )
        self.assertTrue(all(len(t["sha256"]) == 64 for t in total["tokens"].values()))
