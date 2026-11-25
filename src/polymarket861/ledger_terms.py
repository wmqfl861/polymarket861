"""Derive executable EPL contract terms from an authenticated, dated Gamma response."""

import hashlib
import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import dbscope, store
from .ledger_rules import instant, number
from .market_provider import teams

# Reviewed complete rule suffixes. Keys cover every character after whitespace,
# quote and dash normalization, including data sources, clocks and exception clauses.
# Named parameters are limited to the date printed identically throughout the new rules.
# The prefix separately must name precisely this club, half-goal line and token family.
# The first three were observed on 2026-03-01 ARS-CHE, the next four on 2026-08-21
# ARS-COV, the following three on 2026-05-17 AVL-LIV, four on 2026-11-01 LIV-ARS,
# and the final four on 2026-11-27 NFO-CHE. Different wording requires a fresh review.
_REVIEW = {
    "6199a37fb2ef91156ea94d99550902d4244d3f79b338a00502958d30ac2ede17": (
        "total",
        "legacy",
    ),
    "868575a4dac7543a1b0bd8f312026ee4b53401105452c83864c3b777b80e661a": (
        "win",
        "legacy",
    ),
    "ae448be57cfe694dbf4514a4d48f724587a4a02f3cf33fc7534550d3f3d40282": (
        "draw",
        "legacy",
    ),
    "fbcb96f1514a0901ad73d84129234b6558247d797cc3df0241cadcb66b99ea18": (
        "total",
        "legacy",
    ),
    "e0a553be0e538f75f510e9ceb61589ff4feebeead431db62182797ee2b4effdb": (
        "team_total",
        "legacy",
    ),
    "eb81babafa702cfe90d3d4ec29377d5f63ba36ae080f88fd8f01f6eac97a5300": (
        "win",
        "legacy",
    ),
    "2ae0e9bf868abb3793edd2f022c64623901f6c3e70b53d2b037245e5d0bed6b3": (
        "draw",
        "legacy",
    ),
    "5b2dad8c934404876902603999dfd4db0bc641f8e2ad94367f65c7d603d657b6": (
        "total",
        "legacy",
    ),
    "b1b794392bef1d4d7c7cc168804bcfbe8200e85287825bea9c8091c11221514a": (
        "win",
        "legacy",
    ),
    "be054db2c677accc5aa4346241131b6b64fb81e952cdd4434f08a2ded5941cd3": (
        "draw",
        "legacy",
    ),
    "e2383431bf6ca3cd4927539ede417fb43a31b6e3790c41648209ef36644680b4": (
        "total",
        "legacy_24h",
    ),
    "1bbc2ebd68ba0dbb2babddb9c009e92a87930c9cfce3e44180214d919414baff": (
        "team_total",
        "legacy_24h",
    ),
    "4698756049a9b4476dcd14bde1c38fb0ab23b11da3dcaf62d2616b8a99c33625": (
        "win",
        "legacy_24h",
    ),
    "58b276686ff9b9b76506a6a5ae163c2003b7e834c6b1cdd303ac425c3a912675": (
        "draw",
        "legacy_24h",
    ),
    "b866f0da150c4c3ac75db2f0a14e9ccc8fb8f2008a44deb022058b7d74e9e502": (
        "total",
        "deadline",
    ),
    "11d9be8be4e642666943ff986db65a22305e61e2251112bc64ed107194cbb321": (
        "team_total",
        "deadline",
    ),
    "d3f73d64753f04bde832a22ecc656801a30d870abe0b6678a97d5eafc15f1136": (
        "win",
        "deadline",
    ),
    "c6dd5d80e9a97ff7fc3260f6b706a79a75ac8be3f72c5a0842c86e10fa62abe6": (
        "draw",
        "deadline",
    ),
}
_WEEK = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_MONTH = r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
_DAY = _MONTH + r" [0-9]{1,2}"
_YEAR = r", 20[0-9]{2}"
_TIME = r"[0-9]{1,2}:[0-9]{2} [ap]m et"
_DEADLINE = re.compile(_WEEK + ", " + _DAY + _YEAR + ", " + _TIME)
_REGULAR = "within the first 90 minutes of regular play plus stoppage time"
_TRANSLATE = str.maketrans({"–": "-", "’": "'", "“": '"', "”": '"'})


def _norm(value):
    return " ".join(value.casefold().translate(_TRANSLATE).split())


