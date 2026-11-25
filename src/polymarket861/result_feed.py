"""Licensed EPL match results, independent regulation confirmation and research sidecars."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import dbscope
from .football import fixture_id, read_json_rows
from .store import (
    archive_bytes,
    canonical,
    connect,
    eligible_fixtures,
    instant,
    now_utc,
    save_report,
)

BASE = "https://api.openligadb.de"
ATTRIBUTION = "Data from OpenLigaDB (www.openligadb.de), licensed under ODbL 1.0"
LICENSE = "https://www.openligadb.de/en/license"
TERMS = "https://www.openligadb.de/en/terms-of-use"
AGENT = "polymarket861/0.1 (+https://github.com/wmqfl861/polymarket861/issues)"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS rf_cache (
 season INTEGER NOT NULL, scope TEXT NOT NULL, sha TEXT NOT NULL, marker TEXT,
 seen_at TEXT NOT NULL, checked_at TEXT NOT NULL, PRIMARY KEY(season,scope));
CREATE TABLE IF NOT EXISTS rf_observations (
 season INTEGER NOT NULL, fixture TEXT NOT NULL, match_id INTEGER NOT NULL, observed_at TEXT NOT NULL,
 source_sha TEXT NOT NULL, score TEXT, kickoff TEXT NOT NULL, round_id INTEGER NOT NULL,
 PRIMARY KEY(season,fixture,observed_at));
CREATE TABLE IF NOT EXISTS rf_retry (
 season INTEGER PRIMARY KEY, failures INTEGER NOT NULL, retry_at TEXT NOT NULL);
"""


def _db(root):
    db = connect(root)
    dbscope.prepare(db, "result-feed", _SCHEMA)
    return db


def _get(url):
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": AGENT,
            "Cache-Control": "no-cache",
        },
    )
    with urlopen(req, timeout=25) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("oversize OpenLigaDB response")
    return data


def _full_time(data, year, *, only_round=None):
    """Reject opponent/round ambiguity. Only a finished match with a distinct regulation result qualifies."""
    if not isinstance(data, list) or not data:
        raise ValueError("empty EPL feed")
    ids, fixtures = set(), set()
    for match in data:
        if (
            not isinstance(match, dict)
            or match.get("leagueShortcut") != "pl"
            or match.get("leagueSeason") != year
        ):
            raise ValueError("unexpected league or season")
        if year == 2026 and match.get("leagueId") != 5996:
            raise ValueError("unexpected EPL league id for 2026")
        names = [t.get("teamName") for t in (match["team1"], match["team2"])]
        if not all(isinstance(name, str) and name.strip() for name in names):
            raise ValueError("missing EPL team identity")
        names = [re.sub(r"\bF\.C\.", "FC", s, flags=re.I) for s in names]
        fid = fixture_id(f"{year}-{(year + 1) % 100:02d}", *names)
        mid = match.get("matchID")
        rnd = match["group"]["groupOrderID"]
        if (
            type(mid) is not int
            or mid <= 0
            or mid in ids
            or fid in fixtures
            or names[0] == names[1]
        ):
            raise ValueError("duplicate/invalid EPL fixture identity")
        if (
            type(rnd) is not int
            or not 1 <= rnd <= 38
            or (only_round and only_round != rnd)
        ):
            raise ValueError("unexpected matchweek")
        ids.add(mid)
        fixtures.add(fid)
        kickoff = instant(match["matchDateTimeUTC"])
        score = None
        error = None
        if match.get("matchIsFinished") is True:
            finals = [
                r
                for r in match.get("matchResults", [])
                if r.get("resultTypeKind") == "After90Minutes"
            ]
            if len(finals) == 1 and all(
                type(finals[0].get(k)) is int and finals[0][k] >= 0
                for k in ("pointsTeam1", "pointsTeam2")
            ):
                score = [finals[0]["pointsTeam1"], finals[0]["pointsTeam2"]]
            else:
                error = "ambiguous_90_minute_final"
        yield fid, mid, rnd, kickoff, score, error


