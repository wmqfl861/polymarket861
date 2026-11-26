"""Reproduce the review probe, native-format paper buy and retained archive checks.

Run from the candidate checkout using the project's Python environment:

    PYTHONPATH=src:tests python reports/five-leagues-0.5.0a3/verify-paper-fees.py \
        --baseline ../review05a3/before \
        --archive ../pm861-v05-market-archive-final --archive ../review05a2/live

The baseline can be a read-only worktree of 05ac9d86677b6c9b211c6f7b47915b0605dd4471.
Supply directories that actually contain the retained market captures. The script
reads those directories offline. Controlled network replies use a temporary database.
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from polymarket861 import archive_proof, big5_market, budget_archive_proof as proof
from polymarket861 import fee_registry, five_league_budget, ledger_rules, market_archive, store
from test_budget_domestic_archive import domestic_entry
from test_budget_research_fees import BASIS, PROFILE, command
from test_capital_native_proof import record_terminal, terminal_row


REPO = Path(__file__).resolve().parents[2]
CLAUDE = REPO / "docs/review-evidence/code-review-0.5.0a2-2026-11-26/probe_paper_entry_fee_switch.py"


def original_probe(tree, scratch):
    output = subprocess.check_output([
        sys.executable, "-I", str(CLAUDE), "--src", str(tree / "src"), "--tests", str(tree / "tests"), str(scratch),
    ])
    result = json.loads(output)
    return {k: result[k] for k in ("package_version", "reviewed_fee_policies", "entries_filled")}


def controlled(scratch):
    rows = {}
    for code in five_league_budget.CODES:
        root = scratch / code
        doc, (main, _) = domestic_entry(root, code, fee_schedule=PROFILE)
        intent = doc["intents"][0]
        prepared = command(root, "verify-contract", intent["condition_id"], "--as-of", intent["decision_at"],
                           "--token-id", intent["token_id"], "--entry-basis", BASIS)
        intent.update(prepared["intent_terms"])
        filename = root / "input.json"
        filename.write_text(json.dumps(doc))
        strict = command(root, "replay", str(filename))
        paper = command(root, "replay", str(filename), "--entry-basis", BASIS, "--details")
        trade = paper["decisions"][0]
        archive = big5_market.research_root(root)
        market = market_archive.contracts(archive, main["slug"], as_of=intent["decision_at"])[0]
        at = store.iso(store.instant(intent["kickoff"]) + timedelta(hours=3))
        held_sha = None
        for idx, amount in enumerate([1, 0]):
            row = terminal_row(market, idx, amount)
            sha = record_terminal(archive, row["token_id"], json.dumps({"data": [row]}).encode(), at)
            if idx == 0:
                held_sha = sha
        doc["redemptions"] = [dict(id="terminal", at=at, original_sha256=held_sha,
                                   **{k: intent[k] for k in ("fixture_id", "condition_id", "token_id")})]
        filename.write_text(json.dumps(doc))
        settled = command(root, "replay", str(filename), "--entry-basis", BASIS, "--details")
        country = settled["leagues"][code]
        rows[code] = {
            "default_outcomes": strict["leagues"][code]["outcomes"], "research_outcome": trade["outcome"],
            "entry_basis": trade["entry_evidence"]["basis"], "fee_basis": prepared["fee_basis"],
            "gross_shares": trade["fill"]["gross_shares"], "net_shares": trade["fill"]["net_shares"],
            "buy_cash": trade["cost_including_cash_fees"], "at_risk_before_redemption": paper["leagues"][code]["unredeemed_cost"],
            "net_credited": country["cash_from_redemptions"], "closing_cash": country["available_cash"],
            "settled_paper_pnl": country["settled_paper_pnl_by_fee_basis"]["research_assumption"]["settled_paper_pnl"],
            "venue_orders": settled["verified_orders_at_venue"], "venue_realized_pnl": settled["venue_realized_pnl"],
        }
    return rows


def retained(path):
    by_league, profiles, examples = defaultdict(Counter), Counter(), {}
    seen = set()
    with proof.read_archive(path) as (root, db):
        for row in db.execute("SELECT payload FROM ma_contracts ORDER BY received_at,event_slug,market_slug"):
            indexed = json.loads(row[0])
            condition = indexed["condition_id"]
            if indexed["mapping_error"] or condition in seen:
                continue
            seen.add(condition)
            at = indexed["recorded_at"]
            market = archive_proof.original_market(db, root, condition, at)
            league = market["fixture"].split("-", 1)[0]
            count = by_league[league]
            count["verified_mapped_markets"] += 1
            count["fees_enabled_true"] += int(market["fees_enabled"] is True)
            profiles[ledger_rules.packed(market["fee_schedule"])] += 1
            for t in market["tokens"]:
                token = t["token_id"]
                for tier, scheduled in (("strict", False), ("research", True)):
                    try:
                        terms = proof._terms(root, market, token, at, scheduled=scheduled)
                    except ValueError as exc:
                        count[tier + " gap: " + str(exc)] += 1
                    else:
                        basis = proof._fee_basis(terms["fee_schedule"])
                        count[tier + " terms: " + basis] += 1
                        if league not in examples and market["side"] == "draw":
                            ex = proof.verify_contract(path, condition, at, token=token, scheduled=scheduled)
                            examples[league] = {
                                k: ex[k] for k in ("fixture", "condition_id", "as_of", "source_sha256", "fee_basis", "order_books_checked")
                            } | {"token_id": token, "fee_version": terms["fee_schedule"]["version"]}
        previous, distances = {}, []
        for token, at in db.execute("SELECT token,received_at FROM ma_books ORDER BY token,received_at"):
            when = datetime.fromisoformat(at)
            if token in previous:
                distances.append((when - previous[token]).total_seconds())
            previous[token] = when
        delays = [row[0] for row in db.execute("SELECT DISTINCT json_extract(payload,'$.seconds_delay') FROM ma_contracts WHERE json_extract(payload,'$.mapping_error') IS NULL ORDER BY 1")]
        return {
            "dataset": path.name, "leagues": by_league,
            "raw_fee_profiles": [{"schedule": json.loads(s), "markets": n} for s, n in sorted(profiles.items())],
            "book_receipts": db.execute("SELECT count(*) FROM ma_books").fetchone()[0],
            "distinct_book_tokens": len(previous), "repeat_receipts_for_token": len(distances),
            "shortest_repeat_gap_seconds": min(distances) if distances else None,
            "adjacent_repeat_gaps_5_to_40_seconds": sum(5 <= d <= 40 for d in distances),
            "market_seconds_delay": delays, "draw_examples": examples,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--archive", type=Path, action="append", default=[])
    args = parser.parse_args()
    with TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        report = {
            "scope": "Independent price-taking simulations. Cash and payouts in controlled cases are hypothetical.",
            "claude_probe": {"file": str(CLAUDE.relative_to(REPO)),
                             "baseline": original_probe(args.baseline.resolve(), scratch / "baseline"),
                             "candidate": original_probe(REPO, scratch / "candidate")},
            "controlled_native_collectors_and_cli": controlled(scratch / "controlled"),
            "retained_real_captures": [retained(path) for path in args.archive],
            "reviewed_fee_policies_in_release": len(fee_registry.REVIEWED),
            "original_stress_file_sha256": hashlib.sha256((REPO / "reports/five-leagues-0.5/capital-stress.json").read_bytes()).hexdigest(),
        }
        outfile = scratch / "stress.json"
        command(scratch, "stress", "--output", str(outfile))
        report["regenerated_stress_file_sha256"] = hashlib.sha256(outfile.read_bytes()).hexdigest()
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
