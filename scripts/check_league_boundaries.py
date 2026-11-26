#!/usr/bin/env python3
"""Check newly introduced football competition assumptions against reviewed source lines."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts/league-boundaries.json"
PATTERNS = (
    re.compile(r"(\bepl\b|\bepl[_/-]|[\"'/_-]epl\b|KNOWN_EPL|Premier League|premier-league|premierleague|\bEPL\b)"),
    re.compile(r'(en\.1\.json|eng\.1/|getmatchdata/pl/|leagueShortcut"\) != "pl"|== "pl"|\b5996\b|touchline)'),
    re.compile(r"Europe/London|\bLONDON\b"),
    re.compile(
        r"(\b380\b|\b361\b|(?<![\w.])38(?![\w.])|(?<![\w.])39(?![\w.]) - span|"
        r"(!=|==) ?20\b|(!=|==) ?10\b|expected_in_round|10 if rnd|10 - values)"
    ),
    re.compile(r"\b(?:Europe/(?:Madrid|Berlin|Rome|Paris)|(?:es|de|it|fr)\.1\.json)\b"),
    re.compile(r"\b(?:306|291)\b|(?:!=|==)\s*18\b|range\(1,\s*35\)"),
)


def scan(package):
    hits = {}
    for module in sorted(package.rglob("*.py")):
        lines = module.read_text(encoding="utf-8").splitlines()
        selected = [
            line.strip()
            for line in lines
            if any(pattern.search(line) for pattern in PATTERNS)
        ]
        if selected:
            hits[module.relative_to(package).as_posix()] = sorted(selected)
    return hits


def additions(current, allowed):
    for name, lines in current.items():
        for line, count in (Counter(lines) - Counter(allowed.get(name, ()))).items():
            yield name, line, count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="refresh after reviewing the changed source lines",
    )
    args = parser.parse_args()
    current = scan(ROOT / "src/polymarket861")
    if args.write_baseline:
        BASELINE.write_text(
            json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return 0
    allowed = json.loads(BASELINE.read_text(encoding="utf-8"))
    delta = list(additions(current, allowed))
    for name, line, count in delta:
        print(f"{name} (+{count}): {line}")
    if delta:
        print(
            "Review league scope or use league_registry, then update the reviewed list if appropriate."
        )
    return int(bool(delta))


if __name__ == "__main__":
    raise SystemExit(main())
