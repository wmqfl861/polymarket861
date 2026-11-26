"""Grade two predeclared news arms on the same fixture, horizon, inputs and final score."""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import timedelta
import hashlib
import json
import math
from pathlib import Path
from statistics import fmean

from . import (
    experiment_statistics,
    football,
    historical_inputs,
    news_market,
    news_model,
    news_study_plan,
    paired_proof,
    result_admission,
    store,
)

HORIZONS = {"T-24h": timedelta(hours=24), "T-60m": timedelta(minutes=60)}
ARMS = ("no_news", "with_news")
ENDPOINTS = ("score", *football.ITEM_KEYS)
MIN_SEASON_PAIRS = (
    361  # Same 95% coverage floor as the 38-round probability research protocol.
)


def _check_scope(season, rnd, horizon, mode):
    football.season_year(season)
    if rnd is not None and (type(rnd) is not int or not 1 <= rnd <= 38):
        raise ValueError("select an official EPL round from 1 through 38")
    if horizon not in HORIZONS or mode not in ("technical", "formal", "replay"):
        raise ValueError(
            "select T-24h or T-60m and technical, formal or replay evidence"
        )


def _document(db, table, column, ident):
    tables = {
        "comparison_snapshots": "document",
        "football_input_manifests": "manifest",
    }
    if tables.get(table) != column:
        raise ValueError("unsupported evidence table")
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
        return None
    raw = db.execute(
        f"SELECT {column} FROM {table} WHERE digest=?", (ident,)
    ).fetchone()
    if not raw:
        return None
    doc = json.loads(raw[0])
    return doc if store.digest(doc) == ident else None


def _strict_history(db, ident):
    """Read archived bytes directly; a new local arrival cannot create old coverage."""
    snap = _document(db, "comparison_snapshots", "document", ident)
    if (
        not snap
        or snap.get("archive_lag_hours") is not None
        or not snap.get("evidence_sha256")
    ):
        return {}
    observed = snap.get("observed_manifest")
    if not observed or not paired_proof._verified_scope(
        db, observed, snap["files"], snap["evidence_sha256"]
    ):
        return {}
    root = Path(db.execute("PRAGMA database_list").fetchone()[2]).parent
    raw_fixtures = []
    for file in snap["files"]:
        raw = store.read_blob(root, file["sha256"])
        if (
            hashlib.sha256(raw).hexdigest() != file["sha256"]
            or football.blob_sha(raw) != file["git_blob"]
        ):
            return {}
        raw_fixtures.extend(
            football._parse_match(file["season"], item, (), None, file["git_blob"])
            for item in json.loads(raw)["matches"]
        )
    side = store.read_blob(root, snap["evidence_sha256"])
    if hashlib.sha256(side).hexdigest() != snap["evidence_sha256"]:
        return {}
    assertions = [json.loads(s) for s in side.splitlines() if s.strip()]
    if historical_inputs._check_rows(
        db, root, raw_fixtures, assertions, snap["evidence_sha256"]
    ):
        return {}
    return {
        f.fixture_id: f
        for f in raw_fixtures
        if f.status != "quarantined" and f.kickoff_precision == "minute"
    }


