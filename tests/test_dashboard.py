"""Portable dashboard export, escaped report content and loopback HTTP entry."""

import base64
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from polymarket861.dashboard import export_dashboard, make_server, render_dashboard
from polymarket861.store import save_report


class DashboardHTML(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.nav = []
        self.data = []
        self.resources = []
        self.scripts = []
        self.policy = None
        self.active_script = None
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "a" and "data-page" in attr:
            self.nav.append(attr["data-page"])
        if tag == "meta" and attr.get("http-equiv") == "Content-Security-Policy":
            self.policy = attr["content"]
        if tag in {"link", "img", "script"} and ("src" in attr or "href" in attr):
            self.resources.append(attr)
        if tag == "script":
            self.active_script = attr.get("type", "javascript")
            self.scripts.append(self.active_script)

    def handle_endtag(self, tag):
        if tag == "script":
            self.active_script = None

    def handle_data(self, data):
        if self.active_script == "application/json":
            self.data.append(data)
        elif self.active_script == "javascript":
            self.code = data

    def rows(self):
        return json.loads("".join(self.data))


class DashboardTests(unittest.TestCase):
    def test_embedded_json_is_inert_and_assets_offline(self):
        literal = "队名 </script> <em>测试</em> &\u2028\u2029"
        doc = render_dashboard([{
            "kind": "match", "key": literal, "updated_at": "2024-01-01T12:00:00Z",
            "payload": {"home": literal, "away": "另一队"},
        }])
        parsed = DashboardHTML(doc)
        self.assertEqual(parsed.scripts, ["application/json", "javascript"])
        self.assertEqual(parsed.rows()[0]["payload"]["home"], literal)
        self.assertEqual(parsed.resources, [])
        self.assertEqual(len(set(parsed.nav)), 6)
        self.assertEqual(parsed.nav[0], "matches")
        self.assertNotIn("<em>测试</em>", doc)
        digest = base64.b64encode(hashlib.sha256(parsed.code.encode()).digest()).decode()
        self.assertIn(f"sha256-{digest}", parsed.policy)

    def test_links_stay_stable_when_new_reports_arrive(self):
        row = {"kind": "source", "key": "a/b <2>", "payload": {"value": 0}}
        other = {**row, "kind": "incident", "updated_at": "2026-11-01T00:00:00Z"}
        one = DashboardHTML(render_dashboard([row])).rows()[0]
        many = DashboardHTML(render_dashboard([row, other])).rows()
        self.assertEqual(one["id"], next(r["id"] for r in many if r["kind"] == "source"))
        self.assertEqual(len(set(r["id"] for r in many)), 2)

    def test_export_and_server_read_the_same_store(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp) / "data"
            save_report(data_dir, "round", "2025-r1", {"mean_score": 0, "scored": 1})
            output = Path(temp) / "export" / "dashboard.html"
            result = export_dashboard(data_dir, output)
            self.assertEqual(result["reports"], 1)
            expected = output.read_bytes()
            with make_server(data_dir, 0) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    self.assertEqual(server.server_address[0], "127.0.0.1")
                    url = f"http://127.0.0.1:{server.server_port}/"
                    with urlopen(url, timeout=4) as response:
                        self.assertEqual(response.read(), expected)
                        self.assertEqual(response.headers["Cache-Control"], "no-store")
                    save_report(data_dir, "jobs", "latest", {"jobs": []})
                    with urlopen(url, timeout=4) as response:
                        self.assertEqual(len(DashboardHTML(response.read().decode()).rows()), 2)
                    with urlopen(Request(url, method="HEAD"), timeout=4) as response:
                        self.assertEqual(response.read(), b"")
                        self.assertGreater(int(response.headers["Content-Length"]), 0)
                    for request, code in [
                        (url + "reports.html", 404),
                        (Request(url, headers={"Host": "remote.example"}), 403),
                    ]:
                        with self.assertRaises(HTTPError) as error:
                            urlopen(request, timeout=4)
                        self.assertEqual(error.exception.code, code)
                finally:
                    server.shutdown()
                    thread.join(4)

    def test_empty_store_does_not_invent_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            out = directory / "empty.html"
            self.assertEqual(export_dashboard(directory, out)["reports"], 0)
            self.assertEqual(DashboardHTML(out.read_text()).rows(), [])
            with self.assertRaises(ValueError):
                export_dashboard(directory, directory / "runtime.sqlite3")


class DashboardBrowserTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("PM861_TEST_CHROMIUM"), "optional Chromium browser smoke test")
    def test_cross_season_news_report_distinguishes_measured_and_missing_price_pairs(self):
        from playwright.sync_api import sync_playwright

        row = {"kind": "news-accumulation", "key": "news-seasons", "payload": {
            "first_season": "2026-27", "through_season": "2027-28", "horizon": "T-60m",
            "scheduled": 760, "eligible_seals": 3, "eras": {"a" * 64: {
                "score": {"pairs": 3, "mean": 0.1}, "home_close": {"pairs": 0, "mean": None},
            }},
        }}
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            path = Path(tmp) / "news.html"
            path.write_text(render_dashboard([row]), encoding="utf-8")
            browser = playwright.chromium.launch(executable_path=os.environ["PM861_TEST_CHROMIUM"])
            try:
                page = browser.new_page()
                page.goto(path.as_uri())
                page.locator("[data-page=rounds]").click()
                detail = page.locator("#detail")
                self.assertIn("主胜收盘配对", detail.inner_text())
                cells = detail.locator("tbody tr").first.locator("td").all_text_contents()
                self.assertEqual(cells, ["a" * 12, "3", "0.1", "0", "—"])
            finally:
                browser.close()

    @unittest.skipUnless(os.environ.get("PM861_TEST_CHROMIUM"), "optional Chromium browser smoke test")
    def test_decision_scope_filters_links_and_mobile_layout(self):
        from playwright.sync_api import sync_playwright

        # Two reports for one fixture must show only orders frozen at that specific cutoff.
        common = {"fixture_id": "match", "season": "2025-26", "round": 1, "home": "主队甲", "away": "客队乙",
                  "model_version": "B1", "probabilities": {"home_win": .4, "draw": .3, "away_win": .3}}
        early = {**common, "cutoff": "2025-08-01T12:00:00Z", "horizon": "T-24h"}
        late = {**common, "cutoff": "2025-08-02T11:00:00Z", "horizon": "T-60m"}
        order = {"match_id": "match", "selection": "yes", "decision_at": late["cutoff"],
                 "model_version": "B1", "status": "settled", "unit_cost": ".25", "pnl": "1"}
        rows = [
            {"kind": "match", "key": "early", "payload": early},
            {"kind": "match", "key": "late", "payload": late},
            {"kind": "ledger", "key": "account", "payload": {"account": "验证账户", "intents": [order]}},
            {"kind": "strategy", "key": "budget-trial", "payload": {
                "trial": "budget-trial", "budget": "50", "qualified": False,
                "history": {"counts": {"paired_fixtures": 0}}, "forward": {},
            }},
            {"kind": "round", "key": "week", "payload": {
                "round_id": 1, "season": "2025-26", "score": 80, "full_round_range": [72, 82],
                "fixtures": [{"fixture_id": "match", "goals": [2, 0], "status": "scored", "score": 80}],
            }},
        ]
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            path = Path(tmp) / "dashboard.html"
            path.write_text(render_dashboard(rows), encoding="utf-8")
            browser = playwright.chromium.launch(executable_path=os.environ["PM861_TEST_CHROMIUM"])
            try:
                page = browser.new_page(viewport={"width": 1200, "height": 820})
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(path.as_uri())
                page.locator("#search").fill("early")
                self.assertIn("此预测时点尚无匹配", page.locator("#detail").inner_text())
                page.locator("#search").fill("late")
                page.locator("#detail a", has_text="account").wait_for()
                self.assertIn("0.25", page.locator("#detail").inner_text())
                page.locator("#detail a", has_text="account").click()
                page.locator("#detail h2", has_text="验证账户").wait_for()
                page.locator("[data-page=rounds]").click()
                page.locator("#detail h2", has_text="第 1 轮").wait_for()
                self.assertIn("72 至 82", page.locator("#detail").inner_text())
                self.assertIn("2∶0", page.locator("#detail").inner_text())
                page.locator("#detail summary").click()
                page.locator("#detail pre").wait_for()
                page.locator("[data-page=experiments]").click()
                page.locator("#detail h2", has_text="budget-trial").wait_for()
                self.assertIn("等待样本", page.locator("#detail").inner_text())
                page.set_viewport_size({"width": 375, "height": 790})
                for name in ("matches", "sources", "rounds", "incidents", "experiments", "jobs"):
                    page.locator(f"[data-page={name}]").click()
                    self.assertEqual(page.locator("[aria-current=page]").count(), 1)
                    self.assertLessEqual(page.locator("html").evaluate("el => el.scrollWidth"), 375)
                self.assertEqual(errors, [])
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
