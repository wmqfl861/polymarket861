"""Separate forward registration for home-win and regulation full-match Over 2.5."""

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from scipy.stats import t

from polymarket861 import news_endpoint_originals, news_endpoint_study as trial
from polymarket861 import (
    kickoff_proof,
    news,
    news_market,
    news_roster,
    news_sources,
    news_study_plan,
    pipeline,
    result_admission,
    result_feed,
    season,
    store,
)
from polymarket861.blob_storage import blob_path
from polymarket861.market_provider import Reply
from kickoff_fixture import capture_example
from test_news_matchday import (
    AWAY,
    AWAY_RUMOUR,
    FID,
    HOME,
    HOME_OUT,
    KICKOFF,
    add_club_feed,
    roster_transport,
    rss,
    seed_fixture,
)
from test_paired_proof import at
from test_result_feed import match


def fixed_sqlite_clock(stamp):
    # Keep the host clock out of stored fixture tests without weakening the
    # real enrollment trigger or its timestamp checks in production.
    clock = store.iso(stamp)
    trigger = trial._TRIGGER.replace(trial._CLOCK_SQL, f"'{clock}'").replace(
        "julianday('now')", f"julianday('{clock}')"
    )
    return patch.object(trial, "_TRIGGER", trigger)


def collect_native_seal(root, history, feeds, when, cutoff):
    squads = {team: roster_transport(team) for team in (HOME, AWAY)}
    refresh = news_roster.refresh

    def fetch(url):
        if url not in feeds:
            raise ConnectionError("outside offline publisher scope")
        return Reply(feeds[url], store.now_utc(), store.now_utc(), {}, url)

    with (
        patch.dict(os.environ, {"PM861_NEWS_LLM_URL": "", "PM861_NEWS_LLM_MODEL": ""}),
        patch.object(news_sources, "_get", side_effect=fetch),
        patch.object(
            news_roster,
            "refresh",
            side_effect=lambda r, t, s: refresh(r, t, s, getter=squads[t]),
        ),
    ):
        with at(when):
            for _ in range(2):
                season._service(root, "news", when, {"football_root": history})
        with at(cutoff):
            return pipeline.make_forecast(root, FID, history)


def raw_seal(
    db,
    fixture,
    now,
    *,
    horizon="T-60m",
    mode="technical",
    purpose="main",
    plan=None,
    era="a" * 64,
    season="2026-27",
    cutoff=None,
    input_cutoff=None,
    sealed_at=None,
):
    cut = (now + timedelta(seconds=20)) if cutoff is None else cutoff
    # Vary the synthetic fixture id while keeping all binding dimensions explicit.
    kick = cut + trial.HORIZONS[horizon]
    payload = {
        "purpose": purpose,
        "season": season,
        "fixture_id": fixture,
        "input_cutoff": store.iso(input_cutoff or now),
        "news_study": {
            "plan_sha256": plan or news_study_plan.PLAN_SHA256,
            "era_sha256": era,
        },
    }
    digest = store.digest(payload)
    db.execute(
        "INSERT INTO forecast_seals VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            fixture,
            store.iso(kick),
            store.iso(cut),
            horizon,
            "B1",
            store.PROTOCOL,
            mode,
            store.iso(sealed_at or now),
            digest,
            store.canonical(payload),
        ),
    )
    return digest


