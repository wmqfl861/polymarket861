"""Local, input-pinned probability studies. A 24h sensitivity has no financial authority."""

import gzip
import hashlib
import json
from collections import defaultdict
from contextlib import closing
from functools import lru_cache
from datetime import timedelta
from pathlib import Path

from . import experiment_statistics as stats
from . import dbscope, football, forward_window, store

METHOD = "T60-final-schedule-result-lag24h-v1"
BENCHMARK = tuple(f"{y}-{(y + 1) % 100:02}" for y in range(2021, 2026))
LEGACY_RELEASE = "432e0afe485230bd7caa46eb0d1d68b26f01db25"
LEGACY_BENCHMARK_CHECKOUT = "8993f15cfcc61042aedc5e885bb82217488ecb33"
LEGACY_INSTALLATIONS = (LEGACY_RELEASE, LEGACY_BENCHMARK_CHECKOUT)
# git ls-tree yields the same four objects in the published release and the subsequent audit checkout.
LEGACY_EXECUTION_OBJECTS = {
    "src": "9ac7efa8f5a176d41225ba8d6ae627c50d19d575",
    "pyproject.toml": "6ccb6c35bb23f2991841937595afd2c819adf4f5",
    "constraints": "33a340b07b5bf16fb9b12c393ad3b19b6348ab94",
    "deploy": "e60bd9a39a37cb553dfc94ec1797c0087bedeff7",
}
LEGACY_RECIPE = "7594c08a37d847a7c40f256974e2817ffe9c9d7eb8cf720b47a5b7f61dcbb29f"
LEGACY_BENCHMARK = "72f5f905fe8fe014bcef0016e9a810f2a1d925a3b86c3c00eeb9ae22a0ddb795"
SCHEMA = """
CREATE TABLE IF NOT EXISTS probability_studies (
 recipe TEXT NOT NULL, sample TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(recipe,sample));
CREATE TABLE IF NOT EXISTS model_release_comparisons (
 digest TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_release_decisions (
 release_id INTEGER PRIMARY KEY REFERENCES builtin_model_releases(id), comparison TEXT NOT NULL,
 reviewer TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_release_legacy_confirmations (
 release_id INTEGER PRIMARY KEY REFERENCES model_release_decisions(release_id), installed_commit TEXT NOT NULL);
"""


PACKAGE_ROOT = Path(__file__).parent
# These modules render/export views, copy or verify backups, or only expose a package version/entry point.
# Decision, collection, validation and statistical extensions are included by default.
NON_DECISION_MODULES = frozenset(
    {"dashboard.py", "operations.py", "__init__.py", "__main__.py", "cli.py"}
)


@lru_cache(maxsize=512)
def _file_sha(path, identity):
    # Reuse bytes only while device, inode, size and both change clocks match.
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decision_implementation():
    """Version operational Python modules and numerical dependencies used in decision evidence."""
    import numpy
    import scipy

    sources = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        name = path.relative_to(PACKAGE_ROOT).as_posix()
        if name in NON_DECISION_MODULES:
            continue
        st = path.stat()
        identity = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        sources[name] = _file_sha(path, identity)
    return {
        "algorithm_source": sources,
        "numeric_runtime": {"numpy": numpy.__version__, "scipy": scipy.__version__},
    }


def _db(data_dir):
    db = store.connect(data_dir)
    dbscope.prepare(db, "model-study", SCHEMA)
    return db


def _archive_input(root, seasons):
    years = {football.season_year(s) - d for s in seasons for d in range(4)}
    inputs = {}
    games = []
    for y in sorted(years):
        name = f"{y}-{(y + 1) % 100:02}"
        path = Path(root) / name / "en.1.json"
        expected = football.SOURCE_BLOBS.get(name)
        if (
            not expected
            or not path.is_file()
            or football.blob_sha(path.read_bytes()) != expected
        ):
            raise ValueError(
                f"verified OpenFootball source required for {name}, including three training seasons"
            )
        batch = football.load_fixtures(path, archive_lag_hours=24)
        rows = [
            dict(
                fixture_id=f.fixture_id,
                season=f.season,
                round_id=f.round,
                home=f.home,
                away=f.away,
            )
            for f in batch
        ]
        if not forward_window.complete(rows, forward_window.rounds(name, 1, 38)) or any(
            f.home_goals is None or f.away_goals is None for f in batch
        ):
            raise ValueError(f"the official denominator is incomplete for {name}")
        games.extend(batch)
        inputs[name] = expected
    return games, inputs


