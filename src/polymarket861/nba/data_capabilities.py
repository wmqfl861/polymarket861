"""Free-account admission evidence. Player data is never promoted by a probe."""

import hashlib
import json
import os
from contextlib import closing
from datetime import datetime, time, timedelta

import httpx

from . import data, storage

PROBES = {
    "bigballs": ("BIGBALLS_API_KEY", "https://api.bigballsdata.com/v1/injuries", 900),
    "dunkapi": ("DUNKAPI_API_KEY", "https://mcp.dunkapi.com/v1/get_injuries", 43200),
}
RIGHTS = {
    "balldontlie": (
        "https://www.balldontlie.io/terms.html",
        "game schedules and finals",
        "direct provider §6 internal archive, model training and predictions; §11 third-party rights reserved",
    ),
    "bigballs": (
        "https://bigballsdata.com/legal/terms",
        "current injury list; first page only",
        "internal training and retention; raw redistribution restricted; injury upstream unverified",
    ),
    "dunkapi": (
        "https://dunkapi.com/terms",
        "daily NBA injury snapshot",
        "internal storage and training; external services require an active account; upstream rights not granted",
    ),
}
_SCHEMA = """CREATE TABLE IF NOT EXISTS nba_free_probes (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, requested_at TEXT NOT NULL, next_at TEXT NOT NULL,
    receipt TEXT NOT NULL DEFAULT '{}'
)"""
# Restrict field inventory to known data names. Never store arbitrary keys or values.
_FIELDS = {
    "player",
    "player_id",
    "player_name",
    "name",
    "team",
    "team_id",
    "team_name",
    "id",
    "abbreviation",
    "status",
    "injury",
    "injury_type",
    "reason",
    "return_date",
    "expected_return",
    "expected_return_date",
    "reported_at",
    "published_at",
    "updated_at",
    "source",
    "date",
    "note",
    "description",
    "league",
    "last_updated",
    "injury_status",
}


def _probe_http(source, key):
    url = PROBES[source][1]
    options = {"headers": {"x-api-key": key}, "timeout": 12, "follow_redirects": False}
    if source == "bigballs":
        return httpx.get(
            url,
            params={"sport": "basketball", "league": "nba", "limit": 200},
            **options,
        )
    return httpx.post(url, json={"league": "nba"}, **options)


DATE_FIELDS = ("date", "as_of_date", "snapshot_date")
CLOCK_FIELDS = ("published_at", "reported_at", "updated_at", "last_updated")
TIMING_FIELDS = sorted(
    {
        prefix + field
        for prefix in ("", "data.", "meta.", "data.meta.")
        for field in (*DATE_FIELDS, *CLOCK_FIELDS)
    }
    | {
        prefix + field
        for prefix in ("injuries[].", "data.injuries[].")
        for field in CLOCK_FIELDS
    }
)


class ScopeMismatch(ValueError):
    """The authenticated response explicitly identifies another sport or league."""


def _nba_scope(payloads):
    explicit = False
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key, allowed in (
            ("league", ("nba", "national basketball association")),
            ("sport", ("basketball",)),
        ):
            indicia = [
                payload[name]
                for name in (key, key + "_code", key + "_name", key + "_slug")
                if payload.get(name) is not None
            ]
            for value in indicia:
                if isinstance(value, dict):
                    names = [
                        value[k]
                        for k in ("name", "code", "abbreviation", "slug")
                        if value.get(k)
                    ]
                else:
                    names = [value]
                if not names or any(
                    not isinstance(n, str) or n.strip().casefold() not in allowed
                    for n in names
                ):
                    raise ScopeMismatch(
                        "response scope is outside NBA basketball or unresolved"
                    )
                explicit |= key == "league"
    return "response_explicit_nba" if explicit else "nba_scoped_request"


