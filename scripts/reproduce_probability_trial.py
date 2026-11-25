"""Register or reopen the isolated EPL probability study and report only locally observed evidence."""

import argparse
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from polymarket861 import (
    football,
    forward_window,
    jobs_research,
    model_study,
    pipeline,
    store,
)

KEY = "league-frequency-vs-team-strength"
VERSION = "B0-comparator"
PURPOSE = "isolated-probability-study-v1"


def open_state(root):
    existed = (root / "runtime.sqlite3").is_file()
    with closing(store.connect(root)) as db, db:
        marker = db.execute(
            "SELECT value FROM core_metadata WHERE name='probability_review_state'"
        ).fetchone()
        if existed and (not marker or marker[0] != PURPOSE):
            raise ValueError(
                "supply a fresh directory or the previously retained isolated probability-study state"
            )
        db.execute(
            "INSERT OR IGNORE INTO core_metadata VALUES(?,?)",
            ("probability_review_state", PURPOSE),
        )


def register(root, history, season, first):
    with closing(jobs_research._open(root)) as db:
        old = db.execute(
            "SELECT season,first FROM research_candidates WHERE key=?", (KEY,)
        ).fetchone()
    if old:
        if first is not None and (old["season"] != season or old["first"] != first):
            raise ValueError("the retained study has a different registered window")
        return jobs_research.get_candidate(root, KEY)
    path = history / season / "en.1.json"
    raw = path.read_bytes()
    git_blob = football.blob_sha(raw)
    if football.SOURCE_BLOBS.get(season) != git_blob:
        raise ValueError(
            "current-season schedule bytes must match the pinned EPL source"
        )
    capture = store.archive_bytes(root, "probability-study-schedule", raw)
    now = store.now_utc()
    with closing(store.connect(root)) as db, db:
        for f in football.load_fixtures(path):
            if f.kickoff > store.instant(now):
                store.register_fixture_db(
                    db,
                    {
                        "fixture_id": f.fixture_id,
                        "season": f.season,
                        "round_id": f.round,
                        "home": f.home,
                        "away": f.away,
                        "kickoff": store.iso(f.kickoff),
                        "known_at": now,
                        "precision": f.kickoff_precision,
                        "source": "pinned-openfootball",
                        "locator": f"openfootball/{git_blob}/sha256/{capture['sha256']}",
                    },
                    recorded_at=now,
                )
    if first is None:
        season, first = forward_window.next_start(store.eligible_fixtures(root), now)
    pipeline.register_recipe(root, VERSION, "B0")
    return jobs_research.register_candidate(
        root,
        KEY,
        kind="model",
        version=VERSION,
        baseline="B1",
        window=f"{season}/{first}",
        hypothesis="Test whether league-wide frequencies outperform the team-strength model on the four registered markets.",
        manifest={"evidence_track": forward_window.RESEARCH},
    )


def evidence_state(root, c):
    plan = c["manifest"]["forward_window"]
    games = forward_window.fixtures(
        store.eligible_fixtures(root), c["season"], c["first"], c["span"]
    )
    kickoffs = {f["fixture_id"]: f["kickoff"] for f in games}
    registered = {row[-1] for row in plan["scheduled_at_registration"]}
    versions = {
        c["version"]: c["manifest"]["recipe_digest"],
        c["baseline"]: c["manifest"]["baseline_recipe_digest"],
    }
    seals = {v: set() for v in versions}
    with closing(jobs_research._open(root)) as db:
        stamps = [
            r[1]
            for r in db.execute(
                "SELECT fixture_id,recorded_at FROM schedule_receipts WHERE recorded_at<=?",
                (c["registered_at"],),
            )
            if r[0] in registered
        ]
        rows = db.execute(
            "SELECT * FROM forecast_seals WHERE model_version IN (?,?) AND protocol=? AND mode IN ('formal','technical') "
            "AND horizon='T-60m' AND sealed_at>=?",
            (*versions, store.PROTOCOL, c["registered_at"]),
        )
        for row in rows:
            if kickoffs.get(row["fixture_id"]) != row["kickoff"]:
                continue
            seal = pipeline._verified_seal(row)
            if seal["recipe_digest"] != versions[row["model_version"]] or store.instant(
                row["cutoff"]
            ) != store.instant(row["kickoff"]) - timedelta(hours=1):
                continue
            seals[row["model_version"]].add((row["fixture_id"], row["mode"]))
        reviews = [
            dict(r) | {"evidence": json.loads(r["evidence"])}
            for r in db.execute(
                "SELECT at,action,evidence FROM research_events WHERE key=? AND action IN ('historical_study_reviewed','research_reviewed') ORDER BY id",
                (c["key"],),
            )
        ]
    both = seals[c["version"]] & seals[c["baseline"]]
    return {
        "state_context": PURPOSE,
        "experiment_key": c["key"],
        "registration_digest": c["digest"],
        "registered_at": c["registered_at"],
        "registration_receipt_sha256": store.digest(
            [c["key"], c["digest"], c["registered_at"]]
        ),
        "experiment_state": c["state"],
        "assessment": c["assessment"],
        "evidence_track": c["manifest"]["evidence_track"],
        "candidate": c["version"],
        "baseline": c["baseline"],
        "prospective_window": [plan["rounds"][0], plan["rounds"][-1]],
        "prospective_expected_matches": plan["expected_fixtures"],
        "window_fixture_labels_known_at_registration": len(registered),
        "currently_scheduled_window_fixtures": len(games),
        "registration_schedule_receipt_range": [min(stamps), max(stamps)]
        if stamps
        else [],
        "prospective_seals": sum(map(len, seals.values())),
        "prospective_seals_by_version": {v: len(rows) for v, rows in seals.items()},
        "prospective_same_mode_fixture_pairs": len({fid for fid, _mode in both}),
        "named_reviews": reviews,
        "as_of": store.now_utc(),
    }


def reproduce(root, history, season, first):
    open_state(root)
    registered = register(root, history, season, first)
    summary = model_study.trial_history(root, registered, history)
    return evidence_state(root, registered) | {"history": summary}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("history", type=Path)
    parser.add_argument(
        "--state", type=Path, help="retain or reopen a dedicated local study database"
    )
    parser.add_argument("--season", default="2026-27")
    parser.add_argument(
        "--first-round",
        type=int,
        help="the actual first deadline must still be in the future when registering",
    )
    args = parser.parse_args()
    if args.state:
        outcome = reproduce(args.state, args.history, args.season, args.first_round)
    else:
        with TemporaryDirectory(prefix="pm861-isolated-probability-") as temporary:
            outcome = reproduce(
                Path(temporary), args.history, args.season, args.first_round
            )
    print(json.dumps(outcome, sort_keys=True, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
