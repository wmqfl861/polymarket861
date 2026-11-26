"""Count scope decisions actually recorded by the selected forecast across publishers."""

from collections import defaultdict

from . import football, store


# This released probability policy checked the target competition of a ban.
# Older policies can use the same protocol name without that rule. A newer
# format can also prove it from an explicit scope disposition on the seal.
_SCOPED_POLICY = "461ddec6ef376781c14c44b4b839cc1700637da903163d11e3b9760a7595f387"
_SCOPE_EXCLUSION = "suspension_competition_unverified"


def _at_cutoff(value, cutoff):
    try:
        return isinstance(value, str) and store.instant(value) <= cutoff
    except (ValueError, OverflowError):
        return False


def fixture(record, snapshot, applied, excluded):
    cutoff = store.instant(record["input_cutoff"])
    clubs = {football.team_key(record[s]) for s in ("home", "away")}
    registered, observed = set(), set()
    for side in ("home", "away"):
        coverage = (snapshot.get("coverage") or {}).get(side) or {}
        for source in coverage.get("sources") or []:
            if not isinstance(source, dict) or not isinstance(source.get("id"), str):
                continue
            registered.add(source["id"])
            if (source.get("availability_observed") is True and source.get("status") in {"ok", "degraded"}
                    and source.get("recorded_kickoff") == record["kickoff"]
                    and _at_cutoff(source.get("checked_at"), cutoff)
                    and _at_cutoff(source.get("source_observed_at"), cutoff)):
                observed.add(source["id"])

    facts = {r["id"]: r for r in snapshot.get("facts", []) if isinstance(r, dict) and isinstance(r.get("id"), str)}
    scoped = record.get("policy_sha256") == _SCOPED_POLICY
    decisions = []
    judgments = [(r, "applied") for r in applied]
    judgments.extend((r, r.get("reason")) for r in excluded)
    for item, outcome in judgments:
        fact = facts.get(item.get("id")) or {}
        if (item.get("event") or fact.get("event")) != "suspension":
            continue
        pid, team = fact.get("player_id"), fact.get("team")
        source, known = fact.get("source"), fact.get("known_at")
        if (not isinstance(pid, str) or not pid or not isinstance(team, str)
                or football.team_key(team) not in clubs or not isinstance(source, str) or not source
                or fact.get("fixture_id") != record["fixture_id"] or fact.get("kickoff") != record["kickoff"]
                or not _at_cutoff(known, cutoff) or not outcome):
            continue
        if outcome == _SCOPE_EXCLUSION or "suspension_competition" in item:
            scoped = True
        registered.add(source)
        decisions.append({"fixture_id": record["fixture_id"], "team": team, "player_id": pid,
                          "player": fact.get("player_name") or item.get("player_name"), "source": source,
                          "origin": fact.get("origin"), "status": fact.get("status"), "claim_id": fact["id"],
                          "known_at": known, "input_cutoff": record["input_cutoff"], "decision": outcome})
    return {"scoped": scoped, "registered": registered, "observed": observed, "decisions": decisions}


def summarize(fixtures, expected, *, details):
    qualified = [f for f in fixtures if f["scoped"] and (f["observed"] or f["decisions"])]
    rows = [r for f in qualified for r in f["decisions"]]
    by_player = defaultdict(list)
    for r in rows:
        by_player[(r["fixture_id"], football.team_key(r["team"]), r["player_id"])].append(r)
    scope_players = []
    for key in sorted(by_player):
        judged = by_player[key]
        exclusions = {r["claim_id"]: r for r in judged if r["decision"] == _SCOPE_EXCLUSION}
        if exclusions:
            first = next(iter(exclusions.values()))
            scope_players.append({k: first[k] for k in ("fixture_id", "team", "player_id", "player", "input_cutoff")}
                                 | {"also_applied": any(r["decision"] == "applied" for r in judged),
                                    "claims": [exclusions[k] for k in sorted(exclusions)]})
    by_source = {}
    for source in sorted({source for f in fixtures for source in f["registered"]}):
        groups = [[r for r in group if r["source"] == source] for group in by_player.values()]
        represented = sum(source in f["observed"] or any(r["source"] == source for r in f["decisions"]) for f in qualified)
        by_source[source] = {
            "fixtures_with_evidence": represented,
            "evaluated_players": sum(bool(group) for group in groups) if represented else None,
            "scope_excluded_players": sum(any(r["decision"] == _SCOPE_EXCLUSION for r in group) for group in groups) if represented else None,
        }
    return {
        "expected_fixtures": expected, "sealed_fixtures": len(fixtures), "fixtures_with_evidence": len(qualified),
        "scope_format_unverified_fixtures": sum(not f["scoped"] for f in fixtures),
        "evaluated_players": len(by_player) if qualified else None,
        "scope_excluded_players": len(scope_players) if qualified else None,
        "also_applied_from_another_claim": sum(p["also_applied"] for p in scope_players) if qualified else None,
        "by_source": by_source,
        **({"excluded_players": scope_players} if details else {}),
    }
