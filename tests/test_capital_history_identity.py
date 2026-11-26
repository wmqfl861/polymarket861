"""Cash depends on the complete, token-scoped timeline through the requested cutoff."""

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from polymarket861 import (
    budget_archive_proof,
    five_league_budget as risk,
    settlement,
    store,
)
from test_capital_native_proof import received_entry, record_terminal, terminal_row

EARLY = store.iso("2025-02-01T19:00Z")
NEXT = store.iso("2025-02-01T19:01Z")
FINAL = store.iso("2025-02-02T12:00Z")
LATER = store.iso("2025-02-03T12:00Z")


def pending(root, market, index, field, clock):
    token = market["tokens"][index]["token_id"]
    row = dict(terminal_row(market, index, None), resolved=False, closed=False)
    if field == "sparse":
        row = {
            k: row[k] for k in ("token_id", "condition_id", "resolved", "final_price")
        }
    elif field != "empty" and field != "matching":
        wrong = {
            "condition_id": "different",
            "outcome": market["tokens"][1 - index]["outcome"],
            "outcome_index": 1 - index,
            "opposite_token_id": "999",
            "event_slug": "epl-foo-bar-2025-02-01",
            "market_slug": market["slug"] + "-different",
        }
        row[field] = wrong[field]
    data = [] if field == "empty" else [row]
    return record_terminal(root, token, json.dumps({"data": data}).encode(), clock)


def resolved(root, doc, market):
    receipts = []
    for index in (0, 1):
        row = terminal_row(market, index, "0.5")
        receipts.append(
            record_terminal(
                root, row["token_id"], json.dumps({"data": [row]}).encode(), FINAL
            )
        )
    op = doc["intents"][0]
    doc["redemptions"] = [
        {
            "id": "r",
            "fixture_id": op["fixture_id"],
            "condition_id": op["condition_id"],
            "token_id": op["token_id"],
            "at": FINAL,
            "original_sha256": receipts[0],
        }
    ]


class SharedResolutionHistory(unittest.TestCase):
    def test_nonempty_fields_both_sides_and_sparse_or_identical_empty_positives(self):
        cases = [
            ("sparse", [(0, "sparse", EARLY), (1, "sparse", EARLY)], True),
            ("full", [(0, "matching", EARLY), (1, "matching", EARLY)], True),
            ("same-empty", [(0, "empty", EARLY), (1, "empty", EARLY)], True),
            ("reverse-empty", [(1, "empty", EARLY), (0, "empty", EARLY)], True),
            ("distinct-empty", [(0, "empty", EARLY), (1, "empty", NEXT)], True),
        ]
        for side in (0, 1):
            for field in (
                "condition_id",
                "outcome",
                "outcome_index",
                "opposite_token_id",
                "market_slug",
                "event_slug",
            ):
                cases.append((f"{side}-{field}", [(side, field, EARLY)], False))
            for first, last in (
                ("matching", "condition_id"),
                ("condition_id", "matching"),
            ):
                cases.append(
                    (
                        f"{side}-{first}-{last}",
                        [(side, first, EARLY), (side, last, NEXT)],
                        False,
                    )
                )

        for name, history, eligible in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                doc, market = received_entry(root)
                for side, field, stamp in history:
                    pending(root, market, side, field, stamp)
                resolved(root, doc, market)
                report = risk.replay(doc, data_dir=root)
                league = report["leagues"]["epl"]
                self.assertEqual(
                    Decimal(league["cash_from_redemptions"]), 50 if eligible else 0
                )
                self.assertEqual(
                    Decimal(league["unredeemed_cost"]), 0 if eligible else 50
                )
                # Formal readers use exactly the same full timeline. A sparse/latest
                # row cannot erase an earlier claim against this contract.
                with budget_archive_proof.read_archive(root) as (_, db):
                    for side, _, _ in history:
                        token = market["tokens"][side]["token_id"]
                        if eligible:
                            self.assertEqual(
                                settlement._records_from_db(
                                    db, root, token, market=market
                                )["status"],
                                "resolved",
                            )
                        else:
                            with self.assertRaises(ValueError):
                                settlement._records_from_db(
                                    db, root, token, market=market
                                )

    def test_later_changes_only_affect_reports_at_or_after_the_receipt(self):
        for side, kind in ((0, "empty"), (1, "matching"), (1, "outcome")):
            with (
                self.subTest(side=side, kind=kind),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                doc, market = received_entry(root)
                pending(root, market, 0, "empty", EARLY)
                pending(root, market, 1, "empty", EARLY)
                resolved(root, doc, market)
                pending(root, market, side, kind, LATER)
                for when in (EARLY, NEXT):
                    self.assertEqual(
                        Decimal(
                            risk.replay(doc, as_of=when, data_dir=root)["leagues"][
                                "epl"
                            ]["unredeemed_cost"]
                        ),
                        50,
                    )
                self.assertEqual(
                    Decimal(
                        risk.replay(doc, as_of=FINAL, data_dir=root)["leagues"]["epl"][
                            "cash_from_redemptions"
                        ]
                    ),
                    50,
                )
                with self.assertRaises(ValueError):
                    risk.replay(doc, as_of=LATER, data_dir=root)