def _subject(market, rule):
    roster = teams({"teams": market.get("event_teams")})
    if len(roster) != 2:
        raise ValueError(
            "contract text requires independently identified home and away teams"
        )
    home, away = (_norm(roster[s]["name"]) for s in ("home", "away"))
    if not home or not away or home == away:
        raise ValueError("contract text has ambiguous participants")
    header = (
        "in the upcoming premier league game between "
        + re.escape(home + " and " + away)
        + ", "
    )
    old = r"scheduled for " + _DAY + " at " + _TIME + ": "
    new = (
        "originally scheduled for "
        + _WEEK
        + ", "
        + _DAY
        + _YEAR
        + " at "
        + _TIME
        + r" \(as of creation of the first market related to the event\): "
    )
    generic = "in the upcoming game, scheduled for " + _DAY + _YEAR + " "
    h = re.match("(?:" + header + "(?:" + old + "|" + new + ")|" + generic + ")", rule)
    if not h:
        raise ValueError("unreviewed participant, period or rule header")
    family, direction = market["family"], market["side"]
    club = home if direction == "home" else away
    if family == "moneyline":
        predicate = "the game ends in a draw" if direction == "draw" else club + " wins"
        prefix = f'if {predicate}, this market will resolve to "yes". otherwise, this market will resolve to "no".'
        kind = "draw" if direction == "draw" else "win"
    else:
        line = number(market["line"])
        if line < 0 or line > 50 or line % 1 != Decimal(".5"):
            raise ValueError("no reviewed full-time rule for this goal line")
        n = int(line + Decimal(".5"))
        if family == "total":
            prefix = (
                f'this market will resolve to "over" if {home} and {away} combine to score '
                f"{n} or more goals in this game. if the combined total is less than {n}, "
                'this market will resolve to "under".'
            )
            kind = "total"
        elif family in ("home_total", "away_total"):
            prefix = (
                f'this market will resolve to "over" if {club} score {n} or more goals {_REGULAR}. '
                f'if {club} score less than {n} goals {_REGULAR}, this market will resolve to "under". '
                f"this market is based solely on {club}'s score, not the combined score of both teams."
            )
            kind = "team_total"
        else:
            raise ValueError("unreviewed market family")
    rest = rule[h.end() :]
    if not rest.startswith(prefix + " "):
        raise ValueError(
            "rule text disagrees with archived period, side, participant or line"
        )
    return kind, rest[len(prefix) + 1 :]


def _interpret(market, token_side):
    raw = market["rules"]
    if not raw or hashlib.sha256(raw.encode()).hexdigest() != market["rule_sha256"]:
        raise ValueError("rule text SHA does not match source")
    kind, suffix = _subject(market, _norm(raw))
    deadlines = _DEADLINE.findall(suffix)
    digest = hashlib.sha256(_DEADLINE.sub("{deadline}", suffix).encode()).hexdigest()
    reviewed = _REVIEW.get(digest)
    if not reviewed or kind != reviewed[0]:
        raise ValueError(
            "unreviewed exception, resolution source or settlement wording"
        )
    regime = reviewed[1]
    deadline = None
    if regime == "deadline":
        if len(deadlines) != 4 or len(set(deadlines)) != 1:
            raise ValueError("inconsistent settlement deadlines in original rule")
        local = datetime.strptime(deadlines[0], "%A, %B %d, %Y, %I:%M %p et")
        if local.strftime("%A").lower() != deadlines[0].split(",")[0]:
            raise ValueError("settlement calendar weekday disagrees with date")
        deadline = store.iso(local.replace(tzinfo=ZoneInfo("America/New_York")))
    total = kind in ("total", "team_total")
    cancellation = (
        "0.5" if total else str(int((kind == "draw") == (token_side == "yes")))
    )
    # An awarded/final score is a different scenario from cancellation or no official result.
    # A pending exception cannot be used as unconditional cash in the ledger.
    special = {
        "postponed": "pending",
        "canceled": cancellation,
        "no_result": "pending",
        "abandoned": "pending",
    }
    if regime == "deadline":
        special.update(
            no_result=cancellation,
            no_start_by_deadline=cancellation,
            no_acceptable_data_24h=cancellation,
            unidentified_participant=cancellation,
            abandoned_no_official_result={
                "if_decided": True,
                "otherwise": cancellation,
            },
            awarded_result="official_score",
            played_result_declared_final="official_score",
            resumed_official_result="official_score",
        )
    else:
        if (
            "official final score" in suffix
            or "all markets will settle based on the official final result" in suffix
        ):
            special["abandoned_official_final"] = "official_score"
        if regime == "legacy_24h":
            # The shared fallback wording is ambiguous for separately traded Yes/No
            # moneylines. Await the venue resolution when "Draw where applicable" applies.
            fallback = "0.5" if total else "pending"
            special.update(no_result=fallback, no_acceptable_data_24h=fallback)
    return special, {
        "version": "epl-gamma-rules-v1",
        "template_sha256": digest,
        "deadline_utc": deadline,
        "no_data_hours": 24 if regime != "legacy" else None,
    }


