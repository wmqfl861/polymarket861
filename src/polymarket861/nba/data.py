"""Point-in-time NBA game data. Historical end-state archives are opt-in."""

import csv
import hashlib
import io
import json
import os
import re
import time
import zlib
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ARCHIVE_SHA = "d46ed3540ee8d9eca31b3e94cc8c777e0be5156173d814ebf65b8195e8d616bc"
PENDING = "unpublished"
EASTERN = ZoneInfo("America/New_York")
BDL_URL = "https://api.balldontlie.io/nba/v1/games"
SCHEMA = """
CREATE TABLE IF NOT EXISTS nba_game_blobs (
    sha TEXT PRIMARY KEY, body BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS nba_game_requests (
    id INTEGER PRIMARY KEY, query TEXT NOT NULL, received_at TEXT NOT NULL,
    available_at TEXT NOT NULL, status INTEGER NOT NULL, sha TEXT, details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nba_game_versions (
    source TEXT NOT NULL, game_id TEXT NOT NULL, version INTEGER NOT NULL,
    received_at TEXT NOT NULL, available_at TEXT NOT NULL, sha TEXT NOT NULL,
    payload TEXT NOT NULL, PRIMARY KEY(source, game_id, version)
);
CREATE INDEX IF NOT EXISTS nba_game_by_kickoff ON nba_game_versions(source,json_extract(payload,'$.kickoff'));
CREATE INDEX IF NOT EXISTS nba_game_by_day ON nba_game_versions(source,json_extract(payload,'$.game_date'),game_id);
CREATE INDEX IF NOT EXISTS nba_game_chronology ON nba_game_versions(source, available_at);
CREATE TABLE IF NOT EXISTS nba_game_last_observation (
    source TEXT NOT NULL, game_id TEXT NOT NULL, received_at TEXT NOT NULL,
    payload_sha TEXT NOT NULL, receipt_sha TEXT NOT NULL, PRIMARY KEY(source, game_id)
);
CREATE TABLE IF NOT EXISTS nba_schedule_scans (
    id INTEGER PRIMARY KEY, start TEXT NOT NULL, end TEXT NOT NULL, began TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nba_schedule_completions (
    scan_id INTEGER PRIMARY KEY REFERENCES nba_schedule_scans(id),
    finished TEXT NOT NULL, state TEXT NOT NULL, complete INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS nba_game_pending ON nba_game_versions(available_at) WHERE available_at='unpublished';
CREATE INDEX IF NOT EXISTS nba_requests_pending ON nba_game_requests(available_at) WHERE available_at='unpublished';
CREATE TABLE IF NOT EXISTS nba_schedule_evidence (
    scan_id INTEGER PRIMARY KEY REFERENCES nba_schedule_scans(id),
    manifest TEXT NOT NULL, sha TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS nba_scan_pages ON nba_game_requests(json_extract(query,'$.scan_id'));
CREATE TABLE IF NOT EXISTS nba_bdl_budget (
    id INTEGER PRIMARY KEY CHECK(id=1), next_request REAL NOT NULL
);
"""


def _now():
    return datetime.now(timezone.utc)


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("an explicit time zone is required")
    return value.astimezone(timezone.utc)


def _iso(value):
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_538(path: Path):
    """Read the exact published archive, once per game; neutral rows use its designated side."""
    return _archive_rows(Path(path).read_bytes())


def _archive_rows(raw):
    if hashlib.sha256(raw).hexdigest() != ARCHIVE_SHA:
        raise ValueError("unrecognized FiveThirtyEight archive SHA-256")
    games = []
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8"))):
        if row["lg_id"] != "NBA" or row["_iscopy"] != "0":
            continue
        month, day, year = map(int, row["date_game"].split("/"))
        games.append(
            {
                "source": "538",
                "game_id": "538:" + row["game_id"],
                "season": int(row["year_id"]) - 1,
                "game_date": date(year, month, day).isoformat(),
                "kickoff": None,
                "home": row["team_id"],
                "away": row["opp_id"],
                "home_score": int(row["pts"]),
                "away_score": int(row["opp_pts"]),
                "neutral": row["game_location"] == "N",
                "stage": "postseason" if row["is_playoffs"] == "1" else "regular",
                "status": "final",
                "home_designation": "synthetic_neutral"
                if row["game_location"] == "N"
                else "listed_home",
                "evidence_grade": "final_archive_date_only",
                "score_basis": "final_includes_ot",
            }
        )
    return games


def _positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError("missing positive provider identifier")
    return f"bdl:{value}"


def _date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("expected an ISO calendar date")
    return date.fromisoformat(value)


def _team(blob):
    abbr, name = blob["abbreviation"], blob["full_name"]
    if not isinstance(abbr, str) or not re.fullmatch(r"[A-Z]{2,4}", abbr):
        raise ValueError("invalid team abbreviation")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("missing team name")
    return _positive_id(blob["id"]), abbr, name.strip()


