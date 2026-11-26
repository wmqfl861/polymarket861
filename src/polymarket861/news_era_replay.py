"""Bit-exact comparisons on complete, finite inventories of sealed news pairs."""

import hashlib
import json
import math
import re
import struct
from collections import Counter
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from . import football, input_manifest, news_study_plan, recipe_identity, store

METHOD = "sealed-news-arms-binary64-v1"
KIND = "news-era-replay"
# These functions run again from the original match history and frozen facts.
# Everything else that selected or transformed the frozen facts must agree.
_REPLAYED = {
    "football.py",
    "football_model.py",
    "pipeline.py",
    "news_model.py",
}

# Used while constructing frozen facts as well as later in the predictor. A
# model fit on old derived facts cannot establish that these selectors agree.
_MIXED_FEATURES = {
    "football.py": ("team_key", "fixture_id", "season_year"),
    "news_model.py": ("usable_wire_observation",),
    "store.py": ("instant", "iso", "now_utc", "canonical", "digest", "read_blob"),
}


def feature_sources(root, sources, physical, input_sources=None):
    """Exclude comments in feature modules; keep the semantic selectors of mixed modules."""
    required = set(news_study_plan._MODEL_FILES) | set(news_study_plan._SYMBOLS)
    if (
        sources.get(news_study_plan.INPUT_CONTRACT_KEY) != store.digest(news_study_plan.INPUT_CONTRACT)
        or required.difference(sources)
        or (required | _MIXED_FEATURES.keys()).difference(physical)
    ):
        # Physical version registration also supports partial package manifests.
        # An explicit null preserves that provenance without attesting completeness.
        return None
    selected = {
        name: recipe_identity.module_digest(root / name, physical[name])
        if name in news_study_plan._MODEL_FILES
        else value
        for name, value in sources.items()
        if name not in _REPLAYED
    }
    for name, value in (input_sources or {}).items():
        if name not in _REPLAYED:
            selected["input:" + name] = value
    for name, roots in _MIXED_FEATURES.items():
        selected[name] = news_study_plan._symbol_digest(
            root / name, physical[name], roots
        )
    return selected


def _same_features(original, candidate):
    if not candidate.get("news_frozen_feature_source"):
        return False
    if "news_frozen_feature_source" in original:
        return (
            original["news_frozen_feature_source"]
            == candidate["news_frozen_feature_source"]
        )

    # Older manifests lack the comment-independent fingerprint. Exact source
    # matches still prove the guard when the complete feature key set agrees.
    def original_features(recipe):
        return {
            k: v
            for k, v in recipe["news_probability_source"].items()
            if k not in _REPLAYED
        }

    return original_features(original) == original_features(candidate) and all(
        name in original["algorithm_source"]
        and name in candidate["algorithm_source"]
        and original["algorithm_source"][name] == candidate["algorithm_source"][name]
        for name in _MIXED_FEATURES
    )


def _scope(season, horizon, mode):
    first = news_study_plan.FIRST_SEASON
    last = football.season_year(season)
    if last < first or season != f"{last}-{(last + 1) % 100:02d}":
        raise ValueError("choose a through-season starting in 2026-27 or later")
    if mode not in ("technical", "formal") or horizon not in ("T-24h", "T-60m"):
        raise ValueError("era replay requires one prospective mode and fixed horizon")
    return {
        "first_season": f"{first}-{(first + 1) % 100:02d}",
        "through_season": season,
        "horizon": horizon,
        "mode": mode,
        "plan_sha256": news_study_plan.PLAN_SHA256,
    }


