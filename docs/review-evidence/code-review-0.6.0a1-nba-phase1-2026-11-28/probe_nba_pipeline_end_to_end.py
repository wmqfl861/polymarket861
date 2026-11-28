#!/usr/bin/env python3
"""把 NBA 第一阶段的三个部件接在一起跑一场比赛：免费赛程同步（data.sync_bdl）、只读盘口采集
（markets.collect）、封存与评分（operations.forecast_cycle），再读报表。

仓库自带的测试分别覆盖这三个部件，接缝处用的是替身：tests/test_nba_operations.py 的主测试类把
data.game_versions、data.schedule_proof、model.predict、markets.benchmark、markets.fixture_conflict
全部换成了假函数；持续集成的部署演练里交易所目录返回的是空列表。这个探针不换任何内部函数，只在
最外层替换两处联网：BALLDONTLIE 的取数函数（sync_bdl 的 fetch 参数）和 market_provider.get。

交易所一侧直接用 tests/test_nba_markets.py 的 Venue 夹具，它的规则文本是开发方从真实合约抄录的。
BALLDONTLIE 一侧的字段按官方 Python SDK（balldontlie-api/python，2025-09-17）的 NBAGame 模型加上
datetime 构造：id、date、season、status、period、time、postseason、两队比分、两队对象。默认不带
neutral_site、season_type、status_state，这三个字段 SDK 模型里没有。

情景：
  market_first        同一个 T−60m 窗口里，盘口采集先完成，2 秒后封存预测；
  forecast_first      同一个窗口里，封存先运行，8 秒后盘口采集完成，下一分钟再跑一次封存；
  kickoff_mismatch    两个来源的开赛时刻相差 10 分钟，其余同 market_first；
  season_type_ignored 假如 BALLDONTLIE 不认 season_type 这个筛选参数，两次请求返回同一批比赛；
  neutral_site_false  假如来源逐场给出 neutral_site=false，其余同 market_first；
  no_datetime         假如来源不给 datetime 字段；
  payout_disagrees    交易所把胜负盘兑付给了比分上的输家，看有没有任何报表或状态指出来。

用法：
  python -I probe_nba_pipeline_end_to_end.py --src <src 目录> --tests <tests 目录>

不联网，不读写仓库，只写自己建的临时目录。
"""

import argparse
import json
import os
import random
import sys
import tempfile
from contextlib import closing
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
from polymarket861.nba import data, markets, operations as ops, storage  # noqa: E402
from polymarket861.nba.markets_contracts import stamp  # noqa: E402
from polymarket861.nba.operations_report import report  # noqa: E402

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
GAME_ID = 9001
FINAL = (118, 109)  # 主队尼克斯 118，客队热火 109，合计 227


def history_rows(tip, extra):
    """赛前 20 天、每天 3 场已结束的常规赛，字段按 SDK 的 NBAGame 模型。"""
    rng = random.Random(861)
    codes = list(TEAMS)
    rows = []
    game_day = tip.astimezone(EAST).date()
    for back in range(20, 0, -1):
        day = game_day - timedelta(days=back)
        order = codes[:]
        rng.shuffle(order)
        for k in range(3):
            home, away = order[2 * k], order[2 * k + 1]
            hs, vs = int(rng.gauss(115, 11)), int(rng.gauss(112, 11))
            if hs == vs:
                hs += 1
            start = datetime(day.year, day.month, day.day, 19, 30, tzinfo=EAST).astimezone(timezone.utc)
            rows.append({
                "id": 1000 + len(rows), "date": day.isoformat(), "season": 2026, "status": "Final",
                "period": 4, "time": "Final", "postseason": False, "home_team_score": hs,
                "visitor_team_score": vs, "home_team": TEAMS[home], "visitor_team": TEAMS[away],
                "datetime": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"), **extra,
            })
    return rows


