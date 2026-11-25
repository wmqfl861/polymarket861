"""Independent recomputation of the Forest v Chelsea example seal in reports/news-live-0.4.0.json.

Marginals of a Dixon-Coles grid are exactly Poisson, so lambda/mu can be read from
P(team scores 0). rho is then solved from the draw probability. Every other published
number is recomputed on an independent 60x60 grid and compared.
"""
import json, math, sys
from scipy.optimize import brentq
from scipy.stats import poisson
import numpy as np

report = json.load(open(sys.argv[1]))
ex = report["example_seal"]

def grid(lam, mu, rho, n=60):
    k = np.arange(n)
    m = np.outer(poisson.pmf(k, lam), poisson.pmf(k, mu))
    m[0, 0] *= 1 - rho * lam * mu
    m[0, 1] *= 1 + rho * lam
    m[1, 0] *= 1 + rho * mu
    m[1, 1] *= 1 - rho
    return m / m.sum()

def markets(m):
    h, a = np.indices(m.shape)
    out = {"home_win": m[h > a].sum(), "draw": m[h == a].sum(), "away_win": m[h < a].sum()}
    for name, g in (("total", h + a), ("home", h), ("away", a)):
        for line in (0.5, 1.5, 2.5, 3.5):
            out[f"{name}/{line}"] = m[g > line].sum()
    return out

solved = {}
for arm in ("no_news", "with_news"):
    t = ex["totals"][arm]
    lam = -math.log(t["home"]["0.5"]["under"])
    mu = -math.log(t["away"]["0.5"]["under"])
    rho = brentq(lambda r: markets(grid(lam, mu, r))["draw"] - ex["probabilities"][arm]["draw"], -0.25, 0.25, xtol=1e-15)
    solved[arm] = (lam, mu, rho)
    mk = markets(grid(lam, mu, rho))
    worst = 0.0
    for key in ("home_win", "draw", "away_win"):
        worst = max(worst, abs(mk[key] - ex["probabilities"][arm][key]))
    for fam in ("total", "home", "away"):
        for line, sides in t[fam].items():
            worst = max(worst, abs(mk[f"{fam}/{float(line)}"] - sides["over"]), abs(1 - mk[f"{fam}/{float(line)}"] - sides["under"]))
    worst = max(worst, abs(mk["total/2.5"] - ex["probabilities"][arm]["total_over_2_5"]),
                abs(mk["home/1.5"] - ex["probabilities"][arm]["home_over_1_5"]),
                abs(mk["away/1.5"] - ex["probabilities"][arm]["away_over_1_5"]))
    print(f"{arm}: lambda_home={lam:.9f} lambda_away={mu:.9f} rho={rho:.9f} max_abs_err={worst:.3e}")

(l0, m0, r0), (l1, m1, r1) = solved["no_news"], solved["with_news"]
print(f"log shift home={math.log(l1/l0):+.6f} away={math.log(m1/m0):+.6f} rho diff={r1-r0:+.2e}")
