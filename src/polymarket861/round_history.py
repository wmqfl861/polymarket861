"""Publish and retrieve immutable editions of the official-round probability report."""

from __future__ import annotations

import json
from contextlib import closing

from . import football
from . import store


_SCHEMA = """
CREATE TABLE IF NOT EXISTS round_report_editions (
    round_key TEXT NOT NULL,
    edition INTEGER NOT NULL,
    published_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY(round_key, edition)
)"""


def _key(season, round_id, horizon, mode, version):
    return f"{season}/{round_id}/{horizon}/{mode}/{version}"


def _head(db, key):
    return db.execute(
        "SELECT * FROM round_report_editions WHERE round_key=? ORDER BY edition DESC LIMIT 1",
        (key,),
    ).fetchone()


def _read(row):
    body = json.loads(row["body"])
    if store.digest(body) != row["sha256"]:
        raise ValueError("round report edition failed hash verification")
    return body


def _publication(row):
    return {
        "report_version": row["edition"],
        "report_sha256": row["sha256"],
        "report_published_at": row["published_at"],
        "report_origin": row["origin"],
    }


def _initialize(db, key):
    db.execute(_SCHEMA)
    head = _head(db, key)
    if head:
        _read(head)
        return head
    old = db.execute(
        "SELECT updated_at,payload FROM reports WHERE kind='round' AND key=?", (key,)
    ).fetchone()
    if old:
        # Recover precisely the edition that still exists, using its saved date.
        # No historical revision is inferred from mutable current labels.
        db.execute(
            "INSERT INTO round_report_editions VALUES(?,1,?,'legacy_latest',?,?)",
            (
                key,
                old["updated_at"],
                store.digest(json.loads(old["payload"])),
                old["payload"],
            ),
        )
        return _head(db, key)
    return None


def _diagnostics(records):
    hits = {k: {"correct": 0, "eligible": 0, "ties": 0} for k in football.ITEM_KEYS}
    for row in records:
        p = [row["probabilities"][k] for k in football.PROB_KEYS]
        truth = row["observed"]
        best = max(p[:3])
        picks = [i for i in range(3) if p[i] == best]
        observations = [truth[picks[0]] if len(picks) == 1 else None]
        observations += [
            (v > 0.5) == y if v != 0.5 else None for v, y in zip(p[3:], truth[3:])
        ]
        for key, correct in zip(football.ITEM_KEYS, observations):
            if correct is None:
                hits[key]["ties"] += 1
            else:
                hits[key]["eligible"] += 1
                hits[key]["correct"] += int(correct)
    for item in hits.values():
        item["rate"] = item["correct"] / item["eligible"] if item["eligible"] else None
    return {
        "hits": hits,
        "calibration": {
            "n": len(records),
            "bins": [0, 0.2, 0.4, 0.6, 0.8, 1],
            "outcomes": football._calibration(records, "probabilities"),
        },
        "hit_rule": "Unique highest 1X2 probability or binary probability above 0.5; exact ties reported separately.",
    }


def _grade(fixtures, seals, season, round_id, horizon, mode, version):
    indexed = {r["fixture_id"]: r for r in seals}
    scored = []
    records = []
    for f in fixtures:
        result = {
            "fixture_id": f["fixture_id"],
            "kickoff": f["kickoff"],
            "schedule_version": f["schedule_version"],
            "result_version": f["result_version"],
            "result_source": f["result_source"],
            "result_locator": f["result_locator"],
            "result_received_at": f["received_at"],
        }
        scored.append(result)
        if f["home_goals"] is None:
            result["status"] = "pending"
            continue
        goals = (f["home_goals"], f["away_goals"])
        result["goals"] = list(goals)
        seal = indexed.get(f["fixture_id"])
        if not seal:
            result["status"] = "missing_forecast"
            continue
        probabilities = seal["probabilities"]
        result.update(
            status="scored",
            digest=seal["digest"],
            **football.score_probabilities(probabilities, *goals),
        )
        records.append(
            {"probabilities": probabilities, "observed": football._truth_vector(*goals)}
        )
    done = [r for r in scored if r["status"] == "scored"]
    n = len(done)
    expected = max(10, len(fixtures))
    completed = sum(f["home_goals"] is not None for f in fixtures)
    missing = completed - n
    points = sum(r["score"] for r in done)

    def means(field):
        return (
            {k: sum(r[field][k] for r in done) / n for k in football.ITEM_KEYS}
            if n
            else {}
        )

    return {
        "season": season,
        "round_id": round_id,
        "horizon": horizon,
        "mode": mode,
        "model_version": version,
        "protocol": store.PROTOCOL,
        "expected": expected,
        "scheduled": len(fixtures),
        "completed": completed,
        "count": n,
        "missing": missing,
        "pending": expected - completed,
        "schedule_missing": expected - len(fixtures),
        "score": points / n if n else None,
        "full_round_range": [
            points / expected,
            (points + 100 * (expected - n)) / expected,
        ],
        "per_item": means("item_scores"),
        "mean_brier": means("losses"),
        "mean_log_loss": means("log_losses"),
        "fixtures": scored,
        "status": ("incomplete" if missing else "final")
        if completed == expected
        else "provisional",
        "coverage": n / expected,
        **_diagnostics(records),
    }


