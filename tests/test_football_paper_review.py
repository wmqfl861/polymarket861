import hashlib
import importlib.util
import json
from decimal import Decimal as D
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from polymarket861 import big5_market, five_league_budget, market_archive, store
from test_budget_domestic_archive import domestic_entry
from test_capital_native_proof import record_terminal, terminal_row


if sys.platform != "linux":
    raise unittest.SkipTest("football paper deployment uses Linux file locks")

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy/paper-automation"
PAPER = DEPLOY / "paper-10000"
FEED = DEPLOY / "five-automation/recommendation-feed"
REVIEW = ROOT / "docs/review-evidence/code-review-0.5.0a4-nba-plan-2026-11-27/outputs/probe-holdout-confidence-slope.json"


def load_module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


with patch.object(sys, "path", [str(PAPER), str(FEED), *sys.path]):
    feed = load_module(FEED / "discover.py")
    prepare = load_module(PAPER / "prepare-slate.py")
    opening = load_module(PAPER / "open-portfolio.py")
    calibration = load_module(FEED / "paper_calibration.py")
    archive = load_module(PAPER / "archive_slate_evidence.py")
    account = load_module(PAPER / "account_engine.py")
    preflight = load_module(PAPER / "check_account_upgrade.py")
    monitor = load_module(PAPER / "monitor.py")
    bindings = load_module(ROOT / "scripts/check_paper_bindings.py")

odds = feed.watch.football_odds_access


