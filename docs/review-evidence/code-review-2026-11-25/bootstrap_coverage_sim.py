"""模拟“按轮整组重抽样、取分位数下界 > 0 即合格”在真实改进为零时的误判率。

对应 jobs_research._paired_evidence 与 strategy._interval 的判据：4000 次（此处 2000 次）按轮重抽样，
下界取 0.025/候选数 分位。用法：python3 -I bootstrap_coverage_sim.py
"""

import numpy as np

rng = np.random.default_rng(1)


def false_positive_rate(clusters, candidates, heavy_tailed, reps=4000, resamples=2000):
    hits = 0
    for _ in range(reps):
        x = rng.standard_t(4, size=clusters) if heavy_tailed else rng.normal(size=clusters)
        means = x[rng.integers(0, clusters, size=(resamples, clusters))].mean(axis=1)
        hits += np.quantile(means, 0.025 / candidates) > 0
    return hits / reps


for clusters in (12, 38):
    for candidates in (1, 3):
        print(f"rounds {clusters:2d}, candidates {candidates}: nominal {0.025 / candidates:.4f}, "
              f"actual {false_positive_rate(clusters, candidates, False):.4f} (normal), "
              f"{false_positive_rate(clusters, candidates, True):.4f} (heavy-tailed)")
