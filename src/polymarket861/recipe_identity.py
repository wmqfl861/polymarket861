"""Content identities for declared probability-producing code and input rules."""

import ast
import hashlib
from functools import lru_cache

from . import store


def _tree(node):
    # CPython added empty fields (e.g. type_params in 3.12) and changed
    # ast.dump's display defaults in 3.13. Encode the actual syntax ourselves.
    # Field names distinguish an empty true suite from an empty else suite.
    if isinstance(node, ast.AST):
        return [
            type(node).__name__,
            {
                key: _tree(value)
                for key, value in ast.iter_fields(node)
                if value is not None and value != []
            },
        ]
    if isinstance(node, list):
        return [_tree(value) for value in node]
    if isinstance(node, (bytes, complex)) or node is Ellipsis:
        return [type(node).__name__, repr(node)]
    return node


@lru_cache(maxsize=256)
def _module(path, expected):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("source changed while building the probability recipe")
    return ast.parse(raw, filename=str(path))


@lru_cache(maxsize=256)
def module_digest(path, physical):
    return store.digest(_tree(_module(path, physical)))


def bindings(module):
    """Module bindings used by both recipe construction and its dependency check."""
    definitions = {}
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                        definitions[name.id] = node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            prefix = (
                "." * node.level + (node.module or "")
                if isinstance(node, ast.ImportFrom)
                else ""
            )
            for name in node.names:
                binding = name.asname or name.name.split(".")[0]
                definitions[binding] = [prefix, name.name]
    return definitions


def selected_bindings(definitions, roots, ignored=()):
    """Reachable local definitions, defaults, annotations, decorators and imports."""
    selected = {}
    pending = set(roots)
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        node = definitions.get(name)
        selected[name] = node
        if isinstance(node, ast.AST):
            pending.update(
                ref.id
                for ref in ast.walk(node)
                if isinstance(ref, ast.Name)
                and ref.id in definitions
                and ref.id not in ignored
            )
    return selected


@lru_cache(maxsize=512)
def symbols_digest(path, physical, roots, ignored=()):
    """Include decorators, defaults, globals and transitively used local definitions."""
    selected = selected_bindings(bindings(_module(path, physical)), roots, ignored)
    return store.digest({
        name: _tree(node) if isinstance(node, ast.AST) else node
        for name, node in selected.items()
    })


# Entry points that compute probabilities, admit/cut off inputs or freeze
# eligibility. Independent collectors, market discovery, staking and reports
# retain their complete physical identity in release provenance.
_PRIMARY = {
    "football_model.py": ("fit_team_model", "_goals_matrix", "probabilities"),
    "football.py": ("FootballEngine", "load_fixtures", "history_kickoff", "SOURCE_COMMIT", "apply_context"),
    "score_bounds.py": ("valid_score", "INVALID_SCORE"),
    "pipeline.py": ("make_forecast",),
    # The DB and archive are bridges between processes: both the upstream
    # writers deciding the values/receipt clocks and the downstream selectors
    # belong to the recipe even when there is no Python call between them.
    "datasets.py": ("refresh", "observe"),
    "input_manifest.py": ("create_observed_inputs", "verify_observed_inputs"),
    "paired_proof.py": (
        "attest_provider_files",
        "capture_history",
        "frozen_manifest",
        "live_frame",
        "make_proof",
        "check_replay_cache",
    ),
    "evidence.py": (
        "as_of_context",
        "import_evidence",
        "open_incident",
        "resolve_incident",
        "link_lineage_db",
    ),
    "evidence_sources.py": (
        "register_source",
        "source_at",
        "name_key",
        "granted",
        "required",
        "utc",
        "digest",
    ),
    # These imports route the forecast to its model selector. Sign the re-export
    # as well as the destination, so rerouting to another signed helper is visible.
    "jobs.py": ("active_version", "model_for_decision"),
    "jobs_research.py": ("active_version", "model_for_decision", "_pointer"),
    "schedule_consensus.py": (
        "sync",
        "review_schedule",
        "require_fixed",
        "result_links",
    ),
    "result_pairings.py": ("decision", "unique_fixtures", "signature", "SOURCE"),
    "result_admission.py": ("reconcile", "review_pairing", "_raw_rows", "_pair"),
    "result_feed.py": ("sync", "_fixture_rows"),
    "result_operations.py": ("manual", "rollback", "_fallback"),
    "result_training_review.py": ("describe", "validate", "bound", "still_applies"),
    "independent_results.py": ("sync", "verified"),
    "opportunity.py": ("_club",),
    # Consensus re-reads native Gamma receipts for the kickoff itself. Include
    # their receipt times and identities, independently of price/strategy code.
    "market_archive.py": ("_fetch_once", "observe_event", "catalog_window"),
    "market_provider.py": ("get", "FIXTURE", "GAMMA", "normalized_markets"),
    "market_http.py": ("read",),
    "ledger_terms.py": ("_interpret",),
    "ledger_rules.py": ("instant", "number"),
    "blob_storage.py": ("read_blob", "decoded"),
    "archive_proof.py": ("original_receipt", "verify_membership_receipt", "_body", "_event_capture"),
    "dbscope.py": ("verified_bytes", "verify_content", "recheck_originals", "bind", "for_connection", "reading", "checked"),
    "store.py": (
        "archive_bytes",
        "register_fixture",
        "register_fixture_db",
        "record_result",
        "record_result_db",
        "eligible_fixture_rows",
        "eligible_fixtures",
        "builtin_model_at",
        "fixture_at",
        "ensure_unfinished_at",
        "require_formal",
        "instant",
        "iso",
        "digest",
    ),
}
_INFRASTRUCTURE = {
    # Registration, physical provenance and companion study identity do not
    # calculate the probabilities or decide which features reach the model.
    "pipeline.py": (
        "_recipe",
        "prepare_builtin",
        "_build_recipe",
        "_study_recipe",
        "_retain_manifest",
        "_decision_reason",
    ),
    "datasets.py": ("_publish_observation",),
    "result_operations.py": ("publications",),
    "result_admission.py": ("database", "enqueue"),
    "result_feed.py": ("_db",),
    "evidence_sources.py": ("db", "publish_source"),
    "evidence.py": ("_db",),
    "input_manifest.py": ("_db",),
    "schedule_consensus.py": ("_db",),
    "paired_proof.py": ("_open",),
    "store.py": ("connect",),
}


def primary_roots(family):
    for name, roots in _PRIMARY.items():
        if family == "B2" and name == "football_model.py":
            roots += ("apply_context",)
        yield name, roots


def primary_sources(root, physical, family):
    selected = {}
    for name, roots in primary_roots(family):
        if name not in physical:
            continue
        try:
            selected[name] = symbols_digest(
                root / name, physical[name], roots, _INFRASTRUCTURE.get(name, ())
            )
        except SyntaxError:
            if _PRIMARY.keys() <= physical.keys():
                raise
            selected[name] = physical[name]
    return selected