class FootballPaperReview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_denied_before_transport_across_all_three_entry_points(self):
        pick = {"league": "epl", "espn_id": "123", "kickoff": "2026-12-01T12:00:00Z"}
        with patch.object(odds.market_provider, "get") as request:
            for fn, args in [(feed.scoreboard, ("epl", "2026-12-01")), (prepare.espn, (pick,)),
                             (feed.watch.comparison_book, (pick, {}))]:
                with self.assertRaisesRegex(ValueError, "no_reviewed_free"):
                    fn(*args)
        request.assert_not_called()

    def test_old_slate_cannot_open_from_unlicensed_or_stale_evidence(self):
        at = "2026-11-27T10:00:00Z"
        pick = {"id": "entry", "league": "epl", "kickoff": "2026-11-27T12:00:00Z",
                "bookmaker": {"receipt": "2026-11-27T09:40:00Z", "permission_sha256": "approved"}}
        (self.root / "slate.json").write_text(json.dumps([pick]))
        with patch.object(opening, "ROOT", self.root):
            with self.assertRaisesRegex(ValueError, "no_reviewed_free"):
                opening.main()
        self.assertFalse((self.root / "account-input.json").exists())
        with patch.object(odds, "permission_digest", return_value="approved"):
            with self.assertRaisesRegex(ValueError, "outside pregame"):
                opening.verify_comparison_receipt(pick, at)
            pick["bookmaker"]["receipt"] = "2026-11-27T09:58:00Z"
            opening.verify_comparison_receipt(pick, at)
            pick["bookmaker"]["permission_sha256"] = "another"
            with self.assertRaisesRegex(ValueError, "current reviewed permission"):
                opening.verify_comparison_receipt(pick, at)

    def test_odds_grant_is_scoped_to_reviewed_route_digest_and_interval(self):
        digest = hashlib.sha256(b"fixture permit").hexdigest()
        (self.root / digest).write_bytes(b"fixture permit")
        grant = {("epl", "summary"): (digest, "2026-01-01T00:00:00Z", "2026-12-01T00:00:00Z")}
        with patch.object(odds, "PERMISSIONS", self.root), patch.object(odds, "REVIEWED_FREE_GRANTS", grant), \
             patch.object(odds.store, "now_utc", return_value="2026-11-27T00:00:00Z") as now, \
             patch.object(odds.market_provider, "get") as get:
            self.assertEqual(odds.fetch("epl", "summary", {"event": "22"}), (get.return_value, digest))
            get.assert_called_once()
            for league, endpoint in (("lal", "summary"), ("epl", "scoreboard")):
                with self.assertRaisesRegex(ValueError, "no_reviewed_free"):
                    odds.fetch(league, endpoint, {})
            (self.root / digest).write_bytes(b"edited")
            with self.assertRaisesRegex(ValueError, "digest_mismatch"):
                odds.fetch("epl", "summary", {})
            now.return_value = "2026-12-01T00:00:00Z"
            with self.assertRaisesRegex(ValueError, "inactive"):
                odds.fetch("epl", "summary", {})
            get.assert_called_once()

    def test_feed_without_bookmaker_does_not_publish_or_drop_old_direction(self):
        (self.root / "picks.json").write_text("[]")
        at = "2026-11-27T00:00:00.000000Z"
        f = {"league": "epl", "fixture": "fixture-1", "forecast_fixture_id": "match-1",
             "kickoff": "2026-11-28T00:00:00.000000Z"}
        pins = {"match-1": {"token_id": "old", "kickoff": f["kickoff"]}}
        (self.root / "feed.json").write_text(json.dumps({"schema_version": 1, "published_fixtures": pins,
                                                         "policy_id": "verified-football-recommendations-v2"}))
        with patch.object(feed, "PAPER", self.root), patch.object(feed, "HERE", self.root), \
             patch.object(feed, "scheduled", side_effect=lambda league, *_: [f] if league == "epl" else []), \
             patch.object(feed, "verify_model", return_value={"info": {}, "lineups": {}}), \
             patch.object(feed, "markets_for", return_value=([], False)), patch.object(feed.store, "now_utc", return_value=at), \
             patch.object(odds.market_provider, "get") as transport:
            result = feed.run(at)
        transport.assert_not_called()
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(result["published_fixtures"], pins)
        self.assertIn("verified-football-recommendations-v1", feed.PIN_POLICIES)
        self.assertEqual(set(result["coverage"]["directions_pending_prospective_evidence"]), {"home_no", "full_game_total_2.5"})
        self.assertIn("no_reviewed_free_ESPN_DraftKings_grant", str(result["coverage"]["gaps"]))

    def test_pooled_curves_are_bound_to_reviewed_artifact_and_coherent(self):
        raw = REVIEW.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), calibration.REVIEW_SHA256)
        self.assertEqual(hashlib.sha256((ROOT / "reports/five-leagues-0.5.0a2/baseline-study.json").read_bytes()).hexdigest(),
                         calibration.BASELINE_SHA256)
        fit = json.loads(raw)["calibration_slope"]
        for k, (a, b) in calibration.FITS.items():
            self.assertEqual((D(a), D(b), fit[k]["rows"]), (D(str(fit[k]["intercept"])), D(str(fit[k]["slope"])), 1745))
        arm = {"probabilities": {"home_win": ".74", "draw": ".16", "away_win": ".10"},
               "totals": {"total": {"2.5": {"over": ".345", "under": ".655"}}}}
        def result(k, side):
            return calibration.calibrated(arm, {"family": "result", "selection": k, "side": side})
        self.assertAlmostEqual(sum(result(k, "yes") for k in ("home", "draw", "away")), D(1))
        self.assertEqual(result("home", "no"), 1 - result("home", "yes"))
        self.assertLess(result("home", "no"), D(".26"))
        over = calibration.calibrated(arm, {"family": "total", "line": "2.5", "side": "over"})
        under = calibration.calibrated(arm, {"family": "total", "line": "2.5", "side": "under"})
        self.assertEqual(under, 1 - over)
        self.assertLess(under, D(".59"))
        with self.assertRaisesRegex(ValueError, "unsupported"):
            calibration.calibrated(arm, {"family": "total", "line": "3.5", "side": "under"})

    def test_new_directions_require_prospective_evidence_and_keep_external_price_ceiling(self):
        at = "2026-11-27T00:00:00Z"
        base = {"probabilities": {"home_win": ".35", "draw": ".20", "away_win": ".45"}}
        totals = {"family": "total", "line": "2.50", "recorded_at": at,
                  "tokens": [{"token_id": "over"}, {"token_id": "under"}]}
        home = {"family": "moneyline", "side": "home", "recorded_at": at, "tokens": [{"token_id": "no"}]}
        away = {"family": "moneyline", "side": "away", "recorded_at": at, "tokens": [{"token_id": "yes"}]}
        groups = {"total": {"line": "2.5", "probabilities": {}, "prices": {}},
                  "moneyline": {"probabilities": {"away": D(".30")}, "prices": {}}}

        def terms(_, market, token, *args):
            result = ({"family": "total", "line": market["line"]} if market["family"] == "total"
                      else {"family": "result", "selection": market["side"]})
            return dict(result, side=token, special={"canceled": "pending"})

        def candidates(markets):
            return feed.possibilities({}, {"with": base, "without": base}, {"groups": groups}, markets)

        with patch.object(feed.shadow_market, "_fee", return_value={"enabled": False}), \
             patch.object(feed.ledger_terms, "_terms_with_fee", side_effect=terms):
            with self.assertRaisesRegex(ValueError, "full_game_total_2.5,home_no"):
                candidates([totals, home])
            row, = candidates([totals, home, away])
        self.assertEqual(row["market"], away)
        self.assertGreater(row["robust"], D(".33"))
        self.assertEqual(feed.best_limit(row, D(20), D(".01")), D(".30"))

    def test_manual_opener_filters_authenticated_terms_before_books_and_preserves_held_positions(self):
        pick = {"id": "candidate", "token_id": "1", "condition_id": "c"}
        (self.root / "slate.json").write_text(json.dumps([pick, {"id": "existing"}]))
        path = self.root / "account-input.json"
        path.write_text(json.dumps({"intents": [{"id": "existing"}]}))
        original = path.read_bytes()
        with patch.object(opening, "ROOT", self.root), patch.object(opening, "verify_comparison_receipt") as odds_check, \
             patch.object(opening.proof, "verify_contract", return_value={"intent_terms": {"selection": {
                 "family": "result", "selection": "home", "side": "no"}}}) as contract, \
             patch.object(opening, "books_for") as books:
            opening.main()
        self.assertEqual(odds_check.call_count, 1)
        self.assertEqual(contract.call_count, 1)
        books.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.root / "picks.json").exists())

    def test_private_archive_retries_and_detects_changes(self):
        source = self.root / "input"
        source.mkdir()
        for filename in ("markets.json", "forecasts.json", "extra"):
            (source / filename).write_text(filename)
        dest = self.root / "private" / "archive"
        archive.archive(source, dest)
        archive.archive(source, dest)
        self.assertEqual(set(archive.verify(dest)), {"markets.json", "forecasts.json", "extra"})
        argv = ["archive_slate_evidence.py", "--source", str(self.root / "missing"),
                "--destination", str(dest), "--archive-or-verify"]
        with patch.object(sys, "argv", argv), patch("builtins.print"):
            archive.main()
        with patch.object(sys, "argv", [*argv[:4], str(self.root / "no_archive"), argv[-1]]), patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "missing regular"):
                archive.main()
        (source / "extra").write_text("changed")
        with self.assertRaisesRegex(ValueError, "differs"):
            archive.archive(source, dest)
        (dest / "extra").write_text("altered copy")
        with self.assertRaisesRegex(ValueError, "manifest"):
            archive.verify(dest)

    def test_account_replay_requires_local_receipts_before_charging_shared_cash(self):
        doc = five_league_budget.stress_scenario()
        op = doc["intents"][0]
        doc.update(account_policy=account.POLICY, intents=[op], redemptions=[])
        with self.assertRaisesRegex(ValueError, "authenticated"):
            account.replay(doc)
        doc["evidence_mode"] = "local_receipts"
        report = account.replay(doc, as_of="2099-01-01T00:00:00Z", data_dir=self.root)
        self.assertEqual(report["decisions"][0]["outcome"], "skipped_unverified_entry")
        self.assertEqual(D(report["total_available_cash"]), account.FUND)
        self.assertEqual(D(report["total_unredeemed_cost"]), 0)

    def settled_fixture(self):
        doc, (main, _) = domestic_entry(self.root, "bun", explicit=True)
        doc["account_policy"] = account.POLICY
        op = doc["intents"][0]
        op["sizing"] = {"model_fair": ".55", "news_fair": ".55", "external_fair": ".52",
                        "sizing_bankroll": "10000", "kelly_fraction": ".25", "budget": "100"}
        for book in doc["books"]:
            book["bids"] = [{"price": "0.49", "size": "200"}]
        native = big5_market.research_root(self.root)
        market = market_archive.contracts(native, main["slug"], as_of=op["decision_at"])[0]
        at = store.iso(store.instant(op["kickoff"]) + timedelta(hours=3))
        for i, payout in enumerate((1, 0)):
            row = terminal_row(market, i, payout)
            sha = record_terminal(native, row["token_id"], json.dumps({"data": [row]}).encode(), at)
            if row["token_id"] == op["token_id"]:
                doc["redemptions"] = [{"id": "payment", "at": at, "original_sha256": sha,
                                       **{k: op[k] for k in ("fixture_id", "condition_id", "token_id")}}]
        return doc, at

    def test_recorded_fill_redeems_once_and_modified_bids_do_not_spend(self):
        doc, at = self.settled_fixture()
        report = account.replay(doc, as_of=doc["intents"][0]["kickoff"], data_dir=self.root)
        self.assertEqual(report["decisions"][0]["outcome"], "filled", report["proof_gaps"])
        self.assertEqual(D(report["total_unredeemed_cost"]), D(50))
        settled = account.replay(doc, as_of=at, data_dir=self.root)
        self.assertEqual(D(settled["total_settled_paper_pnl"]), D(50))
        self.assertEqual(D(settled["total_available_cash"]), D(10050))
        self.assertEqual(settled, account.replay(doc, as_of=at, data_dir=self.root))
        doc["books"][0]["bids"][0]["price"] = ".48"
        rejected = account.replay(doc, as_of=at, data_dir=self.root)
        self.assertEqual(rejected["decisions"][0]["outcome"], "skipped_unverified_entry")
        self.assertIn("submitted bids", rejected["decisions"][0]["reason"])
        self.assertEqual(D(rejected["total_available_cash"]), account.FUND)

    def test_readonly_upgrade_preflight_checks_recorded_fill_cash_and_each_redemption(self):
        doc, at = self.settled_fixture()
        intent, = doc["intents"]
        events = doc["redemptions"]
        engine = (PAPER / "account_engine.py").read_bytes()
        (self.root / "account_engine.py").write_bytes(engine)
        ledger = self.root / "account-input.json"
        ledger.write_text(json.dumps(dict(doc, redemptions=[])))
        status = account.replay(doc, as_of=at, data_dir=self.root)
        position = monitor.render_position(intent, status["decisions"][0], {},
                                           {"state": "settled", "event": events[0], "payout": "1"})
        status.update(state="ok", account_input_sha256=hashlib.sha256(ledger.read_bytes()).hexdigest(),
                      positions=[position])
        (self.root / "account-status.json").write_text(json.dumps(status))
        journal = self.root / "redemptions.json"
        journal.write_text(json.dumps({"as_of": at, "active_events": events}))

        def checksums():
            return {p.relative_to(self.root): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.root.rglob("*") if p.is_file()}

        def check(candidate, ids):
            with tempfile.TemporaryDirectory() as tmp:
                return preflight.compare(self.root, candidate, Path(tmp), required_positions=ids)

        source = checksums()
        ids = {intent["id"]}
        result = check(PAPER / "account_engine.py", ids)
        self.assertEqual((result["state"], result["positions_checked"], result["redemptions_checked"]), ("match", 1, 1))
        self.assertEqual(checksums(), source)
        with self.assertRaisesRegex(ValueError, "original positions missing"):
            check(PAPER / "account_engine.py", preflight.ORIGINAL_POSITIONS)
        with tempfile.TemporaryDirectory() as folder:
            candidate = Path(folder) / "changed.py"
            candidate.write_bytes(engine + b"""
original = replay
def replay(*a, **kw):
    result = original(*a, **kw)
    result["decisions"][0]["fill"]["net_shares"] = "99"
    return result
""")
            changed = check(candidate, ids)
        self.assertEqual(changed["state"], "mismatch")
        self.assertEqual(changed["candidate"]["position_differences"], [intent["id"]])
        self.assertEqual(changed["deployed"]["position_differences"], [])
        self.assertEqual(checksums(), source)
        position["redemption_cash"] = "0"
        (self.root / "account-status.json").write_text(json.dumps(status))
        self.assertEqual(check(PAPER / "account_engine.py", ids)["state"], "mismatch")
        journal.write_text(json.dumps({"as_of": at, "active_events": []}))
        with self.assertRaisesRegex(ValueError, "differs from journal"):
            check(PAPER / "account_engine.py", ids)

    def test_deployment_references_and_pinned_interpreters(self):
        self.assertIn(("polymarket861.ledger_rules", "_renamed"),
                      bindings.references("from polymarket861 import ledger_rules as r\nf = r._renamed"))
        self.assertEqual(bindings.main(), 0)
        units = DEPLOY / "systemd/system"
        def exe(name):
            return next(line.split()[0] for line in (units / name).read_text().splitlines() if line.startswith("ExecStart="))
        self.assertEqual(exe("pm861-paper-10000.service"), exe("pm861-recommendation-feed.service"))
