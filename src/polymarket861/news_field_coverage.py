"""Describe the Touchline facts retained at each selected primary forecast cutoff."""

from collections import Counter
import json

from . import football, news_scope, news_sources, news_suspension_audit, store


def _timestamp(value):
    if isinstance(value, str):
        try:
            return store.iso(value)
        except (ValueError, OverflowError):
            pass
    return None


def _coverage(record, side, snapshot):
    club = record[side]
    side_data = snapshot.get("coverage", {}).get(side) or {}
    if not isinstance(side_data, dict) or not isinstance(
        side_data.get("sources"), list
    ):
        return "unobserved", None
    source = next(
        (
            s
            for s in side_data["sources"]
            if isinstance(s, dict) and s.get("id") == news_sources.WIRE
        ),
        {},
    )
    proof = source.get("coverage_evidence")
    checked, observed, provider = (
        _timestamp(source.get(k))
        for k in ("checked_at", "source_observed_at", "provider_state_as_of")
    )
    if (
        not all(isinstance(t, str) for t in (checked, observed, provider))
        or not provider <= observed <= checked <= record["input_cutoff"]
        or source.get("recorded_kickoff") != record["kickoff"]
        or not isinstance(side_data.get("team"), str)
        or football.team_key(side_data["team"]) != football.team_key(club)
        or not isinstance(proof, dict)
        or proof.get("source") != news_sources.WIRE
        or proof.get("club") != football.team_key(club)
        or proof.get("season") != record["season"]
        or proof.get("competition") != news_scope.competition(record)
    ):
        return "unobserved", None
    if (
        source.get("status") in {"ok", "degraded"}
        and source.get("availability_observed") is True
        and source.get("fetch_complete") is True
        and not any(
            source.get(k)
            for k in (
                "pending_entries",
                "unmatched_players",
                "invalid_entries",
                "rejected_items",
                "revision_conflicts",
            )
        )
    ):
        return "observed", observed
    if source.get("status") in {"partial", "ok", "degraded"}:
        return "partial", observed
    return "unobserved", None


def _players(record, snapshot, witnesses, decisions):
    """Count one club, player and event per fixture and horizon, using its latest witness."""
    selected = {}
    clubs = {football.team_key(record["home"]), football.team_key(record["away"])}
    for fact in snapshot.get("facts", []):
        if (
            not isinstance(fact, dict)
            or fact.get("source") != news_sources.WIRE
            or not isinstance(fact.get("team"), str)
        ):
            continue
        event, pid, club = (
            fact.get("event"),
            fact.get("player_id"),
            football.team_key(fact["team"]),
        )
        at, known = (
            _timestamp(fact.get("last_seen_at")),
            _timestamp(fact.get("known_at")),
        )
        if (
            not isinstance(fact.get("id"), str)
            or not fact["id"]
            or not isinstance(pid, str)
            or not pid
        ):
            continue
        decision = decisions.get(fact.get("id"))
        # A failed later refresh can make the team coverage incomplete while a
        # timely individual observation still appears in the model's actual decision.
        positive = (
            club not in witnesses
            and decision in {"applied", "suspension_competition_unverified"}
            and fact.get("last_seen_receipt")
        )
        if (
            event not in {"suspension", "injury"}
            or not at
            or not known
            or at > record["input_cutoff"]
            or (at != witnesses.get(club) and not positive)
            or club not in clubs
            or fact.get("fixture_id") != record["fixture_id"]
            or fact.get("kickoff") != record["kickoff"]
            or known > record["input_cutoff"]
            or fact.get("retracted")
            or fact.get("status") in {"withdrawn", "retracted"}
        ):
            continue
        provider_id = fact.get("provider_player_id")
        key = (club, str(provider_id) if provider_id is not None else pid, event)
        rank = (at, known, _timestamp(fact.get("effective_at")) or "", fact["id"])
        if key not in selected or rank > selected[key][0]:
            selected[key] = (rank, fact)

    for key in sorted(selected):
        fact = selected[key][1]
        # Earlier parsers did not retain these optional fields. A missing key in an
        # old projection cannot show that the publisher omitted it from the response.
        field_capable = fact.get("model_version") == "touchline-availability-fields-v2"
        event = fact["event"]
        if event == "suspension":
            value = fact.get("suspension_competition")
            field = "suspension_competition" if value else None
        else:
            field = next(
                (k for k in ("injury_started_at", "injury_date") if fact.get(k)), None
            )
            value = fact.get(field)
        yield {
            "fixture_id": record["fixture_id"],
            "team": fact["team"],
            "player_id": fact["player_id"],
            "provider_player_id": fact.get("provider_player_id"),
            "player": fact.get("player_name"),
            "event": event,
            "source": news_sources.WIRE,
            "status": fact.get("status"),
            "known_at": fact["known_at"],
            "observed_at": fact["last_seen_at"],
            "input_cutoff": record["input_cutoff"],
            "claim_id": fact["id"],
            "decision": decisions.get(fact["id"], "not_selected_individually"),
            "field_status": "supplied"
            if field
            else "missing"
            if field_capable
            else "not_verifiable",
            "field": field,
            "value": value,
        }


