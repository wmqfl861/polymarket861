"""Exercise collector, raw Gamma/CLOB reconstruction and official terminal pair together."""

from base64 import b64decode
from copy import deepcopy
from datetime import timedelta
from email.utils import format_datetime
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from market_terms_fixture import CONDITION, SLUG, TOKEN, event, receive, reply
from polymarket861 import budget_archive_proof as proof, five_league_budget as risk
from polymarket861 import (
    ledger_terms,
    market_archive as ma,
    market_provider as provider,
    store,
)


def record_terminal(root, token, raw, when, url=None):
    url = url or f"{risk.EXCHANGE_URL}?token_id={token}"
    response = provider.Reply(
        raw, when, when, {"date": format_datetime(store.instant(when))}, url
    )
    with patch.object(provider, "get", return_value=response):
        _, _, sha = ma._fetch(
            root, "resolution", token, risk.EXCHANGE_URL, {"token_id": token}
        )
    return sha


def received_entry(root, market_type="total"):
    kickoff = store.instant("2025-02-01T18:00:00Z")
    decision = kickoff - timedelta(hours=1)
    rule_at = store.iso(decision - timedelta(minutes=3))
    native = event(store.iso(kickoff))
    if market_type in {"moneyline", "home_total"}:
        from test_round_comparisons import catalog

        schedule = catalog()
        source_teams = provider.teams(schedule)
        raw = next(
            m
            for m in schedule["markets"]
            if (
                m["sportsMarketType"] == "moneyline"
                and m["groupItemTitle"] == source_teams["home"]["name"]
                and market_type == "moneyline"
            )
            or (
                m["slug"].endswith("team-total-home-1pt5")
                and market_type == "home_total"
            )
        )
        market = native["markets"][0]
        desc = raw["description"]
        for side in ("home", "away"):
            desc = desc.replace(source_teams[side]["name"], side.capitalize())
        desc = desc.replace("March 1, 2026", "February 1, 2025")
        market.update(
            description=desc,
            sportsMarketType=raw["sportsMarketType"],
            outcomes=raw["outcomes"],
            line=raw["line"],
            groupItemTitle=("Home" if market_type == "moneyline" else "Home O/U 1.5"),
            slug=SLUG
            + ("-hom" if market_type == "moneyline" else "-team-total-home-1pt5"),
        )
    series, tag = provider.SPORT_MARKETS["epl"]
    native.update(
        sport={"sport": "epl", "series": series},
        tags=[{"id": tag}],
        series=[{"id": series}],
    )
    for team, identity in zip(native["teams"], ("21", "22")):
        team.update(id=identity, league="epl")
    receive(root, native, rule_at)
    contract = ma.contracts(root, SLUG, as_of=decision)[0]
    terms = ledger_terms.contract_at(root, CONDITION, TOKEN, decision)
    fixture = "epl/2024-25/21/22"
    books = []
    for alias, clock in (
        ("a", decision - timedelta(seconds=1)),
        ("b", decision + timedelta(seconds=5)),
    ):
        stamp = store.iso(clock)
        raw = {
            "asset_id": TOKEN,
            "market": CONDITION,
            "bids": [{"price": "0.49", "size": "300"}],
            "asks": [{"price": "0.50", "size": "300"}],
            "tick_size": "0.01",
            "min_order_size": "1",
            "timestamp": str(int(clock.timestamp() * 1000)),
        }
        url = provider.CLOB + "/book?token_id=" + TOKEN
        with patch.object(provider, "get", return_value=reply(raw, stamp, url)):
            observed = ma.snapshot_book(root, contract, TOKEN)
        books.append(
            {
                "id": alias,
                "source_sha256": observed["source_sha256"],
                "received_at": stamp,
                "asks": observed["asks"],
                "fixture_id": fixture,
                "condition_id": CONDITION,
                "token_id": TOKEN,
            }
        )
    op = {
        "id": "intent",
        "fixture_id": fixture,
        "condition_id": CONDITION,
        "token_id": TOKEN,
        "rank": 0,
        "decision_at": store.iso(decision),
        "kickoff": store.iso(kickoff),
        "decision_book": "a",
        "execution_book": "b",
        "shares": "100",
        "limit_price": "0.50",
        "selection": {
            k: terms[k]
            for k in ("family", "selection", "team", "line", "side")
            if k in terms
        },
        "rule_sha256": terms["rule_sha256"],
        "fee": terms["fee_schedule"],
    }
    return {
        "schema": risk.SCHEMA,
        "evidence_mode": "local_receipts",
        "intents": [op],
        "books": books,
        "originals": {},
        "redemptions": [],
    }, contract


