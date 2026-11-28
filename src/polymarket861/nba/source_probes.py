"""Prospective injury-source timing evidence for the first NBA tip of each Eastern day."""

import hashlib
import json
import zlib
from collections import defaultdict
from contextlib import closing
from datetime import datetime, time, timedelta
from pathlib import Path

from . import data, data_capabilities, storage


def _table(db, name):
    return (
        db is not None
        and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _cutoff(through, now):
    if through is None:
        return data._utc(now)
    end = datetime.combine(
        data._date(through) + timedelta(days=1), time.min, data.EASTERN
    )
    return min(data._utc(now), data._utc(end) - timedelta(microseconds=1))


def _first(rows):
    fixtures = [
        r
        for r in rows
        if r["stage"] == "regular"
        and r.get("kickoff")
        and r["status"] in ("scheduled", "live", "final")
    ]
    return min(fixtures, key=lambda r: (r["kickoff"], r["game_id"]), default=None)


def probe_due(root, *, clock=None, fetch=None):
    clock = clock or data._now
    now = data._utc(clock())
    day = now.astimezone(data.EASTERN).date()
    first = _first(data.games_on_day_as_of(root, day, now))
    proof = data.schedule_proof(root, day, now)
    schedule_ok = bool(
        proof and proof["complete"] and (proof.get("quality") or {}).get("phase_rows")
    )
    target = {
        "game_day": str(day),
        "game_id": first["game_id"] if first else None,
        "kickoff": first["kickoff"] if first else None,
        "schedule_receipt_sha256": first["receipt_sha256"] if first else None,
        "schedule_scan_sha256": proof["evidence_sha256"] if schedule_ok else None,
    }
    targets = {}
    if first and schedule_ok and first["status"] == "scheduled":
        tip = data._utc(first["kickoff"])
        for mins in (90, 60, 15):
            closes = tip - timedelta(minutes=mins)
            opens = closes - timedelta(minutes=5)
            if opens <= now <= closes:
                slot = dict(
                    target,
                    slot=f"T-{mins}",
                    opens=data._iso(opens),
                    closes=data._iso(closes),
                )
                targets["bigballs"] = slot
                if mins == 60:
                    targets["dunkapi"] = dict(slot, slot="daily", horizon_minutes=60)
    # One league sample per Eastern date even on a rest day or after a missed T-60.
    # Such a late sample can never stand in for a timed observation.
    after_window = first and now > data._utc(first["kickoff"]) - timedelta(minutes=60)
    if "dunkapi" not in targets and (
        after_window or now.astimezone(data.EASTERN).hour >= 21
    ):
        closes = datetime.combine(
            day + timedelta(days=1), time.min, data.EASTERN
        ) - timedelta(microseconds=1)
        targets["dunkapi"] = dict(
            target,
            slot="daily",
            horizon_minutes=None,
            opens=data._iso(now),
            closes=data._iso(closes),
        )
    result = {}
    for source in data_capabilities.PROBES:
        if source in targets:
            result[source] = data_capabilities.probe(
                root, source, fetch=fetch, clock=clock, target=targets[source]
            )
        else:
            result[source] = {
                "state": "outside_window" if schedule_ok else "schedule_unverified"
            }
    return {"game_day": str(day), "first_game": target, "sources": result}


def _calendar_gaps(root, first_day, cutoff, targets):
    from .markets_contracts import team_code
    from .markets_schedule import PAIR_TOLERANCE, _catalogue_proof

    first = data._date(first_day)
    last = cutoff.astimezone(data.EASTERN).date()
    nominal = defaultdict(list)
    db = storage.open_existing(root)
    try:
        if _table(db, "nba_market_schedule"):
            query = """SELECT g.id,g.home,g.away,s.kickoff,s.valid_from,p.observed AS play_at
                       FROM nba_market_schedule s JOIN nba_market_games g ON s.id=g.id
                       LEFT JOIN nba_market_play p ON p.id=g.id AND p.observed<=?
                       WHERE s.valid_from<=? ORDER BY s.version"""
            for rec in db.execute(query, (data._iso(cutoff), data._iso(cutoff))):
                nominal[rec["id"]].append(dict(rec))
        native = defaultdict(list)
        for history in nominal.values():
            for day in {
                data._utc(r["kickoff"]).astimezone(data.EASTERN).date() for r in history
            }:
                if not first <= day <= last:
                    continue
                end = _cutoff(day, cutoff)
                visible = [r for r in history if r["valid_from"] <= data._iso(end)]
                if (
                    visible
                    and data._utc(visible[-1]["kickoff"])
                    .astimezone(data.EASTERN)
                    .date()
                    == day
                ):
                    native[str(day)].append(visible[-1])
        verified_rest = []
        day = first
        while day <= last:
            iso_day = str(day)
            end = _cutoff(day, cutoff)
            closed = data._utc(
                datetime.combine(day + timedelta(days=1), time.min, data.EASTERN)
            ) - end <= timedelta(microseconds=1)
            events = [
                r
                for r in native[iso_day]
                if data._utc(r["kickoff"]) - timedelta(minutes=60) <= end
            ]
            known = targets.get(iso_day)
            if known and known["game_id"]:
                known["calendar_status"] = "score_schedule"

                def same_pair(event):
                    return (team_code(event["home"]), team_code(event["away"])) == (
                        team_code(known["home"]),
                        team_code(known["away"]),
                    )

                matches = [e for e in native[iso_day] if same_pair(e)]
                unmatched_earlier = any(
                    e["kickoff"] < known["kickoff"] and not same_pair(e) for e in events
                )
                if len(matches) > 1:
                    known["calendar_status"] = "ambiguous_native_fixture"
                elif matches:
                    venue = matches[0]
                    known.update(
                        native_event_id=venue["id"],
                        native_kickoff=venue["kickoff"],
                        native_play_observed_at=venue["play_at"],
                    )
                    delta = abs(
                        data._utc(venue["kickoff"]) - data._utc(known["kickoff"])
                    )
                    if delta > PAIR_TOLERANCE:
                        known["calendar_status"] = "native_kickoff_conflict"
                if unmatched_earlier:
                    known.update(
                        game_id=None,
                        kickoff=None,
                        t60=None,
                        calendar_status="earlier_native_listing_unmatched",
                    )
            elif events:
                targets[iso_day] = dict(
                    game_id=None,
                    kickoff=None,
                    t60=None,
                    calendar_status="native_listing_without_verified_first_tip",
                )
            elif known:
                known["calendar_status"] = "score_schedule_without_verified_first_tip"
            elif closed:
                proof = data.schedule_proof(root, day, end)
                market = _catalogue_proof(db, iso_day, data._iso(end))
                quality = (proof or {}).get("quality") or {}
                score_clear = (
                    proof
                    and proof["complete"]
                    and quality.get("phase_rows")
                    and not quality["quarantined_rows"]
                    and isinstance(quality.get("game_days"), list)
                    and iso_day not in quality["game_days"]
                    and data._utc(proof["finished"]).astimezone(data.EASTERN).date()
                    == day
                )
                market_clear = (
                    market
                    and data._utc(market["completed"]).astimezone(data.EASTERN).date()
                    == day
                )
                if score_clear and market_clear:
                    verified_rest.append(iso_day)
                else:
                    targets[iso_day] = dict(
                        game_id=None,
                        kickoff=None,
                        t60=None,
                        calendar_status="unverified_calendar_day",
                    )
            day += timedelta(days=1)
        return dict(sorted(targets.items())), verified_rest
    finally:
        if db:
            db.close()


def _expected_days(root, cutoff):
    """Find the first tip using only versions that were already available at its T-60."""
    versions = data.game_versions(root, as_of=cutoff)
    days = defaultdict(set)
    histories = defaultdict(list)
    first_monitored = None
    for row in versions:
        histories[row["game_id"]].append(row)
        if (
            row["stage"] not in ("regular", "unknown")
            or row.get("stage_basis") == "legacy_requires_rescan"
        ):
            continue
        day = row["game_date"]
        days[day].add(row["game_id"])
        if row["stage"] != "regular" or not row.get("kickoff"):
            continue
        if row["status"] == "scheduled" and row["available_at"] < row["kickoff"]:
            first_monitored = min(first_monitored or day, day)
    if first_monitored is None:
        return {}, []
    result = {}
    for day, ids in sorted(days.items()):
        if day < first_monitored or day > str(cutoff.astimezone(data.EASTERN).date()):
            continue
        tips = sorted(
            {
                r["kickoff"]
                for gid in ids
                for r in histories[gid]
                if r["game_date"] == day
                and r["stage"] == "regular"
                and r.get("kickoff")
            }
        )
        for tip in tips:
            gate = data._utc(tip) - timedelta(minutes=60)
            if gate > cutoff:
                break
            visible = []
            for gid in ids:
                rows = [
                    r for r in histories[gid] if r["available_at"] <= data._iso(gate)
                ]
                if rows and rows[-1]["game_date"] == day:
                    visible.append(rows[-1])
            first = _first(visible)
            if first and first["kickoff"] == tip and first["status"] == "scheduled":
                result[day] = {
                    "game_id": first["game_id"],
                    "home": first.get("home_abbr"),
                    "away": first.get("away_abbr"),
                    "kickoff": tip,
                    "t60": data._iso(gate),
                }
                break
        if (
            day not in result
            and not tips
            and day < str(cutoff.astimezone(data.EASTERN).date())
        ):
            result[day] = {"game_id": None, "kickoff": None, "t60": None}
        if (
            day not in result
            and tips
            and data._utc(tips[-1]) - timedelta(minutes=60) <= cutoff
        ):
            latest = [
                histories[gid][-1]
                for gid in ids
                if histories[gid][-1]["game_date"] == day
            ]
            if _first(latest):
                result[day] = {"game_id": None, "kickoff": None, "t60": None}
    return _calendar_gaps(root, first_monitored, cutoff, result)


def record_terms(
    root,
    source,
    reviewer,
    terms_file,
    upstream,
    upstream_file,
    prediction,
    retention,
    timing_field=None,
    timing_file=None,
    *,
    clock=None,
):
    if not reviewer.strip() or not upstream.strip():
        raise ValueError("reviewer and upstream identity are required")
    if prediction not in ("allowed", "denied", "unverified") or retention not in (
        "allowed",
        "denied",
        "unverified",
    ):
        raise ValueError("invalid rights decision")
    if source == "bigballs" and timing_field in (
        *data_capabilities.DATE_FIELDS,
        *data_capabilities.CLOCK_FIELDS,
    ):
        # A leaf refers to the documented source business object, $.data for Big Balls.
        timing_field = "data." + timing_field
    if bool(timing_field) != bool(timing_file) or (
        timing_field is not None and timing_field not in data_capabilities.TIMING_FIELDS
    ):
        raise ValueError(
            "timing field and its upstream documentation must be provided together"
        )
    paths = {"terms": terms_file, "upstream": upstream_file, "timing": timing_file}
    files = {kind: Path(path).read_bytes() for kind, path in paths.items() if path}
    if any(not content.strip() for content in files.values()):
        raise ValueError("empty evidence file")
    hashes = {
        kind: hashlib.sha256(content).hexdigest() for kind, content in files.items()
    }
    with closing(storage.connect(root)) as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS nba_source_rights_blobs(sha TEXT PRIMARY KEY,body BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS nba_source_rights(id INTEGER PRIMARY KEY,source TEXT NOT NULL,observed_at TEXT NOT NULL,decision TEXT NOT NULL);""")
        with db:
            db.execute("BEGIN EXCLUSIVE")
            at = data._iso((clock or data._now)())
            decision = {
                "source": source,
                "reviewer": reviewer.strip(),
                "reviewed_at": at,
                "terms_url": data_capabilities.RIGHTS[source][0],
                "upstream": upstream.strip(),
                "upstream_identified": bool(upstream_file)
                and upstream.strip().lower() not in ("unknown", "unverified", "未知"),
                "prediction_rights": prediction,
                "retention_rights": retention,
                "timing_field": timing_field,
                "evidence_sha256": hashes,
            }
            for kind, content in files.items():
                db.execute(
                    "INSERT OR IGNORE INTO nba_source_rights_blobs VALUES(?,?)",
                    (hashes[kind], zlib.compress(content)),
                )
            db.execute(
                "INSERT INTO nba_source_rights(source,observed_at,decision) VALUES(?,?,?)",
                (source, at, data._encoded(decision)),
            )
    return decision


def _eastern_source_day(stamp):
    try:
        return data._utc(stamp).astimezone(data.EASTERN).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _receipt_evidence(receipt, target, day, terms, *, source):
    fields = (
        "probe_id",
        "state",
        "received_at",
        "ingested_at",
        "sample_sha256",
        "response_sha256",
        "sample_rows",
        "player_status_rows",
        "upstream_clocks",
        "source_dates",
        "league_scope_basis",
        "provider_stale",
        "requested_at",
        "http_status",
        "observed_field_names",
    )
    summary = {k: receipt.get(k) for k in fields}
    slot = receipt.get("target") or {}
    summary["slot"] = slot.get("slot")
    summary["target_game_id"] = slot.get("game_id")
    gate = target["t60"]
    if gate and receipt.get("received_at"):
        summary["seconds_before_t60"] = (
            data._utc(gate) - data._utc(receipt["received_at"])
        ).total_seconds()
    same_fixture = bool(
        gate
        and target.get("calendar_status") == "score_schedule"
        and slot.get("game_id") == target["game_id"]
        and slot.get("kickoff") == target["kickoff"]
        and slot.get("schedule_scan_sha256")
    )
    timed = slot.get("slot") == "T-60" or (
        slot.get("slot") == "daily" and slot.get("horizon_minutes") == 60
    )
    mins = {"T-90": 90, "T-60": 60, "T-15": 15}.get(
        slot.get("slot"), slot.get("horizon_minutes")
    )
    summary["window_usable"] = bool(
        same_fixture
        and mins
        and all(
            slot["closes"] < guard
            for guard in (
                target.get("native_kickoff"),
                target.get("native_play_observed_at"),
            )
            if guard
        )
        and receipt.get("authenticated")
        # Older Big Balls receipts omitted both provider freshness and scope validation.
        and (source != "bigballs" or receipt.get("provider_stale") is False)
        and receipt.get("league_scope_basis")
        in ("response_explicit_nba", "nba_scoped_request")
        and receipt.get("player_status_rows")
        and slot["opens"]
        <= receipt["requested_at"]
        <= receipt["received_at"]
        <= receipt["ingested_at"]
        <= slot["closes"]
    )
    fresh = bool(
        summary["window_usable"]
        and same_fixture
        and timed
        and receipt.get("authenticated")
        and receipt.get("league_scope_basis")
        in ("response_explicit_nba", "nba_scoped_request")
        and receipt.get("player_status_rows")
        and receipt.get("ingested_at")
        and receipt["ingested_at"] <= gate
        and 0 <= summary.get("seconds_before_t60", -1) <= 15 * 60
    )
    # Absence of publication fields cannot be repaired with the local HTTP clock.
    field = (terms or {}).get("timing_field")
    same_day = False
    if fresh and field in (receipt.get("source_dates") or {}):
        same_day = receipt["source_dates"][field] == day
    elif fresh and field:
        stamps = (receipt.get("upstream_clocks") or {}).get(field) or {}
        last = stamps.get("last")
        same_day = _eastern_source_day(last) == day and last <= receipt["received_at"]
    # A provider-declared data vintage can contradict a newer row/transport clock.
    vintage_day = (receipt.get("source_dates") or {}).get("meta.as_of")
    vintage_time = (
        (receipt.get("upstream_clocks") or {}).get("meta.as_of") or {}
    ).get("last")
    if vintage_day:
        same_day = same_day and vintage_day == day
    if vintage_time:
        same_day = (
            same_day
            and _eastern_source_day(vintage_time) == day
            and vintage_time <= receipt["received_at"]
        )
    if source == "dunkapi":
        # Both REST and earlier bare reports describe the latest available daily snapshot.
        report_dates = receipt.get("source_dates") or {}
        snapshot_day = report_dates.get("data.date", report_dates.get("date"))
        same_day = same_day and snapshot_day == day
    summary.update(fresh_at_t60=fresh, source_same_day_status=same_day)
    return summary


def player_summary(root, cutoff):
    days, verified_rest = _expected_days(root, cutoff)
    db = storage.open_existing(root)
    try:
        results = {}
        for source in data_capabilities.PROBES:
            terms = None
            if _table(db, "nba_source_rights"):
                r = db.execute(
                    "SELECT decision FROM nba_source_rights WHERE source=? AND observed_at<=? ORDER BY id DESC LIMIT 1",
                    (source, data._iso(cutoff)),
                ).fetchone()
                terms = json.loads(r[0]) if r else None
            receipts = defaultdict(list)
            if _table(db, "nba_free_probes"):
                for r in db.execute(
                    "SELECT receipt FROM nba_free_probes WHERE source=? AND requested_at<=? ORDER BY id",
                    (source, data._iso(cutoff)),
                ):
                    row = json.loads(r[0])
                    target = row.get("target") or {}
                    if not target.get("game_day"):
                        continue
                    if not row.get("ingested_at") or row["ingested_at"] > data._iso(
                        cutoff
                    ):
                        # The request was reserved, but its response was not yet
                        # available at the historical cutoff (or the process died).
                        row = {
                            key: row.get(key)
                            for key in ("requested_at", "probe_id", "target")
                        }
                        row["state"] = "awaiting_durable_receipt"
                    receipts[target["game_day"]].append(row)
            evidence = []
            for day, target in days.items():
                samples = [
                    _receipt_evidence(r, target, day, terms, source=source)
                    for r in receipts[day]
                ]
                expected_slots = (
                    {"T-90", "T-60", "T-15"} if source == "bigballs" else {"daily"}
                )
                received_slots = {s["slot"] for s in samples if s["window_usable"]}
                evidence.append(
                    dict(
                        all_windows_covered=expected_slots == received_slots,
                        game_day=day,
                        **target,
                        samples=samples,
                        distinct_sample_hashes=len(
                            {s["sample_sha256"] for s in samples if s["sample_sha256"]}
                        ),
                        t60_fresh=any(s["fresh_at_t60"] for s in samples),
                        same_day_status=any(
                            s["source_same_day_status"] for s in samples
                        ),
                    )
                )
            latest = evidence[-7:]
            seven = len(latest) == 7 and all(d["t60_fresh"] for d in latest)
            latest_complete = [
                d
                for d in evidence
                if d["kickoff"] is None
                or data._utc(d["kickoff"])
                - timedelta(minutes=15 if source == "bigballs" else 60)
                <= cutoff
            ][-7:]
            all_windows = len(latest_complete) == 7 and all(
                d["all_windows_covered"] for d in latest_complete
            )
            same_day = seven and all(d["same_day_status"] for d in latest)
            rights = bool(
                terms
                and terms["prediction_rights"] == terms["retention_rights"] == "allowed"
            )
            upstream = bool(terms and terms["upstream_identified"])
            results[source] = {
                "game_days": sum(
                    d["calendar_status"] != "unverified_calendar_day" for d in evidence
                ),
                "unverified_calendar_dates": [
                    d["game_day"]
                    for d in evidence
                    if d["calendar_status"] == "unverified_calendar_day"
                ],
                "verified_rest_days": verified_rest,
                "t60_fresh_days": sum(d["t60_fresh"] for d in evidence),
                "seven_day_coverage": seven,
                "seven_day_all_windows": all_windows,
                "seven_day_same_day_status": same_day,
                "terms_approved": rights,
                "upstream_verified": upstream,
                "terms_review": terms,
                "source_verified": bool(
                    same_day and all_windows and rights and upstream
                ),
                "days": evidence,
            }
        return results
    finally:
        if db:
            db.close()
