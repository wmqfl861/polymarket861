"""Prospective cross-mode EPL endpoints and persistence of the old experiment."""

import argparse
from contextlib import closing, contextmanager
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scipy.stats import t

from polymarket861 import football, news, news_ai, news_endpoint_study as v1
from polymarket861 import news_endpoint_v2 as v2
from polymarket861 import news_endpoint_reference
from polymarket861 import (
    news_market,
    news_roster,
    news_sources,
    news_study_plan,
    pipeline,
    store,
)
from polymarket861.market_provider import Reply
from test_news_endpoint_study import collect_native_seal, raw_seal
from test_news_matchday import AWAY, AWAY_RUMOUR, FID, HOME, HOME_OUT, KICKOFF, SEASON
from test_news_matchday import add_club_feed, roster_transport, rss, seed_fixture
from test_paired_proof import at


@contextmanager
def capture_clock(stamp=None):
    # The wall-clock literal is used to test the production anti-backdating
    # predicate. For an offline series, replay each previously scripted insert
    # at its own known physical instant.
    clock = f"'{store.iso(stamp)}'" if stamp is not None else "NEW.sealed_at"

    def adapted(trigger):
        return trigger.replace(v1._CLOCK_SQL, clock).replace(
            "julianday('now')", f"julianday({clock})"
        )

    with (
        patch.object(v1, "_TRIGGER", adapted(v1._TRIGGER)),
        patch.object(v2, "TRIGGER", adapted(v2.TRIGGER)),
    ):
        yield


def cli(root, *args):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(root))
    v1.register(parser.add_subparsers(required=True))
    parsed = parser.parse_args(["news-endpoints", *args])
    return parsed.func(parsed)


