import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket861 import evidence, evidence_sources, football, football_model, league_player_evidence as usage
from polymarket861 import league_player_status as source, league_registry, store


class DomesticPlayerEvidenceTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.kick = store.instant("2026-11-28T19:30:00Z")
        self.cutoff = self.kick - timedelta(hours=1)
        self.clock = self.cutoff - timedelta(days=30)
        for module in (evidence, evidence_sources, store):
            mocked = patch.object(module, "now_utc", side_effect=lambda: store.iso(self.clock))
            mocked.start()
            self.addCleanup(mocked.stop)
        teams = league_registry.members("bun", "2026-27")
        self.home = "SV Werder Bremen"
        self.opponents = [n for n in teams if n != self.home][:5]
        self.starters = ["Anton Keller", "Henrik Sommer", "Bruno Meister", "Carlo Frey",
                         "Dario Welle", "Emil Horvat", "Felix Brandt", "Goran Ilse",
                         "Ivo Tanner", "Karl Ludwig", "Lukas Fritz"]
        self.players = [{"name": name, "position": "GK" if i == 0 else "FW", "minutes": 360, "starts": 4}
                        for i, name in enumerate(self.starters)]
        self.players += [{"name": "Jonas Reuter", "position": "FW", "minutes": 0, "starts": 0},
                         {"name": "Casimir Lund", "position": "GK", "minutes": 0, "starts": 0}]
        self.prior = [self.match(away, i + 1, self.kick - timedelta(days=14 - 3*i), played=True)
                      for i, away in enumerate(self.opponents[:4])]
        self.game = self.match(self.opponents[4], 5, self.kick)
        self.fixture = self.scoped(self.game)
        self.engine = football.FootballEngine([*self.prior, self.game], league="bun", allow_archive_schedule=False)
        matrix, _ = football_model._goals_matrix(1.50, 1.10, 0.01)
        probabilities, totals = football_model.probabilities(matrix)
        self.base = dict(self.fixture, model="B1", rates={"home": 1.50, "away": 1.10, "rho": 0.01},
                         probabilities=probabilities, totals=totals)
        self.roster = {"players": [{"name": p["name"], "source_position": p["position"]} for p in self.players],
                       "ready_at": store.iso(self.cutoff - timedelta(hours=3)),
                       "evidence": [{"source_sha256": "a" * 64}]}
        self.grant = {"source_id": "first-hand", "status": "active", "free": True, "upstream": "own report",
                      "terms_locator": "local:own-observations", "fields": {
                          f: {"uses": ["prediction"], "methods": ["manual"]} for f in
                          ("minutes", "injury", "lineup", "suspension", "availability")}}
        evidence.register_source(self.root, self.grant)
        self.clock = self.cutoff

    def match(self, away, round_no, kickoff, played=False):
        fid = football.fixture_id("2026-27", self.home, away, league="bun")
        results = (football.ResultVersion(kickoff + timedelta(hours=2), (2, 1), "observed", "test"),) if played else ()
        return football.Fixture(fid, "2026-27", round_no, kickoff, self.home, away, result_versions=results)

    @staticmethod
    def scoped(game):
        return {"fixture_id": game.fixture_id, "kickoff": store.iso(game.kickoff), "league": "bun",
                "season": game.season, "home": game.home, "away": game.away}

    def record(self, field, subject, data, *, game=None, revision=1, published=None, source_id="first-hand", **provenance):
        fixture = self.scoped(game or self.game)
        stamp = store.iso(published or self.clock)
        value = {"competition": fixture["league"], "season": fixture["season"], "kickoff": fixture["kickoff"], **data}
        result = evidence.import_evidence(self.root, {
            "fixture_id": fixture["fixture_id"], "team": self.home, "method": "manual", "source_id": source_id,
            "field": field, "subject": subject, "revision": revision, "locator": "local:observed/" + subject,
            "published_at": stamp, "received_at": stamp,
            "value": {} if provenance.get("state") == "withdrawn" else value, **provenance,
        })
        return result["evidence_ids"][0]

    def directory(self, *, revision=1, **change):
        value = {"scope": "domestic_season_to_date", "as_of": store.iso(self.clock - timedelta(minutes=5)),
                 "completed_fixture_ids": [g.fixture_id for g in self.prior], "players": self.players, **change}
        return self.record("minutes", "registered squad", value, revision=revision)

    def absent(self, name, **data):
        return self.record("injury", name, {"availability": "out", "certainty": "reported", **data})

    def run_scenario(self, at=None):
        at = at or self.clock
        status = source.as_of(self.root, self.fixture, at)
        snapshot = dict(self.fixture, teams={"home": {"team": self.home, "roster": self.roster}}, player_status=status)
        return status, source.absence_scenario(self.base, snapshot, cutoff=at, engine=self.engine)

    def test_full_source_to_scenario_distinguishes_regular_fringe_and_goalkeepers(self):
        unchanged = deepcopy(self.base)
        directory = self.directory()
        for name in ("Henrik Sommer", "Jonas Reuter", "Casimir Lund", "Anton Keller"):
            self.absent(name)
        status, result = self.run_scenario()
        self.assertEqual(self.base, unchanged)
        self.assertEqual(status["source"], usage.SOURCE)
        self.assertTrue(result["changed"])
        outputs = {p["name"]: p for p in result["applied"]}
        self.assertEqual(set(outputs), {"Henrik Sommer", "Jonas Reuter", "Anton Keller"})
        self.assertAlmostEqual(outputs["Henrik Sommer"]["importance_multiplier"], 1)
        self.assertAlmostEqual(outputs["Jonas Reuter"]["importance_multiplier"], .25)
        self.assertAlmostEqual(outputs["Anton Keller"]["importance_multiplier"], 1)
        self.assertEqual(result["excluded"][0]["reason"], "no_verified_majority_of_goalkeeper_starts")
        self.assertEqual(outputs["Henrik Sommer"]["player_importance"]["evidence_id"], directory)
        self.assertEqual(outputs["Henrik Sommer"]["player_importance"]["minutes"], 360)
        self.assertEqual(outputs["Henrik Sommer"]["duration_multiplier"], 1)

    def test_usage_denominator_source_scope_clocks_and_withdrawal(self):
        self.absent("Henrik Sommer", injury_started_at=store.iso(self.cutoff - timedelta(days=10)))
        self.directory()
        _, prior = self.run_scenario()
        self.assertLess(prior["applied"][0]["duration_multiplier"], 1)
        self.clock += timedelta(seconds=1)
        self.directory(revision=2, completed_fixture_ids=[g.fixture_id for g in self.prior[:-1]])
        _, result = self.run_scenario()
        player = result["applied"][0]
        self.assertEqual(player["importance_multiplier"], .55)
        self.assertEqual(player["player_importance"]["reason"], "invalid_or_unreconciled_usage_directory")
        self.assertEqual(self.run_scenario(self.cutoff)[1], prior)
        self.clock += timedelta(seconds=1)
        self.directory(revision=3, season="2025-26")
        self.assertEqual(self.run_scenario()[1]["applied"][0]["player_importance"]["basis"], "neutral")
        self.clock += timedelta(seconds=1)
        self.directory(revision=4, as_of=store.iso(self.clock + timedelta(seconds=1)))
        self.assertEqual(self.run_scenario()[1]["applied"][0]["player_importance"]["basis"], "neutral")
        self.clock += timedelta(seconds=1)
        self.directory(revision=5, players=[dict(p, starts=40) for p in self.players])
        self.assertEqual(self.run_scenario()[1]["applied"][0]["importance_multiplier"], .55)
        self.clock += timedelta(seconds=1)
        self.record("injury", "Henrik Sommer", {"availability": "available", "certainty": "confirmed"}, revision=2)
        self.assertFalse(self.run_scenario()[1]["changed"])

    def test_other_competition_unconfirmed_filter_and_confirmed_xi_contradiction(self):
        self.record("suspension", "Henrik Sommer", {"competition": "uefa", "certainty": "confirmed", "availability": "out"})
        self.absent("Jonas Reuter", certainty="rumour")
        self.absent("Bruno Meister")
        self.record("lineup", "matchday", {"confirmed": True, "starters": self.starters})
        status, result = self.run_scenario()
        self.assertEqual(result["excluded"][0]["reason"], "listed_in_published_starting_eleven")
        self.assertFalse(result["changed"])
        self.assertEqual(len(status["injuries"]["teams"]["home"]["players"]), 1)
        moved = dict(self.fixture, kickoff=store.iso(self.kick + timedelta(minutes=30)))
        self.assertIsNone(source.as_of(self.root, moved, self.cutoff)["evidence_sha256"])

    def test_prior_lineup_coverage_named_bench_cutoffs_and_score_withdrawal(self):
        for i, game in enumerate(self.prior):
            self.clock = game.kickoff - timedelta(hours=1)
            # For the last game only a post-match insert is available.
            if i == 3:
                self.clock = game.kickoff + timedelta(hours=3)
            self.record("lineup", "registered xi", {"confirmed": True, "starters": self.starters,
                                                   "bench": ["Jonas Reuter", "Casimir Lund"]}, game=game)
        self.clock = self.cutoff
        for name in ("Henrik Sommer", "Jonas Reuter", "Casimir Lund"):
            self.absent(name)
        status, result = self.run_scenario()
        self.assertEqual(len(status["prior_lineups"]["home"]), 3)
        regular, fringe = sorted(result["applied"], key=lambda it: it["name"])
        self.assertEqual((regular["importance_multiplier"], fringe["importance_multiplier"]), (1, .25))
        self.assertEqual(fringe["player_importance"]["basis"], "confirmed_domestic_lineup_starts")
        self.assertEqual(fringe["player_importance"]["scheduled_matches"], 4)
        self.assertEqual(len(result["excluded"]), 1)
        self.clock += timedelta(seconds=1)
        self.record("lineup", "registered xi", {"confirmed": False, "starters": []}, game=self.prior[0], revision=2)
        _, incomplete = self.run_scenario()
        self.assertTrue(all(p["importance_multiplier"] == .55 for p in incomplete["applied"]))
        self.assertEqual(self.run_scenario(self.cutoff)[1], result)

    def test_restricted_provider_transcripts_do_not_enter_generic_path(self):
        self.absent("Jonas Reuter")
        injury = {"availability": "out", "certainty": "confirmed"}
        for i, (sid, manifest, origin, meta) in enumerate((
            (source.SOURCE, {}, {}, {}),
            ("name", {"name": "API-Football stats"}, {}, {}),
            ("wrapper", {"upstream": "API-Sports"}, {}, {}),
            ("registry-url", {"terms_locator": "https://www.api-football.com/terms"}, {}, {}),
            ("wire", {}, {"origin_id": "API-FOOTBALL v3"}, {}),
            ("mirror", {}, {"locator": "https://v3.football.api-sports.io/injuries?fixture=91"}, {}),
            ("rapid", {}, {}, {"source_url": "https://api-football-v1.p.rapidapi.com/v3/injuries"}),
        )):
            with self.subTest(source=sid):
                evidence.register_source(self.root, {**self.grant, "source_id": sid, "upstream": sid, **manifest,
                                                    "fields": {"injury": {"methods": ["manual"], "uses": ["prediction"]}}})
                self.record("injury", self.starters[i], {**injury, **meta}, source_id=sid, **origin)
        with patch.dict("os.environ", {"PM861_API_FOOTBALL_KEY": "test-marker"}):
            status, result = self.run_scenario()
        self.assertEqual(status["api_football_state"], "free_season_unavailable")
        self.assertEqual([p["name"] for p in result["applied"]], ["Jonas Reuter"])

    def test_restored_source_permission_excludes_records_received_while_disabled(self):
        self.clock = self.cutoff - timedelta(hours=2)
        evidence.register_source(self.root, dict(self.grant, status="disabled", free=False))
        self.clock = self.cutoff - timedelta(minutes=10)
        evidence.register_source(self.root, self.grant)
        self.clock = self.cutoff
        self.record("injury", "Henrik Sommer", {"availability": "out", "certainty": "confirmed"},
                    published=self.cutoff - timedelta(hours=1))
        self.assertFalse(self.run_scenario()[1]["changed"])

    def test_confirmed_named_bench_and_ambiguous_sheet_override_absence(self):
        self.absent("Jonas Reuter")
        self.record("lineup", "matchday", {"confirmed": True, "starters": self.starters, "bench": ["Jonas Reuter"]})
        status, result = self.run_scenario()
        self.assertEqual(status["lineup"]["teams"]["home"]["bench"][0]["name"], "Jonas Reuter")
        self.assertEqual(result["excluded"][0]["reason"], "listed_on_published_bench")
        self.assertFalse(result["changed"])
        for revision, bench in enumerate((["J. Reuter"], ["Jonas Reuter", "Jonas Reuter"], ["Henrik Sommer"]), 2):
            with self.subTest(bench=bench):
                self.clock += timedelta(seconds=1)
                self.record("lineup", "matchday", {"confirmed": True, "starters": self.starters, "bench": bench},
                            revision=revision)
                observed, decision = self.run_scenario()
                self.assertEqual(observed["lineup_ambiguity"], ["home"])
                self.assertFalse(decision["changed"])
                self.assertEqual(decision["excluded"][0]["reason"], "published_matchday_squad_identity_unresolved")
        self.assertEqual(self.run_scenario(self.cutoff)[1], result)

    def test_regular_keeper_majority_uses_whole_completed_window(self):
        for n, game in enumerate(self.prior[:3]):
            self.clock = game.kickoff - timedelta(minutes=60)
            starters = list(self.starters)
            if n == 2:
                starters[0] = "Casimir Lund"
            bench = ["Anton Keller" if n == 2 else "Casimir Lund"]
            self.record("lineup", "registered xi", {"confirmed": True, "starters": starters, "bench": bench}, game=game)
        self.clock = self.cutoff
        self.absent("Anton Keller")
        _, result = self.run_scenario()
        excluded = result["excluded"][0]
        self.assertFalse(result["changed"])
        self.assertEqual((excluded["player_importance"]["starts"], excluded["player_importance"]["matches"],
                          excluded["player_importance"]["scheduled_matches"]), (2, 3, 4))
        self.assertEqual(excluded["reason"], "no_verified_majority_of_goalkeeper_starts")

    def test_continuous_absence_preserves_first_local_receipt_until_clear_or_withdrawal(self):
        start = self.cutoff - timedelta(days=7)
        self.clock = start
        for name in ("Jonas Reuter", "Henrik Sommer"):
            self.absent(name)
        self.clock = self.cutoff
        for name in ("Jonas Reuter", "Henrik Sommer"):
            self.record("injury", name, {"availability": "out", "certainty": "reported"}, revision=2)
        _, ongoing = self.run_scenario()
        self.assertEqual(len(ongoing["applied"]), 2)
        for p in ongoing["applied"]:
            self.assertEqual(store.instant(p["onset_at"]), start)
            self.assertLess(p["duration_multiplier"], 1)
        self.clock += timedelta(seconds=1)
        self.record("injury", "Jonas Reuter", {"availability": "available", "certainty": "confirmed"}, revision=3)
        self.record("injury", "Henrik Sommer", {}, revision=3, state="withdrawn")
        self.assertFalse(self.run_scenario()[1]["changed"])
        self.clock += timedelta(seconds=1)
        for name in ("Jonas Reuter", "Henrik Sommer"):
            self.record("injury", name, {"availability": "out", "certainty": "confirmed"}, revision=4)
        _, restarted = self.run_scenario()
        for p in restarted["applied"]:
            self.assertEqual(store.instant(p["onset_at"]), self.clock)
            self.assertEqual(p["duration_multiplier"], 1)
        self.assertEqual(self.run_scenario(self.cutoff)[1], ongoing)

    def test_restatement_preserves_previously_documented_native_onset(self):
        first = self.cutoff - timedelta(days=7)
        onset = first - timedelta(days=4)
        self.clock = first
        self.absent("Henrik Sommer", injury_started_at=store.iso(onset))
        self.clock = self.cutoff
        self.record("injury", "Henrik Sommer", {"availability": "out", "certainty": "reported"}, revision=2)
        _, result = self.run_scenario()
        applied = result["applied"][0]
        self.assertEqual(store.instant(applied["onset_at"]), onset)
        self.assertEqual(applied["onset_basis"], "documented_injury_onset")

    def test_last_same_clock_result_controls_minutes_and_lineup_denominator(self):
        self.directory()
        self.absent("Henrik Sommer")
        prior = self.prior[0]
        final = prior.result_versions[0]
        retracted = replace(final, score=None)
        for sequence, multiplier in (((final, retracted), .55), ((retracted, final), 1.0)):
            with self.subTest(sequence=[v.score for v in sequence]):
                games = [replace(prior, result_versions=sequence), *self.prior[1:], self.game]
                self.engine = football.FootballEngine(games, league="bun", allow_archive_schedule=False)
                _, scenario = self.run_scenario()
                self.assertEqual(scenario["applied"][0]["importance_multiplier"], multiplier)

    def test_native_roster_position_and_same_named_player_on_other_club(self):
        for league, defender, keeper in (("bun", "abwehr", "torwart"), ("lal", "DEF", "POR"),
                                        ("sea", "D", "P"), ("fl1", "D", "G")):
            with self.subTest(league=league):
                home, away = list(league_registry.members(league, "2026-27"))[:2]
                fid = football.fixture_id("2026-27", home, away, league=league)
                base = dict(self.base, league=league, home=home, away=away, fixture_id=fid)
                injury = {"state": "observed", "received_at": store.iso(self.cutoff), "teams": {"home": {
                    "players": [{"player_id": 1, "name": "Henrik Sommer", "provider_type": "Missing Fixture"},
                                {"player_id": 2, "name": "Jonas Reuter", "provider_type": "Missing Fixture"},
                                {"player_id": 3, "name": "Other Person", "provider_type": "Missing Fixture"}]}}}
                roster = dict(self.roster, players=[{"name": "Henrik Sommer", "source_position": defender},
                                                    {"name": "Jonas Reuter", "source_position": keeper}])
                snapshot = {"league": league, "season": "2026-27", "fixture_id": fid,
                            "player_status": {"source": source.SOURCE, "injuries": injury},
                            "teams": {"home": {"team": home, "roster": roster},
                                      "away": {"team": away, "roster": dict(roster, players=[{"name": "Other Person"}])}}}
                result = source.absence_scenario(base, snapshot, cutoff=self.cutoff)
                self.assertEqual([(p["role"], p["importance_multiplier"]) for p in result["applied"]], [("defence", .55)])
                self.assertEqual([p["reason"] for p in result["excluded"]],
                                 ["no_verified_majority_of_goalkeeper_starts", "independent_full_name_identity_missing"])
                moved = dict(snapshot, fixture_id=self.game.fixture_id + "/wrong")
                self.assertEqual(source.absence_scenario(base, moved, cutoff=self.cutoff)["status"], "fixture_scope_mismatch")

    def test_only_locally_admitted_free_field_evidence_at_cutoff_counts(self):
        self.clock += timedelta(seconds=1)
        evidence.register_source(self.root, dict(self.grant, status="candidate", fields={
            "injury": {"methods": ["manual"], "uses": ["trial", "prediction"]}}))
        self.absent("Henrik Sommer")
        self.assertFalse(self.run_scenario()[1]["changed"])
        self.clock += timedelta(seconds=1)
        evidence.register_source(self.root, self.grant)
        self.absent("Jonas Reuter")
        self.assertFalse(self.run_scenario(self.cutoff)[1]["changed"])
        _, result = self.run_scenario()
        self.assertEqual([p["name"] for p in result["applied"]], ["Jonas Reuter"])
        self.assertEqual(result["applied"][0]["importance_multiplier"], .55)
        self.assertFalse(self.run_scenario(self.clock + timedelta(hours=7))[1]["changed"])
