"""Probe A (0.4.1) — when does the minutes/starts directory stop being used, and what happens then?

0.4.1 weights each absent player by season minutes and starts (news_importance). The directory is
only trusted when every earlier league result of the club was received *before* the directory
snapshot (news_importance._directory: g["received"] > row["provider_as_of"] -> unusable), and
news_minutes.refresh stores no new snapshot while the provider's numbers are unchanged for six days.

Part 1 replays that on the repository's own fixture (tests/test_news_importance.UsageHistoryTests):
  A. baseline: directory received after all results -> minutes/starts weights
  B. one earlier result is confirmed again later (same score) -> all players fall back to weight 1.0;
     the 12-hourly re-check keeps answering "unchanged" until six days have passed
  C. a held score (as for Bournemouth v Brentford in round 4) -> neutral, even with a new directory
  D. a player with starts no longer listed (a departure) -> totals no longer reconcile -> neutral
Part 2 shows on the real result path (tests/test_result_identity.ResultIdentity) that a slow primary
feed catching up turns "repeated_independent_final" into "two_providers" and appends a new result
version whose received_at is the later time, with the score unchanged. That is the trigger in B.

Usage: PYTHONPATH=src:tests python docs/review-evidence/code-review-0.4.1-2026-11-26/probe_usage_directory.py
"""

from __future__ import annotations

import json
import sys
from contextlib import closing
from datetime import timedelta

from polymarket861 import news, news_importance, news_roster, news_sources, result_freshness, store

import test_news_importance as fixture
import test_result_identity as identity
from test_news_roster import Wiki

SEASON, TEAM, NAMES = fixture.SEASON, fixture.TEAM, fixture.NAMES
T24 = store.iso(store.instant(fixture.KICK) - timedelta(hours=24))
T60 = store.iso(store.instant(fixture.KICK) - timedelta(minutes=60))


def start():
    case = fixture.UsageHistoryTests("test_real_sync_uses_received_cumulative_minutes_and_regular_and_third_keeper")
    case.setUp()
    return case


def refresh_roster(case):
    """The season service refreshes squads every six hours; do the same as the clock moves."""
    wiki = Wiki(case.time)
    wiki.members = NAMES
    wiki.identities = {name: 100 + i for i, name in enumerate(NAMES)}

    def roster(url):
        doc = json.loads(wiki(url))
        for page in doc["query"]["pages"]:
            if page.get("title") in NAMES[:3]:
                page["categories"] = [{"ns": 14, "title": "Category:Men's association football goalkeepers"}]
        return json.dumps(doc).encode()

    return news_roster.refresh(case.root, TEAM, SEASON, getter=roster)


def injuries(case):
    """One real sync with four listed players: regular keeper, deputy, third keeper, ever-present outfielder."""
    case.time += timedelta(seconds=1)
    listed = [NAMES[0], NAMES[1], NAMES[2], NAMES[3]]
    rows = [{"player_id": NAMES.index(name) + 1, "display_name": name, "team": "Chelsea", "status": "injured",
             "category": "injury", "chance_of_playing": 0} for name in listed]
    injury_reply = fixture.reply(fixture.envelope(rows, store.now_utc()))
    return news.sync(
        case.root, team=TEAM, season=SEASON, force=True,
        defaults=lambda t, s: [news_sources.default_sources(t, s)[0]],
        roster_refresh=lambda *_: {"status": "cached"}, extractor=lambda *a, **kw: [],
        poller=lambda src, t, s, cursor: news_sources.poll(src, t, s, cursor, getter=injury_reply),
        importance_getter=fixture.reply(fixture.envelope(fixture.usage_rows(), store.now_utc())),
    )


def weights(case, at=None):
    """Weights the paired model applies at one cutoff, straight from the stored snapshot."""
    moment = at or store.iso(case.time)
    snap = news.snapshot(case.root, "next", moment)
    doc = fixture.pair(snap["facts"], moment)
    return {
        "cutoff": moment,
        "players": {p["player_name"]: {
            "basis": p["player_importance"]["basis"], "reason": p["player_importance"].get("reason"),
            "starts": p["player_importance"].get("starts"), "minutes": p["player_importance"].get("minutes"),
            "importance_multiplier": round(p["importance_multiplier"], 4),
            "absence_weight": round(p["absence_weight"], 4)} for p in doc["applied"]},
        "team_log_goal_shifts": {k: round(v, 5) for k, v in doc["log_goal_shifts"].items()},
        "excluded": doc["excluded_reasons"],
    }


def basis(case, at=None):
    history = case.history(store.instant(at) if at else None)
    return {name: (history[f"enwiki:{100 + i}"]["basis"], history[f"enwiki:{100 + i}"].get("reason"))
            for i, name in enumerate(NAMES[:4])}


