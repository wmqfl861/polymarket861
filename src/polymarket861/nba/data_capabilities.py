"""Free-account admission evidence. Player data is never promoted by a probe."""

import hashlib
import json
import os
from contextlib import closing
from datetime import timedelta

import httpx

from . import data, storage

PROBES = {
    "bigballs": ("BIGBALLS_API_KEY", "https://api.bigballsdata.com/v1/injuries", 3600),
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


def _sample(source, response):
    obj = response.json()
    if source == "bigballs":
        obj = obj["data"]
    rows = obj["injuries"]
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError("invalid injury sample")
    fields = sorted({name for row in rows for name in row} & _FIELDS)
    return {
        "state": "sample_received" if rows else "empty_sample",
        "sample_rows": len(rows),
        "observed_field_names": fields,
        "sample_sha256": hashlib.sha256(data._encoded(rows).encode()).hexdigest(),
        "response_sha256": hashlib.sha256(response.content).hexdigest(),
        "upstream_published_at_semantics_verified": False,
        "sample_contains_all_nba_players": False,
        "history_depth_verified": False,
    }


def probe(root, source, *, fetch=None, clock=None):
    env, _, interval = PROBES[source]
    key = os.environ.get(env)
    if not key:
        return {"source": source, "state": "missing_key", "authenticated": False}
    clock, fetch = clock or data._now, fetch or _probe_http
    with closing(storage.connect(root)) as db:
        db.execute(_SCHEMA)
        with db:
            db.execute("BEGIN IMMEDIATE")
            started = data._utc(clock())
            at = data._iso(started)
            last = db.execute(
                "SELECT next_at FROM nba_free_probes WHERE source=? ORDER BY id DESC LIMIT 1",
                (source,),
            ).fetchone()
            if last and last[0] > at:
                return {"source": source, "state": "budget_wait", "retry_at": last[0]}
            hold = data._iso(started + timedelta(seconds=interval))
            probe_id = db.execute(
                "INSERT INTO nba_free_probes(source,requested_at,next_at) VALUES(?,?,?)",
                (source, at, hold),
            ).lastrowid
        summary = {
            "source": source,
            "authenticated": False,
            "player_model_eligible": False,
        }
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
        except (TypeError, ValueError, KeyError, AttributeError):
            summary["state"] = "invalid_response"
        with db:
            db.execute("BEGIN IMMEDIATE")
            ingested = max(data._utc(clock()), received)
            summary.update(
                received_at=data._iso(received),
                ingested_at=data._iso(ingested),
                receipt_to_ingest_ms=round(
                    (ingested - received).total_seconds() * 1000
                ),
                retry_at=hold,
            )
            db.execute(
                "UPDATE nba_free_probes SET next_at=?,receipt=? WHERE id=?",
                (hold, data._encoded(summary), probe_id),
            )
    return summary


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
                accepted = [r for r in records if r.get("authenticated")]
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
    return {
        "sources": result,
        "player_model_gate": "closed",
        "eligible_player_sources": [],
        "generated_at": data._iso(data._now()),
    }
