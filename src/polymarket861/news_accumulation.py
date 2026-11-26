"""Fixed-design descriptive accumulation of comparable prospective news pairs."""

import math
from collections import Counter, defaultdict
from copy import deepcopy
from functools import lru_cache
from statistics import NormalDist, fmean

from scipy.optimize import brentq
from scipy.stats import ncf

from . import experiment_statistics, news_market, news_study_plan

_LEGACY = news_study_plan.ORIGINAL_PLAN
_NORMAL = NormalDist()


def _student_power(shift, critical, df):
    # Squared noncentral t has an F(1, df, shift²) law. Its CDF avoids
    # cancellation/NaN in the near-zero, opposite tail of a noncentral t.
    return float(1 - ncf.cdf(critical**2, 1, df, shift**2))


@lru_cache(maxsize=256)
def _detectable_shift(critical, df):
    target = _LEGACY["target_power"]
    return brentq(lambda x: _student_power(x, critical, df) - target, 0, 4 * critical)


def _power(field, ci, pairs):
    """Planning approximation conditional on variance, never a test or an observed win rate."""
    target = _LEGACY["practical_effect"][field]
    family = _LEGACY["family_comparisons"]
    z80 = _NORMAL.inv_cdf(_LEGACY["target_power"])
    critical = _NORMAL.inv_cdf(1 - (1 - _LEGACY["family_confidence"]) / (2 * family))
    assumptions = _LEGACY["planning_assumptions"]
    size = assumptions["fixtures_per_round"]
    sd = assumptions["single_fixture_sd"][field]
    design_effect = 1 + (size - 1) * assumptions["intraround_correlation"]
    illustrative = math.ceil(design_effect * (sd * (critical + z80) / target) ** 2)
    result = {
        "unit": field,
        "practical_effect": target,
        "target_power": _LEGACY["target_power"],
        "illustrative_matches_at_assumed_variance": illustrative,
        "rounds_with_empirical_variance": ci["rounds"] if ci else 0,
        "mde_at_target_power": None,
        "power_at_practical_effect": None,
        "projected_matches_at_observed_variance": None,
        "method": "current power and MDE use a noncentral-t approximation with empirical round-clustered SE. Projected matches use large-sample normal critical values and assume similar cluster sizes and variance.",
    }
    if ci is None:
        return result
    se = ci["intervals"][0]["standard_error"]
    if se <= 0:
        return result
    df = ci["degrees_of_freedom"]
    mde = se * _detectable_shift(ci["critical_t"], df)
    return result | {
        "mde_at_target_power": mde,
        "power_at_practical_effect": _student_power(target / se, ci["critical_t"], df),
        "projected_matches_at_observed_variance": math.ceil(
            pairs * (se * (critical + z80) / target) ** 2
        ),
    }


def _endpoint(values, field, *, legacy=False):
    groups = defaultdict(list)
    for season, rnd, value in values:
        groups[season, rnd].append([value])
    ci = experiment_statistics.clustered_t(
        groups.values(),
        (_LEGACY if legacy else news_study_plan.PLAN)["family_comparisons"],
    )
    return {
        "pairs": len(values),
        "rounds": len(groups),
        "mean": fmean(v for _, _, v in values) if values else None,
        "interval": (
            {k: v for k, v in ci.items() if k != "intervals"} | ci["intervals"][0]
            if ci
            else None
        ),
        **({"power": _power(field, ci, len(values))} if legacy else {}),
    }


def guardrail(scores):
    interval = scores["interval"]
    if not interval:
        status = "insufficient_independent_rounds"
    elif interval["upper"] < 0:
        status = "evidence_of_score_deterioration"
    elif interval["lower"] == 0 and interval["upper"] == 0:
        status = "no_observed_score_difference"
    elif interval["lower"] >= 0:
        status = "no_decline_in_estimated_interval"
    else:
        status = "inconclusive"
    return {
        "status": status,
        "direction": "with_news_minus_B1",
        "criterion": "upper simultaneous bound below zero",
        "automatic_promotion": False,
    }