def _stage(blob, filter_type):
    # NBAGame currently lacks season_type; only a positively scoped request
    # establishes the stage when the field is absent. The Cup title does not count.
    supplied = blob.get("season_type")
    phase = supplied if supplied is not None else filter_type
    cup = str(blob.get("ist_stage") or "").strip().lower()
    if supplied is not None and supplied not in (
        "regular",
        "ist",
        "playin",
        "playoffs",
        "preseason",
    ):
        return "unknown"
    if blob.get("postseason") is not None and type(blob["postseason"]) is not bool:
        return "unknown"
    if phase == "preseason" and (blob.get("postseason") is True or cup):
        return "unknown"
    if phase in ("playin", "playoffs", "preseason"):
        return {"playin": "playin", "playoffs": "postseason", "preseason": "preseason"}[
            phase
        ]
    if blob.get("postseason") is True:
        return "postseason"
    if cup in ("championship", "final", "nba cup championship"):
        return "cup_final"
    if cup:
        if re.fullmatch(
            r"(?:(?:east|west)(?:ern)?\s+)?(?:group [abc]|quarterfinals?|semifinals?)",
            cup,
        ):
            return "regular"
        return "unknown"
    return "regular" if phase == "regular" else "unknown"


def _period_in_progress(blob):
    detail = str(blob.get("period_detail") or "").strip().lower()
    return bool(
        re.search(
            r"\b(?:q[1-4]|quarter|qtr|half(?:time)?|overtime|ot|[1-4](?:st|nd|rd|th) period)\b",
            detail,
        )
    ) and not detail.startswith("final")


def _running_clock(blob):
    value = str(blob.get("time") or "").strip()
    match = re.fullmatch(r"([0-9]{1,2}):([0-5][0-9])(?:\.([0-9]+))?", value)
    return bool(match and any(int(x or 0) for x in match.groups()))


def _play_observed(blob):
    counters = ("period", "home_team_score", "visitor_team_score")
    if any(type(blob.get(k)) is int and blob[k] > 0 for k in counters):
        return True
    if any(
        type(value) is int
        and value > 0
        and re.fullmatch(r"(?:home|visitor|away)_(?:q[1-4]|ot[1-9]\d*)", k)
        for k, value in blob.items()
    ):
        return True
    if _period_in_progress(blob) or _running_clock(blob):
        return True
    return any(
        str(blob.get(k) or "").strip().lower().startswith("final")
        for k in ("time", "period_detail", "status")
    )


def _status(blob):
    raw = str(blob.get("status") or "").strip().lower()
    norm = blob.get("status_state")
    period = blob.get("period")
    h, a = blob.get("home_team_score"), blob.get("visitor_team_score")
    final_label = bool(re.fullmatch(r"final(?:\s*[/(-]?\s*\d*ot\)?)?", raw))
    protected = {
        "postponed": "postponed",
        "canceled": "cancelled",
        "cancelled": "cancelled",
        "delayed": "delayed",
        "suspended": "suspended",
        "abandoned": "abandoned",
    }
    if blob.get("postponed") is True:
        return "postponed"
    if blob.get("postponed") is not None and type(blob["postponed"]) is not bool:
        return "unknown"
    if raw in protected or norm in protected:
        return protected.get(raw) or protected[norm]
    scheduled_label = raw in (
        "scheduled",
        "pregame",
        "pre-game",
        "not started",
        "tbd",
    ) or bool(re.fullmatch(r"\d{1,2}:\d{2}\s*[ap]m(?:\s+(?:et|est|edt))?", raw))
    if raw and not scheduled_label:
        try:
            _utc(raw.upper())
            scheduled_label = True
        except (TypeError, ValueError):
            pass
    if norm is None:
        if final_label:
            norm = "final"
        elif scheduled_label:
            norm = "scheduled"
        elif type(period) is int and period > 0:
            norm = "in_progress"
    if norm == "final":
        if (
            (not final_label and raw)
            or _running_clock(blob)
            or _period_in_progress(blob)
            or type(h) is not int
            or type(a) is not int
            or min(h, a) <= 0
            or h == a
        ):
            return "unknown"
        return "final"
    if norm == "in_progress":
        return "live" if not final_label else "unknown"
    if (
        norm == "scheduled"
        and (scheduled_label or not raw)
        and not _play_observed(blob)
        and all(v is None or (type(v) is int and v == 0) for v in (period, h, a))
    ):
        return "scheduled"
    return "unknown"


