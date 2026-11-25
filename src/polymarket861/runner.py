"""Dispatch live EPL forecasts into their real decision windows from a local timer."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from . import jobs, pipeline, store


_WINDOW = timedelta(minutes=5)
_LEASE = timedelta(seconds=30)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_dispatch (
 key TEXT PRIMARY KEY, fixture_id TEXT NOT NULL, kickoff TEXT NOT NULL, horizon TEXT NOT NULL,
 version TEXT NOT NULL, mode TEXT NOT NULL, cutoff TEXT NOT NULL, state TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, lease TEXT, valid_until TEXT, available TEXT,
 current_schedule INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL, digest TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS forecast_dispatch_attempts (
 key TEXT NOT NULL, attempt INTEGER NOT NULL, started TEXT NOT NULL, ended TEXT, state TEXT NOT NULL,
 error TEXT, PRIMARY KEY(key,attempt)
);
CREATE TABLE IF NOT EXISTS forecast_runner_seen (
 fixture_id TEXT NOT NULL, kickoff TEXT NOT NULL, first_seen TEXT NOT NULL, PRIMARY KEY(fixture_id,kickoff)
);
"""


def _open(data_dir):
    db = store.connect(data_dir)
    db.executescript(_SCHEMA)
    return db


def _slot(fixture, horizon, mode, version):
    kickoff = store.iso(fixture["kickoff"])
    cutoff = store.iso(store.instant(kickoff) - pipeline.HORIZONS[horizon])
    identity = [fixture["fixture_id"], kickoff, horizon, version, mode, store.PROTOCOL]
    return {
        "key": store.digest(identity),
        "fixture_id": fixture["fixture_id"],
        "kickoff": kickoff,
        "horizon": horizon,
        "cutoff": cutoff,
        "mode": mode,
        "version": version,
    }


def _existing_seal(db, s):
    seal = db.execute(
        "SELECT data_json,digest FROM forecast_seals WHERE fixture_id=? AND kickoff=? AND horizon=? "
        "AND model_version=? AND mode=? AND protocol=?",
        (
            s["fixture_id"],
            s["kickoff"],
            s["horizon"],
            s["version"],
            s["mode"],
            store.PROTOCOL,
        ),
    ).fetchone()
    if seal and store.digest(json.loads(seal["data_json"])) != seal["digest"]:
        raise ValueError("Forecast seal failed its independent hash check")
    return seal["digest"] if seal else None