def summarize(db, records, expected, *, details=True):
    """Only read immutable seals already selected and validated by the parent evaluation."""
    sealed = {r["seal"]: r for r in records if r["sealed"]}
    sides = Counter()
    players = []
    suspension_fixtures = []
    if sealed:
        args = ",".join("?" for _ in sealed)
        query = f"""SELECT digest, json_extract(data_json,'$.context.player_news'),
                    json_extract(data_json,'$.news_pair.applied'), json_extract(data_json,'$.news_pair.excluded')
                    FROM forecast_seals WHERE digest IN ({args}) ORDER BY fixture_id"""
        for digest, raw, applied, excluded in db.execute(query, tuple(sealed)):
            record = sealed[digest]
            snapshot = json.loads(raw or "{}")
            witnesses = {}
            complete_wire_sides = set()
            for side in ("home", "away"):
                state, at = _coverage(record, side, snapshot)
                sides[state] += 1
                if state == "observed":
                    complete_wire_sides.add(side)
                if at:
                    witnesses[football.team_key(record[side])] = at
            applied, excluded = json.loads(applied), json.loads(excluded)
            suspension_fixtures.append(
                news_suspension_audit.fixture(
                    record, snapshot, applied, excluded, complete_wire_sides
                )
            )
            decisions = {a.get("id"): a.get("reason") for a in excluded}
            decisions.update((a.get("id"), "applied") for a in applied)
            players.extend(_players(record, snapshot, witnesses, decisions))
    complete, partial = sides["observed"], sides["partial"]
    # A complete team poll can establish an empty observed sample. A partial one
    # with no matched players cannot, and no poll says nothing about its team.
    injuries = [r for r in players if r["event"] == "injury"]
    suspensions = [r for r in players if r["event"] == "suspension"]
    excluded = [
        r for r in suspensions if r["decision"] == "suspension_competition_unverified"
    ]
    suspension_counts = {
        "observed": len(suspensions),
        "scope_excluded": len(excluded),
        "scope_excluded_without_named_competition": sum(
            r["field_status"] == "missing" for r in excluded
        ),
        "scope_excluded_with_other_or_unrecognized_competition": sum(
            bool(r["value"]) for r in excluded
        ),
        "scope_excluded_field_support_unverified": sum(
            r["field_status"] == "not_verifiable" for r in excluded
        ),
        "field_support_unverified": sum(
            r["field_status"] == "not_verifiable" for r in suspensions
        ),
        "applied": sum(r["decision"] == "applied" for r in suspensions),
    }
    injury_counts = {
        "observed": len(injuries),
        "field_verifiable": sum(
            r["field_status"] != "not_verifiable" for r in injuries
        ),
        "with_native_onset": sum(r["field_status"] == "supplied" for r in injuries),
        "with_injury_started_at": sum(
            r["field"] == "injury_started_at" for r in injuries
        ),
        "with_injury_date_only": sum(r["field"] == "injury_date" for r in injuries),
        "no_native_onset": sum(r["field_status"] == "missing" for r in injuries),
        "field_support_unverified": sum(
            r["field_status"] == "not_verifiable" for r in injuries
        ),
    }
    status = "complete" if complete == expected * 2 else "partial"
    if not complete and not players:
        status = "partial" if partial else "unobserved"
    else:
        if players and all(r["field_status"] == "not_verifiable" for r in players):
            status = "legacy_fields_unverified"
        if (
            status == "legacy_fields_unverified"
            or injuries
            and not injury_counts["field_verifiable"]
        ):
            for key in (
                "with_native_onset",
                "with_injury_started_at",
                "with_injury_date_only",
                "no_native_onset",
            ):
                injury_counts[key] = None
        if not excluded and (
            status == "legacy_fields_unverified"
            or suspensions
            and suspension_counts["field_support_unverified"] == len(suspensions)
        ):
            for key in (
                "scope_excluded",
                "scope_excluded_without_named_competition",
                "scope_excluded_with_other_or_unrecognized_competition",
                "scope_excluded_field_support_unverified",
            ):
                suspension_counts[key] = None
    if not complete and not suspensions:
        suspension_counts = dict.fromkeys(suspension_counts)
    if not complete and not injuries:
        injury_counts = dict.fromkeys(injury_counts)
    return {
        "all_source_suspensions": news_suspension_audit.summarize(
            suspension_fixtures, expected, details=details
        ),
        "source": news_sources.WIRE,
        "status": status,
        "basis": "latest_grounded_wire_facts_in_selected_seals",
        "expected_team_fixtures": expected * 2,
        "sealed_team_fixtures": len(sealed) * 2,
        "observed_team_fixtures": complete,
        "partial_team_fixtures": partial,
        "unobserved_team_fixtures": expected * 2 - complete - partial,
        "suspensions": suspension_counts,
        "injuries": injury_counts,
        **({"players": players} if details else {}),
    }
