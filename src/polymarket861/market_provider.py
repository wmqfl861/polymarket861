"""Public international Polymarket endpoints and lossless, conservative normalization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode
from . import __version__, market_http

from .store import canonical, iso

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
VENUE = "polymarket.com"
FAMILIES = ("moneyline", "total", "home_total", "away_total")
# The primaryTagId for Serie A is not its working domestic-games query tag.
# Fail closed on a changed live association until a new mapping is verified.
SPORT_MARKETS = {
    "epl": ("10188", "306"),
    "lal": ("10193", "780"),
    "bun": ("10194", "1494"),
    "sea": ("10203", "101962"),
    "fl1": ("10195", "102070"),
}
# Existing public interfaces deliberately keep an EPL-only default.
SPORT_FIXTURES = {
    sport: re.compile(
        rf"^({sport}-([a-z0-9]+)-([a-z0-9]+)-(\d{{4}}-\d{{2}}-\d{{2}}))(?:-(more-markets))?$"
    )
    for sport in SPORT_MARKETS
}
FIXTURE = SPORT_FIXTURES["epl"]


def moment(value: str | float) -> datetime:
    if isinstance(value, (int, float)) or str(value).isdigit():
        n = float(value)
        return datetime.fromtimestamp(n / 1000 if n >= 1e12 else n, UTC)
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("market timestamps must specify their timezone")
    return stamp.astimezone(UTC)


@dataclass(frozen=True)
class Reply:
    body: bytes
    requested_at: str
    received_at: str
    headers: dict[str, str]
    url: str


def get(url: str, params: dict | None = None, *, timeout: float = 25) -> Reply:
    """Read a fixed public endpoint. There are no authenticated or trading routes."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": f"polymarket861/{__version__}",
        "Cache-Control": "no-cache",
    }
    body = None
    if url == CLOB + "/books":
        tokens = (params or {}).get("token_ids", [])
        if not 1 <= len(tokens) <= 500 or not all(str(t).isdigit() for t in tokens):
            raise ValueError("book batch requires 1..500 numeric token IDs")
        # This read-only endpoint takes a POST body even for anonymous market data.
        body = canonical([{"token_id": str(t)} for t in tokens]).encode()
        headers["Content-Type"] = "application/json"
    elif params:
        url += "?" + urlencode(params)
    return Reply(
        *market_http.read(
            "POST" if body is not None else "GET", url, headers, body, timeout
        )
    )


