"""Venue facts used by both learning and inference, restricted to dated regular-season matchups."""

from datetime import date
from zoneinfo import ZoneInfo

from .markets_contracts import team_code

POLICY = "listed-home-with-dated-neutral-exceptions-v1"
EASTERN = ZoneInfo("America/New_York")

# Only manually verified NBA announcements. Days below are publication days in ET.
# The next Eastern calendar day is the conservative first usable time, even when
# an article supplied a timestamp. A rescheduled date or different opponent must
# be independently checked; a city name alone never changes a game's identity.
_PR = "https://pr.nba.com/"
_NEWS = "https://www.nba.com/news/"
_RELEASES = (
    ("2022-08-17", _PR + "2022-23-nba-schedule/",
     (("2022-12-17", "MIA", "SAS"), ("2023-01-19", "CHI", "DET"))),
    ("2023-08-17", _PR + "2023-24-nba-regular-season-schedule/",
     (("2023-11-09", "ATL", "ORL"), ("2024-01-11", "BKN", "CLE"))),
    ("2023-12-06", _NEWS + "nba-starting-5-dec-6-2023",
     (("2023-12-07", "IND", "MIL"), ("2023-12-07", "LAL", "NOP"))),
    ("2024-05-02", _PR + "wembanyama-and-spurs-to-face-haliburton-and-pacers-in-two-regular-season-games-in-paris-in-january-2025/",
     (("2025-01-23", "IND", "SAS"), ("2025-01-25", "IND", "SAS"))),
    ("2024-06-14", _PR + "heat-wizards-nba-mexico-city-game-2024/", (("2024-11-02", "MIA", "WAS"),)),
    ("2024-12-12", _NEWS + "starting-5-dec-12-2024",
     (("2024-12-14", "ATL", "MIL"), ("2024-12-14", "HOU", "OKC"))),
    ("2025-06-10", _PR + "mavericks-pistons-mexico-city-2025/", (("2025-11-01", "DAL", "DET"),)),
    ("2025-07-30", _PR + "grizzlies-magic-berlin-london-2026-games/",
     (("2026-01-15", "MEM", "ORL"), ("2026-01-18", "MEM", "ORL"))),
    ("2025-12-11", _NEWS + "starting-5-dec-11-2025",
     (("2025-12-13", "NYK", "ORL"), ("2025-12-13", "OKC", "SAS"))),
    ("2026-04-16", _PR + "nuggets-pacers-nba-mexico-city-game-2026/", (("2026-11-07", "DEN", "IND"),)),
    ("2026-05-20", _PR + "pelicans-spurs-paris-manchester/",
     (("2027-01-14", "NOP", "SAS"), ("2027-01-17", "NOP", "SAS"))),
)
_BY_DAY = {}
for published, url, matchups in _RELEASES:
    for day, first, second in matchups:
        _BY_DAY.setdefault(day, {})[frozenset((first, second))] = (published, url)


def resolve(game, as_of=None):
    """Return the neutral flag, basis, and optional public source as of prediction."""
    day = str(game["game_date"])
    entries = _BY_DAY.get(day)
    if entries and game.get("stage") == "regular":
        teams = frozenset(team_code(game.get(side + "_abbr") or game[side]) for side in ("home", "away"))
        entry = entries.get(teams)
        if entry:
            cutoff_day = as_of.astimezone(EASTERN).date() if as_of else date.fromisoformat(day)
            if cutoff_day > date.fromisoformat(entry[0]):
                return True, "documented_neutral", entry[1]
    if game.get("neutral") is True:
        return True, "confirmed_neutral", None
    if game.get("neutral") is False:
        return False, "confirmed_non_neutral", None
    # All model training/inference entry points require a regular fixture and an
    # ordered pair of designated teams. We track this inference separately.
    return False, "assumed_listed_home", None