def _sample(source, response):
    envelope = response.json()
    obj = envelope["data"] if source == "bigballs" else envelope
    rows = obj["injuries"]
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError("invalid injury sample")
    fields = sorted({name for row in rows for name in row} & _FIELDS)
    # Keep calendar snapshots and upstream clocks distinct from the HTTP receipt.
    # Only names, dates and aggregate clocks are retained; no player text or IDs.
    locations = {"": envelope}
    if source == "bigballs":
        locations["data."] = obj
    for prefix, value in list(locations.items()):
        if isinstance(value.get("meta"), dict):
            locations[prefix + "meta."] = value["meta"]
    scope = _nba_scope([*locations.values(), *rows, *(r.get("team") for r in rows)])
    source_dates, clocks = {}, {}
    for prefix, container in locations.items():
        for name in DATE_FIELDS:
            try:
                source_dates[prefix + name] = data._date(
                    container.get(name)
                ).isoformat()
            except ValueError:
                pass
        for name in CLOCK_FIELDS:
            try:
                stamp = data._iso(container.get(name))
                clocks[prefix + name] = {"count": 1, "first": stamp, "last": stamp}
            except (ValueError, TypeError, OverflowError):
                pass
    status_rows = [
        row
        for row in rows
        if (row.get("status") or row.get("injury_status"))
        and (row.get("player_id") or row.get("player_name") or row.get("player"))
    ]
    prefix = "data.injuries[]." if source == "bigballs" else "injuries[]."
    for name in CLOCK_FIELDS:
        stamps = []
        for row in status_rows:
            try:
                stamps.append(data._iso(row[name]))
            except (KeyError, ValueError, TypeError, OverflowError):
                pass
        if stamps:
            clocks[prefix + name] = {
                "count": len(stamps),
                "first": min(stamps),
                "last": max(stamps),
            }
    return {
        "state": "sample_received" if rows else "empty_sample",
        "sample_rows": len(rows),
        "player_status_rows": len(status_rows),
        "source_dates": source_dates,
        "league_scope_basis": scope,
        "upstream_clocks": clocks,
        "observed_field_names": fields,
        "sample_sha256": hashlib.sha256(
            data._encoded(sorted(data._encoded(r) for r in rows)).encode()
        ).hexdigest(),
        "response_sha256": hashlib.sha256(response.content).hexdigest(),
        "upstream_published_at_semantics_verified": False,
        "sample_hash_policy": "sorted_rows_v2",
        "sample_contains_all_nba_players": False,
        "history_depth_verified": False,
    }


def _witness(db, clock):
    # The response transaction has committed. A restart witnesses abandoned
    # pending receipts at recovery time, never at an earlier model cutoff.
    with db:
        db.execute("BEGIN EXCLUSIVE")
        stamp = data._iso(clock())
        pending = db.execute(
            """SELECT id,receipt FROM nba_free_probes
               WHERE json_extract(receipt,'$.witness_pending')=1"""
        ).fetchall()
        for item in pending:
            receipt = json.loads(item["receipt"])
            receipt.pop("witness_pending")
            receipt["ingested_at"] = max(stamp, receipt["received_at"])
            receipt["receipt_to_ingest_ms"] = round(
                (
                    data._utc(receipt["ingested_at"])
                    - data._utc(receipt["received_at"])
                ).total_seconds()
                * 1000
            )
            db.execute(
                "UPDATE nba_free_probes SET receipt=? WHERE id=?",
                (data._encoded(receipt), item["id"]),
            )


