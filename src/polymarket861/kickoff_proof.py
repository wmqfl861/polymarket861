"""Proven first-half event times for a particular EPL fixture and schedule revision."""

import hashlib
import json
import re
from contextlib import closing, nullcontext
from datetime import timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree as ET

from . import evidence_sources, store

SOURCE = "espn-epl-first-half-wallclock"
BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event="
# Reviewed source releases only. Public URLs, user-entered labels and unsigned self-reports
# cannot add a permission or a trusted corpus to either map. Empty is a valid coverage state.
FREE_ESPN_PERMISSIONS = {}  # permission-file SHA256 -> issuer's authenticated written grant URL
OPEN_RELEASES = {}  # release key -> pinned publisher URL, public licence URL and both SHA256s
COLUMN_SCHEMA = """
CREATE TABLE IF NOT EXISTS football_kickoff_receipts (
    proof_id TEXT PRIMARY KEY, authority TEXT NOT NULL, fixture_id TEXT NOT NULL, schedule_version INTEGER NOT NULL,
    provider_event_id TEXT NOT NULL, actual_kickoff TEXT NOT NULL, received_at TEXT NOT NULL,
    source_sha256 TEXT NOT NULL, record TEXT NOT NULL
)
"""


class MissingKickoffProof(ValueError):
    """The independent evidence might still arrive after the game."""


def _event_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,11}", value):
        raise ValueError("expected a positive numeric publisher event ID")
    return value


def _provider_url(event_id):
    return BASE + _event_id(event_id)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch(url):
    req = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "polymarket861-paper/1.1"},
    )
    try:
        with build_opener(_NoRedirect).open(req, timeout=15) as reply:
            if reply.geturl() != url or reply.status != 200:
                raise ValueError("unverified match event endpoint")
            raw = reply.read(8_000_001)
    except (OSError, URLError) as exc:
        raise ValueError("the independent match event could not be retrieved") from exc
    if len(raw) > 8_000_000:
        raise ValueError("match event response exceeds the archive limit")
    return raw


def _permission(root, db, when):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='ev_source_versions'"
    ).fetchone():
        raise ValueError(
            "free source permission for event retrieval and retention is missing"
        )
    cut = evidence_sources.utc(when)
    grant = evidence_sources.source_at(db, SOURCE, cut, recorded_by=cut)
    rule = (grant or {}).get("fields", {}).get("schedule", {})
    if not (
        grant
        and grant.get("upstream") == "ESPN"
        and grant.get("status") == "active"
        and "trial" in evidence_sources.granted(grant, "schedule", "auto")
        and rule.get("allow_free_first_half_event_archive") is True
        and isinstance(rule.get("written_permission_locator"), str)
        and rule["written_permission_locator"].strip()
    ):
        raise ValueError(
            "free source permission for event retrieval and retention is missing"
        )
    sha = rule.get("written_permission_sha256")
    if (
        sha not in FREE_ESPN_PERMISSIONS
        or rule["written_permission_locator"] != FREE_ESPN_PERMISSIONS[sha]
    ):
        raise ValueError(
            "independent free written permission has not been verified for this release"
        )
    try:
        permitted = (
            hashlib.sha256(store.blob_path(root, sha).read_bytes()).hexdigest() == sha
        )
    except OSError:
        permitted = False
    if not permitted:
        raise ValueError("verified free written permission original is missing")
    return store.digest(grant)


def _fixture(db, fid, at, planned=None):
    row = db.execute(
        "SELECT f.*,s.precision FROM fixture_versions f JOIN schedule_receipts s USING(fixture_id,version) "
        "WHERE f.fixture_id=? AND f.known_at<=? ORDER BY f.known_at DESC LIMIT 1",
        (fid, store.iso(at)),
    ).fetchone()
    if row is None or row["precision"] not in ("minute", "second"):
        raise ValueError("no precise archived schedule version at the decision cutoff")
    if planned is not None and row["kickoff"] != store.iso(planned):
        raise ValueError("kickoff proof belongs to a different schedule revision")
    return dict(row)


def _team(value):
    # Only spelling already returned by the publisher is accepted. No fuzzy/link-by-time fallback.
    return " ".join(value.split()).casefold() if isinstance(value, str) else ""


