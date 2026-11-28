import gzip
from copy import deepcopy
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import httpx

from polymarket861.nba import data
from polymarket861.nba import markets as m
from polymarket861.nba.markets_contracts import (
    contract,
    contracts,
    fixture,
    eligibility,
    rule_times,
    price,
    stamp,
)


# Captured venue wording, MIA @ NYK, Gamma event 1377575 (2026-11-27).
SAMPLE_RULES = {
    "team_totals": (
        "In the upcoming NBA game between Heat and Knicks, originally scheduled for Friday, "
        "November 27, 2026 at 7:00 PM ET (as of creation of the first market related to the "
        'event): This market will resolve to "Over" if the {team} score {n} or more points in '
        "this game. If the {team} score less than {n} points in this game, this market will "
        'resolve to "Under". Only points scored by the {team} count toward this market. The '
        "entire game including all overtime periods will be considered. If this market's "
        "outcome was definitively determined under its rules by the listed resolution sources "
        "before abandonment, that outcome will stand. Otherwise, the following no-result "
        "provisions apply. If the game is canceled, does not start by Sunday, November 29, "
        "2026, 11:59 PM ET, or is abandoned with no official result by that time, this market "
        "will resolve 50-50. "
    ),
    "totals": (
        "In the upcoming NBA game between Heat and Knicks, originally scheduled for Friday, "
        "November 27, 2026 at 7:00 PM ET (as of creation of the first market related to the "
        'event): This market will resolve to "Over" if the Heat and Knicks combine to score {n} '
        "or more points in this game. If the combined total is less than {n}, this market will "
        'resolve to "Under". If this market\'s outcome was definitively determined under its '
        "rules by the listed resolution sources before abandonment, that outcome will stand. "
        "Otherwise, the following no-result provisions apply. If the game is canceled, does not "
        "start by Sunday, November 29, 2026, 11:59 PM ET, or is abandoned with no official "
        "result by that time, this market will resolve 50-50. "
    ),
    "moneyline": (
        "In the upcoming NBA game between Heat and Knicks, originally scheduled for Friday, "
        "November 27, 2026 at 7:00 PM ET (as of creation of the first market related to the "
        'event): If the Heat win, the market will resolve to "Heat". If the Knicks win, the '
        'market will resolve to "Knicks". If this market\'s outcome was definitively determined '
        "under its rules by the listed resolution sources before abandonment, that outcome will "
        "stand. Otherwise, the following no-result provisions apply. If the game is canceled, "
        "does not start by Sunday, November 29, 2026, 11:59 PM ET, or is abandoned with no "
        "official result by that time, this market will resolve 50-50. The result will be "
        "determined based on the final score including any overtime periods. "
    ),
}


