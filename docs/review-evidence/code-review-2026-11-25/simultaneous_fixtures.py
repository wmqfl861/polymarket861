"""Measure five independently identified EPL test fixtures sharing SQLite and one paper account.

Run with optional --fixtures / --history. This reuses decision_window_benchmark.py's native
pre-match synthetic books, lineup manifests, and 21 lines per game. No network, live funds, or
market data enter this test. The measured interval starts after setup and archive composition.
"""

import argparse
import io
import json
import runpy
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Lock
from unittest.mock import patch


def copy_receipts(source, target):
    # Each fixture was captured independently using the same normal entry points. Retain
    # every native blob/hash/receipt time and input seal; collector row IDs are local keys.
    shutil.copytree(source / "archive", target / "archive", dirs_exist_ok=True)
    with closing(sqlite3.connect(target / "runtime.sqlite3")) as db, db:
        db.execute("ATTACH DATABASE ? AS capture", (str(source / "runtime.sqlite3"),))
        for table in (
            "fixture_versions", "schedule_receipts", "observations", "ma_events",
            "ma_contracts", "ma_books", "forecast_seals", "football_input_manifests",
        ):
            db.execute(f"INSERT OR IGNORE INTO {table} SELECT * FROM capture.{table}")
        cols = "operation,selector,requested_at,received_at,status,sha,detail"
        db.execute(f"INSERT INTO ma_requests({cols}) SELECT {cols} FROM capture.ma_requests")


def make_fixture(index, target, bench):
    case_module = bench["T"]
    store = bench["store"]
    slug = f"epl-h{index}-a{index}-2026-03-01"
    home, away = f"Home {index} FC", f"Away {index} FC"
    fid = case_module.football.fixture_id(case_module.CHAMP, home, away)

    def native(slug):
        event = bench["event"](slug)
        for i, team in enumerate(event["teams"]):
            team["abbreviation"] = ("h" if i == 0 else "a") + str(index)
        event["id"] += index * 1000
        event["gameId"] += index * 1000
        for i, market in enumerate(event["markets"]):
            market["conditionId"] = "0x" + f"{1000 * index + i:064x}"
            market["clobTokenIds"] = json.dumps([f"{index}{i:03}1", f"{index}{i:03}2"])
        return event

    with patch.multiple(case_module, HOME=home, AWAY=away, SLUG=slug, FID=fid, event=native):
        case = case_module.Opportunities()
        case.setUp()
        try:
            seal = case.seal()
            bench["capture_first_books"](case, case_module.CUT - timedelta(seconds=7), seal)
            if index == 1:
                with case_module.at(case_module.CUT - timedelta(seconds=6)):
                    bench["ledger"].open_account(case.root, "main", "formal")
                shutil.copytree(case.root / "archive", target / "archive")
                with closing(store.connect(case.root)) as src, closing(sqlite3.connect(target / "runtime.sqlite3")) as dst:
                    src.backup(dst)
            else:
                copy_receipts(case.root, target)
        finally:
            case.doCleanups()
    return slug


def measure(count, history):
    script = Path(__file__).with_name("decision_window_benchmark.py")
    with patch.object(sys, "argv", [str(script), "21", "0"]), redirect_stdout(io.StringIO()):
        bench = runpy.run_path(str(script))
    case = bench["T"]
    opportunity, store = bench["opportunity"], bench["store"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        slugs = [make_fixture(i, root, bench) for i in range(1, count + 1)]
        bench["preload"](root, history, bench["template"])
        metrics = Counter()
        lock = Lock()
        connect, publish = sqlite3.connect, store.put_report

        def traced_connect(*args, **kw):
            with lock:
                metrics["sqlite_connections"] += 1
            return connect(*args, **kw)

        def traced_publish(db, kind, key, payload):
            if kind == "ledger":
                with lock:
                    metrics["ledger_publications"] += 1
            return publish(db, kind, key, payload)

        start = Barrier(count)
        def tick(slug):
            start.wait(timeout=15)
            before = time.perf_counter()
            out = opportunity.process(root, slug)
            states = Counter(row["state"] for row in out["decisions"])
            if states != {"skipped": 36, "frozen": 6}:
                raise ValueError((slug, states, out["gaps"]))
            return {"fixture": slug, "elapsed_seconds": round(time.perf_counter() - before, 4),
                    "states": states}

        with case.at(case.CUT - timedelta(seconds=5)), patch.object(sqlite3, "connect", traced_connect), patch.object(store, "put_report", traced_publish):
            before = time.perf_counter()
            with ThreadPoolExecutor(max_workers=count) as pool:
                results = list(pool.map(tick, slugs))
            total = time.perf_counter() - before
        return {"fixtures": count, "directions_per_fixture": 42, "existing_intents": history,
                "wall_seconds": round(total, 4), **metrics, "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=int, default=5)
    parser.add_argument("--history", type=int, default=16000)
    args = parser.parse_args()
    print(json.dumps(measure(args.fixtures, args.history)))
