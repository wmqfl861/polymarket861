"""Point-in-time result gaps and the next round's T-60m team freshness check."""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import timedelta

from .score_bounds import INVALID_SCORE, valid_score
from . import evidence, store
from .evidence_sources import name_key
from .football import team_key

_PLAYER_FIELDS = {
    "lineup",
    "availability",
    "injury",
    "suspension",
    "card",
    "form",
    "minutes",
    "player_stats",
}

# Apply the knowledge cutoff *before* selecting the latest schedule, score, and hold.
_AS_OF = """WITH f AS (
 SELECT f.*, row_number() OVER (PARTITION BY f.fixture_id ORDER BY f.known_at DESC,f.version DESC) AS n
 FROM fixture_versions f JOIN schedule_receipts s USING(fixture_id,version)
 WHERE f.known_at<=:at AND s.recorded_at<=:at AND f.season=:season
), r AS (
 SELECT r.*,s.kickoff,row_number() OVER (PARTITION BY r.fixture_id,s.kickoff ORDER BY r.version DESC) AS n
 FROM result_versions r JOIN result_schedule_refs s USING(fixture_id,version)
 WHERE r.recorded_at<=:at AND r.received_at<=:at
), d AS (
 SELECT d.*,row_number() OVER (PARTITION BY fixture_id,kickoff ORDER BY at DESC) AS n
 FROM result_dispositions d WHERE at<=:at
) SELECT f.fixture_id,f.round_id, CASE WHEN coalesce(d.held,0)=0 THEN coalesce(t.actual_kickoff,f.kickoff) ELSE f.kickoff END AS kickoff,f.kickoff AS scheduled,f.home,f.away,
 CASE WHEN coalesce(d.held,0)=0 THEN r.home_goals END AS goals,
 CASE WHEN coalesce(d.held,0)=0 THEN r.away_goals END AS away_goals,
 CASE WHEN coalesce(d.held,0)=0 THEN r.received_at END AS received,
 CASE WHEN d.held=1 THEN d.reason END AS hold_reason
 FROM f LEFT JOIN r ON r.fixture_id=f.fixture_id AND r.kickoff=f.kickoff AND r.n=1
 LEFT JOIN {times} t ON t.fixture_id=r.fixture_id AND t.version=r.version
 LEFT JOIN d ON d.fixture_id=f.fixture_id AND d.kickoff=f.kickoff AND d.n=1 WHERE f.n=1
 ORDER BY kickoff,f.fixture_id"""


