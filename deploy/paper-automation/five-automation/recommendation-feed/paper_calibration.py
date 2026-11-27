"""Pooled B1 calibration for research selections at T−60, fixed before prospective use."""

from decimal import Decimal as D

from polymarket861 import ledger_rules as rules

REVIEW_SHA256 = "2b28edf2cc503825f0c4db4c3f9e268b2003dff63e4b0a613f13107962e3ed2c"
BASELINE_SHA256 = "b7c5f56daf1fc0c2d3b6e8a5290731818e47458230726441f94d0ec3fcdc15ce"
FITS = {
    "home": ("0.0898", "1.1677"),
    "draw": ("0.1306", "1.0903"),
    "away": ("0.0026", "1.1289"),
    "over_2.5": ("0.0802", "0.6309"),
}


def fit_evidence(terms):
    curves = ("over_2.5",) if terms["family"] == "total" else ("home", "draw", "away")
    return {
        "method": "five_league_pooled_logit_v1",
        "fit_season": "2025-26",
        "baseline_horizon": "T-60m",
        "fit_matches": 1745,
        "review_sha256": REVIEW_SHA256,
        "baseline_study_sha256": BASELINE_SHA256,
        "intercept_slope": {key: FITS[key] for key in curves},
    }


def _adjust(probability, key):
    p = rules.unit(probability)
    if not 0 < p < 1:
        raise ValueError("pooled_calibration_requires_interior_probability")
    a, b = map(D, FITS[key])
    return 1 / (1 + (-a).exp() * ((1 - p) / p) ** b)


def calibrated(arm, terms):
    """Correct the no-news baseline, preserving mutually exclusive directions."""
    if terms["family"] == "total" and D(terms["line"]) == D("2.5") and terms["side"] in ("over", "under"):
        over = _adjust(arm["totals"]["total"]["2.5"]["over"], "over_2.5")
        return over if terms["side"] == "over" else 1 - over
    if terms["family"] != "result" or terms["side"] not in ("yes", "no"):
        raise ValueError("unsupported_pooled_calibration_contract")
    outcomes = ("home", "draw", "away")
    raw = {k: rules.unit(arm["probabilities"][k if k == "draw" else k + "_win"]) for k in outcomes}
    if abs(sum(raw.values()) - 1) > D("0.0001"):
        raise ValueError("invalid_baseline_result_distribution")
    adjusted = {k: _adjust(raw[k], k) for k in outcomes}
    p = adjusted[terms["selection"]] / sum(adjusted.values())
    return p if terms["side"] == "yes" else 1 - p
