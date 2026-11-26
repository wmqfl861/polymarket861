"""Audit the optional pre-kickoff research basis from archived, native-format receipts."""

from contextlib import redirect_stdout
from copy import deepcopy
from datetime import timedelta
from email.utils import format_datetime
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import test_capital_native_proof as native
import market_terms_fixture
from kickoff_fixture import capture_example
from polymarket861 import (
    cli,
    five_league_budget as budget,
    kickoff_proof,
    ledger_terms,
    market_archive,
    market_provider,
    store,
)


BASIS = "scheduled_with_incomplete_flags"
DECISION = store.instant("2025-02-01T17:00Z")


def capture(root, flags, *, next_flags=None):
    # Use the real Gamma and CLOB collection paths. Only their network replies are controlled.
    main = {}
    collect = native.receive
    take_book = market_archive.snapshot_book
    calls = 0

    def first_event(root, event, at):
        main.update(event)
        main.pop("live")
        main.pop("ended")
        main.update(flags)
        collect(root, main, at)

    def book(root, contract, token):
        nonlocal calls
        if calls == 1 and next_flags is not None:
            when = store.iso(DECISION + timedelta(seconds=3))
            collect(root, main | next_flags, when)
            contract = market_archive.contracts(root, native.SLUG, as_of=when)[0]
        calls += 1
        return take_book(root, contract, token)

    with (
        patch.object(native, "receive", first_event),
        patch.object(market_archive, "snapshot_book", book),
    ):
        doc, _ = native.received_entry(root)
    return doc, main


def replay(root, doc, **kwargs):
    return budget.replay(doc, data_dir=root, details=True, entry_basis=BASIS, **kwargs)