def status(root, year, at=None):
    at = store.iso(at or store.now_utc())
    now = store.instant(at)
    with closing(store.connect(root)) as db:
        has_times = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='result_event_times'"
        ).fetchone()
        times = (
            "result_event_times"
            if has_times
            else "(SELECT NULL AS fixture_id,NULL AS version,NULL AS actual_kickoff WHERE 0)"
        )
        fixtures = [
            dict(r)
            for r in db.execute(
                _AS_OF.format(times=times),
                {"season": f"{year}-{(year + 1) % 100:02d}", "at": at},
            )
        ]
        for fixture in fixtures:
            score = (fixture["goals"], fixture.pop("away_goals"))
            if score != (None, None) and not valid_score(score):
                fixture.update(goals=None, received=None, hold_reason=INVALID_SCORE, kickoff=fixture["scheduled"])
        has_facts = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='ev_facts'"
        ).fetchone()
        player_fixtures = (
            {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT fixture_id FROM ev_facts WHERE received_at<=?",
                    (at,),
                )
            }
            if has_facts
            else set()
        )
    future = [f for f in fixtures if store.instant(f["kickoff"]) > now]
    next_round = future[0]["round_id"] if future else None
    overdue = []
    by_team = defaultdict(list)
    past = defaultdict(list)
    for f in fixtures:
        if store.instant(f["kickoff"]) >= now:
            continue
        teams = (f["home"], f["away"])
        for team in teams:
            past[team_key(team)].append(f)
        if (
            f["goals"] is None
            and store.instant(f["kickoff"]) + timedelta(hours=48) < now
        ):
            overdue.append(
                {
                    "fixture_id": f["fixture_id"],
                    "round": f["round_id"],
                    "kickoff": f["kickoff"],
                    "reason": f["hold_reason"] or "no_received_result",
                }
            )
            for team in teams:
                by_team[team].append(f["fixture_id"])
    due = [
        f
        for f in fixtures
        if store.instant(f["kickoff"]) + timedelta(minutes=90) <= now
    ]
    missing = [f for f in due if f["goals"] is None]
    last_round = due[-1]["round_id"] if due else None
    due_in_round = [f for f in due if f["round_id"] == last_round]
    latest_round = {
        "round": last_round,
        "due": len(due_in_round),
        "received": sum(f["goals"] is not None for f in due_in_round),
        "scheduled": sum(f["round_id"] == last_round for f in fixtures),
        "missing_fixture_ids": [
            f["fixture_id"] for f in due_in_round if f["goals"] is None
        ],
    }
    oldest = (
        {
            "fixture_id": missing[0]["fixture_id"],
            "kickoff": missing[0]["kickoff"],
            "hours_since_kickoff": round(
                (now - store.instant(missing[0]["kickoff"])).total_seconds() / 3600, 2
            ),
            "hours_since_due": round(
                (
                    now - store.instant(missing[0]["kickoff"]) - timedelta(minutes=90)
                ).total_seconds()
                / 3600,
                2,
            ),
        }
        if missing
        else None
    )
    teams, blocked = {}, []
    for f in future:
        if f["round_id"] != next_round:
            continue
        cutoff = store.instant(f["kickoff"]) - timedelta(hours=1)
        ctx = (
            evidence.as_of_context(
                root, f["fixture_id"], f["home"], f["away"], at, prospective=True
            )
            if f["fixture_id"] in player_fixtures
            else None
        )
        for side in ("home", "away"):
            name = f[side]
            # Same last-two and strict 48h condition as make_forecast. Only results
            # already known at `at` are used when projecting the next decision.
            recent = [
                g
                for g in past[team_key(name)]
                if store.instant(g["kickoff"]) + timedelta(hours=48) < cutoff
            ][-2:]
            gaps = [g["fixture_id"] for g in recent if g["goals"] is None]
            player_facts = (
                [
                    g
                    for g in ctx["facts"]
                    if name_key(g["team"]) == name_key(name)
                    and g["field"] in _PLAYER_FIELDS
                ]
                if ctx
                else []
            )
            teams[name] = {
                "fixture_id": f["fixture_id"],
                "decision_cutoff": store.iso(cutoff),
                "recent_fixtures": [g["fixture_id"] for g in recent],
                "fixture_ids": gaps,
                "overdue_count": len(gaps),
                "critical_stale": len(gaps) >= 2,
                "players": {
                    "state": "observed" if player_facts else "unavailable",
                    "confirmed_lineup": bool(ctx and ctx["confirmed"][side]),
                    "field_counts": dict(Counter(g["field"] for g in player_facts)),
                    "latest_received_at": max(
                        (g["received_at"] for g in player_facts), default=None
                    ),
                },
            }
            if len(gaps) >= 2:
                blocked.append(f["fixture_id"])
    return {
        "state": ("stale" if overdue else "clear") if fixtures else "unavailable",
        "as_of": at,
        "latest_round": latest_round,
        "oldest_missing": oldest,
        "overdue_fixtures": overdue,
        "overdue_by_team": {
            name: {"overdue_count": len(ids), "fixture_ids": ids}
            for name, ids in by_team.items()
        },
        "next_round": next_round,
        "next_round_teams": teams,
        "next_round_blocked_fixtures": sorted(set(blocked)),
        "latest_admitted_receipt": max(
            (f["received"] for f in fixtures if f["received"]), default=None
        ),
    }
