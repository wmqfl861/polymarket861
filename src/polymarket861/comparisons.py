"""Read-only football probability comparisons from native, contemporaneous Gamma/CLOB evidence."""

import json
from collections import Counter, defaultdict
from datetime import timedelta
from statistics import fmean

import numpy as np

from . import (
    archive_proof,
    football,
    kickoff_proof,
    ledger_rules,
    ledger_terms,
    market_provider,
    paired_proof,
    store,
)
from .opportunity import _club

ITEMS = {
    "result": ("home_win", "draw", "away_win"),
    "total": ("total_over_2_5",),
    "home": ("home_over_1_5",),
    "away": ("away_over_1_5",),
}
_BAD = (OSError, ValueError, KeyError, TypeError, ArithmeticError, AttributeError)


def _avg(values):
    return fmean(values) if values else None


def _event(db, root, slug, at):
    rows = db.execute(
        "SELECT * FROM ma_events WHERE slug=? AND received_at=(SELECT MAX(received_at) FROM ma_events WHERE slug=? AND received_at<=?)",
        (slug, slug, at),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("ambiguous_or_missing_gamma_event")
    row = rows[0]
    event = archive_proof._event_capture(db, root, row["sha"], row["received_at"], slug)
    if str(event.get("id")) != row["event_id"] or store.iso(
        event["startTime"]
    ) != store.iso(row["kickoff"]):
        raise ValueError("gamma_identity_or_time_mismatch")
    return event, row


def _participants(event, fixture, slug):
    identity = market_provider.FIXTURE.fullmatch(slug)
    if not identity or store.iso(event["startTime"]) != fixture["kickoff"]:
        return False
    for side, abbr in (("home", identity[2]), ("away", identity[3])):
        rows = [r for r in event.get("teams", []) if r.get("ordering") == side]
        if len(rows) != 1 or _club(fixture[side]) not in {
            _club(rows[0].get(k)) for k in ("name", "alias")
        }:
            return False
        if rows[0].get("abbreviation") and rows[0]["abbreviation"].casefold() != abbr:
            return False
    return True


def _item(m):
    if m["family"] == "moneyline":
        return {"home": "home_win", "draw": "draw", "away": "away_win"}.get(m["side"])
    expected = {
        "total": ("total_over_2_5", 2.5),
        "home_total": ("home_over_1_5", 1.5),
        "away_total": ("away_over_1_5", 1.5),
    }.get(m["family"])
    return (
        expected[0]
        if expected and ledger_rules.number(m["line"]) == expected[1]
        else None
    )


def _catalog(db, root, fixture, at, prospective):
    if prospective:
        known = db.execute(
            "SELECT f.kickoff,f.home,f.away,s.precision FROM fixture_versions f JOIN schedule_receipts s USING(fixture_id,version) "
            "WHERE f.fixture_id=? AND f.known_at<=? AND s.recorded_at<=? ORDER BY f.known_at DESC LIMIT 1",
            (fixture["fixture_id"], at, at),
        ).fetchone()
        if (
            not known
            or known["precision"] == "date"
            or any(known[k] != fixture[k] for k in ("kickoff", "home", "away"))
        ):
            raise ValueError("no_contemporary_local_schedule")
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ma_events'").fetchone():
        raise ValueError("no_gamma_archive")
    slugs = db.execute(
        "SELECT DISTINCT slug FROM ma_events WHERE slug=fixture AND date(kickoff)=date(?) AND received_at<=?",
        (fixture["kickoff"], at),
    )
    matches = []
    for (slug,) in slugs:
        try:
            event, head = _event(db, root, slug, at)
            if _participants(event, fixture, slug):
                matches.append((slug, event, head))
        except _BAD:
            continue
    if len(matches) != 1:
        raise ValueError("no_unique_pregame_fixture_mapping")
    slug, main, head = matches[0]
    events = [(main, head)]
    child = slug + "-more-markets"
    if db.execute(
        "SELECT 1 FROM ma_events WHERE slug=? AND received_at<=?", (child, at)
    ).fetchone():
        other, receipt = _event(db, root, child, at)
        linked = (
            not other.get("teams")
            and main.get("gameId")
            and str(other.get("gameId")) == str(main["gameId"])
        )
        if store.iso(other["startTime"]) != fixture["kickoff"] or not (
            linked or _participants(other, fixture, slug)
        ):
            raise ValueError("auxiliary_fixture_conflict")
        events.append((other, receipt))
    contracts = defaultdict(list)
    for event, row in events:
        for m in market_provider.normalized_markets(event):
            if m["mapping_error"] or m["game_times"] != [fixture["kickoff"]]:
                continue
            key = _item(m)
            if key:
                contracts[key].append(
                    m
                    | {
                        "recorded_at": row["received_at"],
                        "source_sha256": row["sha"],
                        "event_teams": event.get("teams") or [],
                    }
                )
    return slug, contracts


def _compatible(payouts):
    if any(isinstance(p, dict) for p in payouts):
        return all(
            isinstance(p, dict) and p.get("if_decided") is True for p in payouts
        ) and _compatible([p["otherwise"] for p in payouts])
    if all(p == "pending" for p in payouts) or all(
        p == "official_score" for p in payouts
    ):
        return True
    try:
        return sum(ledger_rules.unit(p) for p in payouts) == 1
    except _BAD:
        return False


def _batch(db, root, slug, target, kickoff, before=None):
    lower = store.iso(target - timedelta(seconds=30))
    records = db.execute(
        "SELECT DISTINCT received_at,sha FROM ma_requests WHERE operation='books' AND selector=? AND status='ok' AND received_at BETWEEN ? AND ? AND received_at<? ORDER BY received_at DESC,sha",
        (
            slug,
            lower,
            store.iso(target),
            store.iso(min(before, kickoff) if before else kickoff),
        ),
    ).fetchall()
    if not records:
        old = db.execute(
            "SELECT 1 FROM ma_requests WHERE operation='books' AND selector=? AND status='ok' AND received_at<? LIMIT 1",
            (slug, lower),
        ).fetchone()
        raise ValueError("stale_batch" if old else "no_timely_pregame_batch")
    row = records[0]
    if len(records) > 1 and records[1]["received_at"] == row["received_at"]:
        raise ValueError("ambiguous_batch_at_same_instant")
    reply, _, selector, meta = archive_proof.original_receipt(
        db, root, row["sha"], row["received_at"], {"books"}
    )
    payload = json.loads(reply.body)
    requested = meta.get("request_token_ids")
    if (
        selector != slug
        or not isinstance(requested, list)
        or len(requested) != len(set(requested))
        or not isinstance(payload, list)
        or not market_provider.fresh_observation(reply)
    ):
        raise ValueError("unverified_batch_receipt")
    native = {str(b["asset_id"]): b for b in payload}
    if len(native) != len(payload) or set(native) - set(requested):
        raise ValueError("unexpected_or_duplicate_batch_token")
    return row, reply, native


def _quote(
    db,
    data,
    m,
    outcome,
    batch,
    target,
    kickoff,
    actual,
    as_of_book=False,
    *,
    schedule_reference=None,
):
    row, reply, native = batch
    (token,) = [t["token_id"] for t in m["tokens"] if t["outcome"].lower() == outcome]
    raw = native.get(token)
    if (
        not raw
        or raw.get("market") != m["condition_id"]
        or market_provider.moment(raw["timestamp"]) > store.instant(reply.received_at)
    ):
        raise ValueError("missing_or_wrong_native_token")
    entry = db.execute(
        "SELECT payload FROM ma_books WHERE token=? AND received_at=? AND sha=?",
        (token, row["received_at"], row["sha"]),
    ).fetchone()
    if not entry:
        raise ValueError("missing_indexed_two_sided_book")
    saved = json.loads(entry[0])
    # Replay Gamma as it existed at receipt, including immutable raw origin bytes.
    observed = archive_proof.original_market(
        db,
        data,
        m["condition_id"],
        reply.received_at,
        receipt_at=saved["contract_recorded_at"],
        sha=saved["contract_source_sha256"],
    )
    keys = (
        "fixture",
        "event_slug",
        "game_times",
        "family",
        "side",
        "line",
        "protocol",
        "tokens",
        "rule_sha256",
    )
    # A notice received between this book and the observation boundary can remove
    # the contract, identify in-play, or replace its settlement terms.
    boundary = (
        store.instant(reply.received_at)
        if as_of_book
        else min(target, kickoff - timedelta(microseconds=1))
    )
    current = archive_proof.original_market(db, data, m["condition_id"], boundary)
    if (
        any(current[k] != observed[k] for k in keys)
        or current["live"] is True
        or current["ended"] is True
        or current["market_open"] is not True
    ):
        raise ValueError("market_state_or_terms_changed")
    if any(observed[k] != m[k] for k in keys):
        raise ValueError("contract_changed_since_decision")
    book = market_provider.normalize_book(raw, reply, observed, token, row["sha"])
    if saved != book:
        raise ValueError("derived_book_disagrees_with_original")
    if (
        actual
        and observed["live"] is False
        and observed["ended"] is False
        and store.instant(m["game_times"][0])
        <= store.instant(reply.received_at)
        < kickoff
    ):
        book = book | {"pregame": True}
    if schedule_reference is not None and book["pregame"] is None:
        # The research frame has corroborated the schedule at this exact receipt.
        # No modified book is stored or supplied to a formal mark or execution.
        if not (
            schedule_reference == reply.received_at
            and timedelta(seconds=10)
            <= kickoff - store.instant(reply.received_at)
            <= timedelta(seconds=120)
            and as_of_book
            and not actual
        ):
            raise ValueError("invalid_schedule_reference_window")
        book = book | {"pregame": True}
    special, profile = ledger_terms._interpret(observed, outcome)
    terms = {
        "condition_id": m["condition_id"],
        "token_id": token,
        "rule_sha256": m["rule_sha256"],
        "venue": m["venue"],
        "fee_schedule": None,
    }
    # The close-stage quote checks both sides, spread, tick, clock and open state.
    # Price-only observations have no dependency on executable fee estimates.
    checked = ledger_rules.check_book(book, terms, target, kickoff, "close")
    return {
        "token": token,
        "condition": m["condition_id"],
        "side": outcome,
        "mid": float(checked["mid"]),
        "bid": float(book["bids"][0]["price"]),
        "ask": float(book["asks"][0]["price"]),
        "rule_sha256": m["rule_sha256"],
        "received_at": row["received_at"],
        "batch_sha256": row["sha"],
        "protocol": m["protocol"],
        "gamma_sha256": observed["source_sha256"],
        "profile": profile,
        "special": special,
    }


def frame(
    db,
    root,
    fixture,
    cutoff,
    target,
    kickoff,
    *,
    actual=False,
    before=None,
    prospective=False,
    as_of_book=False,
    schedule_reference=None,
):
    result = {
        "target": store.iso(target),
        "batch": None,
        "received_at": None,
        "probabilities": {},
        "items": {},
        "missing": {},
    }
    try:
        slug, contracts = _catalog(db, root, fixture, store.iso(cutoff), prospective)
        batch = _batch(db, root, slug, target, kickoff, before)
        result.update(
            fixture_slug=slug,
            batch=batch[0]["sha"],
            received_at=batch[0]["received_at"],
        )
    except _BAD as exc:
        result["missing"] = dict.fromkeys(ITEMS, str(exc))
        return result
    for family, keys in ITEMS.items():
        quotes = {}
        try:
            for k in keys:
                if len(contracts[k]) != 1:
                    raise ValueError("missing_or_duplicate_contract")
                market = contracts[k][0]
                sides = ("yes",) if family == "result" else ("over", "under")
                quotes[k] = [
                    _quote(
                        db,
                        root,
                        market,
                        side,
                        batch,
                        target,
                        kickoff,
                        actual,
                        as_of_book,
                        schedule_reference=schedule_reference,
                    )
                    for side in sides
                ]
            if family == "result":
                triple = [quotes[k][0] for k in keys]
                profiles = {
                    (q["profile"]["deadline_utc"], q["profile"]["no_data_hours"])
                    for q in triple
                }
                regimes = [q["special"] for q in triple]
                if (
                    len(profiles) != 1
                    or len({q["protocol"] for q in triple}) != 1
                    or len({q["condition"] for q in triple}) != 3
                    or len({q["token"] for q in triple}) != 3
                    or not all(set(s) == set(regimes[0]) for s in regimes)
                    or not all(
                        _compatible([s[case] for s in regimes]) for case in regimes[0]
                    )
                ):
                    raise ValueError("incompatible_moneyline_rules")
                total = sum(q["mid"] for q in triple)
                result["moneyline_sum"] = total
                result["probabilities"].update(
                    {k: quotes[k][0]["mid"] / total for k in keys}
                )
            else:
                result["probabilities"][keys[0]] = quotes[keys[0]][0]["mid"]
            result["items"][family] = quotes
        except _BAD as exc:
            result["missing"][family] = str(exc)
    return result


def _closing_frame(
    db, root, fixture, cutoff, close, actual, prospective, *, validate_state=None
):
    # Each family uses its latest qualifying native batch. A scheduled-clock
    # study can additionally require an explicit venue state at the book.
    def sample(before=None):
        data = frame(
            db,
            root,
            fixture,
            cutoff,
            close,
            close,
            actual=actual,
            before=before,
            prospective=prospective,
            as_of_book=validate_state is not None,
        )
        receipt = {"received_at": data["received_at"], "batch": data["batch"]}
        if validate_state and data["items"]:
            try:
                receipt["explicit_pregame_event"] = validate_state(
                    data["fixture_slug"], data["received_at"]
                )
            except _BAD as exc:
                data["missing"].update(dict.fromkeys(data["items"], str(exc)))
                data["items"] = {}
        return data, receipt

    last, receipt = sample()
    used = dict.fromkeys(last["items"], receipt)
    boundary = last["received_at"]
    while last["missing"] and boundary:
        older, receipt = sample(store.instant(boundary))
        boundary = older["received_at"]
        for key in older["items"].keys() & last["missing"].keys():
            last["items"][key] = older["items"][key]
            del last["missing"][key]
            used[key] = receipt
    witnesses = {v["batch"]: v["received_at"] for v in used.values()}
    if len(witnesses) == 1:
        last["batch"], last["received_at"] = next(iter(witnesses.items()))
    else:
        last["batch"], last["received_at"] = None, None
    last["family_receipts"] = used
    return last


def _graded(probs, goals):
    items = {}
    truth = football._truth_vector(*goals)
    outcomes = dict(zip(football.PROB_KEYS, truth))
    for family, keys in ITEMS.items():
        if all(k in probs for k in keys):
            error = sum((probs[k] - outcomes[k]) ** 2 for k in keys)
            items[family] = 100 * (1 - error / (2 if family == "result" else 1))
    # Only a complete four-family forecast receives the common four-item score.
    score = (
        football.score_probabilities(probs, *goals)["score"]
        if len(items) == 4
        else None
    )
    return {"score": score, "item_scores": items}


def _baseline(db, entry, body, horizon):
    proof = paired_proof.from_seal(body)
    if not proof or store.digest(
        {k: v for k, v in proof.items() if k != "sha256"}
    ) != proof.get("sha256"):
        return None
    baseline = body.get("baseline_comparator", {})
    if baseline.get("model") == "B0" and (
        baseline.get("input_proof_sha256"),
        baseline.get("training_sha256"),
    ) == (proof["sha256"], proof["training"]):
        return {
            "source": "embedded_same_input",
            "probabilities": baseline["probabilities"],
            "input_proof_sha256": proof["sha256"],
        }
    row = db.execute(
        "SELECT digest,data_json,sealed_at,cutoff FROM forecast_seals WHERE fixture_id=? AND kickoff=? AND mode=? AND horizon=? AND model_version='B0' AND protocol=?",
        (entry["fixture_id"], entry["kickoff"], entry["mode"], horizon, store.PROTOCOL),
    ).fetchone()
    if (
        row
        and (entry["mode"] == "replay" or row["sealed_at"] <= row["cutoff"])
        and row["cutoff"] == entry["cutoff"]
    ):
        other = json.loads(row["data_json"])
        if (
            other.get("model") == "B0"
            and store.digest(other) == row["digest"]
            and paired_proof.from_seal(other) == proof
        ):
            return {
                "source": "separate_same_input_seal",
                "digest": row["digest"],
                "probabilities": other["probabilities"],
                "input_proof_sha256": proof["sha256"],
            }
    return None


def _bounds(probabilities, key, terms):
    normal = probabilities[key] if terms["side"] != "under" else 1 - probabilities[key]
    low, high, _ = ledger_rules.fair_values(normal, terms)
    return float(low), float(high)


def _distance(snapshot, probabilities, baseline, decision):
    items = {}
    for family, grouped in snapshot["items"].items():
        tokens = []
        for key, quotes in grouped.items():
            for q in quotes:
                terms = {
                    "family": "result" if family == "result" else "total",
                    "side": q["side"],
                    "special": q["special"],
                }
                low, high = _bounds(probabilities, key, terms)
                old = next(
                    (
                        o
                        for o in decision["items"].get(family, {}).get(key, [])
                        if (o["token"], o["condition"], o["rule_sha256"])
                        == (q["token"], q["condition"], q["rule_sha256"])
                    ),
                    None,
                )
                tokens.append(
                    {
                        "condition": q["condition"],
                        "token": q["token"],
                        "side": q["side"],
                        "rule_sha256": q["rule_sha256"],
                        "mid": q["mid"],
                        "fair_low": low,
                        "fair_high": high,
                        "squared_distance": (low - q["mid"]) ** 2,
                        "b0_squared_distance": (
                            _bounds(baseline, key, terms)[0] - q["mid"]
                        )
                        ** 2
                        if baseline
                        else None,
                        "market_squared_distance": (old["mid"] - q["mid"]) ** 2
                        if old
                        else None,
                    }
                )
        items[family] = {
            "tokens": tokens,
            "mse": _avg([q["squared_distance"] for q in tokens]),
        }
    return items


def for_fixture(db, root, fixture, seal, horizon, mode):
    if fixture["home_goals"] is None:
        return {"state": "pending_result"}
    goals = (fixture["home_goals"], fixture["away_goals"])
    body, baseline = {}, None
    if seal:
        row = db.execute(
            "SELECT data_json FROM forecast_seals WHERE digest=? AND fixture_id=?",
            (seal["digest"], fixture["fixture_id"]),
        ).fetchone()
        body = json.loads(row[0])
        baseline = _baseline(db, seal, body, horizon)
    kickoff = store.instant(fixture["kickoff"])
    if horizon == "early" and not seal:
        return {"state": "missing_early_cutoff"}
    target = (
        store.instant(seal["cutoff"])
        if seal
        else kickoff
        - (timedelta(hours=24) if horizon == "T-24h" else timedelta(hours=1))
    )
    frozen = store.instant(body["input_cutoff"]) if body.get("input_cutoff") else None
    model = seal["probabilities"] if seal else {}
    result = {
        "state": "graded",
        "decision_at": store.iso(target),
        "input_cutoff": store.iso(frozen) if frozen else None,
        "model": _graded(model, goals),
        "baseline": baseline | _graded(baseline["probabilities"], goals)
        if baseline
        else None,
        "baseline_missing": None if baseline else "no_verified_same_input_b0",
    }
    prospective = mode != "replay"
    invalid_clock = False
    try:
        proof = kickoff_proof.resolve(
            root,
            fixture["fixture_id"],
            kickoff,
            kickoff - timedelta(hours=1),
            db=db,
            prospective=prospective,
        )
    except kickoff_proof.MissingKickoffProof:
        proof, error = None, "no_verified_actual_kickoff"
    except _BAD as exc:
        proof, error, invalid_clock = None, str(exc), True
    else:
        error = None
    closing = store.instant(proof["actual_kickoff"]) if proof else kickoff
    pregame_end = min(kickoff, closing)
    decision = frame(
        db, root, fixture, target, target, pregame_end, prospective=prospective
    )
    decision.update(_graded(decision["probabilities"], goals))
    result["market"] = decision
    if frozen is not None and frozen <= target:
        strict = (
            decision
            if frozen == target
            else frame(
                db, root, fixture, frozen, frozen, pregame_end, prospective=prospective
            )
        )
        result["frozen_market"] = {
            k: v for k, v in strict.items() if k != "items"
        } | _graded(strict["probabilities"], goals)
    else:
        result["frozen_market"] = None
    differences = {}
    for key in ("baseline", "market", "frozen_market"):
        reference = result.get(key) or {}
        available = (
            reference.get("item_scores", {}).keys()
            & result["model"]["item_scores"].keys()
        )
        differences[key] = {
            "score": result["model"]["score"] - reference["score"]
            if result["model"]["score"] is not None
            and reference.get("score") is not None
            else None,
            "by_family": {
                k: result["model"]["item_scores"][k] - reference["item_scores"][k]
                for k in sorted(available)
            },
        }
    result["paired_deltas"] = differences
    if invalid_clock:
        result["close"] = {
            "basis": "invalid_actual_evidence",
            "reason": error,
            "items": {},
        }
        return result
    close = _closing_frame(db, root, fixture, target, closing, bool(proof), prospective)
    result["close"] = {
        "basis": "verified_actual" if proof else "shadow_planned",
        "reason": error,
        "target": store.iso(closing),
        "proof_id": proof["proof_id"] if proof else None,
        "received_at": close["received_at"],
        "batch": close["batch"],
        "family_receipts": close["family_receipts"],
        "missing": close["missing"],
        "items": _distance(
            close, model, baseline["probabilities"] if baseline else None, decision
        )
        if model
        else {},
    }
    return result


def _fixture_interval(values):
    if len(values) < 2:
        return None
    rng = np.random.default_rng(861)
    means = rng.choice(values, size=(2000, len(values)), replace=True).mean(axis=1)
    return np.percentile(means, [2.5, 97.5]).tolist()


def summary(fixtures, expected):
    done = [f["comparison"] for f in fixtures if f["comparison"]["state"] == "graded"]
    pairs = {}
    for ref in ("baseline", "market", "frozen_market"):
        families = {}
        these = [
            r
            for r in done
            if r.get(ref)
            and r[ref]["score"] is not None
            and r["model"]["score"] is not None
        ]
        for k in ITEMS:
            matched = [
                r
                for r in done
                if k in (r.get(ref) or {}).get("item_scores", {})
                and k in r["model"]["item_scores"]
            ]
            families[k] = {
                "paired": len(matched),
                "missing": expected - len(matched),
                "delta": _avg(
                    [
                        r["model"]["item_scores"][k] - r[ref]["item_scores"][k]
                        for r in matched
                    ]
                ),
            }
        pairs[ref] = {
            "paired": len(these),
            "expected": expected,
            "missing": expected - len(these),
            "model_mean": _avg([r["model"]["score"] for r in these]),
            "reference_mean": _avg([r[ref]["score"] for r in these]),
            "delta": _avg([r["model"]["score"] - r[ref]["score"] for r in these]),
            "by_family": families,
        }
    distances = {}
    for basis in ("verified_actual", "shadow_planned"):
        distances[basis] = {}
        for family in ITEMS:
            matches = [
                r["close"]["items"][family]
                for r in done
                if r["close"]["basis"] == basis and family in r["close"]["items"]
            ]
            values = {}
            for col in ("b0_squared_distance", "market_squared_distance"):
                same = [
                    m for m in matches if all(t[col] is not None for t in m["tokens"])
                ]
                values[col] = {
                    "paired": len(same),
                    "reference_mse": _avg(
                        [fmean(t[col] for t in m["tokens"]) for m in same]
                    ),
                    "model_mse": _avg([m["mse"] for m in same]),
                }
            distances[basis][family] = {
                "paired": len(matches),
                "expected": expected,
                "missing": expected - len(matches),
                "tokens": sum(len(m["tokens"]) for m in matches),
                "mse": _avg([m["mse"] for m in matches]),
                "fixture_bootstrap_ci95": _fixture_interval(
                    [m["mse"] for m in matches]
                ),
                "paired_mse": values,
            }
    gaps = {
        key: dict(
            Counter(
                reason
                for r in done
                for reason in (r.get(key) or {}).get("missing", {}).values()
            )
        )
        for key in ("market", "frozen_market", "close")
    }
    return {
        "schema": 1,
        "distance_interval": {
            "cluster": "fixture",
            "resamples": 2000,
            "seed": 861,
            "minimum_fixtures": 2,
        },
        "expected": expected,
        "completed": len(done),
        "pairs": pairs,
        "closing_distance": distances,
        "missing_family_reasons": gaps,
        "method": "Market midpoints use a common native batch received in the preceding 30s. 1X2 Yes is normalized over three compatible contracts. Totals use the Over midpoint with both directions verified. Closing distance is the squared difference to the conservative fair value under 0–1% exceptional outcomes; each fixture carries equal weight within each family.",
    }
