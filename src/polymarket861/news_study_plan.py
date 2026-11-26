"""Prospective, descriptive paired-news study and its probability-era identity."""

import json
import re
from datetime import timedelta

from . import recipe_identity, store

FIRST_SEASON = 2026
ORIGINAL_PLAN = {
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
ORIGINAL_PLAN_SHA256 = store.digest(ORIGINAL_PLAN)
PREVIOUS_POLICY = (
    "epl-player-news-paired-v2",
    "b973656b0a32354e301e03807af5a639c0fa32aee245e4662a15908db4dc386a",
)
PLAN = {
    **{key: ORIGINAL_PLAN[key] for key in (
        "first_season", "stopping", "market_decision", "reference", "coverage_age_hours",
        "direction", "direction_absolute_tolerance", "unit", "family_confidence", "authority",
    )},
    "version": "epl-news-observed-pairs-v2",
    "population": "unique prospective primary seals with timely qualified coverage for both clubs; full coverage reported separately",
    "strata": ["mode", "decision horizon", "probability era", "verified actual or scheduled pregame clock"],
    "market_close": "same native contract, token, rules and two-sided books; last qualified receipt in the final 30 seconds strictly before its clock and after sealing",
    "clock_tiers": {
        "market_close": "independently verified first-half kickoff",
        "scheduled_market_close": "frozen scheduled kickoff with unchanged local schedule, original fresh explicitly pregame exchange state at the closing book and no previously observed start/end",
    },
    "scheduled_state_max_age_seconds": 30,
    "primary_market_estimand": "home win; through-origin slope of (close minus B1) on (news minus B1), per tier",
    "secondary_market_estimand": "absolute distance gain in percentage points and its direction",
    "score": "with-news minus no-news four-component score on verified regulation results; upper simultaneous bound below zero flags deterioration",
    "guardrail": "descriptive only; no minimum positive gain or automatic promotion",
    "interval": "round-clustered slope sandwich CR1 uses G/(G-1)*(N-1)/(N-P) and Student G-1. G counts season-rounds and N pairs with absolute slope predictor above direction_absolute_tolerance after within-era centering for an approved pooled fit. P is one slope plus the informative era intercepts for a pooled fit. Fewer than two informative rounds, N<=P or zero residual variance leaves the interval unavailable. The outcome guardrail uses all scored rounds.",
    "family_comparisons": 6,  # two slopes and one outcome safeguard, at two horizons
    "geometry": "all qualified pairs including zero news moves retained; a zero move provides no slope information",
}
PLAN_SHA256 = store.digest(PLAN)
LEGACY_POLICY = (
    "epl-player-news-paired-v1",
    "fac9ee74e81e1e3d4bb2374b07815cc849e6076c44d00c06721a9b0322f40f3f",
)

# A complete footprint vouches for the operational route from a native reply to
# a time-bound player fact. Earlier manifests identified only selected adapters
# and writers. Their stored identities remain meaningful within their own era.
INPUT_CONTRACT = "epl-news-native-dispatch-to-frozen-facts-v1"
INPUT_CONTRACT_KEY = "news_input_contract"
_MODEL_FILES = (
    "football_model.py",
    "score_bounds.py",
    "news_model.py",
    "news_importance.py",
    "news_wire_identity.py",
    "news_ai.py",
    "news_ai_corpus.py",
    "news_scope.py",
    "news_local_ai.py",
)
_SYMBOLS = {
    "football.py": ("FootballEngine", "load_fixtures", "history_kickoff"),
    "pipeline.py": ("_forecast_arms", "_with_schedule_revisions"),
    # Source registration, scheduling, dispatch, ordinary writes and the as-of
    # reader can change different stages of the same SQLite-backed input.
    "news.py": ("add_feed", "disable_feed", "sync", "snapshot"),
    "news_sources.py": (
        "WIRE_CLUBS",
        "poll",
        "active_sources",
        "validate_source",
        "wire_claim",
        "wire_fingerprint",
        "wiki_prose",
    ),
    "news_roster.py": ("roster_views", "as_of", "refresh"),
    "news_minutes.py": ("parse", "latest", "club_games", "verified_players", "same_snapshot", "refresh"),
    "evidence.py": ("_collect", "_selected_lineup"),
    "evidence_sources.py": ("name_key", "source_at", "granted", "required", "utc", "dump"),
    "jobs_research.py": ("_pointer",),
    "blob_storage.py": ("read_blob",),
    "news_poll_archive.py": ("SCHEMA", "history", "verified_check", "wire_coverage_received", "track"),
    "schedule_consensus.py": ("readiness", "eligible_news", "require_news"),
    "result_freshness.py": ("_AS_OF",),
    # A club's completed-match denominator is itself an input to the frozen
    # player-usage weights. Include provider identity, regulation score bounds
    # (a dedicated model file), ordered fixture uniqueness and club matching.
    "result_pairings.py": ("decision", "unique_fixtures"),
    "result_admission.py": ("_raw_rows", "_pair"),
    "result_feed.py": ("_fixture_rows", "BASE"),
    "independent_results.py": ("verified", "_query", "_title", "_url", "_get"),
    "opportunity.py": ("_club",),
}

# Dispatch completes for every club before passing immutable copies to optional
# research retention. The reporting functions consume completed operational rows.
_STORAGE_ONLY = {
    "news.py": {"_db", "_retain_endpoint_originals", "_status_db", "_publish_research_health"},
    "news_roster.py": {"_connection"},
    "news_minutes.py": {"_connection"},
    "schedule_consensus.py": {"_db"},
}


def _symbol_digest(path, physical_sha, roots):
    return recipe_identity.symbols_digest(
        path, physical_sha, roots, tuple(sorted(_STORAGE_ONLY.get(path.name, ())))
    )


def probability_sources(root, files):
    """Stored alongside, and independently of, the complete physical-release manifest."""
    complete = (set(_MODEL_FILES) | _SYMBOLS.keys()) <= files.keys()
    sources = {}
    for name in _MODEL_FILES:
        if name in files:
            try:
                sources[name] = recipe_identity.module_digest(root / name, files[name])
            except SyntaxError:
                if complete:
                    raise
                sources[name] = files[name]
    for name, names in _SYMBOLS.items():
        if name in files:
            try:
                sources[name] = _symbol_digest(root / name, files[name], names)
            except SyntaxError:
                if complete:
                    raise
                # Inventory tools can register partial, non-executable source bytes.
                # Preserve their physical digest. No era replay can be certified.
                sources[name] = files[name]
    if complete:
        sources[INPUT_CONTRACT_KEY] = store.digest(INPUT_CONTRACT)
    return sources


PAIR_RECIPE_KIND = "paired-news-recipe"


def sealed_recipe(db, seal):
    row = db.execute(
        "SELECT recipe FROM model_recipes WHERE version=?", (seal["model_version"],)
    ).fetchone()
    primary = json.loads(row[0]) if row else None
    if not primary or store.digest(primary) != seal["recipe_digest"]:
        raise ValueError("missing original model recipe")
    companion = seal.get("news_recipe_sha256")
    if companion is None:
        if primary.get("recipe_format") and "news_probability_source" not in primary:
            raise ValueError("missing original paired news recipe")
        return primary
    row = db.execute(
        "SELECT payload FROM reports WHERE kind=? AND key=?", (PAIR_RECIPE_KIND, companion)
    ).fetchone()
    recipe = json.loads(row[0]) if row else None
    if not recipe or store.digest(recipe) != companion or any(recipe.get(k) != v for k, v in primary.items()):
        raise ValueError("missing or inconsistent original paired news recipe")
    return recipe


def extractor_identity(snapshot):
    # A fully processed empty source is evidence too. Reading environment settings
    # at grading time would relabel old analyses after an operator changes models.
    stamps = [
        s.get("extractor_config_sha256")
        for side in ("home", "away")
        for s in snapshot.get("coverage", {}).get(side, {}).get("sources", [])
        if s.get("study_eligible", s.get("availability_observed"))
    ]
    stamps.extend(f.get("extractor_config_sha256") for f in snapshot.get("facts", []))
    distinct = set(stamps)
    if len(distinct) == 1:
        ident = distinct.pop()
        if isinstance(ident, str) and re.fullmatch(r"[0-9a-f]{64}", ident):
            return ident
    return None


def _study_source(source):
    if source.get("status") in ("ok", "degraded"):
        return source.get("availability_observed", source.get("status") == "ok") is True
    return (source.get("id") == "touchline-availability" and source.get("status") == "partial"
            and source.get("study_eligible") is True and source.get("directory_evidence_verified") is True
            and bool(source.get("provider_identity_reviews")) and source.get("fetch_complete") is True
            and all(source.get(key) == 0 for key in
                    ("unresolved_players", "invalid_entries", "rejected_items", "revision_conflicts")))


def timely_coverage(pair):
    cut = store.instant(pair["input_cutoff"])
    lower = cut - timedelta(hours=PLAN["coverage_age_hours"])
    for side in ("home", "away"):
        sources = pair["coverage"].get(side, {}).get("sources", [])
        if not any(
            _study_source(s)
            and s.get("checked_at")
            and lower
            <= store.instant(s.get("source_observed_at") or s["checked_at"])
            <= store.instant(s["checked_at"])
            <= cut
            for s in sources
        ):
            return False
    return pair["coverage"].get("both_teams_study_qualified", pair["coverage"].get("both_teams_observed")) is True


def era(recipe, pair, extractor):
    return store.digest(
        {
            "probability_source": recipe["news_probability_source"],
            **({"primary_sources": recipe["algorithm_source"]} if recipe.get("recipe_format") else {}),
            **({"frozen_feature_source": recipe["news_frozen_feature_source"]} if "news_frozen_feature_source" in recipe else {}),
            "numeric_runtime": recipe["numeric_runtime"],
            "lines": recipe["lines"],
            "protocol": pair["protocol"],
            "policy_sha256": pair["policy_sha256"],
            "availability_basis": pair["availability_basis"],
            "extractor_config_sha256": extractor,
        }
    )


def declaration(recipe, pair, snapshot):
    # Recipes written by 0.4.1 predate this explicit binding and used exactly v1.
    digest = recipe.get("news_study_plan_sha256", ORIGINAL_PLAN_SHA256)
    versions = {ORIGINAL_PLAN_SHA256: ORIGINAL_PLAN["version"], PLAN_SHA256: PLAN["version"]}
    if digest not in versions:
        raise ValueError("unknown preregistered news study")
    extractor = extractor_identity(snapshot)
    return {
        "plan_version": versions[digest],
        "plan_sha256": digest,
        "extractor_config_sha256": extractor,
        "era_sha256": era(recipe, pair, extractor) if extractor else None,
    }
