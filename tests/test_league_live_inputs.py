import hashlib
import itertools
import json
import sqlite3
import tempfile
import unittest
import zlib
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

from polymarket861 import big5_market, football, league_live_inputs as live, league_registry as registry
from polymarket861 import market_archive, market_provider, store


SEASON = "2026-27"
SEEN = "2026-11-26T12:00:00.000000Z"
KICK = "2026-11-27T20:00:00.000000Z"


class LiveFootballInputsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.history = self.root / "football"
        self.market = self.root / "market"
        self.state = self.root / "inputs"
        self.path = self.history / SEASON / "es.1.json"
        self.path.parent.mkdir(parents=True)
        self.home, self.away = "Levante UD", "Real Racing Club de Santander"
        self.target = {"round": "Matchday 14", "date": "2026-11-29", "team1": self.home, "team2": self.away}
        self.prior = {"round": "Matchday 1", "date": "2026-08-15", "team1": self.away,
                      "team2": self.home, "score": {"ft": [0, 1]}}
        self.path.write_text(json.dumps({"matches": [self.target, self.prior]}))
        blob = registry.git_blob(self.path.read_bytes())
        for context in (patch.object(registry, "source_blob", return_value=blob),
                        patch.object(live.league_openfootball_refresh, "refresh", return_value={"state": "ok"}),
                        patch.object(live, "_poll_wiki"), patch.object(live, "_observe_moves"),
                        patch.object(store, "now_utc", return_value=SEEN)):
            context.start()
            self.addCleanup(context.stop)

    def event(self):
        series, tag = big5_market.MARKETS["lal"]
        return {"id": "333", "slug": "lal-lev-rrc-2026-11-27", "startTime": KICK,
                "sport": {"sport": "lal", "series": series}, "series": [{"id": series}],
                "tags": [{"id": tag}], "teams": [
                    {"name": self.home, "league": "lal", "id": 114298, "abbreviation": "lev", "ordering": "home"},
                    {"name": "Real Racing Club", "league": "lal", "id": 3284301, "abbreviation": "rrc",
                     "ordering": "away"}], "live": False, "ended": False, "markets": []}

    def record_event(self, event, stamp):
        root = big5_market.research_root(self.market)
        url = market_provider.GAMMA + "/events/slug/" + event["slug"]
        reply = market_provider.Reply(json.dumps(event).encode(), stamp, stamp,
                                      {"date": format_datetime(football.utc(stamp), usegmt=True)}, url)
        with closing(big5_market._db(root)) as db:
            with patch.object(market_provider, "get", return_value=reply):
                _, _, sha = market_archive._fetch_once(root, "league_event", event["slug"], url)
            with db:
                big5_market._record(db, event, big5_market._identity(event, "lal"), reply, sha)

    def inputs(self, at=None):
        return live.load_inputs(self.state, league="lal", football_root=self.history,
                                market_state_dir=self.market, as_of=at)

    def test_verified_receipt_supplies_minute_clock_and_scoped_training(self):
        stamp = store.iso(football.utc(SEEN) - timedelta(hours=1))
        self.record_event(self.event(), stamp)
        first = self.inputs()
        match, = first.upcoming
        self.assertEqual((match.home, match.away, match.kickoff_precision), (self.home, self.away, "minute"))
        self.assertEqual((store.iso(match.kickoff), store.iso(match.schedule_known_at)), (KICK, SEEN))
        prior = next(f for f in first.fixtures if f != match)
        self.assertEqual(store.iso(prior.result_versions[0].available), SEEN)
        early = self.inputs(stamp)
        self.assertEqual(early.upcoming, ())
        seen = football.utc(SEEN)
        pred = football.FootballEngine(first.fixtures, league="lal").forecast(match.home, match.away,
                                                                              match.kickoff, seen)
        self.assertEqual(pred["evidence"]["training_matches"], 1)
        self.assertAlmostEqual(pred["probabilities"]["away_win"], 1.0)
        self.assertEqual(early.provenance["status"], "current_season_identity_not_yet_observed")
        binding = first.provenance["market_fixtures"][match.fixture_id]
        self.assertTrue(live.still_pregame(self.market, binding, SEEN))
        stopped = self.event() | {"live": True}
        later = store.iso(seen + timedelta(minutes=2))
        self.record_event(stopped, later)
        self.assertFalse(live.still_pregame(self.market, binding, later))
        self.assertEqual(self.inputs(later).upcoming, ())
        self.assertEqual(len(self.inputs(SEEN).upcoming), 1)

    def test_rechecks_native_names_when_same_time_event_has_changed(self):
        self.record_event(self.event(), SEEN)
        original = self.inputs()
        self.assertEqual(len(original.upcoming), 1)
        changed = self.event()
        changed["teams"][1]["name"] = "Arsenal FC"
        later = store.iso(football.utc(SEEN) + timedelta(minutes=12))
        self.record_event(changed, later)
        self.assertFalse(live.still_pregame(self.market, original.provenance["market_fixtures"][
            original.upcoming[0].fixture_id], later))
        result = self.inputs(later)
        self.assertEqual(result.upcoming, ())
        self.assertIn("registered club", next(iter(result.provenance["market_gaps"])))

    def test_unknown_spelling_cannot_substitute_another_club_or_missing_match(self):
        event = self.event()
        event["teams"][1]["id"] = 999
        self.record_event(event, SEEN)
        self.assertEqual(self.inputs().upcoming, ())
        self.path.write_text(json.dumps({"matches": [self.target | {"date": "2027-05-01"}, self.prior]}))
        with self.assertRaisesRegex(ValueError, "unverified domestic archive bytes"):
            self.inputs()

    def test_registered_market_identity_fault_is_per_fixture_and_correctable_before_deadline(self):
        from polymarket861 import big5_scheduler as scheduler

        second = dict(self.target, team1="FC Barcelona", team2="Athletic Club")
        self.path.write_text(json.dumps({"matches": [self.target, second, self.prior]}))
        event = self.event()
        event.update(slug="lal-bar-ath-2026-11-27", id="444")
        for native, (club, abbr, identity) in zip(event["teams"], (("FC Barcelona", "bar", "20"),
                                                                  ("Athletic Club", "ath", "21"))):
            native.update(name=club, abbreviation=abbr, id=identity)
        self.record_event(self.event(), SEEN)
        self.record_event(event, SEEN)
        clock = football.utc(SEEN)
        with patch.object(store, "now_utc", side_effect=lambda: store.iso(clock)), \
             patch.object(registry, "source_blob", return_value=registry.git_blob(self.path.read_bytes())):
            def cycle():
                return scheduler.tick(self.state, leagues=["lal"], market_state_dir=self.market,
                                      football_root=self.history, collect_news=False)["leagues"]["lal"]

            self.assertEqual(cycle()["eligible_upcoming"], 2)
            clock = football.utc(KICK) - timedelta(hours=24, minutes=3)
            invalid = self.event()
            invalid["teams"][1]["name"] = "Arsenal FC"
            self.record_event(invalid, store.iso(clock))
            bad = cycle()
            fid = football.fixture_id(SEASON, self.home, self.away, league="lal")
            self.assertEqual((bad["state"], bad["eligible_upcoming"], bad["fixture_errors"][0]["fixture"]),
                             ("partial", 1, fid))
            with closing(scheduler._database(self.state, "lal")) as db:
                seals = db.execute("SELECT fixture FROM domestic_jobs WHERE horizon='T-24h' AND sealed IS NOT NULL")
                self.assertEqual([r[0] for r in seals], [football.fixture_id(SEASON, second["team1"],
                                                                            second["team2"], league="lal")])
            clock += timedelta(minutes=1)
            self.record_event(self.event(), store.iso(clock))
            fixed = cycle()
            self.assertEqual((fixed["state"], fixed["eligible_upcoming"], fixed["fixture_errors"]), ("ok", 2, []))
            with closing(scheduler._database(self.state, "lal")) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM domestic_jobs WHERE horizon='T-24h' "
                                            "AND sealed IS NOT NULL").fetchone()[0], 2)

    def test_large_schedule_move_needs_a_matching_dated_source_and_native_clock(self):
        self.path.write_text(json.dumps({"matches": [self.target | {"date": "2027-02-07",
                                                                    "round": "Matchday 23"}, self.prior]}))
        blob = registry.git_blob(self.path.read_bytes())
        self.record_event(self.event(), SEEN)
        with patch.object(registry, "source_blob", return_value=blob):
            self.assertEqual(self.inputs().upcoming, ())
            body = b"reviewed club schedule with both teams, date and official report"
            digest = hashlib.sha256(body).hexdigest()
            review = ("2026-27 club season", self.home, self.away, KICK, digest, "verified-report")
            later = store.iso(football.utc(SEEN) + timedelta(minutes=10))
            with patch.dict(live._MOVED, {7: review}, clear=True):
                with closing(sqlite3.connect(self.state / "sources/domestic-live-inputs/receipts.sqlite3")) as db, db:
                    db.execute("INSERT INTO wiki_receipts VALUES(?,?,?,?,?,?,?,?)",
                               ("lal", SEASON, 7, SEEN, later, review[0], digest, zlib.compress(body)))
                self.assertEqual(self.inputs(SEEN).upcoming, ())
                received = self.inputs(later)
                match, = received.upcoming
                self.assertEqual(store.iso(match.schedule_known_at), later)
                binding = received.provenance["market_fixtures"][match.fixture_id]
                self.assertEqual(binding["reschedule_source"]["content_sha256"], digest)
                self.assertFalse(live.still_pregame(self.market, binding, SEEN))
                self.assertTrue(live.still_pregame(self.market, binding, later))

    def test_current_source_correction_and_withdrawal_preserve_as_of_history(self):
        self.inputs()
        mid = "2026-11-26T13:00:00.000000Z"
        end = "2026-11-26T14:00:00.000000Z"
        rows = []
        for when, edition, status in [(mid, "b", {"score": {"ft": [2, 1]}}),
                                      (end, "c", {"status": "awarded"})]:
            file = self.root / edition / SEASON / "es.1.json"
            file.parent.mkdir(parents=True)
            file.write_text(json.dumps({"matches": [self.target, self.prior | status]}))
            rows.append((when, edition * 40, registry.git_blob(file.read_bytes()), file))
        with patch.object(live.league_openfootball_refresh, "snapshots",
                          side_effect=lambda root, lg, s, at: (x for x in rows if x[0] <= at)):
            corrected = self.inputs(mid).fixtures[1]
            self.assertEqual([v.score for v in corrected.result_versions], [(0, 1), (2, 1)])
            replaced = self.inputs(end).fixtures[1]
        self.assertIsNone(replaced.home_goals)
        self.assertEqual([v.score for v in replaced.result_versions], [(0, 1), (2, 1), None])
        engine = football.FootballEngine([replaced], league="lal")
        self.assertEqual(engine.fit(SEASON, football.utc(mid), "another").evidence["training_matches"], 1)
        with self.assertRaisesRegex(ValueError, "no eligible historical results"):
            engine.fit(SEASON, football.utc(end), "another")

    def test_late_replacement_retracts_a_score_with_no_new_score_publication(self):
        self.record_event(self.event(), SEEN)
        initial = self.inputs()
        fid = initial.upcoming[0].fixture_id
        received = "2026-11-27T22:00:00.000000Z"
        moved = "2026-11-28T00:00:00.000000Z"
        new_kick = "2026-11-29T20:00:00.000000Z"
        self.record_event(self.event() | {"startTime": new_kick}, moved)
        archived = [(received, "wikipedia", "wiki:old-final", received, {fid: (2, 1)})]
        with patch.object(live, "_wiki_history", return_value=(archived, [])):
            before = next(f for f in self.inputs(received).fixtures if f.fixture_id == fid)
            self.assertEqual((before.status, before.home_goals, before.away_goals), ("completed", 2, 1))
            after = self.inputs(moved)
        result = next(f for f in after.fixtures if f.fixture_id == fid)
        self.assertEqual((result.status, result.home_goals, result.away_goals), ("scheduled", None, None))
        self.assertEqual([(store.iso(v.available), store.iso(v.actual_kickoff), v.score) for v in result.result_versions],
                         [(received, store.iso(KICK), (2, 1)), (moved, store.iso(KICK), None)])
        self.assertNotIn(fid, {f.fixture_id for f in after.upcoming})
        held, = after.provenance["withheld_fixtures"]
        self.assertEqual((held["fixture"], held["observed_at"], held["replacement_kickoff"]), (fid, moved, new_kick))
        self.assertIn("no pre-kickoff cancellation", held["error"])

    def test_unregistered_new_fixture_is_quarantined_and_independent_schedule_survives(self):
        self.record_event(self.event(), SEEN)
        self.inputs()
        late = store.iso(football.utc(SEEN) + timedelta(hours=1))
        fixed = store.iso(football.utc(late) + timedelta(hours=1))
        extra = dict(self.target, team2=next(team for team in registry.members("lal", SEASON)
                                             if team not in (self.home, self.away)))
        bad_id = football.fixture_id(SEASON, self.home, extra["team2"], league="lal")
        snapshots = []
        for at, revision, matches in ((late, "b", [self.prior, self.target, extra]),
                                      (fixed, "c", [self.prior, self.target])):
            path = self.root / revision / SEASON / "es.1.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"matches": matches}))
            snapshots.append((at, revision * 40, registry.git_blob(path.read_bytes()), path))
        with patch.object(live.league_openfootball_refresh, "snapshots",
                          side_effect=lambda root, league, season, at: [row for row in snapshots if row[0] <= at]):
            partial = self.inputs(late)
            self.assertEqual(partial.provenance["unregistered_fixtures"], [bad_id])
            self.assertNotIn(bad_id, {f.fixture_id for f in partial.fixtures})
            self.assertEqual(len(partial.upcoming), 1)
            valid_id = partial.upcoming[0].fixture_id
            self.assertEqual(set(partial.provenance["market_fixtures"]), {valid_id})
            self.assertEqual(self.inputs(fixed).provenance["unregistered_fixtures"], [])
            self.assertEqual(self.inputs(late).provenance["unregistered_fixtures"], [bad_id])

    def test_market_source_outage_and_fresh_empty_catalog_have_different_health(self):
        from polymarket861 import big5_scheduler as scheduler, market_watch_health as watch

        def cycle():
            return scheduler.tick(self.state, leagues=["lal"], market_state_dir=self.market,
                                  football_root=self.history, collect_news=False)["leagues"]["lal"]

        with watch.monitor(scheduler._directory(self.state, "lal"), ["lal"], None, None,
                           continuous=True, discover=False, notify_systemd=False) as heartbeat:
            self.assertEqual(cycle()["source_errors"]["market"]["state"], "unavailable")
            heartbeat(1, None)
            self.assertFalse(scheduler.status(self.state, leagues=["lal"], health_only=True)["healthy"])
            market_root = big5_market.research_root(self.market)
            with closing(big5_market._db(market_root)):
                pass
            self.assertEqual(cycle()["source_errors"]["market"]["state"], "unobserved")
            # A completed rolling traversal with a genuine empty response is enough to resume.
            with watch.monitor(market_root, ["lal"], None, None, continuous=True, discover=True,
                               rolling=True, notify_systemd=False) as capture:
                capture(1, {"leagues": {"lal": {"page_walk_complete": True, "gaps": [], "pages": [
                    {"rows": 0, "http_fresh": True, "received_at": SEEN}
                ]}}})
            self.assertEqual((cycle()["state"], cycle()["eligible_upcoming"]), ("ok", 0))
            self.assertTrue(scheduler.status(self.state, leagues=["lal"], health_only=True)["healthy"])
            self.record_event(self.event(), SEEN)
            self.assertEqual(cycle()["eligible_upcoming"], 1)
            self.market = self.root / "missing-again"
            self.assertEqual(cycle()["state"], "partial")
            # The unseen new schedule cannot withdraw the earlier verified match or its open windows.
            detail = scheduler.status(self.state, leagues=["lal"])["leagues"]["lal"]
            self.assertEqual(len(detail["eligible_upcoming"]), 1)
            self.assertEqual(detail["dispatch"]["T-24h"], {"pending": 1})

    def test_wikipedia_revision_scope_and_administrative_scores(self):
        for league in live._WIKI_PAGES:
            with self.subTest(league=league):
                clubs = registry.members(league, SEASON)
                names = [f"|name_T{i}=[[{team}]]" for i, team in enumerate(clubs)]
                cells = [f"| match_T{h}_T{a} = " + ("0–0" if (h, a) == (0, 1) else
                                                        "2–0 (awarded)" if (h, a) == (1, 0) else "")
                         for h, a in itertools.permutations(range(len(clubs)), 2)]
                def raw(lines):
                    rev = {"revid": 7, "timestamp": SEEN, "slots": {"main": {"content": "\n".join(lines)}}}
                    page = {"ns": 0 if league == "sea" else 10, "title": live._wiki_title(league, SEASON),
                            "revisions": [rev]}
                    return json.dumps({"query": {"pages": [page], "rightsinfo": {"url": live.wiki.LICENSE}}}).encode()
                _, _, scores = live._wiki_scores(raw(names + cells), league, SEASON)
                self.assertEqual(sum(v is not None for v in scores.values()), 1)
                self.assertEqual(scores[football.fixture_id(SEASON, *clubs[:2], league=league)], (0, 0))
                with self.assertRaisesRegex(ValueError, "duplicate Wikimedia fixture"):
                    live._wiki_scores(raw(names + cells + cells[:1]), league, SEASON)
                with self.assertRaisesRegex(ValueError, "unregistered club"):
                    live._wiki_scores(raw(["|name_T0=[[Different club]]", *names[1:], *cells]), league, SEASON)

    def test_conflicting_score_withdraws_and_reinstates_only_after_new_receipt(self):
        fixture = football.Fixture("example", SEASON, 1, football.utc("2026-08-15T15:00Z"),
                                   self.home, self.away, 2, 0, status="completed")
        stamps = [store.iso(football.utc(SEEN) + timedelta(minutes=i)) for i in range(3)]
        events = [(stamps[0], "openfootball", "original", stamps[0], fixture),
                  (stamps[1], "wikipedia", "conflicting", stamps[1], (1, 0)),
                  (stamps[2], "openfootball", "amended", stamps[2], replace(fixture, home_goals=1))]
        versions, conflict = live._score_versions(fixture, events, ())
        self.assertFalse(conflict)
        self.assertEqual([v.score for v in versions], [(2, 0), None, (1, 0)])
        self.assertEqual([store.iso(v.available) for v in versions], stamps)
