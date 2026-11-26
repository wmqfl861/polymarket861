"""Frozen, research-only football news hypothesis on a shared B1 score distribution."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import timedelta
import math
import re

from . import football, football_model, news_importance, news_scope, store
from .news import COVERAGE_AGE

PROTOCOL = "epl-player-news-paired-v3"
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
    "wire_source": "touchline-availability",
    "wire_absence_probability": "reported.out * (1 - chance_of_playing / 100)",
    "wire_max_age_hours": COVERAGE_AGE / timedelta(hours=1),
    "duration": {
        "basis": "fraction of team training-match weight preceding the verified native injury onset or first observed absence",
        "decay_per_day": football_model.DECAY_PER_DAY,
        "cohort": "exact eligible B1 matches for this club; a match counts as exposed only after observed onset",
        "unavailable": "retain full duration factor and label unavailable training exposure",
    },
    "importance": news_importance.POLICY,
    "effect_basis": "fixed untrained positional effect times absence probability, duration and observed minutes/starts",
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


def _wire(row):
    return row.get("source") == POLICY["wire_source"] and row.get("time_basis") == "provider_snapshot"


def duration_evidence(team_kickoffs, onset):
    """Approximate remaining effect by the fraction of B1's weight preceding the episode.

    The time anchor cancels in the ratio. Use the latest club kickoff (as in
    B1, up to a common factor) so repeated polls leave the estimate identical.
    """
    if not team_kickoffs:
        return {"duration_multiplier": 1.0, "duration_basis": "no_training_match_exposure",
                "team_training_matches": 0, "matches_in_episode": 0}
    last = max(team_kickoffs)
    start = onset.timestamp()
    before, after = [], []
    for played in team_kickoffs:
        weight = math.exp(-football_model.DECAY_PER_DAY * (last - played) / 86400)
        (after if played > start else before).append(weight)
    before_sum, after_sum = math.fsum(before), math.fsum(after)
    return {
        "duration_multiplier": before_sum / (before_sum + after_sum),
        "duration_basis": "b1_weighted_training_matches", "team_training_matches": len(team_kickoffs),
        "matches_in_episode": len(after), "weight_before_episode": before_sum, "weight_during_episode": after_sum,
    }


def _timing(row, cutoff, basis, pub, received, training_kickoffs=None):
    # Text bulletins have no continuing provider presence. Their original clocks
    # and publication window apply. Offline revision reconstructions retain public time.
    onset = received if basis == LOCAL else pub
    details = {}
    if _wire(row):
        if basis != LOCAL:
            return details, "missing_local_wire_observation"
        first = store.instant(row.get("first_seen_at") or received)
        onset = store.instant(row.get("absence_first_seen_at") or first)
        last = store.instant(row.get("last_seen_at") or received)
        publisher = store.instant(row.get("last_seen_state_as_of") or row.get("observed_state_as_of") or pub)
        if not onset <= first <= last <= cutoff or not pub <= publisher <= last or received > last:
            return details, "invalid_wire_observation_clocks"
        details.update(
            first_seen_at=store.iso(first), absence_first_seen_at=store.iso(onset),
            last_seen_at=store.iso(last), last_seen_state_as_of=store.iso(publisher),
            last_seen_receipt=row.get("last_seen_receipt") or row.get("source_receipt"),
        )
        if cutoff - min(last, publisher) > timedelta(hours=POLICY["wire_max_age_hours"]):
            return details, "stale_wire_observation"
        chance = row.get("chance_of_playing")
        if chance is not None and (type(chance) is not int or not 0 <= chance <= 100):
            return details, "invalid_playing_probability"
        details["chance_of_playing"] = chance
        if row.get("event") == "injury":
            # Only this edition's first publication and local receipt can disclose a
            # retrospective medical date. Neither moves the eligibility clock.
            if row.get("injury_started_at") is not None:
                onset = store.instant(row["injury_started_at"])
                if onset > pub:
                    return details, "invalid_native_injury_onset"
                details["injury_onset_basis"] = "provider_injury_started_at"
            elif row.get("injury_date") is not None:
                onset = news_scope.injury_date_upper_bound(row["injury_date"], pub)
                details["injury_onset_basis"] = "provider_injury_date_upper_bound"
    elif cutoff - pub > timedelta(days=POLICY["max_publication_age_days"]):
        return details, "publication_outside_window"
    details["duration_from"] = store.iso(onset)
    details.update(duration_evidence((training_kickoffs or {}).get(football.team_key(row.get("team", ""))), onset))
    return details, None


def next_weight_review(row, cutoff):
    """First source-freshness deadline. Duration changes when training results arrive."""
    cut = store.instant(cutoff)
    try:
        received = store.instant(row["received_at"])
        deadlines = []
        if _wire(row):
            last = store.instant(row.get("last_seen_at") or received)
            published = store.instant(row.get("last_seen_state_as_of") or row.get("observed_state_as_of") or row["published_at"])
            deadlines.append(min(last, published) + timedelta(hours=POLICY["wire_max_age_hours"], microseconds=1))
        history = row.get("player_importance") or {}
        if history.get("basis") == "epl_season_minutes_and_starts":
            deadlines.append(store.instant(history["provider_as_of"]) +
                             timedelta(days=news_importance.POLICY["directory_max_age_days"], microseconds=1))
        return min((d for d in deadlines if d > cut), default=None)
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        return None


def usable_wire_observation(row, cutoff):
    """Keep a stale/inconsistent standing assertion out of other publishers' conflict decisions."""
    try:
        if not _wire(row):
            return False
        pub, received = store.instant(row["published_at"]), store.instant(row["received_at"])
        _, issue = _timing(row, cutoff, LOCAL, pub, received)
        return issue is None
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        return False


