#!/usr/bin/env python3
"""封存预测时能配上盘口的比例，随两个定时器的相位差怎样变化。

部署模板里盘口采集和封存是两个各自每分钟触发一次的 oneshot 服务。封存在窗口内第一次触发时就完成，
只取“封存开始那一刻已经落库”的盘口；盘口采集每次触发要先联网取赛事和订单簿。这个探针用仓库自己的
markets.collect 和 operations.forecast_cycle，在模拟时钟上按给定相位差逐分钟触发，数出配对比例。

  phase    盘口定时器比封存定时器早触发多少秒（0 = 同时触发；文档里的安装命令
           `systemctl --user enable --now …{markets,schedule,forecast,watchdog}.timer` 在用户管理器已经
           运行时会让 OnStartupSec 立即到期，三个服务同时启动，此后各自每 60 秒一次）；
  offset   定时器触发时刻相对整分钟的偏移，逐 5 秒取遍一分钟；
  latency  每次触发是新进程：进程启动 1.0 秒（本机实测 pm861 启动约 1.05 秒）；对每个主机的第一次请求
           按开发方随仓回执取 4.0 秒（其回执为 4.5 / 3.8 / 5.9 秒），同一进程内后续请求 0.2 秒。
           另给一组“快网络”（首次 0.3 秒、后续 0.1 秒）作对照。

交易所一侧用 tests/test_nba_markets.py 的 Venue 夹具，赛程一侧按 BALLDONTLIE 官方 SDK 的字段构造。

用法：
  python -I probe_nba_pairing_by_timer_phase.py --src <src 目录> --tests <tests 目录>

不联网，只写自己建的临时目录。
"""

import argparse
import json
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("--tests", required=True)
args = parser.parse_args()
sys.path[:0] = [str(Path(args.src).resolve()), str(Path(args.tests).resolve())]

import httpx  # noqa: E402

import test_nba_markets as venue_fixture  # noqa: E402
from polymarket861.nba import data, markets, operations as ops  # noqa: E402
from polymarket861.nba.markets_contracts import stamp  # noqa: E402

os.environ["BALLDONTLIE_API_KEY"] = "synthetic-review-probe-key"
EAST = ZoneInfo("America/New_York")
TEAMS = {
    "NYK": {"id": 20, "abbreviation": "NYK", "full_name": "New York Knicks"},
    "MIA": {"id": 16, "abbreviation": "MIA", "full_name": "Miami Heat"},
    "BOS": {"id": 2, "abbreviation": "BOS", "full_name": "Boston Celtics"},
    "CHI": {"id": 5, "abbreviation": "CHI", "full_name": "Chicago Bulls"},
    "DEN": {"id": 8, "abbreviation": "DEN", "full_name": "Denver Nuggets"},
    "LAL": {"id": 14, "abbreviation": "LAL", "full_name": "Los Angeles Lakers"},
}
STARTUP = 1.0
HORIZON_MINUTES = 60


def bdl_rows(tip):
    rng = random.Random(861)
    codes, rows = list(TEAMS), []
    game_day = tip.astimezone(EAST).date()
    for back in range(20, 0, -1):
        day = game_day - timedelta(days=back)
        order = codes[:]
        rng.shuffle(order)
        for k in range(3):
            hs, vs = int(rng.gauss(115, 11)), int(rng.gauss(112, 11))
            hs += hs == vs
            start = datetime(day.year, day.month, day.day, 19, 30, tzinfo=EAST).astimezone(timezone.utc)
            rows.append({"id": 1000 + len(rows), "date": day.isoformat(), "season": 2026, "status": "Final",
                         "period": 4, "time": "Final", "postseason": False, "home_team_score": hs,
                         "visitor_team_score": vs, "home_team": TEAMS[order[2 * k]],
                         "visitor_team": TEAMS[order[2 * k + 1]],
                         "datetime": start.strftime("%Y-%m-%dT%H:%M:%S.000Z")})
    rows.append({"id": 9001, "date": game_day.isoformat(), "season": 2026,
                 "status": tip.strftime("%Y-%m-%dT%H:%M:%SZ"), "period": 0, "time": None, "postseason": False,
                 "home_team_score": 0, "visitor_team_score": 0, "home_team": TEAMS["NYK"],
                 "visitor_team": TEAMS["MIA"], "datetime": tip.strftime("%Y-%m-%dT%H:%M:%S.000Z")})
    return rows


