"""What does the unmodified 0.4.2 news poller conclude from an empty availability reply
for a club outside the provider's documented coverage?

The reply below is synthetic. It has the provider's real header fields, a zero count
and an empty list, which is the shape the 0.5 plan reports for clubs outside the
English top flight (LEAGUES-AND-SOURCES.md, "Touchline Wire · 英超" and the Ligue 1 row).
No network access is used.

Usage: python -I probe_empty_wire_reply.py --src <repo>/src
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", required=True)
    args = parser.parse_args()
    sys.path.insert(0, args.src)
    from polymarket861 import news_sources, store
    from polymarket861.market_provider import Reply

    now = datetime.now(timezone.utc).replace(microsecond=0)
    season = f"{now.year - (now.month < 7)}-{str(now.year - (now.month < 7) + 1)[2:]}"
    body = json.dumps({
        "meta": {
            "source": "Touchline Wire",
            "attribution_url": news_sources.WIRE_HOST,
            "license": "Free for personal, academic, and open-source use with attribution",
            "timestamp_iso": store.iso(now - timedelta(minutes=5)),
            "count": 0,
        },
        "data": [],
    }).encode()

    rows = []
    for club in ("Arsenal", "FC Bayern München", "Paris Saint-Germain FC", "Venezia FC", "Levante UD"):
        sources = news_sources.default_sources(club, season)
        wire = next(s for s in sources if s["id"] == news_sources.WIRE)

        def getter(url):
            return Reply(body, now - timedelta(seconds=1), now, {}, url)

        try:
            answer = news_sources.poll(wire, club, season, getter=getter)
            outcome = {"status": answer["status"], "player_rows": answer["player_rows"],
                       "invalid_entries": answer["invalid_entries"],
                       "provider_state_as_of": answer["provider_state_as_of"],
                       "cursor_complete": answer["cursor"]["complete"]}
        except ValueError as exc:
            outcome = {"status": "rejected", "reason": str(exc)}
        rows.append({"club": club, "in_wire_club_table": news_sources.football.team_key(club) in news_sources.WIRE_CLUBS,
                     "registered_default_sources": [s["id"] for s in sources],
                     "availability_url": wire["url"], "poll": outcome})

    report = {
        "what": "default_sources() and poll() of polymarket861 0.4.2 fed a synthetic empty availability reply",
        "season": season,
        "reading": ("status ok with zero rows is the input from which news._side_coverage sets "
                    "availability_observed for the side (state ok or degraded, not the rumour feed, not baseline_only)"),
        "rows": rows,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