class Venue:
    def __init__(self):
        self.tip = datetime(2026, 11, 28, tzinfo=UTC)
        self.now = self.tip - timedelta(minutes=63)
        self.slug = "nba-mia-nyk-2026-11-27"
        self.event = {
            "id": "48",
            "gameId": "123",
            "active": True,
            "archived": False,
            "closed": False,
            "slug": self.slug,
            "startTime": stamp(self.tip),
            "teams": [
                {
                    "ordering": s,
                    "name": name,
                    "league": "nba",
                    "abbreviation": ab,
                    "id": i,
                }
                for s, name, ab, i in [
                    ("away", "Heat", "mia", 4),
                    ("home", "Knicks", "nyk", 2),
                ]
            ],
            "series": [{"id": "10345"}],
            "tags": [{"id": "100639"}],
            "period": "NS",
            "markets": [],
        }
        for i, (family, line, cents) in enumerate(
            [
                ("win", None, 63),
                ("total", 218.5, 85),
                ("total", 223.5, 52),
                ("home_total", 113.5, 51),
                ("away_total", 109.5, 48),
            ],
            start=1,
        ):
            is_win = family == "win"
            side = "home" if family == "home_total" else "away"
            team, code = ("Knicks", "nyk") if side == "home" else ("Heat", "mia")
            rule = SAMPLE_RULES["moneyline"]
            slug = self.slug
            if not is_win:
                n = int(line) + 1
                rule = SAMPLE_RULES["totals"].format(n=n)
                suffix = "-total-"
                if family != "total":
                    rule = SAMPLE_RULES["team_totals"].format(n=n, team=team)
                    suffix = "-team-total-" + code + "-"
                slug += suffix + str(line).replace(".", "pt")
            meta = {
                "line_family": "full.total"
                if family == "total"
                else "full.team_total." + side,
                "line_kind": "consensus",
            }
            labels = ["Heat", "Knicks"] if is_win else ["Under", "Over"]
            self.event["markets"].append(
                {
                    "id": str(i),
                    "conditionId": f"0x{i:064x}",
                    "clobTokenIds": json.dumps([str(i * 10), str(i * 10 + 1)]),
                    "outcomes": json.dumps(labels),
                    "description": rule,
                    "line": line,
                    "slug": slug,
                    "gameStartTime": stamp(self.tip),
                    "marketMetadata": None if is_win else meta,
                    "active": True,
                    "enableOrderBook": True,
                    "closed": False,
                    "acceptingOrders": True,
                    "sportsMarketType": "moneyline"
                    if is_win
                    else ("totals" if family == "total" else "team_totals"),
                    "test_cents": cents,
                }
            )
        self.empty = set()
        self.bad = set()
        self.late = False
        self.rate_limited = False
        self.stale = False
        self.calls = []

    def clock(self):
        return self.now

    def get(self, url, params, *, timeout):
        self.calls.append((url, params))
        received = self.now
        if self.rate_limited:
            raise HTTPError(url, 429, "limited", {"Retry-After": "180"}, None)
        if url.endswith("/events/keyset"):
            payload = {
                "events": [
                    self.event,
                    {**self.event, "slug": self.slug + "-exact-margin"},
                ]
            }
        elif "/events/" in url:
            payload = self.event
        elif url.endswith("/v2/tokens"):
            t = int(params["token_id"])
            payload = {
                "data": [
                    {
                        "token_id": str(t),
                        "condition_id": f"0x{t // 10:064x}",
                        "resolved": True,
                        "final_price": 0.5,
                    }
                ]
            }
        else:
            assert url == m.api.CLOB + "/books"
            payload = []
            for market in self.event["markets"]:
                for idx, t in enumerate(json.loads(market["clobTokenIds"])):
                    if t not in params["token_ids"]:
                        continue
                    mid = (
                        market["test_cents"] if idx == 1 else 100 - market["test_cents"]
                    )
                    payload.append(
                        {
                            "asset_id": t,
                            "market": "mismatch"
                            if t in self.bad
                            else market["conditionId"],
                            "bids": [{"price": (mid - 1) / 100, "size": "20"}],
                            "asks": []
                            if t in self.empty
                            else [{"price": (mid + 1) / 100, "size": "20"}],
                            "timestamp": int(self.now.timestamp() * 1000),
                        }
                    )
            payload.reverse()  # response array order never determines the side
            if self.late:
                self.now += timedelta(minutes=5)
        return m.api.Reply(
            json.dumps(payload).encode(),
            stamp(received),
            stamp(received),
            {
                "date": format_datetime(received, usegmt=True),
                "age": "90" if self.stale else "0",
            },
            url,
        )


