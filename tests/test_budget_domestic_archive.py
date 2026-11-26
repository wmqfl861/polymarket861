"""The actual five-league collectors and the legacy collector retain distinct Gamma projections."""

from contextlib import closing, redirect_stdout
from datetime import timedelta
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from test_big5_market import native
from test_budget_scheduled_entry import BASIS, capture
from kickoff_fixture import capture_example
from market_terms_fixture import reply
from polymarket861 import (
    big5_market,
    budget_archive_proof,
    cli,
    five_league_budget,
    kickoff_proof,
    ledger_terms,
    market_archive,
    market_provider,
    store,
)


def domestic_entry(data_dir, league, *, explicit=False):
    root = big5_market.research_root(data_dir)
    main, aux = native(league=league), native(True, league=league)
    totals = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "reports/five-leagues-0.5/rule-template-observation.json"
        ).read_bytes()
    )
    # Follow the already reviewed per-league draw templates with real complete source wording.
    authority = totals["authority_words"][league]
    rules = (
        Path(__file__).parent / "fixtures/2026-11-27-epl-draw-rules.txt"
    ).read_text()
    rules = rules.replace("Nottingham Forest FC and Chelsea FC", "One FC and Two FC")
    rules = rules.replace("Premier League (https://www.premierleague.com/).", authority)
    rules = rules.replace(
        "upcoming Premier League game", "upcoming " + authority.split(" (")[0] + " game"
    )
    rules = rules.replace(
        "Friday, November 27, 2026 at 3:00 PM ET",
        "Saturday, January 12, 2030 at 9:00 AM ET",
    )
    rules = rules.replace("Sunday, November 29, 2026", "Monday, January 14, 2030")
    market = main["markets"][1]
    market.update(
        description=rules, feesEnabled=False, orderMinSize=1, orderPriceMinTickSize=0.01
    )
    del market["feeSchedule"]
    main["markets"] = [market]
    aux["markets"] = []
    for ev in (main, aux):
        ev["teams"][0]["alias"] = (
            "An original field intentionally outside the stored projection"
        )
        if not explicit:
            del ev["live"]
            del ev["ended"]
    kickoff = store.instant(main["startTime"])
    decision = kickoff - timedelta(hours=1)
    when = store.iso(decision - timedelta(minutes=3))

    def gamma(url, params=None):
        return reply(aux if url.endswith("-more-markets") else main, when, url)

    with patch.object(market_provider, "get", side_effect=gamma):
        result = big5_market._refresh(root, main["slug"])
    assert not result["gaps"], result
    observed = market_archive.contracts(root, main["slug"], as_of=decision)[0]
    token = observed["tokens"][0]["token_id"]
    condition = observed["condition_id"]
    terms = ledger_terms.contract_at(root, condition, token, decision)
    fixture = league + "/2029-30/21/22"
    books = []
    for ident, delta in (("a", -1), ("b", 5)):
        clock = decision + timedelta(seconds=delta)
        stamp = store.iso(clock)
        raw = {
            "asset_id": token,
            "market": condition,
            "timestamp": str(int(clock.timestamp() * 1000)),
            "bids": [{"price": "0.49", "size": "200"}],
            "asks": [{"price": "0.50", "size": "200"}],
            "tick_size": "0.01",
            "min_order_size": "1",
        }
        url = market_provider.CLOB + "/book?" + urlencode({"token_id": token})
        with patch.object(market_provider, "get", return_value=reply(raw, stamp, url)):
            book = market_archive.snapshot_book(root, observed, token)
        books.append(
            {
                "id": ident,
                "source_sha256": book["source_sha256"],
                "received_at": stamp,
                "asks": book["asks"],
                "fixture_id": fixture,
                "condition_id": condition,
                "token_id": token,
            }
        )
    intent = {
        "id": "paper",
        "fixture_id": fixture,
        "condition_id": condition,
        "token_id": token,
        "rank": 1,
        "decision_at": store.iso(decision),
        "kickoff": store.iso(kickoff),
        "decision_book": "a",
        "execution_book": "b",
        "shares": "100",
        "limit_price": "0.50",
        "rule_sha256": terms["rule_sha256"],
        "selection": {
            k: terms[k]
            for k in ("family", "selection", "team", "line", "side")
            if k in terms
        },
        "fee": terms["fee_schedule"],
    }
    return {
        "schema": five_league_budget.SCHEMA,
        "evidence_mode": "local_receipts",
        "intents": [intent],
        "books": books,
        "originals": {},
        "redemptions": [],
    }, (main, aux)


