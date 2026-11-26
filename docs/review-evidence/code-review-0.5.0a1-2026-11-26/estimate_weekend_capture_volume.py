#!/usr/bin/env python3
"""四国值守采集一个周末大约占多少磁盘（T2-4，推算，不是实测）。

节奏取自 big5_market._slot：开球前 3 小时以上每 900 秒一次，3 小时以内每 60 秒一次，
开球前 120 秒到开球后 600 秒每 12 秒一次；T−24 小时和 T−60 分钟各有决策、执行两个固定时窗。
主要合约每次都采（6 份合约、12 枚代币）；全部盘口线只在固定时窗和收盘段的 6 个点采。
目录在值守进程里每 300 秒刷新一次。

单位成本取自开发方 11 月 26 日的真实采样（reports/five-leagues-0.5/market-observation.json）：
数据库字节数除以代币观测数，原始回复的落盘字节数除以回复份数。前者含一次性的目录和条款，
所以是偏高的上限；后者是全量采样和目录页的平均，日常的小回复会更小，目录页会更大。

随仓报告只有每场比赛的日期，没有开球时刻。周五和周一的场次按 19:45 UTC 算；
周六和周日按 12:00、16:00、20:00 UTC 三种情形各算一遍，给出范围。

用法：
  python -I estimate_weekend_capture_volume.py <仓库>/reports/five-leagues-0.5/market-observation.json \
      [--start 2026-11-26T19:25:00Z]
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

parser = argparse.ArgumentParser()
parser.add_argument("observation")
parser.add_argument("--start", default="2026-11-26T19:25:00Z")
args = parser.parse_args()

report = json.load(open(args.observation, encoding="utf-8"))
cost = report["combined_capture_cost"]
fixtures = [slug for league in report["leagues"].values() for slug in league["fixtures"]]
sample_observations = sum(
    family["expected_token_observations"]
    for league in cost["observations_by_family"].values()
    for family in league.values()
)
ALL_LINE_TOKENS = report["all_leagues"]["requested_tokens"] / report["all_leagues"]["fixtures"]
CORE_TOKENS = 12
DB_BYTES_PER_OBSERVATION = cost["database_bytes_at_report"] / sample_observations
RAW_BYTES_PER_BODY = cost["raw_unique_on_disk_bytes"] / cost["raw_unique_bodies"]
CATALOG_BODIES = sum(1 for r in cost["http_requests"] if r["operation"] == "league_sports") + sum(
    r["n"] for r in cost["http_requests"] if r["operation"] == "league_catalog"
)


def moment(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


start = moment(args.start)


def estimate(weekend_hour):
    periodic = hourly = closing = fixed = 0
    last = start
    for slug in fixtures:
        day = datetime.fromisoformat(slug[-10:]).replace(tzinfo=timezone.utc)
        kick = day + (timedelta(hours=19, minutes=45) if day.weekday() in (0, 4) else timedelta(hours=weekend_hour))
        last = max(last, kick)
        periodic += max(0, int((kick - timedelta(hours=3) - start).total_seconds() // 900))
        hourly += (10800 - 120) // 60
        closing += (120 + 600) // 12
        fixed += 4 if kick - timedelta(hours=24) > start else 2
    captures = periodic + hourly + closing + fixed
    all_line_captures = fixed + 6 * len(fixtures)
    observations = (captures - all_line_captures) * CORE_TOKENS + all_line_captures * ALL_LINE_TOKENS
    refreshes = int((last + timedelta(minutes=10) - start).total_seconds() // 300)
    bodies = captures * 2 + refreshes * CATALOG_BODIES
    database = observations * DB_BYTES_PER_OBSERVATION
    raw = bodies * RAW_BYTES_PER_BODY
    # 抽样没有分开记录目录页和单场回复各占多少，所以再给出两个极端：
    # 目录页小到可以忽略（原始回复全是单场的赛事和订单簿），或者抽样的落盘字节全是目录页。
    raw_low = captures * 2 * cost["raw_unique_on_disk_bytes"] / (cost["raw_unique_bodies"] - CATALOG_BODIES)
    raw_high = refreshes * cost["raw_unique_on_disk_bytes"]
    return {
        "saturday_sunday_kickoff_utc": f"{weekend_hour}:00",
        "captures": captures,
        "captures_per_fixture": round(captures / len(fixtures)),
        "token_observations": round(observations),
        "catalog_refreshes": refreshes,
        "raw_bodies": bodies,
        "database_gb_upper_bound": round(database / 1e9, 2),
        "raw_originals_gb_at_sample_average": round(raw / 1e9, 2),
        "raw_originals_gb_extremes": [round(raw_low / 1e9, 2), round(raw_high / 1e9, 2)],
        "total_gb": round((database + raw) / 1e9, 2),
        "total_gb_worst_case": round((database + raw_high) / 1e9, 2),
    }


json.dump(
    {
        "basis": "planning estimate from the tick schedule and the developer's one real sample; not a measurement",
        "start": args.start,
        "fixtures": len(fixtures),
        "fixtures_by_date": {d: sum(1 for s in fixtures if s.endswith(d)) for d in sorted({s[-10:] for s in fixtures})},
        "sample": {
            "token_observations": sample_observations,
            "database_bytes": cost["database_bytes_at_report"],
            "database_bytes_per_observation": round(DB_BYTES_PER_OBSERVATION),
            "raw_bodies": cost["raw_unique_bodies"],
            "raw_on_disk_bytes": cost["raw_unique_on_disk_bytes"],
            "raw_on_disk_bytes_per_body": round(RAW_BYTES_PER_BODY),
            "bodies_per_catalog_refresh": CATALOG_BODIES,
            "tokens_per_all_line_capture": ALL_LINE_TOKENS,
            "slowest_book_request_ms": max(
                family["latency_ms"]["max"] for league in cost["observations_by_family"].values() for family in league.values()
            ),
        },
        "scenarios": [estimate(hour) for hour in (12, 16, 20)],
    },
    sys.stdout,
    ensure_ascii=False,
    indent=1,
)
print()
