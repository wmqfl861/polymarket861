import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from kickoff_fixture import capture_example, open_release

from polymarket861 import kickoff_proof as K
from polymarket861 import ledger as L
from polymarket861 import store
from polymarket861 import strategy as S


class KickoffTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        grants = patch.dict(K.OPEN_RELEASES, clear=True)
        grants.start()
        self.addCleanup(grants.stop)
        self.clock = "2024-08-16T00:00:00Z"
        clock = patch.object(
            store, "now_utc", side_effect=lambda: store.iso(self.clock)
        )
        clock.start()
        self.addCleanup(clock.stop)
        self.order = L.example()["intent"]
        self.planned = self.order["kickoff"]
        self.cut = self.order["decision_at"]
        self.clock = store.iso(store.instant(self.cut) - timedelta(hours=1))
        self.fx = store.register_fixture(
            self.root,
            {
                "fixture_id": self.order["match_id"],
                "home": "Home",
                "away": "Away",
                "season": "2024/25",
                "round_id": 20,
                "kickoff": self.planned,
                "known_at": self.clock,
                "source": "test",
                "locator": "test",
            },
        )
        self.clock = store.iso(store.instant(self.planned) + timedelta(hours=3))
        self.actual = store.iso(store.instant(self.planned) + timedelta(seconds=42))

    def read(self):
        return K.resolve(
            self.root, self.fx["fixture_id"], self.planned, self.cut, prospective=True
        )

    def test_checked_open_source_conflict_tamper_and_schedule(self):
        key, spec, path, terms = open_release(self.root, self.fx, 17, self.actual)
        with self.assertRaisesRegex(ValueError, "verified openly"):
            K.import_open(
                self.root,
                self.fx["fixture_id"],
                "17",
                key,
                path,
                terms,
                schedule_version=1,
            )
        K.OPEN_RELEASES[key] = spec
        with self.assertRaisesRegex(ValueError, "licence digest"):
            K.import_open(
                self.root,
                self.fx["fixture_id"],
                "17",
                key,
                path,
                path,
                schedule_version=1,
            )
        with self.assertRaisesRegex(ValueError, "publisher export"):
            K.import_open(
                self.root,
                self.fx["fixture_id"],
                "17",
                key,
                terms,
                terms,
                schedule_version=1,
            )
        proof = K.import_open(
            self.root, self.fx["fixture_id"], "17", key, path, terms, schedule_version=1
        )
        self.assertEqual(self.read()["proof_id"], proof["proof_id"])
        self.assertEqual(self.read()["actual_kickoff"], self.actual)
        self.assertEqual(self.read()["schedule_version"], 1)
        with self.assertRaisesRegex(ValueError, "event"):
            K.import_open(
                self.root,
                self.fx["fixture_id"],
                "18",
                key,
                path,
                terms,
                schedule_version=1,
            )
        original = store.blob_path(self.root, proof["source_sha256"])
        original.write_bytes(b"bad")
        with self.assertRaisesRegex(ValueError, "reviewed release"):
            self.read()
        original.write_bytes(path.read_bytes())
        with self.assertRaisesRegex(ValueError, "conflicting"):
            capture_example(
                self.root,
                self.fx,
                18,
                store.iso(store.instant(self.actual) + timedelta(seconds=1)),
            )
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.read()
        new_plan = store.iso(store.instant(self.planned) + timedelta(days=7))
        store.register_fixture(
            self.root, {**self.fx, "known_at": self.clock, "kickoff": new_plan}
        )
        with self.assertRaisesRegex(ValueError, "missing"):
            K.resolve(
                self.root,
                self.fx["fixture_id"],
                new_plan,
                store.instant(new_plan) - timedelta(hours=1),
            )

    def test_only_explicit_independent_first_half_wallclock(self):
        ev = {
            "id": "42",
            "period": {"number": 1},
            "type": {"text": "Kickoff"},
            "text": "First Half begins.",
            "wallclock": self.actual,
        }
        comp = {
            "id": "17",
            "date": self.planned,
            "status": {"type": {"completed": True}},
            "competitors": [
                {"homeAway": s, "team": {"id": s, "displayName": self.fx[s]}}
                for s in ("home", "away")
            ],
        }
        response = {
            "header": {"id": "17", "league": {"slug": "eng.1"}, "competitions": [comp]},
            "keyEvents": [ev],
        }

        def parse(obj):
            return K._parse(json.dumps(obj).encode(), self.fx, "17")

        self.assertEqual(parse(response)["actual_kickoff"], self.actual)
        for change in (
            {"text": "Second Half begins."},
            {"period": {"number": 2}},
            {"wallclock": None},
            {"wallclock": "2024-01-01T12:01Z"},
            {"type": {"text": "Goal"}},
        ):
            with self.assertRaises(ValueError):
                parse({**response, "keyEvents": [ev | change]})
        with self.assertRaises(ValueError):
            parse({**response, "keyEvents": [ev, ev]})
        comp["competitors"][0]["team"]["displayName"] = "Different club"
        with self.assertRaisesRegex(ValueError, "team"):
            parse(response)
        with patch.object(K, "_fetch") as http:
            with self.assertRaisesRegex(ValueError, "permission"):
                K.capture(self.root, self.fx["fixture_id"], "17", schedule_version=1)
            http.assert_not_called()
        # Date overwrites, self-written source labels, and caller times never certify A.
        op = L.example() | {
            "kickoff_source": "reported start",
            "depth_provenance": "self_archive",
        }
        self.assertNotEqual(L._evidence_tier(self.root, op, "historical")[0], "A")

    def test_a_source_manifest_without_an_issuer_verified_free_grant_does_not_authorize_fetch(
        self,
    ):
        from polymarket861 import evidence_sources

        grant_url = "https://publisher.example.test/rights/17"
        terms = store.archive_bytes(
            self.root, "fixture-only-permission", b"Synthetic private written grant."
        )
        source = {
            "source_id": K.SOURCE,
            "upstream": "ESPN",
            "status": "active",
            "free": True,
            "terms_locator": grant_url,
            "fields": {
                "schedule": {
                    "uses": ["trial"],
                    "methods": ["auto"],
                    "allow_free_first_half_event_archive": True,
                    "written_permission_locator": grant_url,
                    "written_permission_sha256": terms["sha256"],
                }
            },
        }
        with patch.object(evidence_sources, "now_utc", side_effect=store.now_utc):
            evidence_sources.register_source(self.root, source)
        with patch.object(K, "_fetch") as fetch:
            with self.assertRaisesRegex(ValueError, "has not been verified"):
                K.capture(self.root, self.fx["fixture_id"], "17", schedule_version=1)
            fetch.assert_not_called()
        timeline = {
            "header": {
                "id": "17",
                "league": {"slug": "eng.1"},
                "competitions": [
                    {
                        "id": "17",
                        "date": self.planned,
                        "status": {"type": {"completed": True}},
                        "competitors": [
                            {
                                "homeAway": side,
                                "team": {"id": side, "displayName": self.fx[side]},
                            }
                            for side in ("home", "away")
                        ],
                    }
                ],
            },
            "keyEvents": [
                {
                    "id": "7",
                    "type": {"text": "Kickoff"},
                    "text": "First Half begins.",
                    "wallclock": self.actual,
                }
            ],
        }
        with (
            patch.dict(K.FREE_ESPN_PERMISSIONS, {terms["sha256"]: grant_url}),
            patch.object(
                K, "_fetch", return_value=json.dumps(timeline).encode()
            ) as fetch,
        ):
            proof = K.capture(
                self.root, self.fx["fixture_id"], "17", schedule_version=1
            )
            fetch.assert_called_once_with(
                "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event=17"
            )
            self.assertEqual(self.read()["proof_id"], proof["proof_id"])
            self.assertEqual(proof["actual_kickoff"], self.actual)

    def test_caller_cannot_turn_an_unexecuted_live_order_into_a_skip(self):
        op = L.example()
        i = op["intent"]
        L.open_account(self.root, "clock", "demo")
        L.freeze(self.root, "clock", i, op["first_book"])
        with L.connection(self.root, True) as db:
            db.execute("UPDATE paper_accounts SET mode='shadow' WHERE account='clock'")
        self.clock = op["second_book"]["received_at"]
        with self.assertRaisesRegex(
            K.MissingKickoffProof, "independent actual kickoff"
        ):
            L.evaluate(
                self.root,
                "clock",
                i["intent_id"],
                op["second_book"],
                actual_kickoff=i["decision_at"],
            )
        self.assertEqual(
            L.ledger_report(self.root, "clock")["intents"][0]["status"], "frozen"
        )
        self.assertEqual(
            L.evaluate(self.root, "clock", i["intent_id"], op["second_book"])["status"],
            "filled",
        )

    def test_finished_independent_event_chooses_the_last_eligible_book(self):
        from archive_capture_fixture import exchange_op

        op = L.example()
        i = op["intent"]
        fixture = "epl-hom-awa-2025-02-01"
        i["match_id"] = fixture
        i["contract"].update(
            match_id=fixture, token_id="71", condition_id="0x" + "5" * 64
        )
        planned = store.instant(i["kickoff"])
        self.clock = store.iso(planned - timedelta(hours=2))
        fx = store.register_fixture(
            self.root, {**self.fx, "fixture_id": fixture, "known_at": self.clock}
        )
        # Deliberately delayed first whistle, 42 seconds after the announced schedule.
        actual = planned + timedelta(seconds=42)
        self.clock = store.iso(actual + timedelta(hours=3))
        op = exchange_op(
            self.root,
            op,
            planned,
            "Home",
            "Away",
            quotes=(
                ("first_book", -1, ".39", ".41"),
                ("second_book", 5, ".40", ".42"),
                ("nominal", 3598, ".16", ".18"),
                ("valid", 3632, ".45", ".47"),
                ("wide", 3640, ".20", ".80"),
            ),
        )
        self.clock = store.iso(actual + timedelta(hours=3))
        proof = capture_example(self.root, fx, 71, actual)
        L.open_account(self.root, "test-depth", "demo")
        L.freeze(self.root, "test-depth", i, op["first_book"])
        self.assertEqual(
            L.evaluate(self.root, "test-depth", i["intent_id"], op["second_book"])[
                "status"
            ],
            "filled",
        )
        with L.connection(self.root, True) as db:
            db.execute(
                "UPDATE paper_accounts SET mode='formal' WHERE account='test-depth'"
            )
        for supplied in (op["nominal"], op["wide"], None):
            mark = L.record_close(self.root, "test-depth", i["intent_id"], supplied)
            self.assertEqual(mark["close_received_at"], op["valid"]["received_at"])
            self.assertEqual(mark["close_mid"], "0.46")
            self.assertEqual(mark["kickoff_proof_id"], proof["proof_id"])
            self.assertEqual(mark["close_grade"], "verified_first_half_event")
        with self.assertRaisesRegex(ValueError, "contradicts"):
            L.record_close(
                self.root, "test-depth", i["intent_id"], op["nominal"], i["kickoff"]
            )
        with L.connection(self.root) as db:
            mid, _ = S._close(
                self.root,
                db,
                "forward",
                "test-depth",
                fixture,
                {"original": {"intent": i}},
                {},
                {self.planned},
            )
        self.assertEqual(str(mid), "0.46")
        before = L.ledger_report(self.root, "test-depth")
        self.assertEqual(before["clv_covered_fills"], 1)
        # A later publisher correction revokes the already visible CLV without changing cash.
        with self.assertRaisesRegex(ValueError, "conflicting"):
            capture_example(self.root, fx, 72, actual + timedelta(seconds=1))
        after = L.ledger_report(self.root, "test-depth")
        self.assertEqual(after["clv_covered_fills"], 0)
        self.assertEqual(after["cash"], before["cash"])

    def test_unproved_formal_close_and_strategy_leave_independent_payout_untouched(
        self,
    ):
        # Use the ordinary synthetic ledger to create a settled test position. Mark its
        # account 'formal' only in this isolated negative test; no forecasts or fills are forged in production.
        op = L.example()
        L.open_account(self.root, "regression", "demo")
        i = op["intent"]
        L.freeze(self.root, "regression", i, op["first_book"])
        L.evaluate(self.root, "regression", i["intent_id"], op["second_book"])
        L.settle(self.root, "regression", op["settlement"])
        L.record_close(
            self.root,
            "regression",
            i["intent_id"],
            op["close_book"],
            op["actual_kickoff"],
        )
        with L.connection(self.root, True) as db:
            db.execute(
                "UPDATE paper_accounts SET mode='formal' WHERE account='regression'"
            )
        before = L.ledger_report(self.root, "regression")
        self.assertEqual(before["clv_covered_fills"], 0)
        for book in (op["second_book"], op["close_book"]):
            fake = store.iso(store.instant(book["received_at"]) + timedelta(seconds=1))
            mark = L.record_close(self.root, "regression", i["intent_id"], book, fake)
            self.assertIsNone(mark["actual_kickoff"])
            self.assertIsNone(mark["clv"])
            with (
                L.connection(self.root) as db,
                self.assertRaisesRegex(ValueError, "proof is missing"),
            ):
                S._close(
                    self.root,
                    db,
                    "forward",
                    "regression",
                    i["match_id"],
                    {"original": {"intent": i}},
                    {},
                    {fake},
                )
        after = L.ledger_report(self.root, "regression")
        self.assertEqual(
            (before["cash"], before["realized_pnl"]),
            (after["cash"], after["realized_pnl"]),
        )
