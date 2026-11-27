"""Measure the same synthetic archives built by Claude's revision-replay probe.

First run probe_league_inputs_revision_replay_cost.py with --counts 2000, then
pass its scratch directory to this probe. The originals are opened read-only.
Run once with each candidate's --src to compare every revision and each
fixture's scored/withdrawn chronology as well as cold/warm/append costs.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import time
import zlib
from contextlib import closing
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--src", required=True)
parser.add_argument("--archives", required=True, type=Path)
parser.add_argument("--count", type=int, default=2000)
args = parser.parse_args()
sys.path.insert(0, args.src)

from polymarket861 import __version__, football, league_live_inputs as live, league_registry, store  # noqa: E402

LEAGUES = ("bun", "sea")
SEASON = "2026-27"
FIRST = "2026-11-27T00:00:00.000000Z"
NEW = "2026-11-27T01:00:00.000000Z"

counters = {"original_decompressions": 0, "page_parses": 0}
parse, decompress = live._wiki_scores, zlib.decompress


def counted(counter, function):
    def call(*a, **kw):
        counters[counter] += 1
        return function(*a, **kw)
    return call


live._wiki_scores = counted("page_parses", parse)
zlib.decompress = counted("original_decompressions", decompress)


def append(db, league):
    n, packed = db.execute("SELECT revision,body FROM wiki_receipts WHERE league=? "
                           "ORDER BY revision DESC LIMIT 1", (league,)).fetchone()
    content = json.loads(decompress(packed))
    revision, = content["query"]["pages"][0]["revisions"]
    revision["revid"], revision["timestamp"] = n + 1, NEW
    revision["slots"]["main"]["content"] = revision["slots"]["main"]["content"].replace("1–0", "2–1", 1)
    raw = json.dumps(content).encode()
    with db:
        db.execute("INSERT INTO wiki_receipts VALUES(?,?,?,?,?,?,?,?)", (league, SEASON, n + 1, NEW, NEW,
                   live._wiki_title(league, SEASON), hashlib.sha256(raw).hexdigest(), zlib.compress(raw)))


def update_version_digest(digest, fid, conflict, versions):
    digest.update(store.canonical([fid, conflict, [
        (store.iso(v.available), v.score, v.grade, v.provenance,
         store.iso(v.actual_kickoff) if v.actual_kickoff else None) for v in versions
    ]]).encode())


def measure(db, league, fixtures, until):
    previous = counters.copy()
    start = time.perf_counter()
    history, metadata = live._wiki_history(db, league, SEASON, until)
    loaded = time.perf_counter()
    changes = None
    if hasattr(live, "league_wiki_cache"):
        changes = live.league_wiki_cache.score_observations(league, SEASON, live._wiki_scores, history,
                                                          fixtures, {}, FIRST)
    chronology = hashlib.sha256()
    full_fields = hashlib.sha256()
    direct_fields = hashlib.sha256()
    for fid, match in fixtures.items():
        evidence = changes.get(fid, ()) if changes is not None else [
            (seen, src, loc, pub, scores[fid]) for seen, src, loc, pub, scores in history]
        versions, conflict = live._score_versions(match, evidence, ())
        chronology.update(store.canonical([fid, conflict, [
            (store.iso(v.available), v.score, v.grade, v.provenance) for v in versions
        ]]).encode())
        update_version_digest(full_fields, fid, conflict, versions)
    scored = time.perf_counter()
    # Do not include the slow control in measured scheduler work. The current
    # result engine records the actual kickoff, which the pre-fix output lacked.
    for fid, match in fixtures.items():
        original = [(seen, src, loc, pub, scores[fid]) for seen, src, loc, pub, scores in history]
        versions, conflict = live._score_versions(match, original, ())
        update_version_digest(direct_fields, fid, conflict, versions)
    assert full_fields.digest() == direct_fields.digest(), (league, until)
    digest = hashlib.sha256()
    for (seen, src, loc, pub, scores), meta in zip(history, metadata, strict=True):
        digest.update(store.canonical([seen, src, loc, pub, sorted(scores.items()), meta]).encode())
    return {"visible_revisions": len(history), "standalone_replay_seconds": round(loaded - start, 4),
            "result_timeline_seconds": round(scored - loaded, 4),
            **{k: v - previous[k] for k, v in counters.items()},
            "revision_matrices_metadata_sha256": digest.hexdigest(),
            "fixture_chronology_sha256": chronology.hexdigest(),
            "all_version_fields_sha256": full_fields.hexdigest(),
            "equal_to_direct_replay_including_actual_kickoff": True}


def results(league, directory):
    original = args.archives / f"{league}-{args.count}.sqlite3"
    local = directory / f"{league}.sqlite3"
    with closing(sqlite3.connect(original.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(local)) as db:
            source.backup(db)
            db.executescript(live._SCHEMA)
    clubs = league_registry.members(league, SEASON)
    fixtures = {}
    for home in clubs:
        for away in clubs:
            if home != away:
                fid = football.fixture_id(SEASON, home, away, league=league)
                fixtures[fid] = football.Fixture(fid, SEASON, 1, football.utc("2026-09-01T10:00Z"), home, away)
    result = {}
    for stage, until in [("cold", FIRST), ("warm", FIRST), ("append_one", NEW), ("rewind", FIRST)]:
        opener = live.league_wiki_cache.connect if hasattr(live, "league_wiki_cache") else sqlite3.connect
        with closing(opener(local)) as db:
            db.row_factory = sqlite3.Row
            if stage == "append_one":
                append(db, league)
            result[stage] = measure(db, league, fixtures, until)
    return result


with tempfile.TemporaryDirectory() as scratch:
    print(json.dumps({"package": __version__, "python": sys.version.split()[0],
                      "measurements": {lg: results(lg, Path(scratch)) for lg in LEAGUES}}, indent=2))
