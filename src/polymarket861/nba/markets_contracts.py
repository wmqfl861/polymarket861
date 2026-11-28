"""Identity and quote checks for full-game NBA contracts on Polymarket."""

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..market_provider import levels, moment, number, sequence

FAMILIES = ("win", "total", "home_total", "away_total")
FIXTURE = re.compile(r"nba-([a-z0-9]+)-([a-z0-9]+)-(\d{4}-\d\d-\d\d)")
CONDITION = re.compile(r"0x[0-9a-fA-F]{64}")
TEAM_ALIASES = {
    "GS": "GSW",
    "NY": "NYK",
    "NO": "NOP",
    "SA": "SAS",
    "PHO": "PHX",
    "BRK": "BKN",
    "BRO": "BKN",
    "CHO": "CHA",
    "UTAH": "UTA",
    "WSH": "WAS",
}


def team_code(value):
    code = str(value).strip().upper()
    return TEAM_ALIASES.get(code, code)


def stamp(dt):
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def fixture(event):
    slug = FIXTURE.fullmatch(str(event.get("slug")))
    if (
        not slug
        or not str(event.get("id", "")).isdigit()
        or not str(event.get("gameId", "")).isdigit()
    ):
        raise ValueError("not a primary NBA fixture")
    if "10345" not in {
        str(s.get("id")) for s in (event.get("series") or []) if isinstance(s, dict)
    }:
        raise ValueError("wrong NBA series")
    if "100639" not in {
        str(s.get("id")) for s in (event.get("tags") or []) if isinstance(s, dict)
    }:
        raise ValueError("not a games event")
    clubs = event.get("teams") or []
    sides = {
        t.get("ordering"): t
        for t in clubs
        if isinstance(t, dict) and t.get("league") == "nba"
    }
    if len(clubs) != 2 or set(sides) != {"away", "home"}:
        raise ValueError("unverified NBA teams")
    for side, abbr in (("away", slug[1]), ("home", slug[2])):
        t = sides[side]
        if (
            not t.get("name")
            or not t.get("id")
            or team_code(t.get("abbreviation")) != team_code(abbr)
        ):
            raise ValueError("team side or identifier conflict")
    if str(sides["away"]["id"]) == str(sides["home"]["id"]) or team_code(
        slug[1]
    ) == team_code(slug[2]):
        raise ValueError("duplicate teams")
    start = stamp(moment(event["startTime"]))
    # Upcoming events often omit live/ended but explicitly carry period=NS.
    started = (
        event.get("live") is True
        or event.get("ended") is True
        or bool(event.get("finishedTimestamp"))
        or event.get("period") not in (None, "", "NS")
        or bool(event.get("score"))
        or bool(event.get("elapsed"))
    )
    unavailable = (
        event.get("active") is False
        or event.get("closed") is True
        or event.get("archived") is True
        or event.get("cancelled") is True
        or event.get("canceled") is True
    )
    pregame = (
        not started
        and not unavailable
        and event.get("active") is True
        and event.get("closed") is False
        and event.get("archived") is False
        and (
            event.get("period") == "NS"
            or (event.get("live") is False and event.get("ended") is False)
        )
    )
    return {
        "id": str(event["id"]),
        "game_id": str(event["gameId"]),
        "kickoff": start,
        "original_date": slug[3],
        "home": team_code(slug[2]),
        "away": team_code(slug[1]),
        "clubs": sides,
        "started": started or unavailable,
        "unavailable": unavailable,
        "pregame": pregame,
    }


_RULE_DATE = (
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December) "
    r"(?:[1-9]|[12][0-9]|3[01]), [0-9]{4}"
)
_RULE_TIME = r"(?:[1-9]|1[0-2]):[0-5][0-9] (?:AM|PM) ET"
_NO_RESULT = (
    "If this market's outcome was definitively determined under its rules by the listed resolution sources "
    "before abandonment, that outcome will stand. Otherwise, the following no-result provisions apply. "
    "If the game is canceled, does not start by "
)
_NO_RESULT_END = ", or is abandoned with no official result by that time, this market will resolve 50-50."
_NO_RESULT_PATTERN = re.compile(
    re.escape(_NO_RESULT)
    + f"({_RULE_DATE}), ({_RULE_TIME})"
    + re.escape(_NO_RESULT_END)
)
_EAST = ZoneInfo("America/New_York")
_MONTHS = "January February March April May June July August September October November December".split()
_WEEKDAYS = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()