def _claim(data_dir, slot):
    with closing(_open(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        now = store.now_utc()
        clock = store.instant(now)
        until = store.instant(slot["cutoff"])
        live = db.execute(
            "SELECT kickoff FROM fixture_versions WHERE fixture_id=? AND known_at<=? ORDER BY known_at DESC LIMIT 1",
            (slot["fixture_id"], now),
        ).fetchone()
        if not live or live[0] != slot["kickoff"]:
            return {"state": "superseded"}
        db.execute(
            "INSERT OR IGNORE INTO forecast_dispatch(key,fixture_id,kickoff,horizon,version,mode,cutoff,state,updated_at) "
            "VALUES(:key,:fixture_id,:kickoff,:horizon,:version,:mode,:cutoff,'ready',:now)",
            slot | {"now": now},
        )
        row = db.execute(
            "SELECT * FROM forecast_dispatch WHERE key=?", (slot["key"],)
        ).fetchone()
        if row["state"] in {"done", "missed"}:
            return {"state": "already_" + row["state"], "digest": row["digest"]}
        digest = _existing_seal(db, slot)
        if digest:
            db.execute(
                "UPDATE forecast_dispatch SET state='done',digest=?,updated_at=?,lease=NULL,valid_until=NULL WHERE key=?",
                (digest, now, slot["key"]),
            )
            db.execute(
                "UPDATE forecast_dispatch_attempts SET state='reconciled',ended=? WHERE key=? AND state='running'",
                (now, slot["key"]),
            )
            return {"state": "reconciled", "digest": digest}
        if row["state"] == "running" and now < row["valid_until"]:
            return {"state": "in_progress"}
        if row["state"] == "running":
            db.execute(
                "UPDATE forecast_dispatch_attempts SET state='expired',ended=? WHERE key=? AND attempt=?",
                (now, slot["key"], row["attempts"]),
            )
        if clock > until:
            reason = "elapsed without a timely seal"
            db.execute(
                "UPDATE forecast_dispatch SET state='missed',lease=NULL,valid_until=NULL,updated_at=?,error=? WHERE key=?",
                (now, reason, slot["key"]),
            )
            return {"state": "missed", "error": reason}
        if clock < until - _WINDOW or (row["available"] and now < row["available"]):
            return {"state": "waiting"}
        lease = uuid.uuid4().hex
        expires = store.iso(min(clock + _LEASE, until + timedelta(microseconds=1)))
        db.execute(
            "UPDATE forecast_dispatch SET state='running',attempts=attempts+1,lease=?,valid_until=?,available=NULL,updated_at=? WHERE key=?",
            (lease, expires, now, slot["key"]),
        )
        db.execute(
            "INSERT INTO forecast_dispatch_attempts(key,attempt,started,state) VALUES(?,?,?,'running')",
            (slot["key"], row["attempts"] + 1, now),
        )
        return {"state": "claimed", "lease": lease, "attempt": row["attempts"] + 1}


def _heartbeat(data_dir, slot, lease, stopped):
    while not stopped.wait(_LEASE.total_seconds() / 3):
        with closing(_open(data_dir)) as db, db:
            now = store.now_utc()
            expires = min(
                store.instant(now) + _LEASE,
                store.instant(slot["cutoff"]) + timedelta(microseconds=1),
            )
            changed = db.execute(
                "UPDATE forecast_dispatch SET valid_until=? WHERE key=? AND lease=? AND valid_until>? AND current_schedule=1",
                (store.iso(expires), slot["key"], lease, now),
            ).rowcount
            if not changed:
                return


def _finish(data_dir, slot, claim, digest, error):
    with closing(_open(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        now = store.now_utc()
        clock = store.instant(now)
        cutoff = store.instant(slot["cutoff"])
        # The forecaster validates schedule, hash and the wall clock in its own sealing transaction.
        # Reconcile a timely seal even when the worker's report arrives just after the deadline.
        valid_seal = _existing_seal(db, slot)
        if valid_seal and digest and valid_seal != digest:
            raise ValueError("Dispatch result differed from the immutable forecast")
        if valid_seal:
            digest, error = valid_seal, None
        elif not error:
            digest, error = (
                None,
                "forecaster returned without an independently stored seal",
            )
        state = "done" if not error else ("missed" if clock > cutoff else "error")
        delay = min(30, 2 ** min(claim["attempt"], 5))
        retry = (
            store.iso(min(clock + timedelta(seconds=delay), cutoff))
            if state == "error"
            else None
        )
        accepted = db.execute(
            "UPDATE forecast_dispatch SET state=?,lease=NULL,valid_until=NULL,updated_at=?,available=?,digest=?,error=? "
            "WHERE key=? AND lease=? AND current_schedule=1 AND (valid_until>? OR ? IS NOT NULL)",
            (
                state,
                now,
                retry,
                digest,
                error,
                slot["key"],
                claim["lease"],
                now,
                valid_seal,
            ),
        ).rowcount
        if not accepted:
            return {"state": "stale_attempt"}
        db.execute(
            "UPDATE forecast_dispatch_attempts SET state=?,ended=?,error=? WHERE key=? AND attempt=?",
            (state, now, error, slot["key"], claim["attempt"]),
        )
        return {
            "state": state,
            "digest": digest,
            "error": error,
            "attempt": claim["attempt"],
        }


def _run_one(data_dir, root, evidence_path, slot):
    try:
        claim = _claim(data_dir, slot)
        if claim["state"] != "claimed":
            return slot | claim
        stopped = threading.Event()
        monitor = threading.Thread(
            target=_heartbeat,
            args=(data_dir, slot, claim["lease"], stopped),
            daemon=True,
        )
        monitor.start()
        try:
            forecast = pipeline.make_forecast(
                data_dir,
                slot["fixture_id"],
                root,
                mode=slot["mode"],
                horizon=slot["horizon"],
                version=slot["version"],
                evidence_path=evidence_path,
            )
            if forecast["kickoff"] != slot["kickoff"]:
                raise ValueError("fixture schedule changed during dispatch")
            digest, error = forecast["digest"], None
        except Exception as exc:
            digest, error = None, f"{type(exc).__name__}: {exc}"[:800]
        finally:
            stopped.set()
            monitor.join()
        return slot | _finish(data_dir, slot, claim, digest, error)
    except Exception as exc:
        return slot | {"state": "error", "error": f"{type(exc).__name__}: {exc}"[:800]}


def runner_status(data_dir):
    with closing(_open(data_dir)) as db:
        records = [
            dict(r)
            for r in db.execute(
                "SELECT key,fixture_id,kickoff,horizon,version,mode,cutoff,state,attempts,"
                "current_schedule,updated_at,digest,error FROM forecast_dispatch ORDER BY updated_at DESC,key"
            )
        ]
        attempts = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM forecast_dispatch_attempts ORDER BY started DESC,key"
            )
        ]
        return {"jobs": records, "attempts": attempts}


def run_due(data_dir, root=None, *, evidence_path=None, versions=(), workers=2):
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be a positive integer")
    observed = store.now_utc()
    clock = store.instant(observed)
    fixtures = store.eligible_fixtures(data_dir)
    with closing(_open(data_dir)) as db, db:
        db.executemany(
            "INSERT OR IGNORE INTO forecast_runner_seen VALUES(?,?,?)",
            [
                (f["fixture_id"], f["kickoff"], observed)
                for f in fixtures
                if f["home_goals"] is None and f["kickoff"] > observed
            ],
        )
        seen = {
            (r[0], r[1])
            for r in db.execute("SELECT fixture_id,kickoff FROM forecast_runner_seen")
        }
        recorded = {
            r["key"]: dict(r)
            for r in db.execute(
                "SELECT key,state,fixture_id,kickoff,horizon,version FROM forecast_dispatch WHERE current_schedule=1"
            )
        }
        scheduled = {r["fixture_id"] for r in recorded.values()}
        superseded = []
        for f in fixtures:
            if f["fixture_id"] not in scheduled:
                continue
            old = [
                r[0]
                for r in db.execute(
                    "SELECT key FROM forecast_dispatch WHERE fixture_id=? AND kickoff<>? AND current_schedule=1",
                    (f["fixture_id"], f["kickoff"]),
                )
            ]
            superseded.extend(old)
            for key in old:
                db.execute(
                    "UPDATE forecast_dispatch SET current_schedule=0,lease=NULL,valid_until=NULL,updated_at=?, "
                    "state=CASE WHEN state='running' OR state='error' THEN 'superseded' ELSE state END WHERE key=?",
                    (observed, key),
                )
                db.execute(
                    "UPDATE forecast_dispatch_attempts SET state='superseded',ended=? WHERE key=? AND state='running'",
                    (observed, key),
                )
    try:
        formal = store.require_formal(data_dir, at=observed)
    except ValueError:
        formal = None
    champions = {}
    candidates = [] if versions else jobs.experiment_status(data_dir)["candidates"]
    past_slots = {}
    for row in recorded.values():
        past_slots.setdefault((row["fixture_id"], row["kickoff"]), []).append(row)
    future, due, unanchored = [], [], []
    previous = Counter()
    for f in fixtures:
        if (
            f["home_goals"] is not None
            and (f["fixture_id"], f["kickoff"]) not in seen
            and f["fixture_id"] not in scheduled
        ):
            continue
        if f["home_goals"] is not None and not versions:
            prior_slots = past_slots.get((f["fixture_id"], f["kickoff"]), [])
            if set(pipeline.HORIZONS) == {s["horizon"] for s in prior_slots} and all(
                s["state"] in {"done", "missed"} for s in prior_slots
            ):
                continue
        if f.get("precision") == "date":
            unanchored.append(f["fixture_id"])
            continue
        for horizon, offset in pipeline.HORIZONS.items():
            cutoff = store.instant(f["kickoff"]) - offset
            opens = cutoff - _WINDOW
            if clock < opens:
                future.append(opens)
                continue
            # Historical misses predating activation are technical observations only.
            mode = "technical"
            if formal is not None:
                try:
                    store.require_formal(data_dir, at=store.iso(min(clock, cutoff)))
                    mode = "formal"
                except ValueError:
                    pass
            at = store.iso(min(clock, cutoff))
            if not versions and at not in champions:
                champions[at] = jobs.active_version(
                    data_dir, "model", "epl", default="B1", at=at
                )
            selected = list(versions) or [champions[at]]
            if not versions and horizon == "T-60m":
                selected.extend(
                    c["version"]
                    for c in candidates
                    if c["kind"] == "model"
                    and c["state"] == "shadow"
                    and c["manifest"].get("recipe_digest")
                    and c["season"][:4] == str(f["season"])[:4]
                    and c["first"] <= f["round_id"] < c["first"] + c["span"]
                    and store.instant(c["registered_at"]) <= cutoff
                )
            for version in dict.fromkeys(selected):
                slot = _slot(f, horizon, mode, version)
                old_state = recorded.get(slot["key"], {}).get("state")
                if old_state in {"done", "missed"}:
                    previous["already_" + old_state] += 1
                else:
                    due.append(slot)
    # Expired slots can be numerous after downtime; jobs that still have a real deadline run first.
    due.sort(
        key=lambda s: (
            store.instant(s["cutoff"]) < clock,
            s["cutoff"],
            s["fixture_id"],
            s["version"],
        )
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(
            pool.map(lambda s: _run_one(data_dir, root, evidence_path, s), due)
        )
    summary = {
        "observed_at": observed,
        "next_window_opens": store.iso(min(future)) if future else None,
        "counts": dict(previous + Counter(r["state"] for r in records)),
        "superseded": superseded,
        "awaiting_exact_kickoff": unanchored,
        "jobs": [
            r for r in records if r["state"] not in {"already_done", "already_missed"}
        ],
    }
    from . import maintenance

    summary["maintenance"] = maintenance.tick(data_dir)
    store.save_report(data_dir, "jobs", "runner", summary)
    store.save_report(data_dir, "jobs", "latest", jobs.jobs_status(data_dir))
    return summary


def _command(args):
    if args.repeat is not None and args.repeat <= 0:
        raise ValueError("repeat must be a positive number of seconds")
    while True:
        started = time.monotonic()
        result = run_due(
            args.data_dir,
            args.root,
            evidence_path=args.evidence,
            versions=args.model_version,
            workers=args.workers,
        )
        if args.repeat is None:
            return result
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
        time.sleep(max(0.0, args.repeat - (time.monotonic() - started)))


def register(subparsers):
    actions = subparsers.add_parser("run", help="本机赛前时点调度").add_subparsers(
        dest="run_action", required=True
    )
    due = actions.add_parser(
        "due",
        help="执行已到窗口的赛前预测",
        description="每分钟由本机计划任务调用一次，或以 --repeat 60 前台循环。",
    )
    due.add_argument(
        "root",
        nargs="?",
        type=Path,
        help="已观察的球队数据目录。省略则随 data observe/refresh 使用最新已验证档案",
    )
    due.add_argument("--evidence", type=Path, help="历史赛果与时间的证据文件")
    due.add_argument(
        "--model-version",
        action="append",
        default=[],
        help="可重复。默认跑当前模型及窗口内已绑定配方的候选",
    )
    due.add_argument("--workers", type=int, default=2)
    due.add_argument("--repeat", type=float, help="前台循环间隔秒数，如 60")
    due.set_defaults(func=_command, handler=_command)
    status = actions.add_parser("status", help="查阅各时点、重试和改期记录")
    status.set_defaults(
        func=lambda a: runner_status(a.data_dir),
        handler=lambda a: runner_status(a.data_dir),
    )
