import hashlib
import json
import tempfile
import unittest
from contextlib import closing

from market_terms_fixture import (
    CONDITION,
    SLUG,
    TOKEN,
    event,
    legacy_total_description,
    receive,
)

from polymarket861 import (
    ledger,
    ledger_rules,
    ledger_terms,
    market_archive,
    market_provider,
    store,
)


class VerifiedTermsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = temp.name
        self.received = store.iso("2025-02-01T11:01:00Z")
        self.at = store.iso("2025-02-01T14:00:00Z")
        self.kickoff = store.iso("2025-02-01T15:00:00Z")

    def capture(self, **kwargs):
        receive(self.root, event(self.kickoff, **kwargs), self.received)
        return ledger_terms.contract_at(self.root, CONDITION, TOKEN, self.at)

    def test_integrity_and_actual_payout_for_both_tokens(self):
        c = self.capture()
        under = ledger_terms.contract_at(self.root, CONDITION, "72", self.at)
        for side in (c, under):
            self.assertEqual(side["special"]["canceled"], "0.5")
            self.assertIsNone(ledger_rules.scenario_payout(side, "postponed"))
            self.assertIsNone(ledger_rules.scenario_payout(side, "no_result"))
        self.assertEqual(
            ledger_rules.scenario_payout(c, "abandoned_official_final", [2, 1]), 1
        )
        self.assertEqual(
            ledger_rules.scenario_payout(under, "abandoned_official_final", [2, 1]), 0
        )
        self.assertEqual(c["fee_schedule"]["source_sha256"], c["rule_source_sha256"])
        with closing(store.connect(self.root)) as db:
            ledger._attest_contract(
                db, self.root, c, store.instant(self.at), store.instant(self.kickoff)
            )
            for change in (
                {"special": c["special"] | {"no_result": "1"}},
                {"special": c["special"] | {"postponed": "0"}},
                {"special": c["special"] | {"canceled": "0"}},
                {
                    "fee_schedule": c["fee_schedule"]
                    | {"asset": "shares", "quantum": "1", "rounding": "down"}
                },
                {
                    "fee_schedule": c["fee_schedule"]
                    | {"effective_at": "2020-01-01T00:00:00Z"}
                },
                {
                    "fee_schedule": c["fee_schedule"]
                    | {"expires_at": "2100-01-01T00:00:00Z"}
                },
                {"fee_schedule": c["fee_schedule"] | {"version": "user-version"}},
                {"rule_profile": c["rule_profile"] | {"no_data_hours": 2}},
                {"rule_source_sha256": "0" * 64},
                {"equal_payout": ".99"},
                {"seconds_delay": 20},
                {"side": "under"},
            ):
                with (
                    self.subTest(change=change),
                    self.assertRaisesRegex(ValueError, "differ"),
                ):
                    ledger._attest_contract(
                        db,
                        self.root,
                        c | change,
                        store.instant(self.at),
                        store.instant(self.kickoff),
                    )
        with self.assertRaises(ValueError):
            ledger_terms.contract_at(
                self.root, CONDITION, TOKEN, "2025-01-31T14:00:00Z"
            )

    def test_fee_receipt_and_immutable_sha_alone_cannot_certify_charging_semantics(
        self,
    ):
        data = event(self.kickoff, fee=True)
        data["markets"][0]["feeSchedule"] = {
            "rate": 0.05,
            "exponent": 1,
            "takerOnly": True,
        }
        receive(self.root, data, self.received)
        store.archive_bytes(
            self.root,
            "polymarket.com",
            b'{"asset":"cash","rounding":"half_up","quantum":0.00001}',
            observed_at=self.received,
            metadata={
                "operation": "fee_document",
                "url": "https://polymarket.com/fees",
            },
        )
        with self.assertRaisesRegex(ValueError, "fee-enabled"):
            ledger_terms.contract_at(self.root, CONDITION, TOKEN, self.at)

    def test_changed_wording_or_mismatched_line_rejected(self):
        desc = legacy_total_description()
        for altered in (
            desc.replace("less than 3", "less than 1"),
            desc.replace("50–50", '"Over"'),
            desc.replace("stoppage time.", "stoppage time plus extra time."),
        ):
            m = market_provider.normalized_markets(
                event(self.kickoff, description=altered)
            )[0]
            m["event_teams"] = event(self.kickoff)["teams"]
            with self.subTest(text=altered), self.assertRaises(ValueError):
                ledger_terms._interpret(m, "over")

    def test_subsequent_gamma_fee_switch_or_unknown_version_stops_execution(self):
        c = self.capture()
        m = market_archive.contracts(self.root, SLUG, as_of=self.at)[0]
        ledger_terms.verify_book_terms(c, m)
        for change in (
            {"fees_enabled": True},
            {"fees_enabled": None},
            {"protocol": "v2"},
            {"seconds_delay": 7},
            {"fee_schedule": {"rate": 0.03}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ledger_terms.verify_book_terms(c, m | change)
        # An arbitrary lecture note and editable normalized metadata carry no authority.
        with closing(store.connect(self.root)) as db, db:
            m["fees_enabled"] = True
            db.execute("UPDATE ma_contracts SET payload=?", (json.dumps(m),))
        with self.assertRaises(ValueError):
            ledger_terms.contract_at(self.root, CONDITION, TOKEN, self.at)

    def test_cancel_payout_for_the_opposite_moneyline_token(self):
        data = event(self.kickoff)
        market = data["markets"][0]
        for selection, predicate, resolved, yes, no in [
            ("hom", "Home wins", '"No"', "0", "1"),
            ("draw", "the game ends in a draw", 'to "Yes"', "1", "0"),
        ]:
            market.update(
                slug=SLUG + "-" + selection,
                sportsMarketType="moneyline",
                outcomes=["Yes", "No"],
                groupItemTitle="Draw" if selection == "draw" else "Home",
            )
            market["description"] = (
                "In the upcoming game, scheduled for February 1, 2025 "
                f'If {predicate}, this market will resolve to "Yes". Otherwise, this market will resolve to "No". '
                "If the game is postponed, this market will remain open until the game has been completed. "
                f"If the game is canceled entirely, with no make-up game, this market will resolve {resolved}. "
                "This market refers only to the outcome within the first 90 minutes of regular play plus stoppage time."
            )
            m = market_provider.normalized_markets(data)[0] | {
                "event_teams": data["teams"]
            }
            self.assertEqual(ledger_terms._interpret(m, "yes")[0]["canceled"], yes)
            self.assertEqual(ledger_terms._interpret(m, "no")[0]["canceled"], no)
            if selection == "hom":
                m["rules"] = m["rules"].replace("Home wins", "Away wins")
                m["rule_sha256"] = hashlib.sha256(m["rules"].encode()).hexdigest()
                with self.assertRaisesRegex(ValueError, "side, participant"):
                    ledger_terms._interpret(m, "yes")
