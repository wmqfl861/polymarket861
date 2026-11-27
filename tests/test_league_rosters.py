import json
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from polymarket861 import football, league_registry, league_rosters as rosters, store
from polymarket861.blob_storage import blob_path
from polymarket861.market_provider import Reply


SEASON = "2026-27"
CLOCK = "2026-11-27T12:00:00.000000Z"


class LeagueRosters(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.root = self.state / "five-league-rosters"
        self.clock = store.instant(CLOCK)
        clock = patch.object(store, "now_utc", side_effect=lambda: store.iso(self.clock))
        clock.start()
        self.addCleanup(clock.stop)
        self.responses = {}
        self.requests = []
        self.fixture = self.match("lal", "FC Barcelona", "Athletic Club")

    def match(self, league, home, away):
        return {
            "league": league, "season": SEASON, "home": home, "away": away,
            "fixture_id": football.fixture_id(SEASON, home, away, league=league),
            "kickoff": store.iso(self.clock + timedelta(days=20)),
        }

    def page(self, team, text, revision=1, *, league="lal", dependency=False, **override):
        scope = rosters.roster_coverage(team, SEASON, league)
        title, page_id = scope["dependency"] if dependency else (scope["title"], scope["page_id"])
        url = rosters._ROSTER_API.format(language=scope["language"], page=quote(title.replace(" ", "_"), safe=""))
        body = json.dumps({
            "id": page_id, "title": title, "content_model": "wikitext", "source": text,
            "latest": {"id": revision, "timestamp": store.iso(self.clock - timedelta(days=1))},
            "license": {"url": f"https://creativecommons.org/licenses/by-sa/4.0/deed.{scope['language']}"},
            **override,
        }).encode()
        self.responses[url] = body
        return body

    def get(self, url):
        self.requests.append(url)
        return Reply(self.responses[url], store.iso(self.clock), store.iso(self.clock), {}, url)

    def collect(self, fixture=None):
        return rosters.collect_fixture_rosters(self.state, fixture or self.fixture, getter=self.get)

    def spanish(self, first="Álex Ejemplo", *, injury=True):
        base = [
            f"{{{{Jugador de fútbol con esquema|num={index}|nombre=[[Futbolista {index}|{name}]]"
            "|pos=MED" + ("|nota={{Lesionado|10px}}" if injury and index == 1 else "")
            + "|procedencia=[[Otro club]]}}"
            for index, name in enumerate([first, *(f"Nombre {i}" for i in range(2, 12))], 1)
        ]
        return ("== Equipo femenino ==\n=== Plantilla 2026-27 ===\n{{Otro equipo}}\n"
                "== Jugadores ==\n=== Plantilla 2026-27 ===\n" + "\n".join(base)
                + "\n<!-- {{Jugador de fútbol con esquema|nombre=[[Comentado]]}} -->"
                  "\n{{Cesiones equipo de fútbol|numcolumnas=11}}"
                  "\n{{Jugador de fútbol con esquema|nombre=[[Cedido]]}}\n")

    def test_reviewed_mappings_match_the_season_membership(self):
        for league, count in (("lal", 20), ("bun", 18), ("sea", 20), ("fl1", 18)):
            rows = [rosters.roster_coverage(team, SEASON, league) for team in league_registry.members(league, SEASON)]
            self.assertTrue(all(rows))
            self.assertEqual(len({row["page_id"] for row in rows}), count)
        self.assertIsNone(rosters.roster_coverage("FC Barcelona", SEASON, "bun"))
        self.assertIsNone(rosters.roster_coverage("Real Madrid CF B", SEASON, "lal"))
        self.assertIsNone(rosters.roster_coverage("FC Barcelona", "2025-26", "lal"))

    def test_receipts_exclude_late_or_damaged_sources_and_qualify_roster_markers(self):
        before = store.iso(self.clock - timedelta(microseconds=1))
        self.assertEqual(rosters.fixture_rosters_as_of(self.state, self.fixture, before)["state"], "unknown")
        self.assertFalse(self.root.exists())
        self.page("FC Barcelona", self.spanish())
        self.page("Athletic Club", self.spanish("Otro Ejemplo", injury=False),
                  license={"url": "https://example.org/unverified-license"})
        first = self.collect()
        self.assertEqual(first["facts"], [])
        self.assertEqual(first["teams"]["away"]["roster"]["status"], "unknown")
        home = first["teams"]["home"]
        self.assertEqual(len(home["roster"]["players"]), 11)
        self.assertEqual(home["availability"]["status"], "roster_marker_only")
        self.assertEqual(home["availability"]["items"], [
            {"player": "Álex Ejemplo", "kind": "injury", "source_label": "Lesionado", "match_scope": "unknown"}
        ])
        parent, = home["roster"]["evidence"]
        self.assertEqual(parent["received_at"], CLOCK)
        self.assertLess(parent["published_at"], parent["received_at"])
        self.assertEqual(rosters.fixture_rosters_as_of(self.state, self.fixture, before)["state"], "unknown")
        self.collect()
        self.assertEqual(len(self.requests), 2)
        blob_path(self.root, parent["source_sha256"]).write_bytes(b"damaged")
        damaged = rosters.fixture_rosters_as_of(self.state, self.fixture, CLOCK)
        self.assertEqual(damaged["teams"]["home"]["roster"]["reason"], "roster_original_unavailable")

    def test_new_parser_and_real_revision_changes_preserve_old_cutoff(self):
        self.page("FC Barcelona", self.spanish())
        self.page("Athletic Club", self.spanish("Otro Ejemplo"))
        first = self.collect()
        self.clock += timedelta(minutes=1)
        next_at = store.iso(self.clock)
        with patch.object(rosters, "_ROSTER_PARSER_VERSION", "upgraded-test"):
            upgraded = self.collect()
            self.assertEqual(len(self.requests), 4)
            self.assertEqual(first["evidence_sha256"], upgraded["evidence_sha256"])
            self.assertEqual(upgraded["teams"]["home"]["roster"]["parser_version"], "upgraded-test")
            self.assertEqual(rosters.fixture_rosters_as_of(self.state, self.fixture, CLOCK), first)
            self.clock += timedelta(hours=6)
            self.page("FC Barcelona", self.spanish("Nuevo Ejemplo"), 2)
            changed = self.collect()
        change = changed["teams"]["home"]["roster"]["changes"]
        self.assertEqual((change["added"], change["removed"]), (["Nuevo Ejemplo"], ["Álex Ejemplo"]))
        self.assertEqual(change["compared_with"], next_at)
        self.assertEqual(change["prior_evidence"][0]["revision_id"], 1)
        self.assertEqual(change["evidence"][0]["revision_id"], 2)
        self.assertNotEqual(first["evidence_sha256"], changed["evidence_sha256"])

    def test_french_template_revision_is_an_independent_receipt(self):
        fixture = self.match("fl1", "AJ Auxerre", "Le Havre AC")
        self.page("AJ Auxerre", "== Effectif professionnel actuel ==\n{{Effectif de l'AJ Auxerre 2026-2027}}",
                  league="fl1")
        self.page("Le Havre AC", "== Effectif professionnel actuel ==\n", league="fl1")
        self.page("AJ Auxerre", "Effectif professionnel pour la saison 2026-2027\n"
                  + "\n".join(f"{{{{Feff joueur|num={i}|prénom=Alain|nom=Exemple {i}|pos=G}}}}" for i in range(1, 12)),
                  revision=2, league="fl1", dependency=True)
        home = self.collect(fixture)["teams"]["home"]["roster"]
        self.assertEqual(len(home["players"]), 11)
        self.assertEqual([parent["revision_id"] for parent in home["evidence"]], [1, 2])
        with closing(rosters._roster_db(self.root)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM observations").fetchone()[0], 3)

    def test_german_club_totals_require_the_source_footnote(self):
        section = (
            "==== Aktueller Kader ====\nStand Spiele und Tore: 21. November 2026<small>\n"
            "{| class=\"wikitable\"\n"
            "! Pos.\n! Nr.\n! Nat.\n! Name\n! Geburtstag\n"
            "! BL-Spiele{{FN|(1)}}\n! BL-Tore{{FN|(1)}}\n! im Verein seit\n! Vertrag bis\n! vorheriger Verein\n"
            + "\n".join(f"| {i} || {{{{DEU|#}}}} || {{{{PersonZelle|Nico|Beispiel {i}}}}} "
                        + "|| 4. Mai 2000 || 13 || {{0}}0 || 2025 || 2027 || [[Alter Verein]]"
                        for i in range(1, 12))
            + "\n|}\n"
        )
        note = "{{FNZ|(1)|angegeben sind die Bundesligaspiele und -tore für den BVB.}}"
        players, _ = rosters._parse_local_roster(section + note, "de", SEASON)
        self.assertEqual(len(players), 11)
        self.assertEqual(players[0]["league_totals"], {
            "competition": "bun", "scope": "club_career", "appearances": 13, "goals": 0,
            "as_of_source_text": "21. November 2026",
        })
        without_note, _ = rosters._parse_local_roster(section, "de", SEASON)
        self.assertTrue(all("league_totals" not in player for player in without_note))
