#!/usr/bin/env python3
"""score_noise_check.py -- review evidence for the EPL v1 blueprint's 0-100 "four-item" score.

Question answered: on real Premier League results, how far apart do a know-nothing
baseline and a plain Dixon-Coles model land on the blueprint's score, and how noisy
is that score per match / per 10-match round / per 380-match season?

What it does (everything is rolling-origin and leak-free):
  * loads openfootball/football.json  <season>/en.1.json  (public domain), validates it and
    repairs one known defect (2019-20 restart matches of July 2020 are dated July 2019);
  * B0  = league-frequency baseline: constant probabilities from the 3 preceding seasons;
  * B1  = plain Dixon-Coles (attack/defence per team, home advantage, rho, exponential time
          decay, ridge shrinkage, promoted-team prior), refitted before every match date on
          matches with a strictly EARLIER date (previous 3 seasons + current season to date);
          decay rate and ridge strength are chosen on 2018-19..2020-21 only, then frozen;
  * two mildly perturbed variants of B1 (decay x2, ridge x2) as a "candidate vs champion" proxy;
  * scores 2021-22..2025-26 with the blueprint's formula, bootstraps paired differences,
    computes sample sizes for a paired comparison, a calibration table and 1X2 log loss;
  * "oracle" simulation: B1's probabilities are treated as the truth and outcomes are
    re-simulated to get the noise floor of the score.

Blueprint score: 1X2 loss = 0.5 * sum_k (p_k - o_k)^2 ; O2.5 total, home O1.5, away O1.5 losses
= (p - o)^2 ; match score = 100 * (1 - mean of the four losses) ; round score = mean of matches.

Extra blocks beyond the brief: a leak check (future results are corrupted and earlier predictions
must not change), an exact (non-simulated) cross-check of the simulated noise figures, and a
"true model vs noisy copy" table that shows how far apart two models must be to be told apart.

Run (deterministic; about 100 s including the 36-point tuning grid, about 10 s with --skip-tuning):
    python3 -I score_noise_check.py /path/to/openfootball/football.json --out score_noise_results.json
--skip-tuning reuses FROZEN_DEFAULT (the grid's own result) and yields identical numbers.
Requires numpy and scipy. Data snapshot used for the review: openfootball/football.json @ e6744429.
"""
import argparse
import collections
import datetime
import json
import math
import os
import sys
import time

import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln

G = 13                      # goals grid 0..12 per team, renormalised
EPS = 1e-10
ALL_SEASONS = ["2010-11", "2011-12", "2012-13", "2013-14", "2014-15", "2015-16", "2016-17",
               "2017-18", "2018-19", "2019-20", "2020-21", "2021-22", "2022-23", "2023-24",
               "2024-25", "2025-26", "2026-27"]
TUNE_SEASONS = ["2018-19", "2019-20", "2020-21"]
TEST_SEASONS = ["2021-22", "2022-23", "2023-24", "2024-25", "2025-26"]
PARTIAL_SEASON = "2026-27"   # incomplete in the data snapshot; reported separately, never pooled
XI_GRID = [0.0005, 0.0010, 0.0015, 0.0020, 0.0030, 0.0040]      # decay per day
KAPPA_GRID = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]                    # ridge precision on att/def
FROZEN_DEFAULT = (0.0020, 8.0)   # result of the tuning grid on 2018-19..2020-21 (see results json)
SEED = 20261125
N_BOOT = 10000
N_SIM = 2000
Z_ALPHA = 1.959963984540054     # two-sided alpha = 0.05
Z_POWER = 0.8416212335729143    # power = 0.80


# ----------------------------------------------------------------------------- data
def norm_team(name):
    n = name.strip()
    for suf in (" AFC", " FC"):
        if n.endswith(suf):
            n = n[: -len(suf)]
    if n.startswith("AFC "):
        n = n[4:]
    return n


