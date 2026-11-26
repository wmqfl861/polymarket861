#!/usr/bin/env python3
"""Where does the 0.4.2 package assume the English Premier League, and does the 0.5 plan name the module?

Reads the package sources and the two plan files that list code touchpoints. A hit is a line that
contains an EPL identifier, an EPL-only source address, the London time zone, or one of the season-shape
constants (380 matches, 361 floor, 38 rounds, 20 clubs, 10 fixtures per round) used as a bound or equality.
Comment-only lines and docstrings are counted too, because several of them are user-facing error texts.

Usage: python -I scan_league_assumptions.py <repo root> > scan-league-assumptions.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CLASSES = {
    "identifier": re.compile(r"(\bepl\b|\bepl[_/-]|[\"'/_-]epl\b|KNOWN_EPL|Premier League|premier-league|premierleague|\bEPL\b)"),
    "source_address": re.compile(r"(en\.1\.json|eng\.1/|getmatchdata/pl/|leagueShortcut\"\) != \"pl\"|== \"pl\"|\b5996\b|touchline)"),
    "time_zone": re.compile(r"Europe/London|\bLONDON\b"),
    "season_shape": re.compile(
        r"(\b380\b|\b361\b|(?<![\w.])38(?![\w.])|(?<![\w.])39(?![\w.]) - span|"
        r"(!=|==) ?20\b|(!=|==) ?10\b|expected_in_round|10 if rnd|10 - values)"
    ),
}


def main() -> None:
    repo = Path(sys.argv[1])
    package = repo / "src" / "polymarket861"
    plan_text = "".join((repo / "docs" / "five-leagues-0.5" / name).read_text(encoding="utf-8") for name in ("DEVELOPMENT-PLAN.md", "MODEL-AND-VALIDATION.md"))
    named = set(re.findall(r"`([a-z_]+\.py)`", plan_text)) | set(re.findall(r"polymarket861/([a-z_]+\.py)", plan_text))
    modules = {}
    for path in sorted(package.glob("*.py")):
        hits = {name: [] for name in CLASSES}
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name, pattern in CLASSES.items():
                if pattern.search(line):
                    hits[name].append(number)
        if any(hits.values()):
            modules[path.name] = {
                "named_in_plan": path.name in named,
                "lines": {k: v for k, v in hits.items() if v},
                "hit_lines": sum(len(set(v)) for v in hits.values()),
            }
    out = {
        "package_modules": len(list(package.glob("*.py"))),
        "modules_with_any_hit": len(modules),
        "modules_named_in_plan": sorted(named),
        "named_in_plan_count": len(named),
        "with_hits_and_named": sorted(m for m, v in modules.items() if v["named_in_plan"]),
        "with_hits_not_named": sorted(m for m, v in modules.items() if not v["named_in_plan"]),
        "with_season_shape_hits_not_named": sorted(m for m, v in modules.items() if not v["named_in_plan"] and "season_shape" in v["lines"]),
        "named_without_hits": sorted(named - set(modules)),
        "modules": modules,
    }
    json.dump(out, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
