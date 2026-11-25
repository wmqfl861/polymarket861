"""Fixed-design descriptive accumulation of comparable prospective news pairs."""

import math
from collections import Counter, defaultdict
from copy import deepcopy
from functools import lru_cache
from statistics import NormalDist, fmean

from scipy.optimize import brentq
from scipy.stats import ncf

from . import experiment_statistics, news_market, news_study_plan

_PLAN = news_study_plan.PLAN
_NORMAL = NormalDist()


def _student_power(shift, critical, df):
    # Squared noncentral t has an F(1, df, shift²) law. Its CDF avoids
    # cancellation/NaN in the near-zero, opposite tail of a noncentral t.
    return float(1 - ncf.cdf(critical**2, 1, df, shift**2))


@lru_cache(maxsize=256)
def _detectable_shift(critical, df):
    target = _PLAN["target_power"]
    return brentq(lambda x: _student_power(x, critical, df) - target, 0, 4 * critical)


def _power(field, ci, pairs):
    """Planning approximation conditional on variance, never a test or an observed win rate."""
    target = _PLAN["practical_effect"][field]
    family = _PLAN["family_comparisons"]
    z80 = _NORMAL.inv_cdf(_PLAN["target_power"])
    critical = _NORMAL.inv_cdf(1 - (1 - _PLAN["family_confidence"]) / (2 * family))
    assumptions = _PLAN["planning_assumptions"]
    size = assumptions["fixtures_per_round"]
    sd = assumptions["single_fixture_sd"][field]
    design_effect = 1 + (size - 1) * assumptions["intraround_correlation"]
    illustrative = math.ceil(design_effect * (sd * (critical + z80) / target) ** 2)
    result = {
        "unit": field,
        "practical_effect": target,
        "target_power": _PLAN["target_power"],
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


def _endpoint(values, field):
    groups = defaultdict(list)
    for season, rnd, value in values:
        groups[season, rnd].append([value])
    ci = experiment_statistics.clustered_t(groups.values(), _PLAN["family_comparisons"])
    return {
        "pairs": len(values),
        "rounds": len(groups),
        "mean": fmean(v for _, _, v in values) if values else None,
        "interval": (
            {k: v for k, v in ci.items() if k != "intervals"} | ci["intervals"][0]
            if ci
            else None
        ),
        "power": _power(field, ci, len(values)),
    }


def _era(rows, capability, mode):
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
    return {
        "policy_sha256": rows[0]["policy_sha256"],
        "news_protocol": rows[0]["news_protocol"],
        "extractor_config_sha256": rows[0]["extractor_config_sha256"],
        "physical_recipe_counts": dict(
            sorted(Counter(r["recipe"] for r in rows).items())
        ),
        "seasons": dict(sorted(Counter(r["season"] for r in rows).items())),
        "eligible_seals": len(rows),
        "score": _endpoint(scores, "score_points"),
        "score_exclusions": dict(
            sorted(Counter(r["reason"] for r in rows if not r["scored"]).items())
        ),
        "home_close": _endpoint(prices, "home_close_distance_pp"),
        "market_close": news_market.summary(rows, capability, mode),
    }


def summarize(records, seasons, horizon, mode, capability=None):
    groups = defaultdict(list)
    excluded = Counter()
    scheduled = Counter(r["season"] for r in records)
    for r in records:
        if not r["sealed"]:
            reason = r["reason"]
        elif r["study_status"] != "declared":
            reason = r["study_status"]
        elif not r["observed_both"] or not r["timely_news_coverage"]:
            reason = "news_coverage_incomplete_or_stale"
        else:
            groups[r["implementation"]].append(r)
            continue
        excluded[reason] += 1
    return {
        "plan_sha256": news_study_plan.PLAN_SHA256,
        "plan": deepcopy(_PLAN),
        "first_season": seasons[0],
        "through_season": seasons[-1],
        "horizon": horizon,
        "mode": mode,
        "status": "descriptive_only",
        "scheduled": len(records),
        "expected": 380 * len(seasons),
        "scheduled_by_season": {s: scheduled[s] for s in seasons},
        "eligible_seals": sum(map(len, groups.values())),
        "market_close": news_market.coverage(
            [r for group in groups.values() for r in group], capability, mode
        ),
        "exclusions": dict(sorted(excluded.items())),
        "planning": {k: _power(k, None, 0) for k in _PLAN["practical_effect"]},
        "eras": {
            key: _era(rows, capability, mode) for key, rows in sorted(groups.items())
        },
    }
