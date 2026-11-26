#!/usr/bin/env python3
"""用 0.5.0a2 打开 0.5.0a1 采出来的四国研究目录：周报怎样重新判定，采集能不能接着做。

先用上一份审核的 probe_big5_missing_flags.py 和 0.5.0a1 的源码在一个空目录里采出五组数据
（交易所标记齐全、都没有、都是 null、只有 live、只有 ended；0.5.0a1 把后四组的时窗记为未完成），
再用本脚本和 0.5.0a2 的源码打开同一个目录：
  1. 出同一时段的周报，看固定时窗和配对现在怎样计数；
  2. 在开球后 3 秒让 league-watch 继续跑两轮（照常刷新目录），看旧目录上能不能接着采。
旧版本写下的时窗记录（big5_slots）不会被改写，一并列出。

用法（两步，目录相同）：
  python -I <上一份证据目录>/probe_big5_missing_flags.py --src <0.5.0a1>/src --tests <0.5.0a1>/tests <空的临时目录>
  python -I continue_a1_capture_with_a2.py --src <0.5.0a2>/src --tests <0.5.0a2>/tests <同一个临时目录>

不联网，不读写仓库，不触碰任何真实数据目录。
"""

import argparse
import json
import sqlite3
import sys
from contextlib import ExitStack, closing
from datetime import timedelta
from pathlib import Path
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

CUTOFF = provider.moment(fixture.DAY + "T13:00:00Z")  # 开球前 60 分钟
KICKOFF = provider.moment(fixture.DAY + "T14:00:00Z")
NAMES = ["对照：live=false 且 ended=false", "两个标记都没有", "两个标记都是 null", "只有 live=false", "只有 ended=false"]

result = {"opened_with": polymarket861.__version__, "arms": {}}
for index, arm in enumerate(sorted(Path(args.scratch).glob("arm*"))):
    row = {}
    root = big5.research_root(arm)
    with closing(sqlite3.connect(root / "runtime.sqlite3")) as db:
        row["books_before"] = db.execute("SELECT COUNT(*) FROM ma_books").fetchone()[0]
    with patch.object(big5, "now_utc", return_value=iso(CUTOFF + timedelta(minutes=2))):
        report = big5.weekly_report(arm, iso(CUTOFF - timedelta(minutes=1)), iso(CUTOFF + timedelta(minutes=1)))
    counts = report["by_league"]["sea"]
    row["weekly_report"] = {k: counts[k] for k in sorted(counts) if k.startswith(("fixed_windows_", "t1_pairs_"))}
    stamp = iso(KICKOFF + timedelta(seconds=3))
    with ExitStack() as stack:
        for module in (store, big5, archive, fixture):
            stack.enter_context(patch.object(module, "now_utc", return_value=stamp))
        stack.enter_context(patch.object(provider, "get", side_effect=fixture.gamma))
        stack.enter_context(patch.object(big5.time, "sleep", lambda seconds: None))
        outcome = big5.watch(arm, ["sea"], fixture.START, fixture.END, cycles=2, interval=1, discover=True)
    row["watch_continued"] = outcome["states"]
    with closing(sqlite3.connect(root / "runtime.sqlite3")) as db:
        row["slot_records"] = [
            {"slot": slot.split("@")[0], "success": bool(ok), "state": state}
            for slot, ok, state in db.execute(
                "SELECT slot,success,json_extract(report,'$.state') FROM big5_slots ORDER BY slot"
            )
        ]
        row["books_after"] = db.execute("SELECT COUNT(*) FROM ma_books").fetchone()[0]
    result["arms"][NAMES[index] if index < len(NAMES) else arm.name] = row

json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
print()
