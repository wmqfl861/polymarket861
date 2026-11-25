import contextlib
import json
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlencode

import test_mixed_research_coverage as mixed
import test_opportunity as fixture
from kickoff_fixture import capture_example

from polymarket861 import ledger, store
from polymarket861 import market_archive as archive
from polymarket861 import market_provider as provider
from polymarket861 import qualification as Q


@contextlib.contextmanager
def altered(path, replacement=None):
    raw = path.read_bytes()
    if replacement is None:
        path.unlink()
    else:
        path.write_bytes(replacement)
    try:
        yield
    finally:
        path.write_bytes(raw)


class ReadinessMarketTests(unittest.TestCase):
    def setUp(self):
        self.match = mixed.MixedResearchCoverage()
        self.match.setUp()
        self.addCleanup(self.match.doCleanups)
        self.root = self.match.root
        clock = fixture.at(self.match.observed)
        clock.__enter__()
        self.addCleanup(clock.__exit__, None, None, None)
        clock = patch.object(Q, "now_utc", side_effect=store.now_utc)
        clock.start()
        self.addCleanup(clock.stop)
        raw = {
            "data": [
                {
                    "condition_id": fixture.CONDITION,
                    "token_id": "711",
                    "resolved": True,
                    "final_price": 1,
                }
            ]
        }

        def reply(url, params):
            return fixture.reply(
                raw, self.match.observed, url + "?" + urlencode(params)
            )

        with patch.object(provider, "get", side_effect=reply):
            terminal = ledger.capture_resolution(self.root, "711")
        ledger.settle(
            self.root, "main", terminal | {"scenario": "normal", "score": [3, 0]}
        )

    def independent_market(self, n, kind):
        start = fixture.KICK - timedelta(days=n)
        slug = f"epl-met-riv-{start.date().isoformat()}"
        condition = "0x" + str(n) * 64
        token = str(2000 + n)
        event = fixture.event()
        event.update(
            slug=slug, id=str(200 + n), gameId=str(3100 + n), startTime=store.iso(start)
        )
        m = event["markets"][0]
        m.update(
            slug=slug + "-total-2pt5",
            conditionId=condition,
            clobTokenIds=json.dumps([token, str(4000 + n)]),
            gameStartTime=store.iso(start),
        )

        def gamma(url, params=None):
            data = (
                event
                if url.endswith("/" + slug)
                else event | {"slug": slug + "-more-markets", "markets": []}
            )
            return fixture.reply(data, start - timedelta(days=1), url)

        with patch.object(provider, "get", side_effect=gamma):
            archive.discover(self.root, slug)

        def capture(offset):
            stamp = int((start - timedelta(minutes=offset)).timestamp())
            item = {"timestamp": stamp, "price": "0.49"}
            if kind == "trades":
                item.update(
                    token_id=token,
                    condition_id=condition,
                    size=4,
                    transaction_hash="trans-" + str(stamp),
                )
                selector = event["id"]
                opts = {}
            else:
                item["resolution_seconds"] = 60
                selector = token
                opts = {"start": stamp - 60, "end": int(start.timestamp())}

            def fetch(url, params):
                return fixture.reply(
                    {
                        "data": [item],
                        "pagination": {"next_cursor": None, "has_more": False},
                    },
                    self.match.observed,
                    url + "?" + urlencode(params),
                )

            with patch.object(provider, "get", side_effect=fetch):
                archive.history(
                    self.root, kind, selector, pages=1, restart=True, **opts
                )
            with contextlib.closing(archive.database(self.root)) as db:
                return store.blob_path(
                    self.root,
                    db.execute(
                        "SELECT sha FROM ma_history WHERE kind=? AND token=? AND timestamp=?",
                        (kind, token, store.iso(provider.moment(stamp))),
                    ).fetchone()[0],
                )

        return slug, capture(8), capture(4)

    def conflict(self):
        fx = store.fixture_at(self.root, fixture.FID)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            capture_example(self.root, fx, 98766, fixture.KICK + timedelta(seconds=2))

    def test_overlapping_scopes_count_each_fixture_once_and_keep_valid_grades(self):
        b, b_first, b_second = self.independent_market(1, "trades")
        c, c_first, c_second = self.independent_market(2, "prices")
        studies = [
            Q.study_archive(self.root, s)
            for s in ([fixture.SLUG, b], [b, c], [fixture.SLUG], [b])
        ]
        initial = Q.qualification(self.root)
        sample = initial["market_sample"]
        self.assertEqual(
            [sample["counts"][k] for k in ("expected", "A", "B", "C", "settled")],
            [15, 1, 1, 1, 1],
        )
        self.assertEqual(sample["fixture_count"], 3)
        self.assertGreater(Decimal(initial["historical_executable_roi"]), 0)
        self.conflict()
        # Direct readiness has to re-read the clock evidence even without another study.
        current = Q.qualification(self.root)
        self.assertTrue(current["checks"]["archive_linked_market_replay"])
        self.assertIsNone(current["historical_executable_roi"])
        self.assertEqual(
            [
                current["market_sample"]["counts"][k]
                for k in ("expected", "A", "B", "C", "settled")
            ],
            [15, 0, 1, 1, 0],
        )
        self.assertFalse(Q._current_market_study(self.root, studies[2]))
        Q.study_archive(self.root, [fixture.SLUG])
        self.assertEqual(
            Q.qualification(self.root)["market_sample"]["counts"],
            current["market_sample"]["counts"],
        )
        with altered(b_first), altered(c_first, b"corrupt"):
            recovered = Q.qualification(self.root)["market_sample"]["counts"]
            self.assertEqual(
                (recovered["B"], recovered["C"]), (1, 1)
            )  # Both use their later authentic originals.
            with altered(b_second):
                price_only = Q.qualification(self.root)
                self.assertTrue(price_only["checks"]["archive_linked_market_replay"])
                self.assertEqual(
                    [price_only["market_sample"]["counts"][k] for k in ("B", "C")],
                    [0, 1],
                )
                with altered(c_second):
                    empty = Q.qualification(self.root)
                    self.assertFalse(empty["checks"]["archive_linked_market_replay"])
                    self.assertIsNone(empty["historical_executable_roi"])
                    self.assertIsNone(empty["market_study"])
                    self.assertEqual(empty["market_sample"]["counts"]["expected"], 15)
                    persisted = store.list_reports(self.root, "qualification")[0][
                        "payload"
                    ]
                    store.save_report(
                        self.root,
                        "qualification",
                        "latest",
                        persisted | {"ready": True},
                    )
                    with self.assertRaisesRegex(
                        ValueError, "archive_linked_market_replay"
                    ):
                        Q.activate(
                            self.root,
                            store.iso(self.match.observed + timedelta(days=1)),
                        )
        self.assertEqual(
            ledger.ledger_report(self.root, "main")["settled_positions"], 1
        )

    def test_legacy_scope_recovers_only_an_exact_manifest_and_rechecks_the_originals(
        self,
    ):
        other, _, _ = self.independent_market(1, "trades")
        frozen = Q.study_archive(self.root, [fixture.SLUG, other])
        legacy = dict(frozen)
        legacy.pop("input_sha256")
        store.save_report(
            self.root, "trial-market-study", frozen["manifest_sha256"], legacy
        )
        with contextlib.closing(store.connect(self.root)) as db, db:
            db.execute("DELETE FROM observations WHERE source='trial-market-inputs'")
        self.conflict()
        status = Q.qualification(self.root)
        self.assertEqual(
            [
                status["market_sample"]["counts"][k]
                for k in ("expected", "A", "B", "settled")
            ],
            [10, 0, 1, 0],
        )
        self.assertIsNone(status["historical_executable_roi"])
        self.assertTrue(status["checks"]["archive_linked_market_replay"])
        self.assertEqual(status["unverified_market_studies"], [])
        with contextlib.closing(store.connect(self.root)) as db:
            bound = db.execute(
                "SELECT sha256 FROM observations WHERE source='trial-market-inputs'"
            ).fetchone()
        self.assertEqual(bound[0], frozen["input_sha256"])

    def test_manifest_integrity_cached_projection_and_originals_on_the_same_fixture(
        self,
    ):
        self.match.history("prices")
        s = Q.study_archive(self.root, [fixture.SLUG])
        self.assertEqual((s["counts"]["A"], s["counts"]["C"]), (1, 1))
        fake = s | {"historical_roi": "999999"}
        store.save_report(self.root, "trial-market-study", s["manifest_sha256"], fake)
        self.assertFalse(Q._current_market_study(self.root, fake))
        self.assertEqual(
            Q.qualification(self.root)["historical_executable_roi"], s["historical_roi"]
        )
        with contextlib.closing(archive.database(self.root)) as db:
            page = db.execute(
                "SELECT sha FROM ma_history WHERE kind='prices' AND token='722'"
            ).fetchone()[0]
        with altered(store.blob_path(self.root, page)):
            good = Q.qualification(self.root)
            self.assertEqual(
                [good["market_sample"]["counts"][k] for k in ("A", "C", "settled")],
                [1, 0, 1],
            )
            self.assertEqual(good["historical_executable_roi"], s["historical_roi"])
        sha = s["input_sha256"]
        with altered(store.blob_path(self.root, sha), b"forged"):
            rejected = Q.qualification(self.root)
            self.assertFalse(rejected["checks"]["archive_linked_market_replay"])
            self.assertIn(s["manifest_sha256"], rejected["unverified_market_studies"])
        bad = dict(s, fixtures=["epl-foo-bar-2020-01-01"])
        store.save_report(self.root, "trial-market-study", s["manifest_sha256"], bad)
        self.assertFalse(
            Q.qualification(self.root)["checks"]["archive_linked_market_replay"]
        )