def _proof_issue(db, fixture, entry, payload, cache):
    proof = paired_proof.from_seal(payload)
    if (
        not proof
        or proof.get("policy") != paired_proof.POLICY
        or proof.get("sha256")
        != store.digest({k: v for k, v in proof.items() if k != "sha256"})
    ):
        return "invalid_shared_input_proof"
    cut = store.iso(proof["input_cutoff"])
    if (
        proof["season"] != fixture["season"]
        or proof["round_id"] != fixture["round_id"]
        or not proof.get("context_timely")
        or not proof["result_grades"]
        or not set(proof["result_grades"]) <= {"archived_upper_bound", "observed"}
        or store.iso(proof["last_result"]) > cut
    ):
        return "unverified_shared_inputs"
    # A later correction to the calendar cannot create evidence of this fixture
    # and opponent existing at the original decision time.
    schedule = db.execute(
        "SELECT f.home,f.away,f.kickoff,s.precision FROM fixture_versions f "
        "JOIN schedule_receipts s USING(fixture_id,version) WHERE f.fixture_id=? "
        "AND f.known_at<=? AND s.recorded_at<=? ORDER BY f.known_at DESC LIMIT 1",
        (fixture["fixture_id"], cut, cut),
    ).fetchone()
    if (
        not schedule
        or schedule["precision"] == "date"
        or any(schedule[k] != fixture[k] for k in ("home", "away", "kickoff"))
    ):
        return "no_contemporaneous_fixture_identity"
    if entry["mode"] == "replay":
        if proof.get("frame") is not None or cut != entry["cutoff"]:
            return "wrong_replay_cutoff"
        scope = ("replay", proof["snapshot"])
        if scope not in cache:
            cache[scope] = _strict_history(db, proof["snapshot"])
        f = cache[scope].get(fixture["fixture_id"])
        if (
            not f
            or f.round != fixture["round_id"]
            or store.iso(f.kickoff) != fixture["kickoff"]
            or (f.home_goals, f.away_goals)
            != (fixture["home_goals"], fixture["away_goals"])
        ):
            return "missing_strict_historical_inputs"
        return None
    scope = ("live", proof["snapshot"])
    if scope not in cache:
        m = _document(db, "football_input_manifests", "manifest", proof["snapshot"])
        cache[scope] = None
        if m and paired_proof._verified_scope(
            db, proof["snapshot"], m["files"], m["evidence_sha256"]
        ):
            cache[scope] = db.execute(
                "SELECT observed_at FROM football_input_manifests WHERE digest=?",
                (proof["snapshot"],),
            ).fetchone()[0]
    if not cache[scope]:
        return "unverified_live_source"
    if cache[scope] > cut:
        return "late_input_manifest"
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='comparison_frames'"
    ).fetchone():
        return "missing_frozen_frame"
    row = db.execute(
        "SELECT digest,document FROM comparison_frames WHERE fixture=? AND kickoff=? AND horizon=? AND mode=? AND protocol=?",
        (
            fixture["fixture_id"],
            fixture["kickoff"],
            entry["horizon"],
            entry["mode"],
            store.PROTOCOL,
        ),
    ).fetchone()
    doc = json.loads(row["document"]) if row else None
    if (
        not row
        or row["digest"] != proof.get("frame")
        or store.digest(doc) != row["digest"]
        or doc["manifest"] != proof["snapshot"]
        or store.iso(doc["cutoff"]) != cut
        or store.digest(doc["context"]) != proof["context"]
        or doc.get("schedules") != proof["schedule_revisions"]
    ):
        return "invalid_frozen_frame"
    return None


def _policy(value, db=None):
    current = (news_model.PROTOCOL, news_model.POLICY_SHA256)
    for identity in (
        current,
        news_study_plan.PREVIOUS_POLICY,
        news_study_plan.LEGACY_POLICY,
    ):
        if value in (identity[0], identity[1], identity[0].rsplit("-", 1)[-1]):
            return identity
    if value in (None, "current"):
        return current
    if db is not None:
        rows = db.execute(
            "SELECT DISTINCT json_extract(recipe,'$.news_probability_policy') FROM "
            "(SELECT recipe FROM model_recipes UNION ALL SELECT payload FROM reports WHERE kind=?) "
            "WHERE json_extract(recipe,'$.news_probability_policy[0]')=? OR json_extract(recipe,'$.news_probability_policy[1]')=?",
            (news_study_plan.PAIR_RECIPE_KIND, value, value),
        ).fetchall()
        if len(rows) == 1:
            return tuple(json.loads(rows[0][0]))
    raise ValueError("unknown sealed news probability policy")