def terminal_row(market, idx, amount):
    t = market["tokens"][idx]
    return {
        "condition_id": market["condition_id"],
        "token_id": t["token_id"],
        "resolved": True,
        "closed": True,
        "final_price": amount,
        "outcome_index": idx,
        "outcome": t["outcome"],
        "opposite_token_id": market["tokens"][1 - idx]["token_id"],
        "event_slug": market["fixture"],
        "market_slug": market["slug"],
    }


class NativeCapital(unittest.TestCase):
    def test_native_entry_and_terminal_require_complete_independent_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc, market = received_entry(root)

            def outcomes(d):
                return risk.replay(d, data_dir=root)["leagues"]["epl"]["outcomes"]

            self.assertEqual(
                outcomes(doc),
                {"filled": 1},
                risk.replay(doc, data_dir=root)["proof_gaps"],
            )
            for field, value in (
                ("selection", {"family": "total", "line": "2.5", "side": "under"}),
                ("rule_sha256", "made-up"),
                ("fee", {"enabled": False}),
            ):
                bad = deepcopy(doc)
                bad["intents"][0][field] = value
                self.assertEqual(outcomes(bad), {"skipped_unverified_entry": 1})
            for field, value in (
                ("asks", [{"price": "0.01", "size": "9999"}]),
                ("source_sha256", "0" * 64),
            ):
                bad = deepcopy(doc)
                bad["books"][1][field] = value
                self.assertEqual(outcomes(bad), {"skipped_unverified_entry": 1})
            bad = deepcopy(doc)
            bad["intents"][0]["execution_book"] = "a"
            with self.assertRaisesRegex(ValueError, "out of order"):
                outcomes(bad)
            bad["books"].append(dict(bad["books"][0], id="same-original"))
            bad["intents"][0]["execution_book"] = "same-original"
            with self.assertRaisesRegex(ValueError, "out of order"):
                outcomes(bad)
            forged = deepcopy(doc)
            for row in forged["intents"] + forged["books"]:
                row["fixture_id"] = "sea/2024-25/21/22"
            self.assertEqual(
                risk.replay(forged, data_dir=root)["leagues"]["sea"]["outcomes"],
                {"skipped_unverified_entry": 1},
            )
            op = doc["intents"][0]
            when = store.iso("2025-02-02T12:00:00Z")
            first, second = (terminal_row(market, n, "0.5") for n in range(2))
            pending_sha = record_terminal(
                root, TOKEN, json.dumps({"data": [first]}).encode(), when
            )
            doc["redemptions"] = [
                {
                    "id": "redemption",
                    "at": when,
                    "fixture_id": op["fixture_id"],
                    "condition_id": CONDITION,
                    "token_id": TOKEN,
                    "original_sha256": pending_sha,
                }
            ]
            self.assertEqual(
                risk.replay(doc, data_dir=root)["redemption_events"],
                {"missing_terminal_pair": 1},
            )
            record_terminal(
                root, second["token_id"], json.dumps({"data": [second]}).encode(), when
            )
            self.assertEqual(
                risk.replay(doc, data_dir=root)["redemption_events"][
                    "positions_redeemed"
                ],
                1,
            )
            contradiction = dict(second, final_price="1")
            record_terminal(
                root,
                second["token_id"],
                json.dumps({"data": [contradiction]}).encode(),
                when,
            )
            report = risk.replay(doc, data_dir=root)
            self.assertEqual(
                report["redemption_events"], {"unverified_terminal_pair": 1}
            )
            self.assertEqual(float(report["total_unredeemed_cost"]), 50)

    def test_unclosed_quarter_and_late_unspecified_restatement(self):
        for broken in ("closed", "quarter", "later"):
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                doc, market = received_entry(root)
                first = terminal_row(market, 0, "1")
                second = terminal_row(market, 1, "0")
                if broken == "closed":
                    first["closed"] = False
                if broken == "quarter":
                    first["final_price"], second["final_price"] = "0.25", "0.75"
                at = store.iso("2025-02-02T10:00Z")
                sha = record_terminal(
                    root, TOKEN, json.dumps({"data": [first]}).encode(), at
                )
                record_terminal(
                    root,
                    second["token_id"],
                    json.dumps({"data": [second]}).encode(),
                    at,
                )
                doc["redemptions"] = [
                    {
                        "id": "release",
                        "fixture_id": doc["intents"][0]["fixture_id"],
                        "condition_id": CONDITION,
                        "token_id": TOKEN,
                        "at": at,
                        "original_sha256": sha,
                    }
                ]
                if broken == "later":
                    stamp = store.iso("2025-02-03T10:00Z")
                    record_terminal(
                        root,
                        second["token_id"],
                        json.dumps(
                            {"data": [dict(second, final_price="0.5")]}
                        ).encode(),
                        stamp,
                    )
                    self.assertEqual(
                        risk.replay(doc, data_dir=root, as_of=at)["redemption_events"][
                            "positions_redeemed"
                        ],
                        1,
                    )
                    with self.assertRaisesRegex(ValueError, "conflicting terminal"):
                        risk.replay(doc, data_dir=root)
                else:
                    self.assertEqual(
                        risk.replay(doc, data_dir=root)["redemption_events"],
                        {"unverified_terminal_pair": 1},
                    )

    def test_frozen_rule_decides_split_after_verified_purchases_and_originals(self):
        cases = [
            ("moneyline", "0.5", "0.5", False),
            ("moneyline", "1", "0", True),
            ("total", "0.5", "0.5", True),
            ("home_total", "0.5", "0.5", True),
        ]
        for kind, payout, opposite, eligible in cases:
            with (
                self.subTest(kind=kind, payout=payout),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                doc, market = received_entry(root, kind)
                op = doc["intents"][0]
                early = store.iso("2025-02-01T19:00Z")
                at = store.iso("2025-02-02T12:00Z")
                # A genuine empty response before publication may be retried.
                record_terminal(root, TOKEN, b'{"data":[]}', early)
                held = terminal_row(market, 0, payout)
                other = terminal_row(market, 1, opposite)
                sha = record_terminal(
                    root, TOKEN, json.dumps({"data": [held]}).encode(), at
                )
                record_terminal(
                    root, other["token_id"], json.dumps({"data": [other]}).encode(), at
                )
                doc["redemptions"] = [
                    {
                        "id": "r",
                        "fixture_id": op["fixture_id"],
                        "token_id": TOKEN,
                        "condition_id": CONDITION,
                        "at": at,
                        "original_sha256": sha,
                    }
                ]
                result = risk.replay(doc, data_dir=root)
                league = result["leagues"]["epl"]
                self.assertEqual(league["outcomes"], {"filled": 1})
                self.assertEqual(
                    float(league["unredeemed_cost"]), 0 if eligible else 50
                )
                self.assertEqual(
                    float(league["cash_from_redemptions"]),
                    float(payout) * 100 if eligible else 0,
                )
                if not eligible:
                    self.assertIn(
                        "frozen entry rules have no reviewed equal-split payout clause",
                        result["proof_gaps"],
                    )
                if kind == "home_total":
                    later = store.iso("2025-02-03T00:00Z")
                    record_terminal(root, TOKEN, b'{"data":[]}', later)
                    with self.assertRaisesRegex(ValueError, "withdrew"):
                        risk.replay(doc, data_dir=root)

    def test_published_nantes_toulouse_originals_verified_by_both_collectors(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "reports/five-leagues-0.5/settlement-observation.json"
        )
        report = json.loads(path.read_bytes())
        receipts = report["receipts"]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("data-0", "data-1"):
                item = receipts[name]
                raw = gzip.decompress(b64decode(item["body"]))
                token = json.loads(raw)["data"][0]["token_id"]
                self.assertEqual(
                    record_terminal(
                        root, token, raw, item["received_at"], item["source"]
                    ),
                    item["sha256"],
                )
            native = json.loads(gzip.decompress(b64decode(receipts["gamma"]["body"])))
            # This published proof includes a Gamma market response, not a pregame book.
            market = {
                "fixture": report["fixture_slug"],
                "slug": report["market_slug"],
                "condition_id": report["condition_id"],
                "tokens": [
                    {"token_id": str(i), "outcome": o}
                    for i, o in zip(
                        provider.sequence(
                            native.get("positionIds") or native.get("clobTokenIds")
                        ),
                        provider.sequence(native["outcomes"]),
                    )
                ],
            }
            token = market["tokens"][1]["token_id"]
            event_record = {
                "token_id": token,
                "at": max(
                    receipts["data-0"]["received_at"], receipts["data-1"]["received_at"]
                ),
                "original_sha256": receipts["data-1"]["sha256"],
            }
            with proof.read_archive(root) as source:
                value, gap = proof.terminal_pair(
                    source, market, event_record, store.instant("2026-05-17T00:00:00Z")
                )
                self.assertIsNone(gap)
                self.assertEqual(value, 1)
                with self.assertRaisesRegex(ValueError, "predates"):
                    proof.terminal_pair(
                        source,
                        market,
                        event_record,
                        store.instant("2030-01-05T00:00:00Z"),
                    )
