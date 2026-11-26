"""Probe C (0.4.1) — what does a fixed B1 seal now need from the two schedule sources, and when is a gap visible?

Since 0.4.1 a fixture that still carries the season file's shared placeholder time ("bulk placeholder")
is sealed at T-24h / T-60m only if the OpenLigaDB matchweek request and the Gamma event receipt are both
younger than 24 hours and agree with the registered kickoff (schedule_consensus.readiness / require_fixed).
In the published matchweek-12 readiness report two fixtures are in exactly that position (Leeds v Coventry,
Manchester United v Brentford: real kickoff equals the placeholder), and so is every fixture from round 13 on
until its real time is published.

The repository's own fixture (tests/test_season_readiness.SchedulePrerequisites) is replayed with ten
placeholder fixtures at 2026-11-28T15:00Z. Both sources confirm the kickoff once, then stop answering.

Usage: PYTHONPATH=src:tests python docs/review-evidence/code-review-0.4.1-2026-11-26/probe_fixed_seal_schedule.py
"""

from __future__ import annotations

import json
import sys
from contextlib import closing
from datetime import timedelta

from polymarket861 import football, runner, store
from polymarket861 import schedule_consensus as schedules

import test_schedule_consensus as native
import test_season_readiness as fixture

CUTOFF = native.OLD - timedelta(hours=24)


def start():
    case = fixture.SchedulePrerequisites(
        "test_fixed_b1_waits_for_bulk_placeholders_and_seals_only_the_corroborated_game")
    case.setUp()
    for n in range(9):
        home, away = f"Other Home {n}", f"Other Away {n}"
        store.register_fixture(case.root, {
            "fixture_id": football.fixture_id(native.SEASON, home, away), "season": native.SEASON,
            "round_id": native.RND, "home": home, "away": away, "kickoff": store.iso(native.OLD),
            "known_at": store.now_utc(), "source": "openfootball", "locator": "test://season-file"})
    return case


def view(case):
    report = schedules.readiness(case.root, fixture_ids={native.FID})
    row = report["fixtures"][0]
    return {"at": report["as_of"], "status": row["status"], "bulk_placeholder": row["bulk_placeholder"],
            "reason": row.get("reason"), "fixed_b1_block_reason": row["fixed_b1_block_reason"],
            "openligadb_received_at": (row["openligadb"] or {}).get("received_at"),
            "gamma_received_at": [g["received_at"] for g in row["gamma"]]}


def dispatch(case):
    summary = runner.run_due(case.root)
    mine = [j for j in summary["jobs"] if j.get("fixture_id") == native.FID and j.get("horizon") == "T-24h"]
    return {"at": summary["observed_at"], "counts": summary["counts"],
            "next_window_opens": summary["next_window_opens"],
            "this_fixture_T-24h": [{"state": j["state"], "error": j.get("error")} for j in mine]}


def sealed(case):
    with closing(store.connect(case.root)) as db:
        return [r[0] for r in db.execute(
            "SELECT horizon FROM forecast_seals WHERE fixture_id=? AND horizon!='early' ORDER BY horizon",
            (native.FID,))]


def scenario(refresh):
    """Both sources confirm at T-56h, then `refresh` decides what is fetched again 3 hours before the window."""
    case = start()
    try:
        steps = []
        case.case.time = CUTOFF - timedelta(hours=32)
        case.case.ol(native.OLD)
        case.case.gamma(native.OLD)
        steps.append({"step": "both sources confirm the kickoff (32 h before the cutoff)", **view(case)})
        case.case.time = CUTOFF - timedelta(hours=7)
        steps.append({"step": "25 h later, nothing new received (7 h before the cutoff)",
                      **view(case), "forecast_service": dispatch(case)})
        case.case.time = CUTOFF - timedelta(hours=3)
        if "openligadb" in refresh:
            case.case.ol(native.OLD)
        if "gamma" in refresh:
            case.case.gamma(native.OLD)
        steps.append({"step": f"3 h before the cutoff, refreshed: {sorted(refresh) or 'nothing'}", **view(case)})
        case.case.time = CUTOFF - timedelta(minutes=4)
        steps.append({"step": "window open (4 min before the cutoff)", "forecast_service": dispatch(case)})
        case.case.time = CUTOFF + timedelta(seconds=1)
        steps.append({"step": "1 s after the cutoff", "forecast_service": dispatch(case)})
        return {"refreshed_before_window": sorted(refresh), "fixed_seals_for_this_fixture": sealed(case),
                "steps": steps}
    finally:
        case.doCleanups()


def main():
    out = {
        "fixture": native.FID, "registered_kickoff": store.iso(native.OLD), "T-24h_cutoff": store.iso(CUTOFF),
        "max_source_age_hours": schedules.MAX_AGE.total_seconds() / 3600,
        "scenarios": [scenario(set()), scenario({"openligadb"}), scenario({"gamma"}),
                      scenario({"openligadb", "gamma"})],
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