def normalize_bdl(blob, filter_type=None):
    home, home_abbr, home_name = _team(blob["home_team"])
    away, away_abbr, away_name = _team(blob["visitor_team"])
    if home == away or home_abbr == away_abbr:
        raise ValueError("identical home and away participants")
    season = blob["season"]
    if type(season) is not int or season < 1946:
        raise ValueError("invalid NBA season")
    day = _date(blob["date"])
    if day.year not in (season, season + 1):
        raise ValueError("game date outside provider NBA season")
    kickoff, timing_problem = None, None
    if blob.get("datetime") is not None:
        try:
            precise = _utc(blob["datetime"])
            if precise.astimezone(EASTERN).date() == day:
                kickoff = _iso(precise)
            else:
                timing_problem = "kickoff_disagrees_with_eastern_game_date"
        except (ValueError, TypeError):
            timing_problem = "invalid_kickoff"
    neutral = blob.get("neutral_site")
    status = "unknown" if timing_problem else _status(blob)
    return {
        "source": "balldontlie",
        "game_id": _positive_id(blob["id"]),
        "season": season,
        "game_date": day.isoformat(),
        "kickoff": kickoff,
        "timing_problem": timing_problem,
        "stage": _stage(blob, filter_type),
        "stage_basis": "provider_fields"
        if _stage(blob, None) != "unknown"
        else "query_filter",
        "provider_season_type": blob.get("season_type")
        if isinstance(blob.get("season_type"), str)
        else None,
        "cup_stage": str(blob.get("ist_stage") or "").strip().lower() or None,
        "home": home,
        "away": away,
        "home_abbr": home_abbr,
        "away_abbr": away_abbr,
        "home_name": home_name,
        "away_name": away_name,
        "neutral": neutral if type(neutral) is bool else None,
        "final_period": blob["period"]
        if status == "final"
        and type(blob.get("period")) is int
        and 4 <= blob["period"] <= 19
        else None,
        "home_score": blob.get("home_team_score") if status == "final" else None,
        "away_score": blob.get("visitor_team_score") if status == "final" else None,
        "status": status,
        "play_observed": status in ("live", "final") or _play_observed(blob),
        "evidence_grade": "local_receipt",
        "score_basis": "final_includes_ot",
    }


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _open(root):
    from . import storage

    db = storage.connect(root)
    db.executescript(SCHEMA)
    return db


def observation_clock_problem(row, received):
    """Compare source-reported play with the actual observation, including ET date-only fixtures."""
    if row.get("source") != "balldontlie" or row.get("status") not in ("live", "final"):
        return None
    try:
        at = _utc(received)
        if _date(row["game_date"]) > at.astimezone(EASTERN).date():
            return "play_received_before_eastern_game_date"
        if row.get("kickoff") and at < _utc(row["kickoff"]):
            return "play_received_before_reported_kickoff"
    except (KeyError, ValueError, TypeError, OverflowError):
        return "unverified_observation_clock"
    return None


def _guard_observed_clock(row, received):
    problem = observation_clock_problem(row, received)
    if problem:
        # A positively identified live same-day start is a pregame veto even
        # when play begins a few minutes earlier than the advertised clock.
        early_live = (
            row["status"] == "live"
            and problem == "play_received_before_reported_kickoff"
        )
        return dict(
            row,
            status="live" if early_live else "unknown",
            home_score=None,
            away_score=None,
            final_period=None,
            timing_problem=problem,
        )
    return row


def _append_versions(db, rows, received, available, sha):
    count = 0
    for row in rows:
        row = _guard_observed_clock(row, received)
        body = _encoded(row)
        identity = (row["source"], row["game_id"])
        last = db.execute(
            """SELECT version,received_at,payload FROM nba_game_versions
                             WHERE source=? AND game_id=? ORDER BY version DESC LIMIT 1""",
            identity,
        ).fetchone()
        if row["source"] == "balldontlie":
            previous = db.execute(
                "SELECT received_at,payload_sha,receipt_sha FROM nba_game_last_observation WHERE source=? AND game_id=?",
                identity,
            ).fetchone()
            fingerprint = hashlib.sha256(body.encode()).hexdigest()
            if previous and previous["received_at"] > received:
                continue
            if previous and previous["received_at"] == received:
                if previous["payload_sha"] == fingerprint:
                    continue
                old = json.loads(last["payload"])
                if old.get("conflict") == "simultaneous_provider_observations":
                    continue
                context = min((row, old), key=_encoded).copy()
                context.update(
                    status="unknown",
                    final_period=None,
                    kickoff=None,
                    home_score=None,
                    away_score=None,
                    play_observed=bool(
                        row.get("play_observed") or old.get("play_observed")
                    ),
                    conflict="simultaneous_provider_observations",
                    conflict_receipts=sorted({sha, previous["receipt_sha"]}),
                )
                body = _encoded(context)
            else:
                db.execute(
                    "INSERT OR REPLACE INTO nba_game_last_observation VALUES(?,?,?,?,?)",
                    (*identity, received, fingerprint, sha),
                )
        if last:
            if last["received_at"] > received:
                continue
            if last["payload"] == body:
                # A once-impossible future final can become independently true
                # when exactly the same bytes are re-observed after game day.
                old = json.loads(last["payload"])
                if (
                    _guard_observed_clock(old, last["received_at"]) == row
                    or row["status"] != "final"
                ):
                    continue
        version = last["version"] + 1 if last else 1
        db.execute(
            "INSERT INTO nba_game_versions VALUES(?,?,?,?,?,?,?)",
            (*identity, version, received, available, sha, body),
        )
        count += 1
    return count


