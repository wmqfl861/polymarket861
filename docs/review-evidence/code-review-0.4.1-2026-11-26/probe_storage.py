"""Probe D (0.4.1) — what still grows on every poll when nothing changes, and where?

0.4.1 stopped re-archiving unchanged availability rows (0.4.0 finding N1-1). Each poll still leaves a
check row and a poll proof so that "seen again, unchanged" can be proven later. This probe polls one
club's availability stream at the scheduler's own 20-minute cadence with only the provider timestamp
moving, then attributes the SQLite growth to tables with the dbstat virtual table.

Usage: PYTHONPATH=src:docs/review-evidence/code-review-0.4.0-2026-11-25 python docs/review-evidence/code-review-0.4.1-2026-11-26/probe_storage.py [--listed 4] [--polls 144]
(probe_common.py is the shared scaffolding of the 0.4.0 review probes)
"""

from __future__ import annotations

import argparse
import json
import tempfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from polymarket861 import news, news_roster, news_sources, store

import probe_common as pc


def table_bytes(root):
    with closing(store.connect(root)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        sizes = dict(db.execute("SELECT name,sum(pgsize) FROM dbstat GROUP BY name"))
        owner = {name: table for name, table in db.execute(
            "SELECT name,tbl_name FROM sqlite_master WHERE type IN ('table','index')")}
        rows = {}
        for table in {t for t in owner.values() if t.startswith("news_")}:
            rows[table] = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    by_table = {}
    for name, size in sizes.items():
        by_table[owner.get(name, name)] = by_table.get(owner.get(name, name), 0) + size
    return by_table, rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listed", type=int, default=4)
    parser.add_argument("--polls", type=int, default=144)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "state"
        clock = pc.Clock("2026-11-25T12:00:00Z")
        with patch.object(store, "now_utc", side_effect=clock):
            club = "Chelsea FC"
            pc.register(root, clock, "epl/2026-27/nottingham-forest/chelsea",
                        "Nottingham Forest FC", club, "2026-12-05T15:00:00Z")
            members = pc.squad("Chelsea")
            names = [f"{first} {last}" for first, last in zip(
                "Aldo Bram Cato Dario Emil Faro Gino Hugo Ivo Jano Kofi Luca Milo Nico Otto Pavel Quin Rafa Sami Tomas Ugo Vito Wim Xavi Yann".split(),
                "Ashdown Birch Calder Denholm Eastwick Farrow Garside Holloway Ingram Jessop Kirkby Lambert Mossop Norcross Oakes Pym Quarmby Rudd Skelton Thwaite Unsworth Varley Whitlock Yardley Zelley".split())]
            members = dict(zip(names, members.values()))
            news_roster.refresh(root, club, pc.SEASON, getter=pc.FakeWiki(clock, members, 5000))
            source = news_sources.default_sources(club, pc.SEASON)[0]
            listed = [(100 + i, name, "injured", "injury", 0) for i, name in enumerate(names[: args.listed])]

            def poller(src, team, season, cursor):
                at = clock()
                return news_sources.poll(
                    src, team, season, cursor,
                    getter=lambda url: pc.reply(pc.availability_payload("Chelsea", listed, at), at, url))

            marks = {}
            attempted = 0
            for number in range(1, args.polls + 1):
                out = news.sync(root, team=club, defaults=lambda t, s: [source], poller=poller,
                                roster_refresh=lambda *_: {"status": "cached"},
                                importance_getter=lambda url: (_ for _ in ()).throw(OSError("offline probe")))
                attempted += out["attempted"]
                if number in (args.polls // 2, args.polls):
                    files, size = pc.disk_usage(root)
                    marks[number] = (*table_bytes(root), files, size)
                clock.advance(minutes=20, seconds=1)
        (b0, r0, f0, s0), (b1, r1, f1, s1) = marks[args.polls // 2], marks[args.polls]
        span = args.polls - args.polls // 2
        growth = {t: (b1.get(t, 0) - b0.get(t, 0)) / span for t in sorted(set(b0) | set(b1))
                  if b1.get(t, 0) != b0.get(t, 0)}
        rows = {t: (r1[t] - r0.get(t, 0)) / span for t in sorted(r1) if r1[t] != r0.get(t, 0)}
        per_poll = (s1 - s0) / span
        streams, polls_per_day = 60, 72
        print(json.dumps({
            "listed_players": args.listed, "polls": args.polls, "stream_polls_attempted": attempted,
            "window": f"polls {args.polls // 2 + 1}..{args.polls}",
            "new_blob_files_in_window": f1 - f0,
            "rows_added_per_poll": rows,
            "sqlite_bytes_added_per_poll_by_table": growth,
            "disk_bytes_added_per_poll": per_poll,
            "extrapolation": {
                "assumption": f"{streams} default streams (20 clubs x 3 sources) x {polls_per_day} polls a day, every "
                              "stream growing like this availability stream",
                "mib_per_day": streams * polls_per_day * per_poll / 2**20,
                "gib_per_280_day_season": 280 * streams * polls_per_day * per_poll / 2**30,
            },
        }, indent=1))


if __name__ == "__main__":
    main()