def _inventory(db, scope):
    """The schedule registry bounds season membership independently of payload metadata."""
    rows = db.execute(
        """
        SELECT s.* FROM forecast_seals s WHERE s.horizon=? AND s.mode=? AND s.protocol=?
        AND EXISTS (SELECT 1 FROM fixture_versions f WHERE f.fixture_id=s.fixture_id
          AND f.season BETWEEN ? AND ?) ORDER BY s.digest
    """,
        (
            scope["horizon"],
            scope["mode"],
            store.PROTOCOL,
            scope["first_season"],
            scope["through_season"],
        ),
    )
    from . import pipeline

    pairs, recipes = [], {}
    for row in rows:
        payload = pipeline._verified_seal(row)
        del payload["digest"]
        if payload.get("purpose") != "main":
            continue
        # Several paired news eras may run against one unchanged B1. Validate
        # and cache the two identities that were actually recorded on this seal.
        key = (payload["model_version"], payload["recipe_digest"], payload.get("news_recipe_sha256"))
        if key not in recipes:
            recipes[key] = _recipe(db, payload)
        recipe = recipes[key]
        if "news_probability_source" not in recipe:
            continue  # No prospective research era existed on these old receipts.
        # Classify by archived recipe and frozen inputs rather than allowing a
        # deleted/edited declaration to remove a disagreeing seal from the census.
        expected = news_study_plan.declaration(
            recipe, payload["news_pair"], payload["context"]["player_news"]
        )
        declaration = payload.get("news_study")
        if declaration != expected:
            raise ValueError("missing_or_invalid_declaration_in_inventory")
        if expected["era_sha256"]:
            pairs.append((expected["era_sha256"], row, payload))
    return pairs


def _ids(items):
    return [row["digest"] for _, row, _ in items]


def _recipe(db, payload):
    return news_study_plan.sealed_recipe(db, payload)


def _projection(pair, lines):
    """Encode exact IEEE-754 values, including signed zero, in a fixed, public order."""
    ordered_lines = [str(x) for x in sorted(map(float, lines))]
    digest = hashlib.sha256()
    count = 0
    for arm_name in ("no_news", "with_news"):
        arm = pair[arm_name]
        if set(arm["probabilities"]) != set(football.PROB_KEYS) or set(
            arm["totals"]
        ) != {"total", "home", "away"}:
            raise ValueError("incomplete_paired_probability_vector")
        values = [arm["probabilities"][key] for key in football.PROB_KEYS]
        for family in ("total", "home", "away"):
            totals = arm["totals"][family]
            if set(totals) != set(ordered_lines):
                raise ValueError("incomplete_configured_goal_lines")
            for line in ordered_lines:
                if set(totals[line]) != {"over", "under"}:
                    raise ValueError("missing_total_direction")
                values.extend(totals[line][key] for key in ("over", "under"))
        for value in values:
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError("invalid_probability_value")
            digest.update(struct.pack(">d", value))
        count += len(values)
    return digest.hexdigest(), count


def _original(db, era, entry, payload, candidate, cache):
    from . import news_evaluation

    pair = payload["news_pair"]
    declaration = payload["news_study"]
    if declaration["plan_sha256"] != news_study_plan.PLAN_SHA256:
        raise ValueError("different_study_plan")
    recipe = _recipe(db, payload)
    if recipe.get("news_probability_policy") != candidate["news_probability_policy"]:
        raise ValueError("different_news_policy")
    if not _same_features(recipe, candidate):
        raise ValueError("different_frozen_feature_semantics")
    if recipe["family"] != candidate["family"] or recipe["lines"] != candidate["lines"]:
        raise ValueError("different_model_family_or_lines")
    fx = db.execute(
        """
        SELECT f.* FROM fixture_versions f JOIN schedule_receipts s USING(fixture_id,version)
        WHERE f.fixture_id=? AND f.known_at<=? AND s.recorded_at<=? ORDER BY f.known_at DESC LIMIT 1
    """,
        (entry["fixture_id"], pair["input_cutoff"], pair["input_cutoff"]),
    ).fetchone()
    if not fx or fx["fixture_id"] != football.fixture_id(
        fx["season"], fx["home"], fx["away"]
    ):
        raise ValueError("missing_original_league_fixture")
    issue = news_evaluation._validate(db, dict(fx), entry, payload, cache, None)
    if issue:
        raise ValueError(issue)
    checked_era, status = news_evaluation._build(db, payload, cache)
    if status != "declared" or checked_era != era:
        raise ValueError("unverified_original_declaration")
    return fx, pair