def _publish_pending(db, clock):
    # The first transaction has already committed all raw bytes and parsed versions.
    # Readers holding pre-commit SQLite snapshots may have delayed that commit.
    # A later writer can also publish receipts left staged by an interrupted process.
    with db:
        db.execute("BEGIN EXCLUSIVE")
        stamp = _iso(clock())
        for table in ("nba_game_versions", "nba_game_requests"):
            db.execute(
                f"UPDATE {table} SET available_at=MAX(received_at,?) WHERE available_at=?",
                (stamp, PENDING),
            )
        db.execute(
            """UPDATE nba_schedule_completions SET finished=MAX(?,(
                       SELECT began FROM nba_schedule_scans WHERE id=scan_id)) WHERE finished=?""",
            (stamp, PENDING),
        )
    return stamp


def _capture(db, query, raw, rows, code, detail, received, clock):
    """Commit primary evidence, then witness its availability in a second transaction."""
    sha = hashlib.sha256(raw).hexdigest() if raw is not None else None
    packed = zlib.compress(raw) if raw is not None else None
    received = _iso(received)
    with db:
        db.execute("BEGIN IMMEDIATE")
        if sha:
            db.execute(
                "INSERT OR IGNORE INTO nba_game_blobs VALUES(?,?)", (sha, packed)
            )
        count = _append_versions(db, rows, received, PENDING, sha)
        db.execute(
            """INSERT INTO nba_game_requests(query,received_at,available_at,status,sha,details)
                      VALUES(?,?,?,?,?,?)""",
            (_encoded(query), received, PENDING, code, sha, detail),
        )
    _publish_pending(db, clock)
    return count, sha


def import_538(root, path):
    raw = Path(path).read_bytes()
    rows = _archive_rows(raw)
    with closing(_open(root)) as db:
        n, sha = _capture(
            db,
            {"source": "538", "dataset": "nba-elo/nbaallelo.csv"},
            raw,
            rows,
            200,
            "fixed_archive",
            _iso(_now()),
            _now,
        )
    return {
        "state": "ok",
        "source": "538",
        "sha256": sha,
        "games": len(rows),
        "new_versions": n,
        "evidence_grade": "final_archive_date_only",
    }


def _read(root, source, as_of, since, game_id, latest, *, day=None):
    from . import storage

    clauses, args = ["source=?", "available_at!=?"], [source, PENDING]
    for field, op, value in (
        ("available_at", "<=", as_of),
        ("available_at", ">=", since),
    ):
        if value is not None:
            clauses.append(field + op + "?")
            args.append(_iso(value))
    if game_id is not None:
        clauses.append("game_id=?")
        args.append(game_id)
    db = storage.open_existing(root)
    if db is None:
        return []
    with closing(db):
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nba_game_versions'"
        ).fetchone():
            return []
        if day is not None:
            # Select candidate IDs by date before ranking, then enforce the date
            # again on their latest version (a fixture can move across ET days).
            clauses.append(
                "game_id IN (SELECT game_id FROM nba_game_versions WHERE source=? AND json_extract(payload,'$.game_date')=?)"
            )
            args.extend([source, str(day)])
        sql = "SELECT * FROM nba_game_versions WHERE " + " AND ".join(clauses)
        if latest:
            sql = (
                "SELECT * FROM (SELECT g.*,ROW_NUMBER() OVER (PARTITION BY game_id "
                "ORDER BY version DESC) AS ranking FROM ("
                + sql
                + ") g) WHERE ranking=1"
            )
        if day is not None:
            sql += " AND json_extract(payload,'$.game_date')=?"
            args.append(str(day))
        rows = db.execute(sql + " ORDER BY available_at,game_id,version", args)
        result = []
        for r in rows:
            payload = _guard_observed_clock(json.loads(r["payload"]), r["received_at"])
            if (
                source == "balldontlie"
                and payload.get("evidence_grade") == "local_receipt"
                and "stage_basis" not in payload
            ):
                # Older scans published filter guesses before checking other phases.
                # Preserve originals for audits; re-observation supplies a verified version.
                payload.update(stage="unknown", stage_basis="legacy_requires_rescan")
            result.append(
                dict(
                    payload,
                    version=r["version"],
                    received_at=r["received_at"],
                    available_at=r["available_at"],
                    receipt_sha256=r["sha"],
                )
            )
        return result