def load_all(data_dir):
    """Return dict of numpy arrays for every fixture plus per-season metadata."""
    rows, meta, team_ids = [], {}, {}
    for s_idx, s in enumerate(ALL_SEASONS):
        path = os.path.join(data_dir, s, "en.1.json")
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        matches = d["matches"] if isinstance(d, dict) else d
        y0 = int(s[:4])
        fixed_dates, played, teams = 0, 0, set()
        pair_count, per_round = collections.Counter(), collections.Counter()
        home_n, away_n = collections.Counter(), collections.Counter()
        for x in matches:
            sc = x.get("score")
            if sc is None:
                ft = None
            elif isinstance(sc, dict):
                ft = sc.get("ft")
            else:                       # bare [h, a] list (source uses it for some 0-0 results)
                ft = sc
            date = datetime.date.fromisoformat(x["date"])
            if date.year == y0 and date.month == 7:
                # source defect: 2019-20 restart matches played in July 2020 carry the year 2019
                date = date.replace(year=y0 + 1)
                fixed_dates += 1
            assert datetime.date(y0, 8, 1) <= date <= datetime.date(y0 + 1, 7, 31), (s, x)
            rnd = int(x["round"].split()[-1])
            h, a = norm_team(x["team1"]), norm_team(x["team2"])
            teams.update((h, a))
            for t in (h, a):
                team_ids.setdefault(t, len(team_ids))
            pair_count[(h, a)] += 1
            per_round[rnd] += 1
            home_n[h] += 1
            away_n[a] += 1
            if ft is not None:
                assert len(ft) == 2 and all(isinstance(v, int) and 0 <= v <= 15 for v in ft), (s, x)
                played += 1
                hg, ag = ft
            else:
                hg, ag = -1, -1
            rows.append((s_idx, rnd, date.toordinal(), team_ids[h], team_ids[a], hg, ag))
        # structural validation
        assert len(matches) == 380, (s, len(matches))
        assert len(teams) == 20, (s, sorted(teams))
        assert sorted(per_round) == list(range(1, 39)) and set(per_round.values()) == {10}, s
        assert max(pair_count.values()) == 1 and len(pair_count) == 380, s
        assert all(home_n[t] == 19 and away_n[t] == 19 for t in teams), s
        if s != PARTIAL_SEASON:
            assert played == 380, (s, played)
        meta[s] = {"fixtures": len(matches), "played": played, "teams": sorted(teams),
                   "dates_shifted_plus_one_year": fixed_dates}
    arr = np.array(rows, dtype=np.int64)
    tab = {"season": arr[:, 0], "round": arr[:, 1], "date": arr[:, 2], "home": arr[:, 3],
           "away": arr[:, 4], "hg": arr[:, 5], "ag": arr[:, 6]}
    tab["played"] = tab["hg"] >= 0
    # a team never plays twice within two days once dates are repaired
    for s_idx in range(len(ALL_SEASONS)):
        m = tab["season"] == s_idx
        for t in np.unique(tab["home"][m]):
            dts = np.sort(tab["date"][m & ((tab["home"] == t) | (tab["away"] == t))])
            assert np.diff(dts).min() >= 2, (ALL_SEASONS[s_idx], t)
    names = [None] * len(team_ids)
    for k, v in team_ids.items():
        names[v] = k
    return tab, meta, names


def outcomes(hg, ag):
    """Indicator matrix (n, 6): home win, draw, away win, total>2.5, home>1.5, away>1.5."""
    return np.stack([hg > ag, hg == ag, hg < ag, (hg + ag) >= 3, hg >= 2, ag >= 2], axis=1).astype(float)


def item_losses(p, o):
    """Blueprint losses, columns: 1X2 (0.5 * multiclass Brier), total O2.5, home O1.5, away O1.5."""
    l1 = 0.5 * ((p[..., :3] - o[..., :3]) ** 2).sum(axis=-1)
    rest = (p[..., 3:] - o[..., 3:]) ** 2
    return np.concatenate([l1[..., None], rest], axis=-1)


def match_score(p, o):
    return 100.0 * (1.0 - item_losses(p, o).mean(axis=-1))


def _self_check():
    p = np.array([[0.5, 0.3, 0.2, 0.40, 0.60, 0.25]])
    o = outcomes(np.array([2]), np.array([0]))
    s = match_score(p, o)[0]
    assert abs(s - 85.6875) < 1e-12, s
    assert np.allclose(item_losses(p, o)[0], [0.19, 0.16, 0.16, 0.0625])


# ----------------------------------------------------------------------------- B0
def b0_probs(tab, s_idx):
    m = np.isin(tab["season"], [s_idx - 3, s_idx - 2, s_idx - 1]) & tab["played"]
    assert m.sum() == 1140
    return outcomes(tab["hg"][m], tab["ag"][m]).mean(axis=0)


# ----------------------------------------------------------------------------- Dixon-Coles
def dc_objective(theta, hi, ai, x, y, w, n, prior_att, prior_def, kappa, m00, m01, m10, m11):
    """Penalised negative weighted log-likelihood and gradient.
    log lam = c + home + att[h] - def[a] ; log mu = c + att[a] - def[h]
    tau(0,0)=1-lam*mu*rho ; tau(0,1)=1+lam*rho ; tau(1,0)=1+mu*rho ; tau(1,1)=1-rho  (x=home, y=away)
    """
    c, home, rho = theta[0], theta[1], theta[2]
    att, dfn = theta[3:3 + n], theta[3 + n:3 + 2 * n]
    ll = c + home + att[hi] - dfn[ai]
    lm = c + att[ai] - dfn[hi]
    lam, mu = np.exp(ll), np.exp(lm)
    tau = np.ones_like(lam)
    tau[m00] = 1.0 - lam[m00] * mu[m00] * rho
    tau[m01] = 1.0 + lam[m01] * rho
    tau[m10] = 1.0 + mu[m10] * rho
    tau[m11] = 1.0 - rho
    tau = np.maximum(tau, EPS)
    f = -(w * (x * ll - lam + y * lm - mu + np.log(tau))).sum()
    f += 0.5 * kappa * (((att - prior_att) ** 2).sum() + ((dfn - prior_def) ** 2).sum())
    gl, gm, gr = x - lam, y - mu, np.zeros_like(lam)
    t = lam[m00] * mu[m00] / tau[m00]
    gl[m00] -= t * rho
    gm[m00] -= t * rho
    gr[m00] = -t
    t = lam[m01] / tau[m01]
    gl[m01] += t * rho
    gr[m01] = t
    t = mu[m10] / tau[m10]
    gm[m10] += t * rho
    gr[m10] = t
    gr[m11] = -1.0 / tau[m11]
    gl, gm, gr = gl * w, gm * w, gr * w
    g = np.empty_like(theta)
    g[0] = -(gl.sum() + gm.sum())
    g[1] = -gl.sum()
    g[2] = -gr.sum()
    g[3:3 + n] = -(np.bincount(hi, weights=gl, minlength=n) + np.bincount(ai, weights=gm, minlength=n)) \
        + kappa * (att - prior_att)
    g[3 + n:] = (np.bincount(ai, weights=gl, minlength=n) + np.bincount(hi, weights=gm, minlength=n)) \
        + kappa * (dfn - prior_def)
    return f, g


