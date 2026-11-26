import json
import math
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket861 import evidence, evidence_sources, news, news_importance, news_minutes, news_model, news_roster, news_sources, store
from polymarket861.market_provider import Reply
from test_news_model import claim, model
from test_news_roster import Wiki


TEAM = "Chelsea FC"
AWAY = "Everton FC"
SEASON = "2026-27"
CUT = "2026-11-25T12:00:00Z"
KICK = "2026-11-27T20:00:00Z"
NAMES = ["Morgan Smith", "River Stone", "Taylor Lake", *[f"Squad Member {i}" for i in range(10)]]


def reply(raw):
    def get(url):
        stamp = store.now_utc()
        return Reply(json.dumps(raw).encode(), stamp, stamp, {}, url)
    return get


def envelope(rows, stamp):
    return {"meta": {
        "source": "Touchline Wire Tactical API", "attribution_url": "https://touchlinewire.com",
        "license": "Free for personal, academic, and open-source use with attribution",
        "timestamp_iso": stamp, "count": len(rows),
    }, "data": rows}


def usage_rows():
    return [{"id": i + 1, "league": "epl", "club": "Chelsea", "position": "GK" if i < 3 else "MID",
             "name": name, "display_name": name, "starts": 0 if i in (1, 2) else 3,
             "minutes": (260, 10, 0)[i] if i < 3 else 270} for i, name in enumerate(NAMES)]


def wire(chance, at=CUT, *, onset=None, **updates):
    availability = "available" if chance == 100 else "out" if chance == 0 else "doubtful"
    return claim(
        team=TEAM, position="goalkeeper", source=news_sources.WIRE, time_basis="provider_snapshot",
        status="reported", authority="secondary", chance_of_playing=chance, availability=availability,
        first_seen_at=at, absence_first_seen_at=onset or at, last_seen_at=at, last_seen_state_as_of=at,
        published_at=at, received_at=at, known_at=at,
    ) | updates


def pair(facts, at=CUT, training_kickoffs=None):
    base = model() | {"cutoff": at, "fixture_id": "next", "kickoff": KICK}
    return news_model.make_pair(base, facts, home=TEAM, away=AWAY, cutoff=at, training_kickoffs=training_kickoffs)


