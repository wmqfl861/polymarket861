#!/usr/bin/env python3
"""四国值守循环里，后台的目录刷新和采样任务同时写库时会不会让整个循环退出。

值守循环（big5_market.watch）第一次目录是同步取的，之后每 300 秒在线程池里刷新一次，
和各场比赛的采样任务并行。此前的探针和仓库测试都没有让这两类任务真正重叠过：
要么只跑一轮（目录是同步的），要么跑不到第二次刷新。

这个探针用一只模拟时钟驱动真正的 watch 循环，每走一步前进 60 秒：开球前 3 小时内每场比赛
每分钟采样一次，每 5 步有一次后台目录刷新，于是每次刷新都和采样任务同时进行。
模拟时钟只在这一步提交的任务全部结束之后才前进，所以任务之间的先后是真实线程的先后，
任务看到的时刻则是确定的。
上游是自己生成的合成赛事（--fixtures 场意甲比赛，结构照 tests/test_big5_market.py 的 native），
--latency real 时给每个上游请求加一段随机的真实延迟（Gamma 0.05 到 0.3 秒，订单簿 0.05 到 0.4 秒），
让两类任务的写库时刻像真实情况那样错开。重复 --runs 次，每次用一个新的数据目录，
记录循环有没有抛出异常、抛出之前完成了几次后台刷新。

--src 可以指向一份改过的源码副本，用来验证修改：修好之后每次都应当走完全部 --minutes 步，
runs_in_which_watch_raised 为 0。

--restart 模拟服务被自动重启：循环抛出异常后，模拟时钟前进 5 秒（服务文件的 RestartSec），
在同一个数据目录上重新进入 watch，把剩下的步数走完，记录重启了几次、最后一共完成多少次采样。
它回答的是“退出之后在原目录上重启，采集能不能接着做”。重启是在同一个进程里重新调用 watch，
不是真的换一个进程；进程一侧见 probe_watch_service_heartbeat.py 的第六遍。

用法：
  python -I probe_big5_refresh_overlap.py --src <仓库>/src <空的临时目录> \
      [--fixtures 10] [--minutes 150] [--runs 10] [--latency none|real] [--seed 1] [--restart]

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
parser.add_argument("--minutes", type=int, default=150)
parser.add_argument("--runs", type=int, default=10)
parser.add_argument("--latency", choices=("none", "real"), default="none")
parser.add_argument("--seed", type=int, default=1)
parser.add_argument("--restart", action="store_true")
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
KICKOFF = provider.moment(DAY + "T14:00:00Z")
SERIES, TAG = big5.MARKETS["sea"]
RNG = random.Random(args.seed)


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
                "gameStartTime": DAY + "T14:00:00Z",
            }
        )
    return {
        "id": str(5000 + 2 * k + (1 if aux else 0)),
        "slug": base + ("-more-markets" if aux else ""),
        "startTime": DAY + "T14:00:00Z",
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


def run(index):
    data = Path(args.scratch) / f"run{index}"
    sim = SimpleNamespace(
        now=KICKOFF - timedelta(minutes=args.minutes + 10), busy=0, gate=threading.Condition(), refreshes=0, jobs=0, steps=0
    )

    def upstream(url, params=None, **kwargs):
        params = params or {}
        books = url.endswith("/books")
        if args.latency == "real":
            time.sleep(RNG.uniform(0.05, 0.4) if books else RNG.uniform(0.05, 0.3))
        now = iso(sim.now)
        if url.endswith("/sports"):
            value = [{"sport": "sea", "series": SERIES, "tags": TAG + ",100618", "primaryTagId": 100618}]
        elif url.endswith("/events"):
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
            json.dumps(value).encode(),
            now,
            now,
            {"date": format_datetime(provider.moment(now))},
            url + ("?" + urlencode(params) if params and not books else ""),
        )

    def counted(real, counter):
        def call(*a, **k):
            result = real(*a, **k)
            with sim.gate:
                setattr(sim, counter, getattr(sim, counter) + 1)
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
        sim.steps += 1

    outcome = {"run": index, "raised": None}
    restarts, states = 0, Counter()
    began = time.monotonic()
    with ExitStack() as stack:
        for module in (store, big5, archive):
            stack.enter_context(patch.object(module, "now_utc", side_effect=lambda: iso(sim.now)))
        stack.enter_context(patch.object(provider, "get", side_effect=upstream))
        stack.enter_context(patch.object(big5, "time", SimpleNamespace(sleep=advance)))
        stack.enter_context(patch.object(big5, "ThreadPoolExecutor", Pool))
        stack.enter_context(patch.object(big5, "catalog", counted(big5.catalog, "refreshes")))
        stack.enter_context(patch.object(big5, "_job", counted(big5._job, "jobs")))
        while True:
            try:
                result = big5.watch(
                    data, ["sea"], START, END, cycles=args.minutes - sim.steps, interval=60, discover=True
                )
                states.update(result["states"])
                break
            except Exception as exc:  # noqa: BLE001 - the point is to see what escapes the loop
                frames = traceback.extract_tb(exc.__traceback__)
                inside = [f for f in frames if "polymarket861" in f.filename]
                outcome["raised"] = outcome["raised"] or {
                    "type": type(exc).__module__ + "." + type(exc).__name__,
                    "message": str(exc),
                    "raised_in": [f"{Path(f.filename).name}:{f.name}" for f in inside][-4:],
                    "minutes_before_kickoff": round((KICKOFF - sim.now).total_seconds() / 60),
                }
                if not args.restart or "database is locked" not in str(exc) or sim.steps >= args.minutes:
                    break
                restarts += 1
                sim.now += timedelta(seconds=5)
        if not outcome["raised"]:
            outcome["job_states"] = dict(states)
    if args.restart:
        with closing(sqlite3.connect(big5.research_root(data) / "runtime.sqlite3")) as db:
            slots = dict(db.execute("SELECT CASE WHEN success THEN 'complete' ELSE 'not_complete' END, COUNT(*) FROM big5_slots GROUP BY 1"))
        outcome.update(restarts=restarts, sampling_slots=slots)
    outcome.update(
        catalogue_refreshes_finished=sim.refreshes,
        sampling_jobs_finished=sim.jobs,
        wall_seconds=round(time.monotonic() - began, 1),
    )
    return outcome


runs = [run(i) for i in range(args.runs)]
raised = [r for r in runs if r["raised"]]
# 每一轮第一次目录是同步的，每次重启之后的第一次也是；其余成功的目录调用都是后台刷新。
# 让循环退出的后台刷新不计入 catalogue_refreshes_finished，另外加上。
failed = sum(r.get("restarts") or (1 if r["raised"] else 0) for r in runs)
background = sum(r["catalogue_refreshes_finished"] - 1 - r.get("restarts", 0) for r in runs) + failed
json.dump(
    {
        "package_version": polymarket861.__version__,
        "python": sys.version.split()[0],
        "fixtures": args.fixtures,
        "simulated_minutes_per_run": args.minutes,
        "upstream_latency": args.latency,
        "runs": args.runs,
        "runs_in_which_watch_raised": len(raised),
        "background_refreshes_attempted": background,
        "share_of_background_refreshes_that_ended_the_loop": round(failed / background, 4) if background else None,
        **(
            {
                "restarted_after_each_exit": True,
                "restarts": failed,
                "sampling_slots": dict(sum((Counter(r["sampling_slots"]) for r in runs), Counter())),
            }
            if args.restart
            else {}
        ),
        "errors": sorted({r["raised"]["type"] + ": " + r["raised"]["message"] for r in raised}),
        "raised_in": sorted({" > ".join(r["raised"]["raised_in"]) for r in raised}),
        "per_run": runs,
    },
    sys.stdout,
    ensure_ascii=False,
    indent=1,
)
print()