def measure(data_dir, version, root, seasons):
    from . import pipeline

    registered = pipeline._recipe(data_dir, version)
    seasons = sorted(set(seasons))
    if not seasons:
        raise ValueError("select completed seasons")
    games, inputs = _archive_input(root, seasons)
    engine = football.FootballEngine(games)
    rows = []
    selected = sorted(
        (f for f in games if f.season in seasons),
        key=lambda f: (f.kickoff, f.fixture_id),
    )
    recipe = registered["recipe"]
    now = store.instant(store.now_utc())
    for f in selected:
        if f.kickoff_precision != "minute" or f.kickoff + timedelta(hours=24) > now:
            raise ValueError(
                "the study requires completed historical fixtures with known kickoff minutes"
            )
        cut = f.kickoff - timedelta(hours=1)
        # No reconstructed news or lineups are supplied to either arm.
        outcome = pipeline.probability(
            engine, f.home, f.away, f.kickoff, cut, f.season, recipe, {}
        )
        p = outcome["probabilities"]
        rows.append(
            [
                f.fixture_id,
                f.season,
                f.round,
                football.iso(cut),
                [f.home_goals, f.away_goals],
                outcome["evidence"]["input_sha256"],
                [p[k] for k in football.PROB_KEYS],
                store.digest(outcome["totals"]),
            ]
        )
    doc = {
        "format": 1,
        "method": METHOD,
        "seasons": seasons,
        "recipe": registered["recipe_digest"],
        "inputs": inputs,
        "rows": rows,
    }
    raw = gzip.compress(store.canonical(doc).encode(), mtime=0)
    sha = store.archive_bytes(data_dir, "probability-study-predictions", raw)["sha256"]
    with closing(_db(data_dir)) as db, db:
        db.execute(
            "INSERT OR IGNORE INTO probability_studies VALUES(?,?,?)",
            (doc["recipe"], store.digest(seasons), sha),
        )
        existing = _read(data_dir, db, doc["recipe"], seasons)
        if existing != doc:
            raise ValueError(
                "the same recipe and fixed sample produced different predictions"
            )
    return {
        "version": version,
        "recipe": doc["recipe"],
        "study_sha256": sha,
        "method": METHOD,
        "matches": len(rows),
        "seasons": seasons,
    }


def _read(data_dir, db, recipe, seasons):
    row = db.execute(
        "SELECT sha256 FROM probability_studies WHERE recipe=? AND sample=?",
        (recipe, store.digest(sorted(seasons))),
    ).fetchone()
    if row:
        sha = row[0]
        path = store.blob_path(data_dir, sha)
    elif recipe == LEGACY_RECIPE and tuple(sorted(seasons)) == BENCHMARK:
        sha = LEGACY_BENCHMARK
        path = Path(__file__).parent / "assets" / "release-benchmark-0.2.0.json.gz"
    else:
        raise ValueError(
            "missing frozen study from this physical recipe; run forecast release-baseline before installing its successor"
        )
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError("probability study bytes differ from their retained receipt")
    doc = json.loads(gzip.decompress(raw))
    if (
        doc["recipe"] != recipe
        or doc["seasons"] != sorted(seasons)
        or doc["method"] != METHOD
    ):
        raise ValueError("study identity differs from its registration")
    return doc