def reconfirm(case, fid):
    """Append the same score again, exactly as result_admission._result does when a pairing proof changes."""
    at = store.now_utc()
    with closing(store.connect(case.root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT * FROM result_versions WHERE fixture_id=? ORDER BY version DESC LIMIT 1",
                         (fid,)).fetchone()
        kickoff = db.execute("SELECT kickoff FROM fixture_versions WHERE fixture_id=? ORDER BY known_at DESC LIMIT 1",
                             (fid,)).fetchone()[0]
        new = store.record_result_db(db, {
            "fixture_id": fid, "home_goals": old["home_goals"], "away_goals": old["away_goals"],
            "archive_at": at, "published_by": at, "finalized_by": at, "kickoff": kickoff,
            "source": "verified-result-pairing", "locator": "result-pairing:" + "0" * 64,
        }, recorded_at=at)
    return {"fixture_id": fid, "score": [new["home_goals"], new["away_goals"]],
            "old_received_at": old["received_at"], "new_version": new["version"], "new_received_at": new["received_at"]}


def part_one():
    out = {}

    # A. Baseline.
    case = start()
    try:
        sync = injuries(case)
        out["A_baseline"] = {
            "directory_capture": sync["streams"][0]["player_usage"]["state"],
            "at": store.iso(case.time), "weights": weights(case),
        }

        # B. Same score confirmed again one hour after the directory was stored.
        case.time += timedelta(hours=1)
        again = reconfirm(case, "past3")
        timeline = []
        usable_again = None
        for step in range(0, 15):
            if step:
                case.time += timedelta(hours=12)
                refresh_roster(case)
                state = case.capture(fixture.usage_rows())["state"]
            else:
                state = "(not re-checked yet)"
            first = basis(case)[NAMES[0]]
            timeline.append({"at": store.iso(case.time), "hours_after_reconfirmation": 12 * step,
                             "directory_recheck": state, "regular_keeper_basis": first[0], "reason": first[1]})
            if first[0] != "neutral" and usable_again is None:
                usable_again = store.iso(case.time)
        out["B_same_score_confirmed_again"] = {
            "reconfirmation": again,
            "weights_right_after": weights(case, store.iso(store.instant(again["new_received_at"]) + timedelta(seconds=1))),
            "at_T-24h": {"cutoff": T24, "basis": basis(case, T24)},
            "at_T-60m": {"cutoff": T60, "basis": basis(case, T60)},
            "timeline": timeline, "usable_again_at": usable_again,
            "kickoff": fixture.KICK,
        }
    finally:
        case.doCleanups()

    # C. Held score, then a fresh directory with different numbers.
    case = start()
    try:
        injuries(case)
        case.time += timedelta(hours=1)
        with closing(store.connect(case.root)) as db, db:
            db.execute("INSERT INTO result_dispositions VALUES(?,?,?,?,?)",
                       ("past1", store.iso("2026-11-04T15:00Z"), store.now_utc(), 1, "cross_source_score_conflict"))
        held = basis(case)
        case.time += timedelta(hours=13)
        rows = fixture.usage_rows()
        rows[0]["minutes"] = 270  # the provider corrects one number, so a new snapshot is stored
        state = case.capture(rows)["state"]
        out["C_held_score"] = {"after_hold": held, "new_directory_state": state, "after_new_directory": basis(case)}
    finally:
        case.doCleanups()

    # D. A listed player with starts disappears from the club page (departure).
    case = start()
    try:
        rows = [r for r in fixture.usage_rows() if r["display_name"] != NAMES[12]]
        state = case.capture(rows)["state"]
        out["D_departed_starter"] = {
            "directory_state": state, "listed_players": len(rows),
            "sum_of_starts": sum(r["starts"] for r in rows), "required": 11 * 3,
            "basis": basis(case),
        }
    finally:
        case.doCleanups()
    return out


def part_two():
    """The real reconcile path: slow primary feed, then the primary publishes the same final."""
    case = identity.ResultIdentity("test_slow_primary_needs_two_distinct_secondary_receipts_and_tracks_withdrawal")
    case.setUp()
    try:
        inner = case.case
        inner.set_time(0)
        final_row = inner.rows[0]
        unfinished = final_row | {"matchIsFinished": False, "matchResults": []}
        actual = final_row["matchDateTimeUTC"]
        inner.collect([unfinished])
        case.evidence(actual, [2, 1])
        inner.reconcile()
        inner.set_time(25)
        case.evidence(actual, [2, 1])
        inner.reconcile()
        inner.set_time(32)
        case.evidence(actual, [2, 1])
        first = inner.reconcile()

        def versions():
            with closing(store.connect(inner.root)) as db:
                return [dict(r) for r in db.execute(
                    "SELECT version,home_goals,away_goals,received_at,source FROM result_versions "
                    "WHERE fixture_id=? ORDER BY version", (case.fid,))]

        def as_of():
            with closing(store.connect(inner.root)) as db:
                row = next(r for r in db.execute(
                    result_freshness._AS_OF.format(times="result_event_times"),
                    {"season": "2026-27", "at": inner.time}) if r["fixture_id"] == case.fid)
                return row["received"]

        before = {"at": inner.time, "coverage": first["coverage"], "versions": versions(),
                  "received_used_by_importance": as_of()}
        # Twenty hours later the primary feed lists the same final (two polls, as the collector requires).
        inner.set_time(32 + 20 * 60)
        inner.collect([final_row])
        case.evidence(actual, [2, 1])
        inner.reconcile()
        inner.set_time(32 + 20 * 60 + 45)
        inner.collect([final_row])
        case.evidence(actual, [2, 1])
        second = inner.reconcile()
        after = {"at": inner.time, "coverage": second["coverage"], "versions": versions(),
                 "received_used_by_importance": as_of()}
        return {"before_primary_final": before, "after_primary_final": after}
    finally:
        case.doCleanups()


def squad_average():
    """Arithmetic only: the mean multiplier over a squad whose starts and minutes reconcile."""
    low, high = (news_importance.POLICY[k] for k in ("minimum_multiplier", "maximum_multiplier"))
    rows = {}
    for size in (25, 28, 31):
        # sum(starts/matches) == 11 exactly; sum(minutes/(90*matches)) <= 11 (11 when no minute is unaccounted).
        rows[size] = round(low + (high - low) * 11 / size, 4)
    return {"neutral_multiplier": news_importance.POLICY["neutral_multiplier"],
            "mean_multiplier_by_squad_size_upper_bound": rows}


def main():
    report = {"part_1_directory_gating": part_one(), "part_2_real_result_path": part_two(),
              "squad_average_multiplier": squad_average()}
    json.dump(report, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
