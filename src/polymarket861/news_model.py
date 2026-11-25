"""Frozen, research-only football news hypothesis on a shared B1 score distribution."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import timedelta
import math
import re

from . import football, football_model, store

PROTOCOL = "epl-player-news-paired-v1"
LOCAL = "local_receipt"
HISTORICAL = "publication_time_reconstruction"

# Fixed before prospective outcomes. These are relative log-goal adjustments for
# a confirmed missing player versus the team's typical lineup, not measured xG.
POLICY = {
    "protocol": PROTOCOL,
    "role_log_rate_effects": {
        "forward": [-0.040, 0.005],
        "midfield": [-0.022, 0.018],
        "defence": [-0.008, 0.032],
        "goalkeeper": [0.000, 0.045],
        "unknown": [-0.012, 0.012],
    },
    "absence_weight": {
        "confirmed": {"out": 1.0, "doubtful": 0.45},
        "reported": {"out": 0.45, "doubtful": 0.20},
    },
    "max_abs_log_adjustment_per_team": 0.12,
    "max_publication_age_days": 14,
    "effect_basis": "fixed untrained positional prior; same role receives the same weight",
    "returns": "restore typical lineup, no additional positive effect",
    "evaluation": "paired four-component proper score, round-clustered, two horizons",
}
POLICY_SHA256 = store.digest(POLICY)


def _role(position):
    words = set(re.findall(r"[a-z]+", str(position or "").casefold()))
    names = {
        "goalkeeper": {"gk", "goalkeeper", "keeper"},
        "defence": {"df", "defender", "back", "centreback", "fullback", "defence"},
        "midfield": {"mf", "midfielder", "midfield"},
        "forward": {"fw", "forward", "striker", "winger", "attacker"},
    }
    matches = [name for name, aliases in names.items() if aliases & words]
    return matches[0] if len(matches) == 1 else "unknown"


def _revision_proof(row):
    proof = row.get("historical_revision")
    return (
        isinstance(proof, dict)
        and bool(proof.get("id"))
        and (
            isinstance(proof.get("url"), str)
            and proof["url"].startswith("https://")
            and re.fullmatch(r"[0-9a-f]{64}", str(proof.get("sha256", ""))) is not None
        )
    )


def _inspect(row, cutoff, clubs, basis, baseline):
    ident = row.get("id") or row.get("claim_id") or row.get("evidence_id")
    name = row.get("player_name") or row.get("name")
    item = {
        "id": ident,
        "player_id": row.get("player_id"),
        "player_name": name,
        "event": row.get("event"),
        "status": row.get("status"),
        "availability": row.get("availability"),
        "team": row.get("team"),
        "origin": row.get("origin") or row.get("source"),
        "position": row.get("position"),
        "quote": row.get("quote"),
        "url": row.get("url") or row.get("locator"),
        "cluster_id": row.get("cluster_id"),
        "authority": row.get("authority"),
    }
    if not all(
        isinstance(item[k], str) and item[k].strip()
        for k in ("id", "player_id", "team", "origin", "quote")
    ):
        return item, "missing_grounded_identity"
    if not all(
        isinstance(item[k], str) for k in ("event", "status", "availability")
    ) or (item["cluster_id"] is not None and not isinstance(item["cluster_id"], str)):
        return item, "invalid_claim"
    if row.get("fixture_id") and row["fixture_id"] != baseline.get("fixture_id"):
        return item, "different_fixture"
    side = clubs.get(football.team_key(item["team"]))
    if not side:
        return item, "different_team"
    item["side"] = side
    try:
        pub = store.instant(row["published_at"])
        effective = store.instant(row.get("effective_at") or row["published_at"])
        received = store.instant(row["received_at"])
        known = store.instant(row.get("known_at") or row["extracted_at"])
        # A publisher may republish the same state under a later timestamp.
        # Keep the earlier substantive update for ordering and require the
        # current revision to have actually existed before either type of use.
        public_at = max(pub, effective)
        if not public_at <= received <= known:
            return item, "inconsistent_claim_clocks"
        if row.get("kickoff") and store.instant(row["kickoff"]) != store.instant(
            baseline["kickoff"]
        ):
            return item, "different_kickoff"
        if (
            row.get("roster_seen_at")
            and basis == LOCAL
            and store.instant(row["roster_seen_at"]) > cutoff
        ):
            return item, "roster_unavailable_at_cutoff"
        item.update(
            published_at=store.iso(pub),
            effective_at=store.iso(effective),
            received_at=store.iso(received),
            known_at=store.iso(known),
        )
        if basis == HISTORICAL:
            if not _revision_proof(row):
                return item, "missing_historical_revision"
            item["historical_revision"] = row["historical_revision"]
        if (known if basis == LOCAL else public_at) > cutoff:
            return item, "unavailable_at_cutoff"
        if cutoff - pub > timedelta(days=POLICY["max_publication_age_days"]):
            return item, "publication_outside_window"
        deadline = row.get("expires_at") or row.get("valid_until")
        if deadline is not None and store.instant(deadline) <= cutoff:
            return item, "expired_claim"
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return item, "invalid_claim_clocks"
    if item["event"] not in {"injury", "suspension", "availability", "return"}:
        return item, "unsupported_event"
    if row.get("retracted") or item["status"] in {"retracted", "withdrawn"}:
        return item, "retracted"
    if (
        row.get("conflict")
        or row.get("conflicted")
        or row.get("disputed")
        or item["status"] in {"conflict", "contradicted", "disputed"}
    ):
        return item, "unresolved_conflict"
    if item["status"] in {"rumour", "rumor", "uncertain"}:
        return item, "unconfirmed_rumour"
    if item["status"] not in {"confirmed", "reported", "denied"}:
        return item, "unsupported_claim_status"
    if item["status"] == "confirmed" and item["authority"] != "official":
        return item, "unverified_confirmation"
    if item["status"] != "denied" and item["availability"] not in {
        "out",
        "doubtful",
        "available",
    }:
        return item, "availability_unknown"
    return item, None


def _active_claims(facts, cutoff, clubs, basis, baseline):
    by_player, excluded = defaultdict(list), []
    for fact in facts:
        if not isinstance(fact, dict):
            excluded.append({"id": None, "reason": "invalid_claim"})
            continue
        item, issue = _inspect(fact, cutoff, clubs, basis, baseline)
        if issue in (None, "retracted", "unresolved_conflict"):
            by_player[item["player_id"]].append((item, issue))
        else:
            excluded.append(item | {"reason": issue})
    chosen = []
    for _, records in sorted(by_player.items()):
        # A withdrawal invalidates all versions from that story, but does not
        # erase a separately sourced diagnosis. The snapshot normally does this
        # already; repeat the check at the point the feature enters the model.
        def cluster(item):
            return item["cluster_id"] or item["id"]

        removed = {cluster(item) for item, issue in records if issue == "retracted"}
        usable = []
        for item, issue in records:
            if cluster(item) in removed:
                excluded.append(item | {"reason": "retracted"})
            else:
                usable.append((item, issue))
        if not usable:
            continue
        reason = None
        if len({item["side"] for item, _ in usable}) != 1:
            reason = "ambiguous_club_identity"
        elif any(issue == "unresolved_conflict" for _, issue in usable):
            reason = "unresolved_conflict"
        items = [item for item, _ in usable]
        # A newer return/denial can clear an older absence. Use the source's
        # publication clock so a later receipt of an old story cannot revive it.
        clears = [
            i
            for i in items
            if i["status"] == "denied" or i["availability"] == "available"
        ]
        absences = [i for i in items if i not in clears]
        if (
            not absences
            or clears
            and max(i["effective_at"] for i in clears)
            >= max(i["effective_at"] for i in absences)
        ):
            reason = reason or "availability_restored_or_disputed"
        if reason:
            excluded.extend(i | {"reason": reason} for i in items)
            continue
        if clears:
            last_clear = max(i["effective_at"] for i in clears)
            absences = [i for i in absences if i["effective_at"] > last_clear]
        latest = max(i["effective_at"] for i in absences)
        candidates = [i for i in absences if i["effective_at"] == latest]
        if len({i["availability"] for i in candidates}) > 1:
            excluded.extend(
                i | {"reason": "conflicting_current_availability"} for i in items
            )
            continue
        item = min(candidates, key=lambda i: (i["status"] != "confirmed", i["id"]))
        chosen.append(item)
        for other in items:
            if other is not item:
                excluded.append(other | {"reason": "duplicate_or_superseded_player"})
    excluded.sort(
        key=lambda i: (str(i.get("player_id")), str(i.get("id")), i["reason"])
    )
    return chosen, excluded


def paired_prediction(
    baseline,
    facts,
    *,
    home,
    away,
    lines=None,
    cutoff=None,
    coverage=None,
    availability_basis=LOCAL,
):
    """Apply an explicitly declared candidate to the exact same pre-match base.

    The publication reconstruction option is exclusively an offline sensitivity
    experiment. The two observed local clocks are retained, never backdated.
    """
    if availability_basis not in (LOCAL, HISTORICAL):
        raise ValueError("unsupported news availability basis")
    instant = store.instant(cutoff or baseline["cutoff"])
    clubs = {football.team_key(home): "home", football.team_key(away): "away"}
    if len(clubs) != 2 or "" in clubs:
        raise ValueError("two distinct teams are required for news comparison")
    original = {key: deepcopy(baseline[key]) for key in ("probabilities", "totals")}
    if lines is None:
        lines = tuple(map(float, original["totals"]["total"]))
    facts = list(facts)
    chosen, excluded = _active_claims(
        facts, instant, clubs, availability_basis, baseline
    )
    candidate = dict(baseline, model="B3-news", base_model=baseline["model"])
    applied = []
    shifts = {"home": 0.0, "away": 0.0}
    status = "no_applicable_news"
    if chosen and (baseline["model"] != "B1" or not baseline.get("rates")):
        status = "no_team_rates"
        excluded += [item | {"reason": status} for item in chosen]
    elif chosen:
        for item in chosen:
            role = _role(item["position"])
            weight = POLICY["absence_weight"][item["status"]][item["availability"]]
            own, opponent = (v * weight for v in POLICY["role_log_rate_effects"][role])
            shifts[item["side"]] += own
            shifts["away" if item["side"] == "home" else "home"] += opponent
            applied.append(
                item
                | {
                    "role": role,
                    "prior_weight": weight,
                    "own_log_shift": own,
                    "opponent_log_shift": opponent,
                }
            )
        bound = POLICY["max_abs_log_adjustment_per_team"]
        shifts = {side: max(-bound, min(bound, v)) for side, v in shifts.items()}
        base_rates = baseline["rates"]
        rates = {k: base_rates[k] * math.exp(shifts[k]) for k in shifts}
        try:
            grid, tail = football_model._goals_matrix(
                rates["home"], rates["away"], base_rates["rho"]
            )
        except ValueError:
            status = "news_rate_outside_model"
            excluded += [item | {"reason": status} for item in applied]
            applied = []
            shifts = {"home": 0.0, "away": 0.0}
        else:
            probs, totals = football_model.probabilities(grid, lines)
            rates["rho"] = base_rates["rho"]
            candidate.update(
                matrix=grid.tolist(),
                tail_bound=tail,
                rates=rates,
                probabilities=probs,
                totals=totals,
            )
            status = "adjusted"
    changed_arm = {key: candidate[key] for key in ("probabilities", "totals")}
    pair = {
        "protocol": PROTOCOL,
        "policy_sha256": POLICY_SHA256,
        "availability_basis": availability_basis,
        "input_cutoff": store.iso(instant),
        "base_model": baseline["model"],
        "base_rates": baseline.get("rates"),
        "candidate_rates": candidate.get("rates"),
        "facts_sha256": store.digest(facts),
        "base_training_sha256": baseline.get("evidence", {}).get("input_sha256"),
        "status": status,
        "changed": changed_arm != original,
        "no_news": original,
        "with_news": changed_arm,
        "applied": applied,
        "excluded": excluded,
        "excluded_reasons": dict(
            sorted(Counter(i["reason"] for i in excluded).items())
        ),
        "log_goal_shifts": shifts,
        "coverage": coverage or {"both_teams_observed": False},
        "effect_basis": POLICY["effect_basis"],
    }
    return candidate, pair


def make_pair(
    baseline,
    facts,
    *,
    home,
    away,
    lines=None,
    cutoff=None,
    coverage=None,
    availability_basis=LOCAL,
):
    return paired_prediction(
        baseline,
        facts,
        home=home,
        away=away,
        lines=lines,
        cutoff=cutoff,
        coverage=coverage,
        availability_basis=availability_basis,
    )[1]
