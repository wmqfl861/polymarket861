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

    def _schedule(
        self, when, kickoff, *, wait=1, cancelled=False, league="sea", aux=False
    ):
        from types import SimpleNamespace

        root = big5.research_root(self.root)
        event = native(aux, league=league)
        event["startTime"] = kickoff
        event["gameStatus"] = "postponed" if cancelled else None
        for m in event["markets"]:
            m.update(gameStartTime=kickoff, secondsDelay=wait)
        with closing(big5._db(root)) as db, db:
            big5._record(
                db,
                event,
                big5._identity(event, league),
                SimpleNamespace(received_at=iso(when)),
                str(abs(hash((when, kickoff, wait, cancelled)))).zfill(64),
            )
        return root

    def _due(self):
        with patch.object(big5, "now_utc", return_value=iso("2030-01-25T12:00Z")):
            return big5.weekly_report(
                self.root, "2030-01-11T13:59Z", "2030-01-11T14:02Z"
            )["by_league"].get("sea", {})

    def test_schedule_revisions_preserve_matured_windows_after_downtime(self):
        kick = "2030-01-12T14:00Z"
        self._schedule("2030-01-10T00:00Z", kick)
        original = self._due()
        self.assertEqual(
            (original["fixed_windows_due"], original["t24_pairs_due"]), (2, 1)
        )
        self._schedule("2030-01-12T08:00Z", "2030-01-18T14:00Z")
        self.assertEqual(self._due(), original)

    def test_schedule_before_open_late_discovery_and_changed_delay(self):
        kick = "2030-01-12T14:00Z"
        self._schedule("2030-01-10T00:00Z", kick)
        self._schedule("2030-01-11T13:55Z", kick, cancelled=True)
        self.assertEqual(self._due(), {})
        # A restart and a new source receipt after the decision expired only grants the execution observation.
        self._schedule("2030-01-11T14:00:05Z", kick, wait=30)
        report = self._due()
        self.assertEqual(report["fixed_windows_due"], 1)
        self.assertNotIn("t24_pairs_due", report)
        self._schedule("2030-01-11T14:01Z", kick, wait=1)
        self.assertEqual(self._due(), report)

    def test_old_future_replacement_does_not_create_a_due_or_a_pair(self):
        self._schedule("2030-01-10T00:00Z", "2030-01-12T14:00Z")
        self._schedule("2030-01-11T13:59:35Z", "2030-01-18T14:00Z")
        self.assertEqual(self._due(), {})

    def test_late_first_receipt_and_delays_retrieved_after_deadline_are_not_backdated(
        self,
    ):
        kick = "2030-01-12T14:00Z"
        self._schedule("2030-01-11T14:00:10Z", kick)
        self.assertEqual(self._due(), {})
        self._schedule("2030-01-11T14:00:12Z", kick, wait=40, aux=True)
        self.assertEqual(self._due()["fixed_windows_due"], 1)
        self.assertNotIn("t24_pairs_due", self._due())

    def test_equal_deadline_fixtures_rotate_across_scheduling_processes(self):
        kick = provider.moment("2030-01-12T14:00Z")
        for league in ("bun", "sea"):
            root = self._schedule("2030-01-12T13:56Z", iso(kick), league=league)
        served = []
        for offset in range(-119, -48, 12):
            at = kick + timedelta(seconds=offset)
            ranked = big5._ranked_jobs(root, ["bun", "sea"], iso(START), iso(END), at)
            _, _, event, (phase, first, end, _) = ranked[0]
            fixture = event["fixture"]
            served.append(event["sport"])
            key = phase + "@" + iso(first)
            attempt = big5._claim(root, fixture, event["kickoff"], key, end, at)
            big5._finish(
                root,
                fixture,
                event["kickoff"],
                key,
                attempt,
                True,
                iso(at),
                {"state": "complete"},
            )
        self.assertEqual(served, ["bun", "sea"] * 3)

    def test_parent_order_and_rules_cannot_change_across_decision_legs(self):
        from contextlib import ExitStack
        from polymarket861 import store
        from unittest.mock import patch

        cutoff = provider.moment(DAY + "T13:00:00Z")
        for change in ("unchanged", "ids", "rules", "direction"):
            data = self.root / change
            for offset in (-10, 6):
                stamp = iso(cutoff + timedelta(seconds=offset))

                def get(url, params=None, **kwargs):
                    base = gamma(url, params, **kwargs)
                    raw = json.loads(base.body)
                    if offset > 0 and url.endswith("/events"):
                        for e in raw:
                            for c in e["markets"]:
                                if change == "ids":
                                    i = int(c["conditionId"], 16) + 100
                                    c.update(
                                        conditionId="0x" + format(i, "064x"),
                                        clobTokenIds=[str(10 * i), str(10 * i + 1)],
                                    )
                                elif change == "rules":
                                    c["description"] += " replacement clause"
                                elif change == "direction":
                                    c["outcomes"] = list(reversed(c["outcomes"]))
                    return provider.Reply(
                        json.dumps(raw).encode(),
                        base.requested_at,
                        base.received_at,
                        base.headers,
                        base.url,
                    )

                with ExitStack() as stack:
                    for module in (store, big5, archive):
                        stack.enter_context(
                            patch.object(module, "now_utc", return_value=stamp)
                        )
                    stack.enter_context(
                        patch(__name__ + ".now_utc", return_value=stamp)
                    )
                    stack.enter_context(patch.object(provider, "get", side_effect=get))
                    big5.catalog(data, ["sea"], START, END)
                    self.assertEqual(
                        big5.watch(data, ["sea"], START, END, discover=False)["states"],
                        {"complete": 1},
                    )
            with patch.object(
                big5, "now_utc", return_value=iso(cutoff + timedelta(minutes=2))
            ):
                report = big5.weekly_report(
                    data,
                    iso(cutoff - timedelta(minutes=1)),
                    iso(cutoff + timedelta(minutes=1)),
                )
            c = report["by_league"]["sea"]
            self.assertEqual(
                (
                    c["fixed_windows_complete"],
                    c["t1_pairs_due"],
                    c["t1_pairs_complete"],
                ),
                (2, 1, int(change == "unchanged")),
            )

    def test_delayed_book_requires_fresh_specific_domestic_state(self):
        from contextlib import ExitStack
        from polymarket861 import store
        from unittest.mock import patch

        for mode, stale in (("catalog", False), ("event", False), ("catalog", True)):
            dest = self.root / (mode + str(stale))
            kick = provider.moment(DAY + "T14:00Z")
            stamp = iso(kick + timedelta(seconds=3))

            def get(url, params=None, **kwargs):
                r = gamma(url, params, **kwargs)
                h = dict(r.headers)
                if stale and url.endswith("/events"):
                    h["age"] = "600"
                return provider.Reply(r.body, r.requested_at, r.received_at, h, r.url)

            with ExitStack() as stack:
                for module in (store, big5, archive):
                    stack.enter_context(
                        patch.object(module, "now_utc", return_value=stamp)
                    )
                stack.enter_context(patch(__name__ + ".now_utc", return_value=stamp))
                stack.enter_context(patch.object(provider, "get", side_effect=get))
                big5.catalog(dest, ["sea"], START, END)
                r = big5._sample(
                    big5.research_root(dest),
                    BASE,
                    all_lines=False,
                    phase="test",
                    refresh=mode == "event",
                )
            if stale:
                self.assertEqual(r["requested_tokens"], 0)
                self.assertTrue(any("unverifiable" in gap["error"] for gap in r["event_gaps"]))
            else:
                self.assertIs(r["contracts"]["moneyline.home"]["tokens"]["Yes"]["pregame"], True)

    def test_legacy_schedule_recovered_from_each_original_and_missing_original_stays_unknown(
        self,
    ):
        from types import SimpleNamespace
        from polymarket861.store import archive_bytes, canonical

        for present in (True, False):
            data = self.root / str(present)
            root = big5.research_root(data)
            for stamp, kick in (
                ("2030-01-10T00:00Z", "2030-01-12T14:00Z"),
                ("2030-01-12T08:00Z", "2030-01-18T14:00Z"),
            ):
                e = native()
                e["startTime"] = kick
                for c in e["markets"]:
                    c["gameStartTime"] = kick
                raw = canonical(e).encode()
                sha = (
                    archive_bytes(root, "polymarket.com", raw, observed_at=stamp)[
                        "sha256"
                    ]
                    if present
                    else "0" * 64
                )
                with closing(big5._db(root)) as db, db:
                    big5._record(
                        db,
                        e,
                        big5._identity(e, "sea"),
                        SimpleNamespace(received_at=iso(stamp)),
                        sha,
                    )
            # Emulate a v1 installation retaining the mutable current index and immutable originals only.
            with closing(big5._db(root)) as db, db:
                db.execute("DELETE FROM big5_schedule_receipts")
                db.execute("DELETE FROM big5_schedule_recovery")
            with patch.object(big5, "now_utc", return_value=iso("2030-01-25T00:00Z")):
                r = big5.weekly_report(data, "2030-01-11T13:59Z", "2030-01-11T14:02Z")
            if present:
                self.assertEqual(r["by_league"]["sea"]["fixed_windows_due"], 2)
                self.assertEqual(
                    r["schedule_receipt_origins_before_report_end"],
                    {"recovered_original": 1},
                )
            else:
                self.assertEqual(r["by_league"], {})
                self.assertEqual(
                    r["schedule_receipt_origins_before_report_end"],
                    {"legacy_unverified": 1},
                )

    def test_missing_time_in_cancelled_receipt_closes_only_its_existing_schedule(self):
        from types import SimpleNamespace

        root = self._schedule("2030-01-10T00:00Z", "2030-01-12T14:00Z")
        e = native()
        e.pop("startTime")
        e["gameStatus"] = "Cancelled"
        with closing(big5._db(root)) as db, db:
            fields = big5._identity(e, "sea", history=db)
            big5._record(
                db,
                e,
                fields,
                SimpleNamespace(received_at=iso("2030-01-11T13:00Z")),
                "d" * 64,
            )
            with self.assertRaisesRegex(ValueError, "missing_scheduled"):
                big5._identity(e | {"id": "987654321"}, "sea", history=db)
        self.assertEqual(self._due(), {})
