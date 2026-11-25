"""测量比赛日调度进程的主循环耗时会不会随赛季里累积的记录数增长。

调度进程 `season run --repeat` 每秒约 5 次调用 Coordinator.tick，每 15 秒（以及每次目录时段完成后）
调用一次 season._plan。本脚本在临时目录里预置不同数量的“已完成时段”（season_slots）和
“赛事目录收件”（ma_events），然后计时这两个函数。预置的行数按产品自己的采样计划推算：
一场比赛 7 天内约 1,630 个时段、约 1,460 份赛事目录收件，本赛季还剩 27 轮 270 场。
预置的全部是已经结束的旧记录，所以两个函数在每种规模下要做的“新工作”相同，耗时差异只来自旧记录的数量。
最后在最大规模上临时加一个只覆盖未完成时段的部分索引，再测一次 tick，用来验证文档里的优化建议。
不访问网络（把五个周期服务标记为已启动，tick 就不会去调用它们），不修改任何仓库文件。

用法：PYTHONDONTWRITEBYTECODE=1 python3 coordinator_tick_scaling.py [仓库根目录] [已赛场数=0,70,140,270]
"""

import statistics
import sys
import tempfile
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
PLAYED = [int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else [0, 70, 140, 270]
sys.path.insert(0, str(ROOT / "src"))
from polymarket861 import season, store  # noqa: E402

SLOTS_PER_FIXTURE = 1630  # 901 个盘口批次 + 731 个目录时段，另有少量纸面决策时段
EVENTS_PER_FIXTURE = 1462  # 731 个目录时段 × 主赛事和扩展盘赛事


def timed(fn, repeat=5):
    values = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        values.append(time.perf_counter() - start)
    return statistics.median(values)


def measure(played, partial_index=False):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        now = store.instant(store.now_utc())
        with closing(season._db(root)) as db, db:
            for n in range(played):
                kick = now - timedelta(days=2 + n // 10 * 7, hours=n % 10)
                slug = f"epl-t{n // 26:02d}{chr(97 + n % 26)}-opp-{kick:%Y-%m-%d}"
                kickoff = store.iso(kick)
                db.execute("INSERT INTO season_fixtures VALUES(?,?,?,?,?)", (slug, kickoff, "{}", kickoff, kickoff))
                db.executemany(
                    "INSERT INTO season_slots VALUES(?,?,?,?,?,?,?,'done',NULL,NULL,'{}')",
                    (
                        (
                            f"{slug}-{i}", slug, kickoff,
                            store.iso(kick - timedelta(seconds=370 * i)),
                            store.iso(kick - timedelta(seconds=370 * i - 45)),
                            "periodic" if i % 9 else "state",
                            "book" if i % 9 else "metadata",
                        )
                        for i in range(SLOTS_PER_FIXTURE)
                    ),
                )
                db.executemany(
                    "INSERT INTO ma_events VALUES(?,?,?,?,?,?)",
                    (
                        (
                            slug if i % 2 else slug + "-more-markets", slug, str(100000 + n), kickoff,
                            store.iso(kick - timedelta(seconds=413 * (i // 2))), f"{n:04d}{i:060d}",
                        )
                        for i in range(EVENTS_PER_FIXTURE)
                    ),
                )
            if partial_index:
                db.execute("CREATE INDEX review_open_slots ON season_slots(deadline) WHERE state IN ('pending','running')")
        agent = season.Coordinator(root, workers=6, once=True)
        agent.services_started = set(season._SERVICES)  # 不启动任何联网的周期服务
        try:
            agent.tick()  # 预热：建立连接和心跳行
            plan = timed(lambda: season._plan(root, store.instant(store.now_utc())))
            agent.next_plan = time.monotonic() + 3600  # 单独计时 tick 自己的部分
            tick = timed(agent.tick, repeat=15)
        finally:
            agent.close()
        with closing(season._db(root)) as db:
            slots = db.execute("SELECT count(*) FROM season_slots").fetchone()[0]
            events = db.execute("SELECT count(*) FROM ma_events").fetchone()[0]
        return {"played_fixtures": played, "season_slots": slots, "ma_events": events,
                "plan_seconds": round(plan, 4), "tick_seconds": round(tick, 4)}


print("已赛场数  时段行数   目录收件行数  _plan 一次(秒)  tick 一次(秒，不含 _plan)")
for played in PLAYED:
    r = measure(played)
    print(f"{r['played_fixtures']:>7}  {r['season_slots']:>9,}  {r['ma_events']:>11,}  {r['plan_seconds']:>13.4f}  {r['tick_seconds']:>12.4f}")
r = measure(max(PLAYED), partial_index=True)
print(f"已赛 {r['played_fixtures']} 场、给未完成时段加部分索引后：tick 一次 {r['tick_seconds']:.4f} 秒（_plan 不受这个索引影响，仍是 {r['plan_seconds']:.4f} 秒）")
