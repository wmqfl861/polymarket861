"""Authenticated schedule acceptance and read-only source admission evidence."""

import hashlib
import json
import zlib
from contextlib import closing
from datetime import date, timedelta

from . import data, data_capabilities, source_probes, storage


def _samples(db, scan_id):
    """Read the immutable response pages for this attempt, never latest cached rows."""
    rows = []
    receipts = []
    for page in db.execute(
        """SELECT r.query,r.sha,r.available_at,b.body FROM nba_game_requests r
           JOIN nba_game_blobs b ON r.sha=b.sha
           WHERE json_extract(r.query,'$.scan_id')=? AND r.details='game_page'
             AND r.status=200 AND r.available_at!=? ORDER BY r.id""",
        (scan_id, data.PENDING),
    ):
        phase = json.loads(page["query"])["season_type"]
        receipts.append(
            {
                "phase": phase,
                "sha256": page["sha"],
                "available_at": page["available_at"],
            }
        )
        for blob in json.loads(zlib.decompress(page["body"]))["data"]:
            try:
                row = data.normalize_bdl(blob, phase)
            except (KeyError, ValueError, TypeError, OverflowError, AttributeError):
                continue
            rows.append((phase, row, page["sha"]))
    return rows, receipts


def _changes(root, ids, through):
    """Bind old and new precise scheduled tips to two point-in-time source receipts."""
    if not ids:
        return []
    changes = []
    previous = {}
    for r in data.game_versions(root, as_of=through):
        gid = r["game_id"]
        if gid not in ids:
            continue
        old = previous.get(gid)
        same_teams = bool(
            old and old["home"] == r.get("home") and old["away"] == r.get("away")
        )
        if r["status"] == "postponed" and same_teams and not r.get("play_observed"):
            # An intermediate postponement may precede a genuine revised schedule.
            continue
        if (
            r["stage"] != "regular"
            or r["status"] != "scheduled"
            or r.get("stage_basis") not in ("provider_fields", "disjoint_phase_queries")
            or not r.get("home")
            or not r.get("away")
            or r.get("play_observed")
            or not r.get("kickoff")
            or not r["received_at"] <= r["available_at"] < r["kickoff"]
        ):
            previous.pop(gid, None)
            continue
        if (
            same_teams
            and old["kickoff"] != r["kickoff"]
            and old["received_at"] < r["received_at"]
            and old["available_at"] <= r["received_at"]
        ):
            changes.append(
                {
                    "game_id": gid,
                    "home": r["home"],
                    "away": r["away"],
                    "previous_kickoff": old["kickoff"],
                    "kickoff": r["kickoff"],
                    "before_received_at": old["received_at"],
                    "before_available_at": old["available_at"],
                    "after_received_at": r["received_at"],
                    "after_available_at": r["available_at"],
                    "before_sha256": old["receipt_sha256"],
                    "after_sha256": r["receipt_sha256"],
                }
            )
        previous[gid] = r
    return changes


def _check(state, **evidence):
    return {
        "state": "pass" if state is True else "fail" if state is False else state,
        **evidence,
    }