def game_versions(root, *, source="balldontlie", as_of=None, since=None, game_id=None):
    return _read(root, source, as_of, since, game_id, False)


def games_as_of(root, as_of=None, *, source="balldontlie"):
    return _read(root, source, as_of, None, None, True)


def games_on_day_as_of(root, game_day, cutoff):
    return _read(root, "balldontlie", cutoff, None, None, True, day=_date(game_day))


def _reserve(db, clock):
    now = _utc(clock()).timestamp()
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT next_request FROM nba_bdl_budget WHERE id=1"
        ).fetchone()
        if row and row[0] > now:
            return row[0] - now
        db.execute("INSERT OR REPLACE INTO nba_bdl_budget VALUES(1,?)", (now + 15.01,))
    return 0


def _retry_after(received, header):
    until = _utc(received) + timedelta(seconds=60)
    if header:
        try:
            seconds = int(header)
            until = _utc(received) + timedelta(seconds=max(seconds, 1))
        except (ValueError, OverflowError):
            try:
                until = max(
                    _utc(parsedate_to_datetime(header)),
                    _utc(received) + timedelta(seconds=1),
                )
            except (TypeError, ValueError, OverflowError):
                pass
    return until


def _defer(db, received, header):
    until = _retry_after(received, header)
    with db:
        db.execute(
            "UPDATE nba_bdl_budget SET next_request=MAX(next_request,?) WHERE id=1",
            (until.timestamp(),),
        )
        effective = db.execute(
            "SELECT next_request FROM nba_bdl_budget WHERE id=1"
        ).fetchone()[0]
    return _iso(datetime.fromtimestamp(effective, timezone.utc))


def _http_get(params, key):
    return httpx.get(
        BDL_URL,
        params=params,
        headers={"Authorization": key},
        timeout=12,
        follow_redirects=False,
    )


def _page(response, params):
    obj = response.json()
    if not isinstance(obj, dict) or not isinstance(obj.get("data"), list):
        raise ValueError("invalid page envelope")
    items = obj["data"]
    meta = obj.get("meta")
    bad_cursor = not isinstance(meta, dict) or len(items) > params["per_page"]
    meta = meta if isinstance(meta, dict) else {}
    cursor = meta.get("next_cursor")
    bad_cursor |= len(items) == params["per_page"] and "next_cursor" not in meta
    bad_cursor |= cursor is not None and (
        type(cursor) is not int or cursor <= params.get("cursor", 0) or not items
    )
    rows = []
    unidentified = 0
    for item in items[: params["per_page"]]:
        try:
            game_id = _positive_id(item["id"])
            day = _date(item["date"]).isoformat()
            if not params["start_date"] <= day <= params["end_date"]:
                raise ValueError("outside requested game-day range")
        except (KeyError, TypeError, ValueError):
            unidentified += 1
            continue
        try:
            row = normalize_bdl(item, params["season_type"])
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            row = {
                "source": "balldontlie",
                "game_id": game_id,
                "game_date": day,
                "season": None,
                "home": None,
                "away": None,
                "kickoff": None,
                "stage": "unknown",
                "stage_basis": "unverified",
                "status": "unknown",
                "neutral": None,
                "home_score": None,
                "away_score": None,
                "play_observed": _play_observed(item),
                "quarantine": "invalid_game_metadata",
                "evidence_grade": "local_receipt",
            }
        rows.append(row)
    return rows, cursor, unidentified, bad_cursor


def _withdraw_missing(db, start, end, observed, receipts, began, clock):
    """Only a complete multi-page scan can invalidate an old locally observed listing."""
    with db:
        db.execute("BEGIN IMMEDIATE")
        candidates = db.execute(
            """SELECT v.* FROM nba_game_versions v WHERE v.source='balldontlie'
            AND v.version=(SELECT MAX(x.version) FROM nba_game_versions x
                           WHERE x.source=v.source AND x.game_id=v.game_id) AND v.received_at<=?
            AND NOT EXISTS (SELECT 1 FROM nba_game_last_observation o WHERE o.source=v.source
                            AND o.game_id=v.game_id AND o.received_at>?)
            AND json_extract(v.payload,'$.game_date') BETWEEN ? AND ?""",
            (began, began, start, end),
        )
        missing = []
        for latest in candidates:
            r = json.loads(latest["payload"])
            if r["game_id"] not in observed and r["status"] != "unknown":
                r.update(
                    status="unknown",
                    final_period=None,
                    home_score=None,
                    away_score=None,
                    absence="absent_from_complete_range_scan",
                )
                missing.append(r)
        if not missing:
            return 0
        raw = _encoded(
            {
                "source": "balldontlie",
                "range": [start, end],
                "page_sha256": receipts,
                "absent_ids": sorted(r["game_id"] for r in missing),
            }
        ).encode()
        sha = hashlib.sha256(raw).hexdigest()
        at = _iso(clock())
        db.execute(
            "INSERT OR IGNORE INTO nba_game_blobs VALUES(?,?)",
            (sha, zlib.compress(raw)),
        )
        db.execute(
            """INSERT INTO nba_game_requests(query,received_at,available_at,status,sha,details)
                      VALUES(?,?,?,?,?,?)""",
            (
                _encoded({"source": "balldontlie", "range": [start, end]}),
                at,
                PENDING,
                200,
                sha,
                "absent_from_complete_range_scan",
            ),
        )
        changed = _append_versions(db, missing, at, PENDING, sha)
    _publish_pending(db, clock)
    return changed