class ProspectiveNewsEndpointsV2(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "runtime"
        self.history = Path(tmp.name) / "history"

    def test_public_defaults_and_legacy_sql_are_stable(self):
        self.assertEqual(cli(self.root, "report")["state"], "not_registered")
        self.assertFalse(self.root.exists())
        proposal = cli(self.root, "plan")
        self.assertEqual(proposal["plan"]["family_comparisons"], 14)
        self.assertEqual(proposal["plan"]["modes"], ["technical", "formal"])
        self.assertEqual(proposal["plan"]["closing_clocks"], list(v2.CLOCKS))
        self.assertTrue(proposal["plan"]["planning_power"])
        self.assertEqual(
            cli(self.root, "plan", "--version", "v1")["plan_sha256"],
            "a3af7b72cf2d8bdfe901742d567f7a39416d7ed04e54a641ac63b64709fcf55d",
        )
        # Hash of the trigger actually shipped in version 0.5.0a1.
        self.assertEqual(
            hashlib.sha256(v1._TRIGGER.encode()).hexdigest(),
            "cd01d0e82775647c97c98ee1ac9fe85d60aee0849c7b5e3131cbed4c9f0fe4ce",
        )
        with at("2026-08-01T10:00Z"):
            old = cli(self.root, "register", "--version", "v1", "--mode", "technical")
            self.assertEqual(cli(self.root, "report")["registration"], old)
            self.assertEqual(cli(self.root, "register", "--mode", "technical"), old)
            self.assertEqual(
                cli(self.root, "report", "--version", "v2")["state"], "not_registered"
            )
            reg = cli(self.root, "register")
            self.assertEqual(reg["plan"], proposal["plan"])
            self.assertEqual(cli(self.root, "register", "--mode", "both"), reg)
            self.assertEqual(cli(self.root, "report")["registration"], reg)
            self.assertEqual(
                cli(self.root, "report", "--version", "v1")["registration"], old
            )
            with self.assertRaisesRegex(ValueError, "both"):
                cli(self.root, "register", "--version", "v2", "--mode", "technical")
            with self.assertRaisesRegex(ValueError, "immutable"):
                cli(self.root, "register", "--starts-at", "2026-08-04T00:00Z")
        with closing(store.connect(self.root)) as db, db:
            db.execute("DROP TRIGGER news_endpoint_v2_bind_on_insert")
        with self.assertRaisesRegex(ValueError, "trigger"):
            v2.report(self.root)
        self.assertEqual(v1.report(self.root)["registration"], old)

    def test_live_capture_does_not_attach_history_or_leak_database_rollback(self):
        stamp = store.instant("2026-12-01T12:00:00Z")
        with capture_clock(stamp):
            with closing(store.connect(self.root)) as db, db:
                raw_seal(db, "epl/2026-27/old/away", stamp)
            with at(stamp - timedelta(seconds=3)):
                with self.assertRaisesRegex(ValueError, "Start"):
                    v2.enroll(self.root, starts_at=stamp - timedelta(days=1))
                with self.assertRaisesRegex(ValueError, "Start"):
                    v2.enroll(self.root, starts_at=v1.END)
                reg = v2.enroll(self.root)
                old = v1.enroll(self.root)
            with closing(store.connect(self.root)) as db:
                with db:
                    first = raw_seal(db, "epl/2026-27/one/away", stamp)
                    second = raw_seal(
                        db, "epl/2026-27/two/away", stamp, mode="formal", era="b" * 64
                    )
                    third = raw_seal(db, "epl/2026-27/three/away", stamp, era="c" * 64)
                    for name, args in (
                        ("late", {"cutoff": stamp - timedelta(seconds=1)}),
                        ("future", {"sealed_at": stamp + timedelta(seconds=1)}),
                        ("replay", {"mode": "replay"}),
                        ("shadow", {"purpose": "shadow"}),
                        ("not_declared", {"plan": "0" * 64}),
                        ("wrong_era", {"era": "z" * 64}),
                        ("wrong_season", {"season": "2025-26"}),
                        (
                            "pre_enrollment",
                            {"input_cutoff": stamp - timedelta(hours=1)},
                        ),
                    ):
                        raw_seal(db, f"epl/2026-27/{name}/away", stamp, **args)
                    raw_seal(db, "lal/2026-27/wrong/league", stamp)
                with self.assertRaises(RuntimeError), db:
                    raw_seal(db, "epl/2026-27/reverted/away", stamp)
                    raise RuntimeError("interrupted primary transaction")
                new = db.execute(
                    "SELECT seal,declaration FROM news_endpoint_v2_bindings ORDER BY capture_order"
                )
                self.assertEqual(
                    [tuple(r) for r in new],
                    [
                        (seal, reg["registration_sha256"])
                        for seal in (first, second, third)
                    ],
                )
                original = db.execute(
                    "SELECT seal,declaration FROM news_endpoint_bindings ORDER BY capture_order"
                )
                self.assertEqual(
                    [tuple(r) for r in original],
                    [(seal, old["registration_sha256"]) for seal in (first, third)],
                )

    def test_native_modes_eras_and_version_specific_originals(self):
        kickoff = store.instant(KICKOFF)
        cutoff = kickoff - timedelta(minutes=61)
        reverse_start = kickoff + timedelta(weeks=19)
        reverse_id = football.fixture_id(SEASON, AWAY, HOME)
        with capture_clock():
            with at("2026-08-01T10:00Z"):
                seed_fixture(self.root, self.history)
                store.register_fixture(
                    self.root,
                    {
                        "fixture_id": reverse_id,
                        "season": SEASON,
                        "home": AWAY,
                        "away": HOME,
                        "round_id": 20,
                        "kickoff": store.iso(reverse_start),
                        "known_at": store.now_utc(),
                        "source": "fixture schedule",
                        "locator": "synthetic://league/20",
                    },
                )
                with closing(store.connect(self.root)) as db, db:
                    db.execute(
                        "INSERT INTO trial_activations VALUES (?,?,?,?)",
                        (store.PROTOCOL, store.now_utc(), store.now_utc(), "{}"),
                    )
                earlier = v1.enroll(self.root)
                joined = v2.enroll(self.root)
                home, away = (add_club_feed(self.root, team) for team in (HOME, AWAY))
            feeds = {
                home["url"]: rss(home, HOME_OUT),
                away["url"]: rss(away, AWAY_RUMOUR),
            }
            technical = collect_native_seal(
                self.root, self.history, feeds, kickoff - timedelta(minutes=95), cutoff
            )
            with at(cutoff + timedelta(seconds=1)):
                formal_duplicate = pipeline.make_forecast(
                    self.root, FID, self.history, mode="formal"
                )
            self.assertNotEqual(technical["digest"], formal_duplicate["digest"])
            # The reverse fixture uses the real native collector with a second extractor
            # configuration. An empty, licensed RSS response still carries this identity.
            feed_by_url = {f["url"]: f for f in (home, away)}

            def fetch(url):
                if url not in feed_by_url:
                    raise ConnectionError("outside offline publisher scope")
                return Reply(
                    rss(feed_by_url[url]), store.now_utc(), store.now_utc(), {}, url
                )

            with (
                at(reverse_start - timedelta(minutes=93)),
                patch.object(news_sources, "_get", side_effect=fetch),
            ):
                for team in (HOME, AWAY):
                    news.sync(
                        self.root,
                        team=team,
                        season=SEASON,
                        force=True,
                        roster_refresh=lambda root, team, season: news_roster.refresh(
                            root, team, season, getter=roster_transport(team)
                        ),
                        extractor=news_ai.extract,
                        workers=1,
                        defaults=lambda club, season: [],
                    )
            with at(reverse_start - timedelta(minutes=61)):
                reverse = pipeline.make_forecast(
                    self.root, reverse_id, self.history, mode="formal"
                )
            self.assertNotEqual(
                reverse["news_study"]["era_sha256"],
                technical["news_study"]["era_sha256"],
            )
            with at(reverse_start + timedelta(hours=3)):
                new = v2.report(self.root)
                legacy = v1.report(self.root)
                current = new["horizons"]["T-60m"]
                self.assertEqual(
                    (
                        new["bound_seals"],
                        current["past_scheduled_windows"],
                        current["duplicate_bindings_ignored"],
                    ),
                    (3, 2, 1),
                )
                self.assertEqual(
                    current["qualified_news_pairs"], 2, current["exclusions"]
                )
                self.assertEqual(
                    {(g["mode"], g["era"]) for g in current["strata"]},
                    {
                        ("technical", technical["news_study"]["era_sha256"]),
                        ("formal", reverse["news_study"]["era_sha256"]),
                    },
                )
                self.assertEqual(legacy["registration"], earlier)
                self.assertEqual(legacy["horizons"]["T-60m"]["qualified_news_pairs"], 1)
                # Native evaluation can fail after its recipe and ID have
                # already been checked, during physical endpoint revalidation.
                with patch.object(
                    news_endpoint_reference,
                    "observe",
                    side_effect=ValueError("invalid archived book"),
                ):
                    incomplete = v2.report(self.root)["horizons"]["T-60m"]
                self.assertEqual(incomplete["qualified_news_pairs"], 0)
                self.assertEqual(
                    incomplete["exclusions"], {"invalid_seal_or_archived_evidence": 2}
                )
                # The later, fully valid formal seal cannot replace an invalid
                # first selection. Its news and market outcome cannot decide admission.
                with closing(store.connect(self.root)) as db, db:
                    db.execute(
                        "UPDATE news_endpoint_v2_bindings SET probability_era=? WHERE seal=?",
                        ("f" * 64, technical["digest"]),
                    )
                invalid_first = v2.report(self.root)["horizons"]["T-60m"]
                self.assertEqual(invalid_first["qualified_news_pairs"], 1)
                self.assertEqual(
                    invalid_first["exclusions"], {"invalid_bound_probability_era": 1}
                )
                with closing(store.connect(self.root)) as db, db:
                    db.execute(
                        "UPDATE news_endpoint_v2_bindings SET probability_era=? WHERE seal=?",
                        (technical["news_study"]["era_sha256"], technical["digest"]),
                    )
                with closing(store.connect(self.root)) as db, db:
                    first = db.execute(
                        "SELECT * FROM news_endpoint_poll_originals ORDER BY observed_at LIMIT 1"
                    ).fetchone()
                    newer = db.execute(
                        "SELECT * FROM news_endpoint_v2_poll_originals WHERE receipt=?",
                        (first["receipt"],),
                    ).fetchone()
                    self.assertEqual(first["response_sha"], newer["response_sha"])
                    self.assertEqual(
                        json.loads(first["document"])["metadata"]["registration"],
                        earlier["registration_sha256"],
                    )
                    self.assertEqual(
                        json.loads(newer["document"])["metadata"]["registration"],
                        joined["registration_sha256"],
                    )
                    # Both versions have physical proofs for the same bytes. A
                    # valid substitute receipt under v1 still has no v2 subscription.
                    db.execute(
                        "UPDATE news_endpoint_v2_poll_originals SET document=?,digest=? WHERE receipt=?",
                        (first["document"], first["digest"], newer["receipt"]),
                    )
                partial = v2.report(self.root)["horizons"]["T-60m"]
                self.assertEqual(partial["qualified_news_pairs"], 1)
                self.assertEqual(
                    partial["exclusions"],
                    {"missing_or_invalid_news_dependency_original": 1},
                )
                self.assertEqual(v1.report(self.root), legacy)
                self.assertEqual(
                    next(s for s in partial["strata"] if s["mode"] == "technical")[
                        "excluded"
                    ],
                    1,
                )
            with at("2028-07-01T00:00:00Z"):
                ended = v2.report(self.root)
                self.assertEqual(ended["state"], "window_ended")
                estimate = ended["horizons"]["T-60m"]["clocks"]["dual_schedule_close"][
                    "home_win"
                ]
                self.assertEqual(
                    estimate["strata"][0]["slope_information"], "no_matched_books"
                )
                self.assertIsNone(estimate["interval"])

    def test_pre_subscription_polls_cannot_be_borrowed_even_for_a_new_bound_seal(self):
        kickoff = store.instant(KICKOFF)
        cutoff = kickoff - timedelta(minutes=61)
        with capture_clock(cutoff):
            with at("2026-08-01T10:00Z"):
                seed_fixture(self.root, self.history)
                v1.enroll(self.root)
                home, away = (add_club_feed(self.root, team) for team in (HOME, AWAY))
            feeds = {
                home["url"]: rss(home, HOME_OUT),
                away["url"]: rss(away, AWAY_RUMOUR),
            }
            original_make_forecast = pipeline.make_forecast

            def subscribe_then_forecast(*args, **kwargs):
                if store.now_utc() == store.iso(cutoff):
                    v2.enroll(self.root)
                return original_make_forecast(*args, **kwargs)

            with patch.object(
                pipeline, "make_forecast", side_effect=subscribe_then_forecast
            ):
                collect_native_seal(
                    self.root,
                    self.history,
                    feeds,
                    kickoff - timedelta(minutes=95),
                    cutoff,
                )
            with at(kickoff + timedelta(hours=2)):
                self.assertEqual(
                    v1.report(self.root)["horizons"]["T-60m"]["qualified_news_pairs"], 1
                )
                new = v2.report(self.root)
                self.assertEqual(new["bound_seals"], 1)
                self.assertEqual(
                    new["horizons"]["T-60m"]["exclusions"],
                    {"missing_or_invalid_news_dependency_original": 1},
                )
                with closing(store.connect(self.root)) as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT count(*) FROM news_endpoint_v2_poll_originals"
                        ).fetchone()[0],
                        0,
                    )

    def test_reverified_era_must_still_match_the_registered_policy_and_local_deadline(
        self,
    ):
        now = store.instant("2026-12-01T12:00:00Z")
        with at(now - timedelta(days=1)):
            reg = v2.enroll(self.root)
        fact = {
            "sealed": True,
            "seal": "abc",
            "implementation": "era",
            "study_status": "declared",
            "study_plan_sha256": news_study_plan.PLAN_SHA256,
            "timely_news_coverage": True,
            "input_cutoff": store.iso(now),
            "kickoff": store.iso(now + timedelta(hours=1)),
            "news_protocol": reg["news_policy"]["protocol"],
            "policy_sha256": reg["news_policy"]["sha256"],
        }
        binding = {
            "seal": "abc",
            "probability_era": "era",
            "bound_at": store.iso(now),
            "cutoff": store.iso(now),
        }
        self.assertIsNone(v2._mismatch(fact, binding, reg))
        self.assertEqual(
            v2._mismatch(fact | {"policy_sha256": "f" * 64}, binding, reg),
            "incompatible_registered_news_policy",
        )
        self.assertEqual(
            v2._mismatch(fact | {"study_plan_sha256": "e" * 64}, binding, reg),
            "incompatible_native_pair_declaration",
        )
        self.assertEqual(
            v2._mismatch(
                fact, binding | {"bound_at": store.iso(now + timedelta(seconds=1))}, reg
            ),
            "invalid_binding_time",
        )

    def test_within_mode_era_centering_round_clusters_and_delayed_reveal(self):
        rows = []
        for mode, era, offset in (
            ("technical", "era-1", 0.05),
            ("formal", "era-1", -0.03),
            ("formal", "era-2", 0.02),
            ("formal", "invariant", 0.01),
        ):
            for rnd, (x, y) in enumerate(
                ((0.04, 0.03), (-0.02, -0.012), (0.01, 0.025), (-0.04, -0.03)), 1
            ):
                if era == "invariant":
                    x = 0
                endpoints = {
                    "home_win": news_market._measure(
                        0.4, 0.4 + x + offset, 0.4, 0.4 + y - 3 * offset
                    ),
                    "total_over_2_5": news_market._measure(
                        0.55, 0.55 - x + offset, 0.55, 0.55 + y - 3 * offset
                    ),
                }
                rows.append(
                    {
                        "mode": mode,
                        "implementation": era,
                        "season": SEASON,
                        "round_id": rnd,
                        "dual_schedule_close": {"endpoints": endpoints},
                        "scored": True,
                        "scores": {
                            "with_news": {"score": 70 + rnd / 10},
                            "no_news": {"score": 72},
                        },
                    }
                )
        interim = v2._slope_cell(rows, "dual_schedule_close", "home_win", False)
        self.assertEqual(interim["matched_books"], 16)
        self.assertIsNone(interim["slope"])
        self.assertIsNone(interim["interval"])
        self.assertNotIn("minimum_detectable_slope_at_80pct", interim)
        self.assertTrue(all("informative_pairs" not in s for s in interim["strata"]))
        final = v2._slope_cell(rows, "dual_schedule_close", "home_win", True)
        total = v2._slope_cell(rows, "dual_schedule_close", "full_over_2_5", True)
        self.assertEqual(
            (final["pairs"], final["informative_pairs"], final["informative_rounds"]),
            (16, 12, 4),
        )
        self.assertEqual(final["interval"]["degrees_of_freedom"], 3)
        self.assertGreater(final["minimum_detectable_slope_at_80pct"], 0)
        self.assertAlmostEqual(
            final["interval"]["critical_t"], t.ppf(1 - 0.025 / 14, 3)
        )
        self.assertAlmostEqual(final["slope"], -total["slope"])
        first = v2._slope_cell(rows[:4], "dual_schedule_close", "home_win", True)
        self.assertAlmostEqual(final["slope"], first["slope"])
        invariant = next(s for s in final["strata"] if s["era"] == "invariant")
        self.assertEqual(invariant["slope_information"], "no_within_stratum_variation")
        self.assertIsNone(v1._guardrail(rows, False, family_size=v2.FAMILY)["interval"])
        guardrail = v1._guardrail(rows, True, family_size=v2.FAMILY)
        self.assertEqual(guardrail["interval"]["family_comparisons"], 14)
        self.assertEqual(guardrail["status"], "evidence_of_score_deterioration")