def trial(phase, offset, first, later):
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "nba"
        venue = venue_fixture.Venue()
        tip = venue.tip
        deadline = tip - timedelta(minutes=HORIZON_MINUTES)
        hosts = set()

        def clock():
            return venue.now

        def advance(seconds):
            venue.now += timedelta(seconds=seconds)

        def bdl_fetch(params, key):
            rows = bdl_rows(tip) if params["season_type"] == "regular" else []
            return httpx.Response(200, json={"data": rows, "meta": {"next_cursor": None}})

        def venue_get(url, params, *, timeout):
            host = url.split("/")[2]
            advance(later if host in hosts else first)
            hosts.add(host)
            return venue.get(url, params, timeout=timeout)

        def markets_tick(fired):
            hosts.clear()  # 每次触发是新进程，连接重新建立
            venue.now = fired + timedelta(seconds=STARTUP)
            markets.collect(root, now=clock)

        with patch.object(markets.api, "get", venue_get):
            venue.now = tip - timedelta(minutes=70)
            day = venue.now.astimezone(EAST).date()
            data.sync_bdl(root, day - timedelta(days=21), day + timedelta(days=2),
                          fetch=bdl_fetch, clock=clock, sleep=advance)
            markets_tick(tip - timedelta(minutes=69))  # 开窗前的例行目录发现
            sealed = None
            for k in range(7):
                forecast_fired = deadline - timedelta(seconds=360) + timedelta(seconds=offset + 60 * k)
                if forecast_fired >= deadline:
                    break
                # 盘口行带有落库时刻，封存只读“开始时刻之前已落库”的行，所以先后执行不影响可见性。
                markets_tick(forecast_fired - timedelta(seconds=phase))
                venue.now = forecast_fired + timedelta(seconds=STARTUP)
                result = ops.forecast_cycle(root, clock=clock)
                if result["sealed"]:
                    sealed = forecast_fired  # 之后的触发照常进行，盘口采集会在同一窗口里补上报价
            window, = [w for w in ops.forecast_show(root, "bdl:9001")["windows"] if w["horizon"] == "T-60m"]
            quotes = markets.benchmark(root, "NYK", "MIA", stamp(tip), "T-60m", as_of=stamp(deadline))
            paired = bool(window.get("prediction") and window["prediction"]["market_at_prediction"])
            return {"sealed": window["state"] == "sealed", "paired": paired,
                    "seal_fired_seconds_before_deadline": (deadline - sealed).total_seconds() if sealed else None,
                    "window_quote_exists_by_deadline": "win" in quotes}


out = {"horizon": "T-60m", "process_startup_seconds": STARTUP, "offsets_per_phase": 12, "settings": {}}
for label, first, later in (("first_request_4.0s_later_0.2s", 4.0, 0.2), ("first_request_0.3s_later_0.1s", 0.3, 0.1)):
    table = []
    for phase in (0, 2, 5, 8, 10, 15, 20, 30, 40, 50, 58):
        runs = [trial(phase, offset, first, later) for offset in range(0, 60, 5)]
        table.append({"markets_timer_leads_forecast_timer_by_seconds": phase,
                      "trials": len(runs),
                      "sealed": sum(r["sealed"] for r in runs),
                      "sealed_with_market_at_prediction": sum(r["paired"] for r in runs),
                      "quote_for_same_window_existed_by_deadline": sum(r["window_quote_exists_by_deadline"] for r in runs)})
    out["settings"][label] = table
json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
print()