def _replay_one(root, db, era, entry, payload, candidate, target, cache):
    from . import pipeline

    fx, pair = _original(db, era, entry, payload, candidate, cache)
    proof = payload["comparison_proof"]
    context = payload[
        "context"
    ]  # _validate authenticated the independent frame and exact context hash
    declared = news_study_plan.declaration(candidate, pair, context["player_news"])
    if not target or declared["era_sha256"] != target:
        raise ValueError("different_extractor_or_availability_basis")
    snapshot = proof["snapshot"]
    history_key = ("history", snapshot)
    if history_key not in cache:
        manifest = json.loads(
            db.execute(
                "SELECT manifest FROM football_input_manifests WHERE digest=?",
                (snapshot,),
            ).fetchone()[0]
        )
        # Reopen raw Git blobs and the original timing sidecar, never latest.
        view = input_manifest._materialize(root, manifest, snapshot)
        cache[history_key] = football.load_fixtures(
            view["root"], evidence_path=view["evidence_path"]
        )
    cutoff = store.instant(proof["input_cutoff"])
    kickoff = store.instant(fx["kickoff"])
    history = pipeline._with_schedule_revisions(
        cache[history_key],
        fx["fixture_id"],
        kickoff,
        cutoff,
        proof["schedule_revisions"],
    )
    model, replayed = pipeline._forecast_arms(
        football.FootballEngine(history),
        fx["home"],
        fx["away"],
        kickoff,
        cutoff,
        fx["season"],
        candidate,
        context,
        include_pair=True,
    )
    if (
        model["evidence"]["input_sha256"] != proof["training"]
        or replayed["facts_sha256"] != pair["facts_sha256"]
    ):
        raise ValueError("different_training_or_news_inputs")
    before, count = _projection(pair, candidate["lines"])
    after, _ = _projection(replayed, candidate["lines"])
    return {
        "era": era,
        "seal": entry["digest"],
        "fixture_id": fx["fixture_id"],
        "input_cutoff": proof["input_cutoff"],
        "original_recipe_sha256": payload["recipe_digest"],
        **({"original_news_recipe_sha256": payload["news_recipe_sha256"]} if "news_recipe_sha256" in payload else {}),
        "frame_sha256": proof["frame"],
        "manifest_sha256": snapshot,
        "training_sha256": proof["training"],
        "facts_sha256": pair["facts_sha256"],
        "sealed_binary64_sha256": before,
        "replayed_binary64_sha256": after,
        "probability_values": count,
        "status": "bit_identical" if before == after else "different_probabilities",
    }


def _run(root, db, scope, source, pairs):
    from . import pipeline

    report = {
        "method": METHOD,
        "scope": scope,
        "source_era": source,
        "target_era": None,
        "candidate_recipe_sha256": None,
        "records": [],
        "eligible_for_pooling": False,
    }
    source_pairs = [p for p in pairs if p[0] == source]
    if not source_pairs:
        return report | {"status": "no_sealed_source_pairs"}
    first = source_pairs[0][2]
    recipe = _recipe(db, first)
    candidate = pipeline._study_recipe(pipeline._build_recipe(recipe["family"], recipe["lines"]))
    report["candidate_recipe_sha256"] = store.digest(candidate)
    if not candidate.get("news_frozen_feature_source") or (
        "news_frozen_feature_source" in recipe
        and not recipe["news_frozen_feature_source"]
    ):
        return report | {"status": "incomplete_feature_manifest"}
    if recipe.get("news_probability_policy") != candidate["news_probability_policy"]:
        return report | {"status": "different_news_policy"}
    if first["news_study"]["plan_sha256"] != scope["plan_sha256"]:
        return report | {"status": "different_study_plan"}
    decl = news_study_plan.declaration(
        candidate, first["news_pair"], first["context"]["player_news"]
    )
    target = decl["era_sha256"]
    report["target_era"] = target
    if target is None or target == source:
        return report | {
            "status": "same_era" if target else "unknown_extractor_configuration"
        }
    selected = [p for p in pairs if p[0] in (source, target)]
    report["inventory_sha256"] = store.digest(_ids(selected))
    report["inventory_count_by_era"] = dict(
        sorted(Counter(p[0] for p in selected).items())
    )
    cache = {}
    for era, entry, payload in selected:
        try:
            audit = _replay_one(root, db, era, entry, payload, candidate, target, cache)
        except (
            ValueError,
            OSError,
            KeyError,
            IndexError,
            TypeError,
            OverflowError,
        ) as exc:
            audit = {
                "era": era,
                "seal": entry["digest"],
                "fixture_id": entry["fixture_id"],
                "status": "unverifiable",
                "reason": str(exc),
            }
        report["records"].append(audit)
    counts = Counter(r["status"] for r in report["records"])
    return report | {
        "status": "bit_identical"
        if counts == {"bit_identical": len(selected)}
        else "not_equivalent",
        "record_status_counts": dict(sorted(counts.items())),
        "eligible_for_pooling": counts == {"bit_identical": len(selected)},
    }


