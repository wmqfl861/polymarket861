"""Bind an explicit training-only choice to the exact existing native score disagreement."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import timedelta

from . import result_pairings, store
from .football import fixture_id

FACT = ("provider", "match_id", "round", "kickoff", "score", "status")
ORIGINAL = (*FACT, "sha256", "received")


def _fields(record, keys):
    return {k: record[k] for k in keys}


def describe(f, primary, other, reference):
    if not primary or not other:
        return None
    a, b = result_pairings._primary(primary[-1]), result_pairings._other(other[-1])
    if (
        not all(
            result_pairings._final(item) and item["requested"] and item["match_id"]
            for item in (a, b)
        )
        or a["round"] != f["round_id"]
        or b["round"] != a["round"]
        or a["kickoff"] != b["kickoff"]
        or a["score"] == b["score"]
        or not a["kickoff"]
        or any(
            store.instant(item["received"])
            < store.instant(a["kickoff"]) + timedelta(minutes=90)
            or store.instant(item["requested"]) > store.instant(item["received"])
            for item in (a, b)
        )
    ):
        return None
    return {
        "scope": "training_only",
        "fixture_id": f["fixture_id"],
        "openfootball_score": reference,
        "reports": [_fields(a, ORIGINAL), _fields(b, ORIGINAL)],
    }


def evidence(root, year, fid):
    from . import independent_results, result_admission as adm

    at = store.now_utc()
    with closing(adm.database(root)) as db:
        f = next(
            (
                r
                for r in store.eligible_fixture_rows(db, season=adm.scope(year))
                if r["fixture_id"] == fid
            ),
            None,
        )
        if not f:
            raise ValueError("unknown registered EPL fixture")
        primary, errors = adm._raw_rows(root, db, year, at, target=fid)
        if any(r["scope"] in (None, f["round_id"]) for r in errors):
            raise ValueError("cannot verify original source receipts")
        other = independent_results.verified(root, db, year, at)
        reference = adm._reference(db, root, f["season"], at).get(fid)
    return {
        "fixture_id": fid,
        "as_of": at,
        "training_dispute": describe(
            f, primary.get(fid, []), other.get(fid, []), reference
        ),
    }


def validate(r, manifest, f, primary, other, reference, errors):
    if "training_dispute" not in r:
        return
    expected = describe(f, primary, other, reference)
    score = [r["home_goals"], r["away_goals"]]
    if (
        not expected
        or store.canonical(r["training_dispute"]) != store.canonical(expected)
        or score not in [p["score"] for p in expected["reports"]]
        or any(e["scope"] in (None, f["round_id"]) for e in errors)
        or not isinstance(manifest.get("reviewer"), str)
        or not manifest["reviewer"].strip()
        or not isinstance(r.get("decision_reason"), str)
        or not r["decision_reason"].strip()
    ):
        raise ValueError(
            "training_dispute requires the two exact verified conflicting originals, an observed score, a named reviewer and a rationale"
        )


def bound(db, f, prior):
    if not prior or prior["source"] != "manual-regulation":
        return None
    marker = prior["locator"].rpartition(" / batch:")[2]
    if len(marker) != 64:
        return None
    batch = db.execute("SELECT * FROM rf_batches WHERE id=?", (marker,)).fetchone()
    if not batch or batch["rollback"]:
        return None
    try:
        item = next(
            (
                r
                for r in json.loads(batch["detail"])["items"]
                if r["fixture"] == f["fixture_id"]
            ),
            None,
        )
    except (KeyError, ValueError, TypeError):
        return batch, {}  # damaged local review metadata cannot authorize training
    if not item or "training_dispute_sha256" not in item:
        return None
    return batch, item


def still_applies(root, db, f, bound_record, primary, other, reference, at):
    from . import result_admission as adm

    batch, item = bound_record
    try:
        manifest = json.loads(store.read_blob(root, batch["id"]))
        store.read_blob(root, batch["attachment"])
        matches = [
            r
            for r in manifest["matches"]
            if fixture_id(manifest["season"], r["home"], r["away"]) == f["fixture_id"]
        ]
        if len(matches) != 1:
            return False
        binding = matches[0]["training_dispute"]
        if store.digest(binding) != item["training_dispute_sha256"]:
            return False
        reviewed = batch["at"]
        by_provider = (
            [result_pairings._primary(e) for e in primary],
            [result_pairings._other(e) for e in other],
        )
        if (
            not primary
            or not other
            or binding["fixture_id"] != f["fixture_id"]
            or binding["openfootball_score"] != reference
            or binding["scope"] != "training_only"
        ):
            return False
        for original, stream in zip(binding["reports"], by_provider, strict=True):
            then = [event for event in stream if event["received"] <= reviewed]
            if not then or _fields(then[-1], ORIGINAL) != original:
                return False
            if any(
                _fields(event, FACT) != _fields(original, FACT)
                for event in stream
                if event["received"] > reviewed
            ):
                return False
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='football_input_manifests'"
        ).fetchone():
            # A changed reference followed by a later restoration also breaks the
            # original decision. Re-read the reference attached to each knowledge time.
            for (when,) in db.execute(
                "SELECT DISTINCT observed_at FROM football_input_manifests WHERE observed_at>? AND observed_at<=?",
                (reviewed, at),
            ):
                if (
                    adm._reference(db, root, f["season"], when).get(f["fixture_id"])
                    != binding["openfootball_score"]
                ):
                    return False
        return True
    except (OSError, KeyError, TypeError, ValueError):
        return False
