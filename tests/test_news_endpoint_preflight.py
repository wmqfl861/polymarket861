"""A v2 preflight inspects local inputs without starting or backfilling a study."""

from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from polymarket861 import news, news_endpoint_originals, news_endpoint_study as v1
from polymarket861 import news_endpoint_v2 as v2, store
from test_news_endpoint_study import collect_native_seal, raw_seal
from test_news_endpoints_v2 import capture_clock, cli
from test_news_matchday import AWAY, AWAY_RUMOUR, HOME, HOME_OUT, KICKOFF
from test_news_matchday import add_club_feed, rss, seed_fixture
from test_paired_proof import at


class EndpointPreflight(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "runtime"
        self.history = Path(tmp.name) / "history"

    def preflight(self, *args):
        return cli(self.root, "preflight", "--version", "v2", *args)

    def test_published_a2_artifact_and_drift_diagnostics(self):
        # Independent literal and the checked-in 0.5.0a2 public design.
        pin = "3db775698c93664b5ba749be8a03f94f75940f212438849ad63e4cf672002a61"
        artifact = Path(__file__).resolve().parents[1] / "reports/five-leagues-0.5.0a2/news-endpoint-study-v2.json"
        frozen = json.loads(artifact.read_text())
        self.assertEqual(v2.proposal(), {"plan": frozen["plan"], "plan_sha256": pin})
        self.assertEqual(v2.PUBLISHED_PLAN_SHA256, store.digest(frozen["plan"]))
        with at("2026-08-01T10:00Z"):
            cert = cli(self.root, "register", "--version", "v2")
            legacy = cli(self.root, "register", "--version", "v1")
        changed = frozen["plan"] | {"reporting": frozen["plan"]["reporting"].replace("end publish", "end, publish")}
        drift = store.digest(changed)
        self.assertEqual(drift, "5ead6f2492d7328b4cdbab5110a877900ec30506d71449e93948a4e39a5948a7")
        with patch.object(v2, "proposal", return_value={"plan": changed, "plan_sha256": drift}):
            for action in ("report", "register", "preflight"):
                with self.subTest(action=action), self.assertRaisesRegex(
                    ValueError, re.escape(f"Installed v2 plan drift: stored={pin} current={drift}")
                ):
                    cli(self.root, action, "--version", "v2")
            with self.assertRaisesRegex(ValueError, f"stored=not_checked current={drift}"):
                cli(self.root, "plan", "--version", "v2")
            self.assertEqual(cli(self.root, "report", "--version", "v1")["registration"], legacy)
        self.assertEqual(cli(self.root, "report")["registration"], cert)
        self.assertEqual(cli(self.root, "register", "--version", "v2"), cert)
        # A self-consistent registration for an unsupported design is a different failure
        # from alteration of persisted certificate or plan bytes.
        future = {k: v for k, v in cert.items() if k != "registration_sha256"}
        future.update(plan=changed, plan_sha256=drift)
        with closing(store.connect(self.root)) as db, db:
            db.execute("UPDATE news_endpoint_declarations SET digest=?,manifest=? WHERE version=?",
                       (store.digest(future), store.canonical(future), v1.V2_VERSION))
        with self.assertRaisesRegex(ValueError, re.escape(f"Unsupported stored v2 plan: stored={drift} current={pin}")):
            self.preflight()
        future["plan_sha256"] = pin
        with closing(store.connect(self.root)) as db, db:
            db.execute("UPDATE news_endpoint_declarations SET digest=?,manifest=? WHERE version=?",
                       (store.digest(future), store.canonical(future), v1.V2_VERSION))
        with self.assertRaisesRegex(ValueError, re.escape(f"Corrupt stored v2 plan: stored={pin} recomputed={drift}")):
            self.preflight()
        with closing(store.connect(self.root)) as db, db:
            db.execute("UPDATE news_endpoint_declarations SET digest=? WHERE version=?", ("f" * 64, v1.V2_VERSION))
        with self.assertRaisesRegex(ValueError, f"Corrupt v2 registration: stored={'f' * 64} recomputed="):
            cli(self.root, "report")

    def test_empty_and_legacy_databases_remain_unchanged(self):
        result = self.preflight()
        self.assertEqual(result["decision"], "insufficient_local_evidence")
        self.assertIsNone(result["future_eligible_windows"])
        self.assertFalse(result["local_sources"])
        self.assertFalse(self.root.exists())
        by_source = {s["source"]: s["raw_response_policy"] for s in result["default_source_policies"]}
        self.assertEqual(by_source, {"touchline-availability": "not_retainable", "touchline-rumours": "not_retainable",
                                     "wikipedia-season-news": "retainable"})
        with self.assertRaisesRegex(ValueError, "1 and 200"):
            self.preflight("--limit", "0")
        self.root.mkdir()
        dbfile = self.root / "runtime.sqlite3"
        with closing(sqlite3.connect(dbfile)) as db:
            db.execute("CREATE TABLE old_table(id)")
        before = dbfile.read_bytes()
        self.assertEqual(self.preflight()["fixed_windows_checked"], 0)
        self.assertEqual(dbfile.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [dbfile])

    def _native(self, *, register=False, restricted_home=False):
        kickoff = store.instant(KICKOFF)
        with at("2026-08-01T10:00Z"):
            seed_fixture(self.root, self.history)
            v1.enroll(self.root)
            if register:
                v2.enroll(self.root)
            home, away = (add_club_feed(self.root, team) for team in (HOME, AWAY))
            if restricted_home:
                news.disable_feed(self.root, home["id"])
                home = news.add_feed(self.root, home | {
                    "id": "private-bulletins", "license": "Private personal use only",
                    "url": "https://arsenal.example/licensed.xml",
                })
        feeds = {home["url"]: rss(home, HOME_OUT), away["url"]: rss(away, AWAY_RUMOUR)}
        return collect_native_seal(self.root, self.history, feeds, kickoff - timedelta(minutes=95),
                                   kickoff - timedelta(minutes=61))

    def test_applied_source_and_both_clubs_checked_before_opt_in(self):
        with capture_clock(), at(KICKOFF):
            self._native(restricted_home=True)
            diag = self.preflight()
            self.assertEqual(diag["decision"], "review_observed_blockers_before_registration")
            window, = diag["windows"]
            self.assertEqual(window["news_evidence"], "expected_ineligible")
            self.assertIn("required_source_does_not_retain_originals", window["known_blockers"])
            applied, = window["applied_facts"]
            used = [d for d in window["fact_dependencies"] if d["id"] in applied["evidence_ids"]]
            self.assertEqual([d["source"] for d in used], ["private-bulletins"])
            self.assertEqual(used[0]["v2_original"], "no_v2_binding")
            self.assertEqual(window["coverage"]["home"]["sources"][0]["raw_response_policy"], "not_retainable")
            self.assertEqual(window["coverage"]["away"]["sources"][0]["raw_response_policy"], "retainable")
            self.assertFalse(window["v2_binding"])
            self.assertEqual(diag["registration"], {"state": "not_registered"})
            self.assertIsNone(diag["future_eligible_windows"])
            # The preflight deliberately does not veto an explicit enrollment.
            v2.enroll(self.root)
            self.assertEqual(v2.report(self.root)["bound_seals"], 0)
            self.assertFalse(self.preflight()["windows"][0]["v2_binding"])

    def test_prior_open_feeds_and_v1_originals_never_become_v2_observations(self):
        with capture_clock(), at(KICKOFF):
            self._native()
            with closing(store.connect(self.root)) as db:
                count = db.execute("SELECT count(*) FROM news_endpoint_poll_originals").fetchone()[0]
                self.assertGreater(count, 0)
            window, = self.preflight()["windows"]
            self.assertTrue(window["timely_two_team_coverage"])
            self.assertEqual(window["news_evidence"], "unknown")
            self.assertTrue(all(d["raw_response_policy"] == "retainable" for d in window["fact_dependencies"]))
            self.assertFalse(window["v2_binding"])
            with patch.object(v2, "_policy", return_value={"protocol": "future", "sha256": "0" * 64}):
                changed = self.preflight()["windows"][0]
            self.assertIn("incompatible_news_policy", changed["known_blockers"])

    def test_bound_originals_reopened_and_missing_fact_not_hidden_by_other_club(self):
        with capture_clock(), at(KICKOFF):
            self._native(register=True)
            window, = self.preflight()["windows"]
            self.assertEqual(window["news_evidence"], "bound_news_originals_verified")
            self.assertTrue(all(d["v2_original"] == "verified" for d in window["fact_dependencies"]))
            with patch.object(v2, "_policy", return_value={"protocol": "future", "sha256": "0" * 64}):
                existing = self.preflight()
            self.assertFalse(existing["registration"]["matches_installed_news_policy"])
            self.assertEqual(existing["windows"][0]["news_evidence"], "bound_news_originals_verified")
            with closing(store.connect(self.root)) as db, db:
                db.execute("DELETE FROM news_endpoint_v2_poll_originals WHERE source='arsenal-bulletins'")
                count = db.execute(
                    f"SELECT count(*) FROM {news_endpoint_originals.V1_ORIGINALS} WHERE source='arsenal-bulletins'"
                ).fetchone()[0]
                self.assertGreater(count, 0)
            after = self.preflight()["windows"][0]
            proof = {d["source"]: d["v2_original"] for d in after["fact_dependencies"]}
            self.assertEqual(proof, {"arsenal-bulletins": "missing_or_invalid", "chelsea-bulletins": "verified"})
            self.assertEqual(after["news_evidence"], "expected_ineligible")
            self.assertIn("missing_or_invalid_v2_original", after["known_blockers"])

    def test_first_fixed_windows_only_and_unverifiable_rows_remain_unknown(self):
        stamp = store.instant("2026-08-10T10:00Z")
        with closing(store.connect(self.root)) as db, db:
            first = raw_seal(db, "epl/2026-27/first/a", stamp)
            raw_seal(db, "epl/2026-27/first/a", stamp, mode="formal", era="b" * 64)
            raw_seal(db, "epl/2026-27/second/a", stamp + timedelta(days=1))
            raw_seal(db, "epl/2026-27/replay/a", stamp, mode="replay")
            raw_seal(db, "epl/2026-27/candidate/a", stamp, purpose="candidate")
            raw_seal(db, "lal/2026-27/other/a", stamp)
            early = raw_seal(db, "epl/2026-27/early/a", stamp)
            db.execute("UPDATE forecast_seals SET horizon='early' WHERE digest=?", (early,))
        with at(stamp + timedelta(days=2)):
            result = self.preflight("--limit", "1")
            full = self.preflight()
        self.assertEqual(result["fixed_windows_total"], 2)
        self.assertEqual(result["fixed_windows_checked"], 1)
        self.assertEqual(full["windows"][1]["seal"], first)
        self.assertEqual(full["news_evidence_counts"]["unknown"], 2)
        self.assertIn("cannot_verify_local_seal", full["windows"][0]["reason"])

    def test_damaged_first_bound_seal_never_yields_to_a_later_mode(self):
        with capture_clock(), at(KICKOFF):
            self._native(register=True)
            with closing(store.connect(self.root)) as db, db:
                first = db.execute("SELECT * FROM forecast_seals").fetchone()
                payload = json.loads(first["data_json"]) | {"mode": "formal"}
                second = dict(first) | {"mode": "formal", "digest": store.digest(payload),
                                        "data_json": store.canonical(payload)}
                db.execute("INSERT INTO forecast_seals VALUES(?,?,?,?,?,?,?,?,?,?)", tuple(second.values()))
                self.assertEqual(db.execute("SELECT count(*) FROM news_endpoint_v2_bindings").fetchone()[0], 2)
            for body in (
                store.canonical({k: v for k, v in json.loads(first["data_json"]).items() if k != "purpose"}),
                "[]", "null", "{broken",
            ):
                with self.subTest(body=body[:30]):
                    with closing(store.connect(self.root)) as db, db:
                        db.execute("UPDATE forecast_seals SET data_json=? WHERE digest=?", (body, first["digest"]))
                    diag = self.preflight()
                    win, = diag["windows"]
                    self.assertTrue(win["v2_binding"])
                    self.assertEqual((win["seal"], win["news_evidence"]), (first["digest"], "unknown"))
                    self.assertEqual(diag["news_evidence_counts"]["bound_news_originals_verified"], 0)
                    self.assertEqual(v2.report(self.root)["horizons"]["T-60m"]["qualified_news_pairs"], 0)

    def test_descriptive_preflight_includes_legacy_main_seals(self):
        stamp = store.instant("2026-08-10T10:00Z")
        with closing(store.connect(self.root)) as db, db:
            old = raw_seal(db, "epl/2026-27/legacy/a", stamp)
            db.execute("""UPDATE forecast_seals SET model_version='B1',
                          data_json=json_remove(data_json,'$.purpose') WHERE digest=?""", (old,))
        with at(stamp + timedelta(days=1)):
            win, = self.preflight()["windows"]
        self.assertEqual(win["seal"], old)
        self.assertEqual(win["news_evidence"], "unknown")


    def test_one_damaged_subscription_is_unknown_without_hiding_the_other_sources(self):
        with capture_clock(), at(KICKOFF):
            self._native(register=True)
            with closing(store.connect(self.root)) as db, db:
                row = db.execute("SELECT * FROM news_streams WHERE source='wikipedia-season-news' LIMIT 1").fetchone()
                self.assertIsNotNone(row)
                db.execute("""UPDATE news_streams SET config=json_set(config,'$.team',NULL)
                              WHERE season=? AND team_key=? AND source=?""",
                           (row["season"], row["team_key"], row["source"]))
            result = self.preflight()
            selected = [source for source in result["local_sources"] if source["type"] == "recorded_subscription"]
            bad, = [source for source in selected if source["raw_response_policy"] == "unknown"]
            self.assertEqual((bad["source"], bad["team"]), (row["source"], row["team"]))
            self.assertGreater(len(selected), 1)
            self.assertEqual(result["fixed_windows_checked"], 1)