def score_guardrail(rows):
    """One outcome safeguard per originally declared probability era."""
    groups = defaultdict(list)
    for row in rows:
        if (
            row.get("scored")
            and row.get("timely_news_coverage")
            and row.get("study_status") == "declared"
            and row.get("study_plan_sha256") == news_study_plan.PLAN_SHA256
        ):
            groups[row["implementation"]].append(
                (
                    row["season"],
                    row["round_id"],
                    row["scores"]["with_news"]["score"]
                    - row["scores"]["no_news"]["score"],
                )
            )
    estimates = {}
    for era, scores in groups.items():
        endpoint = _endpoint(scores, "score_points")
        estimates[era] = endpoint | guardrail(endpoint)
    if len(estimates) == 1:
        return next(iter(estimates.values()))
    return {
        "status": "multiple_probability_eras"
        if estimates
        else "no_eligible_scored_pairs",
        "by_era": estimates,
    }


def _era(rows, capability, mode, *, legacy=False):
    scores, prices = [], []
    for r in rows:
        unit = (r["season"], r["round_id"])
        if r["scored"]:
            scores.append(
                (
                    *unit,
                    r["scores"]["with_news"]["score"] - r["scores"]["no_news"]["score"],
                )
            )
        home = r["market_close"].get("endpoints", {}).get("home_win")
        if home:
            prices.append((*unit, home["distance_gain_pp"]))
    score = _endpoint(scores, "score_points", legacy=legacy)
    if not legacy:
        score["guardrail"] = guardrail(score)
    close = news_market.summary(rows, capability, mode)
    report = {
        "policy_sha256": rows[0]["policy_sha256"],
        "news_protocol": rows[0]["news_protocol"],
        "study_plan_sha256": news_study_plan.ORIGINAL_PLAN_SHA256
        if legacy
        else news_study_plan.PLAN_SHA256,
        "extractor_config_sha256": rows[0]["extractor_config_sha256"],
        "physical_recipe_counts": dict(
            sorted(Counter(r["recipe"] for r in rows).items())
        ),
        "seasons": dict(sorted(Counter(r["season"] for r in rows).items())),
        "eligible_seals": len(rows),
        "score": score,
        "score_exclusions": dict(
            sorted(Counter(r["reason"] for r in rows if not r["scored"]).items())
        ),
        "home_close": _endpoint(prices, "home_close_distance_pp", legacy=legacy),
        "market_close": close,
    }
    if not legacy:
        report["scheduled_market_close"] = news_market.summary(
            rows, mode=mode, field="scheduled_market_close"
        )
    return report


def summarize(records, seasons, horizon, mode, capability=None):
    groups, original = defaultdict(list), defaultdict(list)
    excluded = Counter()
    scheduled = Counter(r["season"] for r in records)
    for r in records:
        if not r["sealed"]:
            reason = r["reason"]
        elif r["study_status"] != "declared":
            reason = r["study_status"]
        elif not r["timely_news_coverage"]:
            reason = "news_coverage_incomplete_or_stale"
        elif r["study_plan_sha256"] == news_study_plan.ORIGINAL_PLAN_SHA256:
            original[r["implementation"]].append(r)
            reason = "separately_reported_original_study"
        elif r["study_plan_sha256"] == news_study_plan.PLAN_SHA256:
            groups[r["implementation"]].append(r)
            continue
        else:
            reason = "unrecognized_study_plan"
        excluded[reason] += 1
    current = [r for group in groups.values() for r in group]
    previous = {}
    if original:
        previous[news_study_plan.ORIGINAL_PLAN_SHA256] = {
            "plan": deepcopy(_LEGACY),
            "eligible_seals": sum(map(len, original.values())),
            "eras": {
                key: _era(rows, capability, mode, legacy=True)
                for key, rows in sorted(original.items())
            },
        }
    return {
        "plan_sha256": news_study_plan.PLAN_SHA256,
        "plan": deepcopy(news_study_plan.PLAN),
        "first_season": seasons[0],
        "through_season": seasons[-1],
        "horizon": horizon,
        "mode": mode,
        "status": "descriptive_only",
        "scheduled": len(records),
        "expected": 380 * len(seasons),
        "scheduled_by_season": {s: scheduled[s] for s in seasons},
        "eligible_seals": len(current),
        "exclusions": dict(sorted(excluded.items())),
        "previous_plans": previous,
        "market_close": news_market.summary(current, capability, mode),
        "scheduled_market_close": news_market.summary(
            current, mode=mode, field="scheduled_market_close"
        ),
        "eras": {
            key: _era(rows, capability, mode) for key, rows in sorted(groups.items())
        },
    }