def fit_dc(hi, ai, x, y, w, n, prior_att, prior_def, kappa, theta0):
    m00 = (x == 0) & (y == 0)
    m01 = (x == 0) & (y == 1)
    m10 = (x == 1) & (y == 0)
    m11 = (x == 1) & (y == 1)
    bounds = [(None, None), (None, None), (-0.3, 0.3)] + [(-3.0, 3.0)] * (2 * n)
    res = minimize(dc_objective, theta0, jac=True, method="L-BFGS-B", bounds=bounds,
                   args=(hi, ai, x.astype(float), y.astype(float), w, n, prior_att, prior_def, kappa,
                         m00, m01, m10, m11),
                   options={"maxiter": 2000, "maxfun": 20000, "ftol": 1e-14, "gtol": 1e-7})
    return res


_K = np.arange(G)
_LOGFACT = gammaln(_K + 1.0)
_I, _J = np.meshgrid(_K, _K, indexing="ij")          # _I = home goals, _J = away goals
_MASKS = [(_I > _J), (_I == _J), (_I < _J), ((_I + _J) >= 3), (_I >= 2), (_J >= 2)]


def score_matrix(lam, mu, rho):
    """Dixon-Coles joint score matrix (n, G, G), renormalised. Returns matrix and #clipped taus."""
    ph = np.exp(_K[None, :] * np.log(lam)[:, None] - lam[:, None] - _LOGFACT[None, :])
    pa = np.exp(_K[None, :] * np.log(mu)[:, None] - mu[:, None] - _LOGFACT[None, :])
    P = ph[:, :, None] * pa[:, None, :]
    taus = np.stack([1.0 - lam * mu * rho, 1.0 + lam * rho, 1.0 + mu * rho, (1.0 - rho) * np.ones_like(lam)], axis=1)
    clipped = int((taus < EPS).sum())
    taus = np.maximum(taus, EPS)
    P[:, 0, 0] *= taus[:, 0]
    P[:, 0, 1] *= taus[:, 1]
    P[:, 1, 0] *= taus[:, 2]
    P[:, 1, 1] *= taus[:, 3]
    P /= P.sum(axis=(1, 2), keepdims=True)
    return P, clipped


def probs_from_matrix(P):
    return np.stack([P[:, m].sum(axis=1) for m in _MASKS], axis=1)


def promoted_sets(tab):
    teams = {}
    for s_idx in range(len(ALL_SEASONS)):
        m = tab["season"] == s_idx
        teams[s_idx] = set(np.unique(tab["home"][m]).tolist())
    prom = {s_idx: (teams[s_idx] - teams[s_idx - 1]) for s_idx in range(1, len(ALL_SEASONS))}
    return teams, prom


def promoted_offsets(tab, prom, s_idx):
    """Mean first-season attack/defence log-offsets of promoted teams in seasons strictly before s_idx."""
    a_list, d_list = [], []
    for q in range(1, s_idx):
        m = (tab["season"] == q) & tab["played"]
        if m.sum() != 380:
            continue
        avg = (tab["hg"][m].sum() + tab["ag"][m].sum()) / (2.0 * 380.0)
        for t in sorted(prom[q]):
            gf = tab["hg"][m & (tab["home"] == t)].sum() + tab["ag"][m & (tab["away"] == t)].sum()
            ga = tab["ag"][m & (tab["home"] == t)].sum() + tab["hg"][m & (tab["away"] == t)].sum()
            a_list.append(math.log(gf / 38.0 / avg))
            d_list.append(-math.log(ga / 38.0 / avg))
    return float(np.mean(a_list)), float(np.mean(d_list)), len(a_list)