def reconcile(root, year):
    """Compare with the project's independent result registry. No single candidate overwrites a score."""
    season = f"{year}-{(year + 1) % 100:02d}"
    official = {r["fixture_id"]: r for r in eligible_fixtures(root, season=season)}
    collected = instant(now_utc())
    with closing(_db(root)) as db:
        rows = list(
            db.execute(
                """SELECT * FROM (
            SELECT *,row_number() OVER(PARTITION BY fixture ORDER BY observed_at DESC) ranking
            FROM rf_observations WHERE season=?) WHERE ranking=1 ORDER BY fixture""",
                (year,),
            )
        )
    counts = Counter()
    conflicts = []
    pending = []
    latest = None
    for row in rows:
        score = json.loads(row["score"]) if row["score"] else None
        known = official.get(row["fixture"])
        if score is None:
            counts["without_confirmed_90m"] += 1
            if instant(row["kickoff"]) < collected - timedelta(days=1):
                counts["past_scheduled_without_90m"] += 1
            continue
        latest = max(latest or row["kickoff"], row["kickoff"])
        if not known:
            counts["unmatched_fixture"] += 1
            continue
        if (
            known["round_id"] != row["round_id"]
            or abs(
                (instant(known["kickoff"]) - instant(row["kickoff"])).total_seconds()
            )
            > 3 * 86400
        ):
            counts["schedule_needs_review"] += 1
            continue
        if known["home_goals"] is None:
            counts["candidate_only"] += 1
            pending.append(row["fixture"])
        elif [known["home_goals"], known["away_goals"]] == score:
            counts["corroborated"] += 1
        else:
            counts["conflicting"] += 1
            conflicts.append(
                {
                    "fixture_id": row["fixture"],
                    "source_match_id": row["match_id"],
                    "candidate_score": score,
                    "existing_score": [known["home_goals"], known["away_goals"]],
                    "reference": f"{BASE}/getmatchdata/{row['match_id']}",
                    "sha256": row["source_sha"],
                }
            )
    result = {
        "source": "OpenLigaDB",
        "role": "candidate",
        "season": season,
        "attribution": ATTRIBUTION,
        "license": LICENSE,
        "terms": TERMS,
        "licence_id": "ODbL-1.0",
        "fixtures_received": len(rows),
        "finished_90m": len(rows) - counts["without_confirmed_90m"],
        "latest_finished_kickoff": latest,
        "coverage": dict(counts),
        "quarantined": conflicts,
        "candidate_only_fixture_ids": pending,
        "policy": "Candidate results are first available at local receipt. "
        "A conflict is isolated. The official result registry and historical performance remain independent.",
    }
    save_report(root, "source", f"openligadb-epl-{year}", result)
    if conflicts:
        save_report(
            root,
            "incident",
            f"openligadb-epl-{year}-score-conflicts",
            {
                "status": "quarantined",
                "source": "OpenLigaDB",
                "attribution": ATTRIBUTION,
                "items": conflicts,
            },
        )
    return result