def _key():
    return os.environ.get("BALLDONTLIE_API_KEY") or os.environ.get(
        "PM861_BALLDONTLIE_KEY"
    )


def sync_bdl(root, start, end, *, fetch=None, clock=None, sleep=None):
    """Collect regular-season and Cup games using a shared 4 requests/minute budget."""
    start, end = _date(start).isoformat(), _date(end).isoformat()
    if start > end:
        raise ValueError("start date must precede end date")
    result = {
        "state": "missing_key",
        "complete": False,
        "partial": False,
        "pages": 0,
        "received_games": 0,
        "quality": {
            "quarantined_rows": 0,
            "unidentified_rows": 0,
            "pagination_errors": 0,
            "overlapping_ids": 0,
            "phase_mismatches": 0,
            "unknown_stages": 0,
            "scheduled_without_kickoff": 0,
            "unknown_statuses": 0,
            "unrecognized_statuses": 0,
            "phase_rows": {},
            "stage_counts": {},
            "status_counts": {},
            "game_days": [],
            "neutral_known": 0,
            "neutral_unknown": 0,
            "cup_knockout": 0,
        },
        "new_versions": 0,
        "start": start,
        "end": end,
    }
    key = _key()
    if not key:
        return result
    clock, fetch, sleep = clock or _now, fetch or _http_get, sleep or time.sleep
    with closing(_open(root)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            began = _iso(clock())
            scan_id = db.execute(
                "INSERT INTO nba_schedule_scans(start,end,began) VALUES(?,?,?)",
                (start, end, began),
            ).lastrowid
        result.update(state="unavailable", scan_id=scan_id, began=began)
        try:
            return _sync_range(db, result, key, fetch, clock, sleep, began)
        finally:
            with db:
                db.execute("BEGIN IMMEDIATE")
                requests = [
                    dict(r)
                    for r in db.execute(
                        """SELECT id,sha,received_at,status,details,query FROM nba_game_requests
                       WHERE json_extract(query,'$.scan_id')=? ORDER BY id""",
                        (scan_id,),
                    )
                ]
                manifest = _encoded(
                    {
                        "requests": requests,
                        "quality": result["quality"],
                        "start": start,
                        "end": end,
                        "scan_id": scan_id,
                        "complete": result["complete"],
                    }
                )
                evidence_sha = hashlib.sha256(manifest.encode()).hexdigest()
                db.execute(
                    "INSERT INTO nba_schedule_evidence VALUES(?,?,?)",
                    (scan_id, manifest, evidence_sha),
                )
                db.execute(
                    "INSERT INTO nba_schedule_completions VALUES(?,?,?,?)",
                    (scan_id, PENDING, result["state"], int(result["complete"])),
                )
            _publish_pending(db, clock)
            result.update(
                finished=db.execute(
                    "SELECT finished FROM nba_schedule_completions WHERE scan_id=?",
                    (scan_id,),
                ).fetchone()[0],
                evidence_sha256=evidence_sha,
            )


def _sync_range(db, result, key, fetch, clock, sleep, began):
    start, end = result["start"], result["end"]
    observations, receipts = {}, []
    phases = ["regular", "preseason"]
    if end >= "2023-11-03":
        phases.append("ist")

    def interrupted():
        if observations:
            result["new_versions"] = _publish_checked(
                db, observations, result["quality"], clock, complete_pages=False
            )
        return result

    for phase in phases:
        params = {
            "start_date": start,
            "end_date": end,
            "per_page": 100,
            "season_type": phase,
        }
        phase_ids = set()
        while True:
            wait = _reserve(db, clock)
            if 0 < wait <= 16:
                sleep(wait + 0.01)
                wait = _reserve(db, clock)
            if wait:
                result.update(
                    state="rate_limited",
                    retry_at=_iso(_utc(clock()) + timedelta(seconds=wait)),
                )
                return interrupted()
            query = {
                "source": "balldontlie",
                "endpoint": BDL_URL,
                "scan_id": result["scan_id"],
                **params,
            }
            try:
                response = fetch(dict(params), key)
            except (httpx.HTTPError, OSError):
                _capture(
                    db, query, None, [], 0, "transport_error", _iso(clock()), clock
                )
                result.update(state="unavailable")
                return interrupted()
            at = _iso(clock())
            code = response.status_code
            if code != 200:
                _capture(db, query, None, [], code, "http_error", at, clock)
                result.update(state="unavailable", http_status=code)
                if code == 429:
                    result.update(
                        state="rate_limited",
                        retry_at=_defer(db, at, response.headers.get("Retry-After")),
                    )
                return interrupted()
            # Never archive a body reflecting the credential, including invalid responses.
            if key.encode() in response.content:
                _capture(db, query, None, [], code, "credential_reflection", at, clock)
                result.update(state="invalid_response")
                return interrupted()
            try:
                rows, cursor, unidentified, bad_cursor = _page(response, params)
            except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
                _capture(db, query, None, [], code, "invalid_response", at, clock)
                result.update(state="invalid_response")
                return interrupted()
            ids = {r["game_id"] for r in rows}
            bad_cursor |= len(ids) < len(rows) or bool(ids & phase_ids)
            quality = result["quality"]
            quality["phase_rows"][phase] = quality["phase_rows"].get(phase, 0) + len(
                rows
            )
            quality["unidentified_rows"] += unidentified
            quality["pagination_errors"] += int(bad_cursor)
            # Raw pages are durable immediately. Never publish a guessed phase
            # during pagination while the negative-control query is still outstanding.
            _, sha = _capture(
                db, query, response.content, [], code, "game_page", at, clock
            )
            for row in rows:
                observations.setdefault(row["game_id"], []).append(
                    (phase, row, at, sha)
                )
            receipts.append(sha)
            phase_ids.update(ids)
            result.update(
                partial=True,
                pages=result["pages"] + 1,
                received_games=len(observations),
            )
            if bad_cursor:
                result.update(state="invalid_response")
                return interrupted()
            if cursor is None:
                break
            params["cursor"] = cursor
    quality = result["quality"]
    result["new_versions"] = _publish_checked(db, observations, quality, clock)
    if quality["unidentified_rows"]:
        result.update(state="invalid_response")
        return result
    if (
        quality["quarantined_rows"]
        or quality["overlapping_ids"]
        or quality["phase_mismatches"]
        or quality["unknown_stages"]
        or quality["scheduled_without_kickoff"]
        or quality["unrecognized_statuses"]
        or quality["unknown_statuses"] == len(observations) > 0
    ):
        result.update(state="source_quality")
        return result
    result["new_versions"] += _withdraw_missing(
        db, start, end, set(observations), receipts, began, clock
    )
    result.update(state="ok", complete=True, partial=False)
    return result


def _phase_mismatch(phase, row):
    supplied = row.get("provider_season_type")
    if supplied and supplied != phase:
        return True
    if row.get("quarantine"):
        return False
    stage = row["stage"]
    return (
        stage in ("postseason", "playin")
        or (stage == "preseason" and phase != "preseason")
        or (phase == "preseason" and stage != "preseason")
    )


def _publish_checked(db, observations, quality, clock, *, complete_pages=True):
    overlap = {
        gid for gid, entries in observations.items() if len({e[0] for e in entries}) > 1
    }
    mismatch = {
        gid
        for gid, entries in observations.items()
        if any(_phase_mismatch(phase, row) for phase, row, _, _ in entries)
    }
    quality.update(overlapping_ids=len(overlap), phase_mismatches=len(mismatch))
    quality["overlap_examples"] = sorted(overlap)[:10]
    days, versions = set(), 0
    with db:
        db.execute("BEGIN IMMEDIATE")
        for gid, entries in observations.items():
            _, row, at, sha = entries[-1]
            row = _guard_observed_clock(row, at)
            if (
                gid in overlap
                or gid in mismatch
                or ((overlap or mismatch) and row.get("stage_basis") == "query_filter")
            ):
                row = dict(
                    row,
                    stage="unknown",
                    stage_basis="filter_conflict",
                    quarantine="season_type_filter_conflict",
                )
            elif not complete_pages:
                # A cancellation, malformed replacement or directly classified
                # non-regular game still invalidates an old positive observation.
                if (
                    row["status"] in ("scheduled", "final", "live")
                    and row.get("stage_basis") != "provider_fields"
                    and row.get("kickoff")
                ):
                    continue
                if (
                    row.get("stage_basis") != "provider_fields"
                    or row["stage"] == "regular"
                ):
                    row = dict(
                        row, stage="unknown", stage_basis="incomplete_phase_queries"
                    )
            elif row.get("stage_basis") == "query_filter":
                row = dict(row, stage_basis="disjoint_phase_queries")
            missing = row["status"] == "scheduled" and not row.get("kickoff")
            if missing:
                row = dict(row, timing_problem="scheduled_without_kickoff")
            quality["scheduled_without_kickoff"] += int(missing)
            quality["unknown_stages"] += int(
                row["stage"] == "unknown"
                and row.get("quarantine") != "invalid_game_metadata"
            )
            quality["unknown_statuses"] += int(row["status"] == "unknown")
            quality["unrecognized_statuses"] += int(
                row["status"] == "unknown"
                and row.get("quarantine") != "invalid_game_metadata"
            )
            quality["quarantined_rows"] += int(
                bool(
                    missing
                    or row.get("quarantine")
                    or row["status"] == "unknown"
                    or row["stage"] == "unknown"
                )
            )
            for field in ("stage", "status"):
                counts = quality[field + "_counts"]
                counts[row[field]] = counts.get(row[field], 0) + 1
            if row["stage"] == "regular" or row["stage"] == "cup_final":
                days.add(row["game_date"])
            quality[
                "neutral_known" if row.get("neutral") is not None else "neutral_unknown"
            ] += 1
            cup = row.get("cup_stage") or ""
            quality["cup_knockout"] += int(
                row["stage"] == "cup_final"
                or "semifinal" in cup
                or "quarterfinal" in cup
            )
            versions += _append_versions(db, [row], at, PENDING, sha)
    if versions:
        _publish_pending(db, clock)
    quality["game_days"] = sorted(days)
    return versions


def schedule_proof(root, game_day, as_of):
    """Latest range scan started by the cutoff, including incomplete and failed attempts."""
    from . import storage

    day = _date(game_day).isoformat()
    cutoff = _iso(as_of)
    db = storage.open_existing(root)
    if db is None:
        return None
    with closing(db):
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='nba_schedule_scans'"
        ).fetchone():
            return None
        r = db.execute(
            """SELECT s.id AS scan_id,s.start,s.end,s.began,c.finished,c.state,c.complete,e.sha AS evidence_sha256,
                          json_extract(e.manifest,'$.quality') AS quality
                          FROM nba_schedule_scans s LEFT JOIN nba_schedule_completions c ON c.scan_id=s.id
                          LEFT JOIN nba_schedule_evidence e ON e.scan_id=s.id
                          WHERE s.start<=? AND s.end>=? AND s.began<=?
                          ORDER BY s.began DESC,s.id DESC LIMIT 1""",
            (day, day, cutoff),
        ).fetchone()
        if not r:
            return None
        proof = dict(r)
        if r["finished"] is None or r["finished"] > cutoff:
            proof.update(
                finished=None,
                state="running",
                complete=False,
                evidence_sha256=None,
                quality=None,
            )
        else:
            proof["complete"] = bool(r["complete"])
            quality = json.loads(r["quality"]) if r["quality"] else None
            proof["quality"] = quality
            if proof["complete"]:
                if not (quality or {}).get("phase_rows"):
                    proof.update(state="legacy_unverified", complete=False)
                elif any(
                    quality.get(k, 0)
                    for k in (
                        "quarantined_rows",
                        "unidentified_rows",
                        "pagination_errors",
                        "overlapping_ids",
                        "phase_mismatches",
                    )
                ):
                    proof.update(state="source_quality", complete=False)
        return proof