def compare(candidate, baseline, count, *, allow_training_change=False):
    if any(candidate[k] != baseline[k] for k in ("method", "inputs", "seasons")):
        raise ValueError(
            "release comparison requires the identical fixed archive and cutoff rule"
        )
    groups = defaultdict(list)
    averages = [0.0, 0.0]
    changed = 0
    changed_training = 0
    for a, b in zip(candidate["rows"], baseline["rows"], strict=True):
        if a[:5] != b[:5] or (a[5] != b[5] and not allow_training_change):
            raise ValueError(
                "fixture, score, cutoff or training inputs differ between arms"
            )
        changed_training += a[5] != b[5]
        scored = [
            football.score_probabilities(dict(zip(football.PROB_KEYS, row[6])), *row[4])
            for row in (a, b)
        ]
        c, p = scored
        for i, r in enumerate(scored):
            averages[i] += r["score"]
        changed += a[6:] != b[6:]
        groups[(a[1], a[2])].append(
            [
                c["score"] - p["score"],
                *(
                    c["item_scores"][k] - p["item_scores"][k]
                    for k in football.ITEM_KEYS
                ),
            ]
        )
    n = len(candidate["rows"])
    if n != 380 * len(candidate["seasons"]) or any(
        len(g) != 10 for g in groups.values()
    ):
        raise ValueError("study is missing official fixtures or paired rounds")
    names = ("overall", *football.ITEM_KEYS)
    inference = stats.clustered_t(
        [g for _, g in sorted(groups.items())], 2 * len(names) * count
    )
    intervals = dict(zip(names, inference.pop("intervals")))
    return {
        "method": METHOD,
        "history_quality": "24h_lag_sensitivity_final_archive_schedule",
        "historical_context": "no_archived_news_or_lineups",
        "formal_eligibility": False,
        "candidate_recipe": candidate["recipe"],
        "baseline_recipe": baseline["recipe"],
        "candidate_sha256": store.digest(candidate),
        "baseline_sha256": store.digest(baseline),
        "input_blobs": candidate["inputs"],
        "seasons": candidate["seasons"],
        "denominator": n,
        "paired_count": n,
        "rounds": len(groups),
        "changed_predictions": changed,
        "changed_training_views": changed_training,
        "candidate_score": averages[0] / n,
        "baseline_score": averages[1] / n,
        "candidate_count": n,
        "baseline_count": n,
        "comparison_rejections": {},
        "inference": inference,
        "intervals": intervals,
    }


def trial_history(data_dir, candidate, root=None, count=None):
    from . import jobs_research

    manifest = candidate["manifest"]
    if manifest.get("evidence_track") != forward_window.RESEARCH:
        raise ValueError(
            "24h sensitivity studies require a separately registered probability-research-24h experiment"
        )
    seasons = jobs_research._history_seasons(candidate)
    if count is None:
        with closing(jobs_research._open(data_dir)) as db:
            count = db.execute(
                "SELECT count(*) FROM research_candidates WHERE season=? AND first=? AND span=?",
                (candidate["season"], candidate["first"], candidate["span"]),
            ).fetchone()[0]
    for field in ("version", "baseline"):
        version = candidate[field]
        expected = manifest[
            "recipe_digest" if field == "version" else "baseline_recipe_digest"
        ]
        if jobs_research._bound_recipe(data_dir, version) != expected:
            raise ValueError(
                "the executable study version does not match the registration"
            )
        if root is not None:
            measure(data_dir, version, root, seasons)
    with closing(_db(data_dir)) as db:
        arms = [
            _read(data_dir, db, manifest[k], seasons)
            for k in ("recipe_digest", "baseline_recipe_digest")
        ]
    return compare(*arms, count)


def _release_versions(data_dir):
    from . import pipeline

    with closing(store.connect(data_dir)) as db:
        incumbent = store.builtin_model_at(db)
        old = db.execute(
            "SELECT recipe FROM model_recipes WHERE version=?", (incumbent,)
        ).fetchone()
    if not old:
        raise ValueError("no previous built-in model in this database")
    old_digest = store.digest(json.loads(old[0]))
    new_recipe = pipeline._build_recipe("B1", (0.5, 1.5, 2.5, 3.5))
    new_digest = store.digest(new_recipe)
    if new_digest == old_digest:
        raise ValueError("the installed probability recipe is already active")
    return incumbent, old_digest, "B1@" + new_digest, new_digest