class NewsEndpointStudy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_empty_report_does_not_create_db_and_is_explicit_for_each_clock(self):
        report = trial.report(self.root)
        self.assertEqual(report["state"], "not_registered")
        self.assertEqual(report["family_comparisons"], 10)
        self.assertFalse((self.root / "runtime.sqlite3").exists())
        for horizon in trial.HORIZONS:
            self.assertEqual(report["horizons"][horizon]["score_guardrail"]["pairs"], 0)
            for field in trial.CLOCKS:
                pair = report["horizons"][horizon]["clocks"][field]["full_over_2_5"]
                self.assertEqual(pair["status"], "no_matched_endpoint_pairs")
                self.assertIsNone(pair["slope"])
                self.assertIsNone(pair["interval"])

    def test_trigger_is_atomic_prospective_and_does_not_relabel_existing_seals(self):
        now = store.instant("2026-12-01T12:00:00Z")
        sqlite_clock = fixed_sqlite_clock(now)
        sqlite_clock.start()
        self.addCleanup(sqlite_clock.stop)
        with closing(store.connect(self.root)) as db, db:
            raw_seal(db, "epl/2026-27/previous/away", now - timedelta(seconds=5))
        registered = now - timedelta(seconds=3)
        with patch.object(store, "now_utc", return_value=store.iso(registered)):
            cert = trial.enroll(self.root)
            self.assertEqual(cert, trial.enroll(self.root))
            with self.assertRaisesRegex(ValueError, "immutable"):
                trial.enroll(self.root, mode="formal")
        with closing(store.connect(self.root)) as db, db:
            primary = raw_seal(db, "epl/2026-27/first/away", now)
            raw_seal(db, "epl/2026-27/old/away", now - timedelta(days=1))
            raw_seal(db, "epl/2026-27/replay/away", now, mode="replay")
            raw_seal(db, "epl/2026-27/formal/away", now, mode="formal")
            raw_seal(db, "epl/2026-27/shadow/away", now, purpose="shadow")
            raw_seal(db, "epl/2026-27/wrongplan/away", now, plan="0" * 64)
            raw_seal(db, "lal/2026-27/first/away", now)
            raw_seal(
                db,
                "epl/2026-27/oldinput/away",
                now,
                input_cutoff=registered - timedelta(seconds=1),
            )
            raw_seal(
                db, "epl/2026-27/future/away", now, sealed_at=now + timedelta(minutes=1)
            )
            rows = db.execute(
                "SELECT seal,declaration FROM news_endpoint_bindings"
            ).fetchall()
            self.assertEqual(
                [tuple(r) for r in rows], [(primary, cert["registration_sha256"])]
            )
            db.execute("DROP TRIGGER news_endpoint_bind_on_insert")
        with self.assertRaisesRegex(ValueError, "trigger"):
            trial.report(self.root)

    def test_registration_refuses_historical_or_expired_start_and_changed_manifest(
        self,
    ):
        now = store.instant("2026-11-26T12:00:00Z")
        with patch.object(store, "now_utc", return_value=store.iso(now)):
            for value in (now - timedelta(seconds=1), store.instant(trial.END)):
                with self.assertRaisesRegex(ValueError, "Start"):
                    trial.enroll(self.root, starts_at=value)
            trial.enroll(self.root, starts_at=now + timedelta(hours=1))
        with closing(store.connect(self.root)) as db, db:
            db.execute("UPDATE news_endpoint_declarations SET mode='formal'")
        with self.assertRaisesRegex(ValueError, "declaration"):
            trial.report(self.root)

    def test_optional_retention_cannot_change_operational_facts_or_sealed_arms(self):
        kickoff = store.instant(KICKOFF)
        cutoff = kickoff - timedelta(minutes=61)
        when = store.instant("2026-08-12T12:25:00Z")
        ordinary_digest = store.digest
        projections = {}
        for variant in (
            "retained",
            "false_replacement",
            "observer_error",
            "copy_error",
        ):
            with self.subTest(variant=variant), fixed_sqlite_clock(cutoff):
                root, history = (
                    self.root / variant / "app",
                    self.root / variant / "history",
                )
                with at("2026-08-01T10:00Z"):
                    seed_fixture(root, history)
                    trial.enroll(root)
                    home, away = (add_club_feed(root, team) for team in (HOME, AWAY))
                feeds = {
                    home["url"]: rss(home, HOME_OUT),
                    away["url"]: rss(away, AWAY_RUMOUR),
                }
                observations = []

                def observe(directory, encoded, receipt):
                    observations.append((encoded, receipt))
                    # Even altering the decoded configuration and proof here cannot
                    # write to the values already used to commit this batch.
                    copy = json.loads(encoded)
                    copy["proof"].clear()
                    copy["config"].clear()
                    if variant == "observer_error":
                        raise RuntimeError("private storage unavailable")
                    return {"articles": [], "status": "error"}

                def no_projection(obj):
                    if (
                        isinstance(obj, dict)
                        and "articles" in obj
                        and "request_url" in obj
                    ):
                        raise ValueError("private projection unavailable")
                    return ordinary_digest(obj)

                with ExitStack() as stack:
                    if variant in {"false_replacement", "observer_error"}:
                        stack.enter_context(
                            patch.object(
                                news_endpoint_originals, "save", side_effect=observe
                            )
                        )
                    if variant == "copy_error":
                        stack.enter_context(
                            patch.object(store, "digest", side_effect=no_projection)
                        )
                    if variant in {"copy_error", "observer_error"}:
                        stack.enter_context(
                            self.assertLogs(news.__name__, level="ERROR")
                        )
                    seal = collect_native_seal(root, history, feeds, when, cutoff)
                if variant in {"false_replacement", "observer_error"}:
                    self.assertEqual(len(observations), 2)
                    for encoded, receipt in observations:
                        self.assertIsInstance(encoded, str)
                        self.assertIsInstance(receipt, tuple)
                        self.assertTrue(
                            all(isinstance(value, (str, bytes)) for value in receipt)
                        )
                pair = seal["news_pair"]
                facts = seal["context"]["player_news"]["facts"]
                self.assertEqual(len(facts), 2)
                self.assertTrue(
                    pair["changed"] and pair["coverage"]["both_teams_observed"]
                )
                projections[variant] = (
                    sorted((fact["event"], fact["original_sha256"]) for fact in facts),
                    pair["with_news"],
                    pair["no_news"],
                    seal["news_recipe_sha256"],
                    seal["news_study"]["era_sha256"],
                )
                with at(kickoff + timedelta(minutes=1)):
                    cell = trial.report(root)["horizons"]["T-60m"]
                    self.assertEqual(
                        cell["qualified_news_pairs"], int(variant == "retained")
                    )
        for projection in projections.values():
            self.assertEqual(projection, projections["retained"])

    def test_both_slopes_have_distinct_book_families_and_tenfold_simultaneous_intervals(
        self,
    ):
        rows = []
        movements = [(0.04, 0.03), (-0.02, -0.012), (0.01, 0.025), (-0.04, -0.03)]
        for rnd, (x, y) in enumerate(movements, 1):
            outcomes = {
                "home_win": news_market._measure(0.4, 0.4 + x, 0.4, 0.4 + y),
                "total_over_2_5": news_market._measure(0.55, 0.55 - x, 0.55, 0.55 + y),
            }
            rows.append(
                {
                    "season": "2026-27",
                    "round_id": rnd,
                    "implementation": "a",
                    "market_close": {"endpoints": outcomes},
                    "scheduled_market_close": {"reason": "no_native_batch"},
                }
            )
        home = trial._cell(rows, "market_close", "home_win", True)
        total = trial._cell(rows, "market_close", "full_over_2_5", True)
        self.assertAlmostEqual(home["slope"], -total["slope"])
        self.assertEqual(total["interval"]["family_comparisons"], 10)
        self.assertAlmostEqual(
            total["interval"]["critical_t"], t.ppf(1 - 0.025 / 10, 3)
        )
        self.assertIsNone(
            trial._cell(rows, "market_close", "full_over_2_5", False)["slope"]
        )
        self.assertEqual(
            trial._cell(rows, "scheduled_market_close", "full_over_2_5", True)[
                "missing"
            ],
            {"no_native_batch": 4},
        )
        for row in rows:
            row.update(
                study_status="declared", study_plan_sha256=news_study_plan.PLAN_SHA256
            )
        legacy = news_market.regression(rows)
        self.assertEqual(legacy["interval"]["family_comparisons"], 6)
        self.assertAlmostEqual(home["slope"], legacy["slope"])
        self.assertGreater(
            total["interval"]["critical_t"], legacy["interval"]["critical_t"]
        )

    def test_native_news_originals_and_fixed_window_revisions(self):
        root, history = self.root / "runtime", self.root / "history"
        kickoff = store.instant(KICKOFF)
        cutoff = kickoff - timedelta(minutes=61)
        when = store.instant("2026-08-12T12:25:00Z")
        with fixed_sqlite_clock(cutoff):
            with at("2026-08-01T10:00Z"):
                seed_fixture(root, history)
                trial.enroll(root)
                home, away = (add_club_feed(root, team) for team in (HOME, AWAY))
            feeds = {
                home["url"]: rss(home, HOME_OUT),
                away["url"]: rss(away, AWAY_RUMOUR),
            }
            seal = collect_native_seal(root, history, feeds, when, cutoff)
            self.assertTrue(seal["news_pair"]["changed"])
            with at(kickoff + timedelta(days=1)):
                before = trial.report(root)
                self.assertEqual(before["bound_seals"], 1)
                self.assertEqual(
                    before["initial_bound_probability_era"],
                    seal["news_study"]["era_sha256"],
                )
                cell = before["horizons"]["T-60m"]
                self.assertEqual(cell["qualified_news_pairs"], 1, cell["exclusions"])
                for field in trial.CLOCKS:
                    total = cell["clocks"][field]["full_over_2_5"]
                    self.assertEqual(total["matched_books"], 0)
                    self.assertEqual(sum(total["missing"].values()), 1)
                fact = next(
                    f
                    for f in seal["context"]["player_news"]["facts"]
                    if f["event"] == "injury"
                )
                original = Path(blob_path(root, fact["original_sha256"]))
                saved = original.read_bytes()
                original.unlink()
                self.assertFalse(
                    news.snapshot(root, FID, seal["input_cutoff"])["coverage"][
                        "both_teams_observed"
                    ]
                )
                rejected = trial.report(root)["horizons"]["T-60m"]
                self.assertEqual(rejected["qualified_news_pairs"], 0)
                self.assertEqual(
                    rejected["exclusions"][
                        "news_originals_do_not_match_sealed_snapshot"
                    ],
                    1,
                )
                original.write_bytes(saved)
                self.assertEqual(trial.report(root), before)
                with closing(store.connect(root)) as db:
                    roster_documents = [
                        json.loads(r[0])
                        for r in db.execute("SELECT document FROM news_roster_content")
                    ]
                    source_inputs = list(
                        db.execute(
                            "SELECT receipt,response_sha FROM news_endpoint_poll_originals"
                        )
                    )
                self.assertEqual(len(source_inputs), 2)
                dependencies = {
                    sha
                    for doc in roster_documents
                    for sha in [doc["squad_sha256"], *doc["identity_sha256"]]
                }
                dependencies.update(row["response_sha"] for row in source_inputs)
                for sha in dependencies:
                    with self.subTest(dependency=sha):
                        path = blob_path(root, sha)
                        payload = path.read_bytes()
                        path.unlink()
                        try:
                            degraded = trial.report(root)["horizons"]["T-60m"]
                            self.assertEqual(degraded["qualified_news_pairs"], 0)
                            self.assertEqual(
                                degraded["exclusions"][
                                    "missing_or_invalid_news_dependency_original"
                                ],
                                1,
                            )
                        finally:
                            path.write_bytes(payload)
                for poll in source_inputs:
                    with (
                        self.subTest(receipt=poll["receipt"]),
                        closing(store.connect(root)) as db,
                    ):
                        receipt = poll["receipt"]
                        (raw,) = db.execute(
                            "SELECT payload FROM news_poll_proofs WHERE id=?",
                            (receipt,),
                        ).fetchone()
                        with db:
                            db.execute(
                                "UPDATE news_poll_proofs SET payload='{}' WHERE id=?",
                                (receipt,),
                            )
                        self.assertEqual(
                            trial.report(root)["horizons"]["T-60m"][
                                "qualified_news_pairs"
                            ],
                            0,
                        )
                        with db:
                            db.execute(
                                "UPDATE news_poll_proofs SET payload=? WHERE id=?",
                                (raw, receipt),
                            )
                self.assertEqual(trial.report(root), before)
                # An alternative provenance with the same probabilities and both
                # teams covered still must not replace the sealed identities.
                substitute = news.snapshot(root, FID, seal["input_cutoff"])
                substitute["facts"][0]["original_sha256"] = "f" * 64
                with patch.object(news, "snapshot", return_value=substitute):
                    other = trial.report(root)["horizons"]["T-60m"]
                    self.assertEqual(
                        other["exclusions"][
                            "news_originals_do_not_match_sealed_snapshot"
                        ],
                        1,
                    )

            def provider_documents(score):
                game = match(101, HOME, AWAY, score) | {"matchDateTimeUTC": KICKOFF}
                raw = json.dumps([game]).encode()
                base = result_feed.BASE
                return {
                    f"{base}/getmatchdata/pl/2026": raw,
                    f"{base}/getmatchdata/pl/2026/1": raw,
                    f"{base}/getlastchangedate/pl/2026/1": json.dumps(
                        f"final-{score[0]}-{score[1]}"
                    ).encode(),
                }

            feeds = provider_documents([0, 2])
            with (
                patch.object(result_feed, "_get", side_effect=feeds.__getitem__),
                patch.object(
                    result_feed, "now_utc", side_effect=lambda: store.now_utc()
                ),
            ):
                with at("2026-08-13T10:00Z"):
                    result_feed.sync_primary(root, 2026)
                with at("2026-08-13T10:41Z"):
                    result_feed.sync_primary(root, 2026, round_id=1)
                    self.assertEqual(
                        result_admission.verified_regulation(root, FID)["away_goals"], 2
                    )
                with at(trial.END):
                    frozen = trial.report(root)
                    self.assertEqual(
                        frozen["horizons"]["T-60m"]["score_guardrail"]["pairs"], 1
                    )
                    feeds.update(provider_documents([3, 1]))
                    result_feed.sync_primary(root, 2026, round_id=1)
                    self.assertIsNone(result_admission.verified_regulation(root, FID))
                    self.assertEqual(trial.report(root), frozen)
                with at("2028-07-02T10:41Z"):
                    result_feed.sync_primary(root, 2026, round_id=1)
                    self.assertEqual(
                        result_admission.verified_regulation(root, FID)["home_goals"], 3
                    )
                    self.assertEqual(trial.report(root), frozen)

            with at("2028-07-02T12:00Z"):
                store.register_fixture(
                    root,
                    {
                        "fixture_id": FID,
                        "season": "2026-27",
                        "home": HOME,
                        "away": AWAY,
                        "round_id": 1,
                        "known_at": store.now_utc(),
                        "kickoff": store.iso(kickoff + timedelta(minutes=5)),
                        "source": "calendar",
                        "locator": "late:reschedule",
                    },
                )
                self.assertEqual(trial.report(root), frozen)

    def test_empty_licensed_responses_require_each_scoped_parsing_receipt(self):
        root, history = self.root / "runtime", self.root / "history"
        kickoff = store.instant(KICKOFF)
        cutoff = kickoff - timedelta(minutes=61)
        when = store.instant("2026-08-12T12:25:00Z")
        with fixed_sqlite_clock(cutoff):
            with at("2026-08-01T10:00Z"):
                seed_fixture(root, history)
                trial.enroll(root)
                home, away = (add_club_feed(root, team) for team in (HOME, AWAY))
            for source in (home, away):
                self.assertTrue(news_endpoint_originals.retainable(source))
                self.assertFalse(
                    news_endpoint_originals.retainable(
                        source | {"license": "title and original link only"}
                    )
                )
                self.assertFalse(
                    news_endpoint_originals.retainable(source | {"rights": ["fetch"]})
                )
            self.assertFalse(
                news_endpoint_originals.retainable(
                    news_sources.default_sources(HOME, "2026-27")[0]
                )
            )
            feeds = {
                s[
                    "url"
                ]: f'<rss version="2.0"><channel><title>{s["team"]}</title></channel></rss>'.encode()
                for s in (home, away)
            }
            seal = collect_native_seal(root, history, feeds, when, cutoff)
            self.assertEqual(seal["context"]["player_news"]["facts"], [])
            with at(kickoff + timedelta(minutes=1)), closing(store.connect(root)) as db:

                def eligible():
                    return trial.report(root)["horizons"]["T-60m"][
                        "qualified_news_pairs"
                    ]

                self.assertEqual(eligible(), 1)
                rows = db.execute(
                    "SELECT * FROM news_endpoint_poll_originals ORDER BY source"
                ).fetchall()
                self.assertEqual(len(rows), 2)
                for row in rows:
                    changed = json.loads(row["document"])
                    changed["parsed_sha256"] = "f" * 64
                    with db:
                        db.execute(
                            "UPDATE news_endpoint_poll_originals SET document=?,digest=? WHERE receipt=?",
                            (
                                store.canonical(changed),
                                store.digest(changed),
                                row["receipt"],
                            ),
                        )
                    self.assertEqual(eligible(), 0)
                    with db:
                        db.execute(
                            "UPDATE news_endpoint_poll_originals SET document=?,digest=? WHERE receipt=?",
                            (row["document"], row["digest"], row["receipt"]),
                        )
                    self.assertEqual(eligible(), 1)

    def test_result_versions_dispositions_and_backdated_publication_are_read_as_received(
        self,
    ):
        before, after = "2028-06-29T12:00:00Z", "2028-07-02T12:00:00Z"
        known = store.iso("2026-08-01T10:00:00Z")
        with at(known):
            store.register_fixture(
                self.root,
                {
                    "fixture_id": FID,
                    "season": "2026-27",
                    "home": HOME,
                    "away": AWAY,
                    "round_id": 1,
                    "known_at": known,
                    "kickoff": KICKOFF,
                    "source": "calendar",
                    "locator": "fixture:1",
                },
            )
        for home_width, publication, recording in (
            (1, "2026-08-13T12:00Z", before),
            (3, "2026-08-14T12:00Z", after),
        ):
            with closing(store.connect(self.root)) as db, db:
                store.record_result_db(
                    db,
                    {
                        "fixture_id": FID,
                        "home_goals": home_width,
                        "away_goals": 1,
                        "finalized_at": "2026-08-12T16:00Z",
                        "published_at": publication,
                        "received_at": publication,
                        "source": "operator",
                        "locator": "report:1",
                    },
                    recorded_at=store.iso(recording),
                )
        with closing(store.connect(self.root)) as db, db:
            db.execute(
                "INSERT INTO result_dispositions VALUES(?,?,?,?,?)",
                (FID, store.iso(KICKOFF), store.iso(after), 1, "late_dispute"),
            )
            (latest,) = store.eligible_fixture_rows(db, season="2026-27")
            trial._scope_fixture_history(
                db, store.iso(store.instant(trial.END) - timedelta(microseconds=1))
            )
            (old,) = store.eligible_fixture_rows(db, season="2026-27")
            self.assertEqual(
                (old["result_version"], old["home_goals"], old["result_disposition"]),
                (1, 1, None),
            )
            self.assertEqual(
                (
                    latest["result_version"],
                    latest["home_goals"],
                    latest["result_disposition"],
                ),
                (2, None, "late_dispute"),
            )
        # A subsequently ingested, backdated fixture is absent from the old denominator.
        with at(after):
            store.register_fixture(
                self.root,
                {
                    "fixture_id": "epl/2026-27/else/away",
                    "season": "2026-27",
                    "home": "Else",
                    "away": "Away",
                    "round_id": 1,
                    "known_at": known,
                    "kickoff": KICKOFF,
                    "source": "calendar",
                    "locator": "fixture:late",
                },
            )
        with closing(store.connect(self.root)) as db:
            self.assertEqual(len(store.eligible_fixture_rows(db, season="2026-27")), 2)
            trial._scope_fixture_history(
                db, store.iso(store.instant(trial.END) - timedelta(microseconds=1))
            )
            self.assertEqual(len(store.eligible_fixture_rows(db, season="2026-27")), 1)

    def test_post_end_conflicting_first_whistle_leaves_old_witness_available(self):
        kickoff = store.instant(KICKOFF)
        cut = kickoff - timedelta(hours=1)
        with at(cut - timedelta(days=1)):
            fixture = store.register_fixture(
                self.root,
                {
                    "fixture_id": FID,
                    "season": "2026-27",
                    "home": HOME,
                    "away": AWAY,
                    "round_id": 1,
                    "known_at": store.now_utc(),
                    "kickoff": KICKOFF,
                    "source": "calendar",
                    "locator": "fixture:1",
                },
            )
        with patch.dict(kickoff_proof.OPEN_RELEASES, clear=True):
            with at(kickoff + timedelta(days=1)):
                original = capture_example(
                    self.root, fixture, 17, store.iso(kickoff + timedelta(seconds=7))
                )
            with at("2028-07-02T00:00Z"):
                with self.assertRaisesRegex(ValueError, "conflicting"):
                    capture_example(
                        self.root,
                        fixture,
                        18,
                        store.iso(kickoff + timedelta(seconds=8)),
                    )
                with self.assertRaisesRegex(ValueError, "conflicting"):
                    kickoff_proof.resolve(
                        self.root, FID, kickoff, cut, prospective=True
                    )
                limited = kickoff_proof.resolve(
                    self.root,
                    FID,
                    kickoff,
                    cut,
                    prospective=True,
                    received_through=trial.END,
                )
                self.assertEqual(limited["proof_id"], original["proof_id"])

    def test_pending_estimates_prior_to_reveal_and_late_first_whistle_or_result(self):
        with sqlite3.connect(":memory:") as db:
            db.execute(
                "CREATE TABLE football_kickoff_receipts(proof_id,fixture_id,received_at)"
            )
            db.execute(
                "INSERT INTO football_kickoff_receipts VALUES('p','epl/x',?)",
                (trial.END,),
            )
            current = {
                "fixture_id": "epl/x",
                "scored": True,
                "result_confirmed_at": trial.END,
                "market_close": {
                    "kickoff_proof": "p",
                    "endpoints": {"home_win": {"close": 0.5}},
                },
                "scheduled_market_close": {"reason": "missing_pregame_source"},
            }
            gated = trial._receipts_before_end(db, current)
            self.assertFalse(gated["scored"])
            self.assertEqual(
                gated["market_close"]["reason"],
                "actual_kickoff_proof_missing_by_study_end",
            )
            self.assertEqual(
                gated["scheduled_market_close"], current["scheduled_market_close"]
            )
            self.assertTrue(current["scored"])
        values = [
            {
                "scored": True,
                "season": "2026-27",
                "round_id": r,
                "scores": {
                    "no_news": {"score": 71},
                    "with_news": {"score": 69 + 0.01 * r},
                },
            }
            for r in range(1, 6)
        ]
        self.assertIsNone(trial._guardrail(values, False)["interval"])
        end = trial._guardrail(values, True)
        self.assertEqual(end["interval"]["family_comparisons"], 10)
        self.assertEqual(end["status"], "evidence_of_score_deterioration")
