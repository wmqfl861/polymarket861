"""Probe A — how much does one club's availability stream write per poll when nothing changes?

The Touchline availability adapter treats every new provider snapshot timestamp as a new
revision of every listed player (news_sources._wire_availability). The 0.4.0 live report shows
snapshot timestamps within ~0.1 s of local receipt, i.e. a new timestamp on every response.
This probe replays that situation offline: the same N players, unchanged status, polled at the
scheduler's own 20-minute cadence, with only meta.timestamp_iso moving.

Usage: PYTHONPATH=src python probe_growth.py [--listed 4] [--polls 9]
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from polymarket861 import news, news_roster, news_sources, store

import probe_common as pc

TABLES = [
    "news_articles", "news_article_proofs", "news_extractions", "news_analysis_runs",
    "news_claims", "news_claim_runs", "news_checks", "news_poll_proofs", "observations",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listed", type=int, default=4, help="players on the provider's list")
    parser.add_argument("--polls", type=int, default=9)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "state"
        clock = pc.Clock("2026-11-25T12:00:00Z")
        with patch.object(store, "now_utc", side_effect=clock):
            club = "Chelsea FC"
            pc.register(root, clock, "epl/2026-27/nottingham-forest/chelsea",
                        "Nottingham Forest FC", club, "2026-11-28T15:00:00Z")
            members = pc.squad("Chelsea")
            names = [f"{first} {last}" for first, last in zip(
                "Aldo Bram Cato Dario Emil Faro Gino Hugo Ivo Jano Kofi Luca Milo Nico Otto Pavel Quin Rafa Sami Tomas Ugo Vito Wim Xavi Yann".split(),
                "Ashdown Birch Calder Denholm Eastwick Farrow Garside Holloway Ingram Jessop Kirkby Lambert Mossop Norcross Oakes Pym Quarmby Rudd Skelton Thwaite Unsworth Varley Whitlock Yardley Zelley".split())]
            members = dict(zip(names, members.values()))
            report = news_roster.refresh(root, club, pc.SEASON, getter=pc.FakeWiki(clock, members, 5000))
            assert report["status"] in ("updated", "ok"), report
            source = news_sources.default_sources(club, pc.SEASON)[0]
            listed = [(100 + i, name, "injured", "injury", 0) for i, name in enumerate(names[: args.listed])]

            def poller(src, team, season, cursor):
                at = clock()
                return news_sources.poll(
                    src, team, season, cursor,
                    getter=lambda url: pc.reply(pc.availability_payload("Chelsea", listed, at), at, url),
                )

            rows = []
            previous = None
            for number in range(1, args.polls + 1):
                out = news.sync(root, team=club, defaults=lambda t, s: [source], poller=poller,
                                roster_refresh=lambda *_: {"status": "cached"})
                counts = pc.table_counts(root, TABLES)
                pc.checkpoint(root)
                files, size = pc.disk_usage(root)
                row = {"poll": number, "at": clock(), "attempted": out["attempted"],
                       "articles_added": out["articles_added"], "claims_added": out["claims_added"],
                       **counts, "files": files, "bytes": size}
                if previous:
                    row["delta"] = {k: row[k] - previous[k] for k in (*TABLES, "files", "bytes")}
                rows.append(row)
                previous = row
                clock.advance(minutes=20, seconds=1)
            snap = news.snapshot(root, "epl/2026-27/nottingham-forest/chelsea", clock())
        # Average over the second half of the run so SQLite page allocation steps even out.
        half = rows[len(rows) // 2 - 1]
        span = rows[-1]["poll"] - half["poll"]
        steady = {k: (rows[-1][k] - half[k]) / span for k in (*TABLES, "files", "bytes")}
        per_player = {k: v / args.listed for k, v in steady.items()}
        league_rows, polls_per_day = 82, 72
        summary = {
            "listed_players": args.listed,
            "polls": args.polls,
            "first_poll": {k: rows[0][k] for k in (*TABLES, "files", "bytes")},
            "steady_state_delta_per_poll_second_half_average": steady,
            "steady_state_per_listed_player_per_poll": per_player,
            "facts_in_final_snapshot": len(snap["facts"]),
            "extrapolation": {
                "assumption": f"{league_rows} listed players league-wide (live report: 59 injured + 17 doubtful + 6 suspended), {polls_per_day} polls/day",
                "article_versions_per_day": league_rows * polls_per_day * per_player["news_articles"],
                "blob_files_per_day": league_rows * polls_per_day * per_player["files"],
                "bytes_per_day": league_rows * polls_per_day * per_player["bytes"],
                "article_versions_per_280_day_season": 280 * league_rows * polls_per_day * per_player["news_articles"],
                "gib_per_280_day_season": 280 * league_rows * polls_per_day * per_player["bytes"] / 2**30,
            },
        }
        shown = rows if len(rows) <= 12 else rows[:3] + rows[-3:]
        print(json.dumps({"polls_first_and_last": shown, "summary": summary}, indent=1))


if __name__ == "__main__":
    main()