def _validate(db, fixture, entry, payload, cache, policy):
    if entry["digest"] != store.digest(payload) or any(
        entry[k] != payload.get(k)
        for k in (
            "fixture_id",
            "protocol",
            "model_version",
            "mode",
            "kickoff",
            "horizon",
            "cutoff",
            "sealed_at",
        )
    ):
        return "invalid_seal_identity"
    pair = payload.get("news_pair")
    if not isinstance(pair, dict):
        return "no_sealed_news_pair"
    identity = (pair.get("protocol"), pair.get("policy_sha256"))
    if (policy is not None and identity != policy) or pair.get(
        "availability_basis"
    ) != news_model.LOCAL:
        return "different_news_protocol_or_availability_basis"
    if (
        fixture.get("result_kickoff")
        and fixture["result_kickoff"] != fixture["kickoff"]
    ):
        return "changed_actual_kickoff"
    cut = store.iso(payload["input_cutoff"])
    if (
        store.iso(store.instant(fixture["kickoff"]) - HORIZONS[entry["horizon"]])
        != entry["cutoff"]
        or cut != pair["input_cutoff"]
        or cut > entry["cutoff"]
    ):
        return "wrong_horizon_or_input_time"
    earliest = store.iso(store.instant(entry["cutoff"]) - timedelta(minutes=5))
    if (
        entry["mode"] != "replay"
        and not earliest <= cut <= entry["sealed_at"] <= entry["cutoff"]
    ):
        return "late_or_inconsistent_seal"
    snapshot = (payload.get("context") or {}).get("player_news")
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("fixture_id") != fixture["fixture_id"]
        or store.iso(snapshot["cutoff"]) != cut
    ):
        return "missing_frozen_news_snapshot"
    coverage = snapshot.get("coverage") or {"both_teams_observed": False}
    if (
        pair["coverage"] != coverage
        or pair["facts_sha256"] != store.digest(snapshot["facts"])
        or (pair["base_training_sha256"] != payload["evidence"]["input_sha256"])
    ):
        return "different_news_inputs"
    observed = _both_observed(coverage, fixture, cut)
    if observed != bool(coverage.get("both_teams_observed")):
        return "inconsistent_news_coverage"
    if entry["mode"] == "replay" and not observed:
        return "no_contemporaneous_news_coverage"
    if bool(pair["changed"]) != (pair["no_news"] != pair["with_news"]):
        return "inconsistent_news_arms"
    for name in ARMS:
        probs = pair[name]["probabilities"]
        if (
            any(
                type(probs[k]) not in (float, int) or not 0 <= probs[k] <= 1
                for k in football.PROB_KEYS
            )
            or abs(sum(probs[k] for k in ("home_win", "draw", "away_win")) - 1) > 1e-9
        ):
            return "invalid_news_probabilities"
    arm = {"B1": "no_news", "B3-news": "with_news"}.get(payload.get("model"))
    if arm and any(pair[arm][k] != payload[k] for k in ("probabilities", "totals")):
        return "arm_differs_from_sealed_model"
    return _proof_issue(db, fixture, entry, payload, cache)


def _both_observed(coverage, fixture, cutoff):
    for side in ("home", "away"):
        team = coverage.get(side) or {}
        if not team.get("observed") or football.team_key(
            team.get("team", "")
        ) != football.team_key(fixture[side]):
            return False
        if not any(
            x.get("status") in {"ok", "degraded"}
            and x.get("availability_observed", x.get("status") == "ok") is True
            and x.get("checked_at")
            and store.iso(x["checked_at"]) <= cutoff
            and store.iso(x.get("source_observed_at") or x["checked_at"]) <= cutoff
            for x in team.get("sources", [])
        ):
            return False
    return True


def _build(db, seal, cache):
    key = ("recipe", seal["model_version"], seal["recipe_digest"], seal.get("news_recipe_sha256"))
    if key not in cache:
        cache[key] = news_study_plan.sealed_recipe(db, seal)
    recipe = cache[key]
    pair = seal["news_pair"]
    registered_policy = recipe.get(
        "news_probability_policy", news_study_plan.LEGACY_POLICY
    )
    if tuple(registered_policy) != (pair["protocol"], pair["policy_sha256"]):
        raise ValueError("news pair differs from its registered probability policy")
    declaration = seal.get("news_study")
    status = "no_prospective_study_declaration"
    if "news_probability_source" in recipe:
        expected = news_study_plan.declaration(
            recipe, pair, seal["context"]["player_news"]
        )
        if declaration is not None:
            if declaration != expected:
                status = "different_or_invalid_study_declaration"
            elif expected["era_sha256"] is None:
                status = "unknown_or_mixed_extractor_configuration"
            else:
                return expected["era_sha256"], "declared"
    # Prior seals retain their original physical provenance. Never assign them
    # a prospective design or a modern extractor identity after the fact.
    fallback = store.digest(
        {
            "physical": {
                k: recipe[k] for k in ("algorithm_source", "numeric_runtime", "lines")
            },
            "policy_sha256": pair["policy_sha256"],
        }
    )
    return fallback, status


