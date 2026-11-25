import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from polymarket861 import ledger as L
from polymarket861 import ledger_rules as rules


class PaperLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        L.open_account(self.root, "t", "demo")
        self.op = L.example()

    def freeze(self, op=None, account="t"):
        op = op or self.op
        return L.freeze(self.root, account, op["intent"], op["first_book"])

    def fill(self, op=None, account="t"):
        op = op or self.op
        self.freeze(op, account)
        return L.evaluate(
            self.root,
            account,
            op["intent"]["intent_id"],
            op["second_book"],
            actual_kickoff=op["actual_kickoff"],
        )

    def test_depth_two_ladders_cash_fee_and_settlement_once(self):
        self.op["first_book"]["asks"] = [
            {"price": ".41", "size": "4"},
            {"price": ".42", "size": "6"},
        ]
        self.op["second_book"]["asks"] = [
            {"price": ".41", "size": "6"},
            {"price": ".43", "size": "3"},
        ]
        out = self.fill()
        self.assertEqual(D(out["gross_shares"]), 9)
        self.assertEqual(
            [(D(x["price"]), D(x["size"])) for x in out["fill_levels"]],
            [(D(".41"), 4), (D(".42"), 2), (D(".43"), 3)],
        )
        self.assertEqual(out["status"], "filled")
        duplicate = self.fill()
        self.assertEqual(out, duplicate)
        resolution = self.op["settlement"]
        L.settle(self.root, "t", resolution)
        L.settle(self.root, "t", resolution)
        report = L.ledger_report(self.root, "t")
        self.assertEqual(report["settled_positions"], 1)
        self.assertEqual(D(report["cash"]), 10000 - D(out["spent"]) + D("4.5"))
        with self.assertRaises(ValueError):
            L.settle(self.root, "t", dict(resolution, payout="0", resolution_id="new"))

    def test_late_lineup_wide_or_stale_book_and_locked_decision(self):
        for key, change in [
            (
                "missing-lineup",
                lambda x: x["intent"]["lineups"]["away"].update(confirmed=False),
            ),
            (
                "late-lineup",
                lambda x: x["intent"]["lineups"]["away"].update(
                    received_at=x["second_book"]["received_at"]
                ),
            ),
            ("wide", lambda x: x["first_book"]["bids"][0].update(price=".15")),
            (
                "late",
                lambda x: x["first_book"].update(
                    received_at=x["second_book"]["received_at"]
                ),
            ),
            ("direction", lambda x: x["first_book"].update(token_id="different")),
            ("fee", lambda x: x["intent"]["contract"]["fee_schedule"].pop("version")),
            ("gap", lambda x: x["intent"].update(normal_fv=".9")),
            ("fallback", lambda x: x["first_book"].update(observed_at=None)),
            ("league", lambda x: x["first_book"].update(venue="polymarket.us")),
        ]:
            with self.subTest(key):
                op = deepcopy(self.op)
                op["intent"]["intent_id"] = key
                L.open_account(self.root, key, "demo")
                change(op)
                self.assertEqual(self.freeze(op, key)["status"], "skipped")
        self.freeze()
        self.op["intent"]["quantity"] = "20"
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.freeze()

    def test_execution_window_and_no_retry_for_better_quote(self):
        for key, stamp in (
            ("early", "2025-02-01T12:00:04Z"),
            ("late", "2025-02-01T12:00:11Z"),
        ):
            L.open_account(self.root, key, "demo")
            self.freeze(account=key)
            bad = dict(
                self.op["second_book"],
                received_at=stamp,
                observed_at=stamp,
                requested_at=stamp,
            )
            self.assertEqual(
                L.evaluate(self.root, key, "demo-intent", bad)["status"], "skipped"
            )
            with self.assertRaisesRegex(ValueError, "finalized"):
                L.evaluate(self.root, key, "demo-intent", self.op["second_book"])
        self.op["intent"]["contract"]["seconds_delay"] = 11
        self.assertEqual(self.fill()["status"], "skipped")

    def test_concurrent_identical_fill_and_settle_are_idempotent(self):
        self.freeze()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda _: L.evaluate(
                        self.root, "t", "demo-intent", self.op["second_book"]
                    ),
                    range(8),
                )
            )
        self.assertTrue(all(x["spent"] == results[0]["spent"] for x in results))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(
                    lambda _: L.settle(self.root, "t", self.op["settlement"]), range(8)
                )
            )
        self.assertEqual(D(L.ledger_report(self.root, "t")["cash"]), D("10000.6782"))

    def test_fractional_payout_postponement_and_clv_are_independent(self):
        out = self.fill()
        original = self.op["settlement"]
        pending = dict(
            original,
            scenario="postponed",
            payout=None,
            status="pending",
            resolution_id="wait",
        )
        L.settle(self.root, "t", pending)
        self.assertEqual(L.ledger_report(self.root, "t")["open_positions"], 1)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            L.settle(self.root, "t", dict(original, scenario="postponed"))
        self.assertEqual(L.ledger_report(self.root, "t")["open_positions"], 1)
        L.settle(self.root, "t", original)
        no_close = L.record_close(
            self.root,
            "t",
            "demo-intent",
            self.op["first_book"],
            self.op["actual_kickoff"],
        )
        self.assertIsNone(no_close["clv"])
        self.assertIsNotNone(L.ledger_report(self.root, "t")["realized_roi"])
        close = L.record_close(
            self.root,
            "t",
            "demo-intent",
            self.op["close_book"],
            self.op["actual_kickoff"],
        )
        self.assertEqual(D(close["clv"]), D(".46") - D(out["unit_cost"]))

    def test_result_mapping_single_teams_and_early_determined_over(self):
        c = self.op["intent"]["contract"]
        for team, h, a, expected in [
            ("home", 2, 0, 1),
            ("away", 2, 0, 0),
            ("away", 0, 2, 1),
            ("home", 0, 2, 0),
        ]:
            self.assertEqual(
                rules.normal_payout(
                    dict(c, family="team_total", team=team, line="1.5"), h, a
                ),
                expected,
            )
        self.assertEqual(rules.scenario_payout(c, "abandoned", (2, 1)), 1)
        self.assertEqual(rules.scenario_payout(c, "abandoned", (0, 1)), D(".5"))
        for selection, void in (("home", "0"), ("draw", "1"), ("away", "0")):
            rc = dict(
                c,
                family="result",
                selection=selection,
                side="yes",
                special={"no_result": void},
            )
            self.assertEqual(rules.scenario_payout(rc, "no_result"), D(void))
        self.assertEqual(
            rules.normal_payout(
                dict(c, line="2.25", split_quarter=True, equal_payout=".5"), 1, 1
            ),
            D(".25"),
        )
        self.assertIsNone(rules.scenario_payout(c, "postponed"))

    def test_cannot_consume_the_same_surviving_book_twice_across_reschedules(self):
        from datetime import timedelta

        op = self.op
        op["intent"]["quantity"] = "8"
        for k in ("first_book", "second_book"):
            book = op[k]
            book.update(
                book_hash="unchanged-exchange-hash",
                bids=[{"price": ".40", "size": "10"}],
                asks=[{"price": ".42", "size": "10"}],
            )
        self.assertEqual(D(self.fill(op)["gross_shares"]), 8)
        later = deepcopy(op)
        later["intent"]["intent_id"] = "rescheduled"

        def shift(value):
            return rules.iso(rules.instant(value) + timedelta(minutes=1))

        for key in ("decision_at", "kickoff"):
            later["intent"][key] = shift(later["intent"][key])
        for key in ("first_book", "second_book"):
            for k in ("requested_at", "received_at", "observed_at"):
                later[key][k] = shift(later[key][k])
        later["actual_kickoff"] = shift(later["actual_kickoff"])
        self.assertEqual(D(self.fill(later)["gross_shares"]), 2)

    def test_executed_fee_can_be_deducted_in_shares(self):
        self.op["intent"]["contract"]["fee_schedule"].update(
            asset="shares", round_in="charged"
        )
        out = self.fill()
        self.assertEqual(D(out["spent"]), D("4.2"))
        self.assertEqual(D(out["net_shares"]), D("9.71"))
        self.assertEqual(D(out["unit_cost"]), D("4.2") / D("9.71"))
        L.settle(self.root, "t", self.op["settlement"])
        self.assertEqual(D(L.ledger_report(self.root, "t")["cash"]), D("10000.655"))

    def test_budget_reservations_cash_risk_and_account_scope(self):
        L.open_account(self.root, "small", "demo", "6")
        for i in range(2):
            op = deepcopy(self.op)
            op["intent"]["intent_id"] = str(i)
            token = "tok" + str(i)
            for obj in (op["intent"]["contract"], op["first_book"], op["second_book"]):
                obj["token_id"] = token
            op["intent"]["budget"] = "6"
            self.freeze(op, "small")
        summary = L.ledger_report(self.root, "small")
        self.assertEqual(D(summary["reserved_cash"]), 6)
        self.assertEqual(D(summary["available_cash"]), 0)
        for i in range(2):
            out = L.evaluate(
                self.root,
                "small",
                str(i),
                dict(self.op["second_book"], token_id="tok" + str(i)),
            )
            self.assertEqual(out["status"], "filled")
        self.assertGreaterEqual(D(L.ledger_report(self.root, "small")["cash"]), 0)
        # Four different markets on the same outcome cannot spend four times 50.
        for i in range(4):
            op = deepcopy(self.op)
            ident = "risk-" + str(i)
            op["intent"].update(intent_id=ident, quantity="140")
            for b in (op["intent"]["contract"], op["first_book"], op["second_book"]):
                b["token_id"] = ident
            for b in (op["first_book"], op["second_book"]):
                b["asks"][0]["size"] = "140"
            out = self.fill(op)
            if out["status"] == "filled":
                self.assertLessEqual(D(out["spent"]), 50)
                self.assertLessEqual(D(out["worst_match_loss_after"]), 150)
        self.assertLessEqual(D(L.ledger_report(self.root, "t")["total_spent"]), 150)

    def test_historical_evidence_separation_and_demo_idempotency(self):
        demo = L.demo(self.root)
        self.assertEqual(
            [demo["market_replay"]["counts"][k] for k in ("A", "B", "C", "missing")],
            [1, 1, 1, 1],
        )
        self.assertEqual(L.demo(self.root)["ledger"]["cash"], demo["ledger"]["cash"])
        historical = L.replay(self.root, [self.op], account="historical")
        self.assertEqual(historical["counts"]["A"], 0)
        self.assertIsNone(historical["roi"])
        self.assertFalse(L.audit_risk(self.root, "demo-only")["passed"])
        with self.assertRaisesRegex(ValueError, "different input"):
            L.replay(self.root, [], account="historical")

    def test_formal_uses_sealed_model_archived_direction_books_and_exchange_cash(self):
        from polymarket861 import (
            datasets,
            football,
            input_manifest,
            jobs,
            market_archive,
            market_provider,
            pipeline,
        )

        op = self.op
        intent = op["intent"]
        intent["model_version"] = "B1"
        from market_terms_fixture import prepare_formal_market

        c = prepare_formal_market(self.root, op)
        token, condition = c["token_id"], c["condition_id"]
        cutoff, kickoff = intent["decision_at"], intent["kickoff"]
        source = Path(self.root) / "test-input"
        season_file = source / "2024-25" / "en.1.json"
        season_file.parent.mkdir(parents=True)
        season_file.write_text(
            json.dumps(
                {
                    "matches": [
                        {
                            "round": "Matchday 1",
                            "date": "2024-08-17",
                            "time": "15:00",
                            "team1": "Home",
                            "team2": "Away",
                            "score": {"ft": [1, 1]},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        observed_at = c["rule_received_at"]
        with (
            patch.object(datasets, "now_utc", return_value=observed_at),
            patch.object(input_manifest, "now_utc", return_value=observed_at),
            patch.object(L.store, "now_utc", return_value=observed_at),
        ):
            training = datasets.observe(
                self.root,
                source,
                revision="a" * 40,
                expected={"2024-25": football.blob_sha(season_file.read_bytes())},
            )
        with patch.object(L.store, "now_utc", return_value=observed_at):
            L.store.register_fixture(
                self.root,
                {
                    "fixture_id": intent["match_id"],
                    "home": "Home",
                    "away": "Away",
                    "season": "2024/25",
                    "round_id": 20,
                    "source": "test-verified",
                    "locator": "test-fixture",
                    "kickoff": kickoff,
                    "known_at": c["rule_received_at"],
                },
            )
        with patch.object(L.store, "now_utc", return_value="2024-07-01T10:00:00.000000Z"):
            pipeline.register_recipe(self.root, "candidate-v", "B2")
            registered = jobs.register_candidate(
                self.root,
                "candidate-test",
                kind="model",
                version="candidate-v",
                baseline="B1",
                window="2024-25/1",
                hypothesis="test registered model",
                manifest={"paper_account": "candidate-account"},
            )
        payload = {
            "input_manifest_sha256": training["manifest_digest"],
            "fixture_id": intent["match_id"],
            "model_version": intent["model_version"],
            "cutoff": cutoff,
            "kickoff": kickoff,
            "lineups": intent["lineups"],
            "matrix": [[0.535], [0], [0], [0.465]],
        }
        intent["forecast_digest"] = L.store.digest(payload)
        with closing(market_archive.database(self.root)) as db, db:
            db.execute(
                "INSERT INTO trial_activations VALUES (?,?,?,?)",
                (L.PROTOCOL, c["rule_received_at"], c["rule_received_at"], "{}"),
            )
            db.execute(
                "INSERT INTO forecast_seals VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    intent["match_id"],
                    kickoff,
                    cutoff,
                    "T-60m",
                    intent["model_version"],
                    L.PROTOCOL,
                    "formal",
                    intent["prediction_at"],
                    intent["forecast_digest"],
                    L.store.canonical(payload),
                ),
            )
            old_payload = {
                k: v for k, v in payload.items() if k != "input_manifest_sha256"
            }
            old_payload["model_version"] = "legacy-no-manifest"
            old_digest = L.store.digest(old_payload)
            db.execute(
                "INSERT INTO forecast_seals VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    intent["match_id"],
                    kickoff,
                    cutoff,
                    "T-60m",
                    old_payload["model_version"],
                    L.PROTOCOL,
                    "formal",
                    intent["prediction_at"],
                    old_digest,
                    L.store.canonical(old_payload),
                ),
            )
            candidate_payload = payload | {
                "model_version": "candidate-v",
                "purpose": "candidate",
            }
            candidate_payload["recipe_digest"] = registered["manifest"]["recipe_digest"]
            candidate_digest = L.store.digest(candidate_payload)
            db.execute(
                "INSERT INTO forecast_seals VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    intent["match_id"],
                    kickoff,
                    cutoff,
                    "T-60m",
                    "candidate-v",
                    L.PROTOCOL,
                    "formal",
                    intent["prediction_at"],
                    candidate_digest,
                    L.store.canonical(candidate_payload),
                ),
            )
        with patch.object(L.store, "now_utc", return_value=cutoff):
            L.open_account(self.root, "f", "formal")
            legacy_order = op | {
                "intent": intent
                | {"model_version": "legacy-no-manifest", "forecast_digest": old_digest}
            }
            with self.assertRaisesRegex(ValueError, "manifest"):
                self.freeze(legacy_order, "f")
            with L.connection(self.root, True) as db:
                row = db.execute(
                    "SELECT manifest FROM football_input_manifests WHERE digest=?",
                    (training["manifest_digest"],),
                ).fetchone()
                saved = row["manifest"]
                changed = json.loads(saved) | {"revision": "b" * 40}
                db.execute(
                    "UPDATE football_input_manifests SET manifest=? WHERE digest=?",
                    (L.store.canonical(changed), training["manifest_digest"]),
                )
            with self.assertRaisesRegex(ValueError, "manifest"):
                self.freeze(op, "f")
            with L.connection(self.root, True) as db:
                db.execute(
                    "UPDATE football_input_manifests SET manifest=? WHERE digest=?",
                    (saved, training["manifest_digest"]),
                )
            candidate_order = deepcopy(op)
            candidate_order["intent"].update(
                forecast_digest=candidate_digest, model_version="candidate-v"
            )
            with self.assertRaisesRegex(ValueError, "purpose|active EPL"):
                self.freeze(candidate_order, "f")
            with self.assertRaisesRegex(ValueError, "unregistered entry strategy"):
                self.freeze(
                    op | {"intent": intent | {"strategy_version": "unregistered"}}, "f"
                )
            self.assertEqual(L.ledger_report(self.root, "f")["intents"], [])
            account = L.open_account(
                self.root,
                "candidate-account",
                "formal",
                role="candidate",
                candidate_key="candidate-test",
                version="candidate-v",
            )
            self.assertEqual(account["registration_digest"], registered["digest"])
            with self.assertRaisesRegex(ValueError, "immutable"):
                L.open_account(self.root, "candidate-account", "formal", role="main")
            with self.assertRaisesRegex(ValueError, "registration does not bind"):
                L.open_account(
                    self.root,
                    "f",
                    "formal",
                    role="candidate",
                    candidate_key="candidate-test",
                )
            with self.assertRaisesRegex(ValueError, "candidate registration|purpose"):
                self.freeze(op, "candidate-account")
            with self.assertRaisesRegex(ValueError, "Unknown experiment"):
                L.open_account(
                    self.root,
                    "rogue",
                    "formal",
                    role="candidate",
                    candidate_key="rogue",
                )
            self.assertEqual(
                self.freeze(candidate_order, "candidate-account")["status"], "frozen"
            )
            self.assertEqual(self.freeze(op, "f")["status"], "frozen")
            tampered = deepcopy(op)
            tampered["intent"]["contract"]["side"] = "under"
            L.open_account(self.root, "other", "formal")
            self.assertEqual(self.freeze(tampered, "other")["status"], "skipped")
        # The worker restarts after kickoff; only its already archived, timely second book qualifies.
        with patch.object(L.store, "now_utc", return_value=op["actual_kickoff"]):
            self.assertEqual(
                L.recover(self.root, "f")["recovered"],
                [{"intent_id": intent["intent_id"], "status": "filled"}],
            )
            self.assertEqual(L.recover(self.root, "f")["recovered"], [])
            self.assertEqual(
                L.recover(self.root, "candidate-account")["recovered"][0]["status"],
                "filled",
            )
            audited = L.audit_risk(
                self.root, "candidate-v", account="candidate-account", mode="formal"
            )
            self.assertTrue(audited["passed"])
            self.assertEqual(audited["accounts"][0]["candidate_key"], "candidate-test")
            self.assertEqual(audited["accounts"][0]["role"], "candidate")

        fake = dict(
            op["settlement"],
            token_id=token,
            source="https://data-api.polymarket.com/v2/tokens?token_id=71",
        )
        with self.assertRaises(ValueError):
            L.settle(self.root, "f", fake)
        raw = json.dumps(
            {
                "data": [
                    {
                        "condition_id": condition,
                        "token_id": token,
                        "resolved": True,
                        "final_price": 0.5,
                    }
                ]
            }
        ).encode()
        captured = market_provider.Reply(
            raw, fake["resolved_at"], fake["resolved_at"], {}, fake["source"]
        )
        with patch.object(market_provider, "get", return_value=captured):
            official = L.capture_resolution(self.root, token)
        L.settle(self.root, "f", dict(official, scenario="canceled"))
        self.assertEqual(D(L.ledger_report(self.root, "f")["cash"]), D("10000.8"))
        self.assertTrue(
            L.audit_risk(
                self.root,
                intent["model_version"],
                account="f",
                mode="formal",
                fixture_ids=[intent["match_id"]],
            )["passed"]
        )
        self.assertFalse(
            L.audit_risk(
                self.root,
                intent["model_version"],
                account="f",
                fixture_ids=["another-fixture"],
            )["passed"]
        )

    def test_source_must_really_cover_the_normalized_historical_book(self):
        op = self.op
        op["depth_provenance"], op["kickoff_source"] = (
            "audited_archive",
            "independent fixture timestamp",
        )
        for key in ("first_book", "second_book"):
            b = op[key]
            archived = L.store.archive_bytes(
                self.root,
                "test-archive",
                json.dumps(b).encode(),
                observed_at=b["received_at"],
            )
            b["source_sha256"] = archived["sha256"]
        audit = {
            "licence": "test only",
            "reviewer": "test independent check",
            "continuity_verified": True,
            "normalized_book_hashes": [
                L.store.digest(op[k]) for k in ("first_book", "second_book")
            ],
            "normalized_contract_hashes": [L.store.digest(op["intent"]["contract"])],
        }
        op["archive_audit_ref"] = L.store.archive_bytes(
            self.root, "test-audit", json.dumps(audit).encode()
        )["sha256"]
        op.pop("settlement")
        rejected = L.replay(self.root, [op])
        self.assertEqual(rejected["counts"]["missing"], 1)
        self.assertEqual(rejected["counts"]["A"], 0)
        self.assertEqual(rejected["counts"]["filled"], 0)
        self.assertEqual(rejected["clv_covered_fills"], 0)
        self.assertIsNone(rejected["roi"])

    def test_legacy_accounts_and_registration_window_use_as_of_time(self):
        from polymarket861 import jobs

        order = self.op["intent"]
        when = rules.instant(order["decision_at"])
        with patch.object(L.store, "require_formal", return_value={}):
            with L.connection(self.root, True) as db:
                db.execute(
                    "INSERT INTO paper_accounts VALUES (?,?,?,?)",
                    ("legacy", "formal", "900", "900"),
                )
            self.assertEqual(
                L.open_account(self.root, "legacy", "formal", "900")["role"], "main"
            )
            with patch.object(
                L.store, "now_utc", return_value="2025-01-01T10:00:00.000000Z"
            ):
                c = jobs.register_candidate(
                    self.root,
                    "new-key",
                    kind="model",
                    version="challenger",
                    baseline="B1",
                    window="2025-26/1",
                    hypothesis="test a stable version",
                    manifest={"paper_account": "trial"},
                )
            L.open_account(
                self.root, "trial", "formal", role="candidate", candidate_key=c["key"]
            )
            # Register an actual model change later than the decision, and query the real time-aware pointer.
            with L.connection(self.root, True) as db:
                db.execute(
                    "INSERT INTO research_events(key,action,at,evidence) VALUES (?,?,?,?)",
                    (c["key"], "promoted", "2025-02-02T00:00:00.000000Z", "{}"),
                )
            incumbent = order | {"model_version": "B1"}
            self.assertEqual(
                L._formal_authority(
                    self.root, "legacy", incumbent, {"model_version": "B1"}, when
                )["role"],
                "main",
            )
            with self.assertRaisesRegex(ValueError, "active EPL model"):
                L._formal_authority(
                    self.root,
                    "legacy",
                    incumbent,
                    {"model_version": "B1"},
                    rules.instant("2025-02-03T00:00:00Z"),
                )
            challenger = order | {"model_version": "challenger"}
            seal = {"model_version": "challenger", "purpose": "candidate"}
            L.store.register_fixture(
                self.root,
                {
                    "fixture_id": order["match_id"],
                    "home": "Alpha",
                    "away": "Beta",
                    "season": "2024-25",
                    "round_id": 30,
                    "source": "test",
                    "locator": "test-round",
                    "kickoff": order["kickoff"],
                    "known_at": "2025-01-01T00:00:00Z",
                },
            )
            with self.assertRaisesRegex(ValueError, "outside the preregistered"):
                L._formal_authority(self.root, "trial", challenger, seal, when)
            with self.assertRaisesRegex(ValueError, "postdates"):
                L._formal_authority(
                    self.root,
                    "trial",
                    challenger | {"prediction_at": "2024-12-10T00:00:00Z"},
                    seal,
                    when,
                )
            with self.assertRaisesRegex(ValueError, "registration does not bind"):
                L.open_account(
                    self.root,
                    "legacy",
                    "formal",
                    "900",
                    role="candidate",
                    candidate_key=c["key"],
                )

    def test_formal_requires_activation_and_a_sealed_forecast(self):
        with self.assertRaises(ValueError):
            L.open_account(self.root, "formal", "formal")
        with patch.object(L.store, "require_formal", return_value={"test": True}):
            L.open_account(self.root, "formal", "formal")
            with self.assertRaisesRegex(ValueError, "forecast digest"):
                self.freeze(account="formal")


if __name__ == "__main__":
    unittest.main()
