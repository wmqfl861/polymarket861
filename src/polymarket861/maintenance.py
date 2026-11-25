"""Turn changes in completed forecasts and free-source evidence into local research jobs."""

from __future__ import annotations

import json
from contextlib import closing

from . import jobs, store
from .evidence_sources import coverage_gaps


_TASKS = {
    "incident": (
        "复现这条数据错误，追查来源字段和受影响对象。提交根因、最小修复建议、"
        "坏样本与正常样本的回归、需补算的范围，列明已有证据及待查项。"
    ),
    "round": (
        "复核已结束的英超轮次、四项误差和漏发。分别检查模型与人员、场外字段的可得性。"
        "给出可预登记的候选假设、历史对照和未来证伪条件。"
    ),
    "source": (
        "复核这个免费来源的异常、上游变化与独立备用源，给出字段映射、使用许可、"
        "时间凭据及复现实例。在试采数据上验证建议。"
    ),
    "discovery": (
        "检查现有英超免费源是否更新或迁移，按缺口寻找新的免费源。输出源地址、免费条件、"
        "字段和各自可用用途、时效、试采方法及失败时的备用路径。"
    ),
}


def _submit(data_dir, topic, ident, snapshot):
    inputs = {"topic": topic, "subject": ident, "evidence": snapshot}
    key = f"research-{topic}-{store.digest(inputs)}"
    return jobs.enqueue_job(
        data_dir, key, task=_TASKS[topic], kind="codex", inputs=inputs
    )


def _signal(kind, report):
    if kind == "round":
        if (
            report.get("mode") not in ("technical", "formal")
            or report.get("horizon") != "T-60m"
        ):
            return None
        if report.get("expected", 0) != 10 or report.get("completed") != 10:
            return None
        return {
            k: report.get(k)
            for k in (
                "season",
                "round_id",
                "model_version",
                "mode",
                "score",
                "expected",
                "count",
                "missing",
                "per_item",
                "fixtures",
                "protocol",
            )
        }
    if kind == "incident":
        return (
            report
            if report.get("status") in ("open", "quarantined", "investigating")
            else None
        )
    issues = {
        field: info
        for field, info in (report.get("health") or {}).items()
        if isinstance(info, dict)
        and info.get("status")
        in {"error", "late", "invalid", "frozen", "freshness_unknown"}
    }
    conflicts = report.get("data_conflicts") or report.get("quarantined")
    if issues or conflicts:
        return {
            "source": report.get("source_id") or report.get("source"),
            "observed_issues": issues,
            "conflicts": conflicts,
        }
    return None


def tick(data_dir):
    """Multiple local schedulers can repeat a tick without duplicating immutable jobs."""
    created = []
    with closing(store.connect(data_dir)) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS maintenance_cursor("
            "kind TEXT, key TEXT, observed TEXT NOT NULL, PRIMARY KEY(kind,key))"
        )
        # Never hold a reader transaction across a job writer.
        rows = list(
            db.execute(
                "SELECT r.kind,r.key,r.updated_at,r.payload FROM reports r LEFT JOIN "
                "maintenance_cursor c ON c.kind=r.kind AND c.key=r.key WHERE "
                "r.kind IN ('round','source','incident') AND "
                "(c.observed IS NULL OR r.updated_at>c.observed) ORDER BY r.updated_at,r.kind,r.key"
            )
        )
        week = store.instant(store.now_utc()).strftime("%G-W%V")
        done = db.execute(
            "SELECT 1 FROM maintenance_cursor WHERE kind='discovery' AND key=?", (week,)
        ).fetchone()
        if not done:
            # Multiple ticks can see the same unmarked week. Freeze one shared snapshot.
            with db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS maintenance_discovery("
                    "week TEXT PRIMARY KEY, snapshot TEXT NOT NULL)"
                )
            with db:
                db.execute(
                    "INSERT OR IGNORE INTO maintenance_discovery VALUES (?,?)",
                    (week, store.canonical(coverage_gaps(data_dir))),
                )
                snapshot = json.loads(
                    db.execute(
                        "SELECT snapshot FROM maintenance_discovery WHERE week=?",
                        (week,),
                    ).fetchone()[0]
                )
            job = _submit(data_dir, "discovery", week, snapshot)
            if job["created"]:
                created.append(job["key"])
            with db:
                db.execute(
                    "INSERT OR IGNORE INTO maintenance_cursor VALUES ('discovery',?,?)",
                    (week, week),
                )
        for row in rows:
            signal = _signal(row["kind"], json.loads(row["payload"]))
            if signal is not None:
                job = _submit(data_dir, row["kind"], row["key"], signal)
                if job["created"]:
                    created.append(job["key"])
            with db:
                db.execute(
                    "INSERT INTO maintenance_cursor VALUES (?,?,?) "
                    "ON CONFLICT(kind,key) DO UPDATE SET observed=max(observed,excluded.observed)",
                    (row["kind"], row["key"], row["updated_at"]),
                )
    return {"new_jobs": created, "reviewed_reports": len(rows), "discovery_week": week}


def register(subparsers):
    parser = subparsers.add_parser(
        "maintenance", help="根据新轮报、故障和免费源缺口派发本地研究"
    )
    actions = parser.add_subparsers(required=True)
    actions.add_parser(
        "tick", help="检查新增证据，登记去重的本地研究任务"
    ).set_defaults(func=lambda args: tick(args.data_dir))
