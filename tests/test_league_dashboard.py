import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from polymarket861 import dashboard, league_dashboard, store
from polymarket861 import news_events, news_roster, news_sources


class LeagueDashboardTest(unittest.TestCase):
    def test_season_match_selects_eligible_rosters_and_scoped_wire_snapshot_and_deep_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = store.iso("2026-11-27T14:00:00Z")
            future = store.iso("2026-11-27T15:00:00Z")
            kick = store.iso("2026-11-27T20:00:00Z")
            fid = "epl/2026-27/nottingham-forest/chelsea"
            game = {"fixture_id":fid, "season":"2026-27", "league":"epl", "kickoff":kick,
                    "home":"Nottingham Forest FC","away":"Chelsea FC","latest_seal":{"digest":"match-content"}}
            report = {"source":news_sources.WIRE,"time_basis":"provider_snapshot","fixture_id":fid,
                      "team":game["away"], "event":"injury", "kickoff":kick, "status":"reported",
                      "availability":"out", "known_at":now,"published_at":now,"received_at":now,
                      "player_name":"Example Striker","chance_of_playing":0}
            invalid = [dict(report, player_name="Future Claim", known_at=future),
                       dict(report, player_name="Cup Suspension", event="suspension", suspension_competition="FA Cup"),
                       dict(report, player_name="Transfer Rumour", event="transfer")]
            with sqlite3.connect(root / "runtime.sqlite3") as db:
                db.executescript(news_roster._SCHEMA + news_events._SCHEMA)
                db.execute("CREATE TABLE reports(kind TEXT,key TEXT, PRIMARY KEY(kind,key))")
                db.execute("INSERT INTO reports VALUES('match','match-content')")
                for side in ("home","away"):
                    team = game[side]
                    names = [{"name":side + " First Team", "player_id":side}]
                    doc = {"team":team, "season":game["season"], "source":"squad", "published_at":now,
                           "source_url":"https://en.wikipedia.org/w/index.php?oldid=1", "squad_sha256":"s",
                           "identity_sha256":["i"], "revision":"1", "players":names, "license":"CC BY-SA",
                           "attribution":"Wikipedia contributors"}
                    sha = store.digest(doc)
                    db.execute("INSERT INTO news_roster_content VALUES(?,?)", (sha,store.canonical(doc)))
                    db.execute("INSERT INTO news_roster_receipts VALUES(?,?,?,?)",
                               (game["season"],news_roster._key(team),now,sha))
                for revision, stamp in ((1,now),(2,future)):
                    snapshot = {"fixture_id":fid,"cutoff":stamp,"schedule":{"kickoff":kick},"coverage":{},
                                "facts":[report,*invalid] if revision == 1 else []}
                    edition = {**game,"cutoff":stamp,"created_at":stamp,"snapshot":snapshot,"revision":revision,
                               "previous_digest":None,"state_sha256":"states"}
                    db.execute("INSERT INTO news_event_updates(fixture_id,kickoff,revision,previous_digest,digest,"
                               "state_sha256,document) VALUES(?,?,?,NULL,?,?,?)",
                               (fid,kick,revision,store.digest(edition),"states",store.canonical(edition)))
            evidence = league_dashboard._season_people(root,game,now)
            self.assertEqual(evidence["snapshot"]["cutoff"],now)
            self.assertTrue(evidence["report_link"].startswith("/#matches/"))
            home = league_dashboard._season_fields(game,evidence,"home")
            away = league_dashboard._season_fields(game,evidence,"away")
            self.assertIn("home First Team",home)
            self.assertNotIn("Example Striker",home)
            for value in ("Example Striker", "来源估计出场率 0%", "away First Team", "来源覆盖有缺项"):
                self.assertIn(value,away)
            for withheld in ("Future Claim", "Cup Suspension", "Transfer Rumour"):
                self.assertNotIn(withheld,away)

    def test_five_links_share_loopback_server_and_readonly_initial_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            epl, other = Path(tmp) / "epl", Path(tmp) / "four"
            with patch.dict(os.environ, {"PM861_FIVE_STATE_DIR": str(other)}), dashboard.make_server(epl, 0) as srv:
                worker = threading.Thread(target=srv.serve_forever, daemon=True)
                worker.start()
                link = f"http://127.0.0.1:{srv.server_port}"
                try:
                    document = urlopen(link + "/leagues", timeout=4).read().decode()
                    for title in ["英超", "西甲", "德甲", "意甲", "法甲"]:
                        self.assertIn(title, document)
                    self.assertIn("未来七天暂无已登记的可展示比赛", document)
                    self.assertFalse(other.exists())
                    self.assertFalse(epl.exists())
                    self.assertIn('href="/leagues"', urlopen(link, timeout=4).read().decode())
                    for query in ("league=other", "league=bun&league=lal", "league=lal&fixture=bun/test"):
                        with self.assertRaises(HTTPError) as exc:
                            urlopen(link + "/leagues?" + query, timeout=4)
                        self.assertEqual(exc.exception.code, 404)
                finally:
                    srv.shutdown()
                    worker.join(3)
            with patch.dict(os.environ, {"PM861_FIVE_STATE_DIR": ""}):
                self.assertNotIn('href="/leagues"', dashboard.render_dashboard([]))

    def test_match_shows_real_seal_probabilities_verified_roster_and_missing_player_fields(self):
        fixture = {"league": "bun", "season": "2026-27", "fixture_id": "bun/2026-27/a/b",
                   "home": "A <Club>", "away": "B", "kickoff": "2026-11-27T20:00:00Z",
                   "latest_seal": {"probabilities": {"home_win": .51, "draw": .26, "away_win": .23},
                                   "model": "B1", "horizon": "preview"}}
        payload = {"input_cutoff": "2026-11-27T15:00:00Z", "pair": {"changed": False}, "news": {},
                   "baseline": {"evidence": {"training_matches": 920}}, "schedule_evidence": {"slug": "a-b"}}
        forecast = {"kickoff": fixture["kickoff"], "digest": store.digest(payload), "payload": payload,
                    "sealed": "2026-11-27T15:00:01Z"}
        report = {"leagues": {"bun": {"state": "ok", "eligible_upcoming": [fixture],
                                      "fixture_dispatch": [forecast]}}}
        receipt = "2026-11-27T14:00:00Z"
        person = {"name": "Player <One>", "league_totals": {"scope": "club_career", "appearances": 126,
                                                           "goals": 31, "as_of_source_text": "21. November"}}
        roster = {"players": [person], "ready_at": receipt,
                  "evidence": [{"revision_url": "https://de.wikipedia.org/w/index.php?oldid=5", "language": "de",
                                "received_at": receipt, "published_at": "2026-11-26T10:00:00Z", "title": "Kader"}],
                  "changes": {"scope":"observed_roster_revision", "observed_at":receipt, "compared_with":receipt,
                              "added":["Player Two"], "removed":["Player Three"], "prior_evidence":[]}}
        facts = {"teams": {"home": {"roster": roster, "availability": {"items": [
            {"player": "Player <One>", "source_label": "Lesionado", "match_scope": "unknown"}]}}},
                 "press": {"teams": {"home": {"items": [
                     {"title": "报道 <1>", "url": "https://publisher.example/article"}]}}},
                 "player_status": {"injuries": {"state": "not_configured"}, "lineup": {"state": "unknown"}}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(league_dashboard.big5_scheduler, "status",
                return_value=report), patch.object(league_dashboard.big5_scheduler, "_news", return_value=facts):
            document = league_dashboard.render(Path(tmp), Path(tmp), urlencode({"league":"bun","fixture":fixture["fixture_id"]}))
        for text in ["51.0%", "26.0%", "23.0%", "920", "Player &lt;One&gt;", "报道 &lt;1&gt;", "未配置数据源",
                     "德语 · Kader", "Lesionado", "适用比赛待核实", "21. November", "126", "Player Two",
                     "Player Three", "本机收件 11-27 14:00 UTC"]:
            self.assertIn(text, document)
        self.assertNotIn("<Club>", document)
