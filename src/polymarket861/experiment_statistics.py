"""Small-sample intervals for matched fixture differences clustered by league round."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
from scipy.stats import t

from . import store

PROTOCOL = "round-cluster-t-v1"


def clustered_t(groups, family_comparisons):
    """Match-weighted paired mean; CR1 standard error with G−1 Student degrees of freedom."""
    if type(family_comparisons) is not int or family_comparisons < 1:
        raise ValueError("the preregistered family must have at least one comparison")
    groups = [
        np.asarray(rows, dtype=float).reshape(len(rows), -1)
        for rows in groups
        if len(rows)
    ]
    g = len(groups)
    if g < 2:
        return None
    if any(not np.isfinite(xs).all() for xs in groups):
        raise ValueError("invalid paired difference")
    sizes = np.array([len(xs) for xs in groups])
    sums = np.stack([xs.sum(axis=0) for xs in groups])
    n = int(sizes.sum())
    mean = sums.sum(axis=0) / n
    # Linearization of the pooled mean under independent round clusters; handles unequal numbers of valid matches.
    influence = sums - sizes[:, None] * mean
    se = np.sqrt(g / (g - 1) * np.square(influence).sum(axis=0)) / n
    tail = 0.025 / family_comparisons
    critical = float(t.ppf(1 - tail, g - 1))
    return {
        "method": PROTOCOL,
        "unit": "season_and_official_round",
        "rounds": g,
        "matches": n,
        "degrees_of_freedom": g - 1,
        "family_comparisons": family_comparisons,
        "family_confidence": 0.95,
        "individual_confidence": 1 - 2 * tail,
        "critical_t": critical,
        "intervals": [
            {
                "difference": float(mu),
                "standard_error": float(s),
                "lower": float(mu - critical * s),
                "upper": float(mu + critical * s),
            }
            for mu, s in zip(mean, se)
        ],
    }


def signoff(reviewer, reason):
    if (
        not isinstance(reviewer, str)
        or not reviewer.strip()
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        raise ValueError(
            "a named reviewer and written reason are required to change the active paper policy"
        )
    return {"reviewer": reviewer.strip(), "reason": reason.strip()}


def require_unopened_season(season, fixtures, now):
    """Before the supported EPL season begins, registration is possible with an unpublished schedule."""
    first_possible = datetime(
        int(season[:4]), 8, 1, tzinfo=ZoneInfo("Europe/London")
    ) - timedelta(hours=1)
    if store.instant(now) < first_possible:
        return
    first = [f for f in fixtures if f["round_id"] == 1]
    if len({f["fixture_id"] for f in first}) != 10:
        raise ValueError(
            "the first-round registration deadline cannot be verified without its full schedule"
        )