def _line_metrics(pair, home, away):
    scores = {}
    for family, goals in (("total", home + away), ("home", home), ("away", away)):
        keys = set(pair["no_news"]["totals"][family])
        if not keys or keys != set(pair["with_news"]["totals"][family]):
            raise ValueError("paired lines disagree")
        for line in sorted(keys, key=float):
            outcome = goals > football.half_line(line)
            arms = {}
            for arm in ARMS:
                probs = pair[arm]["totals"][family][line]
                over, under = probs["over"], probs["under"]
                if not (
                    0 <= over <= 1 and 0 <= under <= 1 and abs(over + under - 1) <= 1e-9
                ):
                    raise ValueError("invalid sealed half-line probabilities")
                arms[arm] = {
                    "brier": (over - outcome) ** 2,
                    "log_loss": -math.log(max(over if outcome else under, 1e-15)),
                }
            scores[f"{family}/{line}"] = arms
    return scores


def _score_one(fixture, entry, payload, prior, db, cache, mode, verified, policy=None, *, as_of=None, reopen_news=False):
    goals = (fixture["home_goals"], fixture["away_goals"])
    if mode == "replay":
        # Strict replay authenticates its archived season and label in _proof_issue.
        completed = goals[0] is not None
    else:
        completed = bool(
            verified
            and verified["fixture_id"] == fixture["fixture_id"]
            and verified["status"] == "final"
            and verified["period"] == "regulation"
            and verified["result_version"] == fixture["result_version"]
            and verified.get("registry_kickoff", verified["kickoff"])
            == fixture["kickoff"]
            and verified["kickoff"]
            == (fixture.get("result_kickoff") or fixture["kickoff"])
            and (verified["home_goals"], verified["away_goals"]) == goals
        )
    record = {
        "fixture_id": fixture["fixture_id"],
        "season": fixture["season"],
        "round_id": fixture["round_id"],
        "home": fixture["home"],
        "away": fixture["away"],
        "kickoff": fixture["kickoff"],
        "result_version": fixture["result_version"],
        "result_source": fixture.get("result_source"),
        "result_received_at": fixture.get("received_at"),
        "schedule_version": fixture["schedule_version"],
        "completed": completed,
        "sealed": False,
        "scored": False,
    }
    if mode != "replay":
        record["result_admission"] = (
            "verified_regulation" if completed else "awaiting_verified_score"
        )
        if completed:
            record.update(
                result_proof_sha256=store.digest(verified["proof"]),
                result_confirmed_at=verified["confirmed_at"],
                result_admission_source=verified["source"],
            )
        elif fixture.get("result_disposition"):
            record["result_disposition"] = fixture["result_disposition"]
    if prior:
        return record | {"reason": prior}
    try:
        issue = _validate(db, fixture, entry, payload, cache, policy)
        if issue:
            return record | {"reason": issue}
        pair = payload["news_pair"]
        root = Path(db.execute("PRAGMA database_list").fetchone()[2]).parent
        if reopen_news:
            from . import news, news_endpoint_originals

            sealed_news = payload["context"]["player_news"]
            reopened = news.snapshot(root, fixture["fixture_id"], pair["input_cutoff"])
            if store.digest(reopened) != store.digest(sealed_news):
                return record | {"reason": "news_originals_do_not_match_sealed_snapshot"}
            if not news_endpoint_originals.verify(db, root, fixture, reopened):
                return record | {"reason": "missing_or_invalid_news_dependency_original"}
        applied_status = {fact["status"] for fact in pair["applied"]}
        if len(applied_status) > 1:
            news_slice = "confirmed_and_reported"
        elif applied_status:
            news_slice = next(iter(applied_status)) + "_only"
        elif pair["excluded"] and all(
            f.get("status") == "rumour" for f in pair["excluded"]
        ):
            news_slice = "rumour_only"
        else:
            news_slice = "no_applied_news"
        implementation, study_status = _build(db, payload, cache)
        if study_status == "declared" and fixture["fixture_id"] != football.fixture_id(
            fixture["season"], fixture["home"], fixture["away"]
        ):
            study_status = "noncanonical_epl_fixture_identity"
        record.update(
            sealed=True,
            seal=entry["digest"],
            recipe=payload["recipe_digest"],
            implementation=implementation,
            study_status=study_status,
            study_plan_sha256=(payload.get("news_study") or {}).get("plan_sha256"),
            extractor_config_sha256=(payload.get("news_study") or {}).get(
                "extractor_config_sha256"
            ),
            news_protocol=pair["protocol"],
            policy_sha256=pair["policy_sha256"],
            timely_news_coverage=news_study_plan.timely_coverage(pair),
            input_cutoff=pair["input_cutoff"],
            changed=pair["changed"],
            observed_both=pair["coverage"].get("both_teams_observed") is True,
            matched_claims=len(pair["applied"]) + len(pair["excluded"]),
            applied_players=len(pair["applied"]),
            news_slice=news_slice,
            digest=store.digest(pair),
        )
        record["market_close"] = news_market.observe(db, root, fixture, entry, pair, as_of=as_of)
        if (
            study_status == "declared"
            and record["study_plan_sha256"] == news_study_plan.PLAN_SHA256
        ):
            record["scheduled_market_close"] = news_market.observe(
                db, root, fixture, entry, pair, scheduled_proxy=True, as_of=as_of
            )
        else:
            record["scheduled_market_close"] = {
                "reason": "requires_scheduled_proxy_declaration"
            }

        if not record["completed"]:
            return record | {
                "reason": "awaiting_verified_score",
                "result_disposition": fixture.get("result_disposition"),
            }
        scores = {
            arm: football.score_probabilities(pair[arm]["probabilities"], *goals)
            for arm in ARMS
        }
        return record | {
            "scored": True,
            "goals": list(goals),
            "scores": scores,
            "lines": _line_metrics(pair, *goals),
        }
    except (
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
        OSError,
        OverflowError,
    ):
        return record | {"sealed": False, "reason": "invalid_seal_or_archived_evidence"}