class UsageModelTests(unittest.TestCase):
    def native_result_sources(self):
        import test_independent_results
        import test_result_admission
        from polymarket861 import independent_results

        case = test_result_admission.VerifiedResults()
        case.setUp()
        self.addCleanup(case.doCleanups)
        wiki = test_independent_results.WikiEvidence()
        wiki.root = case.root
        wiki.teams = ["Arsenal FC", "Brentford FC", *[f"Club {n} FC" for n in range(2, 20)]]
        wiki.titles = tuple(sorted(independent_results._title(2026, team) for team in wiki.teams))
        page = independent_results._title(2026, "Arsenal FC")
        box = wiki.box(round=1, home="Arsenal", away="Brentford", time="20:00 [[British Summer Time|BST]]")
        return case, wiki, page, box

    def test_two_archived_finals_establish_play_without_a_score_even_for_an_advanced_kickoff(self):
        from polymarket861 import result_admission

        case, wiki, page, box = self.native_result_sources()
        first = store.iso("2026-08-20T23:00Z")
        actual = store.iso("2026-08-20T19:00Z")  # Registered fixture is one day later.
        case.time = wiki.start = wiki.seen = first
        row = case.rows[0] | {"matchDateTimeUTC": actual}
        content = box.replace("21 November 2026", "20 August 2026")
        case.collect([row])  # OpenLigaDB says 2–1, the independent archived article says 2–0.
        wiki.receive({page: content})
        self.assertEqual(case.reconcile()["quarantined"][0]["reason"], "cross_source_score_conflict")

        def club_games(at):
            with closing(store.connect(case.root)) as db:
                return news_minutes.club_games(db, case.root, SEASON, "Arsenal FC", at)

        self.assertEqual(club_games(store.iso("2026-08-20T22:59Z")), [])
        game, = club_games(first)
        self.assertEqual((game["completed"], game["kickoff"], game["received"]), (True, actual, first))
        self.assertIsNone(result_admission.verified_regulation(case.root, case.fids[0]))
        case.time = wiki.seen = store.iso("2026-08-21T00:00Z")
        case.collect([row])
        wiki.receive({page: content})
        self.assertEqual(club_games(case.time)[0]["received"], first)
        # A disagreement over kickoff cannot authenticate a pre-schedule appearance.
        wiki.seen = case.time = store.iso("2026-08-21T01:00Z")
        wiki.receive({page: content.replace("20:00", "19:00")})
        self.assertEqual(club_games(case.time), [])
        self.assertEqual(club_games(first)[0]["received"], first)

    def test_later_kickoff_disagreement_overrules_a_previously_admitted_native_final(self):
        from polymarket861 import news_events, result_admission

        case, wiki, page, box = self.native_result_sources()
        first = store.iso("2026-08-21T22:00Z")
        case.time = wiki.start = wiki.seen = first
        score_only = box.replace("21 November 2026", "21 August 2026")
        case.collect([case.rows[0]])
        wiki.receive({page: score_only.replace("2–0", "2–1")})
        self.assertEqual(case.reconcile()["available_results"], 1)
        self.assertIsNotNone(result_admission.verified_regulation(case.root, case.fids[0]))

        def match(at):
            with closing(store.connect(case.root)) as db:
                game, = news_minutes.club_games(db, case.root, SEASON, "Arsenal FC", at)
                return game

        previous = match(first)
        self.assertTrue(previous["completed"])
        agreed = store.iso("2026-08-21T23:00Z")
        wiki.seen = case.time = agreed
        wiki.receive({page: score_only})
        self.assertEqual(case.reconcile()["quarantined"][0]["reason"], "cross_source_score_conflict")
        self.assertEqual((match(agreed)["completed"], match(agreed)["received"]), (True, previous["received"]))
        split = store.iso("2026-08-21T23:50Z")
        wiki.seen = case.time = split
        wiki.receive({page: score_only.replace("20:00", "21:00")})  # 20:00 UTC versus 19:00 UTC.
        self.assertEqual(case.reconcile()["quarantined"][0]["reason"], "cross_source_score_conflict")
        self.assertFalse(match(split)["completed"])
        self.assertEqual(match(first), previous)
        self.assertTrue(match(agreed)["completed"])
        self.assertIsNone(result_admission.verified_regulation(case.root, case.fids[0], at=split))
        with closing(store.connect(case.root)) as db:
            next_game = {"season": SEASON, "home": "Arsenal FC", "away": "Chelsea FC"}
            earlier = news_events._weight_inputs(db, next_game, agreed)
            later = news_events._weight_inputs(db, next_game, split)
            self.assertEqual(earlier["matches"], later["matches"])
            self.assertNotEqual(earlier["disputed_independent"], later["disputed_independent"])

    def test_probability_is_monotone_across_statuses_and_rumours_have_no_effect(self):
        expected = [0.45, 0.3375, 0.225, 0.1125, 0]
        for percent, weight in zip((0, 25, 50, 75, 100), expected, strict=True):
            doc = pair([wire(percent)])
            self.assertAlmostEqual(doc["log_goal_shifts"]["away"], 0.045 * weight * 0.55)
            self.assertEqual(doc["no_news"]["probabilities"], model()["probabilities"])
        for percent in (False, -1, 110, "25"):
            self.assertEqual(pair([wire(percent)])["excluded_reasons"], {"invalid_playing_probability": 1})
        self.assertFalse(pair([wire(0, status="rumour")])["changed"])
        self.assertAlmostEqual(pair([wire(None)])["applied"][0]["absence_weight"], 0.2)
        self.assertEqual(pair([wire(25, availability="unknown")])["applied"][0]["availability"], "doubtful")

    def test_duration_reaffirmation_boundaries_stale_feed_and_separate_official_support(self):
        now = store.instant(CUT)
        onset = store.iso(now - timedelta(days=42))
        fact = wire(25, onset=onset)
        games = {"chelsea": [(now - timedelta(days=days)).timestamp() for days in (180, 120, 60, 21, 7)]}
        applied = pair([fact], training_kickoffs=games)["applied"][0]
        remaining = sum(math.exp(-0.002 * d) for d in (180, 120, 60)) / sum(
            math.exp(-0.002 * d) for d in (180, 120, 60, 21, 7))
        self.assertAlmostEqual(applied["duration_multiplier"], remaining)
        self.assertEqual((applied["matches_in_episode"], applied["team_training_matches"]), (2, 5))
        self.assertAlmostEqual(applied["prior_weight"], 0.3375 * 0.55 * remaining)
        self.assertEqual(pair([fact])["applied"][0]["duration_basis"], "no_training_match_exposure")
        all_after = {"chelsea": games["chelsea"][-2:]}
        self.assertEqual(pair([fact], training_kickoffs=all_after)["excluded_reasons"],
                         {"absence_spans_all_team_training_matches": 1})
        official = claim(team=TEAM, published_at=CUT, received_at=CUT, known_at=CUT,
                         availability="doubtful", id="official", position="goalkeeper")
        support = pair([official, fact], training_kickoffs=games)["applied"][0]
        self.assertAlmostEqual(support["duration_multiplier"], remaining)
        self.assertEqual((support["id"], support["matches_in_episode"]), ("official", 2))
        seen = now - timedelta(hours=6)
        current = wire(50, store.iso(seen))
        self.assertTrue(pair([current])["changed"])
        after = store.iso(now + timedelta(microseconds=1))
        self.assertFalse(pair([current], after)["changed"])
        replay = current | {"last_seen_at": after}
        self.assertEqual(pair([replay], after)["excluded_reasons"], {"stale_wire_observation": 1})
        self.assertEqual(news_model.next_weight_review(current, now), store.instant(after))
        self.assertFalse(pair([current | {"last_seen_at": after}])["changed"])

    def test_stale_standing_wire_cannot_dispute_a_fresh_official_story_in_the_snapshot(self):
        stamp = store.iso(store.instant(CUT) - timedelta(hours=7))
        old = wire(50, stamp, origin="touchlinewire.com", id="wire-observation") | {
            "fixture_id": "next", "effective_at": stamp,
        }
        official = claim(team=TEAM, id="club", fixture_id="next", published_at="2026-11-25T03:00Z",
                         effective_at="2026-11-25T03:00Z", received_at="2026-11-25T04:00Z",
                         known_at="2026-11-25T04:00Z")
        current = news._current_claims([old, official], CUT)
        self.assertEqual([v["id"] for v in pair(current)["applied"]], ["club"])
        self.assertEqual(pair(current)["excluded_reasons"], {"stale_wire_observation": 1})


class UsageHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.time = store.instant("2026-10-01T10:00Z")
        for module in (store, evidence, evidence_sources):
            freeze = patch.object(module, "now_utc", side_effect=lambda: store.iso(self.time))
            freeze.start()
            self.addCleanup(freeze.stop)
        self.fixture = self.schedule("next", KICK, AWAY)
        for index, day in enumerate((4, 11, 18), 1):
            self.schedule(f"past{index}", f"2026-11-{day:02}T15:00Z", f"Club {index}")
        self.time = store.instant("2026-11-20T17:00Z")
        for index, day in enumerate((4, 11, 18), 1):
            end = f"2026-11-{day:02}T17:00Z"
            store.record_result(self.root, {
                "fixture_id": f"past{index}", "home_goals": 2, "away_goals": 0, "source": "league",
                "locator": f"result/{index}", "finalized_at": end, "published_at": end, "received_at": end,
            })
        self.time = store.instant(CUT)
        wiki = Wiki(self.time)
        wiki.members = NAMES
        wiki.identities = {name: 100 + i for i, name in enumerate(NAMES)}
        # The roster parser can only infer a position from an explicit Wikipedia category.
        def roster(url):
            doc = json.loads(wiki(url))
            for page in doc["query"]["pages"]:
                if page.get("title") in NAMES[:3]:
                    page["categories"] = [{"ns": 14, "title": "Category:Men's association football goalkeepers"}]
            return json.dumps(doc).encode()
        news_roster.refresh(self.root, TEAM, SEASON, getter=roster)

    def schedule(self, fid, kickoff, away):
        return store.register_fixture(self.root, dict(fixture_id=fid, home=TEAM, away=away, round_id=12,
                                      season=SEASON, kickoff=kickoff, source="league", locator="schedule/" + fid,
                                      known_at=store.now_utc()))

    def history(self, at=None):
        cut = store.iso(at or self.time)
        with closing(store.connect(self.root)) as db:
            db.execute("BEGIN")
            squads = news_roster.roster_views(db, [TEAM, AWAY], SEASON, cut)
            return news_importance.player_history(db, self.fixture, cut, squads, self.root)

    def capture(self, rows):
        return news_minutes.refresh(self.root, TEAM, SEASON, getter=reply(envelope(rows, store.now_utc())))

    def test_real_sync_uses_received_cumulative_minutes_and_regular_and_third_keeper(self):
        before = store.now_utc()
        self.time += timedelta(seconds=1)
        injuries = [{"player_id": i + 1, "display_name": name, "team": "Chelsea", "status": "injured",
                     "category": "injury", "chance_of_playing": 0} for i, name in enumerate(NAMES[:3])]
        injury_reply = reply(envelope(injuries, store.now_utc()))
        sync = news.sync(
            self.root, team=TEAM, season=SEASON, force=True,
            defaults=lambda t, s: [news_sources.default_sources(t, s)[0]],
            roster_refresh=lambda *_: {"status": "cached"}, extractor=lambda *a, **kw: [],
            poller=lambda src, t, s, cursor: news_sources.poll(src, t, s, cursor, getter=injury_reply),
            importance_getter=reply(envelope(usage_rows(), store.now_utc())),
        )
        self.assertEqual(sync["streams"][0]["player_usage"]["state"], "captured")
        snap = news.snapshot(self.root, "next", self.time)
        doc = pair(snap["facts"], self.time)
        keepers = {p["player_name"]: p for p in doc["applied"]}
        self.assertEqual(set(keepers), set(NAMES[:3]), doc["excluded_reasons"])
        main, deputy, third = (keepers[name] for name in NAMES[:3])
        self.assertEqual(main["role"], "goalkeeper")
        self.assertGreater(main["importance_multiplier"], deputy["importance_multiplier"])
        self.assertGreater(deputy["importance_multiplier"], third["importance_multiplier"])
        self.assertEqual(third["importance_multiplier"], 0.25)
        self.assertEqual(main["player_importance"]["matches"], 3)
        self.assertEqual(news.snapshot(self.root, "next", before)["facts"], [])
        self.assertEqual(self.history(before)["enwiki:100"]["basis"], "neutral")
        frozen = self.history()
        self.time += timedelta(days=1)
        self.assertEqual(self.capture(usage_rows())["state"], "unchanged")
        self.assertEqual(self.history(), frozen)
        mismatch = snap["facts"][0] | {"provider_player_id": 99999}
        item = pair([mismatch], snap["cutoff"])["applied"][0]
        self.assertEqual((item["importance_multiplier"], item["player_importance"]["reason"]),
                         (0.55, "provider_player_id_mismatch"))

    def test_missing_reconciled_starts_ambiguous_ids_held_scores_and_late_data_fall_back(self):
        rows = usage_rows()
        rows[3]["starts"] = rows[4]["starts"] = 0
        self.assertEqual(self.capture(rows)["state"], "captured")
        before = store.now_utc()
        self.assertEqual(self.history()["enwiki:100"]["reason"], "team_appearances_do_not_reconcile")
        self.time += timedelta(hours=13)
        rows = usage_rows()
        rows[0]["display_name"] = NAMES[1]  # Two published names resolve to different players.
        self.assertEqual(self.capture(rows)["state"], "captured")
        self.assertEqual(self.history()["enwiki:100"]["basis"], "neutral")
        self.assertEqual(self.history(before)["enwiki:100"]["reason"], "team_appearances_do_not_reconcile")
        self.assertEqual(self.history()["enwiki:102"]["basis"], "epl_season_minutes_and_starts")
        with closing(store.connect(self.root)) as db, db:
            db.execute("INSERT INTO result_dispositions VALUES(?,?,?,?,?)",
                       ("past1", store.iso("2026-11-04T15:00Z"), store.now_utc(), 1, "recheck"))
        self.assertEqual(self.history()["enwiki:102"]["reason"], "prior_results_incomplete_at_directory_snapshot")

    def test_reconfirmation_partial_roster_and_score_only_holds_preserve_appearance_history(self):
        rows = usage_rows()[:-1]
        self.capture(rows)
        now = self.time
        start = self.history()["enwiki:100"]
        self.assertEqual((start["team_starts"], start["expected_team_starts"]), (30, 33))
        self.time += timedelta(hours=1)
        def result(home=2):
            store.record_result(self.root, {
                "fixture_id":"past3", "home_goals":home, "away_goals":0, "source":"league",
                "locator":"result/3-again", "finalized_at":store.now_utc(), "published_at":store.now_utc(),
                "received_at":store.now_utc(),
            })
        result()
        self.assertEqual(start, self.history()["enwiki:100"])
        self.assertEqual(self.capture(rows)["state"], "cached")
        self.time += timedelta(minutes=10)
        result(3)
        self.assertEqual(self.history()["enwiki:100"]["reason"], "prior_results_incomplete_at_directory_snapshot")
        self.assertEqual(self.capture(rows)["state"], "captured")  # unchanged minutes, new publication after correction
        self.assertEqual(self.history()["enwiki:100"]["basis"], start["basis"])
        self.assertEqual(self.capture(rows)["state"], "cached")
        self.assertEqual(self.history(now)["enwiki:100"], start)
        self.time += timedelta(hours=1)
        with closing(store.connect(self.root)) as db, db:
            db.execute("INSERT INTO result_dispositions VALUES(?,?,?,?,?)",
                       ("past1", store.iso("2026-11-04T15:00Z"), store.now_utc(), 1, "cross_source_score_conflict"))
        disputed = self.history()["enwiki:100"]
        self.assertEqual(disputed["score_disputed_fixtures"], ["past1"])
        self.assertEqual(disputed["matches"], 3)
        self.time += timedelta(hours=1)
        with closing(store.connect(self.root)) as db, db:
            db.execute("INSERT INTO result_dispositions VALUES(?,?,?,?,?)",
                       ("past1", store.iso("2026-11-04T15:00Z"), store.now_utc(), 1, "cross_source_kickoff_conflict"))
        self.assertEqual(self.history()["enwiki:100"]["basis"], "neutral")

    def test_prior_reconciled_directory_is_labelled_when_latest_totals_fail(self):
        first = self.capture(usage_rows())["original_sha256"]
        self.time += timedelta(hours=13)
        impossible = usage_rows()
        impossible[0]["minutes"] = 270  # The two goalkeepers now exceed the club's available minutes.
        invalid = self.capture(impossible)["original_sha256"]
        result = self.history()["enwiki:100"]
        self.assertEqual(result["directory_fallback_reason"], "newer_directory_does_not_reconcile")
        self.assertEqual((result["original_sha256"], result["rejected_directory_sha256"]), (first, invalid))
        self.time += timedelta(minutes=29)
        self.assertEqual(self.capture(usage_rows())["state"], "cached")
        self.time += timedelta(minutes=1)
        self.assertEqual(self.capture(usage_rows())["state"], "captured")
        self.assertNotIn("directory_fallback_reason", self.history()["enwiki:100"])

    def test_legacy_directory_digest_validates_original_optional_appearances(self):
        rows = usage_rows()
        rows[2]["appearances"] = 0
        self.capture(rows)
        with closing(store.connect(self.root)) as db, db:
            original = news_minutes.latest(db, SEASON, "chelsea", store.iso(CUT))
            before = news_minutes.verified_players(self.root, original, TEAM, SEASON)
            legacy = [{k: v for k, v in p.items() if k != "appearances"} for p in before]
            db.execute("UPDATE news_usage_history SET usage_sha256=?", (store.digest(legacy),))
        self.assertEqual(self.history()["enwiki:102"]["appearances"], 0)
        self.assertNotIn("appearances", self.history()["enwiki:101"])
        self.time += timedelta(hours=13)
        self.assertEqual(self.capture(rows)["state"], "captured")
        rows[2]["minutes"] = 1
        self.time += timedelta(hours=13)
        self.assertEqual(self.capture(rows)["state"], "unavailable")
        self.assertEqual(self.history()["enwiki:102"]["minutes"], 0)

    def test_confirmed_prior_sheets_only_and_later_correction_does_not_rewrite_history(self):
        evidence.register_source(self.root, {
            "source_id": "club-team-sheets", "free": True, "status": "active", "upstream": "club",
            "terms_locator": "https://club.example/open-terms", "fields": {
                "lineup": {"methods": ["manual"], "uses": ["prediction"]}},
        })
        def record(i, revision=1, **overrides):
            kickoff = f"2026-11-{(4, 11, 18)[i-1]:02}T15:00Z"
            published = store.iso(store.instant(kickoff) - timedelta(hours=1))
            return {"source_id": "club-team-sheets", "method": "manual", "field": "lineup", "team": TEAM,
                    "fixture_id": f"past{i}", "locator": f"club/match/{i}", "published_at": published,
                    "valid_until": kickoff, "received_at": CUT, "revision": revision,
                    "value": {"confirmed": True, "starters": [NAMES[0], *NAMES[3:]], "bench": [NAMES[1]]},
                    **overrides}
        evidence.import_evidence(self.root, [record(i) for i in (1, 2, 3)])
        early = store.now_utc()
        first = self.history()
        self.assertEqual((first["enwiki:100"]["matches"], first["enwiki:100"]["starts"]), (3, 3))
        self.assertEqual(first["enwiki:101"]["starts"], 0)
        # No membership proof exists at any prior kickoff for today's unlisted third keeper.
        self.assertEqual(first["enwiki:102"]["basis"], "neutral")
        self.time += timedelta(hours=1)
        evidence.import_evidence(self.root, record(3, 2, value={"confirmed": False, "starters": []}))
        self.assertEqual(self.history()["enwiki:100"]["basis"], "neutral")
        self.assertEqual(self.history(early), first)
