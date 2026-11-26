"""Deterministic five-league paper capital replay, independent of live accounts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import league_registry, ledger_rules as rules, store
from .football import _write_json

SCHEMA = "five-league-paper-risk-v1"
CODES = tuple(league_registry.PROFILES)
BUCKET = rules.D("2000")
POSITION_LIMIT = rules.MAX_ORDER
FIXTURE_LIMIT = rules.MAX_MATCH
EXCHANGE_URL = "https://data-api.polymarket.com/v2/tokens"


def register(commands):
    group = commands.add_parser("paper-risk", help="五联赛独立模拟资金压力重放")
    sub = group.add_subparsers(dest="risk_command", required=True)
    demo = sub.add_parser("stress", help="48 场、11 天延迟、含上周占款的合成压力测试")
    demo.add_argument("--write-input", type=Path, help="导出可修改并重放的完整合成输入")
    custom = sub.add_parser("replay", help="重放完整的研究输入")
    custom.add_argument("input", type=Path)
    custom.add_argument("--as-of", help="截止时刻，带 UTC 偏移")
    custom.add_argument("--details", action="store_true", help="包含逐意图的模拟决定")
    for parser in (demo, custom):
        parser.add_argument("--output", type=Path)
        parser.set_defaults(func=_cli)


def _cli(args):
    if args.risk_command == "stress":
        document = stress_scenario()
        if args.write_input:
            _write_json(args.write_input, document)
        result = stress_report(document)
    else:
        document = json.loads(args.input.read_bytes())
        result = replay(document, as_of=args.as_of, data_dir=args.data_dir, details=args.details)
    if args.output:
        _write_json(args.output, result)
    return result


def _indexed(rows, label):
    if not isinstance(rows, list):
        raise ValueError(f"{label} must be a list")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ValueError(f"{label} requires a unique nonempty id")
        key = row["id"]
        if key in indexed and indexed[key] != row:
            raise ValueError(f"conflicting retry for {label} {key}")
        indexed[key] = row
    return indexed


def _scope(fixture):
    parts = str(fixture).split("/")
    if len(parts) != 4 or not all(parts) or parts[0] not in CODES or parts[2] == parts[3]:
        raise ValueError(f"invalid scoped fixture {fixture}")
    league_registry.season_start_year(parts[1])
    return parts[0]


def _fee(row):
    fee = row.get("fee")
    if not isinstance(fee, dict) or type(fee.get("enabled")) is not bool:
        raise ValueError("explicit fee schedule required")
    if fee["enabled"]:
        if (fee.get("model") != "symmetric" or fee.get("asset") not in {"cash", "shares"}
                or fee.get("round_in") not in {"cash", "charged"} or fee.get("rounding") not in rules.ROUNDINGS
                or rules.number(fee.get("exponent")) not in (1, 2)
                or not rules.UNIT <= rules.number(fee.get("quantum")) <= rules.ONE):
            raise ValueError("unsupported paper fee schedule or rounding")
        rules.unit(fee.get("rate"))
    return fee


def _books(rows):
    by_id, remaining, fingerprints = {}, {}, {}
    for bid, book in _indexed(rows, "books").items():
        _scope(book["fixture_id"])
        seen = rules.instant(book["received_at"])
        token, condition = book["token_id"], book["condition_id"]
        if not isinstance(token, str) or not token or not isinstance(condition, str) or not condition:
            raise ValueError("missing book token or condition")
        levels = rules.series(book, "asks") if book.get("asks") else []
        # Different arbitrary IDs or equivalent UTC spellings cannot replenish one observation.
        source = (token, seen)
        signature = (condition, book["fixture_id"], tuple(levels))
        if source in fingerprints and fingerprints[source] != signature:
            raise ValueError("conflicting depth for identical token and receipt")
        fingerprints[source] = signature
        remaining[source] = dict(levels)
        by_id[bid] = (book["fixture_id"], condition, token, seen, source)
    return by_id, remaining


def _original_payout(event, originals, mode, data_dir):
    """Return a payout from the original bytes at its receipt frontier, or a pending reason."""
    sha = event.get("original_sha256")
    original = originals.get(sha)
    if original is None:
        return None, "missing_original"
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("invalid redemption SHA-256")
    received = rules.instant(original["received_at"])
    if received > rules.instant(event["at"]):
        return None, "original_not_yet_received"
    if mode == "synthetic":
        body = original["body"].encode("utf-8")
    else:
        if "body" in original:
            raise ValueError("local receipt replay uses the stored original bytes")
        if data_dir is None:
            raise ValueError("local receipt replay requires the archive data directory")
        archive = Path(data_dir).expanduser()
        path = archive / "runtime.sqlite3"
        if not path.is_file() or not store.blob_path(archive, sha).is_file():
            return None, "missing_local_original"
        body = store.read_blob(archive, sha)
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            receipts = db.execute(
                "SELECT metadata FROM observations WHERE source='polymarket.com' AND sha256=? AND observed_at=?",
                (sha, rules.iso(received)),
            ).fetchall()
        url = original["source"]
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        if (url.split("?", 1)[0] != EXCHANGE_URL
                or ("token_id" in query and query["token_id"] != [event["token_id"]])
                or not any(json.loads(r[0]).get("url") == url for r in receipts)):
            return None, "unverified_local_resolution_receipt"
    if hashlib.sha256(body).hexdigest() != sha:
        raise ValueError("redemption original SHA-256 mismatch")
    payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("unrecognized redemption original")
    matches = [r for r in payload["data"] if isinstance(r, dict) and str(r.get("token_id")) == event["token_id"]]
    if len(matches) != 1 or matches[0].get("condition_id") != event["condition_id"]:
        raise ValueError("ambiguous or mismatched redemption token/condition")
    row = matches[0]
    if row.get("resolved") is False and row.get("final_price") is None:
        return None, "venue_still_pending"
    if row.get("resolved") is not True or row.get("final_price") is None:
        raise ValueError("unrecognized terminal state in redemption original")
    return rules.unit(row["final_price"]), None


def replay(document, *, as_of=None, data_dir=None, details=False):
    """Pure replay of an input set. Exchanging retry order preserves all balances."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError("unsupported five-league paper replay schema")
    mode = document.get("evidence_mode")
    if mode not in {"synthetic", "local_receipts"}:
        raise ValueError("evidence_mode must be synthetic or local_receipts")
    stop = rules.instant(as_of) if as_of is not None else None
    books, depth = _books(document["books"])
    intents = _indexed(document["intents"], "intents")
    redeem = _indexed(document["redemptions"], "redemptions")
    originals = document["originals"]
    if not isinstance(originals, dict):
        raise ValueError("originals must be keyed by exact-byte SHA-256")

    token_scope, fixture_kickoffs = {}, {}
    events = []
    for key, op in intents.items():
        fixture, token, condition = op["fixture_id"], op["token_id"], op["condition_id"]
        _scope(fixture)
        if token_scope.setdefault(token, (fixture, condition)) != (fixture, condition):
            raise ValueError("one token cannot fund two conditions or fixture buckets")
        decision, kickoff = rules.instant(op["decision_at"]), rules.instant(op["kickoff"])
        if fixture_kickoffs.setdefault(fixture, kickoff) != kickoff:
            raise ValueError("conflicting kickoff for one fixture")
        if type(op.get("rank")) is not int or op["rank"] < 0:
            raise ValueError("intent needs a preregistered nonnegative integer rank")
        first, second = (books[op[k]] for k in ("decision_book", "execution_book"))
        if any(book[:3] != (fixture, condition, token) for book in (first, second)):
            raise ValueError("intent and book identity disagree")
        if not first[3] <= decision <= second[3] < kickoff:
            raise ValueError("book receipts, decision and first kickoff are out of order")
        size = rules.number(op["shares"])
        if size <= 0 or size % rules.UNIT or not rules.ZERO < rules.unit(op["limit_price"]) < rules.ONE:
            raise ValueError("invalid requested shares or limit price")
        _fee(op)
        events.append((second[3], 1, op["rank"], key, op))
    for key, event in redeem.items():
        binding = token_scope.get(event["token_id"])
        if binding != (event["fixture_id"], event["condition_id"]):
            raise ValueError("redemption token or fixture outside the intended bucket")
        received = rules.instant(event["at"])
        if received < fixture_kickoffs[event["fixture_id"]]:
            raise ValueError("redemption cannot precede fixture kickoff")
        events.append((received, 0, 0, key, event))

    cash = {code: BUCKET for code in CODES}
    low_cash = dict(cash)
    encumbered = Counter()
    peak = Counter()
    spent = Counter()
    proceeds = Counter()
    exposure = Counter()
    fixture_peak = Counter()
    positions, resolved = {}, {}
    decisions = []
    counts = {code: Counter() for code in CODES}
    observed_fixtures = {code: set() for code in CODES}
    cohorts = {code: {} for code in CODES}
    receipt_counts = Counter()

    for when, action, _, key, op in sorted(events, key=lambda v: v[:4]):
        if stop and when > stop:
            break
        fixture, token = op["fixture_id"], op["token_id"]
        code = _scope(fixture)
        if action == 0:
            amount, reason = _original_payout(op, originals, mode, data_dir)
            if reason:
                receipt_counts[reason] += 1
                continue
            if token in resolved:
                if resolved[token] != amount:
                    raise ValueError("conflicting terminal payout for one token")
                receipt_counts["duplicate_terminal"] += 1
                continue
            resolved[token] = amount
            receipt_counts["terminal_original"] += 1
            for pos in positions.get(token, []):
                receipt_counts["positions_redeemed"] += 1
                credit = (pos["net"] * amount).quantize(rules.UNIT, rounding=ROUND_DOWN)
                cash[code] += credit
                proceeds[code] += credit
                encumbered[code] -= pos["spent"]
                exposure[fixture] -= pos["spent"]
            continue

        cohort = op.get("cohort", "custom")
        if not isinstance(cohort, str) or not cohort:
            raise ValueError("invalid opportunity cohort")
        seen = cohorts[code].setdefault(cohort, {"fixtures": set(), "outcomes": Counter()})
        seen["fixtures"].add(fixture)
        observed_fixtures[code].add(fixture)
        first, second = (books[op[k]][4] for k in ("decision_book", "execution_book"))
        def levels(source):
            return [(p, q) for p, q in sorted(depth[source].items()) if q > 0]
        paired = rules.pair_depth(levels(first), levels(second), rules.unit(op["limit_price"]))
        requested = rules.number(op["shares"])
        fill = rules.price_fill(paired, requested, op["fee"])
        cost = fill["spent"]
        if fill["gross"] < requested:
            outcome = "skipped_depth"
        elif fill["net"] <= 0:
            outcome = "skipped_fee"
        elif cost > POSITION_LIMIT:
            outcome = "skipped_intent_limit"
        elif exposure[fixture] + cost > FIXTURE_LIMIT:
            outcome = "skipped_fixture_limit"
        elif cash[code] < cost:
            outcome = "skipped_cash"
        else:
            outcome = "filled"
            for a, b, used in fill["used"]:
                depth[first][a] -= used
                if second != first:
                    depth[second][b] -= used
            cash[code] -= cost
            low_cash[code] = min(low_cash[code], cash[code])
            spent[code] += cost
            encumbered[code] += cost
            peak[code] = max(peak[code], encumbered[code])
            exposure[fixture] += cost
            fixture_peak[code] = max(fixture_peak[code], exposure[fixture])
            positions.setdefault(token, []).append({"net": fill["net"], "spent": cost})
        counts[code][outcome] += 1
        seen["outcomes"][outcome] += 1
        if details:
            decisions.append({"id": key, "league": code, "fixture_id": fixture, "at": rules.iso(when),
                              "outcome": outcome, "cost_including_cash_fees": rules.fmt(cost) if outcome == "filled" else None})

    summary = {}
    for code in CODES:
        if cash[code] != BUCKET - spent[code] + proceeds[code] or cash[code] < 0 or encumbered[code] < 0:
            raise ValueError("paper capital conservation failed")
        summary[code] = {
            "registered_fixtures": len(observed_fixtures[code]), "outcomes": dict(sorted(counts[code].items())),
            "cohorts": {k: {"fixtures": len(v["fixtures"]), "outcomes": dict(sorted(v["outcomes"].items()))}
                        for k, v in sorted(cohorts[code].items())},
            "opening_cash": rules.fmt(BUCKET), "available_cash": rules.fmt(cash[code]),
            "minimum_cash": rules.fmt(low_cash[code]), "buy_cash_including_fees": rules.fmt(spent[code]),
            "cash_from_redemptions": rules.fmt(proceeds[code]), "unredeemed_cost": rules.fmt(encumbered[code]),
            "peak_unredeemed_cost": rules.fmt(peak[code]),
            "max_fixture_loss_upper_bound": rules.fmt(fixture_peak[code]),
        }
    result = {
        "schema": SCHEMA, "evidence_mode": mode, "as_of": rules.iso(stop) if stop else None,
        "execution_basis": "Research replay of supplied sizes and paired asks; no live orders or return qualification.",
        "limits": {"initial_cash": "10000", "bucket_per_league": "2000", "cost_per_intent": "50",
                   "fixture_unredeemed_cost_upper_bound": "150", "cross_bucket_transfers": False},
        "allocation": "Full size or skip. Each fill consumes both books. At equal times, terminal receipts precede fills, then operator-supplied rank and ID order fills. Cash fees increase the debit; share fees reduce redeemable shares.",
        "risk_basis": "Sum of open entry cash costs conservatively bounds possible net loss for nonnegative payouts. Offsetting positions can have lower actual worst loss.",
        "leagues": summary, "total_available_cash": rules.fmt(sum(cash.values())),
        "total_unredeemed_cost": rules.fmt(sum(encumbered.values(), rules.ZERO)),
        "redemption_events": dict(sorted(receipt_counts.items())),
    }
    if details:
        result["decisions"] = decisions
    return result


