"""Regressions for the 0.5 football-news competition and source evidence audit."""

import json
import unittest
from datetime import timedelta

from polymarket861 import news, news_importance, news_minutes, news_model, news_scope, news_sources, store

import test_news_importance as usage
import test_news_model as modelling
import test_news_sources as feeds
import test_news_wire_states as standing


class CompetitionScopeTests(unittest.TestCase):
    def fixture(self, cls):
        case = cls()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_empty_foreign_reply_and_future_season_cannot_enter_any_subscription(self):
        for club in ("FC Bayern München", "Paris Saint-Germain FC", "Venezia FC", "Levante UD"):
            with self.subTest(club=club):
                self.assertNotIn(news_sources.WIRE, [s["id"] for s in news_sources.active_sources(club, feeds.SEASON)])
                old = news_sources.default_sources(club, feeds.SEASON)[0]
                with self.assertRaisesRegex(ValueError, "source_not_covered"):
                    news_sources.poll(old, club, feeds.SEASON, getter=lambda _: self.fail("unsupported request"))
        src = news_sources.default_sources("Arsenal", feeds.SEASON)[0]
        checked = feeds.run(src, feeds.availability([]))
        self.assertEqual(checked["coverage_evidence"], news_scope.wire_coverage("Arsenal", feeds.SEASON, "epl", feeds.CLOCK))
        with self.assertRaisesRegex(ValueError, "source_not_covered"):
            news_sources.poll(src, "Arsenal", feeds.SEASON, league="ucl", getter=lambda _: self.fail("foreign competition"))
        self.assertIsNone(news_scope.wire_coverage("Arsenal", feeds.SEASON, "epl", "2027-07-01T00:00Z"))
        self.assertIsNone(news_scope.wire_coverage("Arsenal", "2027-28"))
        self.assertEqual(len(news_scope.WIRE_MEMBERS), 20)

    def test_legacy_empty_requires_actual_contemporaneous_named_directory_and_request(self):
        case = self.fixture(standing.WireStateTests)
        first = case.now()
        case.run_wire([])
        def coverage(at):
            return news.snapshot(case.root, "first", at)["coverage"]["home"]["sources"][0]
        self.assertTrue(coverage(first)["availability_observed"])
        with news._db(case.root, write=True) as db:
            row = db.execute("SELECT id,payload FROM news_poll_proofs").fetchone()
            doc = json.loads(row["payload"])
            del doc["coverage_evidence"]
            legacy = store.digest(doc)
            db.execute("INSERT INTO news_poll_proofs VALUES (?,?)", (legacy, store.canonical(doc)))
            db.execute("UPDATE news_wire_polls SET receipt=?", (legacy,))
            db.execute("UPDATE news_checks SET detail=json_remove(detail,'$.coverage_evidence')")
        self.assertFalse(coverage(first)["availability_observed"])
        case.advance()
        # A directory first received AFTER the earlier empty cannot backfill it,
        # even when both are visible to a later forecast.
        raw = usage.envelope(usage.usage_rows(), case.now())
        self.assertEqual(news_minutes.refresh(case.root, case.team, case.season, getter=usage.reply(raw))["state"], "captured")
        self.assertFalse(coverage(case.now())["availability_observed"])
        case.advance()
        case.run_wire([])
        with news._db(case.root, write=True) as db:
            row = db.execute("SELECT id,payload FROM news_poll_proofs ORDER BY rowid DESC LIMIT 1").fetchone()
            doc = json.loads(row["payload"])
            del doc["coverage_evidence"]
            new_id = store.digest(doc)
            db.execute("INSERT INTO news_poll_proofs VALUES (?,?)", (new_id, store.canonical(doc)))
            db.execute("UPDATE news_wire_polls SET receipt=? WHERE receipt=?", (new_id, row["id"]))
            db.execute("UPDATE news_checks SET detail=json_remove(detail,'$.coverage_evidence')")
        self.assertTrue(coverage(case.now())["availability_observed"])
        self.assertFalse(coverage(first)["availability_observed"])
        with news._db(case.root, write=True) as db:
            db.execute("UPDATE news_checks SET detail=json_set(detail,'$.response_sha256',?)", ("0"*64,))
        self.assertFalse(coverage(case.now())["availability_observed"])

    def test_old_foreign_db_stream_and_unscoped_empty_proof_cannot_mark_a_side_observed(self):
        case = self.fixture(standing.WireStateTests)
        case.run_wire([])
        for club, league in (("Paris Saint-Germain FC", "fl1"), ("Chelsea FC", "ucl")):
            with self.subTest(club=club), news._db(case.root, write=True) as db:
                scope = [dict(fixture_id=league + "/foreign", season=case.season, home=club,
                              away="Other", kickoff="2026-11-27T16:00:00.000000Z")]
                source = news_sources.default_sources(club, case.season)[0]
                key = news_sources.football.team_key(club)
                db.execute("""INSERT OR IGNORE INTO news_streams(season,team_key,source,team,config,registered_at)
                             VALUES(?,?,?,?,?,?)""", (case.season,key,news_sources.WIRE,club,store.canonical(source),case.now()))
                db.execute("""INSERT INTO news_checks(season,team_key,source,fixture_id,kickoff,checked_at,state,detail)
                    SELECT season,?,source,?,kickoff,checked_at,'ok',json_remove(detail,'$.coverage_evidence')
                    FROM news_checks WHERE fixture_id='first' LIMIT 1""", (key,scope[0]["fixture_id"]))
                result = news._side_coverage(db,case.root,scope[0],"home",case.now(),scope)
                self.assertFalse(result["observed"])
                self.assertEqual(result["sources"][0]["status"], "source_not_covered")

    def test_suspension_target_remains_in_fact_history_but_only_domestic_changes_forecast(self):
        case = self.fixture(standing.WireStateTests)
        source = case.player | {"status": "suspended", "category": "suspension", "chance_of_playing": 0}
        before = None
        for scope, admissible in ((None, False), ("UEFA Champions League", False), ("FA Cup", False),
                                  ("Premier League", True)):
            case.advance()
            row = source if scope is None else source | {"suspension_competition": scope}
            case.run_wire([row])
            stamp = case.now()
            facts = case.facts()
            self.assertEqual(len(facts), 1)
            fact = facts[0]
            self.assertEqual(fact.get("suspension_competition"), scope)
            base = modelling.model() | {"fixture_id": "first", "kickoff": fact["kickoff"]}
            report = news_model.make_pair(base, facts, home=case.team, away="Forest FC", cutoff=stamp)
            self.assertEqual(bool(report["applied"]), admissible)
            self.assertEqual(report["with_news"] == report["no_news"], not admissible)
            if not admissible:
                self.assertIn("suspension_competition_unverified", report["excluded_reasons"])
            if scope == "UEFA Champions League":
                before = stamp
        self.assertEqual(case.facts(at=before)[0]["suspension_competition"], "UEFA Champions League")
        for quote, expected in (("Joe is suspended for the Premier League match.", True),
                                ("Joe is suspended after a Premier League match.", False),
                                ("Joe is suspended for the FA Cup.", False)):
            self.assertEqual(news_scope.suspension_applies({"event":"suspension","quote":quote}, "epl"), expected)

    def test_native_onset_disclosed_at_receipt_and_date_precision_never_implies_an_early_hour(self):
        case = self.fixture(standing.WireStateTests)
        target = "2026-10-01T12:00:00Z"
        prior = case.now()
        case.advance()
        case.run_wire([case.player | {"injury_started_at": target}])
        first = case.now()
        fact, = case.facts()
        self.assertFalse(case.facts(at=prior))
        self.assertEqual(fact["first_seen_at"], first)
        base = modelling.model() | {"fixture_id": "first", "kickoff": fact["kickoff"]}
        def applied():
            return news_model.make_pair(base, case.facts(), home=case.team, away="Forest FC", cutoff=case.now(),
                    training_kickoffs={"chelsea": [store.instant(d).timestamp() for d in ("2026-09-20T15:00Z", "2026-10-04T15:00Z")]})["applied"][0]
        item = applied()
        self.assertEqual(item["duration_from"], store.iso(target))
        self.assertEqual(item["injury_onset_basis"], "provider_injury_started_at")
        self.assertLess(item["duration_multiplier"], 1)
        case.advance()
        row = case.player | {"injury_date": "2026-10-01"}
        case.run_wire([row])
        day = applied()
        self.assertEqual(day["duration_from"], "2026-10-02T11:59:59.999999Z")
        self.assertEqual(day["injury_onset_basis"], "provider_injury_date_upper_bound")
        case.advance()
        self.assertEqual(case.run_wire([row])["articles_added"], 0)
        self.assertEqual(case.facts(at=first)[0]["injury_started_at"], store.iso(target))
        case.advance()
        self.assertEqual(case.run_wire([case.player | {"injury_date": "2027-01-01"}])["state"], "partial")


