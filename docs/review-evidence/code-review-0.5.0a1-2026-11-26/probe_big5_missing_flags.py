#!/usr/bin/env python3
"""四国盘口固定时窗在交易所不给出 live/ended 标记时是否仍算“完成”。

这个探针沿用仓库自带测试 tests/test_big5_market.py 里的合成赛事和订单簿，
在 T−60 分钟的“决策”与“执行”两个时窗各跑一次 league-watch 的同一入口，
然后读取周报里的分母与完成数。对照组保留测试自带的 live=false、ended=false；
其余各组只改这两个字段，其他一概不动。

用法：
  python -I probe_big5_missing_flags.py --src <仓库>/src --tests <仓库>/tests <空的临时目录>

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


def both_false(event):
    """测试自带的写法：两个标记都明确为 false。"""


def both_absent(event):
    event.pop("live", None)
    event.pop("ended", None)


def both_null(event):
    event["live"] = None
    event["ended"] = None


def only_live_false(event):
    event.pop("ended", None)


def only_ended_false(event):
    event.pop("live", None)


ARMS = {
    "对照：live=false 且 ended=false": both_false,
    "两个标记都没有": both_absent,
    "两个标记都是 null": both_null,
    "只有 live=false": only_live_false,
    "只有 ended=false": only_ended_false,
}


def run(index, mutate):
    data = Path(args.scratch) / f"arm{index}"
    legs = []
    for offset in (-10, 6):
        stamp = iso(CUTOFF + timedelta(seconds=offset))

        def get(url, params=None, **kwargs):
            base = fixture.gamma(url, params, **kwargs)
            raw = json.loads(base.body)
            if url.endswith("/events"):
                for event in raw:
                    mutate(event)
            elif "/events/slug/" in url:
                mutate(raw)
            return provider.Reply(
                json.dumps(raw).encode(),
                base.requested_at,
                base.received_at,
                base.headers,
                base.url,
            )

        with ExitStack() as stack:
            for module in (store, big5, archive, fixture):
                stack.enter_context(patch.object(module, "now_utc", return_value=stamp))
            stack.enter_context(patch.object(provider, "get", side_effect=get))
            big5.catalog(data, ["sea"], fixture.START, fixture.END)
            legs.append(
                big5.watch(data, ["sea"], fixture.START, fixture.END, discover=False)[
                    "states"
                ]
            )
    with patch.object(big5, "now_utc", return_value=iso(CUTOFF + timedelta(minutes=2))):
        report = big5.weekly_report(
            data,
            iso(CUTOFF - timedelta(minutes=1)),
            iso(CUTOFF + timedelta(minutes=1)),
        )
    counts = report["by_league"]["sea"]
    root = big5.research_root(data)
    with closing(sqlite3.connect(root / "runtime.sqlite3")) as db:
        books = db.execute("SELECT COUNT(*) FROM ma_books").fetchone()[0]
        flags = {}
        for (payload,) in db.execute("SELECT payload FROM ma_books"):
            key = json.dumps(json.loads(payload).get("pregame"))
            flags[key] = flags.get(key, 0) + 1
        slots = [
            {"slot": slot, "success": bool(success), "state": json.loads(text)["state"]}
            for slot, success, text in db.execute(
                "SELECT slot,success,report FROM big5_slots ORDER BY slot"
            )
        ]
    return {
        "watch_states_decision_then_execution": legs,
        "fixed_windows_due": counts["fixed_windows_due"],
        "fixed_windows_complete": counts["fixed_windows_complete"],
        "t1_pairs_due": counts["t1_pairs_due"],
        "t1_pairs_complete": counts["t1_pairs_complete"],
        "archived_books": books,
        "archived_books_by_pregame_value": flags,
        "slots": slots,
    }


result = {name: run(i, mutate) for i, (name, mutate) in enumerate(ARMS.items())}
json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
print()
