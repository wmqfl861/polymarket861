import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import test_opportunity as ex
from polymarket861 import (
    datasets,
    football,
    input_manifest,
    market_archive,
    market_provider,
    opportunity,
    pipeline,
)
from polymarket861 import result_feed, schedule_consensus as sc, store
from test_result_feed import match

YEAR, RND, SEASON = 2026, 12, "2026-27"
FID = football.fixture_id(SEASON, ex.HOME, ex.AWAY)
AT, OLD, NEW = map(
    store.instant, ("2026-11-25T07:00Z", "2026-11-28T15:00Z", "2026-11-29T14:05Z")
)


class ProspectiveSchedule(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.time = AT - timedelta(days=2)
        stack = ExitStack()
        self.addCleanup(stack.close)
        for mod in (
            store,
            market_archive,
            result_feed,
            datasets,
            input_manifest,
            pipeline,
        ):
            stack.enter_context(
                patch.object(mod, "now_utc", side_effect=lambda: store.iso(self.time))
            )
        source = self.root / "fixtures"
        files = {}
        for season, rows in [
            (
                SEASON,
                [
                    {
                        "round": "Matchday 12",
                        "date": "2026-11-28",
                        "time": "15:00",
                        "team1": ex.HOME,
                        "team2": ex.AWAY,
                    }
                ],
            ),
            (
                "2025-26",
                [
                    {
                        "round": "Matchday 1",
                        "date": "2025-08-20",
                        "time": "15:00",
                        "team1": "East FC",
                        "team2": "West FC",
                        "score": {"ft": [2, 1]},
                    }
                ],
            ),
        ]:
            f = source / season / "en.1.json"
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps({"matches": rows}))
            files[season] = football.blob_sha(f.read_bytes())
        datasets.observe(
            self.root, source, revision="d" * 40, expected=files, register_season=SEASON
        )
        self.previous = pipeline.make_forecast(
            self.root, FID, None, mode="technical", horizon="early"
        )
        self.time = AT

    def ol(self, kick=NEW, *, reverse=False, legacy_only=False, score=None):
        row = match(902, ex.HOME, ex.AWAY, score, rnd=RND)
        row.update(
            matchDateTimeUTC=store.iso(kick),
            leagueName="Premier League 2026/2027",
            goals=[],
        )
        row["team1"]["teamId"] = 61
        row["team2"]["teamId"] = 79
        if reverse:
            row["team1"], row["team2"] = row["team2"], row["team1"]
        raw = json.dumps([row]).encode()
        if legacy_only:
            store.archive_bytes(
                self.root,
                "openligadb-results",
                raw,
                metadata={
                    "license": "ODbL-1.0",
                    "url": f"{result_feed.BASE}/getmatchdata/pl/{YEAR}/{RND}",
                },
            )
        else:
            with patch.object(
                result_feed,
                "_get",
                side_effect=lambda url: (
                    b'"change"' if "getlastchangedate" in url else raw
                ),
            ):
                result_feed.sync(
                    self.root, YEAR, round_id=RND, confirm=True, require_native=True
                )

    def gamma(self, kick=NEW, *, reverse=False, period=True):
        row = ex.event()
        slug = "epl-nor-sou-" + store.iso(kick)[:10]
        row.update(
            slug=slug,
            startTime=store.iso(kick),
            active=True,
            closed=False,
            archived=False,
            tags=[{"slug": "soccer"}, {"slug": "premier-league"}],
        )
        market = row["markets"][0]
        market["slug"] = slug + "-total-2pt5"
        market["gameStartTime"] = store.iso(kick)
        if not period:
            market["description"] = market["description"].replace(
                "first 90", "first 80"
            )
        if reverse:
            row["teams"][0]["ordering"] = "away"
            row["teams"][1]["ordering"] = "home"
        with patch.object(
            market_provider,
            "get",
            side_effect=lambda url, params=None: ex.reply(row, self.time, url),
        ):
            market_archive.observe_event(self.root, slug)
        return slug

    def counts(self):
        with closing(store.connect(self.root)) as db:
            return db.execute(
                "SELECT count(*) FROM fixture_versions WHERE fixture_id=?", (FID,)
            ).fetchone()[0]

    def test_parent_http_age_rule_is_opt_in_for_paper_entry(self):
        slug = self.gamma()
        with (
            closing(store.connect(self.root)) as db,
            patch.object(market_provider, "fresh_observation", return_value=False) as fresh,
        ):
            row = db.execute("SELECT * FROM ma_events WHERE slug=?", (slug,)).fetchone()
            _, proof = sc._gamma(self.root, db, row, self.time)
            self.assertEqual(proof["kickoff"], store.iso(NEW))
            fresh.assert_not_called()
            with self.assertRaisesRegex(ValueError, "stale Gamma archive"):
                sc._gamma(self.root, db, row, self.time + sc.MAX_AGE + timedelta(seconds=1))
            with self.assertRaisesRegex(ValueError, "fresh HTTP observation"):
                sc.archive_proof._event_capture(
                    db, self.root, row["sha"], row["received_at"], slug, require_fresh=True
                )
            fresh.assert_called_once()

    def test_updates_once_and_preserves_prior_time_and_seal(self):
        self.ol()
        slug = self.gamma()
        earlier = store.iso(self.time - timedelta(seconds=1))
        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(lambda _: sc.sync(self.root, YEAR), range(2)))
        self.assertEqual(sorted(x["corrected"] for x in outcomes), [0, 1])
        self.assertEqual(self.counts(), 2)
        prior = store.fixture_at(self.root, FID, at=earlier, prospective=True)
        new = store.fixture_at(self.root, FID, prospective=True)
        self.assertEqual(
            (prior["kickoff"], new["kickoff"]), (store.iso(OLD), store.iso(NEW))
        )
        self.assertEqual(new["source"], sc.SOURCE)
        self.assertEqual(
            opportunity.verified_fixture(self.root, slug, FID, store.now_utc())[
                "kickoff"
            ],
            new["kickoff"],
        )
        with closing(store.connect(self.root)) as db:
            proof = db.execute(
                "SELECT proof_digest,proof FROM epl_schedule_proofs WHERE fixture=?",
                (FID,),
            ).fetchone()
            self.assertEqual(proof[0], store.digest(json.loads(proof[1])))
            old = db.execute(
                "SELECT digest,data_json FROM forecast_seals WHERE fixture_id=? AND kickoff=?",
                (FID, store.iso(OLD)),
            ).fetchone()
            self.assertEqual(old[0], self.previous["digest"])
            self.assertEqual(store.digest(json.loads(old[1])), old[0])
        again = pipeline.make_forecast(
            self.root, FID, None, mode="technical", horizon="early"
        )
        self.assertNotEqual(again["digest"], self.previous["digest"])
        self.assertEqual(again["kickoff"], store.iso(NEW))
        self.assertEqual(
            result_feed.sync_season(self.root, YEAR, offline=True)[
                "upcoming_schedules"
            ]["matched"],
            1,
        )

    def check_result_learning(self, kick):
        self.ol(kick)
        self.gamma(kick)
        self.assertEqual(sc.sync(self.root, YEAR)["corrected"], 1)
        self.time = kick + timedelta(hours=2)
        self.ol(kick, score=[2, 1])
        self.time += timedelta(minutes=31)
        self.ol(kick, score=[2, 1])
        finished = result_feed.sync_season(self.root, YEAR, offline=True)
        self.assertEqual(finished["available_results"], 1)

        def game():
            m = input_manifest.verify_observed_inputs(self.root)
            return next(
                f
                for f in football.load_fixtures(
                    m["root"], evidence_path=m["evidence_path"]
                )
                if f.fixture_id == FID
            )

        admission = self.time
        if kick < OLD and admission <= OLD:
            self.assertIsNone(football._result_at(game(), admission))
            self.time = OLD + timedelta(minutes=2)
            sc.sync(self.root, YEAR)
        self.assertIsNone(football._result_at(game(), kick))
        self.assertEqual(football._result_at(game(), self.time), (2, 1))
        if admission < OLD:
            self.assertIsNone(football._result_at(game(), admission))
        self.assertEqual(game().result_versions[-1].available, self.time)

    def test_later_game_scores_enter_training_from_real_local_confirmation(self):
        self.check_result_learning(NEW)

    def test_advanced_final_uses_real_recheck_past_stale_raw_time(self):
        self.check_result_learning(OLD - timedelta(hours=19))

    def test_source_retraction_is_versioned_and_tampered_raw_is_not_reusable(self):
        self.ol()
        self.gamma()
        self.assertEqual(sc.sync(self.root, YEAR)["corrected"], 1)
        verified_at = store.now_utc()
        self.time += timedelta(minutes=31)
        self.ol(NEW + timedelta(hours=1))
        self.assertEqual(sc.sync(self.root, YEAR)["matched"], 0)
        with self.assertRaisesRegex(ValueError, "review"):
            store.fixture_at(self.root, FID, prospective=True)
        self.assertEqual(
            store.fixture_at(self.root, FID, at=verified_at, prospective=True)[
                "kickoff"
            ],
            store.iso(NEW),
        )
        self.time += timedelta(minutes=31)
        self.ol()
        self.assertEqual(sc.sync(self.root, YEAR)["matched"], 1)
        self.assertEqual(
            store.fixture_at(self.root, FID, prospective=True)["kickoff"],
            store.iso(NEW),
        )
        with closing(store.connect(self.root)) as db:
            witness = json.loads(
                db.execute(
                    "SELECT proof FROM epl_schedule_proofs WHERE fixture=?", (FID,)
                ).fetchone()[0]
            )
        store.blob_path(self.root, witness["openligadb"]["sha256"]).write_bytes(
            b"invalid"
        )
        with self.assertRaisesRegex(ValueError, "archive"):
            store.fixture_at(self.root, FID, prospective=True)

    def test_uploaded_bytes_wrong_order_and_short_market_rejected(self):
        self.ol(legacy_only=True)
        self.gamma()
        self.assertEqual(sc.sync(self.root, YEAR)["corrected"], 0)
        self.time += timedelta(minutes=1)
        self.ol(reverse=True)
        self.assertEqual(sc.sync(self.root, YEAR)["corrected"], 0)
        self.time += timedelta(minutes=31)
        self.ol()
        self.gamma(period=False)
        r = sc.sync(self.root, YEAR)
        self.assertEqual(r["corrected"], 0)
        self.assertTrue(any("regulation" in e["reason"] for e in r["source_errors"]))
        self.time += timedelta(minutes=1)
        self.gamma(reverse=True)
        self.assertEqual(sc.sync(self.root, YEAR)["matched"], 0)
        self.assertEqual(self.counts(), 1)
        with self.assertRaisesRegex(ValueError, "native"):
            store.register_fixture(self.root, {"source": sc.SOURCE})

    def test_large_moves_late_or_ambiguous_events_are_not_applied(self):
        far = OLD + timedelta(hours=49)
        self.ol(far)
        self.gamma(far)
        self.assertIn("36-hour", sc.sync(self.root, YEAR)["items"][0]["reason"])
        self.time += timedelta(minutes=31)
        self.ol()
        self.gamma()
        self.assertIn("ambiguous", sc.sync(self.root, YEAR)["items"][0]["reason"])
        self.time = OLD - timedelta(minutes=20)
        self.ol()
        self.gamma()
        self.assertIn("T-60", sc.sync(self.root, YEAR)["items"][0]["reason"])
        self.time = OLD + timedelta(minutes=10)
        rtn = sc.sync(self.root, YEAR)
        self.assertEqual(rtn["registered"], 0)
        self.assertIn("passed", rtn["late_skipped"][0]["reason"])
        self.assertEqual(self.counts(), 1)
