"""Probe E — fixture-scope rules applied at extraction time (news.fixture_relevant).

Two things are shown with synthetic sentences:
  1. A sentence that names the match day ("Friday's game") is discarded while the fixture still
     carries the placeholder Saturday kickoff, and accepted once the real Friday kickoff is known.
     The decision is taken once, when the article version is analysed; a later schedule
     correction does not re-run it.
  2. Any sentence that mentions a cup, a friendly or international duty is discarded, including
     the common phrasing "will miss the league match after an injury in <cup/international>".

Usage: PYTHONPATH=src python probe_relevance.py
"""

from __future__ import annotations

import json

from polymarket861 import news

TEAM = "Nottingham Forest FC"
PLACEHOLDER, REAL = "2026-11-28T15:00:00.000000Z", "2026-11-27T20:00:00.000000Z"
CASES = [
    "Aldo Ashdown has been ruled out of the match against Chelsea.",
    "Aldo Ashdown will miss Friday's game against Chelsea with a hamstring injury.",
    "Aldo Ashdown will miss the match against Chelsea after injuring his ankle in the Carabao Cup tie on Tuesday.",
    "Aldo Ashdown will miss the Premier League match against Chelsea after returning injured from international duty.",
]


def game(kickoff):
    return {"home": TEAM, "away": "Chelsea FC", "kickoff": kickoff, "season": "2026-27"}


def main():
    print(json.dumps([
        {"sentence": text,
         "relevant_with_placeholder_saturday_kickoff": news.fixture_relevant(text, "Team news", game(PLACEHOLDER), TEAM),
         "relevant_with_real_friday_kickoff": news.fixture_relevant(text, "Team news", game(REAL), TEAM)}
        for text in CASES], indent=1))


if __name__ == "__main__":
    main()
