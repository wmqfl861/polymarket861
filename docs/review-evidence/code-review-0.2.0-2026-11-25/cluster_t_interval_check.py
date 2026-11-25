"""核对 0.2.0 新的晋级统计口径：按轮聚类的 t 区间（round-cluster-t-v1）。

做四件事：
1. 独立重算聚类 t 区间，与产品函数 experiment_statistics.clustered_t 逐位比对（给出仓库根目录时）。
2. 用回测里真实的“逐场配对分差”按轮重抽 38 轮，测量真实改进为 0 时“下界大于 0”的误判率。
3. 按新规则（38 轮、一个候选时 10 重比较、三个候选时 30 重比较）计算不同真实改进下的通过概率。
4. 说明只改动少数几轮的候选在这套检验下能达到的最大 t 值。

输入是产品命令 `football backtest ... --archive-lag-hours 24 --details lag.jsonl` 生成的明细文件。
只读输入，不写任何文件。随机种子固定为 1。

用法：python3 cluster_t_interval_check.py lag.jsonl [仓库根目录]
"""

import json
import sys
from collections import defaultdict

import numpy as np
from scipy.stats import nct, t

rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
t60 = [r for r in rows if r["cutoff_minutes"] == 60]
grouped = defaultdict(list)
for r in t60:
    grouped[(r["season"], r["round"])].append(r["model_score"] - r["baseline_score"])
keys = sorted(grouped)
sizes = np.array([len(grouped[k]) for k in keys])
sums = np.array([sum(grouped[k]) for k in keys])
G, N = len(keys), int(sizes.sum())


def interval(sums, sizes, family):
    g, n = len(sums), sizes.sum()
    mean = sums.sum() / n
    se = np.sqrt(g / (g - 1) * np.square(sums - sizes * mean).sum()) / n
    crit = t.ppf(1 - 0.025 / family, g - 1)
    return mean, se, crit


mean, se, crit = interval(sums, sizes, 1)
print(f"1. 五季 T-60：{N} 场、{G} 轮，B1 对 B0 的逐场分差均值 {mean:.4f}，聚类标准误 {se:.4f}，95% 区间 [{mean - crit * se:.4f}, {mean + crit * se:.4f}]")
if len(sys.argv) > 2:
    sys.path.insert(0, sys.argv[2] + "/src")
    from polymarket861 import experiment_statistics

    product = experiment_statistics.clustered_t([grouped[k] for k in keys], 1)["intervals"][0]
    print(f"   产品函数：均值 {product['difference']:.4f}，标准误 {product['standard_error']:.4f}，最大偏差 "
          f"{max(abs(product['difference'] - mean), abs(product['standard_error'] - se)):.2e}")

per_round = sums / sizes
sd_round = per_round.std(ddof=1)
print(f"   按轮平均分差的标准差 {sd_round:.3f} 分（每轮 {int(sizes.min())}–{int(sizes.max())} 场）")

rng = np.random.default_rng(1)
REPS, WINDOW = 400_000, 38
centred = sums - sizes * mean  # 真实改进为 0 的总体
print(f"2. 真实改进为 0，从 {G} 个真实轮次里有放回抽 {WINDOW} 轮，{REPS:,} 次：")
idx = rng.integers(0, G, size=(REPS, WINDOW))
s, n = centred[idx], sizes[idx]
m = s.sum(1) / n.sum(1)
e = np.sqrt(WINDOW / (WINDOW - 1) * np.square(s - n * m[:, None]).sum(1)) / n.sum(1)
stat = m / e
for family in (1, 2, 10, 30):
    c = t.ppf(1 - 0.025 / family, WINDOW - 1)
    rate = (stat > c).mean()
    print(f"   比较重数 {family:>2}：临界 t {c:.3f}，名义误判率 {0.025 / family:.4%}，模拟 {rate:.4%}")

print("3. 新规则下的通过概率（非中心 t 分布；前向 38 轮，历史回放 76 轮，两段都要求下界大于 0）：")
print("   按轮标准差   真实改进   单候选(10重) 前向／两段   三候选(30重) 前向／两段")
for sd, label in ((0.2, "0.2 相近模型下沿"), (0.7, "0.7 相近模型上沿"), (sd_round, f"{sd_round:.2f} B1对B0实测")):
    for delta in (0.05, 0.1, 0.25, 0.5, 1.0, 1.89):
        cells = []
        for family in (10, 30):
            power = []
            for g in (38, 76):
                c = t.ppf(1 - 0.025 / family, g - 1)
                power.append(1 - nct.cdf(c, g - 1, delta / (sd / np.sqrt(g))))
            cells.append(f"{power[0]:6.1%} ／ {power[0] * power[1]:6.1%}")
        print(f"   {label:<18} {delta:>5.2f}      {cells[0]}        {cells[1]}")

print("4. 只改动少数几轮的候选（每轮只改 1 场、改进完全相同，这是对它最有利的情形）能达到的最大 t 值：")
for rounds, family, note in ((190, 2, "五季历史研究，2 个候选"), (38, 10, "前向 38 轮，单候选"), (38, 30, "前向 38 轮，三候选")):
    c = t.ppf(1 - 0.025 / family, rounds - 1)
    line = []
    need = None
    for k in range(1, rounds + 1):
        s = np.zeros(rounds)
        s[:k] = 1.0
        n = np.full(rounds, 10.0)
        m = s.sum() / n.sum()
        e = np.sqrt(rounds / (rounds - 1) * np.square(s - n * m).sum()) / n.sum()
        if k in (3, 5, 8, 10):
            line.append(f"{k} 轮 {m / e:.2f}")
        if need is None and m / e > c:
            need = k
    print(f"   {note}：临界 t {c:.3f}；" + "，".join(line) + f"；至少要改动 {need} 轮才有可能通过")
