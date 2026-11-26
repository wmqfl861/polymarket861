"""Player weights as actually used in immutable forecasts and event revisions."""

from collections import Counter
from datetime import timedelta

from . import football, store

# Operations threshold only. It has no effect on probabilities or study eligibility.
FALLBACK_WARNING_SHARE = 0.40
LOOKAHEAD_DAYS = 7


def public_claim(item):
    history = item.get("player_importance") or {}
    chance = item.get("chance_of_playing")
    return {
        "claim_id": item.get("id"), "player_id": item.get("player_id"),
        "player": item.get("player_name"), "team": item.get("team"), "side": item.get("side"),
        "position": item.get("position"), "role": item.get("role"), "event": item.get("event"),
        "availability": item.get("availability"), "evidence_status": item.get("status"),
        "source": item.get("origin"), "source_url": item.get("url"),
        "last_observation_receipt": item.get("last_seen_receipt"),
        "source_chance_of_playing_percent": chance,
        "source_absence_probability": (1.0 - chance / 100) if type(chance) is int and 0 <= chance <= 100 else None,
        "absence_weight": item.get("absence_weight"), "duration_from": item.get("duration_from"),
        "duration_multiplier": item.get("duration_multiplier"), "duration_basis": item.get("duration_basis"),
        "duration_evidence": {k: item[k] for k in (
            "team_training_matches", "matches_in_episode", "weight_before_episode", "weight_during_episode"
        ) if k in item},
        "importance_multiplier": item.get("importance_multiplier"), "combined_weight": item.get("prior_weight"),
        "importance_basis": history.get("basis"), "fallback_reason": history.get("reason"),
        "importance_evidence": history,
        "own_log_shift": item.get("own_log_shift"), "opponent_log_shift": item.get("opponent_log_shift"),
    }


def _latest(db, fixture, at, has_events):
    from . import news_events, pipeline

    fid, kick = fixture["fixture_id"], fixture["kickoff"]
    seal = db.execute(
        """SELECT * FROM forecast_seals WHERE fixture_id=? AND kickoff=? AND sealed_at<=?
        AND mode IN ('technical','formal') AND (model_version='B1' OR model_version LIKE 'B1@%')
        AND json_extract(data_json,'$.purpose')='main' AND json_type(data_json,'$.news_pair')='object'
        ORDER BY sealed_at DESC,digest DESC LIMIT 1""", (fid, kick, at),
    ).fetchone()
    event = db.execute(
        """SELECT * FROM news_event_updates WHERE fixture_id=? AND kickoff=?
        AND json_extract(document,'$.created_at')<=? ORDER BY revision DESC LIMIT 1""", (fid, kick, at),
    ).fetchone() if has_events else None
    eligible = []
    if seal:
        doc = pipeline._verified_seal(seal)
        eligible.append((doc["news_pair"]["input_cutoff"], doc["sealed_at"], "main_seal", doc))
    if event:
        doc = news_events._verified(event)
        eligible.append((doc["news_pair"]["input_cutoff"], doc["created_at"], "event", doc))
    if not eligible:
        return None
    return max(eligible, key=lambda record: record[:3])


def current_usage(db, fixtures, at, team=None):
    """One latest prediction per scheduled fixture; repeated polls/revisions cannot inflate the rate."""
    has_events = bool(db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='news_event_updates'"
    ).fetchone())
    end = store.iso(store.instant(at) + timedelta(days=LOOKAHEAD_DAYS))
    selected_team = football.team_key(team) if team else None
    bases, reasons, policies = Counter(), Counter(), Counter()
    rows = []
    candidates = [f for f in fixtures if at < f["kickoff"] <= end and
                  (not selected_team or selected_team in {football.team_key(f[s]) for s in ("home","away")})]
    for fx in candidates:
        receipt = _latest(db, fx, at, has_events)
        if not receipt:
            continue
        cutoff, saved, kind, doc = receipt
        pair = doc["news_pair"]
        items = [item for item in pair["applied"]
                 if not selected_team or football.team_key(item.get("team", "")) == selected_team]
        for item in items:
            history = item.get("player_importance") or {}
            basis = history.get("basis") or "unrecorded"
            bases[basis] += 1
            if basis == "neutral":
                reasons[history.get("reason") or "reason_unrecorded"] += 1
        policies[pair["policy_sha256"]] += len(items)
        rows.append({"fixture_id": fx["fixture_id"], "receipt_sha256": doc["digest"], "kind": kind,
                     "input_cutoff": cutoff, "saved_at": saved, "applied": len(items),
                     "policy_sha256": pair["policy_sha256"]})
    total = sum(bases.values())
    assessed = total - bases["unrecorded"]
    share = reasons.total() / assessed if assessed else None
    return {
        "as_of": at, "lookahead_days": LOOKAHEAD_DAYS, "scheduled_fixtures": len(candidates),
        "forecasts": len(rows), "applied_claims": total, "weights_with_basis": assessed,
        "unrecorded_basis": bases["unrecorded"], "by_basis": dict(sorted(bases.items())),
        "fallback_claims": reasons.total(), "fallback_reasons": dict(sorted(reasons.items())),
        "fallback_share": share, "alert_threshold": FALLBACK_WARNING_SHARE,
        "alert": share is not None and share > FALLBACK_WARNING_SHARE,
        "by_policy": dict(sorted(policies.items())), "receipts": rows,
    }
