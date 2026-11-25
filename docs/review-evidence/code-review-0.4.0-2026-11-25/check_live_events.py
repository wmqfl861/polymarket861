"""Solve (lambda, mu, rho) for both arms of each of the ten event predictions in
reports/news-live-0.4.0.json from the six published market probabilities and
report the fit residual plus the implied log-rate shifts."""
import json, math, sys
import numpy as np
from scipy.optimize import least_squares
from scipy.stats import poisson

report = json.load(open(sys.argv[1]))
KEYS = ("home_win", "draw", "away_win", "total_over_2_5", "home_over_1_5", "away_over_1_5")

def six(lam, mu, rho, n=60):
    k = np.arange(n)
    m = np.outer(poisson.pmf(k, lam), poisson.pmf(k, mu))
    m[0, 0] *= 1 - rho * lam * mu
    m[0, 1] *= 1 + rho * lam
    m[1, 0] *= 1 + rho * mu
    m[1, 1] *= 1 - rho
    m /= m.sum()
    h, a = np.indices(m.shape)
    return np.array([m[h > a].sum(), m[h == a].sum(), m[h < a].sum(), m[h + a > 2.5].sum(), m[h > 1.5].sum(), m[a > 1.5].sum()])

def solve(target):
    fit = least_squares(lambda x: six(*x) - target, (1.4, 1.2, -0.05), xtol=1e-15, ftol=1e-15, gtol=1e-15)
    return fit.x, float(np.abs(fit.fun).max())

rows = []
for ev in report["event_predictions"]:
    probs = {p["market"]: p for p in ev["probabilities"]}
    base = np.array([probs[k]["without_news"] for k in KEYS])
    news = np.array([probs[k]["with_news"] for k in KEYS])
    (l0, m0, r0), e0 = solve(base)
    (l1, m1, r1), e1 = solve(news)
    rows.append((ev["fixture"].split("/", 2)[2], ev["claims"], ev["applied"], ev["status"], ev["excluded"], l0, m0, r0, math.log(l1 / l0), math.log(m1 / m0), r1 - r0, max(e0, e1), news - base))
print("fixture | claims applied | lam mu rho | dlog_home dlog_away | max fit residual | d(home_win, draw, away_win, over2.5)")
for r in rows:
    print(f"{r[0]:38s} c={r[1]:2d} a={r[2]:2d} lam={r[5]:.4f} mu={r[6]:.4f} rho={r[7]:+.5f} dh={r[8]:+.5f} da={r[9]:+.5f} drho={r[10]:+.1e} res={r[11]:.1e} d=({r[12][0]:+.4f},{r[12][1]:+.4f},{r[12][2]:+.4f},{r[12][3]:+.4f}) {r[3]} excl={r[4]}")
d = np.array([r[12] for r in rows])
print("mean change over 10 fixtures (home_win, draw, away_win, over2.5, home>1.5, away>1.5):", np.round(d.mean(0), 5))
print("mean |change|:", np.round(np.abs(d).mean(0), 5), " max |change|:", np.round(np.abs(d).max(0), 5))
tot = np.array([(r[5] * math.exp(r[8]) + r[6] * math.exp(r[9])) - (r[5] + r[6]) for r in rows])
print("change in expected total goals per fixture:", np.round(tot, 4), "mean", round(float(tot.mean()), 4))
print("sum claims", sum(r[1] for r in rows), "sum applied", sum(r[2] for r in rows))
from collections import Counter
st, ex = Counter(), Counter()
for r in rows:
    st.update(r[3]); ex.update(r[4])
print("status totals", dict(st), "excluded totals", dict(ex))