def stress_scenario():
    """72 simulated fixtures: 24 carry-ins from the prior round and the target 48."""
    document = {"schema": SCHEMA, "evidence_mode": "synthetic", "books": [], "intents": [],
                "originals": {}, "redemptions": []}
    focus = (10, 10, 9, 10, 9)
    carry = (6, 5, 5, 4, 4)
    fee = {"enabled": True, "model": "symmetric", "rate": "0.04", "exponent": 1,
           "asset": "cash", "round_in": "cash", "quantum": "0.01", "rounding": "half_up"}
    base = datetime(2030, 1, 12, 15, tzinfo=UTC)
    for cohort, num, start in [("prior_round", carry, base - timedelta(days=7)), ("stress_round", focus, base)]:
        for league, count in zip(CODES, num):
            for index in range(count):
                kickoff = start + timedelta(hours=24 * (index % 2) + 2 * (index % 3))
                fixture = f"{league}/2029-30/{cohort}-home-{index:02}/{cohort}-away-{index:02}"
                decision = kickoff - timedelta(hours=1)
                for lane in range(3 if cohort == "prior_round" else 5):
                    token = f"{league}-{cohort}-{index:02}-{lane}"
                    condition = f"condition-{token}"
                    shallow = cohort == "stress_round" and lane == 0 and index % 4 == 0
                    # One full 100-share order costs exactly 50 inclusive of the synthetic fee.
                    asks = [{"price": "0.50" if lane == 4 else "0.49", "size": "130"}]
                    for stage, at in (("decision", decision), ("execution", decision + timedelta(seconds=15))):
                        snapshot = [{"price": "0.49", "size": "45"}] if stage == "execution" and shallow else asks
                        document["books"].append({"id": f"{token}-{stage}", "fixture_id": fixture,
                                                  "condition_id": condition, "token_id": token,
                                                  "received_at": rules.iso(at), "asks": snapshot})
                    document["intents"].append({
                        "id": token, "fixture_id": fixture, "cohort": cohort, "condition_id": condition,
                        "token_id": token, "rank": 10 * index + lane, "decision_at": rules.iso(decision),
                        "kickoff": rules.iso(kickoff), "decision_book": f"{token}-decision",
                        "execution_book": f"{token}-execution", "shares": "100", "limit_price": "0.50",
                        "fee": fee,
                    })
                    pending = cohort == "prior_round" and index == 0 and lane == 0
                    body = rules.packed({"data": [{"condition_id": condition, "token_id": token,
                                                   "resolved": not pending, "final_price": None if pending else "0.50"}]})
                    sha = hashlib.sha256(body.encode()).hexdigest()
                    at = rules.iso(kickoff + timedelta(hours=2, days=11))
                    document["originals"][sha] = {"received_at": at, "body": body}
                    document["redemptions"].append({"id": f"redeem-{token}", "fixture_id": fixture,
                                                    "condition_id": condition, "token_id": token,
                                                    "at": at, "original_sha256": sha})
    return document