def _records(db, fixtures, horizon, mode, policy=None, *, as_of=None, reopen_news=False, eligible_seals=None):
    ids = [f["fixture_id"] for f in fixtures]
    found = defaultdict(list)
    if ids:
        placeholders = ",".join("?" for _ in ids)
        sql = (
            "SELECT * FROM forecast_seals WHERE fixture_id IN ("
            + placeholders
            + ") AND protocol=? AND horizon=? AND mode=?"
        )
        for row in db.execute(sql, (*ids, store.PROTOCOL, horizon, mode)):
            if eligible_seals is not None and row["digest"] not in eligible_seals:
                continue
            if as_of is not None and row["sealed_at"] > as_of:
                continue
            try:
                payload = json.loads(row["data_json"])
            except (TypeError, ValueError):
                found[(row["fixture_id"], row["kickoff"])].append((row, {}))
                continue
            if (
                payload.get("purpose") == "main"
                or payload.get("purpose") is None
                and row["model_version"] == "B1"
            ):
                found[(row["fixture_id"], row["kickoff"])].append((row, payload))
    cache = {}
    verified = {}
    if mode != "replay" and fixtures:
        root = Path(db.execute("PRAGMA database_list").fetchone()[2]).parent
        verified = result_admission.verified_season_regulation(
            root,
            db,
            fixtures[0]["season"],
            [f["fixture_id"] for f in fixtures if f["home_goals"] is not None],
            at=as_of or store.now_utc(),
        )
    for f in fixtures:
        outcome = verified.get(f["fixture_id"])
        matches = found.get((f["fixture_id"], f["kickoff"]), [])
        if len(matches) == 1:
            yield _score_one(f, *matches[0], None, db, cache, mode, outcome, policy,
                             as_of=as_of, reopen_news=reopen_news)
        else:
            reason = "ambiguous_primary_seal" if matches else "no_primary_seal"
            yield _score_one(f, None, None, reason, db, cache, mode, outcome)


def _means(rows):
    scores = {
        arm: fmean(r["scores"][arm]["score"] for r in rows) if rows else None
        for arm in ARMS
    }
    return {
        "count": len(rows),
        "scores": scores,
        "delta": scores["with_news"] - scores["no_news"] if rows else None,
    }


def _metrics(rows, fields, accessor):
    result = {}
    for field in fields:
        values = [(accessor(r, arm, field)) for r in rows for arm in ARMS]
        base, news = values[::2], values[1::2]
        cols = tuple(base[0]) if base else ("brier", "log_loss")
        result[field] = {
            "count": len(base),
            **{
                metric: {
                    "no_news": fmean(a[metric] for a in base),
                    "with_news": fmean(a[metric] for a in news),
                    "delta": fmean(b[metric] - a[metric] for a, b in zip(base, news)),
                }
                if base
                else {"no_news": None, "with_news": None, "delta": None}
                for metric in cols
            },
        }
    return result


