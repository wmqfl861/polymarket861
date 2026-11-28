"""Deterministic provider responses for the installed-wheel user-service smoke test."""

import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from polymarket861 import market_provider
from polymarket861.nba import data


def schedule_page(params, key):
    tip = datetime.fromisoformat(os.environ["PM861_CI_NBA_TIP"])
    day = tip.astimezone(ZoneInfo("America/New_York")).date()
    teams = (
        {"id": 1, "abbreviation": "MIA", "full_name": "Miami Heat"},
        {"id": 2, "abbreviation": "NYK", "full_name": "New York Knicks"},
    )
    rows = []
    if params["season_type"] == "regular":
        for i in range(32):
            upcoming = i == 31
            game_day = day if upcoming else day - timedelta(days=2 + i % 9)
            season = game_day.year - int(game_day.month < 7)
            rows.append(
                {
                    "id": i + 1,
                    "home_team": teams[i % 2],
                    "visitor_team": teams[1 - i % 2],
                    "date": game_day.isoformat(),
                    "season": season,
                    "season_type": "regular",
                    "datetime": tip.isoformat() if upcoming else None,
                    "status": "Scheduled" if upcoming else "Final",
                    "status_state": "scheduled" if upcoming else "final",
                    "period": 0 if upcoming else 4,
                    "home_team_score": 0 if upcoming else 99 + i,
                    "visitor_team_score": 0 if upcoming else 98 + i,
                }
            )
    return httpx.Response(200, json={"data": rows, "meta": {"next_cursor": None}})


def catalogue(url, params=None, *, timeout=12):
    # Only the external HTTP transport is replaced. The installed wheel runs
    # discovery, native rules, multi-token books, schedule matching and sealing.
    from test_nba_markets import Venue
    from polymarket861.nba.markets_contracts import stamp

    mock = Venue()
    native = mock.tip.astimezone(ZoneInfo("America/New_York"))
    tip = datetime.fromisoformat(os.environ["PM861_CI_NBA_TIP"])
    east = tip.astimezone(ZoneInfo("America/New_York"))

    def date_label(day):
        return f"{day:%A, %B} {day.day}, {day.year}"

    replacement = f"{date_label(east)} at {east:%I:%M %p} ET".replace(" at 0", " at ")
    old = f"{date_label(native)} at 7:00 PM ET"
    new_slug = f"nba-mia-nyk-{east.date()}"
    mock.event.update(startTime=stamp(tip), slug=new_slug)
    for m in mock.event["markets"]:
        m.update(slug=m["slug"].replace(mock.slug, new_slug), gameStartTime=stamp(tip))
        m["description"] = (
            m["description"]
            .replace(old, replacement)
            .replace(
                date_label(native + timedelta(days=2)),
                date_label(east + timedelta(days=2)),
            )
        )
    mock.slug = new_slug
    mock.now = datetime.now(timezone.utc)
    return mock.get(url, params, timeout=timeout)


sys.path.insert(0, str(Path(os.environ["GITHUB_WORKSPACE"]) / "tests"))
data._http_get = schedule_page
market_provider.get = catalogue
