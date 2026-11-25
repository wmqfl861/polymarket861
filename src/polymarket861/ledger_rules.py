"""Pure pricing and contract rules for Polymarket International paper fills."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

D = Decimal
ZERO, ONE = D(0), D(1)
UNIT = D("0.000001")
MAX_SPREAD, MIN_EDGE, MAX_GAP = D(".04"), D(".02"), D(".08")
MAX_ORDER, MAX_MATCH = D(50), D(150)


def number(value, label="number"):
    try:
        x = D(str(value))
        if not x.is_finite():
            raise ValueError
        return x
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError(f"invalid {label}") from exc


def unit(value):
    x = number(value)
    if not ZERO <= x <= ONE:
        raise ValueError("probability or payout outside [0,1]")
    return x


def fmt(x):
    return format(x, "f")


def packed(data):
    def fallback(o):
        if isinstance(o, D):
            return fmt(o)
        raise TypeError(f"not JSON: {type(o).__name__}")

    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=fallback,
    )


def digest(data):
    return hashlib.sha256(packed(data).encode()).hexdigest()


def instant(value):
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            raise ValueError
        return dt.astimezone(UTC)
    except (ValueError, TypeError) as exc:
        raise ValueError("missing or ambiguous UTC timestamp") from exc


def iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def venue(obj):
    return obj.get("venue", obj.get("site"))


def check_contract(c, intent, cutoff):
    if not isinstance(c, dict) or venue(c) != "polymarket.com":
        raise ValueError("unverified international contract")
    if any(
        not str(c.get(k) or "").strip()
        for k in ("condition_id", "token_id", "rule_sha256")
    ):
        raise ValueError("missing contract or rule identity")
    if c.get("match_id") != intent["match_id"] or c.get("period") != "full_time":
        raise ValueError("unverified match or period")
    if instant(c.get("rule_received_at")) > cutoff:
        raise ValueError("rule first seen after decision")
    fam, side = c.get("family"), c.get("side")
    if fam == "result":
        if c.get("selection") not in ("home", "draw", "away") or side not in (
            "yes",
            "no",
        ):
            raise ValueError("unverified result mapping")
    elif fam in ("total", "team_total"):
        if side not in ("over", "under") or (
            fam == "team_total" and c.get("team") not in ("home", "away")
        ):
            raise ValueError("unverified total direction")
        line = number(c.get("line"), "line")
        if line < 0 or line > 50 or line * 4 != int(line * 4):
            raise ValueError("unsupported goal line")
        if line % 1 != D(".5") and c.get("equal_payout") is None:
            raise ValueError("integer or quarter line requires verified equal payout")
        if c.get("equal_payout") is not None:
            unit(c["equal_payout"])
        if line % D(".5") and c.get("split_quarter") is not True:
            raise ValueError("unverified split line rules")
    else:
        raise ValueError("unverified market family")
    specials = c.get("special")
    if not isinstance(specials, dict) or not specials or "normal" in specials:
        raise ValueError("missing special settlement rules")
    for value in specials.values():
        special_floor(value)
    delay = number(c.get("seconds_delay"), "seconds delay")
    if delay < 0 or delay != int(delay):
        raise ValueError("invalid seconds delay")
    fee = c.get("fee_schedule") or {}
    if not fee.get("version") or not isinstance(fee.get("enabled"), bool):
        raise ValueError("missing historical fee version")
    if (
        instant(fee.get("effective_at")) > cutoff
        or instant(fee.get("received_at")) > cutoff
    ):
        raise ValueError("fee schedule unavailable at decision")
    if fee.get("expires_at") and instant(fee["expires_at"]) <= cutoff:
        raise ValueError("expired historical fee version")
    if fee["enabled"]:
        if fee.get("model") != "symmetric" or fee.get("asset") not in (
            "cash",
            "shares",
        ):
            raise ValueError("unsupported fee formula or charging asset")
        if fee.get("rounding") not in ROUNDINGS or fee.get("round_in") not in (
            "cash",
            "charged",
        ):
            raise ValueError("missing fee rounding semantics")
        rate, exponent = unit(fee.get("rate")), number(fee.get("exponent"))
        quantum = number(fee.get("quantum"))
        if not (rate >= 0 and exponent in (1, 2) and UNIT <= quantum <= 1):
            raise ValueError("unsupported fee parameters")


def special_floor(value):
    if isinstance(value, dict):
        if value.get("if_decided") is not True or "otherwise" not in value:
            raise ValueError("unverified conditional special rule")
        return min(ZERO, special_floor(value["otherwise"]))
    if value in ("pending", "official_score"):
        return ZERO
    return unit(value)


def fair_values(normal, c):
    """Bounds under zero to one percent exceptional outcomes."""
    p = unit(normal)

    def bounds(v):
        if v == "official_score":
            return ZERO, D(1)
        if v == "pending":
            # The original contract continues to the eventual result, with no immediate payment.
            return p, p
        if isinstance(v, dict):
            lo, hi = bounds(v["otherwise"])
            if c["family"] == "result":
                return lo, hi
            determined = D(c["side"] == "over")
            return min(lo, determined), max(hi, determined)
        return unit(v), unit(v)

    spans = [bounds(v) for v in c["special"].values()]
    worst, best = min(x[0] for x in spans), max(x[1] for x in spans)
    return (
        min(p, D(".99") * p + D(".01") * worst),
        max(p, D(".99") * p + D(".01") * best),
        worst,
    )


def goals(value):
    n = number(value, "goals")
    if n < 0 or n != int(n):
        raise ValueError("invalid goal count")
    return int(n)


def normal_payout(c, home, away):
    h, a = goals(home), goals(away)
    if c["family"] == "result":
        winner = "home" if h > a else "away" if h < a else "draw"
        return D((c["selection"] == winner) == (c["side"] == "yes"))
    tally = h + a if c["family"] == "total" else h if c["team"] == "home" else a
    line = number(c["line"])

    def side_at(mark):
        if tally == mark:
            return unit(c["equal_payout"])
        return D((tally > mark) == (c["side"] == "over"))

    if line % D(".5"):
        return (side_at(line - D(".25")) + side_at(line + D(".25"))) / 2
    return side_at(line)


def forecast_fair(contract, seal):
    """Expected normal payout of this exact token under its sealed score distribution."""
    matrix = seal.get("matrix") or []
    values = [
        (h, a, number(p)) for h, row in enumerate(matrix) for a, p in enumerate(row)
    ]
    mass = sum((p for _, _, p in values), ZERO)
    if (
        mass <= 0
        or abs(mass - ONE) > number("0.00000001")
        or any(p < 0 for _, _, p in values)
    ):
        raise ValueError("forecast lacks a normalized nonnegative score distribution")
    return sum((p * normal_payout(contract, h, a) for h, a, p in values), ZERO) / mass


def scenario_payout(c, scenario, score=None):
    if scenario == "normal":
        if score is None:
            raise ValueError("normal settlement needs a final score")
        return normal_payout(c, *score)
    if scenario not in c["special"]:
        raise ValueError("unknown contract settlement scenario")
    value = c["special"][scenario]
    if isinstance(value, dict):
        if c["family"] != "result" and score is not None:
            h, a = map(goals, score)
            tally = h + a if c["family"] == "total" else h if c["team"] == "home" else a
            line = number(c["line"])
            upper = line + (D(".25") if line % D(".5") else ZERO)
            if tally > upper:
                return D(c["side"] == "over")
        value = value["otherwise"]
    if value == "official_score":
        return normal_payout(c, *score) if score is not None else None
    return None if value == "pending" else unit(value)


def series(book, side):
    levels = {}
    for row in book.get(side) or []:
        p, q = unit(row["price"]), number(row["size"], "depth")
        if p <= 0 or p >= 1 or q <= 0 or q % UNIT:
            raise ValueError("invalid price or size in book")
        levels[p] = levels.get(p, ZERO) + q
    if not levels:
        raise ValueError("incomplete two-sided order book")
    return sorted(levels.items(), reverse=side == "bids")


def check_book(book, c, target, kickoff, stage):
    if not isinstance(book, dict):
        raise TypeError("missing order book")
    if venue(book) != venue(c) or any(
        str(book.get(k)) != str(c[k]) for k in ("condition_id", "token_id")
    ):
        raise ValueError("venue, condition or token mismatch")
    if book.get("market_open") is not True or book.get("pregame") is not True:
        raise ValueError("unverified market or pregame state")
    req, seen, obs = (
        instant(book.get(k)) for k in ("requested_at", "received_at", "observed_at")
    )
    if req > seen or obs > seen or not seen < kickoff:
        raise ValueError("late or inconsistent book timestamps")
    if stage == "second":
        if not (target <= req <= seen <= target + timedelta(seconds=5)):
            raise ValueError("execution outside targeted five-second window")
        if obs < target:
            raise ValueError("execution quote predates targeted observation")
        ref = seen
    else:
        if seen > target:
            raise ValueError("quote arrived after cutoff")
        ref = target
    if ref - obs > timedelta(seconds=30) or ref - seen > timedelta(seconds=30):
        raise ValueError("stale book")
    if book.get("rule_sha256") != c.get("rule_sha256"):
        raise ValueError("missing or changed rule fingerprint")
    fee = c["fee_schedule"]
    if (
        stage != "close"
        and fee.get("expires_at")
        and seen >= instant(fee["expires_at"])
    ):
        raise ValueError("fee schedule changed")
    if (
        stage != "close"
        and book.get("fees_enabled") is not None
        and book["fees_enabled"] != fee["enabled"]
    ):
        raise ValueError("market fee switch changed")
    published_fee = book.get("fee_schedule") or {}
    if (
        stage != "close"
        and fee["enabled"]
        and any(
            published_fee.get(k) is not None
            and number(published_fee[k]) != number(fee[k])
            for k in ("rate", "exponent")
        )
    ):
        raise ValueError("published fee parameters changed")
    if (
        stage != "close"
        and book.get("fee_version") is not None
        and book["fee_version"] != fee["version"]
    ):
        raise ValueError("fee schedule changed")
    if (
        stage != "close"
        and book.get("seconds_delay") is not None
        and number(book["seconds_delay"]) != number(c["seconds_delay"])
    ):
        raise ValueError("market delay changed")
    tick, minimum = (
        number(book.get("tick_size"), "tick size"),
        number(book.get("min_order_size"), "minimum size"),
    )
    if tick <= 0 or minimum <= 0:
        raise ValueError("invalid tick or minimum order")
    bids, asks = series(book, "bids"), series(book, "asks")
    if any(p % tick for p, _ in bids + asks):
        raise ValueError("off-tick order book")
    spread = asks[0][0] - bids[0][0]
    if spread <= 0 or spread > MAX_SPREAD:
        raise ValueError("crossed or excessive spread")
    snapshot = (
        book.get("book_hash")
        or book.get("source_sha256")
        or digest(
            [
                c["condition_id"],
                c["token_id"],
                book.get("exchange_at") or book["observed_at"],
                [(fmt(p), fmt(q)) for p, q in bids + asks],
            ]
        )
    )
    return {
        "asks": asks,
        "mid": (asks[0][0] + bids[0][0]) / 2,
        "minimum": minimum,
        "key": str(snapshot),
        "received": seen,
    }


ROUNDINGS = {"half_up": ROUND_HALF_UP, "down": ROUND_DOWN, "up": ROUND_CEILING}


def charge(q, price, fee):
    if not fee["enabled"]:
        return ZERO, ZERO
    usd = q * number(fee["rate"]) * (price * (1 - price)) ** int(fee["exponent"])
    quantum = number(fee["quantum"])

    def rounded(value):
        return (value / quantum).to_integral_value(
            rounding=ROUNDINGS[fee["rounding"]]
        ) * quantum

    if fee["asset"] == "cash":
        return rounded(usd), ZERO
    if fee["round_in"] == "cash":
        return ZERO, (rounded(usd) / price).quantize(UNIT, rounding=ROUND_CEILING)
    return ZERO, rounded(usd / price)


def pair_depth(first, second, limit):
    """Align cumulative sizes, retaining both original price levels for liquidity debits."""
    if not first or not second:
        return []
    i = j = 0
    left, right = first[0][1], second[0][1]
    out = []
    while i < len(first) and j < len(second):
        a, b = first[i][0], second[j][0]
        p = max(a, b)
        if p > limit:
            break
        qty = min(left, right)
        out.append((p, qty, a, b))
        left, right = left - qty, right - qty
        if not left:
            i += 1
            if i < len(first):
                left = first[i][1]
        if not right:
            j += 1
            if j < len(second):
                right = second[j][1]
    return out


def price_fill(depth, quantity, fee):
    left = quantity
    grouped, used = {}, []
    for p, q, a, b in depth:
        take = min(q, left)
        if take:
            grouped[p] = grouped.get(p, ZERO) + take
            used.append((a, b, take))
            left -= take
        if not left:
            break
    shares, notional, fee_cash, fee_shares, fee_value = ZERO, ZERO, ZERO, ZERO, ZERO
    levels = []
    for price, size in grouped.items():
        cash, tokens = charge(size, price, fee)
        shares += size
        notional += size * price
        fee_cash += cash
        fee_shares += tokens
        fee_value += cash + tokens * price
        levels.append(
            {
                "price": fmt(price),
                "size": fmt(size),
                "fee_cash": fmt(cash),
                "fee_shares": fmt(tokens),
            }
        )
    spent = (notional + fee_cash).quantize(UNIT, rounding=ROUND_CEILING)
    net = shares - fee_shares
    return {
        "gross": shares,
        "net": net,
        "spent": spent,
        "notional": notional,
        "fees": fee_value,
        "cost": spent / net if net > 0 else ZERO,
        "levels": levels,
        "used": used,
    }


def fit(depth, requested, fee, budget, fair, *, risk_ok=None):
    high = min(requested, sum((r[1] for r in depth), ZERO))

    def ok(candidate):
        return (
            candidate["net"] > 0
            and candidate["spent"] <= budget
            and fair - candidate["cost"] >= MIN_EDGE
            and (risk_ok is None or risk_ok(candidate))
        )

    full = price_fill(depth, high, fee)
    if high <= 0 or ok(full):
        return full
    lo, hi = 0, int(high / UNIT)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(price_fill(depth, mid * UNIT, fee)):
            lo = mid
        else:
            hi = mid - 1
    return price_fill(depth, lo * UNIT, fee)
