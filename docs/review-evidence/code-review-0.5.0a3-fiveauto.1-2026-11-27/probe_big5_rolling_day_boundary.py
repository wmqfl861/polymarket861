#!/usr/bin/env python3
"""四国值守循环改成“滚动窗口”（--rolling）之后，跨过 UTC 零点时目录是怎么刷新的。

0.5.0a3+fiveauto.1 的值守服务不再写死起止日期，而是每一轮按当前时刻算窗口：
UTC 当天零点往前 14 天、往后 8 天。窗口一变（也就是每天 UTC 零点，北京时间 8 点），
循环把已有的目录结果作废，下一次目录是在主循环里同步取的，取完之前不派发新的采样任务；
平时每 300 秒一次的刷新则在线程池里做。仓库的测试（test_rolling_discovery_advances_after_utc_midnight）
把 _watch_catalog 换成了假函数，没有让真实的目录代码跑过日界。

这个探针用模拟时钟驱动真正的 watch(rolling=True)：从 UTC 23:40 开始走 --minutes 轮
（每轮前进 60 秒，有目录刷新在途或快到下一次刷新时循环自己会缩短这一步），跨过一次零点。上游是合成的意甲赛事（结构同 probe_big5_refresh_overlap.py），
其中 --fixtures 场在窗口内。记录每一次目录调用是在主线程（同步）还是线程池里做的、
用的窗口、请求了几页 /events，以及零点前后采样任务有没有中断。

用法：
  python -I probe_big5_rolling_day_boundary.py --src <仓库>/src <空的临时目录> \
      [--fixtures 10] [--minutes 60]

不联网，不读写仓库，不触碰任何真实数据目录。
"""

import argparse
import json
import random
import sqlite3
import sys
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("scratch")
parser.add_argument("--fixtures", type=int, default=10)
parser.add_argument("--minutes", type=int, default=60)
args = parser.parse_args()
sys.path.insert(0, args.src)

import polymarket861  # noqa: E402
from polymarket861 import (  # noqa: E402
    big5_market as big5,
    market_archive as archive,
    market_provider as provider,
    store,
)
from polymarket861.store import iso  # noqa: E402

DAY = "2030-01-12"
START, END = DAY + "T00:00:00Z", "2030-01-13T00:00:00Z"
KICKOFF = provider.moment(DAY + "T01:30:00Z")
BEGIN = provider.moment("2030-01-11T23:40:00Z")
SERIES, TAG = big5.MARKETS["sea"]


def event(k, aux):
    home, away = f"h{k:02d}", f"a{k:02d}"
    base = f"sea-{home}-{away}-{DAY}"
    names = {"home": f"Home{k:02d} FC", "away": f"Away{k:02d} FC"}
    teams = [
        {"name": names[side], "ordering": side, "abbreviation": abbreviation, "id": 100 + 2 * k + n, "league": "sea"}
        for n, (side, abbreviation) in enumerate((("home", home), ("away", away)))
    ]
    if aux:
        lines = (
            (4, "total-2pt5", "totals", "O/U 2.5", 2.5),
            (5, "team-total-home-1pt5", "soccer_team_totals", names["home"] + " O/U 1.5", 1.5),
            (6, "team-total-away-1pt5", "soccer_team_totals", names["away"] + " O/U 1.5", 1.5),
            (7, "total-3pt5", "totals", "O/U 3.5", 3.5),
            (8, "total-3", "totals", "O/U 3", 3),
        )
    else:
        lines = (
            (1, home, "moneyline", names["home"], None),
            (2, "draw", "moneyline", "Draw", None),
            (3, away, "moneyline", names["away"], None),
        )
    markets = []
    for i, suffix, kind, title, line in lines:
        condition = 1000 * (k + 1) + i
        markets.append(
            {
                "slug": base + "-" + suffix,
                "sportsMarketType": kind,
                "groupItemTitle": title,
                "conditionId": "0x" + format(condition, "064x"),
                "clobTokenIds": [str(condition * 10), str(condition * 10 + 1)],
                "version": "v1",
                "outcomes": ["Yes", "No"] if not aux else ["Over", "Under"],
                "line": line,
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "enableOrderBook": True,
                "description": "unreviewed fixture rules",
                "feesEnabled": True,
                "feeSchedule": {"rate": 0.03},
                "secondsDelay": 1,
                "gameStartTime": DAY + "T01:30:00Z",
            }
        )
    return {
        "id": str(5000 + 2 * k + (1 if aux else 0)),
        "slug": base + ("-more-markets" if aux else ""),
        "startTime": DAY + "T01:30:00Z",
        "title": f"{names['home']} vs. {names['away']}",
        "sport": {"sport": "sea", "series": SERIES},
        "series": [{"id": SERIES}],
        "tags": [{"id": TAG}],
        "teams": teams,
        "markets": markets,
        "live": False,
        "ended": False,
    }


EVENTS = [event(k, aux) for k in range(args.fixtures) for aux in (False, True)]
BY_SLUG = {e["slug"]: e for e in EVENTS}