def sync(
    root, year, *, round_id=None, confirm=False, require_native=False, recent=False
):
    from . import result_admission

    if not 2000 <= year <= 2100 or (round_id is not None and not 1 <= round_id <= 38):
        raise ValueError("invalid EPL season or round")
    scope = str(round_id or "season")
    checked = now_utc()
    with closing(result_admission.database(root)) as db:
        old = db.execute(
            "SELECT * FROM rf_cache WHERE season=? AND scope=?", (year, scope)
        ).fetchone()
        retry = db.execute("SELECT * FROM rf_retry WHERE season=?", (year,)).fetchone()
        needs_first_native = (
            require_native
            and not db.execute(
                "SELECT 1 FROM rf_native_requests WHERE season=? AND scope=? LIMIT 1",
                (year, scope),
            ).fetchone()
        )

    def cached(reason):
        if not old:
            raise ValueError(reason)
        return reconcile(root, year) | {
            "fetch_status": reason,
            "receipt": old["seen_at"],
            "sha256": old["sha"],
        }

    if retry and instant(checked) < instant(retry["retry_at"]):
        return cached("backoff; retry after " + retry["retry_at"])
    if (
        old
        and not needs_first_native
        and instant(checked) - instant(old["checked_at"])
        < (result_admission.INTERVAL if confirm or recent else timedelta(hours=6))
    ):
        return cached("local_cache")
    # Full-season bootstrap, then small changed-round requests. A full-season
    # refetch after bootstrap is deliberately not an automatic poll of 380 matches.
    if old and round_id is None:
        return cached("season_cached; sync a matchweek to check for changes")
    marker = None
    try:
        if round_id:
            parsed = json.loads(_get(f"{BASE}/getlastchangedate/pl/{year}/{round_id}"))
            if not isinstance(parsed, str) or not parsed:
                raise ValueError("invalid OpenLigaDB change marker")
            marker = parsed
            if (
                old
                and old["marker"] == marker
                and not confirm
                and not needs_first_native
            ):
                with closing(_db(root)) as db, db:
                    db.execute(
                        "UPDATE rf_cache SET checked_at=? WHERE season=? AND scope=?",
                        (checked, year, scope),
                    )
                return cached("source_marker_unchanged")
        url = f"{BASE}/getmatchdata/pl/{year}" + (f"/{round_id}" if round_id else "")
        raw = _get(url)
        seen = now_utc()
        rows = list(_full_time(json.loads(raw), year, only_round=round_id))
        if any(
            kickoff >= instant(seen)
            for _, _, _, kickoff, score, _ in rows
            if score is not None
        ):
            raise ValueError("finished result is dated after the actual receipt")
        ref = archive_bytes(
            root,
            "openligadb-results",
            raw,
            observed_at=seen,
            metadata={"url": url, "license": "ODbL-1.0", "attribution": ATTRIBUTION},
        )
        with closing(_db(root)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO rf_native_requests VALUES(?,?,?,?,?,?)",
                (year, scope, checked, seen, ref["sha256"], url),
            )
            # The earliest actual sighting of an unchanged score is retained. A
            # later correction/retraction is appended and never leaks into old cutoffs.
            for fid, mid, rnd, kickoff, score, _error in rows:
                values = (
                    canonical(score) if score is not None else None,
                    kickoff.isoformat(),
                    rnd,
                )
                prior = db.execute(
                    "SELECT score,kickoff,round_id FROM rf_observations WHERE season=? AND fixture=? "
                    "ORDER BY observed_at DESC LIMIT 1",
                    (year, fid),
                ).fetchone()
                if prior and tuple(prior) == values:
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO rf_observations VALUES (?,?,?,?,?,?,?,?)",
                    (year, fid, mid, seen, ref["sha256"], *values),
                )
            db.execute(
                "INSERT INTO rf_cache VALUES (?,?,?,?,?,?) ON CONFLICT(season,scope) DO UPDATE SET "
                "sha=excluded.sha, marker=excluded.marker,seen_at=excluded.seen_at,checked_at=excluded.checked_at "
                "WHERE rf_cache.seen_at<excluded.seen_at",
                (year, scope, ref["sha256"], marker, seen, seen),
            )
            db.execute("DELETE FROM rf_retry WHERE season=?", (year,))
        return reconcile(root, year) | {
            "fetch_status": "retrieved",
            "receipt": seen,
            "sha256": ref["sha256"],
        }
    except (HTTPError, OSError) as exc:
        n = (retry["failures"] if retry else 0) + 1
        wait = min(6 * 3600, 60 * 2 ** min(n, 8))
        if (
            isinstance(exc, HTTPError)
            and str(exc.headers.get("Retry-After", "")).isdigit()
        ):
            wait = max(wait, int(exc.headers["Retry-After"]))
        delay = (instant(checked) + timedelta(seconds=wait)).isoformat()
        with closing(_db(root)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO rf_retry VALUES (?,?,?)", (year, n, delay)
            )
        if old:
            return cached("stale_cache; upstream unavailable, retry after " + delay)
        raise


