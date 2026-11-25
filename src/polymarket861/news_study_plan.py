"""Prospective, descriptive paired-news study and its probability-era identity."""

import ast
import hashlib
import re
from datetime import timedelta
from functools import lru_cache

from . import store

FIRST_SEASON = 2026
PLAN = {
    "version": "epl-news-observed-pairs-v1",
    "first_season": "2026-27",
    "population": "every unique prospectively sealed primary pair with timely observations of both teams",
    "strata": [
        "technical/formal separately",
        "T-60m/T-24h separately",
        "probability era",
    ],
    "stopping": "all eligible fixtures from the first season through the requested season, including partial seasons",
    "score": "with-news minus no-news four-component score, verified regulation outcome",
    "close": "home-win absolute distance to normalized verified actual close, no-news minus with-news, percentage points",
    "market_decision": "native batch up to 30 seconds before the sealed input cutoff",
    "market_close": "same contracts and rules, fresh native batch in the last 30 seconds strictly before independently verified kickoff and after sealing",
    "reference": "three compatible normalized Yes midpoints for 1X2, Over midpoint with both directions checked for totals",
    "coverage_age_hours": 6,
    "direction": "news minus baseline versus close minus baseline; market drift uses close minus decision midpoint",
    "unchanged": "include in the paired means, count zero movements and ties outside directional wins/losses",
    "direction_absolute_tolerance": 1e-12,
    "unit": "one observation per fixture and endpoint, clustered by season and official round",
    "family_comparisons": 4,  # two endpoints at two horizons, within each mode and era
    "family_confidence": 0.95,
    "target_power": 0.80,
    "practical_effect": {"score_points": 0.10, "home_close_distance_pp": 0.25},
    "planning_assumptions": {
        "single_fixture_sd": {"score_points": 1.0, "home_close_distance_pp": 1.5},
        "fixtures_per_round": 10,
        "intraround_correlation": 0.05,
    },
    "empirical_mde": "two-sided noncentral Student-t planning approximation at the observed CR1 standard error and G-1 degrees of freedom; undefined for fewer than two rounds or zero estimated variance",
    "authority": "descriptive repeated looks, no activation, accuracy verdict, CLV, or return inference",
}
PLAN_SHA256 = store.digest(PLAN)
LEGACY_POLICY = (
    "epl-player-news-paired-v1",
    "fac9ee74e81e1e3d4bb2374b07815cc849e6076c44d00c06721a9b0322f40f3f",
)

# Use the full source identities of the dedicated models. In mixed-purpose modules,
# retain the actual definitions and transitive module-local constants/helpers used
# by the prediction path. A collector, CLI, ledger, or report edit is not a model edit.
_MODEL_FILES = (
    "football_model.py",
    "score_bounds.py",
    "news_model.py",
    "news_importance.py",
    "news_ai.py",
    "news_ai_corpus.py",
    "news_scope.py",
    "news_local_ai.py",
)
_SYMBOLS = {
    "football.py": ("FootballEngine", "load_fixtures", "history_kickoff"),
    "pipeline.py": ("_forecast_arms",),
    "news.py": (
        "snapshot",
        # These writers choose the claimant, persist effective/first-observed
        # times and tie the upstream state to its original receipt. The reader
        # alone cannot identify a change to a value already written to SQLite.
        "_extract_pending",
        "_collect_wire",
        "_poll_proof",
        "_wire_unmatched",
    ),
    # The adapter parses/quarantines native rows. The claimant is invoked from
    # news._extract_pending, so it is a separate root (including WIRE_MODEL).
    "news_sources.py": (
        "_wire_availability",
        "wire_claim",
        "wire_fingerprint",
        "wiki_prose",
    ),
    "news_roster.py": ("roster_views", "parse_squad", "_people"),
    "news_minutes.py": ("parse", "latest"),
    "evidence.py": ("_collect", "_selected_lineup"),
    "evidence_sources.py": ("name_key", "source_at", "granted"),
    "result_freshness.py": ("_AS_OF",),
}

# Writers call the connection / schema installer for storage. A new index or
# SQLite connection option changes the physical release, not the news policy.
_STORAGE_ONLY = {"news.py": {"_db"}}


@lru_cache(maxsize=128)
def _symbol_digest(path, physical_sha, roots):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != physical_sha:
        raise ValueError("news probability source changed while registering the recipe")
    definitions = {}
    for node in ast.parse(raw).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            definitions[node.name] = node
        elif isinstance(node, ast.Assign):
            definitions.update(
                {n.id: node for n in node.targets if isinstance(n, ast.Name)}
            )
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            prefix = (
                "." * node.level + (node.module or "")
                if isinstance(node, ast.ImportFrom)
                else ""
            )
            definitions.update(
                {n.asname or n.name: (prefix, n.name) for n in node.names}
            )
    selected = {}
    pending = set(roots)
    storage_only = _STORAGE_ONLY.get(path.name, ())
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        node = definitions.get(name)
        if node is None:
            selected[name] = None
            continue
        if isinstance(node, tuple):
            selected[name] = node
            continue
        selected[name] = ast.dump(node, include_attributes=False)
        pending.update(
            n.id
            for n in ast.walk(node)
            if isinstance(n, ast.Name)
            and n.id in definitions
            and n.id not in storage_only
        )
    return store.digest(selected)


def probability_sources(root, files):
    """Stored alongside, and independently of, the complete physical-release manifest."""
    sources = {name: files[name] for name in _MODEL_FILES if name in files}
    for name, names in _SYMBOLS.items():
        if name in files:
            sources[name] = _symbol_digest(root / name, files[name], names)
    return sources


def extractor_identity(snapshot):
    # A fully processed empty source is evidence too. Reading environment settings
    # at grading time would relabel old analyses after an operator changes models.
    stamps = [
        s.get("extractor_config_sha256")
        for side in ("home", "away")
        for s in snapshot.get("coverage", {}).get(side, {}).get("sources", [])
        if s.get("availability_observed")
    ]
    stamps.extend(f.get("extractor_config_sha256") for f in snapshot.get("facts", []))
    distinct = set(stamps)
    if len(distinct) == 1:
        ident = distinct.pop()
        if isinstance(ident, str) and re.fullmatch(r"[0-9a-f]{64}", ident):
            return ident
    return None


def timely_coverage(pair):
    cut = store.instant(pair["input_cutoff"])
    lower = cut - timedelta(hours=PLAN["coverage_age_hours"])
    for side in ("home", "away"):
        sources = pair["coverage"].get(side, {}).get("sources", [])
        if not any(
            s.get("status") in ("ok", "degraded")
            and s.get("availability_observed", s.get("status") == "ok") is True
            and s.get("checked_at")
            and lower
            <= store.instant(s.get("source_observed_at") or s["checked_at"])
            <= store.instant(s["checked_at"])
            <= cut
            for s in sources
        ):
            return False
    return pair["coverage"].get("both_teams_observed") is True


def era(recipe, pair, extractor):
    return store.digest(
        {
            "probability_source": recipe["news_probability_source"],
            "numeric_runtime": recipe["numeric_runtime"],
            "lines": recipe["lines"],
            "protocol": pair["protocol"],
            "policy_sha256": pair["policy_sha256"],
            "availability_basis": pair["availability_basis"],
            "extractor_config_sha256": extractor,
        }
    )


def declaration(recipe, pair, snapshot):
    extractor = extractor_identity(snapshot)
    return {
        "plan_version": PLAN["version"],
        "plan_sha256": PLAN_SHA256,
        "extractor_config_sha256": extractor,
        "era_sha256": era(recipe, pair, extractor) if extractor else None,
    }