def run():
    data = Path(args.scratch) / "run"
    sim = SimpleNamespace(now=BEGIN, busy=0, gate=threading.Condition(), steps=0, pages=0)
    calls = []

    def upstream(url, params=None, **kwargs):
        params = params or {}
        books = url.endswith("/books")
        now = iso(sim.now)
        if url.endswith("/sports"):
            value = [{"sport": "sea", "series": SERIES, "tags": TAG + ",100618", "primaryTagId": 100618}]
        elif url.endswith("/events"):
            with sim.gate:
                sim.pages += 1
            value = EVENTS[params["offset"] : params["offset"] + params["limit"]]
        elif "/events/slug/" in url:
            value = BY_SLUG[url.rsplit("/", 1)[1]]
        elif books:
            value = [
                {
                    "asset_id": token,
                    "market": "0x" + format(int(token) // 10, "064x"),
                    "bids": [{"price": "0.3", "size": "10"}],
                    "asks": [{"price": "0.4", "size": "50"}, {"price": "0.6", "size": "50"}],
                    "timestamp": str(int(provider.moment(now).timestamp() * 1000)),
                }
                for token in params["token_ids"]
            ]
        else:
            raise AssertionError(url)
        return provider.Reply(
            json.dumps(value).encode(), now, now, {"date": format_datetime(provider.moment(now))},
            url + ("?" + urlencode(params) if params and not books else ""),
        )

    real_catalog = big5._watch_catalog

    def catalog(data_dir, leagues, start, end, **kwargs):
        before = sim.pages
        entry = {
            "at": iso(sim.now), "thread": "main loop (synchronous)" if threading.current_thread() is threading.main_thread() else "worker pool",
            "window": [start, end],
        }
        result = real_catalog(data_dir, leagues, start, end, **kwargs)
        entry.update(
            events_pages_requested=sim.pages - before,
            page_walk_complete=result["leagues"]["sea"].get("page_walk_complete"),
            fixtures=len(result["leagues"]["sea"].get("fixtures", [])),
            result_window=result.get("window"),
        )
        with sim.gate:
            calls.append(entry)
        return result

    jobs = []
    real_job = big5._job

    def job(data_dir, event, work, workers):
        result = real_job(data_dir, event, work, workers)
        with sim.gate:
            jobs.append((iso(sim.now), result["state"]))
        return result

    def finished(_):
        with sim.gate:
            sim.busy -= 1
            sim.gate.notify_all()

    class Pool(ThreadPoolExecutor):
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
        sim.steps += 1

    with ExitStack() as stack:
        for module in (store, big5, archive):
            stack.enter_context(patch.object(module, "now_utc", side_effect=lambda: iso(sim.now)))
        stack.enter_context(patch.object(provider, "get", side_effect=upstream))
        stack.enter_context(patch.object(big5, "time", SimpleNamespace(sleep=advance)))
        stack.enter_context(patch.object(big5, "ThreadPoolExecutor", Pool))
        stack.enter_context(patch.object(big5, "_watch_catalog", side_effect=catalog))
        stack.enter_context(patch.object(big5, "_job", side_effect=job))
        raised = None
        try:
            result = big5.watch(data, ["sea"], None, None, cycles=args.minutes, interval=60, discover=True, rolling=True)
        except Exception as exc:  # noqa: BLE001
            raised = f"{type(exc).__name__}: {exc}"
            result = {"states": {}}
    midnight = iso(provider.moment(DAY + "T00:00:00Z"))
    per_minute = Counter(at[:16] for at, state in jobs if state == "complete")
    return {
        "raised": raised,
        "steps": sim.steps,
        "job_states": dict(result["states"]),
        "catalogue_calls": calls,
        "synchronous_catalogue_calls": sum(c["thread"].startswith("main") for c in calls),
        "pooled_catalogue_calls": sum(c["thread"].startswith("worker") for c in calls),
        "distinct_windows": sorted({tuple(c["window"]) for c in calls}),
        "complete_samples_in_the_five_minutes_before_midnight": sum(n for minute, n in per_minute.items() if "2030-01-11T23:55" <= minute < "2030-01-12T00:00"),
        "complete_samples_in_the_five_minutes_from_midnight": sum(n for minute, n in per_minute.items() if "2030-01-12T00:00" <= minute < "2030-01-12T00:05"),
        "simulated_end": iso(sim.now),
        "minutes_without_a_complete_sample": [
            m for m in sorted({(BEGIN + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M")
                               for i in range(int((sim.now - BEGIN).total_seconds() // 60))})
            if not per_minute.get(m)
        ],
        "midnight": midnight,
    }


print(json.dumps(
    {
        "package_version": polymarket861.__version__,
        "python": sys.version.split()[0],
        "fixtures": args.fixtures,
        "simulated_start": iso(BEGIN),
        "kickoff_of_every_fixture": iso(KICKOFF),
        **run(),
    },
    indent=1, ensure_ascii=False,
))