def _et_instant(date_text, time_text):
    weekday, month_day, year = date_text.split(", ")
    month, day = month_day.split()
    clock, half, _ = time_text.split()
    hour, minute = map(int, clock.split(":"))
    wall = datetime(
        int(year),
        _MONTHS.index(month) + 1,
        int(day),
        hour % 12 + 12 * (half == "PM"),
        minute,
    )
    if weekday != _WEEKDAYS[wall.weekday()]:
        raise ValueError("incorrect weekday in NBA settlement rules")
    candidates = {
        wall.replace(tzinfo=_EAST, fold=fold).astimezone(UTC) for fold in (0, 1)
    }
    valid = [t for t in candidates if t.astimezone(_EAST).replace(tzinfo=None) == wall]
    if len(valid) != 1:
        raise ValueError(
            "ambiguous or nonexistent Eastern time in NBA settlement rules"
        )
    return valid[0]


def rule_times(rules):
    """Reconstruct native times from the exact original text, including previously frozen contracts."""
    text = " ".join(rules.split())
    original = re.findall(
        f"originally scheduled for ({_RULE_DATE}) at ({_RULE_TIME}) "
        + re.escape("(as of creation of the first market related to the event):"),
        text,
    )
    cutoff = _NO_RESULT_PATTERN.findall(text)
    if len(original) != 1 or len(cutoff) != 1:
        raise ValueError("missing or ambiguous native NBA time limit")
    start, until = _et_instant(*original[0]), _et_instant(*cutoff[0])
    if start >= until:
        raise ValueError("native cancellation deadline precedes original start")
    return start, until


def eligibility(c, kickoff, observed=None, *, original_kickoff=None):
    try:
        original, until = rule_times(c["rules"])
        if original_kickoff is not None and moment(original_kickoff) != original:
            return "rule_original_schedule_conflict"
        if moment(kickoff) > until or (
            observed is not None and moment(observed) >= until
        ):
            return "rule_deadline_exceeded"
    except (KeyError, ValueError, TypeError, OverflowError):
        return "unverified_rule_time"
    return None


def _rules_basis(game, family, line, rules):
    """Only the captured venue templates are eligible. Unknown clauses require review."""
    away, home = [game["clubs"][side]["name"] for side in ("away", "home")]
    intro = (
        re.escape(
            f"In the upcoming NBA game between {away} and {home}, originally scheduled for "
        )
        + _RULE_DATE
        + " at "
        + _RULE_TIME
        + re.escape(" (as of creation of the first market related to the event): ")
    )
    text = " ".join(rules.split())
    header = re.match(intro, text)
    if not header:
        raise ValueError("unverified NBA rules introduction or fixture")
    body, n = _NO_RESULT_PATTERN.subn("{no-result}", text[header.end() :])
    if n != 1:
        raise ValueError("unverified cancellation rules")
    if family == "win":
        expected = (
            f'If the {away} win, the market will resolve to "{away}". '
            f'If the {home} win, the market will resolve to "{home}". '
            "{no-result} The result will be determined based on the final score including any overtime periods."
        )
    elif family == "total":
        threshold = int(line) + 1
        expected = (
            f'This market will resolve to "Over" if the {away} and {home} combine to score {threshold} '
            f"or more points in this game. If the combined total is less than {threshold}, "
            'this market will resolve to "Under". {no-result}'
        )
    else:
        club = home if family == "home_total" else away
        threshold = int(line) + 1
        expected = (
            f'This market will resolve to "Over" if the {club} score {threshold} or more points in this game. '
            f'If the {club} score less than {threshold} points in this game, this market will resolve to "Under". '
            f"Only points scored by the {club} count toward this market. "
            "The entire game including all overtime periods will be considered. {no-result}"
        )
    if body != expected:
        raise ValueError(
            "unrecognized rules for NBA scope, teams, threshold or payout directions"
        )
    return "full_game_inferred" if family == "total" else "explicit"


class RuleTemplateError(ValueError):
    """A listed full-game contract uses unverified native settlement wording."""


