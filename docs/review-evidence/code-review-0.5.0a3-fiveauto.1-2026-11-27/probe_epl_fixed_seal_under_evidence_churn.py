#!/usr/bin/env python3
"""英超固定窗口的正式封存，在别的线程不断提交“赛程相关证据”时还能不能按时封出来。

0.5.0a3+eplfix.1 把赛程证据的核验挪到了写事务之外：先在读快照里核验，拿到写锁后只比对
“证据版本号”（epl_schedule_epoch.revision，由触发器在赛程相关表有写入时加一）和原件身份。
版本号变了就放掉写锁重新核验，一次封存最多核验三次，三次都被打断时报
“schedule evidence changed repeatedly before the fixed B1 decision”，由预测调度按 2、4、8、16、30 秒
退避后在窗口内重试（runner._finish）。

仓库里唯一覆盖这条路径的是 tests/test_season_operations.py 的比赛日集成测试，它接受
“第一次就封存”和“第一次报上面的错、下一次轮询封存”两种结果，没有量过别的线程写得多密时会怎样。
这个探针沿用那条测试的数据和假上游（test_opportunity.Opportunities），在 T−60m 窗口截止前 3 分钟
开始，按产品登记的下次轮询时间一格一格推进，直到封存、错过窗口或到截止时刻；同时另起一个线程，
每隔 --intervals 里给的秒数用独立连接提交一次 revision+1（触发器做的就是这一句），
模拟另一个采集线程刚写进一条赛程相关的收据。记录：每个轮询点的调度状态和报错、用了几次尝试、
封存时刻离截止还有多久、期间提交了多少次版本号。

间隔是真实秒数；封存流程里的时刻由测试夹具的模拟时钟给出。所以这里量的是
“两次证据提交之间的真实间隔”对“重新核验加拿写锁所需真实时间”的比值，不是真实部署的耗时：
真实库的核验要读几百份目录原件，会比这里慢（见审核稿对应条目的说明）。

--verify-seconds 给每次赛程核验（schedule_consensus.fixed_schedules，调度前的预检和封存前的
重新核验都走它）额外加一段真实等待，模拟真实库上核验更慢的情形
（随仓报告 epl-sqlite-hotfix.json 记的第 12 轮 10 场赛程报告，热缓存 15.2 秒，冷缓存 30.2 秒）。

用法：
  python -I probe_epl_fixed_seal_under_evidence_churn.py --src <仓库>/src --tests <仓库>/tests \
      <空的临时目录> [--intervals none,5,1,0.2,0.05,0.01,0.002] [--runs 3] [--verify-seconds 0]

不联网，不读写仓库，不触碰任何真实数据目录。
"""

import argparse
import io
import json
import sqlite3
import sys
import threading
import time
from contextlib import closing, redirect_stderr
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import OpenerDirector

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("--tests", required=True)
parser.add_argument("scratch")
parser.add_argument("--intervals", default="none,5,1,0.2,0.05,0.01,0.002")
parser.add_argument("--runs", type=int, default=3)
parser.add_argument("--verify-seconds", type=float, default=0.0)
args = parser.parse_args()
sys.path.insert(0, args.tests)
sys.path.insert(0, args.src)

import tempfile  # noqa: E402

tempfile.tempdir = args.scratch  # the test fixture creates its data directory with tempfile

import test_opportunity as football  # noqa: E402

import polymarket861  # noqa: E402
from polymarket861 import market_provider, pipeline, schedule_consensus, season, store  # noqa: E402


def run(interval, index):
    fixture = football.Opportunities()
    fixture.setUp()
    try:
        return one(fixture, interval, index)
    finally:
        fixture.doCleanups()


