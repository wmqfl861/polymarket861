#!/usr/bin/env python3
"""四国固定时窗的完成判定对“目录有多新”的依赖。

0.5.0a2 把固定时窗是否完成改为重读原件判定（big5_market_receipts.witness）。读码可见其中一条：
订单簿请求发出时，绑定合约的那份 Gamma 回执不能早于 10 分钟。值守进程每 5 分钟刷新一次目录，
平时满足。这个探针量的是不满足时的情形，结果是：回执早于 10 分钟的比赛在采样前就被整场剔除
（big5_market._sample），订单簿请求不会发出，时窗记为未完成。0.5.0a1 的采样也是这样。

这个探针沿用 tests/test_big5_market.py 的合成赛事和订单簿：先在时窗前若干分钟取一次目录，
然后在 T−60 分钟的决策窗（提前 10 秒）和执行窗（延后 6 秒）各跑一次 league-watch，再读周报。
两种情形各做几组，各组只有“最后一次成功的目录距时窗多久”不同：
  - 不刷新：league-watch 只用本地目录（discover=False）；
  - Gamma 中断：league-watch 照常尝试刷新目录（discover=True），但 Gamma 的请求全部失败，订单簿接口正常。

用法：
  python -I probe_big5_catalog_age.py --src <仓库>/src --tests <仓库>/tests <空的临时目录>

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
from polymarket861 import (  # noqa: E402
    big5_market as big5,
    market_archive as archive,
    market_provider as provider,
    store,
)
from polymarket861.store import iso  # noqa: E402

CUTOFF = provider.moment(fixture.DAY + "T13:00:00Z")  # 开球前 60 分钟
ARMS = [("不刷新", m) for m in (2, 9.5, 10, 12, 30)] + [("Gamma 中断", m) for m in (4, 9.5, 12)]


def books_only(url, params=None, **kwargs):
    if not url.endswith("/books"):
        raise OSError("simulated Gamma outage")
    return fixture.gamma(url, params, **kwargs)


def at(moment, upstream=fixture.gamma):
    stack = ExitStack()
    stamp = iso(moment)
    for module in (store, big5, archive, fixture):
        stack.enter_context(patch.object(module, "now_utc", return_value=stamp))
    stack.enter_context(patch.object(provider, "get", side_effect=upstream))
    return stack


def run(index, mode, minutes):
    data = Path(args.scratch) / f"arm{index}"
    with at(CUTOFF - timedelta(minutes=minutes)):
        big5.catalog(data, ["sea"], fixture.START, fixture.END)
    outage = mode == "Gamma 中断"
    legs = []
    for offset in (-10, 6):
        with at(CUTOFF + timedelta(seconds=offset), books_only if outage else fixture.gamma):
            legs.append(big5.watch(data, ["sea"], fixture.START, fixture.END, discover=outage)["states"])
    with patch.object(big5, "now_utc", return_value=iso(CUTOFF + timedelta(minutes=2))):
        report = big5.weekly_report(data, iso(CUTOFF - timedelta(minutes=1)), iso(CUTOFF + timedelta(minutes=1)))
    counts = report["by_league"]["sea"]
    root = big5.research_root(data)
    with closing(sqlite3.connect(root / "runtime.sqlite3")) as db:
        books = db.execute("SELECT COUNT(*) FROM ma_books").fetchone()[0]
        slots = []
        for slot, success, text in db.execute("SELECT slot,success,report FROM big5_slots ORDER BY slot"):
            item = json.loads(text)
            slots.append(
                {
                    "slot": slot.split("@")[0],
                    "success": bool(success),
                    "state": item["state"],
                    "received": item.get("received"),
                    "expected": item.get("expected"),
                    "error": item.get("error"),
                }
            )
    return {
        "mode": mode,
        "last_good_catalogue_minutes_before_T-60m": minutes,
        "watch_states_decision_then_execution": legs,
        "slots": slots,
        "archived_books": books,
        **{k: counts[k] for k in sorted(counts) if k.startswith(("fixed_windows_", "t1_pairs_"))},
    }


json.dump([run(i, mode, m) for i, (mode, m) in enumerate(ARMS)], sys.stdout, ensure_ascii=False, indent=2)
print()
