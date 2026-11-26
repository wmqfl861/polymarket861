#!/usr/bin/env python3
"""值守进程采集固定时窗的同时，另一个进程反复运行周报和诊断查询，会不会互相妨碍。

审核稿第 2 节建议在值守进程运行期间执行 `market league-report` 和两条 SQL 查询。
这个探针沿用 tests/test_big5_market.py 的合成赛事，让模拟时钟按真实速度走过
T−60 分钟的决策窗和执行窗（约 24 秒），其间：
  - 主进程运行 big5.watch(..., interval=1, discover=True)，与 `market league-watch` 的入口相同；
  - 另起一个进程不停地调用 big5.weekly_report，并用普通的 sqlite3 连接执行审核稿里的两条查询。
对照组没有第二个进程。比较两组的时窗完成数，并记录两边有没有抛出异常。

用法：
  python -I probe_big5_report_during_watch.py --src <仓库>/src --tests <仓库>/tests <空的临时目录>

不联网，不读写仓库。耗时约 50 秒，因为时钟按真实速度走。
"""

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from contextlib import ExitStack, closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("--tests", required=True)
parser.add_argument("scratch")
parser.add_argument("--role", choices=("main", "reporter"), default="main")
parser.add_argument("--epoch", type=float)
args = parser.parse_args()
sys.path[:0] = [args.src, args.tests]

import test_big5_market as fixture  # noqa: E402
from polymarket861 import (  # noqa: E402
    big5_market as big5,
    market_archive as archive,
    market_provider as provider,
    store,
)
from polymarket861.store import iso  # noqa: E402

CUTOFF = provider.moment(fixture.DAY + "T13:00:00Z")  # 开球前 60 分钟
BEGIN = -8  # 模拟时钟从决策窗内开始（决策窗是截止前 25 秒到截止）
SECONDS = 24  # 走到截止后 16 秒，执行窗是截止后 5 到 10 秒
WINDOW = (iso(CUTOFF - timedelta(minutes=1)), iso(CUTOFF + timedelta(minutes=1)))
QUERIES = (
    "SELECT live, ended, COUNT(*) FROM ma_event_states GROUP BY 1, 2",
    "SELECT slot, success, json_extract(report, '$.state') FROM big5_slots ORDER BY slot",
)


def clock(epoch):
    return lambda: iso(CUTOFF + timedelta(seconds=BEGIN + time.time() - epoch))


def patched(stack, epoch):
    for module in (store, big5, archive, fixture):
        stack.enter_context(patch.object(module, "now_utc", side_effect=clock(epoch)))


def reporter(data, epoch):
    """第二个进程：不停地出周报、跑查询，直到值守结束。"""
    counts = {"weekly_report_ok": 0, "sql_ok": 0, "errors": {}}
    database = big5.research_root(data) / "runtime.sqlite3"
    with ExitStack() as stack:
        patched(stack, epoch)
        while time.time() - epoch < SECONDS:
            try:
                big5.weekly_report(data, *WINDOW)
                counts["weekly_report_ok"] += 1
                with closing(sqlite3.connect(database)) as db:
                    for query in QUERIES:
                        db.execute(query).fetchall()
                counts["sql_ok"] += 1
            except Exception as exc:  # 记下来，不中断
                key = type(exc).__name__ + ": " + str(exc)[:120]
                counts["errors"][key] = counts["errors"].get(key, 0) + 1
    return counts


def arm(index, with_reporter):
    data = Path(args.scratch) / f"arm{index}"
    epoch = time.time()
    child = None
    if with_reporter:
        child = subprocess.Popen(
            [sys.executable, "-I", __file__, "--src", args.src, "--tests", args.tests, str(data), "--role", "reporter", "--epoch", repr(epoch)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    row = {"second_process": with_reporter}
    with ExitStack() as stack:
        patched(stack, epoch)
        stack.enter_context(patch.object(provider, "get", side_effect=fixture.gamma))
        try:
            outcome = big5.watch(data, ["sea"], fixture.START, fixture.END, cycles=SECONDS, interval=1, discover=True)
            row.update(watch="returned", watch_states=outcome["states"])
        except BaseException as exc:
            row.update(watch="raised", exception=type(exc).__name__, message=str(exc)[:200])
    if child:
        out, err = child.communicate(timeout=120)
        row["second_process_exit"] = child.returncode
        row["second_process_counts"] = json.loads(out) if child.returncode == 0 else err.strip()[-300:]
    with patch.object(big5, "now_utc", return_value=iso(CUTOFF + timedelta(minutes=2))):
        counts = big5.weekly_report(data, *WINDOW)["by_league"]["sea"]
    with closing(sqlite3.connect(big5.research_root(data) / "runtime.sqlite3")) as db:
        row["slots"] = [
            {"slot": slot, "success": bool(success), "state": state}
            for slot, success, state in db.execute(QUERIES[1])
        ]
        row["event_states"] = [list(r) for r in db.execute(QUERIES[0])]
        row["archived_books"] = db.execute("SELECT COUNT(*) FROM ma_books").fetchone()[0]
    row.update(
        fixed_windows_due=counts["fixed_windows_due"],
        fixed_windows_complete=counts["fixed_windows_complete"],
        t1_pairs_due=counts["t1_pairs_due"],
        t1_pairs_complete=counts["t1_pairs_complete"],
    )
    return row


if args.role == "reporter":
    json.dump(reporter(Path(args.scratch), args.epoch), sys.stdout)
else:
    result = {
        "对照：只有值守进程": arm(0, False),
        "值守期间第二个进程不停地出周报和查询": arm(1, True),
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
    print()
