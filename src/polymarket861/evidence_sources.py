"""Field-level permissions, immutable source versions and evidence storage."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .store import connect, now_utc, save_report


FIELDS = {
    "lineup", "availability", "injury", "suspension", "card", "form",
    "news", "weather", "travel", "schedule", "manager", "venue", "off_field",
    "xg", "minutes", "team_stats", "player_stats", "tactics", "impact_validation",
}
USES = {"trial", "prediction", "training"}
METHODS = {"manual", "auto"}


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def required(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value.strip()


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("timestamps must include a UTC offset")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def name_key(value):
    return " ".join(value.split()).casefold()


_DDL = """
CREATE TABLE IF NOT EXISTS ev_source_versions (
    source_id TEXT NOT NULL, effective_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
    manifest TEXT NOT NULL, PRIMARY KEY(source_id, effective_at)
);
CREATE TABLE IF NOT EXISTS ev_facts (
    evidence_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, fixture_id TEXT NOT NULL,
    team_key TEXT NOT NULL, field TEXT NOT NULL, subject TEXT NOT NULL,
    origin_id TEXT NOT NULL, claim_key TEXT NOT NULL, revision INTEGER NOT NULL,
    published_at TEXT NOT NULL, received_at TEXT NOT NULL,
    valid_from TEXT NOT NULL, valid_until TEXT, document TEXT NOT NULL,
    UNIQUE(source_id,fixture_id,team_key,field,subject,origin_id,claim_key,revision)
);
CREATE INDEX IF NOT EXISTS ev_cutoff ON ev_facts(fixture_id,field,received_at);
CREATE INDEX IF NOT EXISTS ev_source_facts ON ev_facts(source_id,field);
CREATE TABLE IF NOT EXISTS ev_source_checks (
    source_id TEXT NOT NULL, field TEXT NOT NULL, checked_at TEXT NOT NULL,
    document TEXT NOT NULL, PRIMARY KEY(source_id,field,checked_at)
);
CREATE TABLE IF NOT EXISTS ev_artifacts (
    kind TEXT NOT NULL, artifact_key TEXT NOT NULL, parents TEXT NOT NULL,
    PRIMARY KEY(kind,artifact_key)
);
CREATE TABLE IF NOT EXISTS ev_edges (
    parent_kind TEXT NOT NULL, parent_key TEXT NOT NULL,
    child_kind TEXT NOT NULL, child_key TEXT NOT NULL,
    PRIMARY KEY(parent_kind,parent_key,child_kind,child_key)
);
CREATE TABLE IF NOT EXISTS ev_incident_versions (
    incident_id TEXT NOT NULL, updated_at TEXT NOT NULL, document TEXT NOT NULL,
    PRIMARY KEY(incident_id,updated_at)
);
CREATE TABLE IF NOT EXISTS ev_quarantine (
    incident_id TEXT NOT NULL, evidence_id TEXT NOT NULL, since TEXT NOT NULL,
    PRIMARY KEY(incident_id,evidence_id)
);
CREATE TABLE IF NOT EXISTS ev_guards (
    source_id TEXT NOT NULL, field TEXT NOT NULL, incident_id TEXT NOT NULL,
    document TEXT NOT NULL, PRIMARY KEY(source_id,field,incident_id)
);
"""


@contextmanager
def db(data_dir, *, write=False):
    conn = connect(data_dir)
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(_DDL)
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.commit()
    except BaseException:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()


def source_at(conn, source_id, cutoff=None, *, recorded_by=None):
    query = "SELECT manifest FROM ev_source_versions WHERE source_id=?"
    params = [source_id]
    if cutoff is not None:
        query += " AND effective_at<=?"
        params.append(cutoff)
    if recorded_by is not None:
        query += " AND recorded_at<=?"
        params.append(recorded_by)
    row = conn.execute(query + " ORDER BY effective_at DESC LIMIT 1", params).fetchone()
    return json.loads(row[0]) if row else None


def promoted_field(conn, source, field, at):
    """A dated field promotion does not authorize other fields or a prohibited use."""
    if not source or source["status"] != "candidate" or not conn or at is None:
        return False
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='research_events'").fetchone():
        return False
    from .jobs_research import _pointer
    primary, _ = _pointer(conn, "source", field, at)
    return primary == source["source_id"]


def granted(source, field, method, *, conn=None, at=None):
    if not source or source["status"] == "disabled" or not source["free"]:
        return set()
    rule = source["fields"].get(field, {})
    if method not in rule.get("methods", []):
        return set()
    uses = set(rule["uses"])
    return uses if source["status"] == "active" or promoted_field(conn, source, field, at) else uses & {"trial"}


def register_source(data_dir, manifest, *, only_new=False):
    source_id = required(manifest.get("source_id"), "source_id")
    status = manifest.get("status", "candidate")
    if status not in {"active", "candidate", "disabled"}:
        raise ValueError("source status must be candidate, active or disabled")
    if not isinstance(manifest.get("free"), bool) or (status != "disabled" and not manifest["free"]):
        raise ValueError("an enabled source must be explicitly free")
    terms = required(manifest.get("terms_locator"), "terms_locator")
    upstream = required(manifest.get("upstream"), "upstream")
    raw_fields = manifest.get("fields")
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise ValueError("source needs at least one field and its permissions")
    fields = {}
    for field, rule in raw_fields.items():
        if field not in FIELDS or not isinstance(rule, dict):
            raise ValueError(f"unrecognised field or invalid rule: {field}")
        uses, methods = rule.get("uses", []), rule.get("methods", [])
        if not isinstance(uses, list) or not set(uses) <= USES:
            raise ValueError(f"invalid uses for {field}")
        if not isinstance(methods, list) or not methods or not set(methods) <= METHODS:
            raise ValueError(f"invalid collection methods for {field}")
        for flag in ("allow_impacts", "allow_match_impacts"):
            if flag in rule and not isinstance(rule[flag], bool):
                raise ValueError(f"{flag} must be a boolean")
        fields[field] = dict(rule, uses=sorted(set(uses)), methods=sorted(set(methods)))
    effective = utc(manifest.get("effective_at", now_utc()))
    source = dict(manifest, source_id=source_id, name=manifest.get("name") or source_id,
                  status=status, terms_locator=terms, upstream=upstream, fields=fields,
                  effective_at=effective)
    encoded = dump(source)
    with db(data_dir, write=True) as conn:
        exists = only_new and source_at(conn, source_id) is not None
        if not exists:
            previous = conn.execute(
                "SELECT manifest FROM ev_source_versions WHERE source_id=? AND effective_at=?",
                (source_id, effective),
            ).fetchone()
            if previous and previous[0] != encoded:
                raise ValueError("source version already exists; use a new effective_at")
            conn.execute("INSERT OR IGNORE INTO ev_source_versions VALUES(?,?,?,?)",
                         (source_id, effective, utc(now_utc()), encoded))
    report = publish_source(data_dir, source_id)
    return dict(report, registered_here=not exists) if only_new else report


def source_report(data_dir, source_id):
    with db(data_dir) as conn:
        source = source_at(conn, source_id)
        if not source:
            raise ValueError(f"unknown source: {source_id}")
        rows = conn.execute("SELECT document FROM ev_facts WHERE source_id=?", (source_id,))
        now = utc(now_utc())
        field_status = {name: ("active" if promoted_field(conn, source, name, now) else source["status"])
                        for name in source["fields"]}
        counts = {}
        delays = []
        confirmed = prediction = training = 0
        for row in rows:
            fact = json.loads(row[0])
            field = fact["field"]
            counts[field] = counts.get(field, 0) + 1
            delta = datetime.fromisoformat(fact["received_at"]) - datetime.fromisoformat(fact["published_at"])
            delays.append(delta.total_seconds())
            if fact["state"] == "active" and field == "lineup" and fact["value"].get("confirmed") is True:
                confirmed += 1
            allowed = set(fact["capture_uses"]) & granted(source, field, fact["method"], conn=conn, at=now)
            prediction += "prediction" in allowed
            training += "training" in allowed
        updated = conn.execute("SELECT MAX(recorded_at) FROM ev_source_versions WHERE source_id=?",
                               (source_id,)).fetchone()[0]
        health = {}
        for check in conn.execute("SELECT field,document FROM ev_source_checks WHERE source_id=? ORDER BY checked_at",
                                  (source_id,)):
            health[check[0]] = json.loads(check[1])
    return dict(source, field_status=field_status, updated_at=updated, health=health, trial={
        "sample_count":sum(counts.values()), "field_counts":counts, "confirmed_count":confirmed,
        "eligible_prediction_count":prediction, "eligible_training_count":training,
        "delays_seconds":sorted(delays),
    })


def publish_source(data_dir, source_id):
    result = source_report(data_dir, source_id)
    save_report(data_dir, "source", source_id, result)
    return result


def list_sources(data_dir):
    with db(data_dir) as conn:
        ids = [r[0] for r in conn.execute("SELECT DISTINCT source_id FROM ev_source_versions ORDER BY source_id")]
    return [source_report(data_dir, ident) for ident in ids]


def coverage_gaps(data_dir, field=None):
    if field is not None and field not in FIELDS:
        raise ValueError("unknown evidence field")
    sources = list_sources(data_dir)
    coverage = {}
    for name in sorted({field} if field else FIELDS):
        reports = [s for s in sources if name in s["fields"]]
        active = [s for s in reports if s["field_status"][name] == "active"
                  and "prediction" in s["fields"][name]["uses"]]
        candidates = [s["source_id"] for s in reports if s["field_status"][name] == "candidate"
                      and "trial" in s["fields"][name]["uses"]]
        issues = []
        if not active:
            issues.append("no_free_prediction_grant")
        elif len({s["upstream"] for s in active}) == 1:
            issues.append("no_independent_backup")
        if not any(s["trial"]["field_counts"].get(name) for s in reports):
            issues.append("no_observed_samples")
        unhealthy = [s["source_id"] for s in active if s["health"].get(name, {}).get("status")
                     in {"frozen", "late", "invalid", "error", "freshness_unknown"}]
        if unhealthy:
            issues.append("observed_source_health_issue")
        coverage[name] = {"prediction_sources": [s["source_id"] for s in active],
                          "upstreams": sorted({s["upstream"] for s in active}),
                          "candidate_sources": candidates, "unhealthy_sources": unhealthy, "gaps": issues}
    return {"fields": coverage}


def discover_sources(data_dir, manifests):
    """Intake a user-authorized catalog as shadow proposals, preserving existing source policies."""
    if not isinstance(manifests, list) or not manifests:
        raise ValueError("discovery needs a nonempty list of candidate source manifests")
    result = {"registered": [], "already_known": [], "rejected": []}
    for manifest in manifests:
        try:
            ident = required(manifest.get("source_id") if isinstance(manifest, dict) else None, "source_id")
            with db(data_dir) as conn:
                known = source_at(conn, ident)
            if known:
                result["already_known"].append(ident)
                continue
            required(manifest.get("discovered_from"), "discovered_from (catalog or announcement locator)")
            saved = register_source(data_dir, dict(manifest, status="candidate"), only_new=True)
            result["registered" if saved["registered_here"] else "already_known"].append(ident)
        except ValueError as error:
            result["rejected"].append({"source_id": manifest.get("source_id") if isinstance(manifest, dict) else None,
                                       "reason": str(error)})
    return result


def observe_source(data_dir, observation):
    """Record a measured fetch result and compare freshness with a scheduled update window."""
    sid = required(observation.get("source_id"), "source_id")
    field = required(observation.get("field"), "field")
    checked = utc(observation.get("checked_at",now_utc()))
    expected = utc(observation["expected_data_at"]) if observation.get("expected_data_at") else None
    latest = utc(observation["latest_data_at"]) if observation.get("latest_data_at") else None
    outcome = observation.get("outcome")
    if outcome not in {"ok","error","invalid"}:
        raise ValueError("source check outcome must be ok, error or invalid")
    content_hash = required(observation.get("content_hash"), "content_hash") if outcome == "ok" else None
    with db(data_dir,write=True) as conn:
        source = source_at(conn,sid,checked)
        if not source or field not in source["fields"]:
            raise ValueError("unknown source field for check")
        previous = conn.execute("""SELECT document FROM ev_source_checks WHERE source_id=? AND field=?
                                   AND checked_at<? ORDER BY checked_at DESC LIMIT 1""",(sid,field,checked)).fetchone()
        duplicate = bool(previous and content_hash and json.loads(previous[0]).get("content_hash") == content_hash)
        status = outcome
        if outcome == "ok" and expected and checked >= expected:
            if latest is None:
                status = "freshness_unknown"
            elif latest < expected:
                status = "frozen" if duplicate else "late"
        result = dict(observation, source_id=sid,field=field,checked_at=checked,
                      expected_data_at=expected,latest_data_at=latest,content_hash=content_hash,status=status)
        encoded = dump(result)
        old = conn.execute("SELECT document FROM ev_source_checks WHERE source_id=? AND field=? AND checked_at=?",
                           (sid,field,checked)).fetchone()
        if old and old[0] != encoded:
            raise ValueError("conflicting source observation")
        conn.execute("INSERT OR IGNORE INTO ev_source_checks VALUES(?,?,?,?)",(sid,field,checked,encoded))
    publish_source(data_dir,sid)
    return result
