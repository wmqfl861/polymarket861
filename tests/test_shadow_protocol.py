import io
import json
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR
from unittest.mock import patch
from urllib.parse import urlencode

import test_opportunity as f
from polymarket861 import (
    ledger,
    market_archive,
    market_provider,
    opportunity,
    season,
    season_health,
    settlement,
    shadow,
    store,
)
from polymarket861 import ledger_rules as rules


class ShadowProtocol(unittest.TestCase):
    def market(self, lines, *, reverse=False, market_type="totals"):
        original = f.event

        def many(slug=f.SLUG):
            event = original(slug)
            if not event["markets"]:
                return event
            template = event["markets"][0]
            rows = []
            for line in lines:
                ident = 10 - line if reverse else line + 1
                rows.append(
                    template
                    | {
                        "conditionId": f"0x{ident:064x}",
                        "sportsMarketType": market_type,
                        "clobTokenIds": json.dumps([f"{ident}01", f"{ident}02"]),
                        "slug": f"{f.SLUG}-total-{line}pt5",
                        "line": line + 0.5,
                        "groupItemTitle": f"O/U {line}.5",
                        "description": template["description"]
                        .replace("3 or more goals", f"{line + 1} or more goals")
                        .replace("less than 3", f"less than {line + 1}"),
                    }
                )
            return event | {"markets": rows}

        case = f.Opportunities()
        with patch.object(f, "event", side_effect=many):
            case.setUp()
        self.addCleanup(case.doCleanups)
        self.root = case.root
        self.seal = case.seal()
        self.first = f.CUT - timedelta(seconds=7)
        self.second = f.CUT + timedelta(seconds=6)
        self.quotes = []
        for market in market_archive.contracts(
            self.root, f.SLUG, as_of=store.iso(self.first)
        ):
            line = Decimal(market["line"])
            over = sum(
                Decimal(str(p))
                for h, row in enumerate(self.seal["matrix"])
                for a, p in enumerate(row)
                if h + a > line
            )
            for token in market["tokens"]:
                fair = over if token["outcome"] == "Over" else 1 - over
                price = min(
                    Decimal(".96"),
                    max(
                        Decimal(".02"),
                        (fair - Decimal(".04")).quantize(
                            Decimal(".01"), rounding=ROUND_FLOOR
                        ),
                    ),
                )
                self.quotes.append((market, token, price))

    def books(self, when, *, side=None, size="2000"):
        for market, token, price in self.quotes:
            if side and token["outcome"] != side:
                continue
            ident = token["token_id"]
            native = {
                "asset_id": ident,
                "market": market["condition_id"],
                "hash": store.digest([ident, store.iso(when)]),
                "tick_size": "0.01",
                "min_order_size": "1",
                "timestamp": int(when.timestamp() * 1000),
                "bids": [{"price": str(price - Decimal(".01")), "size": size}],
                "asks": [{"price": str(price), "size": size}],
            }

            def get(url, params=None):
                return f.reply(native, when, url + "?" + urlencode(params))

            with f.at(when), patch.object(market_provider, "get", side_effect=get):
                market_archive.snapshot_book(self.root, market, ident)

    def freeze(self, *, formal=False):
        with f.at(f.CUT - timedelta(seconds=5)):
            if formal:
                ledger.open_account(self.root, "main", "formal")
                opportunity.process(self.root, f.SLUG)
            return shadow.run(self.root, f.SLUG)

    def execute(self):
        return shadow.run(self.root, f.SLUG)

    def test_six_candidates_renumbered_and_hedged_exposure_matches_formal(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                self.market(range(3), reverse=reverse)
                self.books(self.first)
                out = self.freeze(formal=True)
                self.assertEqual(
                    Counter(r["state"] for r in out["actions"]), {"frozen": 6}, out
                )
                self.books(self.second)
                with f.at(self.second + timedelta(seconds=1)):
                    opportunity.process(self.root, f.SLUG)
                    with ThreadPoolExecutor(3) as pool:
                        list(pool.map(lambda _: self.execute(), range(3)))
                open_report = shadow.report(self.root)
                self.assertEqual(open_report["counts"], {"held": 6}, open_report)
                economics = {(o["line"], o["side"]) for o in open_report["orders"]}
                self.assertEqual(
                    economics,
                    {(f"{i}.5", side) for i in range(3) for side in ("over", "under")},
                )
                total = sum(Decimal(o["spent"]) for o in open_report["orders"])
                self.assertGreater(total, 150)
                self.assertEqual(total + Decimal(open_report["cash"]), 10000)
                formal = {
                    o["token_id"]: o
                    for o in ledger.ledger_details(self.root, "main")["intents"]
                }
                for o in open_report["orders"]:
                    with self.subTest(token=o["token_id"]):
                        self.assertLessEqual(Decimal(o["spent"]), 50)
                        self.assertLessEqual(Decimal(o["worst_match_loss_after"]), 150)
                        other = formal[o["token_id"]]
                        for field in ("spent", "net_shares", "worst_match_loss_after"):
                            self.assertEqual(Decimal(o[field]), Decimal(other[field]))

    def test_correlated_unhedged_loss_and_account_cash_limits(self):
        for initial, cap in (("10000", 150), ("63", 63)):
            with self.subTest(initial=initial):
                self.market(range(5))
                self.books(self.first, side="Under")
                with shadow.db_for(self.root) as db:
                    db.execute(
                        "INSERT INTO research_shadow_accounts VALUES(?,?,?)",
                        ("research-shadow", initial, initial),
                    )
                decision = self.freeze()
                self.assertEqual(
                    Counter(r["state"] for r in decision["actions"]),
                    {"frozen": 5},
                    decision,
                )
                self.books(self.second, side="Under")
                with (
                    f.at(self.second + timedelta(seconds=1)),
                    ThreadPoolExecutor(3) as pool,
                ):
                    list(pool.map(lambda _: self.execute(), range(3)))
                report = shadow.report(self.root)
                spent = sum(
                    Decimal(o["spent"])
                    for o in report["orders"]
                    if o["status"] == "held"
                )
                self.assertLessEqual(spent, cap)
                self.assertGreater(spent, cap - 1)
                self.assertEqual(Decimal(initial) - spent, Decimal(report["cash"]))
                self.assertGreater(report["counts"].get("skipped", 0), 0)

    def test_missing_decision_catalog_alerts_only_during_the_decision_window(self):
        for lines, market_type, reason in (
            ([], "totals", "no verified listing at decision"),
            ([1], "unsupported_player_prop", "no verified token mapping at decision"),
        ):
            with self.subTest(reason=reason):
                self.market(lines, market_type=market_type)
                decision = f.CUT - timedelta(seconds=5)
                for when in (f.CUT - timedelta(seconds=31), self.second):
                    with f.at(when):
                        idle = season._paper(self.root, f.SLUG)["shadow"]
                        self.assertEqual((idle["state"], idle["gaps"]), ("idle", 0))
                with f.at(decision):
                    diagnostic = self.execute()
                    self.assertEqual(diagnostic["actions"], [])
                    self.assertEqual(len(diagnostic["gaps"]), 1)
                    gap = diagnostic["gaps"][0]
                    self.assertEqual(
                        (gap["fixture_id"], gap["decision_at"], gap["listed_markets"]),
                        (f.FID, store.iso(f.CUT), len(lines)),
                    )
                    self.assertEqual(
                        gap["mapping_errors"],
                        {"outside_supported_full_time_families": 1} if lines else {},
                    )
                    result = season._paper(self.root, f.SLUG)
                    self.assertEqual(
                        result["shadow"]["operational_reasons"], {reason: 1}
                    )
                    self.assertEqual(result["shadow"]["state"], "gap")
                    slot = {
                        "fixture": f.SLUG,
                        "kickoff": store.iso(f.KICK),
                        "role": "decision",
                    }
                    with (
                        closing(season._db(self.root)) as db,
                        db,
                        redirect_stderr(io.StringIO()),
                    ):
                        season_health.paper_state(db, result, slot, store.iso(decision))
                        alert = db.execute(
                            "SELECT reason FROM season_alerts WHERE fixture=? AND role='paper_shadow'",
                            (f.SLUG,),
                        ).fetchone()
                        self.assertIn(reason, alert[0])

    def test_empty_cash_and_repeated_skips_retain_the_economic_reason(self):
        self.market(range(1))
        self.books(self.first)
        with f.at(self.first):
            ledger.open_account(self.root, "main", "formal")
            with ledger.connection(self.root, True) as db:
                db.execute("UPDATE paper_accounts SET cash='0' WHERE account='main'")
            with shadow.db_for(self.root) as db:
                db.execute(
                    "INSERT INTO research_shadow_accounts VALUES('research-shadow','10000','0')"
                )
        with f.at(f.CUT - timedelta(seconds=5)):
            formal = opportunity.process(self.root, f.SLUG)
            self.assertEqual(
                {x["reason"] for x in formal["decisions"]}, {"insufficient cash"}
            )
            first = self.execute()
            again = self.execute()
            self.assertEqual(
                season._paper(self.root, f.SLUG)["shadow"]["state"], "no_value"
            )
            self.assertEqual(
                {x["reason"] for x in first["actions"]},
                {"insufficient_edge_depth_cash_or_minimum"},
            )
            self.assertEqual(
                [(x["id"], x["reason"]) for x in first["actions"]],
                [(x["id"], x["reason"]) for x in again["actions"]],
            )
            self.assertTrue(all(x["repeated"] for x in again["actions"]))

    def test_scoped_depth_and_legacy_order_finish_as_sealed(self):
        self.market(range(1))
        self.books(self.first, size="40")
        decision = self.freeze()
        self.assertEqual(len(decision["actions"]), 2)
        with shadow.db_for(self.root) as db:
            rows = list(
                db.execute("SELECT * FROM research_shadow_orders ORDER BY token")
            )
            original = json.loads(rows[0]["plan"])
            for key in ("order_protocol", "season", "round_id"):
                original.pop(key)
            db.execute(
                "UPDATE research_shadow_orders SET plan=?,plan_sha=? WHERE id=?",
                (rules.packed(original), rules.digest(original), rows[0]["id"]),
            )
            for row in rows:
                p = json.loads(row["plan"])
                c = p["intent"]["contract"]
                snap = rules.check_book(p["first_book"], c, f.CUT, f.KICK, "first")
                price = snap["asks"][0][0]
                ledger._consume(
                    db,
                    row["account"],
                    row["condition_id"],
                    row["token"],
                    snap,
                    snap,
                    [(price, price, Decimal("34"))],
                    research=True,
                )
        self.books(self.second, size="40")
        with f.at(self.second + timedelta(seconds=1)):
            self.execute()
            self.execute()
        result = shadow.report(self.root)
        self.assertEqual(result["counts"], {"held": 2}, result)
        by = {r["order_protocol"]: r for r in result["orders"]}
        self.assertEqual(Decimal(by[shadow.LEGACY_PROTOCOL]["gross_shares"]), 40)
        self.assertIsNone(by[shadow.LEGACY_PROTOCOL].get("worst_match_loss_after"))
        self.assertEqual(
            by[shadow.LEGACY_PROTOCOL]["plan_sha256"], rules.digest(original)
        )
        self.assertEqual(Decimal(by[shadow.ORDER_PROTOCOL]["gross_shares"]), 6)
        self.assertEqual(
            set(result["by_order_protocol"]),
            {shadow.LEGACY_PROTOCOL, shadow.ORDER_PROTOCOL},
        )
        self.assertEqual(result["performance_scope"], "per_order_protocol")
        self.assertEqual(by[shadow.LEGACY_PROTOCOL]["round_id"], 26)
        self.assertEqual(
            sum(Decimal(o["spent"]) for o in by.values()) + Decimal(result["cash"]),
            10000,
        )

        # Every old booked amount is still attributed to its original strategy.
        # Crediting the same native payout twice must not increase the cash twice.
        for protocol, order in by.items():
            event = {
                "payout": "1" if protocol == shadow.LEGACY_PROTOCOL else "0",
                "score": [1, 0],
                "resolved_at": store.iso(f.KICK + timedelta(hours=3)),
                "source_sha256": "a" * 64,
                "regulation_receipt": [{"sha256": "b" * 64}],
            }
            position = {"account": "research-shadow", "id": order["id"]}
            with ThreadPoolExecutor(2) as pool:
                list(
                    pool.map(
                        lambda _: settlement._credit_shadow(self.root, position, event),
                        range(2),
                    )
                )
        settled = shadow.report(self.root)
        self.assertEqual(settled["counts"], {"settled": 2})
        self.assertIsNone(settled["settled_roi"])
        self.assertEqual(
            Decimal(settled["cash"]), 10000 + Decimal(settled["realized_pnl"])
        )
        for protocol, r in by.items():
            proceeds = (
                Decimal(r["net_shares"])
                if protocol == shadow.LEGACY_PROTOCOL
                else Decimal(0)
            )
            cohort = settled["by_order_protocol"][protocol]
            self.assertEqual(
                Decimal(cohort["realized_pnl"]), proceeds - Decimal(r["spent"])
            )
            self.assertEqual(
                Decimal(cohort["settled_roi"]),
                (proceeds - Decimal(r["spent"])) / Decimal(r["spent"]),
            )

    def test_real_deadline_crossing_rolls_back_even_the_first_order(self):
        self.market(range(3))
        self.books(self.first)
        freeze = shadow._freeze
        with f.at(f.CUT - timedelta(seconds=1)):

            def delayed(*args, **kwargs):
                action = freeze(*args, **kwargs)
                store.now_utc.return_value = store.iso(
                    f.CUT + timedelta(microseconds=1)
                )
                return action

            with patch.object(shadow, "_freeze", side_effect=delayed):
                result = self.execute()
        self.assertEqual(result["actions"], [])
        self.assertTrue(any("deadline" in g["reason"] for g in result["gaps"]))
        with shadow.db_for(self.root) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM research_shadow_orders").fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM research_shadow_accounts").fetchone()[
                    0
                ],
                0,
            )

    def test_interval_counts_matches_and_rounds_and_keeps_pooled_roi(self):
        orders = [
            {
                "status": "settled",
                "season": "2025-26",
                "round_id": week,
                "fixture_id": fixture,
                "spent": spent,
                "proceeds": proceeds,
            }
            for week, fixture, spent, proceeds in [
                (1, "a", "40", "45"),
                (1, "a", "10", "0"),
                (1, "b", "3", "1"),
                (2, "c", "47", "55"),
            ]
        ]
        value = shadow._round_interval(orders, "settled_roi")
        self.assertEqual((value["matches"], value["rounds"]), (3, 2))
        self.assertEqual(Decimal(value["estimate"]), Decimal(".01"))
        self.assertLess(value["interval"]["lower"], 0.01)
        self.assertGreater(value["interval"]["upper"], 0.01)
        orders[0]["round_id"] = None
        self.assertIsNone(shadow._round_interval(orders, "settled_roi")["interval"])