def stress_report(document=None):
    document = stress_scenario() if document is None else document
    focus = [op for op in document["intents"] if op["cohort"] == "stress_round"]
    weekend_end = max(rules.instant(op["kickoff"]) for op in focus) + timedelta(hours=2)
    held = replay(document, as_of=weekend_end)
    final = replay(document)
    return {
        "study": "five-league-synthetic-capital-stress-v1",
        "evidence": "All fixtures, paired books, fees, token originals and settlement times are synthetic. No production balances, real orders, executable-return claim or formal approval.",
        "scenario_sha256": rules.digest(document),
        "target_fixtures": len({op["fixture_id"] for op in focus}),
        "carry_in_fixtures": len({op["fixture_id"] for op in document["intents"] if op["cohort"] == "prior_round"}),
        "assumptions": {
            "delay_days_after_assumed_finish": 11, "assumed_minutes_to_finish": 120,
            "prior_round_days_before_focus": 7, "synthetic_payout_per_share": "0.50",
            "pending_tokens": 5, "fee_model": "4% symmetric cash, exponent 1, rounded to cents per price level; synthetic.",
            "carry_in": "Prior-round fills debit each bucket through the same engine. No initial credit or manual balance adjustment.",
        },
        "weekend_before_redemption": held, "after_delayed_receipts": final,
        "reproduce": "pm861 paper-risk stress --write-input scenario.json --output capital-stress.json",
        "replay_input": "pm861 paper-risk replay scenario.json --details",
    }
