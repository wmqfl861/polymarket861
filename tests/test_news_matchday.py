"""An offline EPL matchday. Only source bytes and the wall clock are synthetic."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from polymarket861 import (
    dashboard, datasets, football, news, news_ai, news_events, news_roster, news_sources,
    paired_proof, pipeline, result_admission, result_feed, season, store,
)
from polymarket861.market_provider import Reply
from test_dashboard import DashboardHTML
from test_news_roster import Wiki
from test_paired_proof import at, season_data
from test_result_feed import match


YEAR, SEASON = 2026, "2026-27"
HOME, AWAY = "Arsenal FC", "Chelsea FC"
FID = football.fixture_id(SEASON, HOME, AWAY)
KICKOFF = "2026-08-12T14:00:00Z"
PUBLISHED = "2026-08-12T12:05:00Z"
CORRECTED = "2026-08-12T12:50:00Z"
HOME_OUT = "Morgan Smith is ruled out through injury for Arsenal FC."
HOME_BACK = "Morgan Smith is available for selection for Arsenal FC."
AWAY_RUMOUR = "There are rumours that Leo James could leave Chelsea FC to join Aston Villa. The report is unconfirmed."


def seed_fixture(root, history):
    blobs = {}
    for year in range(YEAR - 3, YEAR + 1):
        body = season_data(year, finished=year < YEAR)
        for game in body["matches"]:
            for side in ("team1", "team2"):
                game[side] = {"Club 00": HOME, "Club 19": AWAY}.get(game[side], game[side])
        season_id = f"{year}-{(year + 1) % 100:02d}"
        path = history / season_id / "en.1.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(body), encoding="utf-8")
        blobs[season_id] = football.blob_sha(path.read_bytes())
    revision = "b" * 40
    paired_proof.attest_provider_files(root, revision, blobs)
    datasets.observe(root, history, revision=revision, expected=blobs)
    store.register_fixture(root, {
        "fixture_id": FID, "season": SEASON, "home": HOME, "away": AWAY,
        "round_id": 1, "kickoff": KICKOFF, "known_at": store.now_utc(),
        "source": "fixture schedule", "locator": "synthetic://league/1",
    })
    pipeline.prepare_builtin(root)


def add_club_feed(root, club):
    slug = football.team_key(club)
    host = f"https://{slug}.example"
    return news.add_feed(root, {
        "id": slug + "-bulletins", "kind": "rss", "url": host + "/rss",
        "team": club, "authority": "official", "license": "CC BY 4.0",
        "license_url": host + "/test-content-license", "attribution": club + " test newsroom",
        "free": True, "rights": ["fetch", "store", "extract", "model"],
    })


def rss(config, story=None, updated=PUBLISHED):
    channel = ET.Element("channel")
    if story:
        item = ET.SubElement(channel, "item")
        fields = {
            "guid": "bulletin-1", "link": config["url"].removesuffix("rss") + "team-news",
            "title": "Team news", "description": story, "pubDate": PUBLISHED,
            "{http://www.w3.org/2005/Atom}updated": updated,
        }
        for tag, value in fields.items():
            ET.SubElement(item, tag).text = value
    doc = ET.Element("rss", version="2.0")
    doc.append(channel)
    return ET.tostring(doc, encoding="utf-8")


def roster_transport(team):
    wire = Wiki(store.instant("2026-08-01T09:00Z"))
    name, offset = ("Morgan Smith", 100) if team == HOME else ("Leo James", 200)
    wire.members = [name, *(f"{football.team_key(team).title()} Member{i}" for i in range(10))]
    wire.identities = {who: offset + i for i, who in enumerate(wire.members)}

    def get(url):
        doc = json.loads(wire(url))
        for row in doc["query"]["pages"]:
            if row["title"] == name:
                row["categories"] = [{"ns": 14, "title": "Category:Men's association football forwards"}]
        return json.dumps(doc).encode()

    return get


class MatchdayNewsTests(unittest.TestCase):
    def test_licensed_bulletin_to_sealed_markets_verified_result_and_round(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "app"
        history = Path(tmp.name) / "football"
        with at("2026-08-01T10:00Z"):
            seed_fixture(root, history)
            home, away = (add_club_feed(root, team) for team in (HOME, AWAY))
        feed_bytes = {f["url"]: rss(f) for f in (home, away)}
        squads = {team: roster_transport(team) for team in (HOME, AWAY)}
        real_refresh = news_roster.refresh

        def get_feed(url):
            if url not in feed_bytes:
                # Built-ins are still scheduled. Their isolated failure cannot hide a licensed club feed.
                raise ConnectionError("unavailable external publisher in this offline matchday")
            now = store.now_utc()
            return Reply(feed_bytes[url], now, now, {}, url)

        def collect(when):
            streams = []
            with at(when):
                for _ in range(2):  # the coordinator takes four of these eight streams per tick
                    service = season._service(root, "news", store.instant(when), {"football_root": history})
                    streams.extend(service["streams"])
                    updates = service["prediction_updates"]
                    self.assertIn(updates["state"], ("ok", "idle"), updates)
                    self.assertEqual(updates["deferred"], 0)
            good = {s["source"]: s for s in streams if s["state"] == "ok"}
            self.assertEqual(set(good), {home["id"], away["id"]}, streams)
            self.assertTrue(all(s["roster_players"] == 11 for s in good.values()))
            return good

        env = {"PM861_NEWS_LLM_URL": "", "PM861_NEWS_LLM_MODEL": ""}
        with patch.dict(os.environ, env), patch.object(news_sources, "_get", side_effect=get_feed), patch.object(
            news_roster, "refresh", side_effect=lambda r, t, s: real_refresh(r, t, s, getter=squads[t])
        ):
            collect("2026-08-11T13:25Z")
            self.assertEqual(news_events.list_updates(root, FID), [])
            with at("2026-08-11T13:59Z"):
                early = pipeline.make_forecast(root, FID, history, horizon="T-24h")
            self.assertTrue(early["news_pair"]["coverage"]["both_teams_observed"])
            self.assertFalse(early["news_pair"]["changed"])
            self.assertEqual(early["context"]["player_news"]["facts"], [])

            feed_bytes[home["url"]] = rss(home, HOME_OUT)
            feed_bytes[away["url"]] = rss(away, AWAY_RUMOUR)
            streams = collect("2026-08-12T12:25Z")
            self.assertTrue(all(s["analysis_backends"] == {"bundled_nb": 1} for s in streams.values()))
            first_events = news_events.list_updates(root, FID)
            last_event = first_events[0]
            self.assertEqual(first_events[-1]["trigger"], "first_report")
            self.assertEqual(last_event["news_pair"]["applied"][0]["player_name"], "Morgan Smith")
            self.assertEqual(last_event["news_pair"]["excluded"][0]["status"], "rumour")
            self.assertTrue(last_event["news_pair"]["changed"])
            with at("2026-08-12T12:59Z"):
                main = pipeline.make_forecast(root, FID, history, horizon="T-60m")
                trial = pipeline.make_forecast(root, FID, history, horizon="T-60m", version="B3")
            snapshot = main["context"]["player_news"]
            injury, rumour = (next(f for f in snapshot["facts"] if f["team"] == club) for club in (HOME, AWAY))
            self.assertEqual((injury["player_name"], injury["position"], injury["event"], injury["status"], injury["availability"]),
                             ("Morgan Smith", "forward", "injury", "confirmed", "out"))
            self.assertEqual((rumour["player_name"], rumour["event"], rumour["status"]), ("Leo James", "transfer", "rumour"))
            self.assertEqual(injury["model_version"], news_ai.MODEL_VERSION)
            self.assertEqual(injury["response_sha256"], hashlib.sha256(feed_bytes[home["url"]]).hexdigest())
            original = news.article(root, injury["original_sha256"])
            self.assertEqual(original["text"][injury["start"]:injury["end"]], injury["quote"])
            self.assertEqual(original["license"], "CC BY 4.0")
            pair = main["news_pair"]
            self.assertEqual(pair, trial["news_pair"])
            self.assertEqual([f["id"] for f in pair["applied"]], [injury["id"]])
            self.assertEqual([f["id"] for f in pair["excluded"]], [rumour["id"]])
            self.assertEqual(trial["model"], "B3-news")
            for field in ("probabilities", "totals"):
                self.assertEqual(main[field], pair["no_news"][field])
                self.assertEqual(trial[field], pair["with_news"][field])
            self.assertLess(trial["probabilities"]["home_win"], main["probabilities"]["home_win"])
            for family in ("total", "home", "away"):
                with self.subTest(family=family):
                    base, updated = (p["totals"][family]["2.5"] for p in (main, trial))
                    self.assertNotAlmostEqual(updated["over"], base["over"], places=6)
                    self.assertAlmostEqual(updated["over"] + updated["under"], 1)

            # The publisher backdates its correction to 12:50; our first receipt is after T−60.
            feed_bytes[home["url"]] = rss(home, HOME_BACK, CORRECTED)
            corrected_at = "2026-08-12T13:16Z"
            collect(corrected_at)
            latest, *unmodified = news_events.list_updates(root, FID)
            self.assertEqual(unmodified, first_events)
            self.assertEqual((latest["trigger"], latest["previous_digest"], latest["cutoff"]),
                             ("news_changed", last_event["digest"], store.iso(corrected_at)))
            self.assertFalse(latest["news_pair"]["changed"])
            self.assertEqual(latest["news_pair"]["with_news"], latest["news_pair"]["no_news"])
            given = news.as_of(root, FID, corrected_at)
            corrected = next(f for f in given if f["team"] == HOME)
            self.assertEqual((corrected["availability"], corrected["received_at"]), ("available", store.iso(corrected_at)))
            self.assertEqual(corrected["updated_at"], store.iso(CORRECTED))
            self.assertEqual(corrected["effective_at"], store.iso(CORRECTED))
            self.assertEqual(news.snapshot(root, FID, main["input_cutoff"]), snapshot)
            with at(corrected_at):
                self.assertEqual(pipeline.make_forecast(root, FID, history, version="B3")["digest"], trial["digest"])

        # Submit matching season and matchweek provider documents 41 minutes apart to the actual verifier.
        result = match(101, HOME, AWAY, [0, 2]) | {"matchDateTimeUTC": KICKOFF}
        raw = json.dumps([result]).encode()
        endpoints = {f"{result_feed.BASE}/getmatchdata/pl/{YEAR}": raw,
                     f"{result_feed.BASE}/getmatchdata/pl/{YEAR}/1": raw,
                     f"{result_feed.BASE}/getlastchangedate/pl/{YEAR}/1": b'"round-1-final"'}
        with patch.object(result_feed, "_get", side_effect=endpoints.__getitem__), patch.object(
            result_feed, "now_utc", side_effect=lambda: store.now_utc()
        ):
            with at("2026-08-13T10:00Z"):
                first = result_feed.sync_primary(root, YEAR)["regulation"]
                self.assertEqual(first["available_results"], 0)
                self.assertIsNone(result_admission.verified_regulation(root, FID))
            with at("2026-08-13T10:41Z"):
                closed = result_feed.sync_primary(root, YEAR, round_id=1)["regulation"]
                self.assertEqual(closed["available_results"], 1)
                verified = result_admission.verified_regulation(root, FID)
                self.assertEqual((verified["home_goals"], verified["away_goals"]), (0, 2))
        self.assertEqual(len(closed["rounds"]), 1)
        path = Path(tmp.name) / "dashboard.html"
        dashboard.export_dashboard(root, path)
        rows = DashboardHTML(path.read_text(encoding="utf-8")).rows()
        match_row = next(r for r in rows if r["kind"] == "match" and r["payload"].get("model") == "B3-news")
        round_row = next(r for r in rows if r["kind"] == "round" and r["payload"]["horizon"] == "T-60m")
        study = round_row["payload"]["news_study"]
        self.assertEqual((study["sealed"], study["scored"], study["changed"], study["observed_both"], study["expected"]), (1, 1, 1, 1, 10))
        self.assertEqual(study["fixtures"][0]["seal"], main["digest"])
        self.assertEqual(study["fixtures"][0]["goals"], [0, 2])
        self.assertEqual(study["verdict"], "insufficient_evidence")
        self.assertEqual(len(study["lines"]), 12)
        coverage = next(r for r in rows if r["kind"] == "news-coverage")
        self.assertEqual(coverage["payload"]["archive_counts"]["news_articles"], 3)
        event = next(r for r in rows if r["kind"] == "news-update" and r["payload"]["digest"] == latest["digest"])
        if chromium := os.environ.get("PM861_TEST_CHROMIUM"):
            self.check_browser(path, chromium, match_row, round_row, coverage, event)

    def check_browser(self, path, chromium, match_row, round_row, coverage, event):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chromium)
            try:
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                for section, row, phrases in [
                    ("matches", match_row, ["球队消息与预测变化", "Morgan Smith", "Leo James", "缺阵", "传闻", "变化（百分点）", "查看逐名球员的权重依据"]),
                    ("matches", event, ["消息到达后的赛前预测", "可出场", "两组预测相同", "查看前一次新闻预测", "上次更新"]),
                    ("sources", coverage, ["球队消息采集记录", "arsenal-bulletins", "chelsea-bulletins", "待采条目", "名单未匹配", "异常行"]),
                    ("rounds", round_row, ["新闻对比赛预测有帮助吗？", "无新闻 Brier", "逐条大小球线的评分", "1 / 10", "证据不足，继续观察", "新闻调整与临近开球的盘口"]),
                ]:
                    page.goto(f"{path.as_uri()}#{section}/{row['id']}")
                    detail = page.locator("#detail")
                    detail.get_by_text(phrases[0], exact=True).wait_for(timeout=5000)
                    text = detail.inner_text()
                    for phrase in phrases:
                        self.assertIn(phrase, text)
                    if section == "matches":
                        self.assertEqual(detail.get_by_role("link", name=HOME + " test newsroom").get_attribute("href"),
                                         "https://arsenal.example/team-news")
                self.assertEqual(errors, [])
            finally:
                browser.close()
