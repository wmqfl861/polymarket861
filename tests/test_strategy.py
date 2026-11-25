import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from archive_capture_fixture import exchange_op, forecast_example, formal_example
from polymarket861 import ledger as L
from polymarket861 import kickoff_proof, store
from kickoff_fixture import capture_example
from polymarket861 import strategy as S
from polymarket861.ledger_rules import iso


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        trusted = patch.dict(kickoff_proof.OPEN_RELEASES, clear=True)
        trusted.start()
        self.addCleanup(trusted.stop)
        self.t = datetime(2025, 1, 1, 12, tzinfo=UTC)
        self.clock = patch.object(store, "now_utc", side_effect=lambda: iso(self.t))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        store.connect(self.root).close()

    def op(self, n, season, t, *, round_number=None):
        base = L.example()
        forward = season == "2026-27"
        fixture = f"epl-h{n}-a{n}-{t.date().isoformat()}"
        at = lambda sec: iso(t + timedelta(seconds=sec))
        order = base["intent"]
        c = order["contract"]
        order.update(
            intent_id=fixture,
            match_id=fixture,
            decision_at=at(0),
            kickoff=at(3600),
            model_version="B1",
            prediction_at=at(-2),
            normal_fv=".465",
            quantity="10",
            lineups={
                side: {"confirmed": True, "received_at": at(-400)}
                for side in ("home", "away")
            },
        )
        c.update(
            match_id=fixture,
            token_id=str(n + 1000 * int(forward) + 1),
            condition_id=f"0x{n + 1000 * int(forward) + 1:064x}",
            rule_received_at=at(-500),
            fee_schedule={
                "enabled": False,
                "version": "observed-no-fees",
                "effective_at": at(-1500),
                "received_at": at(-500),
            },
        )
        fx = {
            "fixture_id": fixture,
            "round_id": round_number or 1 + n // 10,
            "season": season,
            "home": f"home{n}",
            "away": f"away{n}",
            "kickoff": at(3600),
            "known_at": iso(min(self.t, t) - timedelta(seconds=10)),
            "source": "test-schedule",
            "locator": "test-schedule",
        }
        with patch.object(store, "now_utc", return_value=fx["known_at"]):
            store.register_fixture(self.root, fx)
        exchange_op(self.root, base, t + timedelta(hours=1), fx["home"], fx["away"])
        base.pop("settlement")
        base.update(
            season=season,
            actual_kickoff=at(3600),
            kickoff_source="test exchange kickoff",
            depth_provenance="self_archive",
        )
        if not forward:
            formal_example(self.root, base)
            capture_example(
                self.root, store.fixture_at(self.root, fixture), n + 1, at(3600)
            )
        return base

    def test_historical_allocations_require_registered_arm_and_full_manifest(self):
        op = self.op(11, "2024-25", self.t - timedelta(days=100))
        L.replay(self.root, [op], account="source-replay")
        S.register_trial(
            self.root,
            "small-budget",
            {"max_orders": 1},
            source_account="champion",
            history_account="source-replay",
            history_inputs=[op],
            season="2026-27",
            first_round=1,
            budget="3.50",
        )
        baseline, candidate = (
            S._arm_account("small-budget", "history", arm)
            for arm in ("baseline", "candidate")
        )
        with self.assertRaisesRegex(ValueError, "manifest"):
            L.replay(self.root, [], account=candidate)
        result = S.history(self.root, "small-budget")
        for arm in ("baseline", "candidate"):
            self.assertEqual(
                (result["arms"][arm]["A"], result["arms"][arm]["filled"]),
                (1, 1),
                result,
            )
        with L.connection(self.root) as db:
            first = json.loads(
                db.execute(
                    "SELECT original FROM paper_intents WHERE account=?", (baseline,)
                ).fetchone()[0]
            )
            second = json.loads(
                db.execute(
                    "SELECT original FROM paper_intents WHERE account=?", (candidate,)
                ).fetchone()[0]
            )
        self.assertNotEqual(first["intent"]["intent_id"], second["intent"]["intent_id"])
        self.assertNotEqual(first["intent"]["budget"], op["intent"]["budget"])
        self.assertEqual(L.number(first["intent"]["budget"]), L.number("3.5"))
        derived = op | {"intent": first["intent"]}
        self.assertEqual(
            L._evidence_tier(self.root, derived, "historical", account=baseline)[0], "A"
        )
        for wrong in ("other", candidate):
            self.assertEqual(
                L._evidence_tier(self.root, derived, "historical", account=wrong)[0],
                "missing",
            )
        changed = op | {"intent": second["intent"] | {"budget": "5"}}
        with self.assertRaisesRegex(ValueError, "manifest"):
            L.replay(self.root, [changed], account=candidate)
        self.assertEqual(
            L._evidence_tier(self.root, changed, "historical", account=candidate)[0],
            "missing",
        )
        self.assertEqual(S.history(self.root, "small-budget"), result)
        verified = S.score(self.root, "small-budget")["history"]
        self.assertEqual(verified["counts"]["paired_fixtures"], 1)
        with L.connection(self.root, True) as db:
            row = db.execute(
                "SELECT id,result FROM paper_intents WHERE account=?", (candidate,)
            ).fetchone()
            corrupt = json.loads(row["result"])
            corrupt["net_shares"] = "999"
            db.execute(
                "UPDATE paper_intents SET result=? WHERE account=? AND id=?",
                (L.packed(corrupt), candidate, row["id"]),
            )
        invalid = S.score(self.root, "small-budget")["history"]
        self.assertEqual(invalid["counts"].get("paired_fixtures", 0), 0)
        self.assertEqual(invalid["missing"]["execution_unverified"], 1)
        with L.connection(self.root, True) as db:
            db.execute(
                "UPDATE paper_intents SET result=? WHERE account=? AND id=?",
                (row["result"], candidate, row["id"]),
            )
        store.blob_path(self.root, op["first_book"]["source_sha256"]).unlink()
        no_book = S.promote(self.root, "small-budget")
        self.assertEqual(
            (
                no_book["decision"],
                no_book["history"]["counts"].get("paired_fixtures", 0),
            ),
            ("remain_shadow", 0),
        )
        self.assertEqual(no_book["history"]["missing"]["entry_evidence_unverified"], 1)

    def test_partial_history_reprices_only_the_remaining_paired_fixtures(self):
        ops = [
            self.op(n, "2024-25", self.t - timedelta(days=150 - n))
            for n in (11, 22, 33, 44)
        ]
        L.replay(self.root, ops, account="origin-replay")
        S.register_trial(
            self.root,
            "partial",
            {"max_orders": 1},
            source_account="main",
            history_account="origin-replay",
            history_inputs=ops,
            season="2026-27",
            first_round=1,
            budget="17",
        )
        S.history(self.root, "partial")
        full = S.score(self.root, "partial")["history"]
        self.assertEqual(full["counts"]["paired_fixtures"], 4)
        arm = S._arm_account("partial", "history", "candidate")
        cash = L.ledger_report(self.root, arm)["cash"]
        # Lose distinct inputs to three genuine fills, after all four have been booked and scored.
        for op, stage in ((ops[0], "first_book"), (ops[1], "second_book")):
            store.blob_path(self.root, op[stage]["source_sha256"]).unlink()
        with L.connection(self.root, True) as db:
            db.execute(
                "DELETE FROM forecast_seals WHERE digest=?",
                (ops[2]["forecast_digest"],),
            )
        scored = S.promote(self.root, "partial")
        partial = scored["history"]
        self.assertEqual(scored["decision"], "remain_shadow")
        self.assertEqual(
            (
                partial["counts"]["paired_fixtures"],
                partial["counts"]["paired_return_opportunities"],
            ),
            (1, 1),
        )
        self.assertEqual(partial["joint_coverage"], 0.25)
        self.assertEqual(partial["missing"]["entry_evidence_unverified"], 3)
        self.assertNotEqual(
            partial["paired_evidence_sha256"], full["paired_evidence_sha256"]
        )
        for side in ("candidate", "baseline"):
            self.assertEqual(
                L.number(partial["paired_spent"][side]) * 4,
                L.number(full["paired_spent"][side]),
            )
            self.assertEqual(
                L.number(partial["paired_contribution"][side]) * 4,
                L.number(full["paired_contribution"][side]),
            )
        self.assertEqual(L.ledger_report(self.root, arm)["cash"], cash)
        # Both arms need the same observation even when only one arm's local execution receipt is corrupt.
        with L.connection(self.root, True) as db:
            row = db.execute(
                "SELECT id,result FROM paper_intents WHERE account=? AND fixture=?",
                (arm, ops[3]["intent"]["match_id"]),
            ).fetchone()
            bad = json.loads(row["result"])
            bad["execution_evidence"] = ops[3]["first_book"]
            db.execute(
                "UPDATE paper_intents SET result=? WHERE account=? AND id=?",
                (L.packed(bad), arm, row["id"]),
            )
        empty = S.score(self.root, "partial")["history"]
        self.assertEqual(empty["counts"].get("paired_fixtures", 0), 0)
        self.assertEqual(empty["joint_coverage"], 0)
        self.assertEqual(empty["missing"]["execution_unverified"], 1)
        self.assertEqual(empty["paired_spent"], {"candidate": "0", "baseline": "0"})
        self.assertEqual(
            empty["paired_contribution"], {"candidate": "0", "baseline": "0"}
        )

    def test_insufficient_history_or_forged_scores_cannot_promote(self):
        L.replay(self.root, [], account="honest", source="historical")
        with self.assertRaisesRegex(ValueError, "original historical ledger"):
            S.register_trial(
                self.root,
                "bad",
                {"families": []},
                source_account="ref",
                history_account="honest",
                history_inputs=[{"score": 999}],
                season="2026-27",
                first_round=1,
                budget=50,
            )
        with self.assertRaisesRegex(ValueError, "selector supports"):
            S.register_trial(
                self.root,
                "score-entry",
                {"clv": 1},
                source_account="ref",
                history_account="honest",
                history_inputs=[],
                season="2026-27",
                first_round=1,
                budget=50,
            )
        trial = S.register_trial(
            self.root,
            "one",
            {"families": []},
            source_account="ref",
            history_account="honest",
            history_inputs=[],
            season="2026-27",
            first_round=1,
            budget="50",
        )
        S.history(self.root, "one")
        verdict = S.promote(self.root, "one")
        self.assertFalse(verdict["qualified"])
        self.assertEqual(verdict["decision"], "remain_shadow")
        self.assertEqual(trial["spec"]["budget"], "50")
        with self.assertRaisesRegex(ValueError, "immutable"):
            S.register_trial(
                self.root,
                "one",
                {"max_orders": 2},
                source_account="ref",
                history_account="honest",
                history_inputs=[],
                season="2026-27",
                first_round=1,
                budget=50,
            )
        self.assertEqual(S.active(self.root), S.recipe({}))

    def test_positive_position_contribution_from_exchange_books_only(self):
        # Compression/printing is separate from primary ledger rows. Skip repeated derived UI
        # report writes in this 220-fixture load test, while using the real cash/fee/settlement paths.
        with patch.object(L, "_publish", return_value=None):
            self.run_protocol()

    def run_protocol(self):
        historical = [
            self.op(n, "2024-25", self.t - timedelta(days=180 - n)) for n in range(100)
        ]
        L.replay(self.root, historical, account="archive", source="historical")
        forward_times = [self.t + timedelta(days=n + 1) for n in range(120)]
        forward = [self.op(n, "2026-27", t, round_number=1+n%38) for n, t in enumerate(forward_times)]
        with L.connection(self.root, True) as db:
            db.execute(
                "INSERT INTO trial_activations VALUES(?,?,?,?)",
                (L.PROTOCOL, iso(self.t), iso(self.t), "{}"),
            )
        L.open_account(self.root, "opportunities", "formal")
        S.register_trial(
            self.root,
            "stand-aside",
            {"max_orders": 0},
            source_account="opportunities",
            history_account="archive",
            history_inputs=historical,
            season="2026-27",
            first_round=1,
            budget="50",
        )
        h = S.history(self.root, "stand-aside")
        self.assertEqual(h["historical_A_fixtures"], 100)
        self.assertEqual(h["arms"]["baseline"]["filled"], 100)
        self.assertEqual(h["arms"]["candidate"]["filled"], 0)

        # The remaining 260 fixtures had no qualified market opportunity. Both arms seal their empty pools.
        empty=[]; decision=self.t+timedelta(hours=12)
        for rnd in range(1,39):
            for n in range(10-(4 if rnd<=6 else 3)):
                fid=f"no-op-{rnd}-{n}"; empty.append(fid)
                store.register_fixture(self.root,dict(fixture_id=fid,season="2026-27",round_id=rnd,home=f"Home {rnd}.{n}",away=f"Away {rnd}.{n}",kickoff=iso(decision+timedelta(hours=1)),known_at=iso(self.t),source="league",locator=fid))
        self.t=decision
        for fid in empty:
            self.assertEqual(S.freeze_fixture(self.root,"stand-aside",fid)["selected"],{"baseline":0,"candidate":0})
        self.t=decision+timedelta(hours=3)
        for fid in empty:
            store.record_result(self.root,dict(fixture_id=fid,home_goals=0,away_goals=0,published_at=iso(self.t),finalized_at=iso(self.t),received_at=iso(self.t),source="league",locator=fid))
        for n, (op, t) in enumerate(zip(forward, forward_times)):
            self.t = t
            order = op["intent"]
            forecast_example(self.root, op)
            out = L.freeze(self.root, "opportunities", order, op["first_book"])
            self.assertEqual(out["status"], "frozen")
            shared = S.freeze_fixture(self.root, "stand-aside", order["match_id"])
            self.assertEqual(shared["selected"], {"baseline": 1, "candidate": 0})
            if n == 0:
                with self.assertRaisesRegex(ValueError, "not selected"):
                    L.freeze(
                        self.root,
                        S._arm_account("stand-aside", "forward", "candidate"),
                        order,
                        op["first_book"],
                    )
            self.t = t + timedelta(seconds=5)
            L.evaluate(
                self.root, "opportunities", order["intent_id"], op["second_book"]
            )
            S.execute(self.root, "stand-aside")
            self.t = t + timedelta(hours=3)
            capture_example(
                self.root,
                store.fixture_at(self.root, order["match_id"]),
                n + 1001,
                op["actual_kickoff"],
            )
            L.record_close(
                self.root,
                "opportunities",
                order["intent_id"],
                op["close_book"],
                op["actual_kickoff"],
            )
            store.record_result(
                self.root,
                {
                    "fixture_id": order["match_id"],
                    "home_goals": 2,
                    "away_goals": 1,
                    "published_at": iso(self.t),
                    "finalized_at": iso(self.t),
                    "received_at": iso(self.t),
                    "source": "independent fixture result",
                    "locator": "test-football",
                },
            )
        result = S.score(self.root, "stand-aside")
        for phase, n in (("history", 100), ("forward", 120)):
            part = result[phase]
            self.assertEqual(part["counts"]["paired_fixtures"], n)
            self.assertEqual(part["counts"]["traded_fixtures"], n)
            self.assertEqual(part["counts"]["candidate_voluntary_skip"], n)
            # Baseline buys ten at .42. Real closing mid is .39. Filtered arm holds cash.
            self.assertAlmostEqual(part["interval"]["difference"], 0.006)
            self.assertGreater(part["interval"]["lower"], 0)
        # Retain all posted cash. A cached passing score must not promote after entry or closing witnesses disappear.
        lost = []
        before_cash = L.ledger_report(self.root, "archive")["cash"]
        unavailable = [(historical[0], "first_book")]
        unavailable.extend((op, "first_book") for op in forward[:2])
        unavailable.extend((op, "second_book") for op in forward[2:4])
        unavailable.extend((op, "close_book") for op in forward[4:7])
        for op, stage in unavailable:
            path = store.blob_path(self.root, op[stage]["source_sha256"])
            lost.append((path, path.read_bytes()))
            path.unlink()
        incomplete = S.promote(self.root, "stand-aside")
        self.assertEqual(incomplete["decision"], "remain_shadow")
        self.assertEqual(incomplete["history"]["counts"]["paired_fixtures"], 99)
        self.assertEqual(
            incomplete["history"]["missing"]["entry_evidence_unverified"], 1
        )
        self.assertEqual(
            incomplete["forward"]["missing"]["entry_evidence_unverified"], 4
        )
        self.assertEqual(incomplete["forward"]["missing"]["missing_close"], 3)
        self.assertAlmostEqual(incomplete["forward"]["joint_coverage"], 113 / 120)
        self.assertEqual(L.ledger_report(self.root, "archive")["cash"], before_cash)
        for path, raw in lost:
            path.write_bytes(raw)
        self.assertTrue(result["qualified"])
        self.assertEqual(S.promote(self.root, "stand-aside")["decision"], "named_approval_required")
        self.assertEqual(S.active(self.root),S.recipe({}))
        switched=S.promote(self.root, "stand-aside",reviewer="trial steward",reason="reviewed both frozen cohorts")
        self.assertEqual(switched["status"], "promoted")
        self.assertEqual(switched["approval"]["reviewer"],"trial steward")
        self.assertEqual(S.active(self.root)["max_orders"], 0)
        with self.assertRaisesRegex(ValueError,"named reviewer"):
            S.rollback(self.root,"stand-aside","test rollback")
        S.rollback(self.root, "stand-aside", "test rollback to incumbent",reviewer="duty steward")
        self.assertEqual(S.status(self.root)["events"][-1]["reviewer"],"duty steward")
        self.assertEqual(S.active(self.root), S.recipe({}))


if __name__ == "__main__":
    unittest.main()