def _parse(raw, fx, event_id):
    try:
        data = json.loads(raw)
        header = data["header"]
        (comp,) = header["competitions"]
        if (
            str(header["id"]) != event_id
            or str(comp["id"]) != event_id
            or header["league"]["slug"] != "eng.1"
            or comp["status"]["type"]["completed"] is not True
            or store.iso(comp["date"]) != fx["kickoff"]
        ):
            raise ValueError(
                "provider event, league, completed status or scheduled kickoff mismatch"
            )
        competitors = comp["competitors"]
        sides = {p["homeAway"]: p["team"] for p in competitors}
        if (
            len(competitors) != 2
            or set(sides) != {"home", "away"}
            or sides["home"]["id"] == sides["away"]["id"]
        ):
            raise ValueError("ambiguous provider teams")
        for side, team in sides.items():
            aliases = {
                _team(team.get(k)) for k in ("displayName", "shortDisplayName", "name")
            }
            if not team["id"] or _team(fx[side]) not in aliases:
                raise ValueError("provider team or home/away mismatch")
        first = []
        for play in data["keyEvents"]:
            if play.get("type", {}).get("text") != "Kickoff":
                continue
            labels = {
                str(play.get(key, "")).strip().rstrip(".").casefold()
                for key in ("text", "shortText", "alternativeText")
            }
            per = play.get("period", {})
            period = per.get("number") if isinstance(per, dict) else per
            if (
                "first half begins" not in labels
                or period not in (None, 1)
                or play.get("shootout") is True
            ):
                continue
            clock = play["wallclock"]
            if (
                not play.get("id")
                or not isinstance(clock, str)
                or not re.search(r"T\d\d:\d\d:\d\d", clock)
            ):
                raise ValueError(
                    "the actual first-half event has no precise wall clock or event ID"
                )
            first.append((str(play["id"]), store.iso(clock)))
        if len(first) != 1:
            raise ValueError(
                "a unique first-half kickoff event with an absolute wall clock is required"
            )
        return {
            "play_id": first[0][0],
            "actual_kickoff": first[0][1],
            "team_ids": {side: str(t["id"]) for side, t in sides.items()},
        }
    except (KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ValueError("unsupported independent EPL event response") from exc


def _open_release(key):
    release = OPEN_RELEASES.get(key)
    if (
        not isinstance(release, dict)
        or release.get("format") != "opta-f24"
        or release.get("licence")
        not in {"CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "ODC-BY-1.0", "PDDL-1.0"}
        or not all(
            str(release.get(k, "")).startswith("https://")
            for k in ("source_url", "licence_url")
        )
        or not all(
            re.fullmatch(r"[0-9a-f]{64}", str(release.get(k, "")))
            for k in ("source_sha256", "licence_sha256")
        )
    ):
        raise ValueError("no independently verified openly licensed source release")
    return release


def _parse_open(raw, fx, event_id, release):
    def utc_stamp(value):
        if not isinstance(value, str) or not re.search(r"T\d\d:\d\d:\d\d", value):
            raise ValueError("open source lacks an absolute second-precision kickoff")
        if release.get("naive_clock_basis") == "UTC" and re.fullmatch(
            r"[\d:T. -]+", value
        ):
            value += "Z"
        return store.iso(value)

    if (
        b"<!DOCTYPE" in raw.upper()
        or b"<!ENTITY" in raw.upper()
        or len(raw) > 8_000_000
    ):
        raise ValueError("unsupported source export")
    try:
        xml = ET.fromstring(raw)
        games = [item.attrib for item in xml.iter("Game") if item.get("id") == event_id]
        if len(games) != 1:
            raise ValueError("open export does not identify one publisher event")
        game = games[0]
        if (
            str(game.get("competition_id")) != "8"
            or _team(game.get("competition_name"))
            not in {
                "english premier league",
                "premier league",
                "barclays premier league",
            }
            or utc_stamp(game.get("game_date")) != fx["kickoff"]
            or any(
                _team(game.get(side + "_team_name")) != _team(fx[side])
                for side in ("home", "away")
            )
        ):
            raise ValueError("open competition, teams or scheduled kickoff mismatch")
        teams = {side: str(game[side + "_team_id"]) for side in ("home", "away")}
        if not all(teams.values()) or teams["home"] == teams["away"]:
            raise ValueError("ambiguous open source participant IDs")
        started = utc_stamp(game.get("period_1_start"))
        # A completed provider export carries the end of normal time independently of game_date.
        finish = utc_stamp(game.get("period_2_end"))
        if not started < finish:
            raise ValueError("open first-half event is not from a completed match")
        return {
            "play_id": "period_1_start",
            "actual_kickoff": started,
            "completed_at": finish,
            "team_ids": teams,
        }
    except (ET.ParseError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("not a supported final match event export") from exc


def _record(fx, event_id, raw, received, permission_sha, *, authority=SOURCE):
    if authority == SOURCE:
        event = _parse(raw, fx, event_id)
        url, semantic = (
            _provider_url(event_id),
            "completed_first_half_kickoff_wallclock",
        )
    else:
        release = _open_release(authority)
        if (
            hashlib.sha256(raw).hexdigest() != release["source_sha256"]
            or permission_sha != release["licence_sha256"]
        ):
            raise ValueError(
                "source export or licence differs from its reviewed release"
            )
        event = _parse_open(raw, fx, event_id, release)
        url, semantic = release["source_url"], "completed_f24_period_1_start"
    if store.instant(
        event.get("completed_at", event["actual_kickoff"])
    ) > store.instant(received):
        raise ValueError("independent match event occurs after receipt")
    return dict(
        event,
        fixture_id=fx["fixture_id"],
        schedule_version=fx["version"],
        scheduled_kickoff=fx["kickoff"],
        schedule_sha256=store.digest(fx),
        provider_event_id=event_id,
        url=url,
        source=authority,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        received_at=store.iso(received),
        permission_sha256=permission_sha,
        licence_url=(
            FREE_ESPN_PERMISSIONS.get(permission_sha)
            if authority == SOURCE
            else release["licence_url"]
        ),
        licence=(
            "express-free-archival-grant" if authority == SOURCE else release["licence"]
        ),
        semantic=semantic,
    )


def _at_version(db, fixture_id, version):
    row = db.execute(
        "SELECT kickoff FROM fixture_versions WHERE fixture_id=? AND version=?",
        (fixture_id, version),
    ).fetchone()
    if row is None:
        raise ValueError("unknown fixture schedule revision")
    fx = _fixture(db, fixture_id, store.instant(row[0]) - timedelta(hours=1), row[0])
    if fx["version"] != version:
        raise ValueError("the nominated schedule revision was not in force at T-60")
    return fx


def _meta(authority, url):
    return (
        {"url": url, "method": "GET", "parser": "espn-epl-summary-v1"}
        if authority == SOURCE
        else {
            "url": url,
            "method": "pinned-open-release",
            "parser": "opta-f24-period-start-v1",
        }
    )


def _save(data_dir, fx, event_id, raw, authority, permission):
    received = store.now_utc()
    record = _record(fx, event_id, raw, received, permission, authority=authority)
    store.archive_bytes(
        data_dir,
        authority,
        raw,
        observed_at=received,
        metadata=_meta(authority, record["url"]),
    )
    proof = store.digest(record)
    with closing(store.connect(data_dir)) as db, db:
        db.execute(COLUMN_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        if authority == SOURCE and permission != _permission(data_dir, db, received):
            raise ValueError("source permission changed during capture")
        db.execute(
            "INSERT OR IGNORE INTO football_kickoff_receipts VALUES (?,?,?,?,?,?,?,?,?)",
            (
                proof,
                authority,
                fx["fixture_id"],
                fx["version"],
                event_id,
                record["actual_kickoff"],
                record["received_at"],
                record["source_sha256"],
                store.canonical(record),
            ),
        )
    return resolve(
        data_dir,
        fx["fixture_id"],
        fx["kickoff"],
        store.instant(fx["kickoff"]) - timedelta(hours=1),
    )


def capture(data_dir, fixture_id, event_id, *, schedule_version):
    """Only the provider's own completed-match endpoint can supply this timestamp."""
    url = _provider_url(event_id)
    with closing(store.connect(data_dir)) as db:
        grant = _permission(data_dir, db, store.now_utc())
        fx = _at_version(db, fixture_id, schedule_version)
    return _save(data_dir, fx, event_id, _fetch(url), SOURCE, grant)


def import_open(
    data_dir, fixture_id, event_id, release_key, timeline, licence, *, schedule_version
):
    """A local copy is equivalent to its publisher original only when both release digests match."""
    release = _open_release(release_key)
    event_id = _event_id(event_id)
    rights = Path(licence).read_bytes()
    if hashlib.sha256(rights).hexdigest() != release["licence_sha256"]:
        raise ValueError(
            "public licence digest differs from the reviewed publisher release"
        )
    with closing(store.connect(data_dir)) as db:
        fx = _at_version(db, fixture_id, schedule_version)
    raw = Path(timeline).read_bytes()
    if hashlib.sha256(raw).hexdigest() != release["source_sha256"]:
        raise ValueError(
            "event file differs from the independently pinned publisher export"
        )
    store.archive_bytes(
        data_dir,
        release_key,
        rights,
        media_type="text/plain",
        metadata={"url": release["licence_url"]},
    )
    return _save(data_dir, fx, event_id, raw, release_key, release["licence_sha256"])


def resolve(
    data_dir, fixture_id, scheduled_kickoff, decision_at, *, db=None, prospective=False
):
    """Recheck every independent receipt. The caller cannot choose between conflicting revisions."""
    if store.instant(scheduled_kickoff) - store.instant(decision_at) != timedelta(
        hours=1
    ):
        raise ValueError("kickoff proof requires the original T-60 schedule")
    with closing(store.connect(data_dir)) if db is None else nullcontext(db) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='football_kickoff_receipts'"
        ).fetchone():
            raise MissingKickoffProof("independent actual kickoff proof is missing")
        if not conn.execute(
            "SELECT 1 FROM football_kickoff_receipts WHERE fixture_id=?", (fixture_id,)
        ).fetchone():
            raise MissingKickoffProof("independent actual kickoff proof is missing")
        fx = _fixture(conn, fixture_id, decision_at, scheduled_kickoff)
        if prospective:
            known = conn.execute(
                "SELECT f.version FROM fixture_versions f JOIN schedule_receipts s USING(fixture_id,version) "
                "WHERE f.fixture_id=? AND f.known_at<=? AND s.recorded_at<=? ORDER BY f.known_at DESC LIMIT 1",
                (fixture_id, store.iso(decision_at), store.iso(decision_at)),
            ).fetchone()
            if known is None or known[0] != fx["version"]:
                raise ValueError(
                    "prospective kickoff proof disagrees with the schedule actually held at decision"
                )
        rows = conn.execute(
            "SELECT * FROM football_kickoff_receipts WHERE fixture_id=? AND schedule_version=? ORDER BY received_at,proof_id",
            (fixture_id, fx["version"]),
        ).fetchall()
        if not rows:
            raise MissingKickoffProof("independent actual kickoff proof is missing")
        seen = set()
        provider_ids = {}
        first = None
        for r in rows:
            authority = r["authority"]
            if authority == SOURCE:
                url = _provider_url(r["provider_event_id"])
                permission = _permission(data_dir, conn, r["received_at"])
            else:
                release = _open_release(authority)
                url = release["source_url"]
                permission = release["licence_sha256"]
                try:
                    licence = store.blob_path(data_dir, permission).read_bytes()
                except OSError as exc:
                    raise ValueError(
                        "published open licence original is missing"
                    ) from exc
                if hashlib.sha256(licence).hexdigest() != permission:
                    raise ValueError("published open licence hash mismatch")
            meta = _meta(authority, url)
            receipt = conn.execute(
                "SELECT 1 FROM observations WHERE source=? AND observed_at=? AND sha256=? AND metadata=?",
                (
                    authority,
                    r["received_at"],
                    r["source_sha256"],
                    store.canonical(meta),
                ),
            ).fetchone()
            if not receipt or r["received_at"] > store.now_utc():
                raise ValueError(
                    "independent kickoff proof receipt is missing or premature"
                )
            same_event = conn.execute(
                "SELECT DISTINCT fixture_id FROM football_kickoff_receipts WHERE (authority=?)=(?=?) AND provider_event_id=?",
                (SOURCE, authority, SOURCE, r["provider_event_id"]),
            ).fetchall()
            if len(same_event) != 1:
                raise ValueError(
                    "the provider event was attached to conflicting fixtures"
                )
            try:
                raw = store.blob_path(data_dir, r["source_sha256"]).read_bytes()
            except OSError as exc:
                raise ValueError(
                    "independent kickoff source bytes are unavailable"
                ) from exc
            rec = _record(
                fx,
                r["provider_event_id"],
                raw,
                r["received_at"],
                permission,
                authority=authority,
            )
            if (
                store.digest(rec) != r["proof_id"]
                or store.canonical(rec) != r["record"]
                or rec["actual_kickoff"] != r["actual_kickoff"]
                or rec["source_sha256"] != r["source_sha256"]
            ):
                raise ValueError(
                    "independent kickoff proof failed raw receipt verification"
                )
            publisher = "ESPN" if authority == SOURCE else "Opta"
            identity = (rec["provider_event_id"], rec["play_id"])
            if provider_ids.setdefault(publisher, identity) != identity:
                raise ValueError(
                    "conflicting source event identities for the same fixture"
                )
            seen.add(rec["actual_kickoff"])
            if first is None:
                first = dict(rec, proof_id=r["proof_id"])
        if len(seen) != 1:
            raise ValueError(
                "conflicting independent actual kickoff evidence; match quarantined"
            )
        return first
