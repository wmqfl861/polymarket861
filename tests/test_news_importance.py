import json
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


def pair(facts, at=CUT):
    base = model() | {"cutoff": at, "fixture_id": "next", "kickoff": KICK}
    return news_model.make_pair(base, facts, home=TEAM, away=AWAY, cutoff=at)


class UsageModelTests(unittest.TestCase):
    def test_probability_is_monotone_across_statuses_and_rumours_have_no_effect(self):
        expected = [0.45, 0.3375, 0.225, 0.1125, 0]
        for percent, weight in zip((0, 25, 50, 75, 100), expected, strict=True):
            doc = pair([wire(percent)])
            self.assertAlmostEqual(doc["log_goal_shifts"]["away"], 0.045 * weight)
            self.assertEqual(doc["no_news"]["probabilities"], model()["probabilities"])
        for percent in (False, -1, 110, "25"):
            self.assertEqual(pair([wire(percent)])["excluded_reasons"], {"invalid_playing_probability": 1})
        self.assertFalse(pair([wire(0, status="rumour")])["changed"])
        self.assertAlmostEqual(pair([wire(None)])["applied"][0]["absence_weight"], 0.2)
        self.assertEqual(pair([wire(25, availability="unknown")])["applied"][0]["availability"], "doubtful")

    def test_duration_reaffirmation_boundaries_stale_feed_and_separate_official_support(self):
        now = store.instant(CUT)
        for days, multiplier in news_model.POLICY["duration_steps_days"]:
            onset = store.iso(now - timedelta(days=days))
            fact = wire(25, onset=onset)
            doc = pair([fact])
            self.assertAlmostEqual(doc["log_goal_shifts"]["away"], 0.045 * 0.3375 * multiplier)
            if multiplier == 0:
                self.assertEqual(doc["excluded_reasons"], {"absence_duration_limit": 1})
                official = claim(team=TEAM, published_at=CUT, received_at=CUT, known_at=CUT,
                                 availability="doubtful", id="official", position="goalkeeper")
                self.assertFalse(pair([official, fact])["changed"])
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
        self.assertEqual(pair([mismatch], snap["cutoff"])["applied"][0]["importance_multiplier"], 1)

    def test_missing_reconciled_starts_ambiguous_ids_held_scores_and_late_data_fall_back(self):
        rows = usage_rows()
        rows[3]["starts"] -= 1
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