def _evidence_present(root, db, selected, expected):
    """A certificate refers to original, currently verifiable inputs as well as outputs."""
    from . import news_evaluation

    cache = {}
    verified = set()
    for (era, entry, payload), prior in zip(selected, expected, strict=True):
        if (prior["era"], prior["seal"], prior["status"]) != (
            era,
            entry["digest"],
            "bit_identical",
        ):
            raise ValueError("replay receipt or era changed")
        proof = payload["comparison_proof"]
        fx = {
            "fixture_id": entry["fixture_id"],
            "home": payload["home"],
            "away": payload["away"],
            "kickoff": entry["kickoff"],
            "season": payload["season"],
            "round_id": payload["round_id"],
        }
        if news_evaluation._proof_issue(db, fx, entry, payload, cache):
            raise ValueError("archived frame or manifest is no longer verifiable")
        _recipe(db, payload)
        snapshot = proof["snapshot"]
        if snapshot in verified:
            continue
        manifest = news_evaluation._document(
            db, "football_input_manifests", "manifest", snapshot
        )
        for sha in [
            manifest["evidence_sha256"],
            *(f["sha256"] for f in manifest["files"]),
        ]:
            if sha not in verified:
                file = store.blob_path(root, sha)
                stat = file.stat()
                # Cache by physical file identity only. Rotation, compression, or
                # modification triggers revalidation against the original bytes.
                if not _stored_bytes_match(
                    root,
                    sha,
                    file,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                ):
                    raise ValueError("damaged archived football input")
                verified.add(sha)
        verified.add(snapshot)


@lru_cache(maxsize=256)
def _stored_bytes_match(root, sha, path, inode, size, mtime, ctime):
    return hashlib.sha256(store.read_blob(root, sha)).hexdigest() == sha


def compare(root, source, season, horizon="T-60m", mode="technical"):
    if not isinstance(source, str) or not re.fullmatch("[0-9a-f]{64}", source):
        raise ValueError("source era must be the full SHA-256 in the cumulative report")
    scope = _scope(season, horizon, mode)
    with closing(store.connect(root)) as db, db:
        # Expensive model fits run against one WAL read snapshot without holding
        # the matchday writer. Installation of the certificate rechecks all rows.
        db.execute("BEGIN")
        try:
            pairs = _inventory(db, scope)
            report = _run(root, db, scope, source, pairs)
        except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
            return {
                "method": METHOD,
                "scope": scope,
                "source_era": source,
                "status": "unverifiable_inventory",
                "eligible_for_pooling": False,
                "reason": str(exc),
            }
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        if report.get("inventory_sha256"):
            try:
                current = [
                    p
                    for p in _inventory(db, scope)
                    if p[0] in (source, report["target_era"])
                ]
                if store.digest(_ids(current)) != report["inventory_sha256"]:
                    raise ValueError("new or changed seal")
                if report.get("eligible_for_pooling"):
                    from . import pipeline

                    recipe = _recipe(db, current[0][2])
                    if (
                        store.digest(
                            pipeline._study_recipe(pipeline._build_recipe(recipe["family"], recipe["lines"]))
                        )
                        != report["candidate_recipe_sha256"]
                    ):
                        raise ValueError("installed candidate changed")
                    _evidence_present(root, db, current, report["records"])
            except (ValueError, OSError, TypeError, KeyError, IndexError) as exc:
                return report | {
                    "status": "inventory_changed_during_replay",
                    "eligible_for_pooling": False,
                    "reason": str(exc),
                }
        # Each comparison is immutable, including failed attempts. Repeating the
        # same calculation returns the same certificate and keeps its first date.
        sha = store.digest(report)
        if not db.execute(
            "SELECT 1 FROM reports WHERE kind=? AND key=?", (KIND, sha)
        ).fetchone():
            store.put_report(db, KIND, sha, report)
        pointer = {
            "scope": scope,
            "source_era": source,
            "target_era": report.get("target_era"),
        }
        store.put_report(
            db,
            KIND + "-latest",
            store.digest(pointer),
            pointer | {"report_sha256": sha},
        )
    return report | {"report_sha256": sha}


