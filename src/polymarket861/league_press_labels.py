"""Club names used in press coverage, scoped to the members of each source season."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from . import league_registry

# Reviewed against each league's club list and the pinned OpenFootball membership.
# Keep supplier-specific nicknames out of fixture binding and the forecast model.
# A new promotion still has its registered name; shorthand is added explicitly.
ALIASES = {
    # LaLiga
    "Athletic Club": ("Athletic Bilbao", "Athletic Club de Bilbao"),
    "CA Osasuna": ("Osasuna",),
    "Club Atlético de Madrid": ("Atlético de Madrid", "Atlético Madrid", "Atleti"),
    "Deportivo Alavés": ("Alavés", "CD Alavés"),
    "Elche CF": ("Elche",),
    "FC Barcelona": ("Barcelona", "Barça"),
    "Getafe CF": ("Getafe",),
    "Levante UD": ("Levante",),
    "Málaga CF": ("Málaga",),
    "RC Celta de Vigo": ("Celta", "Celta Vigo", "Celta de Vigo", "RC Celta"),
    "RC Deportivo La Coruña": ("RC Deportivo", "Deportivo La Coruña", "Deportivo de La Coruña", "Deportivo da Coruña"),
    "RCD Espanyol de Barcelona": ("RCD Espanyol", "Espanyol"),
    "Rayo Vallecano de Madrid": ("Rayo Vallecano", "Rayo"),
    "Real Betis Balompié": ("Real Betis", "Betis"),
    "Real Madrid CF": ("Real Madrid",),
    "Real Racing Club de Santander": ("Racing Santander", "Racing de Santander", "Real Racing Club"),
    "Real Sociedad de Fútbol": ("Real Sociedad", "La Real"),
    "Sevilla FC": ("Sevilla",),
    "Valencia CF": ("Valencia",),
    "Villarreal CF": ("Villarreal",),
    # Bundesliga
    "1. FC Köln": ("FC Köln", "Köln", "FC Koeln", "1 FC Cologne"),
    "1. FC Union Berlin": ("Union Berlin",),
    "1. FSV Mainz 05": ("FSV Mainz", "Mainz", "Mainz 05"),
    "Bayer 04 Leverkusen": ("Bayer Leverkusen", "Leverkusen"),
    "Borussia Dortmund": ("Dortmund", "BVB"),
    "Borussia Mönchengladbach": ("Borussia Moenchengladbach", "Mönchengladbach", "Gladbach"),
    "Eintracht Frankfurt": ("Eintracht", "Frankfurt"),
    "FC Augsburg": ("Augsburg",),
    "FC Bayern München": ("FC Bayern", "Bayern", "Bayern Munich", "Bayern München", "Bayern Muenchen"),
    "FC Schalke 04": ("Schalke", "Schalke 04"),
    "Hamburger SV": ("Hamburg", "HSV"),
    "RB Leipzig": ("Leipzig", "RasenBallsport Leipzig"),
    "SC Freiburg": ("Freiburg", "Sport-Club Freiburg"),
    "SC Paderborn 07": ("Paderborn", "SC Paderborn"),
    "SV 07 Elversberg": ("Elversberg", "SV Elversberg"),
    "SV Werder Bremen": ("Werder Bremen", "Werder", "Bremen", "SV Werder"),
    "TSG 1899 Hoffenheim": ("TSG Hoffenheim", "Hoffenheim", "1899 Hoffenheim"),
    "VfB Stuttgart": ("Stuttgart",),
    # Serie A
    "AC Milan": ("Milan",),
    "AC Monza": ("Monza",),
    "ACF Fiorentina": ("Fiorentina",),
    "AS Roma": ("Roma",),
    "Atalanta BC": ("Atalanta",),
    "Bologna FC 1909": ("Bologna", "Bologna FC"),
    "Cagliari Calcio": ("Cagliari",),
    "Como 1907": ("Como",),
    "FC Internazionale Milano": ("Inter Milan", "Inter Milano", "Internazionale Milano", "Internazionale", "Inter"),
    "Frosinone Calcio": ("Frosinone",),
    "Genoa CFC": ("Genoa",),
    "Juventus FC": ("Juventus", "Juve"),
    "Parma Calcio 1913": ("Parma", "Parma Calcio"),
    "SS Lazio": ("Lazio",),
    "SSC Napoli": ("Napoli",),
    "Torino FC": ("Torino",),
    "US Lecce": ("Lecce",),
    "US Sassuolo Calcio": ("Sassuolo", "Sassuolo Calcio"),
    "Udinese Calcio": ("Udinese",),
    "Venezia FC": ("Venezia",),
    # Ligue 1
    "AJ Auxerre": ("Auxerre",),
    "AS Monaco FC": ("AS Monaco", "Monaco"),
    "Angers SCO": ("Angers", "SCO Angers"),
    "ES Troyes AC": ("Troyes", "ESTAC Troyes", "ESTAC"),
    "FC Lorient": ("Lorient",),
    "Le Havre AC": ("Le Havre", "Havre Athletic Club"),
    "Le Mans FC": ("Le Mans",),
    "Lille OSC": ("Lille", "LOSC", "LOSC Lille"),
    "OGC Nice": ("Nice",),
    "Olympique Lyonnais": ("Lyon", "Olympique Lyon"),
    "Olympique de Marseille": ("Marseille", "Olympique Marseille"),
    "Paris FC": ("Paris Football Club",),
    "Paris Saint-Germain FC": ("Paris Saint-Germain", "Paris St Germain", "PSG"),
    "RC Strasbourg Alsace": ("Strasbourg", "RC Strasbourg"),
    "Racing Club de Lens": ("Lens", "RC Lens"),
    "Stade Brestois 29": ("Brest", "Stade Brestois"),
    "Stade Rennais FC 1901": ("Rennes", "Stade Rennais", "Stade Rennais FC"),
    "Toulouse FC": ("Toulouse",),
    # Premier League (same discovery index)
    "AFC Bournemouth": ("Bournemouth",),
    "Arsenal FC": ("Arsenal",),
    "Aston Villa FC": ("Aston Villa",),
    "Brentford FC": ("Brentford",),
    "Brighton & Hove Albion FC": ("Brighton", "Brighton and Hove Albion", "Brighton Hove Albion"),
    "Chelsea FC": ("Chelsea",),
    "Coventry City FC": ("Coventry", "Coventry City"),
    "Crystal Palace FC": ("Crystal Palace",),
    "Everton FC": ("Everton",),
    "Fulham FC": ("Fulham",),
    "Hull City AFC": ("Hull City", "Hull"),
    "Ipswich Town FC": ("Ipswich Town", "Ipswich"),
    "Leeds United FC": ("Leeds United", "Leeds"),
    "Liverpool FC": ("Liverpool",),
    "Manchester City FC": ("Manchester City", "Man City"),
    "Manchester United FC": ("Manchester United", "Man United", "Man Utd", "Manchester Utd"),
    "Newcastle United FC": ("Newcastle United", "Newcastle"),
    "Nottingham Forest FC": ("Nottingham Forest", "Nottm Forest"),
    "Sunderland AFC": ("Sunderland",),
    "Tottenham Hotspur FC": ("Tottenham Hotspur", "Tottenham", "Spurs"),
}


def words(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(re.findall(r"[^\W_]+", "".join(c for c in folded if not unicodedata.combining(c))))


@lru_cache(maxsize=8)
def club_labels(season: str) -> dict[str, tuple[str, str]]:
    names: dict[str, set[tuple[str, str]]] = {}
    for league in league_registry.PROFILES:
        for team in league_registry.members(league, season):
            stable = league_registry.ALIASES[league].get(team, team)
            labels = {team, stable, *ALIASES.get(stable, ())}
            short = re.sub(r" (?:FC|AFC)$", "", team)
            if " " in short:
                labels.add(short)
            for label in labels:
                names.setdefault(words(label), set()).add((league, team))
    return {name: next(iter(clubs)) for name, clubs in names.items() if len(clubs) == 1}