class TestNBAMarkets(unittest.TestCase):
    def setUp(self):
        self.scope = tempfile.TemporaryDirectory()
        self.addCleanup(self.scope.cleanup)
        self.root = Path(self.scope.name) / "nba"
        self.venue = Venue()
        p = patch.object(m.api, "get", self.venue.get)
        p.start()
        self.addCleanup(p.stop)

    def tick(self):
        return m.collect(self.root, now=self.venue.clock)

    def ref(self, **args):
        args.setdefault("as_of", stamp(self.venue.now))
        return m.benchmark(
            self.root, "NYK", "MIA", stamp(self.venue.tip), "T-60m", **args
        )

    def test_choices_canonical_immutable_roles_close_settlement(self):
        self.assertEqual(self.tick()["state"], "ok")
        r = self.ref()
        self.assertEqual(set(r), set(m.FAMILIES))
        self.assertEqual((r["win"]["token_id"], r["win"]["probability"]), ("11", 0.63))
        self.assertEqual((r["total"]["line"], r["total"]["probability"]), (223.5, 0.52))
        self.assertFalse(self.ref(as_of=stamp(self.venue.now - timedelta(seconds=1))))
        self.assertFalse(
            m.benchmark(self.root, "MIA", "NYK", stamp(self.venue.tip), "T-60m")
        )
        n = len(self.venue.calls)
        self.tick()
        self.assertEqual(len(self.venue.calls), n)
        self.venue.now = self.venue.tip - timedelta(minutes=2)
        self.venue.event["markets"][2]["test_cents"] = 64
        self.tick()
        self.assertIsNone(
            self.ref(as_of=stamp(self.venue.now))["total"]["close_probability"]
        )
        self.venue.now = self.venue.tip + timedelta(hours=3)
        for market in self.venue.event["markets"]:
            market.update(
                closed=True,
                umaResolutionStatus="resolved",
                outcomePrices='["0.5","0.5"]',
            )
        self.venue.event["ended"] = True
        self.assertEqual(self.tick()["state"], "ok")
        self.assertEqual(
            self.ref(as_of=stamp(self.venue.now))["total"]["close_probability"], 0.64
        )
        day = m.status(self.root, now=self.venue.clock)["days"]["2026-11-27"]
        self.assertEqual(
            (day["due"], day["collected"], day["missed"], day["settled_conditions"]),
            (12, 4, 8, 4),
        )
        with closing(m.storage.connect(self.root)) as db:
            for sha, compressed in db.execute("SELECT * FROM nba_market_payloads"):
                self.assertEqual(
                    hashlib.sha256(gzip.decompress(compressed)).hexdigest(), sha
                )

    def test_empty_mismatch_ambiguous_total_rules_and_wrong_family(self):
        self.venue.bad.add("11")
        self.venue.empty.add("51")
        self.venue.event["markets"][3]["description"] = (
            "Over if 114 or more. Less than 114 for the Heat."
        )
        self.venue.event["markets"].append(
            {
                **self.venue.event["markets"][2],
                "sportsMarketType": "spreads",
                "clobTokenIds": '["80", "81"]',
            }
        )
        self.tick()
        self.assertEqual(set(self.ref()), {"total"})
        self.venue.now += timedelta(minutes=4)
        stats = m.status(self.root, now=self.venue.clock)["days"]["2026-11-27"]
        self.assertEqual(
            [
                stats[k]
                for k in [
                    "invalid_book",
                    "empty_book",
                    "rule_template_mismatch",
                    "collected",
                ]
            ],
            [1, 1, 1, 1],
        )

    def test_changed_kickoff_does_not_reuse_window_when_time_changes_back(self):
        self.tick()
        self.assertTrue(self.ref())
        self.venue.now += timedelta(minutes=16)
        self.venue.event["startTime"] = stamp(self.venue.tip + timedelta(hours=2))
        self.tick()
        self.assertFalse(self.ref())
        self.venue.now += timedelta(minutes=16)
        self.venue.event["startTime"] = stamp(self.venue.tip)
        self.tick()
        self.assertFalse(self.ref())

    def test_network_and_late_arrivals_cannot_manufacture_window(self):
        self.venue.late = True
        self.assertEqual(self.tick()["state"], "degraded")
        self.assertFalse(self.ref())
        self.assertIn(
            "outside decision window",
            str(m.status(self.root, now=self.venue.clock)["last_errors"]),
        )

    def test_stale_event_live_event_and_rate_budget(self):
        self.venue.stale = True
        self.assertEqual(self.tick()["state"], "degraded")
        self.venue.stale = False
        self.venue.event["live"] = True
        self.tick()
        self.assertFalse(self.ref())
        self.venue.now += timedelta(minutes=16)
        self.venue.rate_limited = True
        self.assertEqual(self.tick()["state"], "degraded")
        n = len(self.venue.calls)
        self.tick()
        self.assertEqual(len(self.venue.calls), n)
        self.assertEqual(
            m.status(self.root, now=self.venue.clock)["days"]["2026-11-27"]["http_429"],
            1,
        )

    def test_duplicates_spreads_mismatched_teams_and_wrong_series(self):
        e = self.venue.event
        g = fixture(e)
        with self.assertRaises(ValueError):
            contract(e, g, {**e["markets"][0], "sportsMarketType": "spreads"})
        with self.assertRaises(ValueError):
            fixture({**e, "series": [{"id": "99"}]})
        with self.assertRaises(ValueError):
            fixture(
                {
                    **e,
                    "teams": list(
                        reversed([dict(t, ordering="home") for t in e["teams"]])
                    ),
                }
            )
        e["markets"].append(dict(e["markets"][0]))
        cs = contracts(e, g)
        self.assertNotIn("win", [c["family"] for c in cs])
        self.assertIsNone(price(cs[0], {}, self.venue.now))

    def test_contradictory_rules_never_enter_a_benchmark(self):
        event = self.venue.event
        game = fixture(event)
        challenges = [
            (
                0,
                "including any overtime periods",
                "in regulation only, excluding overtime",
            ),
            (
                0,
                'the Heat win, the market will resolve to "Heat"',
                'the Heat win, the market will resolve to "Knicks"',
            ),
            (2, '"Over"', '"Under"'),
            (2, "less than 224", "less than 223"),
            (2, "combine to score", "in the first half combine to score"),
            (3, "The entire game including all overtime periods", "The first half"),
            (3, "Only points scored by the Knicks", "Only points scored by the Heat"),
        ]
        for idx, old, replacement in challenges:
            market = dict(event["markets"][idx])
            raw = " ".join(market["description"].split())
            self.assertIn(old, raw)
            market["description"] = raw.replace(old, replacement)
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                contract(event, game, market)
        for market in event["markets"]:
            with self.assertRaises(ValueError):
                contract(
                    event,
                    game,
                    {
                        **market,
                        "description": market["description"] + " Excludes overtime.",
                    },
                )

    def test_two_sources_pregame_conflict_uses_only_row_versions_known_by_cutoff(self):
        def reason(tip, as_of):
            return m.fixture_conflict(
                self.root, "NYK", "MIA", stamp(tip), as_of=stamp(as_of)
            )

        v = self.venue
        original = v.now
        self.assertIsNone(reason(v.tip, original))
        self.tick()
        self.assertIsNone(reason(v.tip, original))
        self.assertIsNone(reason(v.tip + timedelta(minutes=20), original))
        self.assertEqual(
            m.schedule_match(
                self.root, "NYK", "MIA", v.tip + timedelta(minutes=20), as_of=original
            )["status"],
            "within_tolerance",
        )
        v.now += timedelta(minutes=16)
        v.event["startTime"] = stamp(v.tip + timedelta(hours=1))
        self.tick()
        self.assertEqual(reason(v.tip, v.now), "venue_schedule_replaced")
        self.assertIsNone(reason(v.tip, original))
        v.now += timedelta(minutes=16)
        v.event["live"] = True
        self.tick()
        self.assertEqual(
            reason(v.tip + timedelta(hours=1), v.now),
            "venue_play_or_non_pregame_period_observed",
        )
        self.assertIsNone(
            reason(v.tip + timedelta(hours=1), v.now - timedelta(minutes=1))
        )

    def test_tied_midpoint_uses_fixed_condition_id(self):
        v = self.venue
        e = v.event
        for idx in (1, 2):
            e["markets"][idx]["test_cents"] = 50
        # The older/lower condition has wider spread, less depth and a higher market id.
        first = e["markets"][1]
        first["id"] = "900"
        second = e["markets"][2]
        for market, gap in ((first, 3), (second, 1)):
            c = contract(e, fixture(e), market)
            books = {
                t: dict(
                    asset_id=t,
                    market=c["condition_id"],
                    timestamp=int(v.now.timestamp() * 1000),
                    bids=[dict(price=(50 - gap) / 100, size=1 / gap)],
                    asks=[dict(price=(50 + gap) / 100, size=1 / gap)],
                )
                for t in (c["token_id"], c["opposite"])
            }
            rank, probability = price(c, books, v.now)
            self.assertEqual(probability, 0.5)
            self.assertEqual(rank, (0, c["condition_id"]))
        self.tick()
        self.assertEqual(self.ref()["total"]["condition_id"], first["conditionId"])

    def _bdl_live(self, observed, game_id=704):
        self.venue.now = observed
        raw = {
            "id": game_id,
            "season": 2026,
            "date": "2026-11-27",
            "datetime": stamp(self.venue.tip),
            "status_state": "in_progress",
            "status": "1st Quarter",
            "period": 1,
            "home_team_score": 2,
            "visitor_team_score": 0,
            "postseason": False,
            "home_team": {
                "id": 2,
                "abbreviation": "NYK",
                "full_name": "New York Knicks",
            },
            "visitor_team": {"id": 4, "abbreviation": "MIA", "full_name": "Miami Heat"},
        }

        def get(params, _key):
            return httpx.Response(
                200,
                json={
                    "data": [raw] if params["season_type"] == "regular" else [],
                    "meta": {"next_cursor": None},
                },
            )

        def advance(seconds):
            self.venue.now += timedelta(seconds=seconds)

        with patch.dict(
            os.environ, {"BALLDONTLIE_API_KEY": "synthetic-nba-regression-key"}
        ):
            result = data.sync_bdl(
                self.root,
                "2026-11-27",
                "2026-11-27",
                fetch=get,
                clock=self.venue.clock,
                sleep=advance,
            )
        self.assertTrue(result["complete"])

    def test_source_reported_early_tip_blocks_venue_ns_and_closing(self):
        self.tick()
        self.assertTrue(self.ref())
        self._bdl_live(self.venue.tip - timedelta(minutes=4))
        self.venue.now = self.venue.tip - timedelta(minutes=2)
        out = self.tick()
        self.assertIn("score_source_observed_play", str(out["errors"]))
        self.venue.now = self.venue.tip + timedelta(minutes=2)
        self.assertTrue(all(r["close_at"] is None for r in self.ref().values()))

    def test_benchmark_rechecks_previously_archived_close_with_actual_receipt_frontier(
        self,
    ):
        self.tick()
        self.venue.now = self.venue.tip - timedelta(minutes=2)
        self.tick()
        self.venue.now = self.venue.tip + timedelta(minutes=2)
        self.assertIsNotNone(self.ref()["win"]["close_at"])
        # Import the identical real BDL receipt frontier from another archived run.
        # A live notice first received after the closing observation cannot exclude it.
        self._bdl_live(self.venue.tip + timedelta(minutes=1))
        self.venue.now = self.venue.tip + timedelta(minutes=2)
        self.assertIsNotNone(self.ref()["win"]["close_at"])
        # Recompute what the ledger can prove if an earlier independent receipt exists.
        self.root = Path(self.scope.name) / "earlier-live-receipt"
        self.venue.now = self.venue.tip - timedelta(minutes=63)
        self.tick()
        self.venue.now = self.venue.tip - timedelta(minutes=2)
        self.tick()
        self._bdl_live(self.venue.tip - timedelta(minutes=4), game_id=705)
        self.venue.now = self.venue.tip + timedelta(minutes=2)
        for r in self.ref().values():
            self.assertIsNone(r["close_at"])
            self.assertEqual(r["close_excluded_reason"], "score_source_observed_play")

    def test_affirmative_venue_unavailability_vetoes_model_and_book(self):
        for flag, val in (
            ("archived", True),
            ("closed", True),
            ("active", False),
            ("cancelled", True),
        ):
            with self.subTest(flag=flag):
                self.root = Path(self.scope.name) / flag
                self.venue.event.update(
                    active=True, archived=False, closed=False, cancelled=False
                )
                self.venue.event[flag] = val
                self.tick()
                self.assertFalse(self.ref())
                reason = m.fixture_conflict(
                    self.root,
                    "NYK",
                    "MIA",
                    stamp(self.venue.tip),
                    as_of=stamp(self.venue.now),
                )
                self.assertEqual(reason, "venue_archived_closed_cancelled_or_inactive")

    def test_identity_quarantine_preserves_both_candidates_and_same_page_progress(self):
        v = self.venue
        before = v.now
        self.tick()
        original = deepcopy(v.event)
        claim = deepcopy(original)
        claim.update(
            id="49",
            slug="nba-mia-bos-2026-11-27",
            startTime=stamp(v.tip + timedelta(hours=1)),
        )
        claim["teams"][1].update(id=33, abbreviation="bos", name="Celtics")
        # Reused gameId from event 48. This valid sibling arrives on the same catalogue page.
        sibling = deepcopy(original)
        sibling.update(
            id="50",
            gameId="777",
            slug="nba-mia-phx-2026-11-27",
            startTime=stamp(v.tip + timedelta(hours=2)),
        )
        sibling["teams"][1].update(id=34, abbreviation="phx", name="Suns")
        v.now += timedelta(minutes=16)
        first_seen = v.now

        def modified_page(url, params, **kw):
            reply = v.get(url, params, **kw)
            if not url.endswith("/events/keyset"):
                return reply
            return m.api.Reply(
                json.dumps({"events": [claim, sibling]}).encode(),
                reply.requested_at,
                reply.received_at,
                reply.headers,
                reply.url,
            )

        with patch.object(m.api, "get", modified_page):
            self.assertIn("venue_identity_conflict", str(self.tick()["errors"]))
        for home, tip in (("NYK", v.tip), ("BOS", v.tip + timedelta(hours=1))):
            self.assertIsNone(
                m.fixture_conflict(
                    self.root, home, "MIA", stamp(tip), as_of=stamp(before)
                )
            )
            self.assertEqual(
                m.fixture_conflict(
                    self.root, home, "MIA", stamp(tip), as_of=stamp(first_seen)
                ),
                "venue_identity_conflict",
            )
        self.assertIsNone(
            m.fixture_conflict(
                self.root, "PHX", "MIA", sibling["startTime"], as_of=stamp(first_seen)
            )
        )
        self.assertTrue(self.ref(as_of=stamp(before)))
        with closing(m.storage.connect(self.root)) as db:
            claims = db.execute(
                "SELECT c.home,c.id,c.first_seen,req.sha FROM nba_market_identity_conflicts c "
                "JOIN nba_market_requests req ON req.id=c.receipt ORDER BY c.home"
            ).fetchall()
            self.assertEqual(
                [(r[0], r[1]) for r in claims], [("BOS", "49"), ("NYK", "48")]
            )
            self.assertTrue(all(r[2] == stamp(first_seen) and r[3] for r in claims))
            self.assertIsNotNone(
                db.execute("SELECT id FROM nba_market_games WHERE id='50'").fetchone()
            )
        # Even an apparently clean response from the old event cannot reopen the fixture.
        for minutes in (17, 2):
            v.now = v.tip - timedelta(minutes=minutes)
            self.assertIn("venue_identity_conflict", str(self.tick()["errors"]))
        v.now = v.tip + timedelta(hours=3)
        v.event["ended"] = True
        for market in v.event["markets"]:
            market.update(
                closed=True,
                umaResolutionStatus="resolved",
                outcomePrices='["0.5","0.5"]',
            )
        self.tick()
        self.assertFalse(
            m.benchmark(
                self.root, "NYK", "MIA", stamp(v.tip), "T-15m", as_of=stamp(v.now)
            )
        )
        self.assertTrue(all(r["close_at"] is None for r in self.ref().values()))
        self.assertFalse(any(url.endswith("/v2/tokens") for url, _ in v.calls))

    def test_real_rule_times_calendar_timezone_and_midnight_boundaries(self):
        v = self.venue
        e = v.event
        _, cutoff = rule_times(SAMPLE_RULES["moneyline"])
        self.assertEqual(stamp(cutoff), "2026-11-30T04:59:00.000000Z")
        for c in contracts(e, fixture(e)):
            self.assertEqual(c["original_kickoff"], stamp(v.tip))
            self.assertEqual(c["start_deadline"], stamp(cutoff))
            self.assertEqual(
                eligibility(c, stamp(v.tip), stamp(cutoff)), "rule_deadline_exceeded"
            )
            self.assertEqual(
                eligibility(
                    c,
                    stamp(v.tip),
                    stamp(v.now),
                    original_kickoff=stamp(v.tip + timedelta(minutes=3)),
                ),
                "rule_original_schedule_conflict",
            )
        source = " ".join(SAMPLE_RULES["moneyline"].split())
        early = "Friday, November 27, 2026 at 7:00 PM ET"
        latest = "Sunday, November 29, 2026, 11:59 PM ET"
        year_end = source.replace(
            early, "Thursday, December 31, 2026 at 7:00 PM ET"
        ).replace(latest, "Friday, January 1, 2027, 12:00 AM ET")
        self.assertEqual(stamp(rule_times(year_end)[1]), "2027-01-01T05:00:00.000000Z")
        march = source.replace(early, "Friday, March 6, 2026 at 7:00 PM ET")
        self.assertEqual(
            stamp(
                rule_times(march.replace(latest, "Sunday, March 8, 2026, 3:30 AM ET"))[
                    1
                ]
            ),
            "2026-03-08T07:30:00.000000Z",
        )
        for invalid in (
            "Sunday, March 8, 2026, 2:30 AM ET",
            "Sunday, November 1, 2026, 1:30 AM ET",
            "Friday, November 29, 2026, 11:59 PM ET",
            "Sunday, November 31, 2026, 11:59 PM ET",
            "Sunday, November 29, 2026, 11:59 PM local",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                rule_times(march.replace(latest, invalid))

    def test_postponed_past_rule_deadline_is_native_payout_only(self):
        v = self.venue
        self.tick()
        original_tip, original_observation = v.tip, v.now
        v.tip += timedelta(days=5)
        v.event["startTime"] = stamp(v.tip)
        for c in v.event["markets"]:
            c["gameStartTime"] = stamp(v.tip)
        v.now += timedelta(minutes=20)
        self.tick()  # reschedule was known prior to the original tip
        v.now = v.tip - timedelta(minutes=63)
        self.assertEqual(self.tick()["state"], "ok")
        self.assertFalse(self.ref())
        old = m.benchmark(
            self.root,
            "NYK",
            "MIA",
            stamp(original_tip),
            "T-60m",
            as_of=stamp(original_observation),
        )
        self.assertEqual(set(old), set(m.FAMILIES))
        with closing(m.storage.connect(self.root)) as db:
            q = db.execute(
                "SELECT quality FROM nba_market_choices WHERE version=2"
            ).fetchall()
            self.assertEqual([r[0] for r in q], ["rule_deadline_exceeded"] * 4)
            # A previously deployed collector may have marked these observations eligible.
            with db:
                db.execute(
                    "UPDATE nba_market_choices SET quality='ok', probability=.5 WHERE version=2"
                )
            self.assertFalse(self.ref())
            with db:
                db.execute(
                    "UPDATE nba_market_choices SET quality='rule_deadline_exceeded',probability=NULL WHERE version=2"
                )
        v.now = v.tip + timedelta(hours=3)
        v.event["ended"] = True
        for c in v.event["markets"]:
            c.update(
                closed=True,
                umaResolutionStatus="resolved",
                outcomePrices='["0.5","0.5"]',
            )
        self.assertEqual(self.tick()["state"], "ok")
        with closing(m.storage.connect(self.root)) as db:
            payments = [
                json.loads(r[0])
                for r in db.execute("SELECT payouts FROM nba_market_settled")
            ]
        self.assertGreaterEqual(len(payments), 4)
        self.assertTrue(all(list(p.values()) == [0.5, 0.5] for p in payments))

    def test_monitor_empty_is_read_only(self):
        self.assertEqual(m.status(self.root)["state"], "unavailable")
        self.assertFalse(self.root.exists())
