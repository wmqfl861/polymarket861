"""Probe B — what happens to already collected team news when a placeholder kickoff is corrected?

news_articles, news_checks and the event chain are keyed by (fixture_id, kickoff), and
news.snapshot() selects `a.fixture_id=? AND a.kickoff=?` for the schedule version known at
the cutoff. The 0.4.0 live evidence was collected while all ten round-12 fixtures still carried
the OpenFootball placeholder 2026-11-28T15:00Z; the repository's own schedule report gives
2026-11-27T20:00Z for Nottingham Forest v Chelsea.

Sequence replayed here (offline, synthetic player names):
  1. fixture registered at the placeholder kickoff;
  2. first collection: Touchline availability (two listed Forest players) + one official club
     RSS item ("ruled out of the match against Chelsea");
  3. the schedule is corrected to the real kickoff (a new fixture version);
  4. snapshot immediately after the correction;
  5. the next regular poll 20 minutes later (new availability snapshot, unchanged RSS feed);
  6. snapshot again.

Usage: PYTHONPATH=src python probe_kickoff_correction.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from polymarket861 import football_model, news, news_model, news_roster, news_sources, store

import probe_common as pc

FIXTURE = "epl/2026-27/nottingham-forest/chelsea"
HOME, AWAY = "Nottingham Forest FC", "Chelsea FC"
PLACEHOLDER, REAL = "2026-11-28T15:00:00Z", "2026-11-27T20:00:00Z"
FIRST = "Aldo Bram Cato Dario Emil Faro Gino Hugo Ivo Jano Kofi Luca Milo Nico Otto Pavel Quin Rafa Sami Tomas Ugo Vito Wim Xavi Yann".split()
LAST = "Ashdown Birch Calder Denholm Eastwick Farrow Garside Holloway Ingram Jessop Kirkby Lambert Mossop Norcross Oakes Pym Quarmby Rudd Skelton Thwaite Unsworth Varley Whitlock Yardley Zelley".split()
FEED = {
    "id": "forest-official", "kind": "rss", "url": "https://forest.example/club.xml",
    "team": HOME, "free": True, "authority": "official", "license": "operator-grant",
    "license_url": "https://forest.example/grant", "attribution": "Club staff",
    "rights": list(news_sources.RIGHTS),
}


def squad(offset, suffix=""):
    roles = list(pc.squad("x").values())
    return {f"{FIRST[i]} {LAST[(i + offset) % 25]}{suffix}": roles[i] for i in range(25)}


def baseline(cutoff, kickoff):
    """A B1-shaped baseline using the rates recovered from the published example seal."""
    rates = {"home": 1.410293491, "away": 1.400116882, "rho": -0.053203538}
    grid, tail = football_model._goals_matrix(rates["home"], rates["away"], rates["rho"])
    lines = (0.5, 1.5, 2.5, 3.5, 4.5)
    probs, totals = football_model.probabilities(grid, lines)
    return {"model": "B1", "fixture_id": FIXTURE, "kickoff": store.iso(kickoff), "cutoff": cutoff,
            "rates": rates, "probabilities": probs, "totals": totals, "tail_bound": tail}


def view(root, clock, kickoff, label):
    cut = clock()
    snap = news.snapshot(root, FIXTURE, cut)
    _, pair = news_model.paired_prediction(baseline(cut, kickoff), snap["facts"], home=HOME, away=AWAY,
                                           cutoff=cut, coverage=snap["coverage"])
    return {
        "step": label, "at": cut,
        "facts": [(f["player_name"], f["source"], f["status"], f["availability"]) for f in snap["facts"]],
        "home_observed": snap["coverage"]["home"]["observed"],
        "pair_status": pair["status"],
        "applied": [(a["player_name"], a["origin"], a["prior_weight"]) for a in pair["applied"]],
        "excluded": pair["excluded_reasons"],
        "home_log_shift": round(pair["log_goal_shifts"]["home"], 6),
        "home_win_no_news": round(pair["no_news"]["probabilities"]["home_win"], 6),
        "home_win_with_news": round(pair["with_news"]["probabilities"]["home_win"], 6),
    }


def main():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "state"
        clock = pc.Clock("2026-11-25T12:00:00Z")
        with patch.object(store, "now_utc", side_effect=clock):
            pc.register(root, clock, FIXTURE, HOME, AWAY, PLACEHOLDER)
            forest, chelsea = squad(0), squad(7, "son")
            for club, members, base in ((HOME, forest, 5000), (AWAY, chelsea, 6000)):
                report = news_roster.refresh(root, club, pc.SEASON, getter=pc.FakeWiki(clock, members, base))
                assert report["status"] in ("updated", "ok"), report
            names = list(forest)
            official_player = names[24]          # a forward
            wire_players = [(201, names[11], "injured", "injury", 0),      # midfielder
                            (202, names[5], "suspended", "suspension", None)]  # defender
            rss = f"""<rss><channel>
              <item><guid>team-news-1</guid><link>https://forest.example/news/1</link><title>Team news</title>
              <pubDate>Wed, 25 Nov 2026 10:05:00 GMT</pubDate>
              <description>{official_player} has been ruled out of the match against Chelsea with a hamstring injury.</description></item>
              </channel></rss>""".encode()
            news.add_feed(root, FEED)
            wire = news_sources.default_sources(HOME, pc.SEASON)[0]

            def poller(src, team, season, cursor):
                at = clock()
                if src["id"] == news_sources.WIRE:
                    body = pc.availability_payload("Nott'm Forest", wire_players, at)
                else:
                    body = rss
                return news_sources.poll(src, team, season, cursor, getter=lambda url: pc.reply(body, at, url))

            def collect():
                out = news.sync(root, team=HOME, limit=8, defaults=lambda t, s: [wire], poller=poller,
                                roster_refresh=lambda *_: {"status": "cached"})
                return {k: out[k] for k in ("state", "attempted", "articles_added", "claims_added")}

            steps = []
            clock.advance(minutes=1)
            steps.append({"collect_1": collect()})
            clock.advance(minutes=1)
            steps.append(view(root, clock, PLACEHOLDER, "after first collection, placeholder kickoff"))
            clock.advance(minutes=5)
            pc.register(root, clock, FIXTURE, HOME, AWAY, REAL, source="probe-correction")
            clock.advance(seconds=5)
            steps.append(view(root, clock, REAL, "immediately after the kickoff correction"))
            clock.advance(minutes=21)
            steps.append({"collect_2": collect()})
            clock.advance(minutes=1)
            steps.append(view(root, clock, REAL, "after the next regular poll"))
            clock.advance(hours=3)
            steps.append({"collect_3": collect()})
            clock.advance(minutes=1)
            steps.append(view(root, clock, REAL, "three hours later"))
            from contextlib import closing
            with closing(store.connect(root)) as db:
                bound = [dict(r) for r in db.execute(
                    "SELECT source,kickoff,count(*) AS versions FROM news_articles GROUP BY source,kickoff ORDER BY source,kickoff")]
        print(json.dumps({"steps": steps, "articles_by_source_and_kickoff": bound}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