def probe(root, source, *, fetch=None, clock=None, target=None):
    env, _, interval = PROBES[source]
    key = os.environ.get(env)
    if not key:
        return {"source": source, "state": "missing_key", "authenticated": False}
    clock, fetch = clock or data._now, fetch or _probe_http
    with closing(storage.connect(root)) as db:
        db.execute(_SCHEMA)
        db.execute("""CREATE TABLE IF NOT EXISTS nba_source_slots (
            source TEXT NOT NULL, game_day TEXT NOT NULL, slot TEXT NOT NULL,
            probe_id INTEGER NOT NULL REFERENCES nba_free_probes(id), PRIMARY KEY(source,game_day,slot))""")
        _witness(db, clock)
        with db:
            db.execute("BEGIN IMMEDIATE")
            started = data._utc(clock())
            at = data._iso(started)
            day = started.astimezone(data.EASTERN).date()
            start_day = datetime.combine(day, time.min, data.EASTERN)
            next_day = datetime.combine(day + timedelta(days=1), time.min, data.EASTERN)
            if target:
                if (
                    str(day) != target["game_day"]
                    or not target["opens"] <= at <= target["closes"]
                ):
                    return {"source": source, "state": "outside_window"}
                previous = db.execute(
                    "SELECT probe_id FROM nba_source_slots WHERE source=? AND game_day=? AND slot=?",
                    (source, str(day), target["slot"]),
                ).fetchone()
                if previous:
                    return {
                        "source": source,
                        "state": "already_attempted",
                        "probe_id": previous[0],
                    }
            last = db.execute(
                "SELECT next_at FROM nba_free_probes WHERE source=? ORDER BY id DESC LIMIT 1",
                (source,),
            ).fetchone()
            if last and last[0] > at:
                return {"source": source, "state": "budget_wait", "retry_at": last[0]}
            used = db.execute(
                "SELECT COUNT(*) FROM nba_free_probes WHERE source=? AND requested_at>=? AND requested_at<?",
                (source, data._iso(start_day), data._iso(next_day)),
            ).fetchone()[0]
            if used >= (3 if source == "bigballs" else 1):
                return {
                    "source": source,
                    "state": "budget_wait",
                    "retry_at": data._iso(next_day),
                }
            hold = (
                data._iso(started + timedelta(seconds=interval))
                if source == "bigballs"
                else data._iso(next_day)
            )
            probe_id = db.execute(
                "INSERT INTO nba_free_probes(source,requested_at,next_at) VALUES(?,?,?)",
                (source, at, hold),
            ).lastrowid
            if target:
                db.execute(
                    "INSERT INTO nba_source_slots VALUES(?,?,?,?)",
                    (source, str(day), target["slot"], probe_id),
                )
            summary = {
                "source": source,
                "probe_id": probe_id,
                "requested_at": at,
                "target": target,
                "authenticated": False,
                "player_model_eligible": False,
            }
            db.execute(
                "UPDATE nba_free_probes SET receipt=? WHERE id=?",
                (
                    data._encoded(dict(summary, state="awaiting_durable_receipt")),
                    probe_id,
                ),
            )
        received = None
        try:
            response = fetch(source, key)
            received = data._utc(clock())
            code = response.status_code
            summary["http_status"] = code
            if code == 200 and key.encode() not in response.content:
                summary.update(_sample(source, response), authenticated=True)
            elif code == 200:
                summary["state"] = "invalid_response"
            else:
                summary["state"] = "quota_exhausted" if code == 402 else "http_error"
                if code == 402:
                    hold = data._iso(received + timedelta(days=31))
                elif code == 429:
                    cooldown = data._retry_after(
                        received, response.headers.get("Retry-After")
                    )
                    hold = max(hold, data._iso(cooldown))
        except (httpx.HTTPError, OSError):
            received = data._utc(clock())
            summary["state"] = "transport_error"
        except ScopeMismatch:
            summary.update(
                state="scope_mismatch",
                response_sha256=hashlib.sha256(response.content).hexdigest(),
            )
        except (TypeError, ValueError, KeyError, AttributeError):
            summary["state"] = "invalid_response"
        received = received or data._utc(clock())
        with db:
            db.execute("BEGIN IMMEDIATE")
            summary.update(
                received_at=data._iso(received), witness_pending=True, retry_at=hold
            )
            db.execute(
                "UPDATE nba_free_probes SET next_at=?,receipt=? WHERE id=?",
                (hold, data._encoded(summary), probe_id),
            )
        _witness(db, clock)
        return json.loads(
            db.execute(
                "SELECT receipt FROM nba_free_probes WHERE id=?", (probe_id,)
            ).fetchone()[0]
        )


