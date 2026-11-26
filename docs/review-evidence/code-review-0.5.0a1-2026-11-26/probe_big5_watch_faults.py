#!/usr/bin/env python3
"""四国盘口的值守循环（market league-watch）遇到上游异常时会不会整个退出。

`--cycles 0` 是前台常驻进程，仓库里没有为它配服务单元或自动重启。这个探针沿用
tests/test_big5_market.py 的合成赛事，在 T−60 分钟决策时窗和开球后 3 秒的收盘时窗里，
给上游回复注入几种常见的坏情况，各跑三轮循环，记录 watch() 是正常返回还是抛出异常。

用法：
  python -I probe_big5_watch_faults.py --src <仓库>/src --tests <仓库>/tests <空的临时目录>

不联网，不读写仓库。
"""

import argparse
import json
import sys
import traceback
from contextlib import ExitStack
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

KICK = provider.moment(fixture.DAY + "T14:00:00Z")
MOMENTS = {
    "T−60 分钟决策时窗": KICK - timedelta(hours=1, seconds=10),
    "开球后 3 秒的收盘时窗": KICK + timedelta(seconds=3),
}


def reply(base, value):
    return provider.Reply(json.dumps(value).encode(), base.requested_at, base.received_at, base.headers, base.url)


def fault_none(url, base):
    return base


def fault_books_network(url, base):
    if url.endswith("/books"):
        raise OSError("connection reset by peer")
    return base


def fault_books_object(url, base):
    if url.endswith("/books"):
        return reply(base, {"error": "Too Many Requests"})
    return base


def fault_books_null_side(url, base):
    if url.endswith("/books"):
        rows = json.loads(base.body)
        rows[0]["bids"] = None
        return reply(base, rows)
    return base


def fault_books_string_rows(url, base):
    if url.endswith("/books"):
        return reply(base, ["unexpected"] * len(json.loads(base.body)))
    return base


def fault_books_empty(url, base):
    if url.endswith("/books"):
        return reply(base, [])
    return base


def fault_books_not_json(url, base):
    if url.endswith("/books"):
        return provider.Reply(b"<html>502 Bad Gateway</html>", base.requested_at, base.received_at, base.headers, base.url)
    return base


def fault_event_null(url, base):
    if "/events/slug/" in url:
        return reply(base, None)
    return base


def fault_event_list(url, base):
    if "/events/slug/" in url:
        return reply(base, [])
    return base


def fault_event_network(url, base):
    if "/events/slug/" in url:
        raise OSError("timed out")
    return base


def fault_catalog_network(url, base):
    if url.endswith("/events"):
        raise OSError("temporary failure in name resolution")
    return base


def fault_catalog_object(url, base):
    if url.endswith("/events"):
        return reply(base, {"error": "internal"})
    return base


def fault_catalog_markets_null(url, base):
    if url.endswith("/events"):
        rows = json.loads(base.body)
        for row in rows:
            row["markets"] = None
        return reply(base, rows)
    return base


def fault_sports_network(url, base):
    if url.endswith("/sports"):
        raise OSError("timed out")
    return base


FAULTS = {
    "对照：一切正常": fault_none,
    "订单簿请求断网": fault_books_network,
    "订单簿返回一个对象而不是列表": fault_books_object,
    "订单簿里有一行 bids 为 null": fault_books_null_side,
    "订单簿每行是字符串": fault_books_string_rows,
    "订单簿返回空列表": fault_books_empty,
    "订单簿返回 HTML 错误页": fault_books_not_json,
    "单场赛事接口返回 null": fault_event_null,
    "单场赛事接口返回空列表": fault_event_list,
    "单场赛事接口断网": fault_event_network,
    "目录刷新断网": fault_catalog_network,
    "目录返回一个对象而不是列表": fault_catalog_object,
    "目录里 markets 为 null": fault_catalog_markets_null,
    "联赛清单接口断网": fault_sports_network,
}

results = []
index = 0
for moment_name, moment in MOMENTS.items():
    for fault_name, fault in FAULTS.items():
        for discover in (False, True):
            index += 1
            data = Path(args.scratch) / f"case{index}"
            stamp = iso(moment)
            row = {"moment": moment_name, "fault": fault_name, "catalog_refresh_inside_watch": discover}
            # 先用正常回复建好目录，再在值守循环里注入故障。
            with ExitStack() as stack:
                for module in (store, big5, archive, fixture):
                    stack.enter_context(patch.object(module, "now_utc", return_value=stamp))
                stack.enter_context(patch.object(provider, "get", side_effect=fixture.gamma))
                big5.catalog(data, ["sea"], fixture.START, fixture.END)

            def get(url, params=None, **kwargs):
                return fault(url, fixture.gamma(url, params, **kwargs))

            with ExitStack() as stack:
                for module in (store, big5, archive, fixture):
                    stack.enter_context(patch.object(module, "now_utc", return_value=stamp))
                stack.enter_context(patch.object(provider, "get", side_effect=get))
                stack.enter_context(patch.object(big5.time, "sleep", lambda seconds: None))
                try:
                    outcome = big5.watch(data, ["sea"], fixture.START, fixture.END, cycles=3, interval=1, discover=discover)
                    row.update(watch="returned", states=outcome["states"])
                except BaseException as exc:  # 值守进程会因此退出
                    frames = traceback.extract_tb(exc.__traceback__)
                    inside = [f for f in frames if "polymarket861" in f.filename]
                    last = inside[-1] if inside else frames[-1]
                    row.update(
                        watch="raised",
                        exception=type(exc).__name__,
                        message=str(exc)[:160],
                        raised_at=f"{Path(last.filename).name}:{last.lineno} in {last.name}",
                    )
            results.append(row)

raised = [r for r in results if r["watch"] == "raised"]
json.dump(
    {"cases": len(results), "watch_returned": len(results) - len(raised), "watch_raised": len(raised), "rows": results},
    sys.stdout,
    ensure_ascii=False,
    indent=1,
)
print()
