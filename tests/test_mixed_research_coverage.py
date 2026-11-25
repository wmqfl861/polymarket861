"""A verified formal opportunity and the opposing token's independent historical evidence."""

import unittest
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR
from urllib.parse import urlencode
from unittest.mock import patch

import test_opportunity as fixture
from kickoff_fixture import capture_example
from polymarket861 import archive_proof, kickoff_proof, ledger, market_archive, market_provider, opportunity, store


class MixedResearchCoverage(unittest.TestCase):
    def setUp(self):
        case = fixture.Opportunities()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.root = case.root
        release = patch.dict(kickoff_proof.OPEN_RELEASES)
        release.start()
        self.addCleanup(release.stop)
        seal = case.seal()
        p = Decimal(str(seal["probabilities"]["total_over_2_5"]))
        price = (p - Decimal(".04")).quantize(Decimal(".01"), rounding=ROUND_FLOOR)
        case.book(fixture.CUT-timedelta(seconds=7), price)
        with fixture.at(fixture.CUT-timedelta(seconds=5)):
            opportunity.process(self.root, fixture.SLUG)
        case.book(fixture.CUT+timedelta(seconds=6), price)
        with fixture.at(fixture.CUT+timedelta(seconds=9)):
            opportunity.process(self.root, fixture.SLUG)
        self.observed = fixture.KICK+timedelta(hours=3)
        with fixture.at(self.observed):
            fx = store.fixture_at(self.root, fixture.FID)
            capture_example(self.root, fx, "98765", fixture.KICK)

    def history(self, kind):
        stamp = int((fixture.CUT-timedelta(minutes=6)).timestamp())
        raw = {"price":0.42, "timestamp":stamp}
        if kind == "prices":
            raw["resolution_seconds"] = 60
            selector = "722"
            options = {"start":stamp-60, "end":int(fixture.KICK.timestamp())}
        else:
            raw.update(token_id="722", condition_id=fixture.CONDITION, size=5, transaction_hash="test-transaction")
            selector, options = "101", {}
        def fetch(url, params):
            return fixture.reply({"data":[raw], "pagination":{"next_cursor":None,"has_more":False}},
                                 self.observed, url + "?" + urlencode(params))
        with patch.object(market_provider, "get", side_effect=fetch):
            market_archive.history(self.root,kind,selector,pages=1,**options)

    def test_prices_and_trades_survive_the_alias_rewrite(self):
        with fixture.at(self.observed):
            for kind, expected in (("prices","C"),("trades","B")):
                self.history(kind)
                plain = market_archive.replay_inputs(self.root,[fixture.SLUG],account="no-formal-decisions")
                mixed = market_archive.replay_inputs(self.root,[fixture.SLUG],account="main")
                for rows, a_count, local_id in ((plain,0,fixture.SLUG),(mixed,1,fixture.FID)):
                    self.assertEqual(len(rows),5)
                    row = next(op for op in rows if op.get("token_id") == "722")
                    self.assertEqual((row["match_id"],row["market_fixture"]),(local_id,fixture.SLUG))
                    result = ledger.replay(self.root,rows)
                    counts = result["counts"]
                    self.assertEqual((counts["expected"],counts["A"],counts[expected],counts["missing"]),
                                     (5,a_count,1,4-a_count))
                    self.assertEqual(counts["filled"],a_count)

    def test_wrong_local_match_market_alias_or_receipt_cannot_inherit_coverage(self):
        with fixture.at(self.observed):
            self.history("prices")
            rows = market_archive.replay_inputs(self.root,[fixture.SLUG],account="main")
            row = next(op for op in rows if op.get("token_id") == "722")
            with closing(store.connect(self.root)) as db:
                for changes in (
                    {"match_id":"unrelated"},
                    {"match_id":fixture.SLUG, "market_fixture":"epl-sou-nor-2026-03-01"},
                    {"market_fixture":"epl-sou-nor-2026-03-01"},
                    {"market_recorded_at":store.iso(self.observed-timedelta(days=12))},
                    {"market_source_sha256":"a"*64},
                    {"market_slug":"other-contract"},
                    {"token_id":"711"}, {"family":"moneyline"}, {"outcome":"Over"},
                ):
                    with self.subTest(changes=changes):
                        self.assertEqual(archive_proof.research_tier(db,self.root,deepcopy(row)|changes),"missing")
                # Legacy pure-slug manifests remain eligible against their genuine provider receipts.
                legacy = {k:v for k,v in row.items() if k not in {"market_recorded_at","market_source_sha256","market_fixture"}}
                legacy["match_id"] = fixture.SLUG
                self.assertEqual(archive_proof.research_tier(db,self.root,legacy),"C")
            fx = store.fixture_at(self.root,fixture.FID)
            store.register_fixture(self.root,dict(fx,fixture_id="duplicate-football-identity"))
            with closing(store.connect(self.root)) as db:
                self.assertEqual(archive_proof.research_tier(db,self.root,row),"missing")