def _delta_vector(row):
    left, right = (row["scores"][arm] for arm in ARMS)
    return [
        right["score"] - left["score"],
        *(right["item_scores"][k] - left["item_scores"][k] for k in football.ITEM_KEYS),
    ]


def _complete_calendar(records):
    rounds = defaultdict(list)
    for row in records:
        rounds[row["round_id"]].append(
            (football.team_key(row["home"]), football.team_key(row["away"]))
        )
    matches = [pair for group in rounds.values() for pair in group]
    clubs = {team for pair in matches for team in pair}
    return (
        len(clubs) == 20
        and set(matches) == {(a, b) for a in clubs for b in clubs if a != b}
        and set(rounds) == set(range(1, 39))
        and all(
            len(group) == 10 and len({c for pair in group for c in pair}) == 20
            for group in rounds.values()
        )
    )


def _summary(records, season, rnd, horizon, mode, policy=None, capability=None):
    from . import news_accumulation

    done = [r for r in records if r["scored"]]
    expected = max(10 if rnd else 380, len(records))
    sealed = [r for r in records if r["sealed"]]
    observed, changed = (
        [r for r in done if r["observed_both"]],
        [r for r in done if r["changed"]],
    )
    metrics = _metrics(
        done,
        football.ITEM_KEYS,
        lambda r, a, k: {
            "brier": r["scores"][a]["losses"][k],
            "log_loss": r["scores"][a]["log_losses"][k],
        },
    )
    by_line = defaultdict(list)
    clusters = defaultdict(list)
    slices = defaultdict(list)
    for row in done:
        for name in row["lines"]:
            by_line[name].append(row)
        clusters[(row["season"], row["round_id"])].append(_delta_vector(row))
        slices[row["news_slice"]].append(row)
    lines = {
        name: _metrics(rows, [name], lambda r, a, k: r["lines"][k][a])[name]
        for name, rows in sorted(by_line.items())
    }
    interval = experiment_statistics.clustered_t(clusters.values(), 2 * len(ENDPOINTS))
    if interval:
        interval["endpoints"] = dict(zip(ENDPOINTS, interval.pop("intervals")))
    recipes = dict(sorted(Counter(r["recipe"] for r in done).items()))
    builds = dict(sorted(Counter(r["implementation"] for r in done).items()))
    completed = sum(r["completed"] for r in records)
    verdict = "insufficient_evidence"
    if (
        rnd is None
        and mode != "replay"
        and completed == expected == 380
        and len(clusters) == 38
        and len(observed) >= MIN_SEASON_PAIRS
        and len(builds) == 1
        and interval
        and _complete_calendar(records)
    ):
        primary = interval["endpoints"]["score"]
        current_policy = policy is None or policy == _policy(None)
        if not changed:
            verdict = "no_observed_changes"
        elif primary["lower"] > 0:
            verdict = (
                "no_decline_in_estimated_interval"
                if current_policy
                else "higher_score_this_season"
            )
        elif primary["upper"] < 0:
            verdict = (
                "evidence_of_score_deterioration"
                if current_policy
                else "lower_score_this_season"
            )
        else:
            verdict = "inconclusive"
    counts = Counter(r["reason"] for r in records if not r["sealed"])
    return {
        "protocol": (policy or _policy(None))[0],
        "policy_sha256": (policy or _policy(None))[1],
        "season": season,
        "round_id": rnd,
        "horizon": horizon,
        "mode": mode,
        "availability_basis": news_model.LOCAL,
        "expected": expected,
        "scheduled": len(records),
        "completed": completed,
        "sealed": len(sealed),
        "sealed_changed": sum(r["changed"] for r in sealed),
        "scored": len(done),
        "changed": len(changed),
        "observed_both": len(observed),
        "coverage": {
            "paired": len(done) / expected,
            "both_teams_observed": len(observed) / expected,
            "schedule_missing": expected - len(records),
            "final_without_pair": completed - len(done),
            "sealed_awaiting_final": sum(not r["completed"] for r in sealed),
        },
        **_means(done),
        "endpoints": metrics,
        "lines": lines,
        "market_close": news_market.summary(records, capability, mode),
        "scheduled_market_close": news_market.summary(
            records, mode=mode, field="scheduled_market_close"
        ),
        "score_guardrail": news_accumulation.score_guardrail(records),
        "affected": _means(changed),
        "covered_sample": _means(observed),
        "by_news_status": {key: _means(rows) for key, rows in sorted(slices.items())},
        "exclusions": dict(sorted(counts.items())),
        "recipes": recipes,
        "implementations": builds,
        "interval": interval,
        "verdict": verdict,
        "fixtures": [
            {k: v for k, v in r.items() if k not in {"scores", "lines"}}
            | (_means([r]) if r["scored"] else {})
            for r in records
        ],
    }