def publish_round(
    data_dir, season, round_id, *, version="B1", mode="formal", horizon="T-60m"
):
    """Read the sports labels and immutable seals, grade, append and update the current view atomically."""
    if type(round_id) is not int or not 1 <= round_id <= 38:
        raise ValueError("unknown EPL round")
    key = _key(season, round_id, horizon, mode, version)
    with closing(store.connect(data_dir)) as db, db:
        # Take the write reservation before reading any input. A concurrent
        # review or correction either precedes this snapshot or follows it.
        db.execute("BEGIN IMMEDIATE")
        head = _initialize(db, key)
        fixtures = [
            f
            for f in store.eligible_fixture_rows(db, season=season)
            if f["round_id"] == round_id
        ]
        if not fixtures:
            raise ValueError("unknown EPL round; register its schedule first")
        seals = store.scored_fixture_rows(
            db, fixtures, version, horizon, mode, one_round=True
        )
        report = _grade(fixtures, seals, season, round_id, horizon, mode, version)
        checksum = store.digest(report)
        if head and checksum == head["sha256"]:
            return report | _publication(head)
        edition = head["edition"] + 1 if head else 1
        db.execute(
            "INSERT INTO round_report_editions VALUES(?,?,?,'published',?,?)",
            (key, edition, store.now_utc(), checksum, store.canonical(report)),
        )
        latest = report | _publication(_head(db, key))
        store.put_report(db, "round", key, latest)
        return latest


def _references(report):
    fields = (
        "status",
        "kickoff",
        "schedule_version",
        "result_version",
        "goals",
        "digest",
    )
    return {
        f["fixture_id"]: {k: f[k] for k in fields if k in f}
        for f in report.get("fixtures", [])
        if isinstance(f, dict) and "fixture_id" in f
    }


def round_history(
    data_dir,
    season,
    round_id,
    *,
    version="B1",
    mode="formal",
    horizon="T-60m",
    edition=None,
):
    if type(round_id) is not int or not 1 <= round_id <= 38:
        raise ValueError("unknown EPL round")
    if edition is not None and (type(edition) is not int or edition < 1):
        raise ValueError("edition must be a positive integer")
    key = _key(season, round_id, horizon, mode, version)
    with closing(store.connect(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _initialize(db, key)
        if edition is not None:
            found = db.execute(
                "SELECT * FROM round_report_editions WHERE round_key=? AND edition=?",
                (key, edition),
            ).fetchone()
            if not found:
                raise ValueError("round report edition not found")
            return {"round_key": key, **_publication(found), "report": _read(found)}
        rows = db.execute(
            "SELECT * FROM round_report_editions WHERE round_key=? ORDER BY edition",
            (key,),
        ).fetchall()
    items = []
    previous = {}
    for row in rows:
        report = _read(row)
        current = _references(report)
        changes = [
            {"fixture_id": fid, "before": previous.get(fid), "after": current.get(fid)}
            for fid in sorted(previous.keys() | current.keys())
            if previous.get(fid) != current.get(fid)
        ]
        items.append(
            _publication(row)
            | {
                "status": report.get("status"),
                "scored": report.get("count"),
                "score": report.get("score"),
                "fixture_changes": changes,
            }
        )
        previous = current
    return {"round_key": key, "editions": items, "count": len(items)}


def register_history(forecasts):
    p = forecasts.add_parser(
        "round-history", help="查看历次轮报，带 --edition 导出当时完整的成绩单"
    )
    p.add_argument("season")
    p.add_argument("round_id", type=int)
    p.add_argument("--model-version", default="B1")
    p.add_argument(
        "--mode", choices=["technical", "formal", "replay"], default="formal"
    )
    p.add_argument("--horizon", choices=["T-24h", "T-60m", "early"], default="T-60m")
    p.add_argument("--edition", type=int)
    p.set_defaults(
        func=lambda a: round_history(
            a.data_dir,
            a.season,
            a.round_id,
            version=a.model_version,
            mode=a.mode,
            horizon=a.horizon,
            edition=a.edition,
        )
    )