def _fee(market, data_dir=None, at=None):
    switch = market.get("fees_enabled")
    if switch is True:
        from .fee_registry import resolve

        if data_dir is None or at is None:
            raise ValueError(
                "fee-enabled market requires original fee evidence at a verified time"
            )
        return resolve(data_dir, market, at)
    if switch is not False or market.get("fee_schedule") not in (None, {}):
        raise ValueError("unverified or contradictory market fee schedule")
    profile = {
        "policy": "gamma-fees-disabled-v1",
        "enabled": False,
        "venue": market["venue"],
        "condition_id": market["condition_id"],
        "protocol": market["protocol"],
    }
    seen = market["recorded_at"]
    return {
        "enabled": False,
        "version": store.digest(profile),
        "effective_at": seen,
        "received_at": seen,
        "source_sha256": market["source_sha256"],
        "effective_basis": "observed_switch",
    }


def from_original(data_dir, market, token_id, at, match_id=None):
    """Construct terms from original_market(), after origin and as-of validation."""
    return _terms_with_fee(
        data_dir, market, token_id, at, match_id, _fee(market, data_dir, at)
    )


def _terms_with_fee(data_dir, market, token_id, at, match_id, fee):
    """Apply the same sports contract and match binding after the caller has established a fee policy."""
    if market.get("mapping_error") or not isinstance(token_id, str):
        raise ValueError("unverified contract token")
    sides = [
        t["outcome"].casefold() for t in market["tokens"] if t["token_id"] == token_id
    ]
    if len(sides) != 1 or len(market["game_times"]) != 1:
        raise ValueError("ambiguous contract token or fixture schedule")
    fixture = market["fixture"]
    registered = False
    if match_id is not None:
        with dbscope.reading(data_dir) as connection:
            registered = (
                connection.execute(
                    "SELECT 1 FROM fixture_versions WHERE fixture_id=? LIMIT 1",
                    (match_id,),
                ).fetchone()
                is not None
            )
    if match_id is not None and (match_id != fixture or registered):
        from .opportunity import verified_fixture

        verified_fixture(data_dir, fixture, match_id, at)
    token_side = sides[0]
    special, rule_profile = _interpret(market, token_side)
    delay = market.get("seconds_delay")
    if type(delay) is not int or delay < 0:
        raise ValueError("missing original integer seconds delay")
    terms = {
        "venue": market["venue"],
        "match_id": match_id or fixture,
        "condition_id": market["condition_id"],
        "token_id": token_id,
        "period": "full_time",
        "side": token_side,
        "seconds_delay": delay,
        "rule_sha256": market["rule_sha256"],
        "rule_received_at": market["recorded_at"],
        "rule_source_sha256": market["source_sha256"],
        "rule_profile": rule_profile,
        "special": special,
        "fee_schedule": fee,
    }
    if market["family"] == "moneyline":
        terms.update(family="result", selection=market["side"])
    else:
        is_team = market["family"] != "total"
        terms.update(family="team_total" if is_team else "total", line=market["line"])
        if is_team:
            terms["team"] = market["side"]
    return terms


def contract_at(data_dir, condition_id, token_id, at, *, match_id=None):
    """Resolve one international token against the last original receipt by the cutoff."""
    from .archive_proof import original_market

    with dbscope.reading(data_dir) as db:
        market = original_market(db, data_dir, condition_id, at)
    return from_original(data_dir, market, token_id, at, match_id)


def attest(db, data_dir, supplied, at, kickoff):
    from .archive_proof import original_market

    market = original_market(db, data_dir, supplied["condition_id"], at)
    expected = from_original(
        data_dir, market, supplied["token_id"], at, supplied["match_id"]
    )
    if len(market["game_times"]) != 1 or instant(market["game_times"][0]) != kickoff:
        raise ValueError("contract kickoff differs from frozen fixture")
    # Full canonical comparison also rejects operator additions impacting payout
    # (e.g. equal_payout, split_quarter, fee expiry or custom scenario tables).
    if store.canonical(supplied) != store.canonical(expected):
        raise ValueError(
            "submitted contract terms differ from verified original rule or fee policy"
        )


def verify_book_terms(contract, market, *, data_dir=None, at=None):
    """A later quote must still have a valid fee policy and the same economics."""
    if (
        market["rule_sha256"] != contract["rule_sha256"]
        or market["seconds_delay"] != contract["seconds_delay"]
    ):
        raise ValueError("rule or execution delay changed at book capture")
    observed = _fee(market, data_dir, at)
    quoted_version = contract["fee_schedule"]
    if (
        observed["enabled"] is not quoted_version["enabled"]
        or observed["version"] != quoted_version["version"]
    ):
        raise ValueError("fee policy version changed at book capture")
