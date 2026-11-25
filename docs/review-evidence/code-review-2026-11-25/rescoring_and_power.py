"""独立重算回测明细的四项评分，并测量校准斜率、按轮波动和检验功效。

用法（明细文件由产品命令 `football backtest ... --details lag.jsonl` 生成）：
    python3 -I rescoring_and_power.py lag.jsonl

只读输入文件，不依赖 polymarket861 包，不写任何文件。
"""

import json
import math
import sys
from collections import defaultdict

import numpy as np

KEYS = ("home_win", "draw", "away_win", "total_over_2_5", "home_over_1_5", "away_over_1_5")
GROUPS = (("1x2", [0, 1, 2]), ("total", [3]), ("home", [4]), ("away", [5]))


def outcome(score):
    h, a = score
    return [h > a, h == a, h < a, h + a > 2.5, h > 1.5, a > 1.5]


def blueprint_score(p, score):
    o = outcome(score)
    x = [p[k] for k in KEYS]
    losses = [sum((x[i] - o[i]) ** 2 for i in range(3)) / 2] + [(x[i] - o[i]) ** 2 for i in (3, 4, 5)]
    return 100 * (1 - sum(losses) / 4)


def matrix_score(P, o):
    first = ((P[:, :3] - o[:, :3]) ** 2).sum(1) / 2
    return 100 * (1 - (first + ((P[:, 3:] - o[:, 3:]) ** 2).sum(1)) / 4)


def calibration_slope(p, y):
    """Logistic recalibration y ~ a + b*logit(p); b < 1 means over-confident probabilities."""
    x = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
    a, b = 0.0, 1.0
    for _ in range(100):
        q = 1 / (1 + np.exp(-(a + b * x)))
        w = q * (1 - q)
        grad = np.array([(y - q).sum(), ((y - q) * x).sum()])
        hess = np.array([[w.sum(), (w * x).sum()], [(w * x).sum(), (w * x * x).sum()]])
        step = np.linalg.solve(hess, grad)
        a, b = a + step[0], b + step[1]
        if abs(step).max() < 1e-10:
            break
    return a, b, math.sqrt(np.linalg.inv(hess)[1, 1])


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def main(path):
    rows = [json.loads(line) for line in open(path, encoding="utf-8")]
    t60 = [r for r in rows if r["cutoff_minutes"] == 60]
    worst = unnormalised = 0.0
    for r in rows:
        worst = max(worst, abs(blueprint_score(r["model_probabilities"], r["score"]) - r["model_score"]))
        worst = max(worst, abs(blueprint_score(r["baseline_probabilities"], r["score"]) - r["baseline_score"]))
        assert outcome(r["score"]) == r["observed"]
        for name in ("model_probabilities", "baseline_probabilities"):
            unnormalised = max(unnormalised, abs(sum(r[name][k] for k in KEYS[:3]) - 1))
    print(f"rows {len(rows)}; independent rescoring max abs error {worst:.2e}")
    print(f"largest deviation of home+draw+away from 1: {unnormalised:.2e}")

    model = np.array([r["model_score"] for r in t60])
    base = np.array([r["baseline_score"] for r in t60])
    print(f"T-60: model {model.mean():.5f}  B0 {base.mean():.5f}  paired {np.mean(model - base):.5f}  n={len(t60)}")

    by_round = defaultdict(list)
    for r in t60:
        by_round[(r["season"], r["round"])].append(r["model_score"] - r["baseline_score"])
    diff = np.array([np.mean(v) for v in by_round.values()])
    sd = diff.std(ddof=1)
    print(f"rounds {len(diff)}; per-round paired difference mean {diff.mean():.3f} sd {sd:.3f}; "
          f"share of rounds where the model scores below B0 {np.mean(diff < 0):.3f}")
    for delta in (0.25, 0.5, 1.0, 1.89):
        need = ((1.96 + 0.84) * sd / delta) ** 2
        se = sd / math.sqrt(12)
        print(f"  true gain {delta:.2f}: rounds for 80% power {need:6.0f} ({need / 38:4.1f} seasons); "
              f"power of a 12-round window: 1 candidate {1 - phi(1.96 - delta / se):.2f}, "
              f"3 candidates {1 - phi(2.394 - delta / se):.2f}")

    obs = np.array([outcome(r["score"]) for r in t60], float)
    P = np.array([[r["model_probabilities"][k] for k in KEYS] for r in t60])
    B = np.array([[r["baseline_probabilities"][k] for k in KEYS] for r in t60])
    for i, key in enumerate(KEYS):
        a, b, se = calibration_slope(P[:, i], obs[:, i])
        print(f"calibration {key:15s} slope {b:.3f} (se {se:.3f})  Brier model {np.mean((P[:, i] - obs[:, i]) ** 2):.5f}"
              f"  B0 {np.mean((B[:, i] - obs[:, i]) ** 2):.5f}")
    p, y = P[:, 3], obs[:, 3]
    for lo, hi in ((0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.65), (0.65, 1)):
        m = (p >= lo) & (p < hi)
        print(f"  total over 2.5, forecast in [{lo:.2f},{hi:.2f}): n={m.sum():4d} mean forecast {p[m].mean():.3f} observed {y[m].mean():.3f}")

    seasons = np.array([r["season"] for r in t60])
    gains = []
    for held in sorted(set(seasons)):
        train, test, Q = seasons != held, seasons == held, P.copy()
        for _, idx in GROUPS:
            best = max(
                (matrix_score(np.where(np.isin(np.arange(6), idx), w * P + (1 - w) * B, P)[train], obs[train]).mean(), w)
                for w in np.linspace(0, 1.5, 31)
            )[1]
            Q[:, idx] = best * P[:, idx] + (1 - best) * B[:, idx]
        gains.append(matrix_score(Q[test], obs[test]).mean() - matrix_score(P[test], obs[test]).mean())
        print(f"  leave-one-season-out linear shrink toward B0, held out {held}: gain {gains[-1]:+.4f}")
    print(f"  mean out-of-sample gain {np.mean(gains):+.4f}")


if __name__ == "__main__":
    main(sys.argv[1])
