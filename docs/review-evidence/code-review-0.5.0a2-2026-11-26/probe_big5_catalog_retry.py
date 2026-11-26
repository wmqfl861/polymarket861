#!/usr/bin/env python3
"""四国值守循环连续运行时，目录刷新失败几次、失败多久，会丢掉紧接着的固定时窗。

probe_big5_catalog_age.py 用一次性的调用量出了门槛：某场比赛最近一次成功的目录早于 10 分钟时，
这场比赛的订单簿根本不会去取。这个探针回答下一个问题：常驻的值守循环自己多久重试一次目录，
也就是什么样的故障会让目录真的超过 10 分钟。

读码（big5_market.watch）：目录每 300 秒刷新一次；“上一次结果不完整就 30 秒后重试”是在提交下一次
刷新的那一刻、按当时手里的上一次结果决定的。所以一次刷新失败之后，要等满 300 秒才有第二次，
第二次提交之后才进入 30 秒一次的重试。

做法：用一只模拟时钟驱动真正的 watch 循环（--cycles N、--interval 2 的同一入口），每走一步时钟前进 2 秒，
这一步提交给线程池的目录刷新和采集任务全部结束之后才走下一步。上游沿用 tests/test_big5_market.py 的合成赛事；
指定的时间段内 Gamma 的请求全部失败，订单簿接口一直正常。每组从开球前 60 分钟的时窗之前十几分钟
走到时窗结束，记录每一次目录刷新的时刻和成败、两个固定时窗各试了几次、存下多少份订单簿、
以及这段时间里每分钟一次的例行采样成功了几次。

后台的目录刷新偶尔会和采样任务同时写库，这时 watch 循环以 sqlite3.OperationalError("database is locked")
整个退出（审核稿 U0-1，专门的探针是 probe_big5_refresh_overlap.py）。碰上时这一组换一个新目录重来，
放弃了几次记在 runs_abandoned_because_the_loop_raised_database_is_locked 里。

用法：
  python -I probe_big5_catalog_retry.py --src <仓库>/src --tests <仓库>/tests <空的临时目录>

不联网，不读写仓库，不触碰任何真实数据目录。约 1 分钟。
"""

import argparse
import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("--tests", required=True)
parser.add_argument("scratch")
args = parser.parse_args()
sys.path[:0] = [args.src, args.tests]

import test_big5_market as fixture  # noqa: E402

import polymarket861  # noqa: E402
from polymarket861 import (  # noqa: E402
    big5_market as big5,
    market_archive as archive,
    market_provider as provider,
    store,
)
from polymarket861.store import iso  # noqa: E402

CUTOFF = provider.moment(fixture.DAY + "T13:00:00Z")  # 开球前 60 分钟：决策窗 [−25, 0)，执行窗 [+5, +10)
STEP = 2
# (说明, 循环开始的时刻, Gamma 失败的时间段)，时刻都是相对 CUTOFF 的秒数。
# 循环在 start 做第一次目录，之后的定时刷新落在 start+300、start+600。
ARMS = [
    ("对照：没有故障", -640, []),
    ("一次定时刷新失败（−340 那一次）", -640, [(-345, -335)]),
    ("相隔 5 分钟的两次定时刷新各失败一次（−340 和 −40）", -640, [(-345, -335), (-45, -35)]),
    ("Gamma 连续中断 5 分 10 秒（−345 到 −35）", -640, [(-345, -35)]),
    ("换一个相位：定时刷新落在 −600 和 −300，Gamma 连续中断 5 分 10 秒（−605 到 −295）", -900, [(-605, -295)]),
    ("同一相位，Gamma 连续中断 10 分钟（−605 到 −5）", -900, [(-605, -5)]),
]


def run(index, label, start, outages):
    abandoned = 0
    while True:
        outcome = attempt(index, abandoned, label, start, outages)
        if outcome is not None:
            return {**outcome, "runs_abandoned_because_the_loop_raised_database_is_locked": abandoned}
        abandoned += 1
        if abandoned == 20:
            raise SystemExit(f"arm {index}: the watch loop raised 'database is locked' 20 times in a row")