def round_summary(
    db, season, round_id, *, horizon="T-60m", mode="technical", policy=None
):
    """Use the caller's SQLite snapshot, also when embedding into a versioned round report."""
    _check_scope(season, round_id, horizon, mode)
    fixtures = [
        f
        for f in store.eligible_fixture_rows(db, season=season)
        if f["round_id"] == round_id
    ]
    selected = _policy(policy, db)
    rows = list(_records(db, fixtures, horizon, mode, selected))
    return _summary(
        rows,
        season,
        round_id,
        horizon,
        mode,
        selected,
        news_market.source_capability(db),
    )


def _evaluate(data_dir, season, rnd, horizon, mode, policy):
    _check_scope(season, rnd, horizon, mode)
    with closing(store.connect(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        selected = _policy(policy, db)
        if rnd:
            result = round_summary(
                db, season, rnd, horizon=horizon, mode=mode, policy=policy
            )
        else:
            fixtures = store.eligible_fixture_rows(db, season=season)
            rows = list(_records(db, fixtures, horizon, mode, selected))
            result = _summary(
                rows,
                season,
                None,
                horizon,
                mode,
                selected,
                news_market.source_capability(db),
            )
            groups = defaultdict(list)
            for row in rows:
                groups[row["round_id"]].append(row)
            result["rounds"] = [
                {
                    "round_id": n,
                    "scheduled": len(group),
                    "scored": sum(r["scored"] for r in group),
                    **_means([r for r in group if r["scored"]]),
                }
                for n, group in sorted(groups.items())
            ]
        key = f"{season}/{rnd or 'season'}/{horizon}/{mode}/{selected[0]}"
        prev = db.execute(
            "SELECT payload FROM reports WHERE kind='news-study' AND key=?", (key,)
        ).fetchone()
        if not prev or prev[0] != store.canonical(result):
            store.put_report(db, "news-study", key, result)
        return result


def evaluate_round(
    data_dir, season, round_id, *, horizon="T-60m", mode="technical", policy=None
):
    return _evaluate(data_dir, season, round_id, horizon, mode, policy)


def evaluate_season(
    data_dir, season, *, horizon="T-60m", mode="technical", policy=None
):
    return _evaluate(data_dir, season, None, horizon, mode, policy)


def evaluate_accumulation(
    data_dir, through_season, *, horizon="T-60m", mode="technical"
):
    """Use the original immutable declarations and one consistent read of all eligible seasons."""
    from . import news_accumulation

    _check_scope(through_season, None, horizon, mode)
    last = football.season_year(through_season)
    if mode == "replay" or last < news_study_plan.FIRST_SEASON:
        raise ValueError(
            "news accumulation requires prospective technical or formal seals from 2026-27 onward"
        )
    seasons = [
        f"{y}-{(y + 1) % 100:02d}"
        for y in range(news_study_plan.FIRST_SEASON, last + 1)
    ]
    with closing(store.connect(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        present = {
            r[0] for r in db.execute("SELECT DISTINCT season FROM fixture_versions")
        }
        records = [
            r
            for season in seasons
            if season in present
            for r in _records(
                db, store.eligible_fixture_rows(db, season=season), horizon, mode
            )
        ]
        result = news_accumulation.summarize(
            records, seasons, horizon, mode, news_market.source_capability(db)
        )
        from . import news_era_replay

        result["era_equivalence"] = news_era_replay.accumulation(
            db, records, seasons, horizon, mode, result
        )
        key = f"{through_season}/{horizon}/{mode}/{news_study_plan.PLAN_SHA256}"
        prev = db.execute(
            "SELECT payload FROM reports WHERE kind='news-accumulation' AND key=?",
            (key,),
        ).fetchone()
        if not prev or prev[0] != store.canonical(result):
            store.put_report(db, "news-accumulation", key, result)
        return result