def capabilities(root):
    from .data_capabilities import capabilities as report

    return report(root)


def register(commands):
    from .data_capabilities import PROBES, probe

    actions = commands.add_parser(
        "data", help="NBA 赛程、实收和免费来源核验"
    ).add_subparsers(dest="nba_data_command", required=True)
    p = actions.add_parser(
        "sync", help="按日期同步常规赛和 NBA Cup 免费赛程、比分及更正"
    )
    p.add_argument("--start", required=True, type=_date, metavar="YYYY-MM-DD")
    p.add_argument("--end", required=True, type=_date, metavar="YYYY-MM-DD")
    p.set_defaults(
        func=lambda a: sync_bdl(a.data_dir, a.start, a.end),
        result_exit_code=lambda r: int(not r["complete"]),
    )
    p = actions.add_parser("import-538", help="校验并导入固定历史比分档案用于技术回放")
    p.add_argument("path", type=Path)
    p.set_defaults(func=lambda a: import_538(a.data_dir, a.path))
    p = actions.add_parser("capabilities", help="读取本机的免费来源验证表")
    p.set_defaults(func=lambda a: capabilities(a.data_dir))
    p = actions.add_parser("probe", help="按单来源免费预算试取伤停字段和回执摘要")
    p.add_argument("--source", required=True, choices=sorted(PROBES))
    p.set_defaults(
        func=lambda a: probe(a.data_dir, a.source),
        result_exit_code=lambda r: int(not r.get("authenticated")),
    )
    from . import source_acceptance

    source_acceptance.register(actions)
