"""Competition, OpenFootball identity and match-lifecycle registry for men's regular seasons."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from functools import cache
from importlib.resources import files

SOURCE_COMMIT = "e6744429ee395bc86f247348c6184bb08d4eb361"
SOURCE_ROOT = f"https://github.com/openfootball/football.json/blob/{SOURCE_COMMIT}"
REGISTRY_VERSION = "openfootball-five-v1"


@dataclass(frozen=True)
class LeagueProfile:
    code: str
    archive_code: str
    name: str
    timezone: str
    lineup_deadline_minutes_2026: int
    rule_source: str
    competition_boundary_2026: str

    @property
    def source_file(self) -> str:
        return f"{self.archive_code}.1.json"


PROFILES = {p.code: p for p in (
    LeagueProfile("epl", "en", "Premier League", "Europe/London", 75,
                  "https://www.premierleague.com/en/media/publications",
                  "Bottom three relegated. An additional title, relegation or European-place decider is a separate stage."),
    LeagueProfile("lal", "es", "LaLiga", "Europe/Madrid", 75,
                  "https://rfef.es/sites/default/files/pdf/circulares/1._Circular_84_-_CNL_Primera_y_Segunda_Division___Anexo.pdf?cb=6df81e55",
                  "Bottom three relegated. Two-club and multi-club head-to-head tie-breaks differ."),
    LeagueProfile("bun", "de", "Bundesliga", "Europe/Berlin", 60,
                  "https://www.bundesliga.com/en/bundesliga/news/how-does-promotion-and-relegation-work-in-the-bundesliga-10645",
                  "Bottom two relegated. The 16th club's two-legged relegation play-off is separate."),
    LeagueProfile("sea", "it", "Serie A", "Europe/Rome", 60,
                  "https://www.legaseriea.it/serie-a/news/criteri-compilazione-calendario-serie-a-enilive-2026-2027",
                  "Bottom three relegated. Applicable title and relegation play-offs are separate stages."),
    LeagueProfile("fl1", "fr", "Ligue 1", "Europe/Paris", 75,
                  "https://www.lfp.fr/article/l-assemblee-generale-adopte-a-97-28-le-passage-a-18-clubs",
                  "From 2023/24 there are 18 clubs. Bottom two relegated, 16th club's promotion/relegation play-off is separate."),
)}


def profile(league: str) -> LeagueProfile:
    try:
        return PROFILES[league]
    except KeyError as exc:
        raise ValueError(f"Unknown league: {league}") from exc


def season_start_year(season: str) -> int:
    if not isinstance(season, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", season):
        raise ValueError(f"Invalid season: {season}")
    year = int(season[:4])
    if not 1 <= year <= 9998 or (year + 1) % 100 != int(season[-2:]):
        raise ValueError(f"Invalid season: {season}")
    return year


def team_count(league: str, season: str) -> int:
    profile(league)
    year = season_start_year(season)
    return 18 if league == "bun" or (league == "fl1" and year >= 2023) else 20


def round_count(league: str, season: str) -> int:
    return 2 * (team_count(league, season) - 1)


def fixture_count(league: str, season: str) -> int:
    n = team_count(league, season)
    return n * (n - 1)


def season_window(league: str, season: str) -> tuple[date, date]:
    profile(league)
    year = season_start_year(season)
    if league == "sea" and season == "2019-20":
        return date(2019, 8, 1), date(2020, 8, 3)
    return date(year, 8, 1), date(year + 1, 8, 1)


@cache
def _archive() -> dict:
    member_asset = files(__package__).joinpath("assets/league-memberships-v1.json.gz")
    return json.loads(gzip.decompress(member_asset.read_bytes()))


def source_blobs(league: str) -> dict[str, str]:
    profile(league)
    return {s: row["blob"] for s, row in _archive()[league].items()}


def source_blob(league: str, season: str) -> str:
    return _season_record(league, season)["blob"]


def git_blob(raw: bytes) -> str:
    return hashlib.sha1(f"blob {len(raw)}\0".encode("ascii") + raw).hexdigest()


def _season_record(league: str, season: str) -> dict:
    profile(league)
    season_start_year(season)
    try:
        return _archive()[league][season]
    except KeyError as exc:
        raise ValueError(f"Unregistered OpenFootball season: {league}/{season}") from exc


def members(league: str, season: str) -> tuple[str, ...]:
    """Verbatim names in this pinned archive season, including all promoted clubs."""
    return tuple(_season_record(league, season)["teams"])


# Older literal source spelling to the stable spelling. Effective dates are the
# observed seasons per spelling in the pinned source, including gaps/relegations.
# Other providers need their own reviewed alias set.
ALIASES = {
    "epl": {},
    "lal": {
        "Atlético Madrid": "Club Atlético de Madrid", "CD Alavés": "Deportivo Alavés",
        "Espanyol Barcelona": "RCD Espanyol de Barcelona", "Rayo Vallecano": "Rayo Vallecano de Madrid",
        "RC Celta": "RC Celta de Vigo", "Real Betis": "Real Betis Balompié", "Real Madrid": "Real Madrid CF",
        "Real Sociedad": "Real Sociedad de Fútbol", "Real Valladolid": "Real Valladolid CF",
        "Deportivo La Coruña": "RC Deportivo La Coruña",
    },
    "bun": {
        "1899 Hoffenheim": "TSG 1899 Hoffenheim", "Bayer Leverkusen": "Bayer 04 Leverkusen",
        "Bor. Mönchengladbach": "Borussia Mönchengladbach", "Werder Bremen": "SV Werder Bremen",
        "FC St. Pauli": "FC St. Pauli 1910", "SpVgg Greuther Fürth": "SpVgg Greuther Fürth 1903",
    },
    "sea": {
        "Atalanta": "Atalanta BC", "Bologna FC": "Bologna FC 1909", "Inter": "FC Internazionale Milano",
        "Lazio Roma": "SS Lazio", "Sampdoria": "UC Sampdoria", "Sassuolo Calcio": "US Sassuolo Calcio",
    },
    "fl1": {
        "Olympique Marseille": "Olympique de Marseille", "RC Lens": "Racing Club de Lens",
        "RC Strasbourg": "RC Strasbourg Alsace", "Stade Rennais": "Stade Rennais FC 1901",
        "ESTAC Troyes": "ES Troyes AC",
    },
}


@cache
def _season_names(league: str, season: str) -> frozenset[str]:
    return frozenset(members(league, season))


def club_key(league: str, season: str, raw_name: str) -> str:
    """Scoped keys. A partial/custom archive must validate source names at ingestion."""
    from .football import team_key

    profile(league)
    season_start_year(season)
    if not isinstance(raw_name, str) or not (key := team_key(raw_name)):
        raise ValueError(f"Invalid club name: {raw_name!r}")
    canonical = ALIASES[league].get(raw_name)
    if canonical and season in _archive()[league] and raw_name in _season_names(league, season):
        return team_key(canonical)
    return key


def validate_members(league: str, season: str, raw_names, *, require_complete: bool = False) -> None:
    expected = _season_names(league, season)
    supplied = set(raw_names)
    unknown = supplied - expected
    if unknown:
        raise ValueError(f"{league}/{season}: unknown OpenFootball member spelling(s): {sorted(unknown)!r}")
    if require_complete and supplied != expected:
        raise ValueError(f"{league}/{season}: missing registered clubs: {sorted(expected - supplied)!r}")
    if len({club_key(league, season, raw) for raw in supplied}) != len(supplied):
        raise ValueError(f"{league}/{season}: two source clubs share a canonical identity")


def alias_rows(league: str) -> list[dict]:
    p = profile(league)
    seasons = _archive()[league]
    rows = []
    for old, stable in sorted(ALIASES[league].items()):
        observed = {spelling: [s for s, row in seasons.items() if spelling in row["teams"]]
                    for spelling in (old, stable)}
        evidence = {s: f"{SOURCE_ROOT}/{s}/{p.source_file}"
                    for s in sorted(set(observed[old]) | set(observed[stable]))}
        rows.append({"source": "openfootball", "league": league, "raw": old, "canonical": stable,
                     "effective_seasons_by_spelling": observed, "sources_by_season": evidence})
    return rows


@dataclass(frozen=True)
class MatchException:
    kind: str
    archive_date: str
    first_played_on: str | None
    completion_date: str | None
    sources: tuple[str, ...]
    first_kickoff_utc: str | None = None
    predict_prematch: bool = False

    @property
    def train_regulation(self) -> bool:
        return self.kind in {"resumed", "replayed"} and self.completion_date is not None


# A pinned archive usually labels neither interruptions nor later resumptions.
# Keys are ordered and stage-scoped. Archive calendar, first play, full-score
# completion and actual first-whistle timestamp are four different observations.
_EXCEPTION_ROWS = (
    ("epl", "2023-24", "AFC Bournemouth", "Luton Town FC", "replayed", "2024-03-13", "2023-12-16", "2024-03-13", (
        "https://www.premierleague.com/en/news/3830154",
        "https://www.premierleague.com/en/news/3885238",
        "https://www.premierleague.com/en/news/3930809")),
    ("lal", "2023-24", "Granada CF", "Athletic Club", "resumed", "2023-12-11", "2023-12-10", "2023-12-11", (
        "https://www.athletic-club.eus/en/news/2023/12/10/athletic-club-vs-granada-cf-will-resume-on-monday-at-21-00-cet/",
        "https://rfef.es/es/noticias/el-granada-athletic-se-reanudara-este-lunes-las-21-horas",
        "https://www.athletic-club.eus/en/teams/athletic-club/2023-24/matches/1_8394/")),
    ("lal", "2026-27", "Rayo Vallecano de Madrid", "Athletic Club", "resumed", "2026-10-10", "2026-10-10", None, (
        "https://www.athletic-club.eus/en/news/2026/10/10/the-athletic-club-squad-returns-to-bilbao/",
        "https://www.laliga.com/partido/temporada-2026-2027-laliga-ea-sports-rayo-vallecano-athletic-club-8")),
    ("bun", "2021-22", "VfL Bochum 1848", "Borussia Mönchengladbach", "awarded", "2022-03-18", "2022-03-18", None, (
        "https://www.dfb.de/fileadmin/_dfbdam/257416-220324_VfL_Bochum.pdf",)),
    ("bun", "2024-25", "1. FC Union Berlin", "VfL Bochum 1848", "awarded", "2024-12-14", "2024-12-14", None, (
        "https://www.dfb.de/news/dfb-sportgericht-wertet-bundesligaspiel-in-berlin-fuer-bochum",)),
    ("sea", "2020-21", "Hellas Verona FC", "AS Roma", "awarded", "2020-09-19", "2020-09-19", None, (
        "https://www.asroma.com/en/news/59098/verona-0-0-roma-season-starts-with-a-draw",
        "https://www.asroma.com/en/news/59181/roma-to-appeal-against-decision-on-verona-result")),
    ("sea", "2023-24", "Udinese Calcio", "AS Roma", "resumed", "2024-04-25", "2024-04-14", "2024-04-25", (
        "https://www.udinese.it/news/squad/date-confirmed-for-resumption-of-udinese-v-roma",
        "https://www.asroma.com/en/news/71049/gallery-late-goal-seals-all-three-points")),
    ("sea", "2024-25", "ACF Fiorentina", "FC Internazionale Milano", "resumed", "2025-02-06", "2024-12-01", "2025-02-06", (
        "https://www.inter.it/en/news/rescheduled-continuation-fiorentina-inter-6-february-2025",
        "https://www.inter.it/en/news/continuation-fiorentina-inter-serie-a-24-25")),
    ("fl1", "2021-22", "OGC Nice", "Olympique Marseille", "replayed", "2021-10-27", "2021-08-22", "2021-10-27", (
        "https://www.ogcnice.com/en/article/119352/nice-marseille-to-be-played-in-troyes.html",
        "https://www.ogcnice.com/en/resume/119587/a-derby-without-a-winner-1-1.html")),
    ("fl1", "2021-22", "Olympique Lyonnais", "Olympique Marseille", "replayed", "2022-02-01", "2021-11-21", "2022-02-01", (
        "https://www.lfp.fr/article/commission-de-discipline-les-decisions-du-8-decembre-2021",
        "https://www.lfp.fr/article/olympique-lyonnais-olympique-de-marseille-reprogramme-le-1er-ou-le-10-fevrier-2022")),
    ("fl1", "2023-24", "Montpellier HSC", "Clermont Foot 63", "replayed", "2023-11-29", "2023-10-08", "2023-11-29", (
        "https://www.lfp.fr/article/commission-de-discipline-les-decisions-du-25-octobre-2023",
        "https://www.lfp.fr/article/montpellier-herault-sc-clermont-foot-63-reprogramme-j8",
        "https://www.clermontfoot.com/le-clermont-foot-63-decroche-un-point-important-a-montpellier")),
    ("fl1", "2024-25", "Montpellier HSC", "AS Saint-Étienne", "awarded", "2025-03-16", "2025-03-16", None, (
        "https://www.lfp.fr/article/commission-de-discipline-les-decisions-du-2-avril-2025",)),
    ("fl1", "2025-26", "FC Nantes", "Toulouse FC", "abandoned", "2026-05-17", "2026-05-17", None, (
        "https://www.lfp.fr/article/commission-de-discipline-les-decisions-du-27-mai-2026",)),
)


@cache
def _exceptions() -> dict:
    table = {(lg, yr, h, a): MatchException(kind, archive, first, finish, sources)
             for lg, yr, h, a, kind, archive, first, finish, sources in _EXCEPTION_ROWS}
    authority = ("https://www.conseil-etat.fr/fr/arianeweb/CE/decision/2020-10-23/440810",)
    for h, a, day in _season_record("fl1", "2019-20")["cancelled"]:
        table["fl1", "2019-20", h, a] = MatchException("cancelled", day, None, None, authority)
    return table


def fixture_exception(league: str, season: str, home: str, away: str, *,
                      stage: str = "regular") -> MatchException | None:
    if stage != "regular":
        return None
    return _exceptions().get((league, season, home, away))


def source_timezone(league: str, source: str = "openfootball") -> str:
    """Archive wall-clock display zone; a stadium or another feed can differ."""
    if source != "openfootball":
        raise ValueError(f"Unregistered source clock for {league}/{source}")
    return profile(league).timezone


def venue_timezone(league: str, home: str, *, neutral: bool = False) -> str | None:
    """Default domestic home ground only. Neutral/relocated venues need separate evidence."""
    if neutral:
        return None
    if league == "lal" and home in {"UD Las Palmas", "CD Tenerife"}:
        return "Atlantic/Canary"
    return profile(league).timezone
