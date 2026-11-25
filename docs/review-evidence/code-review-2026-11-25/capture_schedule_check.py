"""用产品自己的 market_archive.next_capture 列出一场比赛的采样时刻，检查收盘窗口能否被覆盖。

收盘簿的合格窗口是 [实际开球 − 30 秒, 实际开球)（archive_proof.eligible_book 的 close 阶段）。
用法：PYTHONDONTWRITEBYTECODE=1 python3 capture_schedule_check.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from polymarket861.market_archive import next_capture  # noqa: E402

kickoff = datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc)
now = kickoff - timedelta(minutes=63)
plan = []
while True:
    target = next_capture(kickoff, now, delay=5)
    if not target:
        break
    plan.append(target)
    now = target[0] + timedelta(seconds=1)
for moment, role in plan:
    offset = (moment - kickoff).total_seconds()
    if role != "periodic" or offset > -200 or -3700 < offset < -3500:
        print(f"{offset:+8.0f} s  {role}")
for late in (0, 10, 30, 60, 120, 180):
    actual = kickoff + timedelta(seconds=late)
    inside = [(m - kickoff).total_seconds() for m, _ in plan if actual - timedelta(seconds=30) <= m < actual]
    print(f"actual kickoff {late:+4d} s after schedule -> captures inside the close window: {inside or 'NONE'}")
