"""Reconcile a selector's historical fixture scope against the independent formal paper account."""

import json
from collections import defaultdict
from datetime import timedelta

from . import ledger as L
from .ledger_rules import digest, instant


def _original_is_a(root, db, row):
    """An omitted opportunity remains part of the universe even without any closing book or payout."""
    try:
        receipt, original = L._formal_receipt(root, db, row)
        intent, first = original["intent"], original["book"]
        cutoff, planned = instant(intent["decision_at"]), instant(intent["kickoff"])
        proof = L._proven_kickoff(root, intent, db=db)
        kickoff = min(planned, instant(proof["actual_kickoff"]))
        sealed = instant(receipt["decision_sealed_at"])
        c = intent["contract"]
        L._attest_contract(db, root, c, sealed, planned)
        target = cutoff + timedelta(seconds=max(5, int(c["seconds_delay"])))
        chosen = L.eligible_book(
            db, root, c, cutoff, kickoff, "first", observed_by=sealed
        )
        if chosen is None or digest(chosen) != digest(first):
            return False
        return L.eligible_book(db, root, c, target, kickoff, "second") is not None
    except (ValueError, KeyError, TypeError, OSError):
        return False


def scope(root, inputs, prospective_source, *, registered=None, source=None):
    """The operator chooses fixtures and one parent account. Its authenticated T−60 parents define the sample."""
    inputs_by_id = {}
    labels = set()
    accounts = set()
    for op in inputs:
        i = op.get("intent") or {}
        labels.update(
            str(x)
            for x in (op.get("match_id"), op.get("market_fixture"), i.get("match_id"))
            if x
        )
        if i and op.get("paper_account"):
            accounts.add(op["paper_account"])
            identity = (op["paper_account"], op.get("intent_id"))
            if identity in inputs_by_id:
                raise ValueError(
                    "duplicate formal parent in historical selection scope"
                )
            inputs_by_id[identity] = op
    pinned = registered or {}
    parent = (
        source
        or pinned.get("source_account")
        or next(iter(accounts), prospective_source)
    )
    if len(accounts) > 1 or accounts - {parent} or parent.startswith("selector."):
        raise ValueError(
            "a historical selection pool must use one independent formal parent account"
        )
    if registered is not None and sorted(labels) != pinned["fixture_labels"]:
        raise ValueError("historical fixture scope differs from its registration")
    missing = []
    current = {}
    with L.connection(root) as db:
        if labels:
            account = db.execute(
                "SELECT mode FROM paper_accounts WHERE account=?", (parent,)
            ).fetchone()
            if (
                account
                and (
                    account["mode"] != "formal"
                    or L._binding(db, parent)["role"] != "main"
                )
            ) or (not account and (accounts or source)):
                raise ValueError(
                    "historical parent account must be an independent formal main account"
                )
        # Slugs and football fixture IDs are both accepted in the input. The Gamma index only
        # locates candidate rows. Their own fixture, native terms, clocks and books still have to verify.
        ids = json.dumps(sorted(labels))
        match = "(i.fixture IN (SELECT value FROM json_each(?)) OR json_extract(i.original,'$.intent.market_slug') IN (SELECT value FROM json_each(?))"
        params = [parent, ids, ids]
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='ma_contracts'"
        ).fetchone():
            match += " OR i.condition_id IN (SELECT json_extract(payload,'$.condition_id') FROM ma_contracts WHERE fixture IN (SELECT value FROM json_each(?)))"
            params.append(ids)
        rows = db.execute(
            "SELECT i.*,a.mode,b.role FROM paper_intents i JOIN paper_accounts a ON a.account=i.account "
            "JOIN paper_account_bindings b ON b.account=i.account WHERE i.account=? AND a.mode='formal' AND b.role='main' AND "
            + match
            + ") "
            "ORDER BY i.fixture,i.cutoff,i.id",
            params,
        )
        for row in rows:
            if row["fixture"] not in labels:
                from .opportunity import verified_fixture

                if not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='ma_contracts'"
                ).fetchone():
                    continue
                slugs = db.execute(
                    "SELECT DISTINCT fixture FROM ma_contracts WHERE json_extract(payload,'$.condition_id')=? "
                    "AND fixture IN (SELECT value FROM json_each(?))",
                    (row["condition_id"], ids),
                ).fetchall()
                for slug in slugs:
                    try:
                        verified_fixture(
                            root,
                            slug[0],
                            row["fixture"],
                            json.loads(row["result"])["sealed_at"],
                        )
                        break
                    except (ValueError, KeyError, TypeError, OSError):
                        continue
                else:
                    continue
            supplied = inputs_by_id.get((parent, row["id"]))
            admitted = (
                supplied is not None
                and L._evidence_tier(root, supplied, "historical")[0] == "A"
            )
            if not admitted and not _original_is_a(root, db, row):
                continue
            record = (
                row["fixture"],
                row["cutoff"],
                row["id"],
                row["fingerprint"],
                bool(json.loads(row["result"]).get("locked_quantity")),
            )
            current[(row["fixture"], row["id"])] = list(record)
            if not admitted:
                missing.append(list(record))
    pinned_records = {(r[0], r[2]): r for r in pinned.get("parents", [])}
    if any(
        key in current and value != current[key]
        for key, value in pinned_records.items()
    ):
        raise ValueError(
            "original formal historical parent differs from its registration"
        )
    parents = dict(pinned_records)
    parents.update(current)
    return {
        "source_account": parent,
        "fixture_labels": sorted(labels),
        "parents": sorted(parents.values()),
        "missing_parents": missing,
    }


def require_complete(audit):
    if audit["missing_parents"]:
        subjects = ", ".join(r[0] + "/" + r[2] for r in audit["missing_parents"])
        raise ValueError(
            "incomplete historical selection pool; missing verified formal T-60 parents: "
            + subjects
        )


def sheet_gaps(db, key, audit):
    """Record a missing historical fixture even when an old filtered sheet contains no trace of it."""
    expected = defaultdict(set)
    parent_counts = defaultdict(int)
    actual = defaultdict(set)
    for fixture, _cut, parent, fingerprint, signal in audit["parents"]:
        parent_counts[fixture] += 1
        if signal:
            expected[fixture].add((parent, fingerprint))
    for row in db.execute(
        "SELECT fixture,sheet FROM selection_sheets WHERE trial=? AND phase='history'",
        (key,),
    ):
        pool = json.loads(row["sheet"])["pool"]
        actual[row["fixture"]].update((v["source_id"], v["source_sha"]) for v in pool)
    rejected = {r[0] for r in audit["missing_parents"]}
    rejected.update(
        f
        for f in parent_counts
        if f not in actual or not expected[f].issubset(actual[f])
    )
    return {f: parent_counts[f] for f in sorted(rejected)}
