"""Separate an explicit match-availability statement from the scene of an injury."""

import re
from datetime import datetime, timedelta, timezone

from . import football, store


# The Premier League's dated fixture release identifies all 20 competing clubs.
# Pairings form a locally reproducible projection of the opening round.
WIRE_MEMBERSHIP = {
    "source": "touchline-availability",
    "season": "2026-27",
    "competition": "epl",
    "provider_scope_url": "https://touchlinewire.com/docs",
    "membership_url": "https://www.premierleague.com/en/news/4675508/premier-league-fixture-schedulereleased-for-season-202627",
    "published_by": "2026-06-19T23:59:59.999999Z",
    "valid_from": "2026-07-01T00:00:00.000000Z",
    "valid_until": "2027-07-01T00:00:00.000000Z",
    "opening_round": (
        ("Arsenal", "Coventry City"), ("Hull City", "Manchester United"),
        ("Everton", "Crystal Palace"), ("Ipswich Town", "Sunderland"),
        ("Nottingham Forest", "Leeds United"), ("Brentford", "Tottenham Hotspur"),
        ("Brighton & Hove Albion", "Aston Villa"), ("Manchester City", "AFC Bournemouth"),
        ("Newcastle United", "Liverpool"), ("Fulham", "Chelsea"),
    ),
}
WIRE_MEMBERS = {football.team_key(team) for pair in WIRE_MEMBERSHIP["opening_round"] for team in pair}


def competition(fixture):
    """The pre-multileague store also allowed operator-assigned opaque EPL fixture IDs."""
    fixture = dict(fixture)
    fid = fixture.get("fixture_id", "")
    named = fixture.get("competition") or fixture.get("league")
    prefix = fid.split("/", 1)[0] if "/" in fid else None
    return (None if named and prefix and named != prefix else named or prefix or "epl")


def wire_coverage(team, season, league="epl", at=None):
    spec = WIRE_MEMBERSHIP
    club = football.team_key(team)
    if league != spec["competition"] or season != spec["season"] or club not in WIRE_MEMBERS:
        return None
    if at is not None and not spec["valid_from"] <= store.iso(at) < spec["valid_until"]:
        return None
    return {"source": spec["source"], "competition": league, "season": season, "club": club,
            "membership_sha256": store.digest(spec), "membership_url": spec["membership_url"],
            "provider_scope_url": spec["provider_scope_url"], "published_by": spec["published_by"],
            "valid_from": spec["valid_from"], "valid_until": spec["valid_until"]}


def injury_date_upper_bound(day, snapshot):
    """Latest possible onset in a civil date with an unreported local time zone."""
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("invalid native injury date")
    try:
        midnight = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        earliest = midnight - timedelta(hours=14)
        latest = midnight + timedelta(hours=36, microseconds=-1)
    except (ValueError, OverflowError) as exc:
        raise ValueError("invalid native injury date") from exc
    if earliest > snapshot:
        raise ValueError("injury date after provider snapshot")
    return min(latest, snapshot)


_LEAGUE_NAMES = {
    "epl": ("epl", "premier league", "english premier league"),
    "lal": ("la liga", "laliga"), "bun": ("bundesliga",),
    "sea": ("serie a",), "fl1": ("ligue 1",),
}


def suspension_applies(row, league):
    """Use the target of the suspension, never the club's general league membership."""
    if row.get("event") != "suspension":
        return True
    names = _LEAGUE_NAMES.get(league, ())
    if row.get("source") == "touchline-availability" or row.get("time_basis") == "provider_snapshot":
        target = row.get("suspension_competition")
        return isinstance(target, str) and target.strip().casefold() in {league, *names}
    quote = row.get("quote", "")
    if not isinstance(quote, str) or COMPETITION.search(quote):
        return False
    # Require the ban to target the domestic competition in the same clause.
    # A description of a previous league card or a league club is insufficient.
    name = "(?:" + "|".join(map(re.escape, names)) + ")"
    return bool(names and re.search(
        rf"\b(?:suspended|banned|ineligible|ruled out) (?:for|from|in) (?:the |all |his |their |next |upcoming )*{name}\b"
        rf"|\b{name}[ -](?:suspension|ban)\b|\b(?:will|must) miss (?:the |his |their |next |upcoming )*{name}\b",
        quote, re.I))


COMPETITION = re.compile(
    r"\b(?:champions league|europa league|conference league|fa cup|league cup|carabao cup|"
    r"efl cup|world cup|international (?:duty|break|fixture|match|friendly)|"
    r"national team|nations league|european championship|uefa|friendl(?:y|ies)|"
    r"youth league|under[- ](?:18|21|23)|u(?:18|21|23)|women['’]s)\b", re.I,
)
_INJURY = re.compile(
    r"\b(?:injur\w*|hurt|suffer\w*|sustain\w*|pick(?:ed|ing) up|knock|strain\w*|"
    r"sprain\w*|fractur\w*|twist\w*|tore|torn|limp\w*|ligament|concussion)\b", re.I,
)
_DECISION = re.compile(
    r"\b(?:will (?:not |now |also )?(?:miss|play|feature|face|start|be)|would (?:miss|sideline)|"
    r"(?:set|expected|likely) to miss|ruled out|unavailable|available|doubtful|"
    r"fit (?:to|for)|sidelined|out of|back in the squad)\b", re.I,
)
MATCH_TARGET = re.compile(
    r"\b(?:premier league|premiership|epl|against|versus|vs\.?|match|game|fixture|clash|"
    r"trip to|visit of|(?:coming|upcoming|rest of the|remainder of the) season|"
    r"(?:days?|weeks?|months?)|this weekend)\b", re.I,
)
_BOUNDARY = re.compile(
    r",\s*|\s+\b(?:after|following|having|despite|because of|due to|"
    r"and(?:\s+(?:so|therefore|now))?|but)\s+", re.I,
)


def injury_headline(title, target):
    """A title naming the injury scene can accompany an explicit league decision."""
    return (_INJURY.search(title) and COMPETITION.search(title) and not _DECISION.search(title)
            and _DECISION.search(target) and re.search(r"\b(?:premier league|premiership|epl)\b", target, re.I))


def competition_target(text):
    """Return only the target clause when another competition describes a medical incident."""
    if not COMPETITION.search(text):
        return text
    for boundary in _BOUNDARY.finditer(text):
        before, after = text[:boundary.start()], text[boundary.end():]
        for main, background in ((before, after), (after, before)):
            if (not COMPETITION.search(main) and _DECISION.search(main) and MATCH_TARGET.search(main)
                    and COMPETITION.search(background) and _INJURY.search(background)
                    and not _DECISION.search(background)):
                return main
    return None
