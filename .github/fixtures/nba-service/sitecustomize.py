"""Deterministic provider responses for the installed-wheel user-service smoke test."""
import json
import os
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from zoneinfo import ZoneInfo

import httpx

from polymarket861 import market_provider
from polymarket861.nba import data


def schedule_page(params, key):
    tip = datetime.fromisoformat(os.environ["PM861_CI_NBA_TIP"])
    day = tip.astimezone(ZoneInfo("America/New_York")).date()
    teams = ({"id": 1, "abbreviation": "BOS", "full_name": "Boston Celtics"},
             {"id": 2, "abbreviation": "NYK", "full_name": "New York Knicks"})
    rows = []
    if params["season_type"] == "regular":
        for i in range(32):
            upcoming = i == 31
            game_day = day if upcoming else day - timedelta(days=2 + i % 9)
            season = game_day.year - int(game_day.month < 7)
            rows.append({"id": i + 1, "home_team": teams[i % 2], "visitor_team": teams[1 - i % 2],
                         "date": game_day.isoformat(), "season": season, "season_type": "regular",
                         "datetime": tip.isoformat() if upcoming else None,
                         "status": "Scheduled" if upcoming else "Final",
                         "status_state": "scheduled" if upcoming else "final", "period": 0 if upcoming else 4,
                         "home_team_score": 0 if upcoming else 99 + i,
                         "visitor_team_score": 0 if upcoming else 98 + i})
    return httpx.Response(200, json={"data": rows, "meta": {"next_cursor": None}})


def catalogue(url, params=None, *, timeout=12):
    if url != market_provider.GAMMA + "/events/keyset":
        raise AssertionError("unexpected CI URL " + url)
    now = datetime.now(timezone.utc)
    return market_provider.Reply(json.dumps({"events": []}).encode(), now.isoformat(), now.isoformat(),
                                 {"date": format_datetime(now, usegmt=True)}, url)


data._http_get = schedule_page
market_provider.get = catalogue