def resolution_row(payload: Any, token: str) -> dict | None:
    """A successful single-token feed may return an empty list before it knows the token."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if (
        not isinstance(rows, list)
        or payload.get("error")
        or payload.get("success") is False
    ):
        raise ValueError("unrecognized official token resolution envelope")
    if not rows:
        return None
    if (
        len(rows) != 1
        or not isinstance(rows[0], dict)
        or str(rows[0].get("token_id")) != str(token)
    ):
        raise ValueError("wrong or ambiguous token in official resolution response")
    row = rows[0]
    if (
        not isinstance(row.get("condition_id"), str)
        or not row["condition_id"]
        or type(row.get("resolved")) is not bool
        or row["resolved"] != (row.get("final_price") is not None)
    ):
        raise ValueError("unrecognized official token resolution")
    return row


def sequence(raw: Any) -> list:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return raw if isinstance(raw, list) else []


def number(value: Any) -> Decimal:
    try:
        n = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid numeric market field") from exc
    if not n.is_finite():
        raise ValueError("invalid nonfinite market field")
    return n


def teams(event: dict) -> dict:
    return {
        str(t["ordering"]): t
        for t in event.get("teams") or []
        if isinstance(t, dict) and t.get("ordering") in ("home", "away")
    }


def family(event: dict, market: dict, *, sport: str = "epl") -> tuple[str | None, str | None, str | None]:
    """Use type AND explicit side/line labels; never infer a team from outcome index zero."""
    identity = SPORT_FIXTURES[sport].fullmatch(str(event.get("slug", "")))
    if not identity:
        return None, None, "not_an_epl_fixture" if sport == "epl" else "wrong_sport_fixture"
    slug = market.get("slug", "")
    typ = market.get("sportsMarketType")
    labels = sequence(market.get("outcomes"))
    # Early EPL binaries lack sportsMarketType. Only recover the strict fixture,
    # title, side and question pattern below; periods and props stay unclassified.
    if sport == "epl" and typ is None and not identity[5]:
        question = str(market.get("question") or "").lower()
        title = str(market.get("groupItemTitle") or "").lower()
        expected = "will " + title + " win on " + identity[4] + "?"
        if question == expected or (
            " end in a draw?" in question and title.startswith("draw (")
        ):
            typ = "moneyline"
    if typ not in ("moneyline", "totals", "soccer_team_totals"):
        return None, None, "outside_supported_full_time_families"
    if not isinstance(slug, str) or not slug.startswith(identity[1] + "-"):
        return None, None, "fixture_slug_mismatch"
    suffix = slug[len(identity[1]) + 1 :]
    title = str(market.get("groupItemTitle") or "").casefold()
    roster = teams(event)
    if typ == "moneyline":
        if sorted(labels) != ["No", "Yes"]:
            return None, None, "unrecognized_moneyline_outcomes"
        if suffix == "draw" and (
            "draw" in title or "draw" in str(market.get("question", "")).casefold()
        ):
            return "moneyline", "draw", None
        side = (
            "home"
            if suffix == identity[2]
            else "away"
            if suffix == identity[3]
            else None
        )
        club = roster.get(side, {})
        if (
            not club
            or not title
            or club.get("abbreviation", suffix).casefold() != suffix
        ):
            return None, None, "moneyline_team_unverified"
        aliases = {str(club.get(k) or "").strip().casefold() for k in ("alias", "name")}
        aliases.update({re.sub(r" (?:afc|fc)$", "", alias) for alias in aliases})
        # Early titles use occasional short names (Tottenham vs. Wolves). The
        # roster side, slug abbreviation and the same side of the fixture title must agree.
        title_parts = re.split(r"\s+vs\.?\s+", str(event.get("title") or "").casefold())
        if len(title_parts) == 2 and club.get("abbreviation", "").casefold() == suffix:
            aliases.add(title_parts[0 if side == "home" else 1].strip())
        if title not in aliases:
            return None, None, "moneyline_team_unverified"
        return "moneyline", side, None
    line_match = re.fullmatch(
        r"(total|team-total-(home|away))-(\d+)(?:pt(\d+))?", suffix
    )
    if not line_match or sorted(labels) != ["Over", "Under"]:
        return None, None, "unknown_total_period_or_side"
    side = line_match[2]
    if (typ == "soccer_team_totals") != bool(side):
        return None, None, "market_type_disagrees_with_team_scope"
    line = number(line_match[3] + "." + (line_match[4] or "0"))
    if market.get("line") is None or number(market["line"]) != line:
        return None, None, "numeric_line_mismatch"
    if side:
        team = roster.get(side, {})
        name = str(team.get("name") or "").casefold()
        if not name or not title.startswith(name + " "):
            return None, None, "team_title_unverified"
    return side + "_total" if side else "total", side, None


def normalized_markets(event: dict, *, sport: str = "epl") -> list[dict]:
    identity = SPORT_FIXTURES[sport].fullmatch(str(event.get("slug", "")))
    if not identity:
        return []
    extracted = []
    for market in event.get("markets") or []:
        if not isinstance(market, dict):
            continue
        kind, side, error = family(event, market, sport=sport)
        version = market.get("version")
        key = {"v1": "clobTokenIds", "v2": "positionIds"}.get(version)
        ids = sequence(market.get(key)) if key else []
        labels = sequence(market.get("outcomes"))
        if (
            not key
            or len(ids) != 2
            or len(labels) != 2
            or not all(str(i).isdigit() for i in ids)
            or len(set(map(str, ids))) != 2
        ):
            ids = []
            error = error or "unknown_protocol_or_token_binding"
        description = market.get("description") or ""
        condition = market.get("conditionId") or ""
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition):
            error = error or "invalid_condition_id"
        event_time = event.get("startTime")
        market_time = market.get("gameStartTime")
        times = sorted({iso(moment(x)) for x in (event_time, market_time) if x})
        state = [
            market.get(k)
            for k in ("active", "closed", "acceptingOrders", "enableOrderBook")
        ]
        expected = (True, False, True, True)
        is_open = (
            False
            if any(v is not None and v is not e for v, e in zip(state, expected))
            else (True if all(v is e for v, e in zip(state, expected)) else None)
        )
        extracted.append(
            {
                "venue": VENUE,
                "fixture": identity[1],
                "event_slug": event["slug"],
                "event_id": str(event["id"]),
                "slug": market.get("slug"),
                "condition_id": condition,
                "protocol": version,
                "id_field": key,
                "family": kind,
                "side": side,
                "line": str(market["line"]) if market.get("line") is not None else None,
                "mapping_error": error,
                "type": market.get("sportsMarketType"),
                "question": market.get("question"),
                "rules": description,
                "rule_sha256": hashlib.sha256(description.encode()).hexdigest()
                if description
                else None,
                "tokens": [
                    {"token_id": str(t), "outcome": l} for t, l in zip(ids, labels)
                ],
                "fees_enabled": market.get("feesEnabled"),
                "fee_schedule": market.get("feeSchedule"),
                "seconds_delay": market.get("secondsDelay"),
                "min_order_size": market.get("orderMinSize"),
                "tick_size": market.get("orderPriceMinTickSize"),
                "game_times": times,
                "market_open": is_open,
                "live": event.get("live"),
                "ended": event.get("ended"),
            }
        )
    return extracted


def fresh_observation(reply: Reply) -> bool:
    """Require an independently fresh HTTP reply; a stale cache can return unchanged depth."""
    try:
        age = float(reply.headers.get("age", 0))
        server = parsedate_to_datetime(reply.headers["date"])
        received = moment(reply.received_at)
        return (
            0 <= age <= 30
            and abs((received - server).total_seconds()) <= 30
            and (0 <= (received - moment(reply.requested_at)).total_seconds() <= 30)
        )
    except (KeyError, TypeError, ValueError):
        return False


def levels(raw: Any, descending: bool) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("book must contain both bids and asks arrays")
    by_price = {}
    for row in raw:
        p, s = number(row["price"]), number(row["size"])
        if not 0 < p < 1 or s <= 0 or p in by_price:
            raise ValueError("out-of-range or duplicate order-book level")
        by_price[p] = s
    return [
        {"price": format(p, "f"), "size": format(by_price[p], "f")}
        for p in sorted(by_price, reverse=descending)
    ]


def normalize_book(
    raw: dict, reply: Reply, contract: dict, token: str, source_hash: str
) -> dict:
    if (
        str(raw.get("asset_id")) != token
        or raw.get("market") != contract["condition_id"]
    ):
        raise ValueError("CLOB returned a different condition or token")
    bids, asks = levels(raw.get("bids"), True), levels(raw.get("asks"), False)
    if bids and asks and number(bids[0]["price"]) >= number(asks[0]["price"]):
        raise ValueError("crossed or locked book")
    times = contract["game_times"]
    pregame = None
    if contract["live"] is True or contract["ended"] is True:
        pregame = False
    elif len(times) == 1:
        receipt = moment(reply.received_at)
        if contract["live"] is False and contract["ended"] is False:
            if receipt < moment(times[0]):
                pregame = True
            elif (
                contract.get("recorded_at")
                and 0
                <= (receipt - moment(contract["recorded_at"])).total_seconds()
                <= 15
            ):
                # Even after the scheduled start the venue can explicitly attest a delay.
                # The closing ledger still separately requires the independently proven actual kickoff.
                pregame = True
    exchange_at = (
        iso(moment(raw["timestamp"])) if raw.get("timestamp") is not None else None
    )
    return {
        "venue": VENUE,
        "fixture": contract["fixture"],
        "event_slug": contract["event_slug"],
        "condition_id": contract["condition_id"],
        "token_id": token,
        "received_at": reply.received_at,
        "observed_at": reply.received_at if fresh_observation(reply) else None,
        "requested_at": reply.requested_at,
        "exchange_at": exchange_at,
        "market_open": contract["market_open"],
        "pregame": pregame,
        "bids": bids,
        "asks": asks,
        "min_order_size": raw.get("min_order_size"),
        "tick_size": raw.get("tick_size"),
        "source_sha256": source_hash,
        "book_hash": raw.get("hash"),
        "rule_sha256": contract["rule_sha256"],
        "fees_enabled": contract["fees_enabled"],
        "fee_schedule": contract["fee_schedule"],
        "seconds_delay": contract["seconds_delay"],
        "contract_recorded_at": contract.get("recorded_at"),
        "contract_source_sha256": contract.get("source_sha256"),
        "fee_effective_at": None,
        "fee_asset": None,
        "fee_rounding": None,
    }