def rolling_season(tab, prom, s_idx, xi, kappa, stats=None):
    """Leak-free rolling-origin predictions for every played match of season s_idx.
    Returns (match_row_indices, probs (n,6), score matrices (n,G,G), info dict with end-of-season
    parameters and the per-match (lam, mu, rho) under key "rates")."""
    win = np.isin(tab["season"], [s_idx - 3, s_idx - 2, s_idx - 1, s_idx]) & tab["played"]
    rows = np.flatnonzero(win)
    rows = rows[np.argsort(tab["date"][rows], kind="stable")]
    dates = tab["date"][rows]
    cur_all = tab["season"] == s_idx
    team_list = sorted(set(tab["home"][rows].tolist()) | set(tab["away"][rows].tolist())
                       | set(tab["home"][cur_all].tolist()))
    loc = {t: i for i, t in enumerate(team_list)}
    n = len(team_list)
    hi = np.array([loc[t] for t in tab["home"][rows]], dtype=np.int64)
    ai = np.array([loc[t] for t in tab["away"][rows]], dtype=np.int64)
    x, y = tab["hg"][rows], tab["ag"][rows]
    att_off, def_off, n_prom = promoted_offsets(tab, prom, s_idx)
    prior_att, prior_def = np.zeros(n), np.zeros(n)
    for t in prom[s_idx]:
        prior_att[loc[t]] = att_off
        prior_def[loc[t]] = def_off
    theta = np.zeros(3 + 2 * n)
    theta[0], theta[1], theta[2] = 0.1, 0.25, -0.05
    theta[3:3 + n], theta[3 + n:] = prior_att, prior_def
    cur = np.flatnonzero(cur_all & tab["played"])
    out_rows, out_probs, out_mats, out_rates = [], [], [], []
    for D in np.unique(tab["date"][cur]):
        ntr = int(np.searchsorted(dates, D, side="left"))        # strictly earlier dates only
        assert ntr >= 1140 and dates[ntr - 1] < D
        w = np.exp(-xi * (D - dates[:ntr]).astype(float))
        res = fit_dc(hi[:ntr], ai[:ntr], x[:ntr], y[:ntr], w, n, prior_att, prior_def, kappa, theta)
        theta = res.x
        if stats is not None:
            stats["fits"] += 1
            stats["not_converged"] += int(not res.success)
            stats["iters"] += int(res.nit)
        todays = cur[tab["date"][cur] == D]
        h = np.array([loc[t] for t in tab["home"][todays]])
        a = np.array([loc[t] for t in tab["away"][todays]])
        att, dfn = theta[3:3 + n], theta[3 + n:]
        lam = np.exp(theta[0] + theta[1] + att[h] - dfn[a])
        mu = np.exp(theta[0] + att[a] - dfn[h])
        P, clipped = score_matrix(lam, mu, theta[2])
        if stats is not None:
            stats["tau_clipped"] += clipped
        out_rows.append(todays)
        out_probs.append(probs_from_matrix(P))
        out_mats.append(P)
        out_rates.append(np.stack([lam, mu, np.full_like(lam, theta[2])], axis=1))
    final = {"intercept": float(theta[0]), "home": float(theta[1]), "rho": float(theta[2]),
             "promoted_att_offset": att_off, "promoted_def_offset": def_off,
             "promoted_team_seasons_used": n_prom}
    final["rates"] = np.concatenate(out_rates)
    return np.concatenate(out_rows), np.concatenate(out_probs), np.concatenate(out_mats), final


def gradient_check(tab, prom):
    """Finite-difference check of the analytic gradient on a real training window."""
    s_idx = ALL_SEASONS.index("2018-19")
    win = np.isin(tab["season"], [s_idx - 3, s_idx - 2, s_idx - 1]) & tab["played"]
    rows = np.flatnonzero(win)
    team_list = sorted(set(tab["home"][rows].tolist()) | set(tab["away"][rows].tolist()))
    loc = {t: i for i, t in enumerate(team_list)}
    n = len(team_list)
    hi = np.array([loc[t] for t in tab["home"][rows]])
    ai = np.array([loc[t] for t in tab["away"][rows]])
    x, y = tab["hg"][rows].astype(float), tab["ag"][rows].astype(float)
    rng = np.random.default_rng(1)
    w = rng.uniform(0.2, 1.0, size=len(rows))
    theta = rng.normal(0, 0.2, size=3 + 2 * n)
    theta[2] = -0.08
    pa, pd = rng.normal(0, 0.1, n), rng.normal(0, 0.1, n)
    args = (hi, ai, x, y, w, n, pa, pd, 3.0, (x == 0) & (y == 0), (x == 0) & (y == 1),
            (x == 1) & (y == 0), (x == 1) & (y == 1))
    f0, g = dc_objective(theta, *args)
    num = np.empty_like(theta)
    for i in range(len(theta)):
        e = np.zeros_like(theta)
        e[i] = 1e-6
        num[i] = (dc_objective(theta + e, *args)[0] - dc_objective(theta - e, *args)[0]) / 2e-6
    err = float(np.max(np.abs(num - g) / (1.0 + np.abs(g))))
    assert err < 1e-5, err
    return err


def leak_check(tab, prom):
    """Corrupt every result from a mid-season cut date onwards; predictions for matches on or
    before the cut date must be bit-identical (a prediction may only use strictly earlier dates)."""
    s_idx = ALL_SEASONS.index("2021-22")
    rows0, p0, _, _ = rolling_season(tab, prom, s_idx, 0.002, 8.0)
    cut = int(np.median(tab["date"][rows0]))
    bad = {k: v.copy() for k, v in tab.items()}
    m = (bad["season"] == s_idx) & (bad["date"] >= cut)
    bad["hg"][m], bad["ag"][m] = 9, 0
    rows1, p1, _, _ = rolling_season(bad, prom, s_idx, 0.002, 8.0)
    assert np.array_equal(rows0, rows1)
    early = tab["date"][rows0] <= cut
    assert early.sum() > 100 and np.array_equal(p0[early], p1[early]), "future results leaked into predictions"
    assert not np.allclose(p0[~early], p1[~early]), "corruption had no effect: check is vacuous"
    return int(early.sum())


