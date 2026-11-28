"""Compare independently observed NBA finals and native payouts. Neither overwrites the other."""

import gzip
import hashlib
import json
import re
import zlib
from collections import Counter
from zoneinfo import ZoneInfo

from .. import market_provider as api
from ..store import canonical, digest, instant
from . import markets
from .markets_contracts import contract, eligibility, fixture, team_code

EAST = ZoneInfo("America/New_York")
STATES = (
    "match",
    "disagrees",
    "void_50_50",
    "awaiting_score",
    "unpaired",
    "invalid_score",
    "outside_rules",
    "invalid_native_evidence",
    "pending_reconciliation",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nba_payout_native (
    condition_id TEXT PRIMARY KEY, event_id TEXT, game_day TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS nba_payout_day ON nba_payout_native(game_day);
CREATE TABLE IF NOT EXISTS nba_payout_pending (condition_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS nba_payout_dirty (game_day TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS nba_payout_report_backlog (game_day TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS nba_payout_revisions (
    condition_id TEXT NOT NULL, revision INTEGER NOT NULL, recorded_at TEXT NOT NULL,
    game_day TEXT NOT NULL, game_id TEXT, state TEXT NOT NULL, sha TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(condition_id,revision)
);
CREATE INDEX IF NOT EXISTS nba_payout_revisions_day ON nba_payout_revisions(game_day);
"""

# The first run also enrolls settlements captured by older versions. Later cycles read only
# newly settled conditions and dates touched by the score or market feeds.
_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS nba_payout_received AFTER INSERT ON nba_market_settled BEGIN
    INSERT OR IGNORE INTO nba_payout_pending VALUES(NEW.condition_id);
END;
CREATE TRIGGER IF NOT EXISTS nba_payout_corrected AFTER UPDATE ON nba_market_settled BEGIN
    INSERT OR IGNORE INTO nba_payout_pending VALUES(NEW.condition_id);
END;
CREATE TRIGGER IF NOT EXISTS nba_payout_identity AFTER INSERT ON nba_market_identity_conflicts BEGIN
    INSERT OR IGNORE INTO nba_payout_dirty SELECT game_day FROM nba_payout_native
        WHERE event_id=NEW.id OR json_extract(payload,'$.venue_game_id')=NEW.game_id;
END;
CREATE TRIGGER IF NOT EXISTS nba_payout_schedule AFTER INSERT ON nba_market_schedule BEGIN
    INSERT OR IGNORE INTO nba_payout_dirty SELECT n.game_day FROM nba_payout_native n
        JOIN nba_market_games g ON g.id=NEW.id WHERE n.event_id=NEW.id OR
        (g.home=json_extract(n.payload,'$.home') AND g.away=json_extract(n.payload,'$.away'));
END;
INSERT OR IGNORE INTO nba_payout_pending SELECT condition_id FROM nba_market_settled;
"""


def _exists(db, name):
    return (
        db is not None
        and db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
        is not None
    )


def _receipt(db, rid, cutoff):
    row = db.execute(
        """SELECT r.*,p.compressed FROM nba_market_requests r JOIN nba_market_payloads p USING(sha)
               WHERE r.id=? AND r.code=200 AND r.received<=? AND r.sent<=r.received""",
        (rid, cutoff),
    ).fetchone()
    if not row:
        raise ValueError("missing native receipt or invalid receipt time")
    raw = gzip.decompress(row["compressed"])
    if hashlib.sha256(raw).hexdigest() != row["sha"]:
        raise ValueError("native receipt digest mismatch")
    return row, json.loads(raw)


def _period(event):
    """A terminal Q4/OT ordinal is evidence. A generic final marker gives no OT information."""
    raw = event.get("period")
    evidence = {"field": "period", "value": raw, "overtime": None}
    if event.get("ended") is not True or event.get("live") is True:
        return evidence
    label = str(raw).strip().upper()
    quarter = re.fullmatch(r"(?:Q|QUARTER\s*)?([4-9]|1[0-9])", label)
    if quarter:
        evidence["overtime"] = int(quarter[1]) > 4
    elif re.fullmatch(
        r"(?:(?:FINAL|FT|F)(?:[ /-]| \())?(?:[1-9][0-9]?OT|OT(?:[1-9][0-9]?)?)\)?",
        label,
    ):
        evidence["overtime"] = True
    return evidence


def _verify_native(db, settled, out):
    cid, cutoff = settled["condition_id"], settled["observed"]
    event_receipt, event = _receipt(db, settled["receipt"], cutoff)
    g = fixture(event)
    if event_receipt["url"] != f"{api.GAMMA}/events/{g['id']}":
        raise ValueError("unexpected Gamma event receipt")
    tip = instant(g["kickoff"])
    out.update(
        venue_event_id=g["id"],
        venue_game_id=g["game_id"],
        home=g["home"],
        away=g["away"],
        venue_kickoff=g["kickoff"],
        game_day=tip.astimezone(EAST).date().isoformat(),
        game_day_basis="venue_kickoff",
        period=_period(event),
        event_sha256=event_receipt["sha"],
    )
    registered = db.execute(
        "SELECT * FROM nba_market_games WHERE id=?", (g["id"],)
    ).fetchone()
    if (
        not registered
        or any(registered[k] != g[k] for k in ("game_id", "home", "away"))
        or any(
            registered[s + "_id"] != str(g["clubs"][s]["id"]) for s in ("home", "away")
        )
    ):
        raise ValueError("native participant identity differs from registration")
    native = [
        m
        for m in event.get("markets", [])
        if isinstance(m, dict) and m.get("conditionId") == cid
    ]
    if (
        len(native) != 1
        or native[0].get("closed") is not True
        or native[0].get("umaResolutionStatus") != "resolved"
    ):
        raise ValueError("missing unique terminal Gamma condition")
    m = native[0]
    schedule = db.execute(
        "SELECT version,kickoff FROM nba_market_schedule WHERE id=? AND valid_from<=? ORDER BY version DESC LIMIT 1",
        (g["id"], cutoff),
    ).fetchone()
    if not schedule or schedule["kickoff"] != g["kickoff"]:
        raise ValueError(
            "terminal event kickoff differs from observed schedule version"
        )
    out["settlement_venue_version"] = schedule["version"]
    c = contract(event, g, m, main_only=False, native_only=True)
    out.update(
        {
            k: c[k]
            for k in ("family", "line", "token_id", "opposite", "ot_basis", "market_id")
        }
    )
    saved = db.execute(
        """SELECT c.contract,c.version,s.kickoff,c.quality FROM nba_market_choices c
           JOIN nba_market_schedule s USING(id,version) WHERE c.id=? AND c.contract IS NOT NULL
           AND c.available<>'pending' AND c.available<=?""",
        (g["id"], cutoff),
    )
    identity = ("family", "line", "token_id", "opposite", "rules", "market_id")
    versions = set()
    observed = False
    for choice in saved:
        original = json.loads(choice["contract"])
        if original["condition_id"] != cid:
            continue
        observed = True
        if any(original.get(k) != c[k] for k in identity):
            raise ValueError("frozen contract identity or rule changed")
        if choice["kickoff"] == g["kickoff"] and choice["quality"] == "ok":
            versions.add(choice["version"])
    if not observed:
        raise ValueError("missing original contract observation")
    original = markets._first_tip(db, g["id"])
    out.update(
        venue_versions=sorted(versions),
        rules_sha256=hashlib.sha256(c["rules"].encode()).hexdigest(),
        original_kickoff=c["original_kickoff"],
        start_deadline=c["start_deadline"],
        rule_exclusion=eligibility(c, g["kickoff"], original_kickoff=original),
    )
    tokens = [str(t) for t in api.sequence(m["clobTokenIds"])]
    gamma = dict(
        zip(tokens, map(api.number, api.sequence(m["outcomePrices"])), strict=True)
    )
    payouts = json.loads(settled["payouts"])
    if (
        not isinstance(payouts, dict)
        or any(type(v) not in (float, int) for v in payouts.values())
        or sorted(gamma.values()) not in ([0, 1], [0.5, 0.5])
        or gamma != payouts
    ):
        raise ValueError("terminal Gamma vector differs from recorded payouts")
    token_receipts = {}
    for rid in json.loads(settled["official_receipt"]):
        row, payload = _receipt(db, rid, cutoff)
        params = json.loads(row["params"])
        token = str(params["token_id"])
        if (
            token not in gamma
            or token in token_receipts
            or row["url"] != api.DATA + "/v2/tokens"
        ):
            raise ValueError("wrong or duplicate official token receipt")
        resolution = api.resolution_row(payload, token)
        if (
            not resolution
            or not resolution["resolved"]
            or resolution["condition_id"] != cid
            or api.number(resolution["final_price"]) != gamma[token]
        ):
            raise ValueError("official token contradicts recorded payout")
        token_receipts[token] = {"receipt": rid, "sha256": row["sha"]}
    if set(token_receipts) != set(gamma):
        raise ValueError("incomplete official token evidence")
    out.update(payouts=payouts, token_receipts=token_receipts)


def _record_native(db, row):
    out = {
        "condition_id": row["condition_id"],
        "native_observed_at": row["observed"],
        "event_receipt": row["receipt"],
        "game_day_basis": "native_observation_date_unknown_fixture",
        "game_day": instant(row["observed"]).astimezone(EAST).date().isoformat(),
    }
    try:
        _verify_native(db, row, out)
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OSError,
        EOFError,
        OverflowError,
        zlib.error,
    ) as exc:
        out["evidence_error"] = str(exc)
    old = db.execute(
        "SELECT game_day,payload FROM nba_payout_native WHERE condition_id=?",
        (row["condition_id"],),
    ).fetchone()
    if old:
        previous = json.loads(old["payload"])
        registered = previous.get("venue_event_id")
        if registered and out.get("venue_event_id", registered) != registered:
            out.update(
                evidence_error="previously recorded condition attributed to a different event",
                claimed_venue_event_id=out["venue_event_id"],
            )
        if registered and out.get("evidence_error"):
            # Retain its last auditable fixture/day for the alert, even when the new bytes cannot be identified.
            for key in (
                "game_day",
                "venue_event_id",
                "venue_game_id",
                "venue_kickoff",
                "venue_versions",
                "home",
                "away",
                "family",
                "token_id",
                "opposite",
                "market_id",
                "line",
            ):
                if key in previous:
                    out[key] = previous[key]
            out["game_day_basis"] = "previous_native_receipt"
    db.execute(
        "INSERT OR REPLACE INTO nba_payout_native VALUES(?,?,?,?)",
        (
            row["condition_id"],
            out.get("venue_event_id"),
            out["game_day"],
            canonical(out),
        ),
    )
    for day in {out["game_day"], old[0] if old else out["game_day"]}:
        db.execute("INSERT OR IGNORE INTO nba_payout_dirty VALUES(?)", (day,))


def _score(db, native, games, now):
    gid = native["venue_event_id"]
    candidates = [
        g
        for g in games
        if team_code(g.get("home_abbr")) == native["home"]
        and team_code(g.get("away_abbr")) == native["away"]
    ]
    if len(candidates) != 1:
        return None, "ambiguous_score_fixture" if candidates else "no_score_fixture"
    g = candidates[0]
    if not g.get("kickoff") or g.get("source") != "balldontlie":
        return None, "unverified_score_kickoff"
    if instant(g["kickoff"]).astimezone(EAST).date().isoformat() != native["game_day"]:
        return None, "score_date_conflict"
    match = markets.schedule_match_db(
        db, native["home"], native["away"], g["kickoff"], as_of=now
    )
    if (
        match["status"] not in ("exact", "within_tolerance")
        or match["venue_event_id"] != gid
        or match["venue_kickoff"] != native["venue_kickoff"]
        or match["venue_version"] != native["settlement_venue_version"]
    ):
        return None, match.get("reason") or match["status"]
    if g.get("stage") != "regular" or g.get("conflict") or g.get("timing_problem"):
        return None, "unverified_score_identity_or_stage"
    return g, None


def _overtime(native, score, score_state):
    occurred, basis = native["overtime"], "unverified"
    if occurred is not None:
        basis = "venue_period"
    ordinal = (score or {}).get("final_period")
    if score_state == "final" and type(ordinal) is int and 4 <= ordinal <= 19:
        if occurred is None:
            occurred, basis = ordinal > 4, "score_period"
        elif occurred == (ordinal > 4):
            basis = "both"
        else:
            occurred, basis = None, "conflicting_periods"
    return {
        "occurred": occurred,
        "basis": basis,
        "native_period": native["value"],
        "score_final_period": ordinal,
    }


def _compare(db, native, games, now):
    out = native.copy()
    if native.get("evidence_error"):
        return dict(out, state="invalid_native_evidence")
    g, problem = _score(db, native, games, now)
    score, expected = None, None
    if g:
        score = {
            k: g.get(k)
            for k in (
                "source",
                "game_id",
                "version",
                "received_at",
                "available_at",
                "receipt_sha256",
                "status",
                "home_score",
                "away_score",
                "home",
                "away",
                "kickoff",
                "score_basis",
                "final_period",
            )
        }
    if problem:
        score_state = "unpaired"
    elif score["status"] != "final":
        score_state = "awaiting_score"
    else:
        h, a = score["home_score"], score["away_score"]
        if (
            not all(type(n) is int and n > 0 for n in (h, a))
            or h == a
            or score["score_basis"] != "final_includes_ot"
            or not score["receipt_sha256"]
            or not score["available_at"]
            or score["available_at"] > now
            or not score["received_at"]
            or score["received_at"] < score["kickoff"]
        ):
            score_state = "invalid_score"
        else:
            score_state = "final"
            f = out["family"]
            expected = (
                int(h > a)
                if f == "win"
                else int(
                    {"total": h + a, "home_total": h, "away_total": a}[f] > out["line"]
                )
            )
    native_value = out["payouts"][out["token_id"]]
    state = score_state
    if native_value == 0.5:
        state = "void_50_50"
    elif (
        out["rule_exclusion"]
        or out["settlement_venue_version"] not in out["venue_versions"]
        or out["native_observed_at"] < out["venue_kickoff"]
        or (score and score["kickoff"] > out["start_deadline"])
    ):
        state = "outside_rules"
    elif score_state == "final":
        state = "match" if expected == native_value else "disagrees"
    return dict(
        out,
        state=state,
        score_state=score_state,
        overtime=_overtime(out["period"], score, score_state),
        pairing_reason=problem,
        score=score,
        score_implied_primary=expected,
        native_primary=native_value,
    )


def _persist(db, body, now, ops_ready):
    cid, sha, day = body["condition_id"], digest(body), body["game_day"]
    prev = db.execute(
        "SELECT * FROM nba_payout_revisions WHERE condition_id=? ORDER BY revision DESC LIMIT 1",
        (cid,),
    ).fetchone()
    if prev and prev["sha"] == sha:
        return
    gid = (body.get("score") or {}).get("game_id")
    db.execute(
        "INSERT INTO nba_payout_revisions VALUES(?,?,?,?,?,?,?,?)",
        (
            cid,
            prev["revision"] + 1 if prev else 1,
            now,
            day,
            gid,
            body["state"],
            sha,
            canonical(body),
        ),
    )
    if not ops_ready:
        db.execute("INSERT OR IGNORE INTO nba_payout_report_backlog VALUES(?)", (day,))
        return
    from .operations import _dirty_day

    _dirty_day(db, day)
    if prev and prev["game_day"] != day:
        _dirty_day(db, prev["game_day"])
    # An unmatched/withdrawn condition has no trustworthy current score ID. Its
    # originally sealed references must still acquire the new adjudication version.
    db.execute(
        """INSERT OR IGNORE INTO nba_grade_queue
           SELECT DISTINCT w.game_id FROM nba_windows w, json_each(w.payload,'$.market_at_prediction') r
           WHERE w.sealed_at IS NOT NULL AND w.game_day=? AND json_extract(r.value,'$.condition_id')=?""",
        (day, cid),
    )
    if prev and prev["game_day"] != day and prev["game_id"]:
        db.execute(
            "INSERT OR IGNORE INTO nba_grade_queue VALUES(?)", (prev["game_id"],)
        )


def refresh(db, now):
    """Run after NBA source sync, before grades, with no open transaction."""
    if not _exists(db, "nba_market_settled"):
        return
    ops_ready = _exists(db, "nba_report_queue")
    if not _exists(db, "nba_payout_received"):
        # Atomic backfill/installation if two scheduler processes initialize simultaneously.
        with db:
            db.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA + _TRIGGERS + "COMMIT;")
    with db:
        db.execute("BEGIN IMMEDIATE")
        for row in db.execute(
            "SELECT s.* FROM nba_payout_pending p JOIN nba_market_settled s USING(condition_id) WHERE s.observed<=?",
            (now,),
        ).fetchall():
            _record_native(db, row)
            db.execute(
                "DELETE FROM nba_payout_pending WHERE condition_id=?",
                (row["condition_id"],),
            )
        dates = "SELECT game_day FROM nba_payout_dirty"
        if ops_ready:
            # A market-only installation can acquire its score service and reports later.
            from .operations import _dirty_day

            for (day,) in db.execute(
                "SELECT game_day FROM nba_payout_report_backlog"
            ).fetchall():
                _dirty_day(db, day)
            db.execute("DELETE FROM nba_payout_report_backlog")
            dates += """ UNION SELECT period FROM nba_report_queue q WHERE kind='day'
                         AND EXISTS(SELECT 1 FROM nba_payout_native n WHERE n.game_day=q.period)"""
        days = db.execute(dates).fetchall()
        for (day,) in days:
            games = (
                [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT game FROM nba_ops_games WHERE game_day=?", (day,)
                    )
                ]
                if ops_ready
                else []
            )
            for row in db.execute(
                "SELECT payload FROM nba_payout_native WHERE game_day=?", (day,)
            ):
                _persist(
                    db, _compare(db, json.loads(row[0]), games, now), now, ops_ready
                )
            db.execute("DELETE FROM nba_payout_dirty WHERE game_day=?", (day,))


def collect_native_only(db, now):
    """Give market-only deployments a native evidence ledger before the score service starts."""
    if not _exists(db, "nba_ops_games"):
        refresh(db, now)


def by_reference(db, game_id, refs, *, source_kickoff=None):
    """Link only the same sealed token/line/fixture to its independently verified revision."""
    out = {}
    ready = _exists(db, "nba_payout_revisions")
    for family, ref in refs.items():
        cid = ref.get("condition_id")
        row = (
            db.execute(
                "SELECT * FROM nba_payout_revisions WHERE condition_id=? ORDER BY revision DESC LIMIT 1",
                (cid,),
            ).fetchone()
            if ready
            else None
        )
        if not row:
            out[family] = {"state": "awaiting_native", "condition_id": cid}
            continue
        b = json.loads(row["payload"])
        original_tip = ref.get("source_kickoff") or source_kickoff
        result_tip = (b.get("score") or {}).get("kickoff")
        if (
            family != b.get("family")
            or ref.get("token_id") != b.get("token_id")
            or ref.get("line") != b.get("line")
            or ref.get("venue_event_id", b.get("venue_event_id"))
            != b.get("venue_event_id")
            or (
                "venue_version" in ref
                and ref["venue_version"] not in b.get("venue_versions", ())
            )
            or (
                ref.get("venue_kickoff")
                and ref["venue_kickoff"] != b.get("venue_kickoff")
            )
            or (original_tip and result_tip and original_tip != result_tip)
            or (not ref.get("venue_event_id") and row["game_id"] != game_id)
            or (row["game_id"] is not None and row["game_id"] != game_id)
        ):
            out[family] = {
                "state": "unpaired",
                "condition_id": cid,
                "reason": "reference_identity_conflict",
            }
            continue
        out[family] = {
            k: b.get(k)
            for k in (
                "state",
                "condition_id",
                "score_state",
                "pairing_reason",
                "evidence_error",
                "native_primary",
                "score_implied_primary",
                "native_observed_at",
                "event_sha256",
                "period",
                "overtime",
            )
        }
        out[family]["revision"] = row["revision"]
    return out


def summary(db, start=None, end=None):
    """Count each condition once and OT precedents once per native game, across all horizons."""
    counts, scores, by_family, disagreements, voids, overtime = (
        Counter(),
        Counter(),
        {},
        {},
        {},
        {},
    )
    issues = []
    if _exists(db, "nba_payout_revisions"):
        dates, args = "", ()
        if start is not None and end is not None:
            dates, args = " AND r.game_day>=? AND r.game_day<?", (str(start), str(end))
        rows = db.execute(
            """SELECT r.* FROM nba_payout_revisions r WHERE r.revision=(SELECT MAX(s.revision)
               FROM nba_payout_revisions s WHERE s.condition_id=r.condition_id)"""
            + dates
            + " ORDER BY r.game_day,r.condition_id",
            args,
        )
        for row in rows:
            b = json.loads(row["payload"])
            state, family = row["state"], b.get("family", "unknown")
            counts[state] += 1
            scores[b.get("score_state", "unchecked")] += 1
            by_family.setdefault(family, Counter())[state] += 1
            key = b.get("venue_event_id") or row["condition_id"]
            if (
                state in ("invalid_native_evidence", "invalid_score")
                or b.get("overtime", {}).get("basis") == "conflicting_periods"
            ):
                issues.append(
                    {
                        "condition_id": b["condition_id"],
                        "venue_event_id": b.get("venue_event_id"),
                        "game_id": row["game_id"],
                        "game_day": row["game_day"],
                        "revision": row["revision"],
                        "reason": b.get("evidence_error")
                        or (
                            "conflicting_periods"
                            if b.get("overtime", {}).get("basis")
                            == "conflicting_periods"
                            else state
                        ),
                    }
                )
            for listed, include in (
                (disagreements, state == "disagrees"),
                (voids, state == "void_50_50"),
                (
                    overtime,
                    state == "match"
                    and family == "total"
                    and b.get("overtime", {}).get("occurred") is True,
                ),
            ):
                if include:
                    detail = listed.setdefault(
                        key,
                        {
                            "venue_event_id": b.get("venue_event_id"),
                            "game_id": row["game_id"],
                            "game_day": row["game_day"],
                            "conditions": [],
                        },
                    )
                    detail["conditions"].append(
                        {
                            "condition_id": b["condition_id"],
                            "family": family,
                            "line": b.get("line"),
                            "native_primary": b.get("native_primary"),
                            "score_implied_primary": b.get("score_implied_primary"),
                            "period": b.get("period"),
                            "overtime": b.get("overtime"),
                            "event_receipt": b.get("event_receipt"),
                            "event_sha256": b.get("event_sha256"),
                            "result_receipt_sha256": (b.get("score") or {}).get(
                                "receipt_sha256"
                            ),
                            "revision": row["revision"],
                        }
                    )
    if start is None and _exists(db, "nba_market_settled"):
        # A native capture may commit between scheduler ticks or precede installation of this ledger.
        resolved = db.execute("SELECT count(*) FROM nba_market_settled").fetchone()[0]
        counts["pending_reconciliation"] = max(resolved - sum(counts.values()), 0)
        scores["unchecked"] += counts["pending_reconciliation"]
    return {
        "native_conditions": sum(counts.values()),
        "score_evidence": {
            status: scores[status]
            for status in (
                "final",
                "awaiting_score",
                "unpaired",
                "invalid_score",
                "unchecked",
            )
        },
        "comparable_conditions": counts["match"] + counts["disagrees"],
        "counts": {state: counts[state] for state in STATES},
        "by_family": dict(by_family),
        "evidence_issues": issues,
        "disagreement_games": list(disagreements.values()),
        "void_games": list(voids.values()),
        "verified_overtime_total_games": len(overtime),
        "overtime_total_evidence": list(overtime.values()),
    }