def _audit_range(root, name, scan, anchor):
    quality = scan["quality"]
    rows, pages = [], []
    if scan.get("scan_id"):
        with closing(storage.open_existing(root)) as db:
            rows, pages = _samples(db, scan["scan_id"])
    phases = quality["phase_rows"]
    scheduled = [r for _, r, _ in rows if r["status"] == "scheduled"]
    recent_days = [day for day in quality["game_days"] if day <= str(anchor)][-3:]
    preseason = {r["game_id"] for phase, r, _ in rows if phase == "preseason"}
    regular = {
        r["game_id"]
        for phase, r, _ in rows
        if phase == "regular" or r["stage"] == "regular"
    }
    postponed = [
        {"game_id": r["game_id"], "status": r["status"], "sha256": sha}
        for _, r, sha in rows
        if r["status"] == "postponed"
    ]
    # Restrict date-change witnesses to the scope of these actual page receipts.
    changes = _changes(root, regular, scan["finished"]) if scan.get("complete") else []
    checks = {
        "authenticated_pages": _check(
            bool(pages) and len(pages) == scan["pages"], pages=len(pages)
        ),
        "complete_pagination_and_disjoint_phases": _check(
            bool(
                scan["complete"]
                and phases
                and not quality["overlapping_ids"]
                and not quality["phase_mismatches"]
            ),
            phase_rows=phases,
            overlapping_ids=quality["overlapping_ids"],
            phase_mismatches=quality["phase_mismatches"],
        ),
        "zero_quarantine": _check(
            bool(pages)
            and not any(
                quality[k]
                for k in ("quarantined_rows", "unidentified_rows", "pagination_errors")
            ),
            quarantined=quality["quarantined_rows"],
            unidentified=quality["unidentified_rows"],
        ),
        "scheduled_kickoffs": _check(
            bool(pages)
            and all(r.get("kickoff") for r in scheduled)
            and (bool(scheduled) or name != "recent"),
            scheduled=len(scheduled),
            missing=sum(not r.get("kickoff") for r in scheduled),
        ),
        "venue_field_inventory": _check(
            bool(rows),
            present_boolean=quality["neutral_known"],
            absent_or_null=quality["neutral_unknown"],
        ),
    }
    if name == "recent":
        from . import venue

        games = {
            r["game_id"]: r
            for _, r, _ in rows
            if r["stage"] == "regular" and r["game_date"] in recent_days
        }
        bases = {}
        for r in games.values():
            _, basis, _ = venue.resolve(r, as_of=data._utc(scan["finished"]))
            bases[basis] = bases.get(basis, 0) + 1
        with_home = bases.get("confirmed_non_neutral", 0) + bases.get(
            "assumed_listed_home", 0
        )
        checks["regular_home_court_coverage"] = _check(
            bool(games) and with_home / len(games) >= 0.99,
            games=len(games),
            with_home_court=with_home,
            proportion=with_home / len(games) if games else None,
            basis_counts=bases,
        )
        checks["three_recent_game_days"] = _check(
            len(recent_days) == 3, days=recent_days
        )
    if name == "october":
        checks["preseason_excluded_from_training"] = _check(
            bool(preseason and regular)
            and not (preseason & regular)
            and scan["complete"],
            preseason_ids=len(preseason),
            regular_ids=len(regular),
            overlapping_ids=sorted(preseason & regular)[:10],
        )
    if name == "previous_cup":
        checks["cup_knockout_observed"] = _check(
            quality["cup_knockout"] > 0 and scan["complete"],
            rows=quality["cup_knockout"],
        )
    return {
        "name": name,
        "range": [scan["start"], scan["end"]],
        "state": scan["state"],
        "scan_id": scan.get("scan_id"),
        "manifest_sha256": scan.get("evidence_sha256"),
        "receipts": pages,
        "checks": checks,
        "postponed_examples": postponed[:8],
        "rescheduled_examples": changes[:8],
    }