# ----------------------------------------------------------------------------- statistics
def boot_ci(values, rng, n_boot=N_BOOT):
    values = np.asarray(values, dtype=float)
    n = len(values)
    means = np.empty(n_boot)
    step = 500
    for b0 in range(0, n_boot, step):
        idx = rng.integers(0, n, size=(min(step, n_boot - b0), n))
        means[b0:b0 + idx.shape[0]] = values[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return [float(lo), float(hi)]


def n_required(sd, delta):
    return int(math.ceil(((Z_ALPHA + Z_POWER) * sd / delta) ** 2))


def round_means(values, season, rnd):
    """Mean per (season, official matchday), in sorted key order."""
    keys = season * 100 + rnd
    uniq, inv = np.unique(keys, return_inverse=True)
    sums = np.bincount(inv, weights=values, minlength=len(uniq))
    cnt = np.bincount(inv, minlength=len(uniq))
    return uniq, sums / cnt, cnt


def paired_summary(a, b, season, rnd, rng):
    """Paired difference a - b in score points, per match and per round."""
    d = a - b
    _, rd, cnt = round_means(d, season, rnd)
    out = {
        "n_matches": int(len(d)), "n_rounds": int(len(rd)),
        "mean_diff": float(d.mean()),
        "sd_match": float(d.std(ddof=1)), "se_match": float(d.std(ddof=1) / math.sqrt(len(d))),
        "ci95_boot_matches": boot_ci(d, rng),
        "sd_round": float(rd.std(ddof=1)), "se_round": float(rd.std(ddof=1) / math.sqrt(len(rd))),
        "ci95_boot_rounds": boot_ci(rd, rng),
        "share_matches_a_better": float((d > 0).mean()),
        "share_rounds_a_better": float((rd > 0).mean()),
        "design_effect_round_clustering": float((rd.var(ddof=1) * cnt.mean()) / d.var(ddof=1)),
    }
    return out


def power_table(sd_match, sd_round):
    tab = {}
    for delta in (0.1, 0.2, 0.5, 1.0, 2.0):
        n_m = n_required(sd_match, delta)
        n_r = n_required(sd_round, delta)
        tab[str(delta)] = {
            "matches_needed_iid": n_m, "rounds_equiv": round(n_m / 10.0, 1),
            "seasons_equiv": round(n_m / 380.0, 2),
            "rounds_needed_using_round_sd": n_r, "seasons_equiv_using_round_sd": round(n_r / 38.0, 2),
        }
    return tab


def calibration_table(p, o, edges):
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi) if hi < 1.0 else (p >= lo) & (p <= hi)
        if m.sum() == 0:
            rows.append({"bin": f"[{lo:.1f},{hi:.1f})", "n": 0})
            continue
        rows.append({"bin": f"[{lo:.1f},{hi:.1f}{']' if hi >= 1.0 else ')'}", "n": int(m.sum()),
                     "mean_pred": float(p[m].mean()), "observed": float(o[m].mean())})
    return rows


def model_block(p, o, season, rnd):
    L = item_losses(p, o)
    sc = 100.0 * (1.0 - L.mean(axis=1))
    _, rs, _ = round_means(sc, season, rnd)
    pout = (p[:, :3] * o[:, :3]).sum(axis=1)
    return {
        "mean_score": float(sc.mean()),
        "mean_loss_1x2": float(L[:, 0].mean()), "mean_loss_total_o25": float(L[:, 1].mean()),
        "mean_loss_home_o15": float(L[:, 2].mean()), "mean_loss_away_o15": float(L[:, 3].mean()),
        "logloss_1x2": float(-np.log(np.maximum(pout, 1e-15)).mean()),
        "sd_match_score": float(sc.std(ddof=1)),
        "round_score_sd": float(rs.std(ddof=1)), "round_score_min": float(rs.min()),
        "round_score_max": float(rs.max()), "n_rounds": int(len(rs)),
    }, sc