def one(fixture, interval, index):
    root = fixture.root
    inputs = fixture.prepare_inputs()
    clock = {"when": football.CUT - timedelta(minutes=3)}
    price = Decimal(".50")

    def http(url, params=None, **kwargs):
        when = clock["when"]
        src = url
        if params and url != market_provider.CLOB + "/books":
            src += "?" + urlencode(params)

        def event(slug):
            body = football.event(slug)
            body["live"] = when >= football.KICK
            return body

        def book(token):
            ask = price if token == "711" else Decimal(".98")
            return {
                "asset_id": token,
                "market": football.CONDITION,
                "hash": store.digest([token, store.iso(when)]),
                "tick_size": ".01",
                "min_order_size": "1",
                "timestamp": int(when.timestamp() * 1000),
                "bids": [{"price": str(ask - Decimal(".01")), "size": "40"}],
                "asks": [{"price": str(ask), "size": "40"}],
            }

        if url.endswith("/sports"):
            data = [{"sport": "epl", "primaryTagId": 82}]
        elif "/events/slug/" in url:
            data = event(url.rsplit("/", 1)[-1])
        elif url.endswith("/events"):
            data = [event(football.SLUG), event(football.SLUG + "-more-markets")]
        elif url.endswith("/books"):
            data = [book(token) for token in params["token_ids"]]
        elif url.endswith("/book"):
            data = book(params["token_id"])
        else:
            raise OSError("offline public endpoint " + url)
        return football.reply(data, when, src)

    options = {
        "football_root": Path(inputs["fixture_root"]),
        "evidence": Path(inputs["evidence_path"]),
    }
    stop = threading.Event()
    bumps = {"committed": 0, "busy": 0, "no_table": 0}

    def bumper():
        db = sqlite3.connect(Path(root) / "runtime.sqlite3", timeout=30, isolation_level=None)
        try:
            while not stop.wait(interval):
                try:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("UPDATE epl_schedule_epoch SET revision=revision+1 WHERE id=1")
                    db.execute("COMMIT")
                    bumps["committed"] += 1
                except sqlite3.OperationalError as exc:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    bumps["no_table" if "no such table" in str(exc) else "busy"] += 1
        finally:
            db.close()

    refresh_seconds = []
    real_refresh = schedule_consensus.refresh_fixed

    def timed_refresh(*a, **k):
        began = time.perf_counter()
        try:
            return real_refresh(*a, **k)
        finally:
            refresh_seconds.append(round(time.perf_counter() - began, 4))

    real_checks = schedule_consensus.fixed_schedules

    def slow_checks(*a, **k):
        checks = real_checks(*a, **k)
        # 模拟真实库上更慢的赛程核验（调度前的预检和封存前的重新核验走的都是这个函数）：
        # 核验结果已经取好，这段等待里别的线程提交的证据会让它作废。
        time.sleep(args.verify_seconds)
        return checks

    def dispatch():
        with closing(season._db(root)) as db:
            row = db.execute(
                "SELECT state,error,attempts,available,digest FROM forecast_dispatch WHERE fixture_id=? "
                "AND horizon='T-60m' AND mode='formal' AND current_schedule=1",
                (football.FID,),
            ).fetchone()
            seal = db.execute(
                "SELECT sealed_at FROM forecast_seals WHERE fixture_id=? AND horizon='T-60m' AND mode='formal'",
                (football.FID,),
            ).fetchall()
        return (dict(row) if row else None), [s[0] for s in seal]

    outcome = {"run": index, "ticks": []}
    thread = None
    began = time.monotonic()
    with (
        patch.object(market_provider, "get", side_effect=http),
        patch.object(OpenerDirector, "open", side_effect=OSError("offline football sources")),
        patch.object(schedule_consensus, "refresh_fixed", side_effect=timed_refresh),
        patch.object(schedule_consensus, "fixed_schedules", side_effect=slow_checks),
        redirect_stderr(io.StringIO()),
    ):
        if interval is not None:
            thread = threading.Thread(target=bumper, daemon=True)
            thread.start()
        try:
            for _ in range(40):
                when = clock["when"]
                with football.at(when):
                    report = season.run(root, workers=2, **options)
                row, seals = dispatch()
                outcome["ticks"].append(
                    {
                        "seconds_before_cutoff": round((football.CUT - when).total_seconds(), 3),
                        "dispatch_state": row and row["state"],
                        "attempts_so_far": row and row["attempts"],
                        "error": row and row["error"],
                    }
                )
                if seals or (row and row["state"] in {"done", "missed"}):
                    break
                poll = next(s for s in report["services"] if s["service"] == "forecast")
                following = store.instant(poll["next_at"])
                if row and row["available"]:
                    following = max(following, store.instant(row["available"]))
                if following <= when:
                    following = when + timedelta(seconds=1)
                clock["when"] = following
                if when > football.CUT + timedelta(seconds=40):
                    break
        finally:
            stop.set()
            if thread:
                thread.join(timeout=30)
    row, seals = dispatch()
    outcome.update(
        sealed=bool(seals),
        seals=len(seals),
        final_state=row and row["state"],
        final_error=row and row["error"],
        dispatch_attempts=row and row["attempts"],
        sealed_seconds_before_cutoff=(
            round((football.CUT - store.instant(seals[0])).total_seconds(), 3) if seals else None
        ),
        evidence_version_commits=bumps["committed"],
        bumper_busy_errors=bumps["busy"],
        reverification_calls=len(refresh_seconds),
        reverification_seconds_max=max(refresh_seconds, default=None),
        wall_seconds=round(time.monotonic() - began, 2),
    )
    return outcome


arms = []
for text in args.intervals.split(","):
    interval = None if text == "none" else float(text)
    runs = [run(interval, i) for i in range(args.runs)]
    arms.append(
        {
            "seconds_between_evidence_commits": interval,
            "runs": len(runs),
            "sealed_before_cutoff": sum(r["sealed"] and r["seals"] == 1 for r in runs),
            "missed": sum(r["final_state"] == "missed" for r in runs),
            "polls_needed": [len(r["ticks"]) for r in runs],
            "dispatch_attempts": [r["dispatch_attempts"] for r in runs],
            "per_run": runs,
        }
    )
print(
    json.dumps(
        {
            "package_version": polymarket861.__version__,
            "python": sys.version.split()[0],
            "inner_verifications_per_dispatch_attempt": 3,
            "extra_seconds_per_schedule_verification": args.verify_seconds,
            "window": "T-60m, starts 180 s before the cutoff",
            "arms": arms,
        },
        indent=1,
        ensure_ascii=False,
    )
)
