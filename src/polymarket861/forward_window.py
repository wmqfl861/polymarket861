"""Preregister a fixed sequence of official EPL rounds, including unpublished rounds."""

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import store

PROTOCOL = "consecutive-official-epl-rounds-v1"
RESEARCH = "probability-research-24h"
FORMAL = "formal-strict"


def rounds(season, first, span):
    if not 1 <= first <= 38 or not 1 <= span <= 38:
        raise ValueError("an EPL window must name 1–38 consecutive official rounds")
    year = int(season[:4])
    result = []
    for offset in range(first - 1, first - 1 + span):
        advance, index = divmod(offset, 38)
        start = year + advance
        result.append((f"{start:04}-{(start + 1) % 100:02}", index + 1))
    return tuple(result)


def label(f):
    year = int(f["season"][:4])
    return f"{year:04}-{(year + 1) % 100:02}", int(f["round_id"])


def fixtures(schedule, season, first, span):
    wanted = set(rounds(season, first, span))
    return [f for f in schedule if label(f) in wanted]


def complete(games, labels):
    clubs = defaultdict(list)
    if len({f["fixture_id"] for f in games}) != len(games):
        return False
    for f in games:
        clubs[label(f)].extend(
            (f[side] or "").strip().casefold() for side in ("home", "away")
        )
    return set(clubs) == set(labels) and all(
        "" not in names and len(names) == len(set(names)) == 20
        for names in clubs.values()
    )


def register(season, first, span, games, stamp):
    members = defaultdict(list)
    for game in games:
        members[label(game)].extend(
            (game[side] or "").strip().casefold() for side in ("home", "away")
        )
    if len({f["fixture_id"] for f in games}) != len(games) or any(
        "" in names or len(names) > 20 or len(names) != len(set(names))
        for names in members.values()
    ):
        raise ValueError(
            "duplicate or conflicting fixture in the published official window"
        )
    first_label = rounds(season, first, span)[0]
    cut = store.instant(stamp)
    before_season = datetime(
        int(season[:4]), 8, 1, tzinfo=ZoneInfo("Europe/London")
    ) - timedelta(hours=1)
    if cut >= before_season and not complete(
        [f for f in games if label(f) == first_label], [first_label]
    ):
        raise ValueError(
            "the first decision deadline requires the full schedule for its starting round"
        )
    if any(store.instant(f["kickoff"]) - timedelta(hours=1) <= cut for f in games):
        raise ValueError("a registered decision deadline has passed")
    return {
        "protocol": PROTOCOL,
        "rounds": [list(pair) for pair in rounds(season, first, span)],
        "scheduled_at_registration": sorted(
            [*label(f), f["fixture_id"]] for f in games
        ),
        "expected_fixtures": span * 10,
    }


def audit(data_dir, plan, games):
    """The first locally received assignment fixes membership. Later kickoffs need timely receipts."""
    if not plan:
        return None
    labels = {tuple(k) for k in plan["rounds"]}
    latest = {f["fixture_id"]: label(f) for f in games}
    members = {
        fid: (season, rnd) for season, rnd, fid in plan["scheduled_at_registration"]
    }
    for fid, pair in members.items():
        if latest.get(fid) != pair:
            return "previously_registered_fixture_removed_or_reassigned"
    with closing(store.connect(data_dir)) as db:
        seasons = sorted({s for s, _ in labels})
        table = db.execute(
            "SELECT f.fixture_id,f.season,f.round_id,f.kickoff,s.precision,max(f.known_at,s.recorded_at) AS received "
            "FROM fixture_versions f JOIN schedule_receipts s USING(fixture_id,version) "
            f"WHERE f.season IN ({','.join('?' for _ in seasons)}) ORDER BY received,f.version",
            seasons,
        )
        timely = set()
        current = {f["fixture_id"]: store.iso(f["kickoff"]) for f in games}
        for row in table:
            fid, pair = row["fixture_id"], label(row)
            # Every assignment, including one subsequently removed from the schedule, is auditable.
            if pair in labels:
                if latest.get(fid) != pair:
                    return "previously_registered_fixture_removed_or_reassigned"
                if (
                    row["precision"] == "minute"
                    and row["kickoff"] == current[fid]
                    and store.instant(row["received"])
                    <= store.instant(row["kickoff"]) - timedelta(hours=1)
                ):
                    timely.add(fid)
    if latest.keys() - timely:
        return "missing_pregame_schedule_receipt"
    return None


def next_start(schedule, when):
    """Choose the next completely unstarted published official round or the next opening round."""
    cut = store.instant(when)
    grouped = defaultdict(list)
    for f in schedule:
        grouped[label(f)].append(f)
    for pair, games in sorted(grouped.items()):
        if complete(games, [pair]) and all(
            store.instant(f["kickoff"]) - timedelta(hours=1) > cut for f in games
        ):
            return pair
    year = cut.year + (cut.month >= 8)
    return f"{year:04}-{(year + 1) % 100:02}", 1