class DirectoryEvidenceTests(unittest.TestCase):
    def test_small_start_surplus_and_limits_do_not_permit_a_foreign_match(self):
        players = usage.usage_rows()
        players.append({"id":99, "position":"MID", "starts":1, "minutes":0})
        self.assertEqual(news_importance.appearance_totals(players, 3)["extra_starts"], 1)
        players[-1]["starts"] = 2
        diagnostics = {}
        self.assertIsNone(news_importance.appearance_totals(players, 3, diagnostics))
        self.assertEqual((diagnostics["team_starts"], diagnostics["expected_team_starts"]), (35,33))
        players[-1].update(starts=1, position="GK")
        self.assertIsNone(news_importance.appearance_totals(players, 3))
        players[-1].update(position="MID", minutes=1)
        self.assertIsNone(news_importance.appearance_totals(players, 3))

    def test_incomplete_newer_directory_falls_back_to_preceding_valid_receipt(self):
        case = usage.UsageHistoryTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        first = case.capture(usage.usage_rows())["original_sha256"]
        stamp = store.iso(case.time)
        kickoff = case.time + timedelta(hours=1)
        case.schedule("fourth", store.iso(kickoff), "Another FC")
        case.time += timedelta(hours=13)
        corrected = usage.usage_rows()
        corrected[4]["minutes"] -= 1
        newer = case.capture(corrected)["original_sha256"]
        weight = case.history()["enwiki:100"]
        self.assertEqual(weight["original_sha256"], first)
        self.assertEqual(weight["rejected_directory"]["original_sha256"], newer)
        self.assertEqual(weight["rejected_directory"]["reason"], "prior_results_incomplete_at_directory_snapshot")
        self.assertEqual(weight["rejected_directory"]["completed_at_provider_time"], 3)
        self.assertNotIn("rejected_directory", case.history(stamp)["enwiki:100"])