def _inspect(row, cutoff, clubs, basis, baseline, training_kickoffs=None):
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
    if row.get("source") == POLICY["wire_source"]:
        if not _wire(row):
            return item, "missing_local_wire_observation"
        year = cutoff.year - (cutoff.month < 7)
        season = baseline.get("season") or row.get("season") or f"{year}-{(year + 1) % 100:02}"
        if not news_scope.wire_coverage(row["team"], season, news_scope.competition(baseline), cutoff):
            return item, "source_not_covered"
    if news_scope.competition(baseline) != "epl":
        return item, "league_news_not_validated"
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
        timing, issue = _timing(row, cutoff, basis, pub, received, training_kickoffs)
        item.update(timing)
        if issue:
            return item, issue
        if row.get("source") == POLICY["wire_source"]:
            if not _wire(row):
                return item, "invalid_wire_observation_clocks"
            # Pre-v2 wire archives can label a player with a nonzero supplied
            # probability as unknown. Recover the factual field at this cutoff.
            chance = item.get("chance_of_playing")
            if chance is not None and item["event"] in {"injury", "suspension"}:
                item["availability"] = "available" if chance == 100 else "out" if chance == 0 else "doubtful"
        deadline = row.get("expires_at") or row.get("valid_until")
        if deadline is not None and store.instant(deadline) <= cutoff:
            return item, "expired_claim"
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return item, "invalid_claim_clocks"
    if item["event"] not in {"injury", "suspension", "availability", "return"}:
        return item, "unsupported_event"
    if not news_scope.suspension_applies(row, news_scope.competition(baseline)):
        item["suspension_competition"] = row.get("suspension_competition")
        return item, "suspension_competition_unverified"
    if item["event"] == "suspension":
        item["suspension_competition"] = row.get("suspension_competition") or news_scope.competition(baseline)
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
    if item["status"] == "confirmed" and (item["authority"] != "official" or _wire(row)):
        return item, "unverified_confirmation"
    if item["status"] != "denied" and item["availability"] not in {
        "out",
        "doubtful",
        "available",
    }:
        return item, "availability_unknown"
    item["importance_multiplier"], item["player_importance"] = news_importance.assess(
        row, cutoff, local=basis == LOCAL
    )
    return item, None


def _supported_absence(absences):
    # Keep each origin's current availability and still-valid support for it.
    # An agreeing wire update cannot downgrade a club's own confirmation.
    by_origin = defaultdict(list)
    for item in absences:
        by_origin[item["origin"]].append(item)
    support = []
    for origin in by_origin.values():
        last = max(i["effective_at"] for i in origin)
        latest = [i for i in origin if i["effective_at"] == last]
        available = {i["availability"] for i in latest}
        if len(available) != 1:
            support.extend(latest)
            continue
        changed = max(
            (i["effective_at"] for i in origin if i["availability"] not in available),
            default="",
        )
        support.extend(i for i in origin if i["effective_at"] > changed)
    # A new club confirmation can resolve older disagreement. A later
    # contradictory independent report keeps the player's state disputed.
    official = max(
        (i["effective_at"] for i in support if i["status"] == "confirmed"), default=""
    )
    current = [i for i in support if i["effective_at"] >= official]
    if len({i["availability"] for i in current}) != 1:
        return None
    strongest = [i for i in current if i["status"] == "confirmed"] or current
    last = max(i["effective_at"] for i in strongest)
    item = min(
        (i for i in strongest if i["effective_at"] == last), key=lambda i: i["id"]
    )
    # A freshly published club confirmation can corroborate a longstanding
    # continuously observed injury. Keep its official confidence and the
    # corroborating episode's original duration, including a fully absorbed injury.
    episode = min(
        (i for i in support if i["event"] == item["event"] and i["availability"] == item["availability"]),
        key=lambda i: (i["duration_from"], i["id"]),
    )
    if episode["duration_from"] < item["duration_from"]:
        item.update({k: episode[k] for k in ("duration_from", "duration_multiplier", "duration_basis",
                    "team_training_matches", "matches_in_episode", "weight_before_episode", "weight_during_episode",
                    "injury_onset_basis") if k in episode})
        item["duration_evidence_id"] = episode["id"]
    return item


def _active_claims(facts, cutoff, clubs, basis, baseline, training_kickoffs=None):
    by_player, excluded = defaultdict(list), []
    for fact in facts:
        if not isinstance(fact, dict):
            excluded.append({"id": None, "reason": "invalid_claim"})
            continue
        item, issue = _inspect(fact, cutoff, clubs, basis, baseline, training_kickoffs)
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
        item = _supported_absence(absences)
        if item is None:
            excluded.extend(
                i | {"reason": "conflicting_current_availability"} for i in items
            )
            continue
        if item["duration_multiplier"] == 0:
            excluded.append(item | {"reason": "absence_spans_all_team_training_matches"})
        else:
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
    training_kickoffs=None,
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
        facts, instant, clubs, availability_basis, baseline, training_kickoffs
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
            chance = item.get("chance_of_playing")
            confidence = POLICY["absence_weight"][item["status"]][item["availability"]]
            if chance is not None:
                confidence = POLICY["absence_weight"][item["status"]]["out"] * (1 - chance / 100)
            weight = confidence * item["duration_multiplier"] * item["importance_multiplier"]
            own, opponent = (v * weight for v in POLICY["role_log_rate_effects"][role])
            shifts[item["side"]] += own
            shifts["away" if item["side"] == "home" else "home"] += opponent
            applied.append(
                item
                | {
                    "role": role,
                    "prior_weight": weight,
                    "absence_weight": confidence,
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
        "max_abs_log_adjustment_per_team": POLICY["max_abs_log_adjustment_per_team"],
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
    training_kickoffs=None,
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
        training_kickoffs=training_kickoffs,
    )[1]