class DomesticArchive(unittest.TestCase):
    def test_later_final_original_without_a_kickoff_clock_cannot_backdate_play(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc, events = domestic_entry(root, "sea")
            main, aux = (dict(e, ended=True) for e in events)
            for e in (main, aux):
                del e["startTime"]
            when = store.iso(
                store.instant(doc["intents"][0]["kickoff"]) + timedelta(days=1)
            )

            def final(url, params=None):
                return reply(aux if url.endswith("-more-markets") else main, when, url)

            with patch.object(market_provider, "get", side_effect=final):
                result = big5_market._refresh(
                    big5_market.research_root(root), main["slug"]
                )
            self.assertFalse(result["gaps"], result)
            out = five_league_budget.replay(doc, data_dir=root, entry_basis=BASIS)
            self.assertEqual(
                out["leagues"]["sea"]["outcomes"], {"filled": 1}, out["proof_gaps"]
            )

    def test_all_five_genuine_collector_paths_and_gamma_only_cli(self):
        for code in five_league_budget.CODES:
            with self.subTest(code=code), TemporaryDirectory() as tmp:
                path = Path(tmp)
                doc, (main, _) = domestic_entry(path, code)
                file = path / "input.json"
                file.write_text(json.dumps(doc))

                def command(*args):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(
                            cli.main(["--data-dir", tmp, "paper-risk", *args]), 0
                        )
                    return json.loads(out.getvalue())

                strict = command("replay", str(file))
                self.assertEqual(
                    strict["leagues"][code]["outcomes"], {"skipped_unverified_entry": 1}
                )
                report = command(
                    "replay", str(file), "--entry-basis", BASIS, "--details"
                )
                self.assertEqual(
                    report["leagues"][code]["outcomes"],
                    {"filled": 1},
                    report["proof_gaps"],
                )
                self.assertEqual(
                    report["decisions"][0]["entry_evidence"]["basis"], BASIS
                )
                intent = doc["intents"][0]
                gamma = command(
                    "verify-contract",
                    intent["condition_id"],
                    "--as-of",
                    intent["decision_at"],
                )
                self.assertEqual(
                    (gamma["fixture"], gamma["home_club_id"], gamma["away_club_id"]),
                    (main["slug"], "21", "22"),
                )
                self.assertEqual(gamma["order_books_checked"], 0)
                self.assertEqual(
                    gamma["source_sha256"],
                    report["decisions"][0]["entry_evidence"]["books"][0][
                        "contract_source_sha256"
                    ],
                )

    def test_legacy_and_domestic_cannot_assume_each_others_indexed_team_projection(
        self,
    ):
        for domestic in (False, True):
            with self.subTest(domestic=domestic), TemporaryDirectory() as tmp:
                root = Path(tmp)
                if domestic:
                    doc, _ = domestic_entry(root, "epl", explicit=True)
                    archive = big5_market.research_root(root)
                else:
                    doc, _ = capture(root, {"live": False, "ended": False})
                    archive = root
                op = doc["intents"][0]
                normal = five_league_budget.replay(doc, data_dir=root)
                self.assertEqual(
                    normal["leagues"]["epl"]["outcomes"],
                    {"filled": 1},
                    normal["proof_gaps"],
                )
                with closing(market_archive.database(archive)) as db, db:
                    query = " WHERE condition_id=?"
                    m = json.loads(
                        db.execute(
                            "SELECT payload FROM ma_contracts" + query,
                            (op["condition_id"],),
                        ).fetchone()[0]
                    )
                    if domestic:
                        self.assertNotIn("alias", m["event_teams"][0])
                        del m["event_teams"]
                    else:
                        m["event_teams"] = [{"ordering": "home", "id": "21"}]
                    db.execute(
                        "UPDATE ma_contracts SET payload=?" + query,
                        (json.dumps(m), op["condition_id"]),
                    )
                with self.assertRaisesRegex(ValueError, "diverges"):
                    budget_archive_proof.verify_contract(
                        root, op["condition_id"], op["decision_at"]
                    )
                out = five_league_budget.replay(doc, data_dir=root, entry_basis=BASIS)
                self.assertEqual(
                    out["leagues"]["epl"]["outcomes"], {"skipped_unverified_entry": 1}
                )

    def test_successful_raw_aux_with_inconsistent_schedule_cannot_disappear_from_the_replay(
        self,
    ):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc, (main, aux) = domestic_entry(root, "lal")
            archive = big5_market.research_root(root)
            op = doc["intents"][0]
            cut = store.instant(op["decision_at"])
            late = aux | {
                "startTime": store.iso(
                    store.instant(op["kickoff"]) + timedelta(hours=1)
                )
            }
            for seconds, other in ((1, late), (3, aux)):

                def gamma(url, params=None):
                    return reply(
                        other if url.endswith("-more-markets") else main,
                        store.iso(cut + timedelta(seconds=seconds)),
                        url,
                    )

                with patch.object(market_provider, "get", side_effect=gamma):
                    captured = big5_market._refresh(archive, main["slug"])
                self.assertEqual(len(captured["gaps"]), int(seconds == 1))
            report = five_league_budget.replay(doc, data_dir=root, entry_basis=BASIS)
            self.assertEqual(
                report["leagues"]["lal"]["outcomes"], {"skipped_unverified_entry": 1}
            )
            self.assertIn(
                "Gamma schedule or fixture changed across paper entry books",
                report["proof_gaps"],
            )

    def test_nested_market_archive_checks_primary_football_store_and_competition_scope(
        self,
    ):
        for code in ("epl", "bun"):
            with (
                self.subTest(code=code),
                TemporaryDirectory() as tmp,
                patch.dict(kickoff_proof.OPEN_RELEASES, clear=True),
            ):
                root = Path(tmp)
                doc, _ = domestic_entry(root, code)
                op = doc["intents"][0]
                decision = store.instant(op["decision_at"])
                known = store.iso(decision - timedelta(days=2))
                fx = {
                    "fixture_id": "english-provider-match",
                    "home": "One FC",
                    "away": "Two FC",
                    "season": "2029-30",
                    "round_id": 1,
                    "kickoff": op["kickoff"],
                    "known_at": known,
                    "source": "original schedule",
                    "locator": "fixture",
                }
                with patch.object(store, "now_utc", return_value=known):
                    registered = store.register_fixture(root, fx)
                received = store.iso(decision + timedelta(days=1))
                with patch.object(store, "now_utc", return_value=received):
                    capture_example(
                        root, registered, 48, doc["books"][1]["received_at"]
                    )
                before = five_league_budget.replay(
                    doc, data_dir=root, entry_basis=BASIS, as_of=op["kickoff"]
                )
                self.assertEqual(
                    before["leagues"][code]["outcomes"],
                    {"filled": 1},
                    before["proof_gaps"],
                )
                now = five_league_budget.replay(doc, data_dir=root, entry_basis=BASIS)
                self.assertEqual(
                    now["leagues"][code]["outcomes"],
                    {"skipped_unverified_entry": 1} if code == "epl" else {"filled": 1},
                    now["proof_gaps"],
                )