def verify_schedule(
    root, through=None, exception_days=(), *, fetch=None, clock=None, sleep=None
):
    clock = clock or data._now
    today = data._utc(clock()).astimezone(data.EASTERN).date()
    anchor = data._date(through) if through else today
    if anchor > today:
        raise ValueError("source acceptance cannot be anchored to a future day")
    season = anchor.year - int(anchor.month < 10)
    ranges = [
        ("recent", anchor - timedelta(days=13), anchor + timedelta(days=3)),
        ("october", date(season, 10, 1), date(season, 10, 31)),
        ("previous_cup", date(season - 1, 12, 1), date(season - 1, 12, 31)),
    ]
    for d in sorted({data._date(d) for d in exception_days}):
        if d > today:
            raise ValueError("exception date must be observed")
        ranges.append(("exception", d, d))
    results = []
    for name, start, end in ranges:
        scan = data.sync_bdl(root, start, end, fetch=fetch, clock=clock, sleep=sleep)
        result = _audit_range(root, name, scan, anchor)
        results.append(result)
    postponed = [
        r
        for scan in results
        if scan["state"] == "ok"
        for r in scan["postponed_examples"]
    ]
    rescheduled = [
        r
        for scan in results
        if scan["state"] == "ok"
        for r in scan["rescheduled_examples"]
    ]
    checks = {
        "postponement_observed": _check(
            True if postponed else "not_observed", examples=postponed[:8]
        ),
        "reschedule_observed": _check(
            True if rescheduled else "not_observed", examples=rescheduled[:8]
        ),
    }
    check_results = [
        v["state"] for result in results for v in result["checks"].values()
    ] + [v["state"] for v in checks.values()]
    state = (
        "missing_key"
        if not data._key()
        else "failed_checks"
        if "fail" in check_results
        else "needs_observation"
        if "not_observed" in check_results
        else "verified"
    )
    result = {
        "state": state,
        "verified": state == "verified",
        "reschedule_policy": "same-teams-two-pregame-commits-v1",
        "anchor_day": str(anchor),
        "ranges": results,
        "checks": checks,
        "observed_at": data._iso(clock()),
    }
    if data._key():
        body = data._encoded(result)
        with closing(storage.connect(root)) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS nba_schedule_acceptance (
                id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, sha TEXT NOT NULL, report TEXT NOT NULL)""")
            with db:
                db.execute("BEGIN EXCLUSIVE")
                db.execute(
                    "INSERT INTO nba_schedule_acceptance(observed_at,sha,report) VALUES(?,?,?)",
                    (
                        data._iso(clock()),
                        hashlib.sha256(body.encode()).hexdigest(),
                        body,
                    ),
                )
    return result


def admission_summary(root, through=None):
    cutoff = source_probes._cutoff(through, data._now())
    db = storage.open_existing(root)
    report = None
    try:
        if source_probes._table(db, "nba_schedule_acceptance"):
            r = db.execute(
                "SELECT sha,report FROM nba_schedule_acceptance WHERE observed_at<=? ORDER BY id DESC LIMIT 1",
                (data._iso(cutoff),),
            ).fetchone()
            if r:
                report = dict(json.loads(r["report"]), report_sha256=r["sha"])
    finally:
        if db:
            db.close()
    current_proof = bool(
        report
        and report.get("reschedule_policy") == "same-teams-two-pregame-commits-v1"
    )
    schedule_state = (
        report["state"] if report else "not_run" if data._key() else "missing_key"
    )
    if report and report["verified"] and not current_proof:
        schedule_state = "reschedule_evidence_requires_rescan"
    sources = source_probes.player_summary(root, cutoff)
    return {
        "as_of": data._iso(cutoff),
        "schedule_verified": bool(report and report["verified"] and current_proof),
        "schedule_state": schedule_state,
        "schedule_checks": report,
        "sources": sources,
        "eligible_player_sources": [
            source for source, item in sources.items() if item["source_verified"]
        ],
    }


def register(actions):
    p = actions.add_parser(
        "verify-schedule", help="分三段认证核对赛程；另可给出已有改期/延期的证据日期"
    )
    p.add_argument(
        "--through", type=data._date, help="最近比赛日查询的美东参考日期，默认今天"
    )
    p.add_argument("--exception-day", type=data._date, action="append", default=[])
    p.set_defaults(
        func=lambda a: verify_schedule(a.data_dir, a.through, a.exception_day),
        result_exit_code=lambda r: int(not r["verified"]),
    )
    p = actions.add_parser(
        "probe-due", help="在美东首场的到期窗口内采集本次独立免费回执"
    )
    p.set_defaults(func=lambda a: source_probes.probe_due(a.data_dir))
    p = actions.add_parser(
        "record-terms", help="在本机保存条款、上游身份和状态时间语义的具名审核凭据"
    )
    p.add_argument("--source", choices=sorted(data_capabilities.PROBES), required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--terms-file", required=True)
    p.add_argument("--upstream", required=True)
    p.add_argument("--upstream-file")
    for field in ("prediction", "retention"):
        p.add_argument(
            "--" + field, choices=("allowed", "denied", "unverified"), required=True
        )
    p.add_argument(
        "--timing-field",
        choices=data_capabilities.TIMING_FIELDS,
    )
    p.add_argument("--timing-file")
    p.set_defaults(
        func=lambda a: source_probes.record_terms(
            a.data_dir,
            a.source,
            a.reviewer,
            a.terms_file,
            a.upstream,
            a.upstream_file,
            a.prediction,
            a.retention,
            a.timing_field,
            a.timing_file,
        )
    )