def release_report(data_dir, root, *, benchmark_only=False):
    from . import pipeline

    if benchmark_only:
        before, old_sha = "B1", LEGACY_RECIPE
        new_sha = store.digest(pipeline._build_recipe("B1", (0.5, 1.5, 2.5, 3.5)))
        after = "B1@" + new_sha
    else:
        before, old_sha, after, new_sha = _release_versions(data_dir)
    registered = pipeline.register_recipe(data_dir, after, "B1")
    if registered["recipe_digest"] != new_sha:
        raise ValueError("installed executable changed while preparing the release")
    measure(data_dir, after, root, BENCHMARK)
    with closing(_db(data_dir)) as db, db:
        arms = [_read(data_dir, db, sha, BENCHMARK) for sha in (new_sha, old_sha)]
        local = db.execute(
            "SELECT 1 FROM probability_studies WHERE recipe=? AND sample=?",
            (old_sha, store.digest(BENCHMARK)),
        ).fetchone()
        predecessor = (
            {"source": "measured_on_incumbent"}
            if local
            else {
                "source": "reproduced_tagged_predecessor",
                "reference_commit": LEGACY_RELEASE,
                "benchmark_checkout": LEGACY_BENCHMARK_CHECKOUT,
                "identical_execution_git_objects": LEGACY_EXECUTION_OBJECTS,
                "reference_pipeline_sha256": "46d04d6080969e467a6752121fc946c2f61dac668698c3454b906416dbee0449",
                "legacy_recipe_files": ["football.py", "football_model.py"],
                "local_previous_pipeline_independently_known": False,
            }
        )
        report = {
            "from_version": before,
            "baseline_identity": predecessor,
            "to_version": after,
            "comparison": compare(*arms, 1, allow_training_change=True),
        }
        ident = store.digest(report)
        if not benchmark_only:
            db.execute(
                "INSERT OR IGNORE INTO model_release_comparisons VALUES(?,?)",
                (ident, store.canonical(report)),
            )
    return report | {"report_sha256": ident}


def _stored_comparison(db, report_sha):
    row = db.execute(
        "SELECT body FROM model_release_comparisons WHERE digest=?", (report_sha,)
    ).fetchone()
    if not row or store.digest(json.loads(row[0])) != report_sha:
        raise ValueError("a verified local release comparison is required")
    return json.loads(row[0])


def _release_decision(db, report_sha):
    row = db.execute(
        "SELECT r.version,r.effective_at,d.*,l.installed_commit AS legacy_reference FROM model_release_decisions d "
        "JOIN builtin_model_releases r ON r.id=d.release_id LEFT JOIN model_release_legacy_confirmations l "
        "ON l.release_id=d.release_id WHERE d.comparison=? ORDER BY r.id DESC LIMIT 1",
        (report_sha,),
    ).fetchone()
    return dict(row) if row else None


def inspect_release(data_dir, report_sha):
    """Read the retained comparison and the exact identity signed, including pre-confirmation records."""
    with closing(_db(data_dir)) as db:
        return {
            "report_sha256": report_sha,
            "report": _stored_comparison(db, report_sha),
            "decision": _release_decision(db, report_sha),
        }


