"""Replay the public pregame NBA market sample offline, including all native order-book levels."""

import argparse
import hashlib
import json
import zipfile
from collections import Counter
from decimal import Decimal
from pathlib import Path

from polymarket861 import market_provider as api
from polymarket861.nba.markets_contracts import (
    FAMILIES,
    contract_inventory,
    eligibility,
    fixture,
    price,
    selected_line,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests/fixtures/nba/market-lines-20261128.zip"
LINE_FAMILIES = {
    "full.total": "total",
    "full.team_total.home": "home_total",
    "full.team_total.away": "away_total",
    "full.spread": "spread",
}


def require(valid, message):
    if not valid:
        raise ValueError(message)


def native_family(e, m):
    if m.get("sportsMarketType") == "moneyline" and m.get("slug") == e["slug"]:
        return "win"
    return LINE_FAMILIES.get((m.get("marketMetadata") or {}).get("line_family"))


def center(m, books, label, observed):
    """Independent calculation using the original outcome names, decimal price and all returned levels."""
    tokens = json.loads(m["clobTokenIds"])
    labels = [v.casefold() for v in json.loads(m["outcomes"])]
    require(
        len(tokens) == len(set(tokens)) == len(labels) == len(set(labels)) == 2
        and label in labels,
        "ambiguous native sides",
    )
    top = []
    for token in tokens:
        b = books.get(token)
        if (
            not b
            or b["market"] != m["conditionId"]
            or api.moment(b["timestamp"]) > observed
        ):
            return None
        levels = []
        for field in ("bids", "asks"):
            pairs = [
                (Decimal(str(x["price"])), Decimal(str(x["size"]))) for x in b[field]
            ]
            if not pairs or any(not 0 < p < 1 or q <= 0 for p, q in pairs):
                return None
            prices = [p for p, _ in pairs]
            if len(set(prices)) != len(prices):
                return None
            levels.append((max if field == "bids" else min)(prices))
        if levels[0] >= levels[1]:
            return None
        top.append(levels)
    if top[0][0] + top[1][1] != 1 or top[0][1] + top[1][0] != 1:
        return None
    side = labels.index(label)
    mid = sum(top[side]) / 2
    return (abs(mid - Decimal(".5")), m["conditionId"].lower()), mid, tokens[side]


def body(archive, receipt):
    raw = archive.read(receipt["path"])
    require(
        receipt["http_status"] == 200
        and hashlib.sha256(raw).hexdigest() == receipt["sha256"],
        "raw receipt hash or status differs",
    )
    reply = api.Reply(
        raw, receipt["sent"], receipt["received"], receipt["headers"], receipt["url"]
    )
    require(
        api.fresh_observation(reply), "archived HTTP receipt failed freshness check"
    )
    return json.loads(raw)


def replay(path=DEFAULT_FIXTURE):
    signature = hashlib.sha256(path.read_bytes()).hexdigest()
    results, counters = [], Counter()
    with zipfile.ZipFile(path) as archive:
        for frozen in json.loads(archive.read("manifest.json")):
            e = body(archive, frozen["gamma"])
            raw_books = body(archive, frozen["clob"])
            g = fixture(e)
            observed = api.moment(frozen["clob"]["received"])
            require(
                g["id"] == frozen["event_id"]
                and g["pregame"]
                and api.moment(frozen["gamma"]["received"])
                <= observed
                < api.moment(g["kickoff"]),
                "pregame evidence missing",
            )
            books = {str(b["asset_id"]): b for b in raw_books}
            targets = {str(x) for x in frozen["clob"]["params"]["token_ids"]}
            require(
                len(books) == len(raw_books) and set(books) == targets,
                "unexpected, duplicate or omitted native book",
            )
            markets = [
                m
                for m in e["markets"]
                if native_family(e, m)
                and m.get("active") is True
                and m.get("closed") is False
                and m.get("acceptingOrders") is True
                and m.get("enableOrderBook") is True
            ]
            require(
                {str(t) for m in markets for t in json.loads(m["clobTokenIds"])}
                == targets,
                "not every available full-game line was requested",
            )
            candidates, problems = contract_inventory(
                e, g, main_only=False, native_only=True
            )
            require(
                not any(
                    v.get("rule_template_mismatch") or v.get("unverified_contract")
                    for v in problems.values()
                ),
                "unverified native listing",
            )
            candidates = [
                c
                for c in candidates
                if not eligibility(
                    c, g["kickoff"], observed, original_kickoff=g["kickoff"]
                )
            ]
            quotes = {
                c["condition_id"]: q
                for c in candidates
                if (q := price(c, books, observed)) is not None
            }
            families = {}
            for family in FAMILIES:
                listed = [m for m in markets if native_family(e, m) == family]
                label = (
                    g["clubs"]["home"]["name"].casefold() if family == "win" else "over"
                )
                baseline = [
                    (q, m) for m in listed if (q := center(m, books, label, observed))
                ]
                expected = min(baseline, key=lambda pair: pair[0][0], default=None)
                current = [c for c in candidates if c["family"] == family]
                require(
                    len(listed) == len(current),
                    "candidate contract coverage differs from listing",
                )
                selected = selected_line(current, quotes)
                actual = (
                    (
                        selected["condition_id"],
                        selected["token_id"],
                        quotes[selected["condition_id"]][1],
                    )
                    if selected
                    else None
                )
                reference = (
                    (expected[1]["conditionId"], expected[0][2], float(expected[0][1]))
                    if expected
                    else None
                )
                require(actual == reference, f"center mismatch on {e['slug']} {family}")
                families[family] = {
                    "listed": len(listed),
                    "two_sided": len(baseline),
                    "quality": "ok"
                    if selected
                    else ("unlisted" if not listed else "no_valid_book"),
                }
                if expected:
                    q, m = expected
                    kind = (m.get("marketMetadata") or {}).get("line_kind")
                    families[family].update(
                        condition_id=m["conditionId"],
                        token_id=q[2],
                        line=selected["line"],
                        midpoint=float(q[1]),
                        line_kind=kind,
                    )
                    counters["alternate_selections"] += int(kind == "alternate")
                counters["selected_families" if selected else "no_selection"] += 1
            spreads = [m for m in markets if native_family(e, m) == "spread"]
            counters.update(
                events=1,
                full_game_listings=len(markets),
                native_books=len(books),
                spread_listings=len(spreads),
                forecast_listings=sum(v["listed"] for v in families.values()),
                valid_two_sided_forecast_listings=sum(
                    v["two_sided"] for v in families.values()
                ),
            )
            results.append(
                {
                    "slug": e["slug"],
                    "event_id": g["id"],
                    "venue_game_id": g["game_id"],
                    "kickoff": g["kickoff"],
                    "gamma_received": frozen["gamma"]["received"],
                    "clob_received": frozen["clob"]["received"],
                    "gamma_sha256": frozen["gamma"]["sha256"],
                    "clob_sha256": frozen["clob"]["sha256"],
                    "native_books": len(books),
                    "spread_listings": len(spreads),
                    "families": families,
                }
            )
    return {
        "fixture": path.name,
        "fixture_sha256": signature,
        "selection": "All still-pregame NBA fixtures on the two earliest Eastern game dates in the 52-hour native catalogue, in kickoff and event ID order.",
        "scope": "Observed public Gamma/CLOB pregame cross-section; not a frozen prediction, fixed-window benchmark, execution or settlement.",
        "ranking": "All verified full-game consensus and alternate lines with complementary two-sided books; smallest absolute distance to 0.50, then condition ID.",
        "counts": dict(counters),
        "events": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", nargs="?", type=Path, default=DEFAULT_FIXTURE)
    print(json.dumps(replay(parser.parse_args().fixture), sort_keys=True, indent=2))
