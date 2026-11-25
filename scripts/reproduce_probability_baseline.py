"""Reproduce the fixed legacy rows on the original executable. See docs/model-release-governance.md."""

import gzip, hashlib
from datetime import timedelta
from pathlib import Path
from polymarket861 import football, pipeline, store
import argparse

parser = argparse.ArgumentParser(
    description="Reproduce the shipped predecessor probabilities with the original executable on PYTHONPATH."
)
parser.add_argument("root", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
root = args.root
if (
    hashlib.sha256(Path(pipeline.__file__).read_bytes()).hexdigest()
    != "46d04d6080969e467a6752121fc946c2f61dac668698c3454b906416dbee0449"
):
    raise SystemExit("Run against the predecessor checkout at 8993f15.")
wanted = [f"{y}-{(y + 1) % 100:02}" for y in range(2021, 2026)]
required = {f"{y}-{(y + 1) % 100:02}" for y in range(2018, 2026)}
fixtures = [
    f
    for f in football.load_fixtures(root, archive_lag_hours=24)
    if f.season in required
]
if len(fixtures) != len(required) * 380 or any(
    f.source_blob != football.SOURCE_BLOBS[f.season] for f in fixtures
):
    raise SystemExit("Expected the original complete EPL archives.")
engine = football.FootballEngine(fixtures)
out = []
for f in sorted(
    (f for f in fixtures if f.season in wanted), key=lambda f: (f.kickoff, f.fixture_id)
):
    cut = f.kickoff - timedelta(minutes=60)
    result = engine.forecast(
        f.home,
        f.away,
        f.kickoff,
        cut,
        season=f.season,
        lines=(0.5, 1.5, 2.5, 3.5),
        baseline=False,
    )
    pp = result["probabilities"]
    out.append(
        [
            f.fixture_id,
            f.season,
            f.round,
            football.iso(cut),
            [f.home_goals, f.away_goals],
            result["evidence"]["input_sha256"],
            [pp[k] for k in football.PROB_KEYS],
            store.digest(result["totals"]),
        ]
    )
r = {
    "format": 1,
    "method": "T60-final-schedule-result-lag24h-v1",
    "recipe": store.digest(pipeline._build_recipe("B1", (0.5, 1.5, 2.5, 3.5))),
    "inputs": {s: football.SOURCE_BLOBS[s] for s in sorted(required)},
    "seasons": wanted,
    "rows": out,
}
raw = gzip.compress(store.canonical(r).encode(), mtime=0)
path = args.output
path.write_bytes(raw)
print(len(out), len(raw), hashlib.sha256(raw).hexdigest(), r["recipe"], flush=True)