def approve_release(data_dir, report_sha, reviewer, reason, *, legacy_reference=None):
    signature = stats.signoff(reviewer, reason)
    from . import pipeline

    with closing(_db(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        report = _stored_comparison(db, report_sha)
        old, new = report["from_version"], report["to_version"]
        c = report["comparison"]
        if new != "B1@" + store.digest(
            pipeline._build_recipe("B1", (0.5, 1.5, 2.5, 3.5))
        ):
            raise ValueError("the installed code differs from the reviewed release")
        for v, k in ((new, "candidate_recipe"), (old, "baseline_recipe")):
            bound = db.execute(
                "SELECT recipe FROM model_recipes WHERE version=?", (v,)
            ).fetchone()
            if not bound or store.digest(json.loads(bound[0])) != c[k]:
                raise ValueError(
                    "release recipe differs from the reviewed physical build"
                )
        identity = report.get("baseline_identity", {})
        # An early preparation report can lack the provenance block. The known two-file recipe still
        # needs an explicit installation identity before a new decision can use it.
        packaged = identity.get("source") == "reproduced_tagged_predecessor" or (
            not identity and c["baseline_recipe"] == LEGACY_RECIPE
        )
        if packaged and (
            c["baseline_recipe"] != LEGACY_RECIPE
            or identity.get("reference_commit", LEGACY_RELEASE)
            not in LEGACY_INSTALLATIONS
        ):
            raise ValueError("the retained benchmark identifies an unknown predecessor")
        if legacy_reference is not None and (
            not packaged or legacy_reference not in LEGACY_INSTALLATIONS
        ):
            raise ValueError(
                "unverified predecessor commit supplied for this comparison"
            )
        decision = _release_decision(db, report_sha)
        if decision and store.builtin_model_at(db) == new:
            if legacy_reference is not None and decision["legacy_reference"] not in (
                None,
                legacy_reference,
            ):
                raise ValueError(
                    "the existing decision confirms a different installed commit"
                )
            return decision
        if packaged and legacy_reference is None:
            raise ValueError(
                "confirm the actual previous installation with --legacy-reference "
                + LEGACY_RELEASE
                + " (published 0.2.0) or "
                + LEGACY_BENCHMARK_CHECKOUT
                + " (equivalent audit checkout)"
            )
        if store.builtin_model_at(db) != old:
            raise ValueError("active builtin changed since release comparison")
        actual = compare(
            *[
                _read(data_dir, db, c[k], BENCHMARK)
                for k in ("candidate_recipe", "baseline_recipe")
            ],
            1,
            allow_training_change=True,
        )
        if actual != c:
            raise ValueError("retained evidence changed before approval")
        stamp = store.now_utc()
        release_id = db.execute(
            "INSERT INTO builtin_model_releases(version,effective_at) VALUES(?,?)",
            (new, stamp),
        ).lastrowid
        db.execute(
            "INSERT INTO model_release_decisions VALUES(?,?,?,?)",
            (release_id, report_sha, signature["reviewer"], signature["reason"]),
        )
        if packaged:
            db.execute(
                "INSERT INTO model_release_legacy_confirmations VALUES(?,?)",
                (release_id, legacy_reference),
            )
    return {
        "version": new,
        "effective_at": stamp,
        "comparison": report_sha,
        "legacy_reference": legacy_reference,
        **signature,
    }


def register(forecasts):
    base = forecasts.add_parser(
        "release-baseline",
        help="retain the executable incumbent on the fixed five-season probability benchmark before an upgrade",
    )
    base.add_argument("root", type=Path)

    def old(a):
        from . import pipeline

        return measure(
            a.data_dir, pipeline.prepare_builtin(a.data_dir), a.root, BENCHMARK
        )

    base.set_defaults(func=old)
    comp = forecasts.add_parser(
        "release-compare",
        help="compare a newly installed built-in against retained incumbent probabilities",
    )
    comp.add_argument("root", type=Path)
    comp.add_argument(
        "--reference-0.2.0",
        dest="benchmark_only",
        action="store_true",
        help="compare against the packaged, reproduced 0.2.0 baseline for independent verification",
    )
    comp.set_defaults(
        func=lambda a: release_report(
            a.data_dir, a.root, benchmark_only=a.benchmark_only
        )
    )
    saved = forecasts.add_parser(
        "release-report", help="read a retained comparison and its original sign-off"
    )
    saved.add_argument("report_sha256")
    saved.set_defaults(func=lambda a: inspect_release(a.data_dir, a.report_sha256))
    decide = forecasts.add_parser(
        "release-approve", help="activate the reviewed built-in prospectively"
    )
    decide.add_argument("report_sha256")
    decide.add_argument(
        "--legacy-reference",
        help="confirm the previous installed commit when a legacy two-file recipe uses the packaged reference",
    )
    decide.add_argument("--reviewer", required=True)
    decide.add_argument("--reason", required=True)
    decide.set_defaults(
        func=lambda a: approve_release(
            a.data_dir,
            a.report_sha256,
            a.reviewer,
            a.reason,
            legacy_reference=a.legacy_reference,
        )
    )