def capabilities(root):
    db = storage.open_existing(root)
    try:
        tables = (
            {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if db
            else set()
        )
        result = {}
        for source, (terms, scope, rights) in RIGHTS.items():
            key = (
                data._key()
                if source == "balldontlie"
                else os.environ.get(PROBES[source][0])
            )
            item = {
                "key_configured": bool(key),
                "sample_scope": scope,
                "terms": terms,
                "rights": rights,
                "upstream_identity": "unverified",
                "t_minus_60m_same_day_status_verified": False,
                "player_model_eligible": False,
                "authenticated_business_receipts": 0,
                "observed_injury_fields": [],
                "upstream_published_at_semantics_verified": False,
                "receipt_to_ingest_ms": None,
                "distinct_sample_hashes_same_utc_day": None,
                "state": "unverified" if key else "missing_key",
            }
            if source == "balldontlie":
                item["free_player_injury_endpoint"] = False
                if "nba_game_requests" in tables:
                    where = "details='game_page' AND status=200 AND available_at!='unpublished'"
                    item["authenticated_business_receipts"] = db.execute(
                        "SELECT COUNT(*) FROM nba_game_requests WHERE " + where
                    ).fetchone()[0]
                    last = db.execute(
                        "SELECT sha, received_at, available_at FROM nba_game_requests WHERE "
                        + where
                        + " ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if last:
                        item["latest_receipt"] = dict(last)
                        item["state"] = "game_page_received"
                        item["receipt_to_ingest_ms"] = round(
                            (
                                data._utc(last["available_at"])
                                - data._utc(last["received_at"])
                            ).total_seconds()
                            * 1000
                        )
            elif "nba_free_probes" in tables:
                records = [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT receipt FROM nba_free_probes WHERE source=? AND receipt!='{}' ORDER BY id",
                        (source,),
                    )
                ]
                accepted = [
                    r
                    for r in records
                    if r.get("authenticated") and r.get("ingested_at")
                ]
                item["authenticated_business_receipts"] = len(accepted)
                if records:
                    item.update(latest_attempt=records[-1], state=records[-1]["state"])
                if accepted:
                    last = accepted[-1]
                    day = last["received_at"][:10]
                    item.update(
                        latest_receipt=last,
                        latest_observation_utc_day=day,
                        observed_injury_fields=last["observed_field_names"],
                        receipt_to_ingest_ms=last["receipt_to_ingest_ms"],
                        distinct_sample_hashes_same_utc_day=len(
                            {
                                r["sample_sha256"]
                                for r in accepted
                                if r["received_at"].startswith(day)
                            }
                        ),
                    )
            result[source] = item
    finally:
        if db:
            db.close()
    from .source_acceptance import admission_summary

    admission = admission_summary(root)
    for source, evidence in admission["sources"].items():
        result[source].update(
            admission=evidence,
            t_minus_60m_same_day_status_verified=evidence["seven_day_same_day_status"],
        )
        if evidence["terms_review"]:
            result[source]["upstream_identity"] = evidence["terms_review"]["upstream"]
            result[source]["upstream_published_at_semantics_verified"] = bool(
                (evidence["terms_review"]["timing_field"] or "").endswith(
                    "published_at"
                )
                and any(
                    evidence["terms_review"]["timing_field"]
                    in (r.get("upstream_clocks") or {})
                    for day in evidence["days"]
                    for r in day["samples"]
                )
            )
    return {
        "sources": result,
        "admission": admission,
        "player_model_gate": "closed",
        "eligible_player_sources": admission["eligible_player_sources"],
        "generated_at": data._iso(data._now()),
    }
