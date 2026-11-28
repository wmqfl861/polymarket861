"""Compare the published football account with its deployed and proposed replay on a private snapshot."""

import argparse
from contextlib import closing
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import sqlite3
import sys
import tempfile

from polymarket861 import big5_market, ledger_rules, store


ACCOUNT_FIELDS = (
    "schema", "account_policy", "evidence_mode", "as_of", "opening_cash", "limits",
    "total_available_cash", "total_unredeemed_cost", "total_settled_paper_pnl",
    "redemption_events", "leagues",
)
STATUS_FILES = ("account-input.json", "account-status.json", "redemptions.json")
# IDs frozen by the original 2026-11-27 slate. Later openings are compared as well.
ORIGINAL_POSITIONS = frozenset({
    "sea-rom-mon-2026-11-29-rom-no", "fl1-sbr-pfc-2026-11-27-pfc-no",
    "epl-ful-bou-2026-11-29-total-2pt5-under", "sea-fro-par-2026-11-28-par-yes",
    "epl-lee-cov-2026-11-28-cov-yes", "sea-int-gen-2026-11-28-int-no",
})
# SQLite reconstructs its shared-memory index on the private copy.
DATABASE_FILES = ("runtime.sqlite3", "runtime.sqlite3-wal", "runtime.sqlite3-journal")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def stamp(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def database_state(directory):
    return {name: stamp(directory / name) for name in DATABASE_FILES if (directory / name).is_file()}


def copy_store(source, target):
    """Take stable DB/WAL bytes without opening a connection on the deployment."""
    before = database_state(source)
    require(DATABASE_FILES[0] in before, f"missing receipt database in {source}")
    target.mkdir(parents=True, exist_ok=True)
    for name in before:
        shutil.copyfile(source / name, target / name)
    require(before == database_state(source), "receipt database changed during copy; retry")
    with closing(sqlite3.connect(target / DATABASE_FILES[0])) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        require(db.execute("PRAGMA quick_check").fetchone()[0] == "ok", "invalid snapshot database")
        for (digest,) in db.execute("SELECT DISTINCT sha256 FROM observations"):
            original = store.blob_path(source, digest)
            destination = target / original.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, destination)


def indexed(rows, key):
    result = {row[key]: row for row in rows}
    require(len(rows) == len(result), f"duplicate {key}")
    return result


def positions(decisions, intents):
    result = {}
    for key, fill in indexed(decisions, "id").items():
        if fill["outcome"] != "filled":
            continue
        intent = intents[key]
        result[key] = {
            **{k: fill[k] for k in ("fixture_id", "league", "at", "cost_including_cash_fees", "fill")},
            **{k: intent[k] for k in ("condition_id", "token_id")},
            "redemption_cash": None, "settled_paper_pnl": None, "event": None,
        }
    return result


def recorded(status, intents, events):
    result = positions(status["positions"], intents)
    require(len(result) == len(status["positions"]), "published positions include a non-fill")
    for row in status["positions"]:
        pos = result[row["id"]]
        require(all(pos[k] == row[k] for k in ("condition_id", "token_id", "fixture_id")),
                "published position identity differs from account input")
        settlement = row["settlement"]
        event = events.get(pos["token_id"])
        require((event is not None) == (settlement["state"] == "settled")
                and settlement.get("event") == event, "published settlement differs from journal")
        require((row["redemption_cash"] is not None) == (event is not None)
                and (row["settled_paper_pnl"] is not None) == (event is not None),
                "published cash or PnL is missing or contradicts the settlement")
        pos.update(redemption_cash=row["redemption_cash"], settled_paper_pnl=row["settled_paper_pnl"], event=event)
    return {"account": {k: status[k] for k in ACCOUNT_FIELDS}, "positions": result}


