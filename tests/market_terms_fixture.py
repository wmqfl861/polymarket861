"""Small native Gamma/CLOB responses for the full-time totals integration tests."""

import json
from email.utils import format_datetime
from unittest.mock import patch
from urllib.parse import urlencode

from polymarket861 import ledger_terms, market_archive, market_provider, store

SLUG = "epl-hom-awa-2025-02-01"
CONDITION = "0x" + "2" * 64
TOKEN = "71"


def legacy_total_description(home="Home", away="Away"):
    return (
        f"In the upcoming Premier League game between {home} and {away}, scheduled for February 1 at 10:00 AM ET: "
        f'This market will resolve to "Over" if {home} and {away} combine to score 3 or more goals in this game. '
        'If the combined total is less than 3, this market will resolve to "Under". '
        "If the game is postponed, this market will remain open until the game has been completed. "
        "If the game is canceled entirely, with no make-up game, this market will resolve 50–50. "
        "If the game is started but not completed, this market will resolve according to the official final score published on premierleague.com. "
        "This market refers only to the outcome within the first 90 minutes of regular play plus stoppage time."
    )


def event(kickoff, *, description=None, fee=False):
    return {
        "slug": SLUG,
        "id": "712",
        "title": "Home vs. Away",
        "startTime": kickoff,
        "live": False,
        "ended": False,
        "teams": [
            {"name": "Home", "abbreviation": "hom", "ordering": "home"},
            {"name": "Away", "abbreviation": "awa", "ordering": "away"},
        ],
        "markets": [
            {
                "slug": SLUG + "-total-2pt5",
                "conditionId": CONDITION,
                "outcomes": ["Over", "Under"],
                "clobTokenIds": [TOKEN, "72"],
                "line": 2.5,
                "version": "v1",
                "sportsMarketType": "totals",
                "description": description or legacy_total_description(),
                "feesEnabled": fee,
                "secondsDelay": 0,
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "enableOrderBook": True,
                "orderMinSize": 1,
                "orderPriceMinTickSize": 0.01,
            }
        ],
    }


def reply(body, at, url):
    return market_provider.Reply(
        json.dumps(body).encode(),
        at,
        at,
        {"date": format_datetime(store.instant(at))},
        url,
    )


def receive(root, data, at):
    def get(url, params=None):
        body = data
        if url.endswith("more-markets"):
            body = dict(data, slug=SLUG + "-more-markets", markets=[])
        return reply(body, at, url)

    with patch.object(market_provider, "get", side_effect=get):
        result = market_archive.discover(root, SLUG)
    if result["gaps"]:
        raise AssertionError(result)


def prepare_formal_market(root, op):
    intent = op["intent"]
    at = intent["contract"]["rule_received_at"]
    data = event(intent["kickoff"])
    receive(root, data, at)
    intent["match_id"] = SLUG
    op["match_id"] = SLUG
    intent["contract"] = ledger_terms.contract_at(
        root, CONDITION, TOKEN, intent["decision_at"]
    )
    market = market_archive.contracts(root, SLUG, as_of=intent["decision_at"])[0]
    for name in ("first_book", "second_book", "close_book"):
        old = op[name]
        stamp = old["received_at"]
        native = {
            "asset_id": TOKEN,
            "market": CONDITION,
            "bids": old["bids"],
            "asks": old["asks"],
            "tick_size": old["tick_size"],
            "min_order_size": old["min_order_size"],
            "timestamp": str(int(store.instant(stamp).timestamp() * 1000)),
        }
        url = market_provider.CLOB + "/book?" + urlencode({"token_id": TOKEN})
        with patch.object(
            market_provider, "get", return_value=reply(native, stamp, url)
        ):
            op[name] = market_archive.snapshot_book(root, market, TOKEN)
    return intent["contract"]
