"""Reviewed, free access to ESPN/DraftKings football comparison evidence."""

import hashlib
from pathlib import Path

from polymarket861 import market_provider, store

LEAGUES = {"epl": "eng.1", "lal": "esp.1", "bun": "ger.1", "sea": "ita.1", "fl1": "fra.1"}
ROUTES = {"scoreboard", "summary"}
PERMISSIONS = Path(__file__).resolve().parent / "source-permissions"
# (league, route) -> (sha256 of reviewed free written grant, inclusive start, exclusive end).
# Review must cover automated collection and retention of the schedule, lineup (summary)
# and sportsbook fields for paper comparison. A kickoff-only grant does not cover these uses.
REVIEWED_FREE_GRANTS = {}


def permission_digest(league, route):
    if league not in LEAGUES or route not in ROUTES:
        raise ValueError("unsupported_football_odds_route")
    grant = REVIEWED_FREE_GRANTS.get((league, route))
    if grant is None:
        raise ValueError("no_reviewed_free_ESPN_DraftKings_grant")
    digest, start, end = grant
    now = store.instant(store.now_utc())
    if not store.instant(start) <= now < store.instant(end):
        raise ValueError("inactive_ESPN_DraftKings_grant")
    if hashlib.sha256((PERMISSIONS / digest).read_bytes()).hexdigest() != digest:
        raise ValueError("ESPN_DraftKings_grant_digest_mismatch")
    return digest


def fetch(league, route, params):
    digest = permission_digest(league, route)
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{LEAGUES[league]}/{route}"
    return market_provider.get(url, params, timeout=12), digest