def _open(market):
    return (
        market.get("active") is True
        and market.get("closed") is False
        and market.get("acceptingOrders") is True
        and market.get("enableOrderBook") is True
    )


def _family(event, market):
    typ = market.get("sportsMarketType")
    meta = market.get("marketMetadata")
    slug = str(market.get("slug", ""))
    if typ == "moneyline" and slug == event["slug"]:
        return "win"
    if typ not in ("totals", "team_totals"):
        return None
    family = (
        {
            "full.total": "total",
            "full.team_total.home": "home_total",
            "full.team_total.away": "away_total",
        }.get(meta.get("line_family"))
        if isinstance(meta, dict)
        else None
    )
    if family:
        return family
    # A venue metadata change on an otherwise recognizable full-game listing is a
    # broken contract, not evidence that this family no longer exists.
    if typ == "totals" and slug.startswith(event["slug"] + "-total-"):
        return "total"
    if typ == "team_totals":
        for team in event.get("teams", []):
            side = team.get("ordering")
            if side in ("home", "away") and slug.startswith(
                event["slug"]
                + "-team-total-"
                + str(team.get("abbreviation")).lower()
                + "-"
            ):
                return side + "_total"
    return None


def spread_archives(event, game):
    """List only the two native token identifiers. No spread orientation or price is inferred."""
    result = []
    for market in event.get("markets", []):
        if not isinstance(market, dict) or not _open(market):
            continue
        meta = market.get("marketMetadata")
        if not isinstance(meta, dict) or (
            market.get("sportsMarketType") != "spreads"
            or meta.get("line_family") != "full.spread"
            or meta.get("line_kind") != "consensus"
        ):
            continue
        try:
            tokens = [str(t) for t in sequence(market.get("clobTokenIds"))]
            labels = {str(x).casefold() for x in sequence(market.get("outcomes"))}
            if (
                CONDITION.fullmatch(str(market.get("conditionId", "")))
                and len(tokens) == 2
                and len(set(tokens)) == 2
                and all(t.isdigit() for t in tokens)
                and labels == {c["name"].casefold() for c in game["clubs"].values()}
                and moment(market["gameStartTime"]) == moment(game["kickoff"])
                and re.fullmatch(
                    re.escape(event["slug"]) + r"-spread-(?:home|away)-[0-9]+pt5",
                    str(market.get("slug")),
                )
            ):
                result.append((market["conditionId"], *tokens))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    counts = Counter(item for row in result for item in row)
    return [r for r in result if all(counts[item] == 1 for item in r)]


def contract(event, game, market, *, main_only=True, native_only=False):
    """Resolve a side to its token using the provider's roster and explicit full-game metadata."""
    typ = market.get("sportsMarketType")
    meta = market.get("marketMetadata") or {}
    if not isinstance(meta, dict):
        raise ValueError("invalid market metadata")
    slug = event["slug"]
    tokens = list(map(str, sequence(market.get("clobTokenIds"))))
    labels = [str(x).strip().casefold() for x in sequence(market.get("outcomes"))]
    condition = str(market.get("conditionId", ""))
    rules = str(market.get("description") or "")
    if (
        not CONDITION.fullmatch(condition)
        or len(tokens) != 2
        or len(set(tokens)) != 2
        or not all(t.isdigit() for t in tokens)
        or len(labels) != 2
        or len(set(labels)) != 2
        or not str(market.get("id", "")).isdigit()
        or moment(market.get("gameStartTime")) != moment(game["kickoff"])
    ):
        raise ValueError("unverified contract")
    line = None
    market_slug = market.get("slug", "")
    if typ == "moneyline" and market_slug == slug:
        family = "win"
        names = [str(game["clubs"][s]["name"]).casefold() for s in ("home", "away")]
        if set(labels) != set(names):
            raise ValueError("unverified moneyline sides or rules")
        idx = labels.index(names[0])
    else:
        family = {
            "full.total": "total",
            "full.team_total.home": "home_total",
            "full.team_total.away": "away_total",
        }.get(meta.get("line_family"))
        if (
            not family
            or typ != ("totals" if family == "total" else "team_totals")
            or meta.get("line_kind")
            not in (("consensus",) if main_only else ("consensus", "alternate"))
            or set(labels) != {"over", "under"}
        ):
            raise ValueError("not a supported full-game main line")
        line = number(market.get("line"))
        if line <= 0 or line % 1 != Decimal("0.5"):
            raise ValueError("ambiguous total threshold")
        prefix = "-total-" if family == "total" else "-team-total-"
        if family != "total":
            side = family.split("_")[0]
            club = game["clubs"][side]
            prefix += club["abbreviation"].lower() + "-"
        if market_slug != slug + prefix + format(line, "f").replace(".", "pt"):
            raise ValueError("ambiguous market slug or threshold")
        idx = labels.index("over")
    try:
        basis = _rules_basis(game, family, line, rules)
        original, latest = rule_times(rules)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuleTemplateError(str(exc)) from exc
    if original.astimezone(_EAST).date().isoformat() != game["original_date"]:
        raise ValueError("original scheduled date conflicts with NBA fixture")
    if not native_only and moment(game["kickoff"]) > latest:
        raise ValueError("rule_deadline_exceeded")
    return {
        "family": family,
        "condition_id": condition,
        "market_id": str(market["id"]),
        "token_id": tokens[idx],
        "opposite": tokens[1 - idx],
        "line": float(line) if line else None,
        "rules": rules,
        "fee_schedule": market.get("feeSchedule"),
        "fees_enabled": market.get("feesEnabled"),
        "ot_basis": basis,
        "original_kickoff": stamp(original),
        "start_deadline": stamp(latest),
    }