def trial_sidecar(root, year, base_evidence, output):
    """Write a private independent study sidecar. Never overwrite the primary sidecar."""
    if Path(base_evidence).resolve() == Path(output).resolve():
        raise ValueError("the trial sidecar must differ from the primary file")
    report = reconcile(root, year)
    allowed = set(report["candidate_only_fixture_ids"])
    with closing(_db(root)) as db:
        rows = list(
            db.execute(
                "SELECT * FROM rf_observations WHERE season=? ORDER BY observed_at,fixture",
                (year,),
            )
        )
    entries = list(read_json_rows(base_evidence))
    observed_scores = set()
    primary = {x["fixture_id"] for x in entries if "score" in x}
    taken = 0
    for row in rows:
        if row["fixture"] not in allowed or row["fixture"] in primary:
            continue
        if row["score"] is None and row["fixture"] not in observed_scores:
            continue
        if row["score"] is not None:
            observed_scores.add(row["fixture"])
        receipt = row["observed_at"]
        # A withdrawal is an explicit version and must not become a zero score.
        entries.append(
            {
                "fixture_id": row["fixture"],
                "score": json.loads(row["score"]) if row["score"] else None,
                "retracted": row["score"] is None,
                "archive_at": receipt,
                "published_by": receipt,
                "finalized_by": receipt,
                "source": f"OpenLigaDB pl/{year}, match {row['match_id']}, raw sha256 {row['source_sha']}; {ATTRIBUTION}",
            }
        )
        taken += 1
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as fh:
        temp = Path(fh.name)
        fh.write(("".join(canonical(x) + "\n" for x in entries)).encode())
    try:
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    return {
        "attribution": ATTRIBUTION,
        "appended_candidate_versions": taken,
        "output": str(output),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "mode": "research_candidate",
    }


def sync_primary(root, year, *, round_id=None):
    capture = sync(root, year, round_id=round_id, confirm=round_id is not None)
    return capture | {"regulation": sync_season(root, year, offline=True)}


def sync_season(root, year, *, offline=False):
    from .result_operations import season

    return season(root, year, offline=offline)


def import_round(root, year, round_id, file):
    from .result_operations import manual

    return manual(root, year, round_id, file)


def rollback_batch(root, batch_id, reason):
    from .result_operations import rollback

    return rollback(root, batch_id, reason)


def register(subparsers):
    cmd = subparsers.add_parser(
        "result-feed", help="候选免费赛果、对账与影子样本"
    ).add_subparsers(required=True)
    from . import independent_results, result_freshness

    p = cmd.add_parser("secondary", help="获取带署名与修订号的英超俱乐部终场赛果原件")
    p.add_argument("year", type=int)
    p.set_defaults(
        func=lambda a: independent_results.sync(
            a.data_dir,
            a.year,
            eligible_fixtures(a.data_dir, season=f"{a.year}-{(a.year + 1) % 100:02d}"),
        )
    )
    p = cmd.add_parser("freshness", help="查看最近轮次的赛果缺口和下一轮各队的陈旧状态")
    p.add_argument("year", type=int)
    p.add_argument("--at", help="只用这个带时区时间之前实际收到并登记的信息")
    p.set_defaults(func=lambda a: result_freshness.status(a.data_dir, a.year, a.at))
    p = cmd.add_parser("season", help="确认英超90分钟比分、刷新训练输入并发布逐轮评分")
    p.add_argument("year", type=int)
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=lambda a: sync_season(a.data_dir, a.year, offline=a.offline))
    p = cmd.add_parser("import-round", help="从获准原件原子导入已登记整轮的90分钟赛果")
    p.add_argument("year", type=int)
    p.add_argument("round_id", type=int)
    p.add_argument("file", type=Path)
    p.set_defaults(func=lambda a: import_round(a.data_dir, a.year, a.round_id, a.file))
    p = cmd.add_parser("rollback-batch", help="撤销整批赛果并保留原件、理由和历次评分")
    p.add_argument("batch_id")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=lambda a: rollback_batch(a.data_dir, a.batch_id, a.reason))
    p = cmd.add_parser("sync", help="缓存并核对 OpenLigaDB 当季或单轮赛果")
    p.add_argument("year", type=int, help="赛季开始年份")
    p.add_argument("--round", dest="round_id", type=int)
    p.set_defaults(func=lambda a: sync_primary(a.data_dir, a.year, round_id=a.round_id))
    p = cmd.add_parser("compare", help="重新对照正式比分并隔离冲突")
    p.add_argument("year", type=int)
    p.set_defaults(func=lambda a: reconcile(a.data_dir, a.year))
    p = cmd.add_parser("trial-sidecar", help="生成单独的候选历史输入，实际收件前不可用")
    p.add_argument("year", type=int)
    p.add_argument("primary", type=Path)
    p.add_argument("output", type=Path)
    p.set_defaults(
        func=lambda a: trial_sidecar(a.data_dir, a.year, a.primary, a.output)
    )
