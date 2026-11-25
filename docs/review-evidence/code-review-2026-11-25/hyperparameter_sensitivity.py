"""B1 超参数敏感度：只在内存里替换 football_model 的两个常量，重跑产品回测，不改任何仓库文件。

用法（INPUT 为 `pm861 football fetch INPUT --from-season 2010-11 --to-season 2026-27` 的输出目录）：
    PYTHONDONTWRITEBYTECODE=1 python3 hyperparameter_sensitivity.py INPUT
"""

import itertools
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from polymarket861 import football, football_model  # noqa: E402

fixtures = football.load_fixtures(Path(sys.argv[1]), archive_lag_hours=24)


def run(decay, ridge):
    football_model.DECAY_PER_DAY, football_model.RIDGE = decay, ridge
    rows = []
    football.backtest(fixtures, "2021-22", "2025-26", horizons=(60,), bootstrap=0,
                      archive_lag_hours=24, details_sink=rows.append)
    return {r["fixture_id"]: r for r in rows}


shipped = run(0.002, 8.0)
ids = sorted(shipped)
rounds = [(shipped[i]["season"], shipped[i]["round"]) for i in ids]
base = np.array([shipped[i]["model_score"] for i in ids])
print(f"shipped decay=0.002 ridge=8.0: {base.mean():.4f}")
for decay, ridge in itertools.product((0.001, 0.002, 0.003, 0.004), (2.0, 4.0, 8.0, 16.0)):
    if (decay, ridge) == (0.002, 8.0):
        continue
    result = run(decay, ridge)
    delta = np.array([result[i]["model_score"] for i in ids]) - base
    groups = defaultdict(list)
    for key, value in zip(rounds, delta):
        groups[key].append(value)
    per_round = np.array([np.mean(v) for v in groups.values()])
    print(f"decay={decay:.3f} ridge={ridge:4.1f}: vs shipped {delta.mean():+.4f}, per-round paired sd {per_round.std(ddof=1):.3f}")