def contract_inventory(event, game, *, active=True, main_only=True, native_only=False):
    rows, issues = [], defaultdict(Counter)
    for market in event.get("markets", []):
        if not isinstance(market, dict) or not (family := _family(event, market)):
            continue
        if active and not _open(market):
            issues[family]["listed_inactive"] += 1
            continue
        meta = market.get("marketMetadata")
        if (
            main_only
            and isinstance(meta, dict)
            and meta.get("line_kind") == "alternate"
        ):
            continue
        try:
            rows.append(
                contract(
                    event, game, market, main_only=main_only, native_only=native_only
                )
            )
        except RuleTemplateError:
            issues[family]["rule_template_mismatch"] += 1
        except (TypeError, ValueError, KeyError, OverflowError):
            issues[family]["unverified_contract"] += 1
    ids = Counter(row["condition_id"].lower() for row in rows)
    tokens = Counter(t for row in rows for t in (row["token_id"], row["opposite"]))
    verified = []
    for row in rows:
        if (
            ids[row["condition_id"].lower()]
            == tokens[row["token_id"]]
            == tokens[row["opposite"]]
            == 1
        ):
            verified.append(row)
        else:
            issues[row["family"]]["unverified_contract"] += 1
    return verified, issues


def contracts(event, game, *, active=True, main_only=True, native_only=False):
    rows, _ = contract_inventory(
        event, game, active=active, main_only=main_only, native_only=native_only
    )
    return rows


def price(candidate, books, received):
    sides = []
    for token in (candidate["token_id"], candidate["opposite"]):
        b = books.get(token)
        if (
            not b
            or b.get("market") != candidate["condition_id"]
            or str(b.get("asset_id")) != token
        ):
            return None
        try:
            # The exchange timestamp can predate receipt on an unchanged, thin book.
            # HTTP freshness establishes when the live depth was actually verified.
            if moment(b["timestamp"]) > received:
                return None
            bids, asks = levels(b["bids"], True), levels(b["asks"], False)
            if not bids or not asks:
                return None
            bid, ask = number(bids[0]["price"]), number(asks[0]["price"])
            if bid >= ask:
                return None
            sides.append((bid, ask))
        except (TypeError, ValueError, KeyError, OverflowError):
            return None
    bid, ask = sides[0]
    if bid != 1 - sides[1][1] or ask != 1 - sides[1][0]:
        return None
    mid = (bid + ask) / 2
    rank = (abs(mid - Decimal("0.5")), candidate["condition_id"].lower())
    return rank, float(mid)


def selected_line(candidates, quotes):
    """Choose the closest verified two-sided midpoint, then its native condition ID."""
    return min(
        (c for c in candidates if c["condition_id"] in quotes),
        key=lambda c: quotes[c["condition_id"]][0],
        default=None,
    )
