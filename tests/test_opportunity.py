"""Synthetic event bytes pass through the same capture, model, account and replay paths as a match."""

import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from polymarket861 import (
    datasets,
    evidence,
    evidence_sources,
    football,
    input_manifest,
    kickoff_proof,
    ledger,
    opportunity,
    pipeline,
    store,
)
from polymarket861 import (
    market_archive as archive,
)
from polymarket861 import (
    market_provider as provider,
)

CHAMP = "2025-26"
HOME, AWAY = "Northbridge FC", "Southbank FC"
SLUG = "epl-nor-sou-2026-03-01"
FID = football.fixture_id(CHAMP, HOME, AWAY)
KICK = store.instant("2026-03-01T16:30:00Z")
CUT = KICK - timedelta(hours=1)
CONDITION = "0x" + "3" * 64


@contextmanager
def at(when):
    with ExitStack() as stack:
        for module in (
            store,
            pipeline,
            datasets,
            input_manifest,
            evidence,
            evidence_sources,
            archive,
        ):
            stack.enter_context(
                patch.object(module, "now_utc", return_value=store.iso(when))
            )
        yield


def event(slug=SLUG):
    # Formulaic old-format total-goal terms with invented clubs, event IDs and tokens.
    rules = (
        f"In the upcoming Premier League game between {HOME} and {AWAY}, scheduled for March 1 at 11:30 AM ET: "
        f'This market will resolve to "Over" if {HOME} and {AWAY} combine to score 3 or more goals in this game. '
        'If the combined total is less than 3, this market will resolve to "Under". '
        "If the game is postponed, this market will remain open until the game has been completed. "
        "If the game is canceled entirely, with no make-up game, this market will resolve 50–50. "
        "If the game is started but not completed, this market will resolve according to the official final score published on premierleague.com. "
        "This market refers only to the outcome within the first 90 minutes of regular play plus stoppage time."
    )
    child = slug != SLUG
    return {
        "slug": slug,
        "id": 102 if child else 101,
        "gameId": 9001,
        "startTime": store.iso(KICK),
        "live": False,
        "ended": False,
        "teams": [
            {"ordering": side, "name": name, "alias": name, "abbreviation": abbr}
            for side, name, abbr in (("home", HOME, "nor"), ("away", AWAY, "sou"))
        ],
        "markets": []
        if child
        else [
            {
                "slug": SLUG + "-total-2pt5",
                "conditionId": CONDITION,
                "sportsMarketType": "totals",
                "version": "v1",
                "clobTokenIds": '["711","722"]',
                "line": 2.5,
                "outcomes": '["Over","Under"]',
                "groupItemTitle": "O/U 2.5",
                "description": rules,
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "enableOrderBook": True,
                "secondsDelay": 3,
                "feesEnabled": False,
                "orderMinSize": "1",
                "orderPriceMinTickSize": "0.01",
            }
        ],
    }


def reply(value, when, url):
    return provider.Reply(
        json.dumps(value).encode(),
        store.iso(when - timedelta(milliseconds=100)),
        store.iso(when),
        {"date": format_datetime(when), "age": "0"},
        url,
    )


