"""As-of evidence for linking a regulation final to its registered EPL fixture."""

from collections import Counter
from datetime import timedelta

from . import opportunity, store

SOURCE = "verified-result-pairing"


def unique_fixtures(fixtures):
    def key(f):
        return f["season"], tuple(opportunity._club(f[k]) for k in ("home", "away"))

    counts = Counter(key(f) for f in fixtures)
    return {
        f["fixture_id"]
        for f in fixtures
        if counts[key(f)] == 1 and len(set(key(f)[1])) == 2 and all(key(f)[1])
    }


def _primary(event):
    return {
        "provider": "openligadb",
        "match_id": event["source_id"],
        "round": event["round"],
        "kickoff": event["kickoff"],
        "score": event["score"],
        "received": event["received"],
        "sha256": event["sha256"],
        "url": event["url"],
        "status": event["error"]
        or ("final" if event["score"] is not None else "unfinished"),
        "requested": event["requested"],
    }


def _other(event):
    return {
        "provider": event["source_id"],
        "match_id": event["source_match_id"],
        "round": event["round"],
        "kickoff": event["kickoff"],
        "score": event["score"],
        "received": event["received"],
        "sha256": event["sha256"],
        "url": event["url"],
        "status": event["status"],
        "requested": event.get("started"),
        "references": event.get("revisions", []),
    }


def _final(r):
    score = r["score"]
    return (
        r["status"] == "final"
        and isinstance(score, (tuple, list))
        and len(score) == 2
        and all(type(g) is int and g >= 0 for g in score)
    )


def _repeat(rows, actual):
    """Only separate, original receipts in the current uninterrupted final run count."""
    last = rows[-1]
    fields = ("provider", "match_id", "round", "kickoff", "score", "status")
    if not last["requested"] or not (actual <= last["requested"] <= last["received"]):
        return None
    for first in reversed(rows[:-1]):
        if any(first[k] != last[k] for k in fields) or first["received"] < actual:
            break
        if store.instant(last["requested"]) - store.instant(
            first["received"]
        ) >= timedelta(minutes=30):
            return first
    return None


def decision(f, events, independent, unique, at):
    """Return (proof, blocking reason). A missing independent feed alone is neutral."""
    if not independent:
        return None, None
    if f["fixture_id"] not in unique:
        return None, "ambiguous_local_fixture"
    other = sorted((_other(e) for e in independent), key=lambda e: e["received"])
    native = [_primary(e) for e in events]
    last = other[-1]
    primary = native[-1] if native else None
    times = [store.instant(r["kickoff"]) for r in (last, primary) if r and r["kickoff"]]
    if min(times or [store.instant(f["kickoff"])]) + timedelta(
        minutes=90
    ) > store.instant(at) and not any(_final(r) for r in other + native):
        return None, None
    if any(
        r["provider"] in (None, "", "openligadb", "openfootball")
        or r["provider"] != last["provider"]
        for r in other
    ):
        return None, "ambiguous_independent_source"
    # A published ID must remain associated with one ordered EPL pair and round.
    for history in (native, other):
        if len({str(r["match_id"]) for r in history if r["match_id"] is not None}) > 1:
            return None, "ambiguous_source_match_id"
    if last["status"] in {"withdrawn", "conflict"} or (
        last["status"] != "final" and any(_final(r) for r in other[:-1])
    ):
        return None, "independent_final_changed_or_withdrawn"
    if _final(last) and not last["match_id"]:
        return None, "unconfirmed_independent_match_id"
    if last["status"] not in {"final", "unfinished"}:
        return None, None
    if primary and (
        primary["status"] not in {"unfinished", "final"}
        or primary["status"] == "unfinished"
        and any(_final(r) for r in native[:-1])
    ):
        return None, "primary_final_changed_or_withdrawn"
    if not _final(last) and not (primary and _final(primary) and last["kickoff"]):
        return None, None
    if (
        _final(last)
        and primary
        and _final(primary)
        and primary["score"] != last["score"]
    ):
        return None, "cross_source_score_conflict"
    witnesses = [r for r in (primary, last) if r]
    if any(r["round"] is not None and r["round"] != f["round_id"] for r in witnesses):
        return None, "source_round_disagreement"
    if not any(r["round"] == f["round_id"] for r in witnesses):
        return None, "unconfirmed_result_round"
    precise = {r["kickoff"] for r in witnesses if r["kickoff"]}
    if len(precise) > 1:
        return None, "cross_source_kickoff_conflict"
    if not precise:
        # A score alone can flag a contradiction, but cannot supply match timing.
        return None, None
    actual = store.iso(precise.pop())
    year = int(f["season"][:4])
    if not f"{year}-08-01" <= actual[:10] < f"{year + 1}-08-01":
        return None, "kickoff_outside_epl_season"
    moved = actual != f["kickoff"]
    if moved and (not primary or any(r["kickoff"] is None for r in witnesses)):
        return None, "independent_kickoff_confirmation_missing"
    earliest = store.iso(store.instant(actual) + timedelta(minutes=90))
    if at < earliest or _final(last) and last["received"] < earliest:
        return None, "premature_regulation_final"
    if not _final(last):
        from .result_admission import _pair

        pair = _pair(events)
        if not pair:
            return None, "awaiting_second_receipt"
        score = primary["score"]
        mode = "primary_final_independent_schedule"
        receipts = [_primary(e) for e in pair] + [last]
    elif primary and _final(primary):
        if primary["received"] < earliest:
            return None, "premature_regulation_final"
        score = last["score"]
        mode = "two_providers"
        receipts = witnesses
    else:
        earlier = _repeat(other, earliest)
        if not earlier or not last["match_id"]:
            return None, "awaiting_independent_confirmation"
        # A withdrawn primary final must never be treated as an ordinary slow source.
        score = last["score"]
        mode = "repeated_independent_final"
        receipts = ([primary] if primary else []) + [earlier, last]
    return {
        "format": 2,
        "fixture_id": f["fixture_id"],
        "season": f["season"],
        "round": f["round_id"],
        "registry_kickoff": f["kickoff"],
        "actual_kickoff": actual,
        "score": score,
        "method": mode,
        "receipts": receipts,
    }, None


def signature(proof):
    return (
        proof["fixture_id"],
        proof["round"],
        proof["registry_kickoff"],
        proof["actual_kickoff"],
        proof["score"],
        proof["method"],
        sorted({(r["provider"], str(r["match_id"])) for r in proof["receipts"]}),
    )
