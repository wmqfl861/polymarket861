import json
import tempfile
import unittest
from contextlib import ExitStack, closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import test_big5_market as fixture
from polymarket861 import (
    big5_market as big5,
    big5_market_schedule as schedule,
    market_archive as archive,
    market_provider as provider,
    store,
)


CUTOFF = provider.moment(fixture.DAY + "T13:00:00Z")
STRONG = {"live": False, "ended": False}


class TestDomesticReceiptGrades(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)

    def _leg(self, offset, main, sibling, *, edit=None, watch=True, direct=False):
        stamp = store.iso(CUTOFF + timedelta(seconds=offset))

        def get(url, params=None, **kw):
            reply = fixture.gamma(url, params, **kw)
            raw = json.loads(reply.body)
            if url.endswith("/events") or "/events/slug/" in url:
                for e in raw if isinstance(raw, list) else [raw]:
                    e.pop("live", None)
                    e.pop("ended", None)
                    e.update(sibling if e["slug"].endswith("-more-markets") else main)
            if edit:
                return edit(url, reply, raw)
            return provider.Reply(
                json.dumps(raw).encode(), stamp, stamp, reply.headers, reply.url
            )

        with ExitStack() as stack:
            for module in (store, big5, archive, fixture):
                stack.enter_context(patch.object(module, "now_utc", return_value=stamp))
            stack.enter_context(patch.object(provider, "get", side_effect=get))
            if direct:
                big5._refresh(big5.research_root(self.base), fixture.BASE)
            else:
                big5.catalog(self.base, ["sea"], fixture.START, fixture.END)
            if watch:
                big5.watch(
                    self.base, ["sea"], fixture.START, fixture.END, discover=False
                )

    def _report(self):
        with patch.object(
            big5, "now_utc", return_value=store.iso(CUTOFF + timedelta(minutes=2))
        ):
            return big5.weekly_report(
                self.base,
                store.iso(CUTOFF - timedelta(minutes=1)),
                store.iso(CUTOFF + timedelta(minutes=1)),
            )

    def _counts(self):
        return self._report()["by_league"]["sea"]

    def test_type_and_status_evidence(self):
        for event, grade in (
            (STRONG, "explicit_pregame"),
            ({**STRONG, "gameStatus": "Not Started"}, "explicit_pregame"),
            ({}, "schedule_only"),
            ({"live": None, "ended": None}, "schedule_only"),
            ({"ended": False}, "schedule_only"),
            ({"live": False}, "schedule_only"),
            ({"ended": 0}, "unclassified"),
            ({"live": "false"}, "unclassified"),
            ({**STRONG, "gameStatus": "unknown"}, "unclassified"),
            ({**STRONG, "cancelled": "false"}, "unclassified"),
            ({"live": True, "ended": "no"}, "not_pregame"),
            ({"ended": True}, "not_pregame"),
            ({**STRONG, "gameStatus": "HT"}, "not_pregame"),
            ({**STRONG, "status": "FT"}, "not_pregame"),
            ({**STRONG, "cancelled": True}, "not_pregame"),
            ({**STRONG, "status": "postponed"}, "not_pregame"),
        ):
            with self.subTest(event=event):
                self.assertEqual(schedule.pregame_flags(event), grade)
        self.assertTrue(schedule.has_started({"gameStatus": "in-play"}))
        self.assertFalse(schedule.has_started({"status": "postponed"}))

    def test_weakest_sibling_and_each_leg(self):
        self._leg(-10, STRONG, STRONG)
        self._leg(6, STRONG, {"live": False})
        counts = self._counts()
        self.assertEqual(
            [
                counts["fixed_windows_" + k]
                for k in ("due", "complete", "explicit_pregame", "schedule_only")
            ],
            [2, 2, 1, 1],
        )
        self.assertEqual(counts["t1_pairs_schedule_only"], 1)
        self.assertEqual(counts["t1_pairs_explicit_pregame"], 0)
        self.assertFalse((self.base / "runtime.sqlite3").exists())

    def test_malformed_flags_keep_only_the_receipt(self):
        self._leg(-10, {"live": "false"}, STRONG)
        self._leg(6, {"ended": 0}, STRONG)
        counts = self._counts()
        self.assertEqual(counts["fixed_windows_complete"], 2)
        self.assertEqual(counts["fixed_windows_unclassified"], 2)
        self.assertEqual(counts["t1_pairs_unclassified"], 1)
        self.assertEqual(counts["t1_pairs_schedule_only"], 0)
        self.assertEqual(counts["t1_pairs_explicit_pregame"], 0)

    def test_stop_before_execution_does_not_create_a_pregame_pair(self):
        self._leg(-10, {}, {})
        self._leg(6, {"live": True}, {})
        report = self._report()
        self.assertTrue(
            any(f["live"] is True for f in report["catalogue_exchange_flags"])
        )
        counts = report["by_league"]["sea"]
        self.assertEqual(counts["fixed_windows_schedule_only"], 1)
        self.assertEqual(counts.get("t1_pairs_complete", 0), 0)

    def test_legacy_run_with_opposing_status_only_counts_physical_receipts(self):
        # a1 did not recognize HT/FT when the two typed flags were absent.
        with patch.object(schedule, "stopped", return_value=False):
            self._leg(-10, {"gameStatus": "HT"}, {})
            self._leg(6, {"gameStatus": "FT"}, {})
        counts = self._counts()
        self.assertEqual(counts["fixed_windows_complete"], 2)
        self.assertEqual(counts["fixed_windows_not_pregame"], 2)
        self.assertEqual(counts["t1_pairs_not_pregame"], 1)
        self.assertEqual(counts["t1_pairs_schedule_only"], 0)
        self.assertEqual(counts["t1_pairs_explicit_pregame"], 0)

    def test_started_evidence_survives_superseding_metadata_on_either_event(self):
        parent = self.base
        for side, native in (
            ("main", {"live": True}),
            ("aux", {"ended": True}),
            ("main", {"gameStatus": "In Progress"}),
            ("aux", {"status": "FT"}),
        ):
            with self.subTest(side=side, native=native):
                self.base = parent / (side + next(iter(native)))
                self._leg(
                    -15,
                    native if side == "main" else {},
                    native if side == "aux" else {},
                    watch=False,
                )
                self._leg(-10, {}, {})
                self._leg(6, STRONG, STRONG)
                counts = self._counts()
                self.assertEqual(counts["fixed_windows_not_pregame"], 2)
                self.assertEqual(counts["t1_pairs_not_pregame"], 1)
                self.assertEqual(
                    counts["t1_pairs_schedule_only"]
                    + counts["t1_pairs_explicit_pregame"],
                    0,
                )
                # Simulate a1 with no persisted play index. Reconstruct text and typed
                # evidence from the originals even where a1 never marked the text as stopped.
                root = big5.research_root(self.base)
                with closing(big5._db(root)) as db, db:
                    db.execute("DELETE FROM big5_play_evidence")
                    db.execute("DELETE FROM big5_schedule_recovery WHERE version=2")
                    db.execute("UPDATE big5_schedule_receipts SET stopped=0")
                self.assertEqual(self._counts()["t1_pairs_not_pregame"], 1)

    def test_later_report_of_play_cannot_precede_its_receipt(self):
        self._leg(-10, {}, {})
        self._leg(6, {}, {})
        self._leg(18, {"gameStatus": "FT"}, {"ended": True}, watch=False)
        counts = self._counts()
        self.assertEqual(counts["t1_pairs_schedule_only"], 1)
        self.assertEqual(counts["fixed_windows_not_pregame"], 0)

    def test_schedule_disagreement_breaks_only_pairs_across_that_receipt(self):
        parent = self.base
        revised = {"startTime": store.iso(CUTOFF + timedelta(hours=2))}
        for side in ("main", "aux", "both"):
            for when in ("before", "between", "after"):
                with self.subTest(side=side, when=when):
                    self.base = parent / (side + when)
                    start, restored = {
                        "before": (-20, -15),
                        "between": (1, 3),
                        "after": (15, 20),
                    }[when]
                    for at in sorted((-30, -10, 6, start, restored)):
                        self._leg(
                            at,
                            revised if at == start and side in ("main", "both") else {},
                            revised if at == start and side in ("aux", "both") else {},
                            watch=at in (-10, 6),
                        )
                    counts = self._counts()
                    self.assertEqual(counts["fixed_windows_schedule_only"], 2)
                    self.assertEqual(
                        counts.get("t1_pairs_schedule_only", 0), int(when != "between")
                    )

    def test_rejected_direct_auxiliary_clock_is_preserved_and_recovered_on_a1(self):
        self._leg(-10, {}, {})
        revised = {"startTime": store.iso(CUTOFF + timedelta(hours=2))}
        self._leg(1, {}, revised, direct=True, watch=False)
        self._leg(3, {}, {}, direct=True, watch=False)
        self._leg(6, {}, {})
        self.assertEqual(self._counts().get("t1_pairs_complete", 0), 0)
        root = big5.research_root(self.base)
        with closing(big5._db(root)) as db, db:
            self.assertEqual(
                db.execute(
                    "DELETE FROM big5_schedule_receipts WHERE origin='conflicting_aux_schedule'"
                ).rowcount,
                1,
            )
            db.execute("DELETE FROM big5_schedule_recovery WHERE version=2")
        counts = self._counts()
        self.assertEqual(counts["fixed_windows_schedule_only"], 2)
        self.assertEqual(counts.get("t1_pairs_complete", 0), 0)

    def test_cross_fixture_request_log_cannot_attest_the_native_books(self):
        self._leg(-10, {}, {})
        self._leg(6, {}, {})
        root = big5.research_root(self.base)
        with closing(big5._db(root)) as db, db:
            db.execute(
                "UPDATE ma_requests SET selector=? WHERE operation='books' AND received_at>?",
                ("sea-other-fixture", store.iso(CUTOFF)),
            )
        counts = self._counts()
        self.assertEqual(counts["fixed_windows_schedule_only"], 1)
        self.assertEqual(counts["t1_pairs_complete"], 0)

    def test_untrusted_legacy_summary_reopens_each_physical_original(self):
        self._leg(-10, {}, {})
        self._leg(6, {}, {})
        root = big5.research_root(self.base)
        with closing(big5._db(root)) as db, db:
            # An a1 partial receipt had neither successful slot nor bindings.
            db.execute(
                """UPDATE big5_slots SET success=0,report='{"state":"partial"}'"""
            )
            chosen = db.execute(
                "SELECT sha FROM ma_books ORDER BY received_at DESC LIMIT 1"
            ).fetchone()[0]
            self.assertEqual(
                db.execute("SELECT count(*) FROM ma_books").fetchone()[0], 28
            )
        self.assertEqual(self._counts()["t1_pairs_schedule_only"], 1)
        path = store.blob_path(root, chosen)
        moved = path.with_name(path.name + ".backup")
        path.rename(moved)
        try:
            report = self._report()
            self.assertEqual(report["raw_missing_bodies"], 1)
            self.assertEqual(report["by_league"]["sea"]["fixed_windows_complete"], 1)
            self.assertEqual(report["by_league"]["sea"]["t1_pairs_complete"], 0)
        finally:
            moved.rename(path)
        self.assertEqual(self._counts()["t1_pairs_schedule_only"], 1)
        with closing(big5._db(root)) as db, db:
            db.execute(
                "UPDATE ma_books SET payload=json_set(payload,'$.pregame',1) WHERE sha=?",
                (chosen,),
            )
        self.assertEqual(self._counts()["fixed_windows_complete"], 1)

    def test_changed_terms_are_not_a_matched_pair(self):
        self._leg(-10, {}, {})

        def change(url, reply, raw):
            if url.endswith("/events") or "/events/slug/" in url:
                for e in raw if isinstance(raw, list) else [raw]:
                    e["markets"][0]["feeSchedule"]["rate"] = 0.04
            return provider.Reply(
                json.dumps(raw).encode(),
                reply.requested_at,
                reply.received_at,
                reply.headers,
                reply.url,
            )

        self._leg(6, {}, {}, edit=change)
        counts = self._counts()
        self.assertEqual(counts["fixed_windows_schedule_only"], 2)
        self.assertEqual(counts["t1_pairs_due"], 1)
        self.assertEqual(counts["t1_pairs_complete"], 0)

    def test_right_window_boundary_excluded_and_stale_gamma_is_unclassified(self):
        def stale(url, reply, raw):
            headers = (
                {**reply.headers, "age": "31"}
                if url.endswith("/events")
                else reply.headers
            )
            return provider.Reply(
                json.dumps(raw).encode(),
                reply.requested_at,
                reply.received_at,
                headers,
                reply.url,
            )

        def late(url, reply, raw):
            received = (
                store.iso(CUTOFF + timedelta(seconds=10))
                if url.endswith("/books")
                else reply.received_at
            )
            return provider.Reply(
                json.dumps(raw).encode(),
                reply.requested_at,
                received,
                reply.headers,
                reply.url,
            )

        self._leg(-10, STRONG, STRONG, edit=stale)
        self._leg(6, STRONG, STRONG, edit=late)
        counts = self._counts()
        self.assertEqual(counts["fixed_windows_due"], 2)
        self.assertEqual(counts["fixed_windows_complete"], 1)
        self.assertEqual(counts["fixed_windows_unclassified"], 1)
        self.assertEqual(counts["t1_pairs_complete"], 0)

    def test_metadata_received_during_native_book_changes_its_grade(self):
        def slow(url, reply, raw):
            received = (
                store.iso(CUTOFF - timedelta(seconds=7))
                if url.endswith("/books")
                else reply.received_at
            )
            return provider.Reply(
                json.dumps(raw).encode(),
                reply.requested_at,
                received,
                reply.headers,
                reply.url,
            )

        self._leg(-10, STRONG, STRONG, edit=slow)
        # The first leg was in flight. The second catalog receipt changes only the match-state flags.
        self._leg(-8, {}, STRONG, watch=False)
        self._leg(6, STRONG, STRONG)
        self.assertEqual(self._counts()["t1_pairs_schedule_only"], 1)
        # A still later catalog receiving weakened flags cannot relabel the past.
        self._leg(20, {"ended": 0}, STRONG, watch=False)
        self.assertEqual(self._counts()["fixed_windows_unclassified"], 0)