class Opportunities(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.early = CUT - timedelta(days=2)
        with at(self.early):
            fixture = {
                "fixture_id": FID,
                "season": CHAMP,
                "round_id": 26,
                "home": HOME,
                "away": AWAY,
                "source": "synthetic schedule",
                "locator": "fixture://safe",
                "kickoff": store.iso(KICK),
                "known_at": store.iso(self.early),
            }
            store.register_fixture(self.root, fixture)
            store.register_fixture(
                self.root,
                fixture
                | {
                    "fixture_id": "unrelated",
                    "home": "Eastgate FC",
                    "away": "Hilltown FC",
                },
            )
            with patch.object(
                provider,
                "get",
                side_effect=lambda url, params=None: reply(
                    event(url.rsplit("/", 1)[-1]), self.early, url
                ),
            ):
                archive.discover(self.root, SLUG)

    def prepare_inputs(self):
        raw = self.root / "inputs"
        (raw / CHAMP).mkdir(parents=True)
        games = [
            {
                "round": "Matchday 26",
                "date": "2026-03-01",
                "time": "16:30",
                "team1": HOME,
                "team2": AWAY,
                "score": {},
            },
            {
                "round": "Matchday 25",
                "date": "2026-02-19",
                "time": "14:00",
                "team1": "Previous H",
                "team2": "Previous A",
                "score": {"ft": [2, 1]},
            },
            {
                "round": "Matchday 25",
                "date": "2026-02-20",
                "time": "16:00",
                "team1": "Previous X",
                "team2": "Previous Y",
                "score": {"ft": [0, 0]},
            },
        ]
        source = raw / CHAMP / "en.1.json"
        source.write_text(json.dumps({"matches": games}))
        with at(self.early):
            data = datasets.observe(
                self.root,
                raw,
                revision="a" * 40,
                expected={CHAMP: football.blob_sha(source.read_bytes())},
            )
            with closing(store.connect(self.root)) as db, db:
                db.execute(
                    "INSERT INTO trial_activations VALUES(?,?,?,?)",
                    (
                        store.PROTOCOL,
                        store.iso(self.early),
                        store.iso(self.early),
                        "{}",
                    ),
                )
            evidence.register_source(
                self.root,
                {
                    "source_id": "fixture-owned",
                    "status": "active",
                    "free": True,
                    "upstream": "test-owned",
                    "terms_locator": "fixture://source/terms",
                    "fields": {
                        "lineup": {
                            "methods": ["manual"],
                            "uses": ["prediction", "trial"],
                        }
                    },
                },
            )
        lineup_at = CUT - timedelta(minutes=8)
        with at(lineup_at):
            evidence.import_evidence(
                self.root,
                [
                    {
                        "source_id": "fixture-owned",
                        "field": "lineup",
                        "fixture_id": FID,
                        "team": club,
                        "method": "manual",
                        "published_at": store.iso(lineup_at),
                        "received_at": store.iso(lineup_at),
                        "locator": "fixture://teams/" + club,
                        "value": {
                            "confirmed": True,
                            "starters": [club + str(i) for i in range(11)],
                        },
                    }
                    for club in (HOME, AWAY)
                ],
            )
        return data

    def seal(self):
        data = self.prepare_inputs()
        with at(CUT - timedelta(minutes=1)):
            return pipeline.make_forecast(
                self.root,
                FID,
                Path(data["fixture_root"]),
                evidence_path=Path(data["evidence_path"]),
                mode="formal",
                version="B1",
            )

    def book(self, when, price, *, spread="0.01", tick="0.01", token="711"):
        price = Decimal(str(price))
        native = {
            "asset_id": token,
            "market": CONDITION,
            "hash": store.digest([token, store.iso(when)]),
            "tick_size": tick,
            "min_order_size": "1",
            "timestamp": int(when.timestamp() * 1000),
            "bids": [{"price": str(price - Decimal(spread)), "size": "40"}],
            "asks": [{"price": str(price), "size": "40"}],
        }

        def http(url, params=None):
            return reply(native, when, url + "?" + urlencode(params))

        with at(when), patch.object(provider, "get", side_effect=http):
            return archive.snapshot_book(
                self.root,
                archive.contracts(self.root, SLUG, as_of=store.iso(when))[0],
                token,
            )

    def test_require_ordered_clubs_even_for_simultaneous_games_and_manual_locator(self):
        correct = opportunity._locate(self.root, SLUG, None, CUT)
        self.assertEqual(correct["fixture_id"], FID)
        with self.assertRaisesRegex(ValueError, "club identity"):
            opportunity.verified_fixture(self.root, SLUG, "unrelated", CUT)
        with self.assertRaisesRegex(ValueError, "timely"):
            opportunity.verified_fixture(
                self.root, SLUG, FID, self.early - timedelta(seconds=1)
            )
        with at(CUT):
            out = opportunity.process(self.root, SLUG)
            self.assertEqual(out["decisions"], [])
            self.assertTrue(out["gaps"])
        original = archive.replay_inputs(self.root, [SLUG])
        self.assertTrue(original)
        self.assertTrue(all("intent" not in op for op in original))

    def test_full_formal_entry_and_archive_replay_requires_independent_kickoff(self):
        sealed = self.seal()
        fair = Decimal(str(sealed["probabilities"]["total_over_2_5"]))
        price = (fair - Decimal("0.04")).quantize(Decimal(".01"), rounding=ROUND_FLOOR)
        original_first = self.book(CUT - timedelta(seconds=7), price)
        with at(CUT - timedelta(seconds=5)):
            placed = opportunity.process(self.root, SLUG)
            self.assertIn("frozen", [r["state"] for r in placed["decisions"]], placed)
            opportunity.process(self.root, SLUG)
        later_first = self.book(CUT - timedelta(seconds=2), price + Decimal("0.01"))
        self.book(CUT + timedelta(seconds=5.3), price, spread="0.10")
        valid = self.book(CUT + timedelta(seconds=6.4), price)
        self.book(CUT + timedelta(seconds=7.2), price)
        with at(CUT + timedelta(seconds=8)):
            execution = opportunity.process(self.root, SLUG)
            self.assertIn(
                "filled", [r["state"] for r in execution["decisions"]], execution
            )
        self.assertEqual(len(ledger.ledger_report(self.root, "main")["intents"]), 1)
        self.assertEqual(
            ledger.ledger_report(self.root, "main")["intents"][0]["filled_at"],
            valid["received_at"],
        )
        self.book(KICK - timedelta(seconds=8), price + Decimal("0.04"))
        self.book(KICK - timedelta(seconds=4), price + Decimal("0.04"), spread="0.10")
        before = archive.replay_inputs(self.root, [SLUG], account="main")
        self.assertTrue(any(op.get("archive_gap") for op in before))
        self.assertFalse(any(op.get("first_book") for op in before))
        self.assertTrue(
            any(
                x["detail"].get("token_id") == "722" and x["stage"] == "gap"
                for x in opportunity.history(self.root, SLUG)["events"]
            )
        )
        xml = (
            f'<Game id="700123" competition_id="8" competition_name="Premier League" game_date="{store.iso(KICK)}" '
            f'home_team_name="{HOME}" away_team_name="{AWAY}" home_team_id="N" away_team_id="S" '
            f'period_1_start="{store.iso(KICK)}" period_2_end="{store.iso(KICK + timedelta(hours=2))}"/>'
        ).encode()
        rights = b"Test-only synthetic event release; CC0."
        sha = lambda b: hashlib.sha256(b).hexdigest()
        timeline = self.root / "test-event.xml"
        timeline.write_bytes(xml)
        terms = self.root / "test-permission.txt"
        terms.write_bytes(rights)
        release = {
            "format": "opta-f24",
            "licence": "CC0-1.0",
            "licence_url": "https://example.invalid/test-rights",
            "source_url": "https://example.invalid/test-event",
            "source_sha256": sha(xml),
            "licence_sha256": sha(rights),
        }
        # Only the invented fixture collection is approved in this test process, never a real match or feed.
        with (
            patch.dict(kickoff_proof.OPEN_RELEASES, {"fixture-open": release}),
            at(KICK + timedelta(hours=3)),
        ):
            kickoff_proof.import_open(
                self.root,
                FID,
                "700123",
                "fixture-open",
                timeline,
                terms,
                schedule_version=1,
            )
            marked = opportunity.process(self.root, SLUG)
            self.assertTrue(
                any(r.get("clv") is not None for r in marked["decisions"]), marked
            )
            ops = archive.replay_inputs(self.root, [SLUG], account="main")
            eligible = [x for x in ops if "intent" in x]
            self.assertEqual(len(eligible), 1, ops)
            item = eligible[0]
            self.assertEqual(
                (
                    item["match_id"],
                    item["token_id"],
                    item["second_book"]["source_sha256"],
                ),
                (FID, "711", valid["source_sha256"]),
            )
            report = ledger.replay(self.root, ops)
            self.assertEqual(
                (
                    report["counts"]["A"],
                    report["counts"]["filled"],
                    report["counts"]["settled"],
                ),
                (1, 1, 0),
                report,
            )
            self.assertEqual(report["counts"]["expected"], 5)
            self.assertEqual(item["first_book"], original_first)
            rebuilt = ledger.ledger_report(self.root, report["account"])["intents"][0]
            self.assertEqual(rebuilt["sealed_at"], item["decision_sealed_at"])
            self.assertEqual(rebuilt["source_decision_sha256"], item["decision_sha256"])
            for field, value in {
                "intent_id": "unsealed",
                "model_version": "unknown",
                "forecast_digest": "0" * 64,
                "normal_fv": ".44",
                "quantity": "20",
                "budget": "12.0",
                "limit_price": ".433",
                "priority": 19,
                "lineups": {"home": {}, "away": {}},
            }.items():
                with self.subTest(field=field):
                    forged = deepcopy(item)
                    forged["intent"][field] = value
                    isolated = ledger.replay(self.root, [forged])
                    self.assertEqual(
                        (isolated["counts"]["A"], isolated["counts"]["filled"]), (0, 0)
                    )
                    self.assertEqual(
                        ledger.ledger_report(self.root, isolated["account"])["intents"],
                        [],
                    )
            for field, changed in (
                ("first_book", later_first),
                ("decision_sealed_at", store.iso(CUT)),
                ("decision_sha256", "0" * 64),
                ("intent_id", "other-formal-order"),
                ("paper_account", report["account"]),
                ("forecast_digest", "0" * 64),
            ):
                with self.subTest(receipt=field):
                    self.assertEqual(
                        ledger._evidence_tier(
                            self.root, item | {field: changed}, "historical"
                        )[0],
                        "missing",
                    )
            self.assertEqual(ledger.replay(self.root, ops)["counts"], report["counts"])


if __name__ == "__main__":
    unittest.main()