def fixture_row(tip, bdl_tip, extra, *, final=None, with_datetime=True):
    row = {
        "id": GAME_ID, "date": tip.astimezone(EAST).date().isoformat(), "season": 2026,
        "status": bdl_tip.strftime("%Y-%m-%dT%H:%M:%SZ"), "period": 0, "time": None, "postseason": False,
        "home_team_score": 0, "visitor_team_score": 0, "home_team": TEAMS["NYK"], "visitor_team": TEAMS["MIA"],
        "datetime": bdl_tip.strftime("%Y-%m-%dT%H:%M:%S.000Z"), **extra,
    }
    if final:
        row.update(status="Final", period=4, time="Final", home_team_score=final[0], visitor_team_score=final[1])
    if not with_datetime:
        row.pop("datetime")
    return row


def run(name, *, order="market_first", bdl_offset_minutes=0, honor_season_type=True, extra=None,
        with_datetime=True, wrong_win_payout=False):
    extra = extra or {}
    out = {"scenario": name}
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "nba"
        venue = venue_fixture.Venue()
        tip = venue.tip
        bdl_tip = tip + timedelta(minutes=bdl_offset_minutes)
        state = {"final": None, "payouts": {}}

        def clock():
            return venue.now

        def advance(seconds):
            venue.now += timedelta(seconds=seconds)

        def bdl_fetch(params, key):
            rows = history_rows(tip, extra) + [
                fixture_row(tip, bdl_tip, extra, final=state["final"], with_datetime=with_datetime)]
            if honor_season_type and params["season_type"] != "regular":
                rows = []
            return httpx.Response(200, json={"data": rows, "meta": {"next_cursor": None}})

        def venue_get(url, params, *, timeout):
            if url.endswith("/v2/tokens") and state["payouts"]:
                token = str(params["token_id"])
                received = venue.now
                body = {"data": [{"token_id": token, "condition_id": f"0x{int(token) // 10:064x}",
                                  "resolved": True, "final_price": state["payouts"][token]}]}
                venue.calls.append((url, params))
                return markets.api.Reply(json.dumps(body).encode(), stamp(received), stamp(received),
                                         {"date": venue_fixture.format_datetime(received, usegmt=True), "age": "0"}, url)
            return venue.get(url, params, timeout=timeout)

        def sync():
            day = venue.now.astimezone(EAST).date()
            result = data.sync_bdl(root, day - timedelta(days=21), day + timedelta(days=2),
                                   fetch=bdl_fetch, clock=clock, sleep=advance)
            return {k: result[k] for k in ("state", "complete", "pages", "received_games", "new_versions", "quality")}

        def collect():
            result = markets.collect(root, now=clock)
            return {"state": result["state"], "errors": result["errors"]}

        def forecast():
            result = ops.forecast_cycle(root, clock=clock)
            return {k: result[k] for k in ("state", "schedule_games", "sealed", "fixture_errors")}

        with patch.object(markets.api, "get", venue_get):
            window_open = bdl_tip - timedelta(minutes=63)
            venue.now = min(tip, bdl_tip) - timedelta(minutes=70)
            out["schedule_sync"] = sync()
            listed = [g for g in data.games_as_of(root) if g["game_id"] == f"bdl:{GAME_ID}"]
            out["fixture_as_stored"] = {k: listed[0].get(k) for k in ("stage", "status", "kickoff", "neutral", "version")} if listed else None
            stages = {}
            for g in data.games_as_of(root):
                stages[g["stage"]] = stages.get(g["stage"], 0) + 1
            out["stored_games_by_stage"] = stages

            steps = []
            if order == "market_first":
                # 交易所按自己的开赛时刻开窗；两个来源时刻不同时，各自在各自的窗口里运行。
                venue.now = tip - timedelta(minutes=63)
                steps.append({"at": stamp(venue.now), "markets": collect()})
                venue.now = max(venue.now + timedelta(seconds=2), window_open)
                steps.append({"at": stamp(venue.now), "forecast": forecast()})
            else:
                venue.now = window_open
                steps.append({"at": stamp(venue.now), "forecast": forecast()})
                venue.now += timedelta(seconds=8)
                steps.append({"at": stamp(venue.now), "markets": collect()})
                venue.now += timedelta(seconds=52)
                steps.append({"at": stamp(venue.now), "forecast": forecast()})
            out["window_T-60m"] = steps
            out["market_choices_in_window"] = markets.benchmark(
                root, "NYK", "MIA", stamp(tip), "T-60m", as_of=stamp(tip - timedelta(minutes=60)))
            out["market_choices_in_window"] = {
                k: {"line": v["line"], "probability": v["probability"], "available_at": v["available_at"]}
                for k, v in out["market_choices_in_window"].items()}

            venue.now = tip - timedelta(minutes=2)
            out["close_tick"] = collect()

            # 终场：比分来源给出 118:109，交易所各合约关闭并兑付。
            venue.now = tip + timedelta(hours=3)
            state["final"] = FINAL
            home, away = FINAL
            for market in venue.event["markets"]:
                first, second = json.loads(market["clobTokenIds"])
                if market["sportsMarketType"] == "moneyline":
                    home_wins = home > away
                    if wrong_win_payout:
                        home_wins = not home_wins
                    prices = (0.0, 1.0) if home_wins else (1.0, 0.0)  # outcomes: [Heat, Knicks]
                else:
                    target = {"totals": home + away}.get(market["sportsMarketType"])
                    if target is None:
                        target = home if "nyk" in market["slug"].split("team-total-")[1] else away
                    over = target > market["line"]
                    prices = (0.0, 1.0) if over else (1.0, 0.0)  # outcomes: [Under, Over]
                state["payouts"].update({first: prices[0], second: prices[1]})
                market.update(closed=True, umaResolutionStatus="resolved",
                              outcomePrices=json.dumps([str(p) for p in prices]))
            venue.event["ended"] = True
            out["final_sync"] = sync()
            out["settlement_tick"] = collect()
            out["grading_tick"] = forecast()

            shown = ops.forecast_show(root, f"bdl:{GAME_ID}")
            windows = []
            for w in shown["windows"]:
                item = {k: w[k] for k in ("epoch", "horizon", "deadline", "active", "state", "note", "sealed_at")}
                if w.get("prediction"):
                    f = w["prediction"]["forecast"]
                    item["venue_basis"] = f["venue_basis"]
                    item["training_games"] = f["training_games"]
                    item["p_home_win"] = {k: round(v["p_home_win"], 4) for k, v in f["models"].items()}
                    item["market_at_prediction"] = sorted(w["prediction"]["market_at_prediction"])
                    item["grades"] = [g["grade"]["state"] for g in w["grade_history"]]
                    last = w["grade_history"][-1]["grade"] if w["grade_history"] else {}
                    item["benchmarks_in_last_grade"] = sorted(last.get("benchmarks", {}))
                windows.append(item)
            out["windows"] = windows

            day = tip.astimezone(EAST).date().isoformat()
            rep = report(root, "day", day)
            if rep["state"] == "available":
                h = rep["report"]["horizons"]["T-60m"]
                out["day_report_T-60m"] = {
                    "states": h["states"], "grades": h["grades"],
                    "models_scored": {k: v["games"] for k, v in h["models"].items()},
                    "market_coverage": h["market_coverage"],
                    "paired_games_win": {k: v["games"] for k, v in h["market_comparisons"]["win"].items()},
                }
                out["report_keys"] = sorted(rep["report"])
            else:
                out["day_report_T-60m"] = rep
            status = markets.status(root, now=clock)
            out["markets_status_day"] = status["days"].get(day)
            with closing(storage.open_existing(root)) as db:
                out["native_payouts"] = {
                    row["condition_id"][-2:]: json.loads(row["payouts"])
                    for row in db.execute("SELECT condition_id,payouts FROM nba_market_settled ORDER BY condition_id")}
    return out


results = [
    run("market_first"),
    run("forecast_first", order="forecast_first"),
    run("kickoff_mismatch", bdl_offset_minutes=10),
    run("season_type_ignored", honor_season_type=False),
    run("neutral_site_false", extra={"neutral_site": False}),
    run("no_datetime", with_datetime=False),
    run("payout_disagrees", wrong_win_payout=True),
]
json.dump({"final_score_home_away": FINAL, "scenarios": results}, sys.stdout, ensure_ascii=False, indent=1)
print()