def attempt(index, abandoned, label, start, outages):
    data = Path(args.scratch) / (f"arm{index}" + (f"-again{abandoned}" if abandoned else ""))
    sim = SimpleNamespace(now=CUTOFF + timedelta(seconds=start), busy=0, gate=threading.Condition(), refreshes=[])

    def offset():
        return round((sim.now - CUTOFF).total_seconds())

    def upstream(url, params=None, **kwargs):
        if not url.endswith("/books") and any(lo <= offset() < hi for lo, hi in outages):
            raise OSError("simulated Gamma failure")
        return fixture.gamma(url, params, **kwargs)

    def recorded(real):
        def call(*a, **k):
            result = real(*a, **k)
            complete = all(v["page_walk_complete"] for v in result["leagues"].values())
            with sim.gate:
                sim.refreshes.append({"at": offset(), "succeeded": complete})
            return result

        return call

    def finished(_):
        with sim.gate:
            sim.busy -= 1
            sim.gate.notify_all()

    class Pool(ThreadPoolExecutor):
        """与产品用的线程池相同，只是记下还有几个已提交的任务没有结束。"""

        def submit(self, fn, /, *a, **k):
            with sim.gate:
                sim.busy += 1
            future = super().submit(fn, *a, **k)
            future.add_done_callback(finished)
            return future

    def advance(seconds):
        with sim.gate:
            if not sim.gate.wait_for(lambda: sim.busy == 0, timeout=300):
                raise RuntimeError("probe: submitted tasks did not finish within 300 s")
        sim.now += timedelta(seconds=seconds)

    cycles = (12 - start) // STEP
    with ExitStack() as stack:
        for module in (store, big5, archive, fixture):
            stack.enter_context(patch.object(module, "now_utc", side_effect=lambda: iso(sim.now)))
        stack.enter_context(patch.object(provider, "get", side_effect=upstream))
        stack.enter_context(patch.object(big5, "time", SimpleNamespace(sleep=advance)))
        stack.enter_context(patch.object(big5, "ThreadPoolExecutor", Pool))
        stack.enter_context(patch.object(big5, "catalog", recorded(big5.catalog)))
        try:
            big5.watch(data, ["sea"], fixture.START, fixture.END, cycles=cycles, interval=STEP, discover=True)
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc):
                raise
            return None
        report = big5.weekly_report(
            data, iso(CUTOFF - timedelta(minutes=1)), iso(CUTOFF + timedelta(minutes=1))
        )
    counts = report["by_league"]["sea"]
    with closing(sqlite3.connect(big5.research_root(data) / "runtime.sqlite3")) as db:
        books = db.execute("SELECT COUNT(*) FROM ma_books").fetchone()[0]
        fixed, routine = [], {"complete": 0, "not_complete": 0}
        for slot, attempts, success, text in db.execute(
            "SELECT slot,attempts,success,report FROM big5_slots ORDER BY slot"
        ):
            item = json.loads(text) if text else {}
            name = slot.split("@")[0]
            if name.startswith("t1_"):
                fixed.append(
                    {
                        "slot": name,
                        "attempts": attempts,
                        "success": bool(success),
                        "state": item.get("state"),
                        "received": item.get("received"),
                        "expected": item.get("expected"),
                        "error": item.get("error"),
                    }
                )
            else:
                routine["complete" if success else "not_complete"] += 1
    return {
        "arm": label,
        "loop_started_at": start,
        "gamma_failing_during": outages,
        "catalogue_refreshes": sim.refreshes,
        "fixed_windows": fixed,
        "fixed_windows_complete": counts["fixed_windows_complete"],
        "fixed_windows_due": counts["fixed_windows_due"],
        "t1_pairs_complete": counts["t1_pairs_complete"],
        "t1_pairs_due": counts["t1_pairs_due"],
        "routine_one_minute_samples": routine,
        "archived_books": books,
    }


json.dump(
    {
        "package_version": polymarket861.__version__,
        "python": sys.version.split()[0],
        "times": "seconds relative to the T-60m target; decision window [-25, 0), execution window [+5, +10)",
        "arms": [run(i, *arm) for i, arm in enumerate(ARMS)],
    },
    sys.stdout,
    ensure_ascii=False,
    indent=1,
)
print()