class ScheduleEntryTest(unittest.TestCase):
    def test_each_bound_gamma_response_checks_its_own_http_age_date_and_request(self):
        original = market_terms_fixture.reply
        for tier in ({}, {"live": False, "ended": False}):
            for leg in ("decision", "execution"):
                for problem, expected in (
                    ("age30", "filled"),
                    ("age31", "skipped_unverified_entry"),
                    ("old_date", "skipped_unverified_entry"),
                    ("missing_date", "skipped_unverified_entry"),
                    ("slow", "skipped_unverified_entry"),
                ):
                    with (
                        self.subTest(tier=tier, leg=leg, problem=problem),
                        TemporaryDirectory() as tmp,
                    ):
                        root = Path(tmp)

                        def serve(body, stamp, url):
                            r = original(body, stamp, url)
                            now = store.instant(stamp)
                            if (now > DECISION) != (leg == "execution"):
                                return r
                            h = dict(r.headers)
                            requested = r.requested_at
                            if problem.startswith("age"):
                                h["age"] = problem[3:]
                            elif problem == "old_date":
                                h["date"] = format_datetime(now - timedelta(seconds=31))
                            elif problem == "missing_date":
                                del h["date"]
                            else:
                                requested = store.iso(now - timedelta(seconds=31))
                            return market_provider.Reply(
                                r.body, requested, r.received_at, h, r.url
                            )

                        with patch.object(
                            market_terms_fixture, "reply", side_effect=serve
                        ):
                            doc, _ = capture(
                                root, tier, next_flags={"status": "scheduled"}
                            )
                        for policy in ("explicit_pregame", BASIS) if tier else (BASIS,):
                            report = budget.replay(
                                doc, data_dir=root, entry_basis=policy
                            )
                            self.assertEqual(
                                report["leagues"]["epl"]["outcomes"],
                                {expected: 1},
                                report["proof_gaps"],
                            )
                            if expected != "filled":
                                self.assertIn(
                                    "Gamma parent event lacks a fresh HTTP observation at its receipt time",
                                    report["proof_gaps"],
                                )

    def test_cached_earlier_live_information_still_vetoes_later_schedule_only(self):
        original = market_terms_fixture.reply
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc, event = capture(root, {})

            def old(body, stamp, url):
                r = original(body, stamp, url)
                return market_provider.Reply(
                    r.body,
                    r.requested_at,
                    r.received_at,
                    r.headers | {"age": "86400"},
                    r.url,
                )

            with patch.object(market_terms_fixture, "reply", side_effect=old):
                native.receive(
                    root,
                    event | {"live": True},
                    store.iso(DECISION - timedelta(hours=1)),
                )
            report = replay(root, doc)
            self.assertEqual(
                report["leagues"]["epl"]["outcomes"], {"skipped_unverified_entry": 1}
            )
            self.assertIn(
                "exchange had already observed play before paper entry",
                report["proof_gaps"],
            )

    def test_closed_archived_or_inactive_parent_rejects_each_leg_despite_open_children(
        self,
    ):
        open_event = {"active": True, "closed": False, "archived": False}
        for invalid in ({"closed": True}, {"archived": True}, {"active": False}):
            for flags in ({}, {"live": False, "ended": False}):
                for leg in ("decision", "execution"):
                    with (
                        self.subTest(invalid=invalid, flags=flags, leg=leg),
                        TemporaryDirectory() as tmp,
                    ):
                        root = Path(tmp)
                        start = flags | open_event
                        end = open_event
                        if leg == "decision":
                            start |= invalid
                        else:
                            end = invalid
                        doc, _ = capture(root, start, next_flags=end)
                        for basis in ("explicit_pregame", BASIS):
                            result = budget.replay(
                                doc, data_dir=root, entry_basis=basis
                            )
                            self.assertEqual(
                                result["leagues"]["epl"]["outcomes"],
                                {"skipped_unverified_entry": 1},
                                result,
                            )
                            self.assertEqual(
                                float(result["leagues"]["epl"]["unredeemed_cost"]), 0
                            )

    def test_reopened_parent_cannot_repair_a_book_bound_to_its_closed_original(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc, originally_closed = capture(
                root, {"closed": True, "live": False, "ended": False}
            )
            native.receive(
                root,
                originally_closed | {"closed": False},
                store.iso(DECISION - timedelta(minutes=1)),
            )
            doc["intents"][0]["fee"] = ledger_terms.contract_at(
                root, native.CONDITION, native.TOKEN, DECISION
            )["fee_schedule"]
            # Both native books still cite the older closed parent. Its child contract never changed.
            for basis in ("explicit_pregame", BASIS):
                out = budget.replay(doc, data_dir=root, entry_basis=basis)
                self.assertEqual(
                    out["leagues"]["epl"]["outcomes"], {"skipped_unverified_entry": 1}
                )
                self.assertIn(
                    "unavailable or contrary Gamma parent event at paper entry",
                    out["proof_gaps"],
                )

    def test_postgame_parent_closure_still_redeems_the_original_held_position(self):
        for flags in ({}, {"live": False, "ended": False}):
            with self.subTest(flags=flags), TemporaryDirectory() as tmp:
                root = Path(tmp)
                doc, event = capture(
                    root, flags | {"active": True, "closed": False, "archived": False}
                )
                op = doc["intents"][0]
                market = market_archive.contracts(
                    root, native.SLUG, as_of=op["decision_at"]
                )[0]
                when = store.iso(DECISION + timedelta(days=1))
                native.receive(
                    root,
                    event
                    | {
                        "active": False,
                        "closed": True,
                        "archived": True,
                        "ended": True,
                    },
                    when,
                )
                for i, amount in enumerate(("1", "0")):
                    row = native.terminal_row(market, i, amount)
                    sha = native.record_terminal(
                        root,
                        row["token_id"],
                        json.dumps({"data": [row]}).encode(),
                        when,
                    )
                    if i == 0:
                        doc["redemptions"] = [
                            {
                                "id": "final",
                                "fixture_id": op["fixture_id"],
                                "condition_id": op["condition_id"],
                                "token_id": op["token_id"],
                                "at": when,
                                "original_sha256": sha,
                            }
                        ]
                for basis in ("explicit_pregame", BASIS) if flags else (BASIS,):
                    before = budget.replay(
                        doc, data_dir=root, entry_basis=basis, as_of=op["kickoff"]
                    )
                    self.assertEqual(
                        float(before["leagues"]["epl"]["unredeemed_cost"]), 50
                    )
                    done = budget.replay(doc, data_dir=root, entry_basis=basis)
                    self.assertEqual(
                        done["leagues"]["epl"]["outcomes"], {"filled": 1}, done
                    )
                    self.assertEqual(
                        float(done["leagues"]["epl"]["cash_from_redemptions"]), 100
                    )
                    self.assertEqual(done["redemption_events"]["positions_redeemed"], 1)

    def test_explicit_and_incomplete_both_legs_and_cli_selection(self):
        cases = [
            ({}, None, BASIS, BASIS),
            ({"live": None, "ended": None}, None, BASIS, BASIS),
            ({"live": False}, None, BASIS, BASIS),
            ({"ended": False}, None, BASIS, BASIS),
            ({}, {"live": False, "ended": False}, BASIS, "explicit_pregame"),
            (
                {"live": False, "ended": False},
                None,
                "explicit_pregame",
                "explicit_pregame",
            ),
        ]
        for first, following, a, b in cases:
            with self.subTest(first=first, next=following), TemporaryDirectory() as tmp:
                root = Path(tmp)
                doc, _ = capture(root, first, next_flags=following)
                strict = budget.replay(doc, data_dir=root)
                self.assertNotIn("entry_policy", strict)
                self.assertEqual(
                    strict["leagues"]["epl"]["outcomes"],
                    {"filled": 1}
                    if a == b == "explicit_pregame"
                    else {"skipped_unverified_entry": 1},
                )
                report = replay(root, doc)
                self.assertEqual(
                    report["leagues"]["epl"]["outcomes"],
                    {"filled": 1},
                    report["proof_gaps"],
                )
                evidence = report["decisions"][0]["entry_evidence"]
                self.assertEqual((evidence["decision"], evidence["execution"]), (a, b))
                weakest = BASIS if BASIS in (a, b) else "explicit_pregame"
                self.assertEqual(evidence["basis"], weakest)
                self.assertTrue(evidence["schedule"])
                self.assertEqual(
                    [r["source_sha256"] for r in evidence["books"]],
                    [r["source_sha256"] for r in doc["books"]],
                )
                self.assertEqual(
                    report["entry_policy"]["outcomes_by_league_and_evidence"]["epl"],
                    {weakest: {"filled": 1}},
                )
                self.assertEqual(report["verified_orders_at_venue"], 0)
                if not first and following is None:
                    input_file = root / "entry.json"
                    input_file.write_text(json.dumps(doc), encoding="utf-8")
                    out = io.StringIO()
                    with redirect_stdout(out):
                        status = cli.main(
                            [
                                "--data-dir",
                                str(root),
                                "paper-risk",
                                "replay",
                                str(input_file),
                                "--entry-basis",
                                BASIS,
                                "--details",
                            ]
                        )
                    self.assertEqual(status, 0)
                    self.assertEqual(json.loads(out.getvalue()), report)
                    bad = deepcopy(doc)
                    bad["intents"][0]["fee"] = {"enabled": False}
                    self.assertEqual(
                        replay(root, bad)["leagues"]["epl"]["outcomes"],
                        {"skipped_unverified_entry": 1},
                    )
                    bad = deepcopy(doc)
                    bad["books"][1]["source_sha256"] = doc["books"][0]["source_sha256"]
                    self.assertEqual(
                        replay(root, bad)["leagues"]["epl"]["outcomes"],
                        {"skipped_unverified_entry": 1},
                    )

    def test_contrary_or_malformed_native_state_is_never_schedule_only(self):
        for flags in (
            {"live": True},
            {"ended": True},
            {"live": "false"},
            {"ended": 0},
            {"gameStatus": "in progress"},
            {"cancelled": True},
        ):
            with self.subTest(flags=flags), TemporaryDirectory() as tmp:
                doc, _ = capture(Path(tmp), flags)
                out = replay(Path(tmp), doc)
                self.assertEqual(
                    out["decisions"][0]["outcome"], "skipped_unverified_entry"
                )
                self.assertEqual(out["leagues"]["epl"]["available_cash"], "2000")

    def test_contrary_state_or_schedule_between_legs_cannot_be_hidden_by_restoration(
        self,
    ):
        for change in (
            {"startTime": store.iso(DECISION + timedelta(hours=2))},
            {"gameStatus": "postponed"},
            {"gameStatus": "live"},
        ):
            with self.subTest(change=change), TemporaryDirectory() as tmp:
                root = Path(tmp)
                doc, original = capture(root, {})
                native.receive(
                    root, original | change, store.iso(DECISION + timedelta(seconds=1))
                )
                native.receive(
                    root, original, store.iso(DECISION + timedelta(seconds=3))
                )
                report = replay(root, doc)
                self.assertEqual(
                    report["leagues"]["epl"]["outcomes"],
                    {"skipped_unverified_entry": 1},
                )
                self.assertEqual(
                    report["entry_policy"]["outcomes_by_league_and_evidence"]["epl"],
                    {"unverified": {"skipped_unverified_entry": 1}},
                )

    def test_early_kickoff_revision_uses_its_receipt_cutoff_and_ordinary_later_live_is_harmless(
        self,
    ):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc, original = capture(root, {})
            after = DECISION + timedelta(hours=2)
            native.receive(root, original | {"live": True}, store.iso(after))
            self.assertEqual(
                replay(root, doc)["leagues"]["epl"]["outcomes"], {"filled": 1}
            )
            when = store.iso(after + timedelta(minutes=5))
            earlier = store.iso(DECISION + timedelta(seconds=5))
            native.receive(root, original | {"startTime": earlier}, when)
            self.assertEqual(
                replay(root, doc, as_of=store.iso(after))["leagues"]["epl"]["outcomes"],
                {"filled": 1},
            )
            for end in (when, None):
                report = replay(root, doc, as_of=end)
                self.assertEqual(
                    report["leagues"]["epl"]["outcomes"],
                    {"skipped_unverified_entry": 1},
                )
                self.assertIn(
                    "later Gamma schedule places paper entry at or after kickoff",
                    report["proof_gaps"],
                )

    def test_synthetic_input_cannot_select_a_research_receipt_basis(self):
        doc = budget.stress_scenario()
        with self.assertRaisesRegex(ValueError, "authentic local receipts"):
            budget.replay(doc, entry_basis=BASIS)
        with self.assertRaisesRegex(ValueError, "unrecognized"):
            budget.replay(doc, entry_basis="user-attested")

    def test_authenticated_earlier_whistle_is_scoped_to_the_match_and_as_of(self):
        with (
            TemporaryDirectory() as tmp,
            patch.dict(kickoff_proof.OPEN_RELEASES, clear=True),
        ):
            root = Path(tmp)
            doc, _ = capture(root, {})
            published = store.iso(DECISION - timedelta(hours=2))
            row = {
                "home": "Home",
                "away": "Away",
                "round_id": 20,
                "season": "2024-25",
                "kickoff": doc["intents"][0]["kickoff"],
                "known_at": published,
                "source": "schedule fixture",
                "locator": "fixture",
            }
            # The soccer data fixture has a different ID from the market's numeric club IDs.
            with patch.object(store, "now_utc", return_value=published):
                unrelated = store.register_fixture(
                    root, row | {"fixture_id": "match-other", "home": "Another"}
                )
                held = store.register_fixture(root, row | {"fixture_id": "match-held"})
            first_receipt = store.iso(DECISION + timedelta(hours=3))
            second_receipt = store.iso(DECISION + timedelta(hours=4))
            early = store.iso(DECISION + timedelta(seconds=5))
            with patch.object(store, "now_utc", return_value=first_receipt):
                capture_example(root, unrelated, 17, early)
            with patch.object(store, "now_utc", return_value=second_receipt):
                capture_example(root, held, 18, early)
            before = replay(root, doc, as_of=first_receipt)
            self.assertEqual(
                before["leagues"]["epl"]["outcomes"],
                {"filled": 1},
                before["proof_gaps"],
            )
            current = replay(root, doc, as_of=second_receipt)
            self.assertEqual(
                current["leagues"]["epl"]["outcomes"], {"skipped_unverified_entry": 1}
            )
            self.assertIn(
                "verified actual kickoff precedes or equals paper entry",
                current["proof_gaps"],
            )