def simulated(replay, ledger, intents, events, as_of, root):
    def run(payments, cutoff):
        return replay({**ledger, "redemptions": payments}, as_of=cutoff, data_dir=root)

    payments = sorted(events.values(), key=lambda e: (store.instant(e["at"]), e["id"]))
    report = run(payments, as_of)
    result = positions(report["decisions"], intents)
    by_token = indexed(list(result.values()), "token_id")
    previous = Decimal(0)
    # Replaying successive journal entries isolates each actual credit, including zero payouts
    # and simultaneous resolutions, without assuming how either engine prices a redemption.
    for count, event in enumerate(payments, 1):
        prefix = run(payments[:count], event["at"])
        require(prefix["redemption_events"].get("positions_redeemed", 0) == count,
                f"unverified redemption {event['id']}")
        returned = sum(Decimal(group["cash_from_redemptions"]) for group in prefix["leagues"].values())
        delta = returned - previous
        pos = by_token[event["token_id"]]
        pos.update(redemption_cash=ledger_rules.fmt(delta), event=event,
                   settled_paper_pnl=ledger_rules.fmt(delta - Decimal(pos["cost_including_cash_fees"])))
        previous = returned
    return {"account": {k: report[k] for k in ACCOUNT_FIELDS}, "positions": result}


def compare(account_dir, candidate, scratch, *, required_positions=ORIGINAL_POSITIONS):
    account_dir = account_dir.resolve()
    require(not scratch.resolve().is_relative_to(account_dir), "use a private temporary directory outside the deployment")
    snapshot = scratch / "account"
    snapshot.mkdir(mode=0o700)
    paths = [account_dir / name for name in (*STATUS_FILES, "account_engine.py")]
    before = [stamp(path) for path in paths]
    ledger_bytes, status_bytes, journal_bytes, old_code = [path.read_bytes() for path in paths]
    require(before == [stamp(path) for path in paths], "published files changed during copy; retry")
    ledger, status, journal = (json.loads(raw) for raw in (ledger_bytes, status_bytes, journal_bytes))
    input_digest = hashlib.sha256(ledger_bytes).hexdigest()
    require(status["state"] == "ok" and status["evidence_mode"] == "local_receipts"
            and status["account_input_sha256"] == input_digest, "status does not attest to the recorded inputs")
    require(status["as_of"] == journal["as_of"] and not ledger["redemptions"], "journal cutoff or ownership differs")
    intents = indexed(ledger["intents"], "id")
    events = indexed(journal["active_events"], "token_id")
    expected = recorded(status, intents, events)
    missing = required_positions - expected["positions"].keys()
    require(not missing, f"original positions missing from published account: {sorted(missing)}")
    require(len(events) == status["redemption_events"].get("positions_redeemed", 0),
            "published redemption count differs from journal")
    nested = big5_market.research_root(account_dir)
    roots = [r for r in (account_dir, nested) if (r / DATABASE_FILES[0]).is_file()]
    require(roots, "missing receipt database")
    for root in roots:
        copy_store(root, snapshot / root.relative_to(account_dir))

    candidate_code = candidate.read_bytes()
    results = {}
    for label, code in (("deployed", old_code), ("candidate", candidate_code)):
        script = scratch / (label + "_account_engine.py")
        script.write_bytes(code)
        replay = runpy.run_path(str(script), run_name="account_replay_preflight")["replay"]
        actual = simulated(replay, ledger, intents, events, status["as_of"], snapshot)
        results[label] = {
            "engine_sha256": hashlib.sha256(code).hexdigest(),
            "account_differences": sorted(k for k in ACCOUNT_FIELDS if expected["account"][k] != actual["account"][k]),
            "position_differences": sorted(k for k in expected["positions"].keys() | actual["positions"].keys()
                                           if expected["positions"].get(k) != actual["positions"].get(k)),
        }
    match = all(not part["account_differences"] and not part["position_differences"] for part in results.values())
    return {"state": "match" if match else "mismatch", "as_of": status["as_of"], "account_input_sha256": input_digest,
            "positions_checked": len(expected["positions"]), "redemptions_checked": len(events), **results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-dir", required=True, type=Path, help="deployed account, before replacing its scripts")
    parser.add_argument("--candidate-engine", type=Path, default=Path(__file__).with_name("account_engine.py"))
    args = parser.parse_args()
    try:
        scratch_root = Path(tempfile.gettempdir()).resolve()
        require(not scratch_root.is_relative_to(args.account_dir.resolve()), "TMPDIR must be outside the account")
        with tempfile.TemporaryDirectory(prefix="pm861-football-preflight-", dir=scratch_root) as tmp:
            result = compare(args.account_dir, args.candidate_engine, Path(tmp))
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error, ArithmeticError) as exc:
        result = {"state": "unverified", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["state"] == "match" else 1


if __name__ == "__main__":
    sys.exit(main())