def accumulation(db, records, seasons, horizon, mode, result):
    """Combine only directly compared inventories. No transitive or future equivalence."""
    from . import news_accumulation, news_market

    scope = _scope(seasons[-1], horizon, mode)
    output = {
        "method": METHOD,
        "scope": scope,
        "comparisons": [],
        "scope_note": "Each comparison covers only its complete recorded seal inventory. Overlapping comparisons are separate estimates.",
    }
    saved = db.execute(
        "SELECT key,payload FROM reports WHERE kind=? ORDER BY updated_at DESC,key",
        (KIND + "-latest",),
    )
    inventory = None
    for row in saved:
        try:
            pointer = json.loads(row["payload"])
            sha = pointer.pop("report_sha256")
            if store.digest(pointer) != row["key"]:
                raise ValueError("changed replay certificate pointer")
            if pointer["scope"] != scope:
                continue
            stored = db.execute(
                "SELECT payload FROM reports WHERE kind=? AND key=?", (KIND, sha)
            ).fetchone()
            cert = json.loads(stored[0]) if stored else {}
            if (
                store.digest(cert) != sha
                or cert.get("method") != METHOD
                or any(cert.get(k) != v for k, v in pointer.items())
            ):
                raise ValueError("missing or changed replay certificate")
        except (KeyError, ValueError, TypeError):
            output["comparisons"].append(
                {"status": "invalid_replay_certificate", "pooled": None}
            )
            continue
        eras = (cert["source_era"], cert["target_era"])
        summary = {
            "report_sha256": sha,
            "source_era": eras[0],
            "target_era": eras[1],
            "status": cert["status"],
            "pooled": None,
        }
        output["comparisons"].append(summary)
        if not cert.get("eligible_for_pooling") or cert["status"] != "bit_identical":
            continue
        try:
            if inventory is None:
                inventory = _inventory(db, scope)
            selected = [p for p in inventory if p[0] in eras]
            ids = _ids(selected)
            if (
                not ids
                or store.digest(ids) != cert["inventory_sha256"]
                or ids != [r["seal"] for r in cert["records"]]
            ):
                summary["status"] = "inventory_changed_replay_required"
                continue
            _evidence_present(
                Path(db.execute("PRAGMA database_list").fetchone()[2]).parent,
                db,
                selected,
                cert["records"],
            )
            id_set = set(ids)
            included = [
                r
                for r in records
                if r.get("seal") in id_set
                and r.get("implementation") in eras
                and r.get("study_status") == "declared"
                and r.get("timely_news_coverage")
                and r.get("study_plan_sha256") == scope["plan_sha256"]
            ]
            if {r["implementation"] for r in included} != set(eras):
                summary["status"] = "awaiting_eligible_seals_in_both_eras"
                continue
            if len({r["fixture_id"] for r in included}) != len(included):
                summary["status"] = "overlapping_fixtures"
                continue
            pooled = news_accumulation._era(
                included, result["market_close"]["source_capability"], mode
            )
            pooled["fixed_effect_slopes"] = {
                field: news_market.regression(included, field, fixed_effects=True)
                for field in ("market_close", "scheduled_market_close")
            }
            summary["pooled"] = pooled
        except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
            summary["status"] = "unverifiable_inventory"
            summary["reason"] = str(exc)
    return output


def register(commands):
    command = commands.add_parser(
        "compare-eras", help="用当前程序逐位重放所选范围内封存的两组完整概率"
    )
    command.add_argument("source_era", help="原研究概率纪元的完整 SHA-256")
    command.add_argument("--through-season", required=True)
    command.add_argument("--horizon", choices=("T-24h", "T-60m"), default="T-60m")
    command.add_argument("--mode", choices=("technical", "formal"), default="technical")
    command.set_defaults(
        func=lambda a: compare(
            a.data_dir, a.source_era, a.through_season, a.horizon, a.mode
        )
    )
