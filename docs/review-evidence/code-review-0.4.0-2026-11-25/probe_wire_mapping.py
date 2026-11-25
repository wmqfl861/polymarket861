"""Probe D — what each Touchline availability state becomes in the model, end to end.

Provider row (status, category, chance_of_playing)
  -> news_sources._wire_availability synthesizes an English sentence
  -> news_ai.extract (the bundled classifier/rules) reads that sentence back
  -> news_model.POLICY["absence_weight"] decides how much it moves the goal rates.

Also shown: (a) one unrecognised row fails the whole club response; (b) a listed player whose
name is not a unique roster name yields no claim. Player names are synthetic.

Usage: PYTHONPATH=src python probe_wire_mapping.py
"""

from __future__ import annotations

import json

from polymarket861 import news_ai, news_model, news_sources

import probe_common as pc

CLUB = "Chelsea FC"
ROSTER = [
    {"player_id": "enwiki:1", "name": "Aldo Ashdown", "aliases": ["Aldo Ashdown"], "position": "defender"},
    {"player_id": "enwiki:2", "name": "Bram Birch", "aliases": ["Bram Birch"], "position": "forward"},
]
AT = "2026-11-25T12:00:00.000000Z"


def run(rows):
    source = news_sources.default_sources(CLUB, pc.SEASON)[0]
    return news_sources.poll(source, CLUB, pc.SEASON, None,
                             getter=lambda url: pc.reply(pc.availability_payload("Chelsea", rows, AT), AT, url))


def main():
    cases = [
        ("suspended", "suspension", None),
        ("doubtful", "injury", 75),
        ("doubtful", "injury", 50),
        ("doubtful", "injury", 25),
        ("injured", "injury", 0),
        ("injured", "injury", None),
        ("injured", "injury", 25),
        ("injured", "injury", 50),
        ("injured", "injury", 75),
    ]
    table = []
    for status, category, chance in cases:
        (article,) = run([(11, "Aldo Ashdown", status, category, chance)])["articles"]
        claims = news_ai.extract(article["text"], ROSTER, authority="secondary", title=article["title"], team=CLUB)
        row = {"provider": {"status": status, "category": category, "chance_of_playing": chance},
               "synthesized_text": article["text"]}
        if claims:
            (claim,) = claims
            weight = news_model.POLICY["absence_weight"].get(claim["status"], {}).get(claim["availability"])
            row |= {"claim": [claim["event"], claim["status"], claim["availability"]],
                    "model_weight": weight, "enters_model": weight is not None}
        else:
            row |= {"claim": None, "model_weight": None, "enters_model": False}
        table.append(row)

    failures = {}
    for label, bad in (("unknown category", (12, "Bram Birch", "injured", "illness", None)),
                       ("unknown status", (12, "Bram Birch", "unavailable", "injury", None)),
                       ("name with digits or brackets", (12, "Bram Birch (U21)", "injured", "injury", 0))):
        try:
            out = run([(11, "Aldo Ashdown", "injured", "injury", 0), bad])
            failures[label] = {"articles": len(out["articles"])}
        except ValueError as exc:
            failures[label] = {"whole_club_poll_fails_with": str(exc)}

    (unlinked,) = run([(13, "Cato Calder", "injured", "injury", 0)])["articles"]
    unlinked_claims = news_ai.extract(unlinked["text"], ROSTER, authority="secondary", title=unlinked["title"], team=CLUB)
    print(json.dumps({
        "policy_absence_weight": news_model.POLICY["absence_weight"],
        "provider_state_to_model": table,
        "one_bad_row_next_to_a_valid_row": failures,
        "listed_player_not_on_roster": {"text": unlinked["text"], "claims": len(unlinked_claims)},
    }, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
