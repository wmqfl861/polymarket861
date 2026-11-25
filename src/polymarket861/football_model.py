"""Distribution of regulation-time EPL scores; no network or database access."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln
from scipy.stats import poisson

DEFAULT_LINES = (0.5, 1.5, 2.5, 3.5)
PROB_KEYS = (
    "home_win",
    "draw",
    "away_win",
    "total_over_2_5",
    "home_over_1_5",
    "away_over_1_5",
)
ITEM_KEYS = ("result", "total", "home", "away")
TAIL_TOLERANCE = 1e-12
DECAY_PER_DAY = 0.002
RIDGE = 8.0
MIN_MATCHES = 40


def half_line(value):
    line = float(value)
    if not math.isfinite(line) or line < 0.5 or line % 1 != 0.5:
        raise ValueError("each goal line must be a positive half-integer")
    return line


def probabilities(matrix, lines=DEFAULT_LINES):
    """Market probabilities from one joint score distribution; rows are home goals."""
    grid = np.asarray(matrix, dtype=float)
    if (
        grid.ndim != 2
        or not grid.size
        or not np.isfinite(grid).all()
        or np.min(grid) < 0
        or abs(float(grid.sum()) - 1) > 1e-9
    ):
        raise ValueError("invalid score probability matrix")
    h, a = np.indices(grid.shape)
    normalized = grid / grid.sum()

    def prob(mask):
        return min(1.0, max(0.0, float(normalized[mask].sum())))

    p = dict(
        zip(
            PROB_KEYS,
            (
                prob(h > a),
                prob(h == a),
                prob(h < a),
                prob(h + a > 2.5),
                prob(h > 1.5),
                prob(a > 1.5),
            ),
        )
    )
    totals = {}
    clean_lines = sorted({half_line(v) for v in lines})
    for name, goals in (("total", h + a), ("home", h), ("away", a)):
        totals[name] = {}
        for line in clean_lines:
            over = prob(goals > line)
            totals[name][str(line)] = {"over": over, "under": 1.0 - over}
    return p, totals


def _goals_matrix(lam, mu, rho):
    """Retain enough support for the marginal tails; report the omitted probability."""
    if not all(math.isfinite(x) for x in (lam, mu, rho)) or min(lam, mu) <= 0:
        raise ValueError("invalid goal intensity")
    factors = np.array([[1 - rho * lam * mu, 1 + rho * lam], [1 + rho * mu, 1 - rho]])
    if min(factors.flat) <= 0:
        raise ValueError("Dixon-Coles adjustment outside the probability domain")
    n = int(max(3, poisson.isf(TAIL_TOLERANCE / 4, max(lam, mu))))
    k = np.arange(n + 1)
    logfac = gammaln(k + 1)
    matrix = np.outer(
        np.exp(k * np.log(lam) - lam - logfac), np.exp(k * np.log(mu) - mu - logfac)
    )
    matrix[:2, :2] *= factors
    tail = float(poisson.sf(n, lam) + poisson.sf(n, mu))
    matrix /= matrix.sum()
    return matrix, tail


@dataclass
class TeamModel:
    positions: dict
    attack: np.ndarray
    defence: np.ndarray
    intercept: float
    home_advantage: float
    rho: float

    def rates(self, home, away):
        hi, ai = self.positions[home], self.positions[away]
        h = math.exp(
            self.intercept + self.home_advantage + self.attack[hi] - self.defence[ai]
        )
        a = math.exp(self.intercept + self.attack[ai] - self.defence[hi])
        return h, a, self.rho


def fit_team_model(records):
    """Penalized Dixon–Coles likelihood, fit only to the supplied eligible observations.

    A record is (home_name, away_name, home_goals, away_goals, kickoff_epoch_seconds).
    Weights are relative to the latest eligible training kickoff, so an unchanged
    training set yields an identical fit at either decision horizon.
    """
    if len(records) < MIN_MATCHES:
        return None
    teams = sorted({row[0] for row in records} | {row[1] for row in records})
    pos = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    hi = np.array([pos[r[0]] for r in records])
    ai = np.array([pos[r[1]] for r in records])
    hg = np.asarray([r[2] for r in records], dtype=float)
    ag = np.asarray([r[3] for r in records], dtype=float)
    times = np.asarray([r[4] for r in records], dtype=float)
    w = np.exp(-DECAY_PER_DAY * (times.max() - times) / 86400)
    masks = [(hg == i) & (ag == j) for i, j in ((0, 0), (0, 1), (1, 0), (1, 1))]

    def objective(theta):
        intercept, advantage, rho = theta[:3]
        attack, defence = theta[3 : n + 3], theta[3 + n :]
        lh = intercept + advantage + attack[hi] - defence[ai]
        la = intercept + attack[ai] - defence[hi]
        lam, mu = np.exp(lh), np.exp(la)
        tau = np.ones_like(hg)
        dlogh, dloga, drho = np.zeros_like(hg), np.zeros_like(hg), np.zeros_like(hg)
        m00, m01, m10, m11 = masks
        prod = lam[m00] * mu[m00]
        tau[m00] -= rho * prod
        dlogh[m00] = dloga[m00] = -rho * prod
        drho[m00] = -prod
        tau[m01] += rho * lam[m01]
        dlogh[m01], drho[m01] = rho * lam[m01], lam[m01]
        tau[m10] += rho * mu[m10]
        dloga[m10], drho[m10] = rho * mu[m10], mu[m10]
        tau[m11] -= rho
        drho[m11] = -1
        # Matched log and derivative below epsilon preserve a strong concave
        # barrier during line search; no invalid factors enter output prices.
        eps = 1e-6
        denom = np.maximum(tau, eps)
        logtau = np.log(denom) + np.minimum(tau - eps, 0) / eps
        loss = -np.dot(w, hg * lh - lam + ag * la - mu + logtau)
        loss += 0.5 * RIDGE * (np.dot(attack, attack) + np.dot(defence, defence))
        gh = w * (hg - lam + dlogh / denom)
        ga = w * (ag - mu + dloga / denom)
        gatt = (
            -np.bincount(hi, gh, minlength=n)
            - np.bincount(ai, ga, minlength=n)
            + RIDGE * attack
        )
        gdef = (
            np.bincount(ai, gh, minlength=n)
            + np.bincount(hi, ga, minlength=n)
            + RIDGE * defence
        )
        grad = np.r_[
            -gh.sum() - ga.sum(), -gh.sum(), -np.dot(w, drho / denom), gatt, gdef
        ]
        return loss, grad

    start = np.zeros(3 + 2 * n)
    start[:3] = (0.15, 0.20, -0.03)
    bounds = [(-1.5, 1.5), (-1, 1), (-0.25, 0.25)] + [(-2, 2)] * (2 * n)
    opt = minimize(
        objective,
        start,
        method="L-BFGS-B",
        jac=True,
        bounds=bounds,
        options={"ftol": 1e-10, "gtol": 1e-5, "maxiter": 350, "maxls": 40},
    )
    if not opt.success or not np.isfinite(opt.x).all():
        return None
    it, adv, rho = map(float, opt.x[:3])
    return TeamModel(pos, opt.x[3 : n + 3], opt.x[3 + n :], it, adv, rho)


def score_probabilities(probs, home_goals, away_goals):
    """Return the proper score, the four unrounded losses, points and log losses."""
    if any(
        not isinstance(v, (int, np.integer)) or isinstance(v, bool) or v < 0
        for v in (home_goals, away_goals)
    ):
        raise ValueError("nonnegative integer full-time goals required")
    values = np.asarray([probs[k] for k in PROB_KEYS], dtype=float)
    if (
        not np.isfinite(values).all()
        or (values < 0).any()
        or (values > 1).any()
        or abs(float(values[:3].sum()) - 1) > 1e-8
    ):
        raise ValueError("invalid football probabilities")
    y = np.array(
        [
            home_goals > away_goals,
            home_goals == away_goals,
            home_goals < away_goals,
            home_goals + away_goals >= 3,
            home_goals >= 2,
            away_goals >= 2,
        ]
    )
    se = (values - y) ** 2
    losses = dict(zip(ITEM_KEYS, map(float, np.r_[se[:3].sum() / 2, se[3:]])))
    correct = [values[:3][y[:3]][0], *np.where(y[3:], values[3:], 1 - values[3:])]
    return {
        "score": 100 * (1 - sum(losses.values()) / 4),
        "losses": losses,
        "item_scores": {k: 100 * (1 - v) for k, v in losses.items()},
        "log_losses": dict(
            zip(ITEM_KEYS, (-math.log(max(float(p), 1e-15)) for p in correct))
        ),
    }


def apply_context(prediction, context, *, lines=None):
    """Mix finite independent, evidence-keyed lineup scenarios with stated multipliers.

    Multipliers are changes relative to the incumbent lineup. Shared items are
    deduplicated. Above ten distinct binary uncertainties retain the B1
    prediction rather than silently dropping players or sampling nondeterministically.
    """
    if lines is None:
        lines = (
            tuple(map(float, prediction.get("totals", {}).get("total", {})))
            or DEFAULT_LINES
        )
    if prediction.get("model") != "B1":
        return prediction
    # Each item is one event with a joint pair of intensity changes. A fixture-wide
    # weather scenario acts on both teams in the same outcome of its Bernoulli draw.
    events = []
    for side in ("home", "away"):
        for impact in context.get("teams", {}).get(side, {}).get("impacts", []):
            if isinstance(impact, dict):
                events.append(
                    (
                        impact,
                        impact if side == "home" else {},
                        impact if side == "away" else {},
                    )
                )
    for impact in context.get("match_impacts", []):
        if not isinstance(impact, dict) or not all(
            isinstance(impact.get(k), str) and impact[k].strip()
            for k in ("basis", "uncertainty")
        ):
            continue
        if not all(isinstance(impact.get(k), dict) for k in ("home", "away")):
            continue
        events.append((impact, impact["home"], impact["away"]))
    impacts = {}
    for source, home, away in events:
        key = source.get("evidence_id")
        if (
            not isinstance(key, str)
            or not key
            or source.get("experimental") is not True
        ):
            continue
        try:
            factors = [
                float(v.get(k, 1))
                for v in (home, away)
                for k in ("attack_multiplier", "defense_multiplier")
            ]
            prob = float(source["probability"])
        except (ValueError, TypeError, KeyError):
            continue
        if (
            not all(math.isfinite(x) and 0 < x <= 4 for x in factors)
            or not 0 < prob <= 1
        ):
            continue
        home_attack, home_defence, away_attack, away_defence = factors
        item = (home_attack * away_defence, away_attack * home_defence, prob)
        if item[:2] == (1, 1):
            continue
        if key in impacts and impacts[key] != item:
            return dict(prediction, context_status="conflicting_impact_evidence")
        impacts[key] = item
    if not impacts:
        return dict(prediction, context_status="no_verified_impact")
    if sum(p < 1 for _, _, p in impacts.values()) > 10:
        return dict(prediction, context_status="uncertain_scenario_limit")
    rates = prediction["rates"]
    scenarios = [(1.0, rates["home"], rates["away"])]
    for home_factor, away_factor, prob in (impacts[k] for k in sorted(impacts)):
        updated = []
        for weight, h, a in scenarios:
            if prob < 1:
                updated.append((weight * (1 - prob), h, a))
            updated.append((weight * prob, h * home_factor, a * away_factor))
        scenarios = updated
    matrices = []
    for w, h, a in scenarios:
        if not (0 < h <= 15 and 0 < a <= 15):
            return dict(prediction, context_status="scenario_rate_outside_model")
        try:
            m, t = _goals_matrix(h, a, rates["rho"])
        except ValueError:
            return dict(prediction, context_status="scenario_outside_dc_domain")
        matrices.append((w, m, t))
    sz = max(m.shape[0] for _, m, _ in matrices)
    joint = np.zeros((sz, sz))
    for w, m, _ in matrices:
        joint[: m.shape[0], : m.shape[1]] += w * m
    p, totals = probabilities(joint, lines)
    return dict(
        prediction,
        model="B2-shadow",
        context_status="experimental_mixture",
        rates={
            "home": sum(w * h for w, h, _ in scenarios),
            "away": sum(w * a for w, _, a in scenarios),
            "rho": rates["rho"],
        },
        matrix=joint.tolist(),
        probabilities=p,
        totals=totals,
        tail_bound=sum(w * t for w, _, t in matrices),
        scenario_count=len(scenarios),
        impact_evidence_ids=sorted(impacts),
    )
