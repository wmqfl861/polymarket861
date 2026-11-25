"""Export stored news forecasts or reconstruct the partial 0.4.0 public record.

`verify` and `reconstruct` use the standard library alone. All goal probabilities
are summed independently of the application and of the historical review scripts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "reports/news-live-0.4.0.json"
LEGACY_SHA = "1bb16c7f0e742fa53e2c672d764cb5104e2dd55f48bc490a52117e3cc14fe883"
PROTOCOL = "epl-news-probability-replay-v1"
TAIL = 1e-12
ATOL = 1e-12
MARKETS = ("home_win", "draw", "away_win", "total_over_2_5", "home_over_1_5", "away_over_1_5")
FAMILIES = ("total", "home", "away")
ARMS = ("no_news", "with_news")
LEGACY_LINES = [0.5, 1.5, 2.5, 3.5]


def _marginal(rate, n):
    values = [math.exp(-rate)]
    for k in range(1, n + 1):
        values.append(values[-1] * rate / k)
    return values


def _tail(rate, n):
    # Sum positive terms in the upper tail, avoiding 1 - CDF cancellation.
    k = n + 1
    term = math.exp(-rate + k * math.log(rate) - math.lgamma(k + 1))
    total = term
    while term > total * 1e-17:
        k += 1
        term *= rate / k
        total += term
    return total


def _support(home, away):
    larger = max(home, away)
    n = max(3, math.floor(larger))
    while _tail(larger, n) > TAIL / 4:
        n += 1
    return n


def replay(rates, lines):
    home, away, rho = (rates[k] for k in ("lambda_home", "lambda_away", "rho"))
    if not all(math.isfinite(x) for x in (home, away, rho)) or min(home, away) <= 0:
        raise ValueError("expected finite, positive scoring rates")
    if not lines or sorted(set(lines)) != lines or any(
        not math.isfinite(x) or x < 0.5 or x % 1 != 0.5 for x in lines
    ):
        raise ValueError("expected increasing distinct positive half-goal lines")
    n = _support(home, away)
    weights = {(0, 0): 1 - rho * home * away, (0, 1): 1 + rho * home,
               (1, 0): 1 + rho * away, (1, 1): 1 - rho}
    if min(weights.values()) <= 0:
        raise ValueError("invalid low-score correction")
    home_p, away_p = _marginal(home, n), _marginal(away, n)
    cells = [(h, a, ph * pa * weights.get((h, a), 1.0))
             for h, ph in enumerate(home_p) for a, pa in enumerate(away_p)]
    normalizer = math.fsum(p for _, _, p in cells)
    probs = {key: math.fsum(p for h, a, p in cells if compare(h, a)) / normalizer
             for key, compare in zip(MARKETS[:3],
                                     (lambda h, a: h > a, lambda h, a: h == a,
                                      lambda h, a: h < a), strict=True)}
    totals = {}
    for name, goal in zip(FAMILIES, (lambda h, a: h + a, lambda h, a: h, lambda h, a: a), strict=True):
        goals = [(goal(h, a), p) for h, a, p in cells]
        def over(line, goals=goals):
            return math.fsum(p for g, p in goals if g > line) / normalizer
        primary = 2.5 if name == "total" else 1.5
        probs[f"{name}_over_{str(primary).replace('.', '_')}"] = over(primary)
        totals[name] = {str(line): {"over": over(line),
                                    "under": math.fsum(p for g, p in goals if g < line) / normalizer}
                        for line in lines}
    ht, at = _tail(home, n), _tail(away, n)
    grid = {"max_goals_inclusive": n, "shape": [n + 1, n + 1], "tail_tolerance": TAIL,
            "home_marginal_tail": ht, "away_marginal_tail": at,
            "joint_tail_union_bound": ht + at, "omitted_joint_mass": ht + at - ht * at}
    return {"rates": rates, "adaptive_grid": grid, "probabilities": probs, "totals": totals}


def _infer_rates(target):
    # The four Dixon–Coles adjustments cancel in each marginal. Invert the
    # published team O1.5 values using the same finite-support conditioning.
    def solve_tail(probability, n=None):
        if not 0 < probability < 1:
            raise ValueError("cannot identify a finite scoring rate from this team total")
        def tail(rate):
            if n is None:
                return 1 - math.exp(-rate) * (1 + rate)
            pmf = _marginal(rate, n)
            return math.fsum(pmf[2:]) / math.fsum(pmf)
        lo, hi = 0.0, 2.0
        while tail(hi) < probability:
            hi *= 2
        for _ in range(64):
            mid = (lo + hi) / 2
            if tail(mid) < probability:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    targets = [target["home_over_1_5"], target["away_over_1_5"]]
    n = _support(*(solve_tail(x) for x in targets))
    home, away = (solve_tail(x, n) for x in targets)
    if _support(home, away) != n:
        raise ValueError("public probabilities leave the adaptive support ambiguous")
    hp, ap = _marginal(home, n), _marginal(away, n)
    mass = math.fsum(hp) * math.fsum(ap)
    independent_draw = math.fsum(ph * pa for ph, pa in zip(hp, ap, strict=True))
    rho = (independent_draw - target["draw"] * mass) / (2 * hp[1] * ap[1])
    return {"lambda_home": home, "lambda_away": away, "rho": rho}


def reconstruct(source):
    raw = Path(source).read_bytes()
    if hashlib.sha256(raw).hexdigest() != LEGACY_SHA:
        raise ValueError("expected the exact published 0.4.0 live report")
    legacy = json.loads(raw)
    events = []
    for doc in legacy["event_predictions"]:
        row = {"fixture_id": doc["fixture"], "revision": doc["revision"],
               "receipt_sha256": doc["receipt"], "kickoff": doc["kickoff"], "cutoff": doc["cutoff"],
               "history_manifest_sha256": doc["history_manifest_sha256"], "policy_sha256": doc["news_policy"],
               "lines": LEGACY_LINES, "arms": {}}
        for arm, key in zip(ARMS, ("without_news", "with_news"), strict=True):
            target = {p["market"]: p[key] for p in doc["probabilities"]}
            row["arms"][arm] = replay(_infer_rates(target), LEGACY_LINES) | {"probabilities": target}
        events.append(row)
    ex = legacy["example_seal"]
    early = {"fixture_id": ex["fixture_id"], "kickoff": ex["kickoff"], "cutoff": ex["input_cutoff"],
             "main_seal": ex["main_seal"], "candidate_seal": ex["candidate_seal"], "lines": LEGACY_LINES,
             "arms": {}}
    for arm in ARMS:
        target = ex["probabilities"][arm]
        early["arms"][arm] = replay(_infer_rates(target), LEGACY_LINES) | {
            "probabilities": target, "totals": ex["totals"][arm]}
    return {
        "protocol": PROTOCOL, "source_kind": "inferred_from_historical_public_probabilities",
        "source_report": "reports/news-live-0.4.0.json", "source_report_sha256": LEGACY_SHA,
        "provenance": ("The original ten event receipts exposed six probabilities per arm at placeholder "
                       "kickoffs. The two team O1.5 values identify the scoring rates and the draw identifies "
                       "rho. All 480 event line directions are newly derived. The separate early seal "
                       "also published its actual line vectors. Original stored event rates and line vectors "
                       "were unavailable. This supplement does not attest to the original 600-value audit."),
        "events": events, "separate_early_seal": early,
    }


def _stored_arms(pair, lines):
    arms = {}
    for arm, key in zip(ARMS, ("base_rates", "candidate_rates"), strict=True):
        stored = pair[key]
        rates = None if stored is None else {
            "lambda_home": stored["home"], "lambda_away": stored["away"], "rho": stored["rho"]}
        grid = replay(rates, lines)["adaptive_grid"] if rates is not None else None
        arms[arm] = {"rates": rates, "adaptive_grid": grid, **pair[arm]}
    return {"lines": lines, "policy_sha256": pair["policy_sha256"], "base_model": pair["base_model"],
            "changed": pair["changed"], "applied_claims": len(pair["applied"]),
            "excluded_reasons": pair["excluded_reasons"], "arms": arms}


def _early_seals(db, season, round_id, through, recipe):
    from polymarket861 import pipeline, store

    rows = db.execute("""SELECT * FROM forecast_seals WHERE horizon='early' AND mode='technical'
        AND (model_version='B1' OR model_version LIKE 'B1@%') AND json_extract(data_json,'$.purpose')='main'
        AND json_extract(data_json,'$.season')=? AND json_extract(data_json,'$.round_id')=?
        AND (? IS NULL OR sealed_at<=?) AND (? IS NULL OR json_extract(data_json,'$.recipe_digest')=?)
        ORDER BY fixture_id,kickoff,sealed_at,digest""", (season, round_id, through, through, recipe, recipe))
    result = []
    for row in rows:
        doc = pipeline._verified_seal(row)
        pair = doc.get("news_pair")
        if not isinstance(pair, dict):
            raise ValueError("a selected early seal has no attached news pair; select a news-enabled recipe")
        news = doc["context"]["player_news"]
        if (doc["rates"] != pair["base_rates"] or pair["base_model"] != doc["model"]
                or any(doc[k] != pair["no_news"][k] for k in ("probabilities", "totals"))
                or doc["input_cutoff"] != pair["input_cutoff"] or doc["input_cutoff"] != news["cutoff"]
                or not doc["input_cutoff"] <= doc["sealed_at"] == doc["cutoff"] < doc["kickoff"]
                or doc["fixture_id"] != news["fixture_id"] or store.digest(news["facts"]) != pair["facts_sha256"]
                or doc["evidence"]["input_sha256"] != pair["base_training_sha256"]):
            raise ValueError("early seal and attached news pair disagree on baseline, input clocks or evidence")
        record = {k: doc[k] for k in ("fixture_id", "kickoff", "cutoff", "input_cutoff", "sealed_at",
                                      "mode", "horizon", "purpose", "model_version")}
        record.update(receipt_sha256=doc["digest"], recipe_sha256=doc["recipe_digest"],
                      history_manifest_sha256=doc["input_manifest_sha256"],
                      training_sha256=pair["base_training_sha256"], facts_sha256=pair["facts_sha256"],
                      comparison_frame_sha256=doc["comparison_proof"]["frame"])
        result.append(record | _stored_arms(pair, sorted(map(float, doc["totals"]["total"]))))
    return result


def export_runtime(root, season, round_id, *, pre_release=False, early_seals=False, as_of=None, recipe=None):
    from polymarket861 import news_events, store

    path = Path(root).resolve() / "runtime.sqlite3"
    through = store.iso(as_of) if as_of else None
    metadata = {"protocol": PROTOCOL, "season": season, "round_id": round_id,
                "capture_stage": "pre_release" if pre_release else "unspecified", "as_of": through}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if early_seals:
            seals = _early_seals(db, season, round_id, through, recipe)
            if not seals:
                raise ValueError("no matching technical early main B1 seals")
            return metadata | {
                "source_kind": "verified_stored_technical_early_seals", "required_recipe_sha256": recipe,
                "provenance": ("Numerical values copied from the news arms attached to hash-verified primary B1 "
                               "technical early seals. The unchanged arm equals the sealed primary forecast. "
                               "These unscheduled observations have their own input and sealing clocks."),
                "technical_early_seals": seals}
        if recipe:
            raise ValueError("the recipe filter applies only to early seals")
        # All revisions in the requested cohort, regardless of recency or the UI listing limit.
        rows = db.execute("""SELECT * FROM news_event_updates WHERE json_extract(document,'$.season')=?
            AND json_extract(document,'$.round_id')=? AND (? IS NULL OR json_extract(document,'$.created_at')<=?)
            ORDER BY fixture_id,kickoff,revision""", (season, round_id, through, through))
        docs = [news_events._verified(row) for row in rows]
    if not docs:
        raise ValueError("no stored news event receipts for this season and round")
    events = []
    for doc in docs:
        pair, lines = doc["news_pair"], sorted(map(float, doc["lines"]))
        event = {k: doc[k] for k in ("fixture_id", "kickoff", "cutoff", "created_at", "revision",
                                     "previous_digest", "state_sha256", "history_manifest_sha256",
                                     "recipe_sha256", "training_sha256")}
        event["receipt_sha256"] = doc["digest"]
        events.append(event | _stored_arms(pair, lines))
    return metadata | {"source_kind": "verified_stored_event_documents",
            "provenance": ("Rates, rho and both direction vectors are copied from hash-verified "
                           "immutable event documents. Grid policies and tail bounds were recalculated. "
                           "An empirical B0 fallback has no Dixon–Coles rates. These are event forecasts."),
            "events": events}


def _vector(arm, lines):
    p, totals = arm["probabilities"], arm["totals"]
    if set(p) != set(MARKETS) or set(totals) != set(FAMILIES):
        raise ValueError("missing or extra probability family")
    result = dict(p)
    for family in FAMILIES:
        if set(totals[family]) != {str(line) for line in lines}:
            raise ValueError("configured line set mismatch")
        for line, sides in totals[family].items():
            if set(sides) != {"under", "over"}:
                raise ValueError("missing or extra market direction")
            result.update({f"{family}/{line}/{side}": value for side, value in sides.items()})
    if any(isinstance(v, bool) or not math.isfinite(v) or not 0 <= v <= 1 for v in result.values()):
        raise ValueError("invalid market probability")
    return result


def _verify_events(events):
    trials = comparable = comparisons = 0
    worst = 0.0
    receipts = set()
    for event in events:
        if event.get("receipt_sha256"):
            if event["receipt_sha256"] in receipts:
                raise ValueError("duplicate event receipt")
            receipts.add(event["receipt_sha256"])
        if set(event["arms"]) != set(ARMS):
            raise ValueError("expected both forecast arms")
        for name, arm in event["arms"].items():
            trials += 1
            lines = event["lines"]
            observed = _vector(arm, lines)
            if arm["rates"] is None:
                if event.get("base_model") != "B0" or arm["adaptive_grid"] is not None:
                    raise ValueError("missing rates for a parametric forecast")
                continue
            if set(arm["rates"]) != {"lambda_home", "lambda_away", "rho"} or set(arm["adaptive_grid"]) != {
                "max_goals_inclusive", "shape", "tail_tolerance", "home_marginal_tail",
                "away_marginal_tail", "joint_tail_union_bound", "omitted_joint_mass",
            }:
                raise ValueError("incomplete scoring rates or adaptive tail metadata")
            computed = replay(arm["rates"], lines)
            for k, expected in computed["adaptive_grid"].items():
                actual = arm["adaptive_grid"][k]
                if isinstance(expected, float):
                    ok = math.isclose(actual, expected, rel_tol=1e-12, abs_tol=0)
                else:
                    ok = actual == expected
                if not ok:
                    raise ValueError(f"adaptive grid mismatch for {event['fixture_id']}/{name}/{k}")
            replayed = _vector(computed, lines)
            for k, value in observed.items():
                error = abs(value - replayed[k])
                if error > ATOL:
                    raise ValueError(f"probability mismatch for {event['fixture_id']}/{name}/{k}: {error}")
                worst = max(worst, error)
            comparable += 1
            comparisons += len(observed)
    return {"records": len(events), "distinct_fixtures": len({e["fixture_id"] for e in events}),
            "arms": trials, "replayed_arms": comparable,
            "empirical_arms_without_rates": trials - comparable, "probability_values": comparisons,
            "max_absolute_error": worst}


def verify(report, historical_source=LEGACY):
    name = "technical_early_seals" if report["source_kind"] == "verified_stored_technical_early_seals" else "events"
    records = report.get(name)
    if report["protocol"] != PROTOCOL or not records:
        raise ValueError("unknown or empty replay report")
    summary = _verify_events(records)
    checked = {"absolute_tolerance": ATOL, name: summary}
    if report["source_kind"] == "inferred_from_historical_public_probabilities":
        raw = Path(historical_source).read_bytes()
        if report["source_report_sha256"] != LEGACY_SHA or hashlib.sha256(raw).hexdigest() != LEGACY_SHA:
            raise ValueError("wrong historical source identity")
        source = json.loads(raw)
        originals = {doc["receipt"]: doc for doc in source["event_predictions"]}
        if {row["receipt_sha256"] for row in report["events"]} != set(originals):
            raise ValueError("historical event cohort mismatch")
        for row in report["events"]:
            original = originals[row["receipt_sha256"]]
            if (row["fixture_id"], row["kickoff"], row["cutoff"], row["lines"]) != (
                original["fixture"], original["kickoff"], original["cutoff"], LEGACY_LINES
            ):
                raise ValueError("historical fixture, cutoff or line set mismatch")
            for name, key in zip(ARMS, ("without_news", "with_news"), strict=True):
                if row["arms"][name]["probabilities"] != {p["market"]: p[key] for p in original["probabilities"]}:
                    raise ValueError("historical public probability changed")
        example, early = source["example_seal"], report["separate_early_seal"]
        if (early["main_seal"], early["candidate_seal"], early["fixture_id"], early["cutoff"], early["kickoff"]) != (
            example["main_seal"], example["candidate_seal"], example["fixture_id"], example["input_cutoff"], example["kickoff"]
        ):
            raise ValueError("historical early seal identity mismatch")
        for arm in ARMS:
            if any(early["arms"][arm][field] != example[field][arm] for field in ("probabilities", "totals")):
                raise ValueError("historical early seal probabilities changed")
        checked["separate_early_seal"] = _verify_events([report["separate_early_seal"]])
        checked["events"]["copied_original_probability_values"] = len(originals) * 2 * len(MARKETS)
        checked["events"]["newly_reconstructed_line_directions"] = len(originals) * 2 * 3 * len(LEGACY_LINES) * 2
    elif report["source_kind"] in ("verified_stored_event_documents", "verified_stored_technical_early_seals"):
        stamp = "sealed_at" if name == "technical_early_seals" else "created_at"
        if report.get("as_of") and any(r[stamp] > report["as_of"] for r in records):
            raise ValueError("receipt later than selected as-of clock")
        if name == "technical_early_seals" and any(
            (s["mode"], s["horizon"], s["purpose"]) != ("technical", "early", "main")
            or not s["input_cutoff"] <= s["sealed_at"] == s["cutoff"] < s["kickoff"]
            or (report.get("required_recipe_sha256") and s["recipe_sha256"] != report["required_recipe_sha256"])
            for s in records
        ):
            raise ValueError("early seal cohort mismatch")
        summary["probability_values_compared_to_stored_vectors"] = summary["probability_values"]
        summary["recipe_receipt_counts"] = dict(Counter(e["recipe_sha256"] for e in records))
        summary["policy_receipt_counts"] = dict(Counter(e["policy_sha256"] for e in records))
    else:
        raise ValueError("unknown rate provenance")
    return checked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    legacy = commands.add_parser("reconstruct", help="infer 0.4.0 rates from the six original markets")
    legacy.add_argument("--source", type=Path, default=LEGACY)
    live = commands.add_parser("export", help="copy exact rates and vectors from stored events or technical early seals")
    live.add_argument("data_dir", type=Path)
    live.add_argument("--season", required=True)
    live.add_argument("--round", dest="round_id", type=int, required=True)
    live.add_argument("--pre-release", action="store_true", help="label the collection as a pre-release capture")
    live.add_argument("--as-of", help="include only receipts sealed or created by this UTC timestamp")
    live.add_argument("--early-seals", action="store_true", help="select stored technical early main B1 seals")
    live.add_argument("--recipe", help="select the exact stored primary recipe digest for early seals")
    check = commands.add_parser("verify", help="independently verify every published arm and line")
    check.add_argument("report", type=Path)
    check.add_argument("--historical-source", type=Path, default=LEGACY, help="original 0.4.0 report for provenance checks")
    for command in (legacy, live):
        command.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.mode == "verify":
        result = verify(json.loads(args.report.read_bytes()), args.historical_source)
    else:
        result = (reconstruct(args.source) if args.mode == "reconstruct"
                  else export_runtime(args.data_dir, args.season, args.round_id, pre_release=args.pre_release,
                                      early_seals=args.early_seals, as_of=args.as_of, recipe=args.recipe))
        result["verification"] = verify(result, args.source if args.mode == "reconstruct" else LEGACY)
    body = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if getattr(args, "output", None):
        args.output.write_text(body, encoding="utf-8")
    else:
        print(body, end="")


if __name__ == "__main__":
    main()