def r(x, nd=4):
    if isinstance(x, dict):
        return {k: r(v, nd) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [r(v, nd) for v in x]
    if isinstance(x, float):
        return round(x, nd)
    return x


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_dir", help="path to a checkout of openfootball/football.json")
    ap.add_argument("--out", default="score_noise_results.json")
    ap.add_argument("--skip-tuning", action="store_true",
                    help="reuse FROZEN_DEFAULT instead of re-running the 2018-19..2020-21 grid")
    args = ap.parse_args()
    t0 = time.time()
    _self_check()
    tab, meta, names = load_all(args.data_dir)
    teams_by_season, prom = promoted_sets(tab)
    grad_err = gradient_check(tab, prom)
    n_leak_checked = leak_check(tab, prom)
    S = {s: i for i, s in enumerate(ALL_SEASONS)}
    results = {
        "script": "score_noise_check.py", "seed": SEED, "grid_goals": G - 1,
        "data": {"seasons": {s: {k: v for k, v in meta[s].items() if k != "teams"} for s in ALL_SEASONS},
                 "note": "2019-20: 66 restart matches dated July 2019 in the source were moved to July 2020; "
                         "2025-26 and 2026-27 store some 0-0 results as a bare [0, 0] list.",
                 "promoted": {s: sorted(names[t] for t in prom[S[s]]) for s in ALL_SEASONS[1:]}},
        "gradient_check_max_rel_err": grad_err,
        "leak_check": {"passed": True, "predictions_compared_bit_identical": n_leak_checked,
                       "method": "all 2021-22 results from the median match date onwards replaced by 9-0; "
                                 "predictions on or before that date unchanged"},
    }

    # ---- tuning on 2018-19..2020-21 only
    if args.skip_tuning:
        xi_star, kappa_star = FROZEN_DEFAULT
        results["tuning"] = {"skipped": True, "xi": xi_star, "kappa": kappa_star}
    else:
        grid = []
        for xi in XI_GRID:
            for kappa in KAPPA_GRID:
                scs = []
                for s in TUNE_SEASONS:
                    rows, p, _, _ = rolling_season(tab, prom, S[s], xi, kappa)
                    scs.append(match_score(p, outcomes(tab["hg"][rows], tab["ag"][rows])))
                grid.append({"xi": xi, "kappa": kappa, "mean_score": float(np.concatenate(scs).mean())})
        best = max(grid, key=lambda g: g["mean_score"])
        xi_star, kappa_star = best["xi"], best["kappa"]
        results["tuning"] = {"skipped": False, "seasons": TUNE_SEASONS, "n_matches": 1140,
                             "criterion": "mean blueprint score", "xi": xi_star, "kappa": kappa_star,
                             "half_life_days": math.log(2) / xi_star, "grid": r(grid)}
    print(f"[{time.time() - t0:6.1f}s] frozen hyper-parameters: xi={xi_star} kappa={kappa_star}", file=sys.stderr)

    variants = {"B1": (xi_star, kappa_star), "B1_decay_x2": (2 * xi_star, kappa_star),
                "B1_ridge_x2": (xi_star, 2 * kappa_star)}

    # ---- rolling predictions on the test seasons (+ the partial season, kept separate)
    stats = collections.Counter()
    preds, mats, finals, rates = {v: {} for v in variants}, {}, {}, {}
    for s in TEST_SEASONS + [PARTIAL_SEASON]:
        base_rows = None
        for v, (xi, kappa) in variants.items():
            rows, p, P, fin = rolling_season(tab, prom, S[s], xi, kappa, stats if v == "B1" else None)
            order = np.argsort(rows, kind="stable")
            rows, p, P = rows[order], p[order], P[order]
            if base_rows is None:
                base_rows = rows
            assert np.array_equal(rows, base_rows)
            assert np.allclose(p[:, :3].sum(axis=1), 1.0) and (p > 0).all() and (p < 1).all()
            preds[v][s] = p
            if v == "B1":
                rates[s] = fin.pop("rates")[order]
                mats[s], finals[s] = P, fin
        preds.setdefault("rows", {})[s] = base_rows
        print(f"[{time.time() - t0:6.1f}s] predicted {s}", file=sys.stderr)
    results["fit_diagnostics_B1"] = dict(stats) | {"end_of_season_params": r(finals, 4)}

    def gather(seasons):
        rows = np.concatenate([preds["rows"][s] for s in seasons])
        o = outcomes(tab["hg"][rows], tab["ag"][rows])
        pb0 = np.concatenate([np.tile(b0_probs(tab, S[s]), (len(preds["rows"][s]), 1)) for s in seasons])
        pv = {v: np.concatenate([preds[v][s] for s in seasons]) for v in variants}
        return rows, o, pb0, pv, tab["season"][rows], tab["round"][rows]

    rng = np.random.default_rng(SEED)
    report = {}
    for label, seasons in [("pooled_2021-22_to_2025-26", TEST_SEASONS)] + [(s, [s]) for s in TEST_SEASONS] \
            + [("partial_" + PARTIAL_SEASON + "_not_pooled", [PARTIAL_SEASON])]:
        rows, o, pb0, pv, sea, rnd = gather(seasons)
        blk = {"n_matches": int(len(rows))}
        blk["B0"], sc0 = model_block(pb0, o, sea, rnd)
        sc = {}
        for v in variants:
            blk[v], sc[v] = model_block(pv[v], o, sea, rnd)
        blk["B0_probs_by_season"] = {s: r(b0_probs(tab, S[s]).tolist()) for s in seasons}
        blk["paired_B1_minus_B0"] = paired_summary(sc["B1"], sc0, sea, rnd, rng)
        blk["paired_B1_minus_B1_decay_x2"] = paired_summary(sc["B1"], sc["B1_decay_x2"], sea, rnd, rng)
        blk["paired_B1_minus_B1_ridge_x2"] = paired_summary(sc["B1"], sc["B1_ridge_x2"], sea, rnd, rng)
        if label.startswith("pooled"):
            blk["power_B1_vs_B0_sd"] = power_table(blk["paired_B1_minus_B0"]["sd_match"],
                                                   blk["paired_B1_minus_B0"]["sd_round"])
            blk["power_similar_models_sd_decay_x2"] = power_table(
                blk["paired_B1_minus_B1_decay_x2"]["sd_match"], blk["paired_B1_minus_B1_decay_x2"]["sd_round"])
            blk["power_similar_models_sd_ridge_x2"] = power_table(
                blk["paired_B1_minus_B1_ridge_x2"]["sd_match"], blk["paired_B1_minus_B1_ridge_x2"]["sd_round"])
            blk["calibration_B1_home_win"] = calibration_table(
                pv["B1"][:, 0], o[:, 0], [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0])
            blk["outcome_frequencies"] = r(o.mean(axis=0).tolist())
            blk["mean_pred_B1"] = r(pv["B1"].mean(axis=0).tolist())
        report[label] = blk
    results["real_data"] = report

    # ---- oracle simulation: B1's probabilities are the truth
    rows, o, pb0, pv, sea, rnd = gather(TEST_SEASONS)
    P = np.concatenate([mats[s] for s in TEST_SEASONS]).reshape(len(rows), G * G)
    q = pv["B1"]                                   # true probabilities of the six indicators
    M = len(rows)
    srng = np.random.default_rng(SEED + 1)
    cdf = np.cumsum(P, axis=1)
    cdf[:, -1] = 1.0
    sim_true = np.empty((M, N_SIM))
    sim_b0 = np.empty((M, N_SIM))
    for m in range(M):
        cell = np.searchsorted(cdf[m], srng.random(N_SIM), side="right")
        hg, ag = cell // G, cell % G
        om = outcomes(hg, ag)
        sim_true[m] = match_score(q[m][None, :], om)
        sim_b0[m] = match_score(pb0[m][None, :], om)
    # exact expectations: E[(p-o)^2] = q(1-p)^2 + (1-q)p^2 for each indicator
    def exp_score(p):
        e = q * (1 - p) ** 2 + (1 - q) * p ** 2
        l = np.concatenate([0.5 * e[:, :3].sum(axis=1, keepdims=True), e[:, 3:]], axis=1)
        return 100.0 * (1.0 - l.mean(axis=1))
    keys = sea * 100 + rnd
    uniq, inv = np.unique(keys, return_inverse=True)
    R = len(uniq)
    onehot_r = np.zeros((R, M))
    onehot_r[inv, np.arange(M)] = 1.0 / 10.0
    s_ids = np.unique(sea)
    onehot_s = np.stack([(sea == s) / float((sea == s).sum()) for s in s_ids])
    diff = sim_true - sim_b0
    rt, rb, rd = onehot_r @ sim_true, onehot_r @ sim_b0, onehot_r @ diff      # (rounds, sims)
    st, sb, sd = onehot_s @ sim_true, onehot_s @ sim_b0, onehot_s @ diff      # (seasons, sims)
    pooled_d = diff.mean(axis=0)                                              # all 1,900 matches
    oracle = {
        "n_matches": M, "n_rounds": R, "replications": N_SIM,
        "expected_score_true_model_exact": float(exp_score(q).mean()),
        "expected_score_B0_exact": float(exp_score(pb0).mean()),
        "expected_gap_true_minus_B0_exact": float((exp_score(q) - exp_score(pb0)).mean()),
        "expected_score_true_model_sim": float(sim_true.mean()),
        "expected_score_B0_sim": float(sim_b0.mean()),
        "expected_item_losses_true_model": r((np.concatenate(
            [0.5 * (q[:, :3] * (1 - q[:, :3])).sum(axis=1, keepdims=True), q[:, 3:] * (1 - q[:, 3:])],
            axis=1)).mean(axis=0).tolist()),
        "true_model_score_sd": {
            "one_match_outcome_noise": float(math.sqrt(sim_true.var(axis=1, ddof=1).mean())),
            "one_match_total_incl_fixture_mix": float(sim_true.std(ddof=1)),
            "round_of_10_outcome_noise": float(math.sqrt(rt.var(axis=1, ddof=1).mean())),
            "round_of_10_total_incl_fixture_mix": float(rt.std(ddof=1)),
            "season_of_380_outcome_noise": float(math.sqrt(st.var(axis=1, ddof=1).mean())),
        },
        "B0_score_sd": {
            "one_match_outcome_noise": float(math.sqrt(sim_b0.var(axis=1, ddof=1).mean())),
            "round_of_10_outcome_noise": float(math.sqrt(rb.var(axis=1, ddof=1).mean())),
            "season_of_380_outcome_noise": float(math.sqrt(sb.var(axis=1, ddof=1).mean())),
        },
        "paired_true_minus_B0": {
            "mean_per_match": float(diff.mean()),
            "sd_one_match_outcome_noise": float(math.sqrt(diff.var(axis=1, ddof=1).mean())),
            "sd_one_match_total": float(diff.std(ddof=1)),
            "sd_round_of_10_outcome_noise": float(math.sqrt(rd.var(axis=1, ddof=1).mean())),
            "sd_round_of_10_total": float(rd.std(ddof=1)),
            "sd_season_of_380_outcome_noise": float(math.sqrt(sd.var(axis=1, ddof=1).mean())),
            "sd_five_seasons_1900_outcome_noise": float(pooled_d.std(ddof=1)),
            "prob_true_model_beats_B0_single_match": float((diff > 0).mean()),
            "prob_true_model_beats_B0_in_a_round": float((rd > 0).mean()),
            "prob_true_model_beats_B0_in_a_season": float((sd > 0).mean()),
            "prob_true_model_beats_B0_over_5_seasons": float((pooled_d > 0).mean()),
        },
    }
    # ---- exact (no simulation) cross-check and "how far apart must two models be" analysis
    rate = np.concatenate([rates[s] for s in TEST_SEASONS])           # (M, 3): lam, mu, rho of the truth
    O = outcomes(_I.reshape(-1), _J.reshape(-1))                      # (G*G, 6) indicators per score cell

    def cell_scores(p):                                               # (M, G*G) score in every cell
        return match_score(p[:, None, :], O[None, :, :])

    def moments(D):                                                   # exact per-match mean / variance
        e = (P * D).sum(axis=1)
        return e, (P * D * D).sum(axis=1) - e * e

    s_true = cell_scores(q)
    e_t, v_t = moments(s_true)
    e_d, v_d = moments(s_true - cell_scores(pb0))
    oracle["exact_cross_check"] = {
        "true_model_one_match_outcome_noise_sd": float(math.sqrt(v_t.mean())),
        "true_model_one_match_total_sd": float(math.sqrt(v_t.mean() + e_t.var())),
        "paired_true_minus_B0_mean": float(e_d.mean()),
        "paired_true_minus_B0_one_match_outcome_noise_sd": float(math.sqrt(v_d.mean())),
        "paired_true_minus_B0_one_match_total_sd": float(math.sqrt(v_d.mean() + e_d.var())),
    }
    prng = np.random.default_rng(SEED + 2)
    dist = []
    for sdev in (0.02, 0.05, 0.10, 0.20):
        eps = prng.normal(0.0, sdev, size=(M, 2))
        Pp, _ = score_matrix(rate[:, 0] * np.exp(eps[:, 0]), rate[:, 1] * np.exp(eps[:, 1]), rate[:, 2])
        pp = probs_from_matrix(Pp)
        e, v = moments(s_true - cell_scores(pp))
        gap, sd_m = float(e.mean()), float(math.sqrt(v.mean() + e.var()))
        n_need = n_required(sd_m, gap)
        dist.append({"log_rate_noise_sd": sdev, "expected_gap_points": gap, "paired_sd_per_match": sd_m,
                     "mean_abs_change_home_win_prob": float(np.abs(pp[:, 0] - q[:, 0]).mean()),
                     "matches_for_80pct_power": n_need, "seasons_equiv": round(n_need / 380.0, 2)})
    oracle["true_model_vs_noisy_copy"] = {
        "description": "a rival model whose expected goals differ from the truth by exp(N(0, sd^2)) per team "
                       "per match; gap = expected score advantage of the truth; n = matches for a paired "
                       "two-sided test (alpha 0.05) to detect it with 80% power",
        "rows": dist}
    results["oracle_simulation"] = oracle
    results["power_formula"] = "n = ceil(((1.95996 + 0.84162) * SD / delta)^2), paired, two-sided alpha 0.05, power 0.80"
    results["runtime_seconds"] = round(time.time() - t0, 1)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(r(results, 5), fh, indent=1, sort_keys=False)
        fh.write("\n")

    # ---- compact console summary
    pool = report["pooled_2021-22_to_2025-26"]
    print(f"hyper-parameters (tuned on 2018-19..2020-21): xi={xi_star}/day, kappa={kappa_star}")
    print(f"{'set':<28}{'n':>5}{'B0':>8}{'B1':>8}{'B1-B0':>8}{'SE':>7}{'rounds B1>B0':>14}{'LL B0':>8}{'LL B1':>8}")
    for label, blk in report.items():
        pd_ = blk["paired_B1_minus_B0"]
        print(f"{label[:27]:<28}{blk['n_matches']:>5}{blk['B0']['mean_score']:>8.3f}{blk['B1']['mean_score']:>8.3f}"
              f"{pd_['mean_diff']:>8.3f}{pd_['se_match']:>7.3f}{pd_['share_rounds_a_better']:>14.3f}"
              f"{blk['B0']['logloss_1x2']:>8.4f}{blk['B1']['logloss_1x2']:>8.4f}")
    print("pooled item losses  B0:", [round(pool['B0'][k], 4) for k in
          ("mean_loss_1x2", "mean_loss_total_o25", "mean_loss_home_o15", "mean_loss_away_o15")])
    print("pooled item losses  B1:", [round(pool['B1'][k], 4) for k in
          ("mean_loss_1x2", "mean_loss_total_o25", "mean_loss_home_o15", "mean_loss_away_o15")])
    print("B1 round score: sd=%.3f min=%.2f max=%.2f | match score sd=%.2f" % (
        pool["B1"]["round_score_sd"], pool["B1"]["round_score_min"], pool["B1"]["round_score_max"],
        pool["B1"]["sd_match_score"]))
    for k in ("paired_B1_minus_B0", "paired_B1_minus_B1_decay_x2", "paired_B1_minus_B1_ridge_x2"):
        b = pool[k]
        print(f"{k}: mean={b['mean_diff']:.4f} sd_match={b['sd_match']:.3f} sd_round={b['sd_round']:.3f} "
              f"CI_matches={[round(v, 3) for v in b['ci95_boot_matches']]} "
              f"CI_rounds={[round(v, 3) for v in b['ci95_boot_rounds']]} deff={b['design_effect_round_clustering']:.2f}")
    for k in ("power_B1_vs_B0_sd", "power_similar_models_sd_decay_x2", "power_similar_models_sd_ridge_x2"):
        print(k, {d: (v["matches_needed_iid"], v["seasons_equiv"]) for d, v in pool[k].items()})
    print("oracle:", json.dumps(r({k: v for k, v in oracle.items() if k != "true_model_vs_noisy_copy"}, 3)))
    for row in oracle["true_model_vs_noisy_copy"]["rows"]:
        print("noisy copy:", json.dumps(r(row, 4)))
    print(f"wrote {args.out} in {results['runtime_seconds']}s")


if __name__ == "__main__":
    main()
