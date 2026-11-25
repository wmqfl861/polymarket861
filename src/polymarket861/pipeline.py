"""Connect schedules, point-in-time evidence, model forecasts and round reports."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from . import evidence, football, paired_proof
from .round_history import publish_round, ready_for_experiment_review, register_history
from .store import (
    PROTOCOL,
    builtin_model_at,
    canonical,
    connect,
    digest,
    eligible_fixtures,
    ensure_unfinished_at,
    fixture_at,
    instant,
    iso,
    now_utc,
    put_report,
    record_result,
    register_fixture,
    require_formal,
)

HORIZONS = {"T-24h": timedelta(hours=24), "T-60m": timedelta(minutes=60)}
FAMILIES = ("B0", "B1", "B2", "B3")


def _build_recipe(family, lines):
    if family not in FAMILIES:
        raise ValueError("select a version name and a supported model family")
    if not lines or any(
        isinstance(x, bool) or float(x) < 0.5 or float(x) % 1 != 0.5 or float(x) > 20
        for x in lines
    ):
        raise ValueError("select half-goal lines between 0.5 and 19.5")
    from . import model_study

    return {
        "family": family,
        **model_study.decision_implementation(),
        "lines": sorted(set(map(float, lines)) | {1.5, 2.5}),
    }


def register_recipe(data_dir, version, family, *, lines=(0.5, 1.5, 2.5, 3.5)):
    if not isinstance(version, str) or not version.strip():
        raise ValueError("model version must have a name")
    recipe = _build_recipe(family, lines)
    with closing(connect(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT recipe FROM model_recipes WHERE version=?", (version,)
        ).fetchone()
        if old and json.loads(old["recipe"]) != recipe:
            raise ValueError(
                "the version already names a different recipe; register a new version"
            )
        if not old:
            db.execute(
                "INSERT INTO model_recipes VALUES (?,?,?)",
                (version, canonical(recipe), now_utc()),
            )
    return {"version": version, "recipe": recipe, "recipe_digest": digest(recipe)}


def prepare_builtin(data_dir):
    """Read the last effective built-in. New builds must pass the explicit release gate."""
    with closing(connect(data_dir)) as db:
        return builtin_model_at(db)


def _forecast_arms(engine, home, away, kickoff, cutoff, season, recipe, context, *, include_pair):
    family = recipe["family"]
    result = engine.forecast(
        home,
        away,
        kickoff,
        cutoff,
        season=season,
        lines=recipe["lines"],
        baseline=family == "B0",
    )
    pair = None
    if include_pair or family == "B3":
        from .news_model import paired_prediction

        # The two news arms always start from the same team-strength distribution,
        # including its ordinary cold-start fallback, whatever the active recipe is.
        team_model = result
        if family == "B0":
            team_model = engine.forecast(home, away, kickoff, cutoff, season=season, lines=recipe["lines"])
        observation = context.get("player_news", {})
        with_news, pair = paired_prediction(
            team_model, observation.get("facts", []), home=home, away=away,
            lines=recipe["lines"], cutoff=iso(cutoff), coverage=observation.get("coverage"),
        )
        if family == "B3":
            result = with_news
    if family == "B2":
        result = football.apply_context(result, context, lines=recipe["lines"])
    return result, pair


def probability(engine, home, away, kickoff, cutoff, season, recipe, context):
    result, _ = _forecast_arms(
        engine, home, away, kickoff, cutoff, season, recipe, context, include_pair=False
    )
    return result


def _recipe(data_dir, version):
    with closing(connect(data_dir)) as db:
        row = db.execute(
            "SELECT recipe FROM model_recipes WHERE version=?", (version,)
        ).fetchone()
    if row:
        recipe = json.loads(row["recipe"])
        return register_recipe(
            data_dir, version, recipe["family"], lines=recipe["lines"]
        )
    if version in FAMILIES:
        return register_recipe(data_dir, version, version)
    raise ValueError("model version must have a registered, executable recipe")


def _lineups(context):
    facts = {row["evidence_id"]: row for row in context["facts"]}
    result = {}
    for side in ("home", "away"):
        team = context["teams"][side]
        eid = (team["lineup"] or {}).get("evidence_id")
        result[side] = {
            "confirmed": team["confirmed"],
            "received_at": facts.get(eid, {}).get("received_at"),
            "evidence_id": eid,
        }
    return result


def _existing(db, fixture, horizon, version, mode):
    return db.execute(
        "SELECT * FROM forecast_seals WHERE fixture_id=? AND kickoff=? "
        "AND horizon=? AND model_version=? AND protocol=? AND mode=?",
        (fixture["fixture_id"], fixture["kickoff"], horizon, version, PROTOCOL, mode),
    ).fetchone()


def _verified_seal(row):
    payload = json.loads(row["data_json"])
    if digest(payload) != row["digest"]:
        raise ValueError("sealed forecast hash mismatch")
    for key in (
        "fixture_id",
        "kickoff",
        "cutoff",
        "horizon",
        "model_version",
        "protocol",
        "mode",
        "sealed_at",
    ):
        if payload.get(key) != row[key]:
            raise ValueError("sealed forecast identity or clock mismatch")
    if row["mode"] != "replay" and (
        row["sealed_at"] > row["cutoff"] or row["cutoff"] >= row["kickoff"]
    ):
        raise ValueError("sealed forecast has inconsistent pregame clocks")
    return payload | {"digest": row["digest"]}


def read_forecast(data_dir, seal_digest):
    """Return the original immutable prediction even after its schedule, model, or gate changes."""
    with closing(connect(data_dir)) as db:
        row = db.execute(
            "SELECT * FROM forecast_seals WHERE digest=?", (seal_digest,)
        ).fetchone()
    if row is None:
        raise ValueError("unknown sealed forecast digest")
    return _verified_seal(row)


def make_forecast(
    data_dir,
    fixture_id,
    root,
    *,
    mode="technical",
    horizon="T-60m",
    version=None,
    evidence_path=None,
    archive_lag_hours=None,
    _snapshot=None,
):
    from .jobs import active_version, model_for_decision

    if mode not in ("technical", "formal", "replay") or horizon not in (
        *HORIZONS,
        "early",
    ):
        raise ValueError("unknown forecast mode or horizon")
    if archive_lag_hours is not None and mode != "replay":
        raise ValueError("assumed historical availability is restricted to replay")
    if horizon == "early" and mode != "technical":
        raise ValueError("early forecasts are technical observations")
    fixture = fixture_at(data_dir, fixture_id)
    kickoff = instant(fixture["kickoff"])
    arrival = instant(now_utc())
    deadline = kickoff - HORIZONS[horizon] if horizon in HORIZONS else arrival
    replay = mode == "replay"
    champion = active_version(
        data_dir, "model", "epl", default="B1", at=iso(min(deadline, arrival))
    )
    requested = version
    if not replay:
        current_schedule = fixture_at(
            data_dir, fixture_id, at=arrival, prospective=True
        )
        if current_schedule["precision"] == "date":
            raise ValueError("a date-only schedule cannot anchor a live cutoff")
        if current_schedule["kickoff"] != fixture["kickoff"]:
            raise ValueError("schedule changed; retry")
    if mode == "formal":
        require_formal(data_dir, at=arrival)
    if not requested and not replay:
        champion = model_for_decision(
            data_dir,
            fixture_id,
            fixture["kickoff"],
            horizon,
            (mode,),
            iso(min(deadline, arrival)),
        )
    with closing(connect(data_dir)) as db:
        sealed = _existing(db, fixture, horizon, requested or champion, mode)
        if not sealed and not replay:
            ensure_unfinished_at(db, fixture_id, fixture["kickoff"], iso(arrival))
    if sealed and not replay:
        return _verified_seal(sealed)
    if not replay:
        if arrival >= kickoff or (
            horizon != "early"
            and not deadline - timedelta(minutes=5) <= arrival <= deadline
        ):
            raise ValueError(
                "outside the pre-decision sealing window; use technical 'early' before it or replay after it"
            )
    if replay and arrival < deadline:
        raise ValueError("cannot replay a future decision")
    if replay and root is None and _snapshot is None:
        raise ValueError("historical replay requires its input archive")

    if requested is None and (champion == "B1" or champion.startswith("B1@")):
        build = prepare_builtin(data_dir)
        # An unscheduled early observation starts now. Its frame cannot predate a
        # build installed during this invocation; the named T-24/T-60 deadlines stay fixed.
        if horizon == "early":
            deadline = instant(now_utc())
        champion = active_version(
            data_dir,
            "model",
            "epl",
            default="B1",
            at=iso(min(deadline, instant(now_utc()))),
        )
        version = build if replay else champion
    else:
        version = requested or champion
    purpose = "main" if champion == version else "candidate"
    try:
        selected = _recipe(data_dir, version)
    except ValueError as exc:
        if sealed and replay:
            selected = None
        elif requested is None and (version == "B1" or version.startswith("B1@")):
            raise ValueError(
                "a changed builtin requires forecast release-compare and named release-approve"
            ) from exc
        else:
            raise
    recipe = selected["recipe"] if selected else None
    if replay:
        with closing(connect(data_dir)) as db:
            sealed = _existing(db, fixture, horizon, version, mode)

    def schedules(cutoff, prospective):
        timestamp = iso(cutoff)
        with closing(connect(data_dir)) as db:
            if prospective:
                rows = db.execute(
                    "SELECT f.kickoff,max(f.known_at,s.recorded_at) AS known_at FROM fixture_versions f "
                    "JOIN schedule_receipts s USING(fixture_id,version) WHERE f.fixture_id=? AND f.known_at<=? AND "
                    "s.recorded_at<=? ORDER BY f.known_at",
                    (fixture_id, timestamp, timestamp),
                )
            else:
                rows = db.execute(
                    "SELECT kickoff,known_at FROM fixture_versions WHERE fixture_id=? AND known_at<=? "
                    "ORDER BY known_at",
                    (fixture_id, timestamp),
                )
            return [dict(row) for row in rows]

    def context_at(cutoff, prospective):
        from . import news

        context = evidence.as_of_context(
            data_dir,
            fixture_id,
            fixture["home"],
            fixture["away"],
            iso(cutoff),
            prospective=prospective,
        )
        # Replay must use the same local publication, receipt and extraction
        # cutoffs as live prediction. Old sealed frames remain readable as-is.
        context["player_news"] = news.snapshot(data_dir, fixture_id, iso(cutoff))
        return context

    manifest = None
    frame = None
    if replay:
        snapshot = _snapshot or paired_proof.capture_history(
            data_dir, root, evidence_path, archive_lag_hours
        )
        root, evidence_path = snapshot["root"], snapshot["evidence_path"]
        snapshot_digest = snapshot["digest"]
        if sealed:
            previous = json.loads(sealed["data_json"])
            if digest(previous) != sealed["digest"]:
                raise ValueError("sealed forecast hash mismatch")
            if (previous.get("comparison_proof") or {}).get(
                "snapshot"
            ) != snapshot_digest:
                raise paired_proof.SealedInputConflict(
                    "A replay was already sealed with different source or timing bytes; use a new model version."
                )
            if selected is None:
                return _verified_seal(sealed)
        evidence_cutoff = deadline
        revisions = schedules(evidence_cutoff, False)
        context = context_at(evidence_cutoff, False)
        strict_context = context_at(evidence_cutoff, True)
        # The prospective selector checks validation evidence as well as every fact.
        context_verified = (context | {"prospective": True}) == strict_context
    else:
        from .input_manifest import verify_observed_inputs

        def freeze():
            observed = verify_observed_inputs(data_dir, root, evidence_path)
            cut = min(instant(now_utc()), deadline)
            known = fixture_at(data_dir, fixture_id, at=cut, prospective=True)
            if known["kickoff"] != fixture["kickoff"]:
                raise ValueError("schedule revision is outside the evidence cutoff")
            return {
                "manifest": observed["digest"],
                "cutoff": iso(cut),
                "context": context_at(cut, True),
                "schedules": schedules(cut, True),
            }

        frame = paired_proof.live_frame(
            data_dir, (fixture_id, fixture["kickoff"], horizon, mode, PROTOCOL), freeze
        )
        evidence_cutoff = instant(frame["cutoff"])
        if evidence_cutoff > instant(now_utc()) or evidence_cutoff > deadline:
            raise ValueError("frozen evidence time is after this decision")
        manifest = paired_proof.frozen_manifest(data_dir, frame["manifest"])
        root, evidence_path = Path(manifest["root"]), Path(manifest["evidence_path"])
        snapshot_digest = manifest["digest"]
        context, revisions = frame["context"], frame["schedules"]
        context_verified = True
    history = football.load_fixtures(
        root, evidence_path=evidence_path, archive_lag_hours=archive_lag_hours
    )
    if revisions:
        updated = []
        for match in history:
            if match.fixture_id == fixture_id:
                timeline = {
                    when: k
                    for when, k in match.schedule_versions
                    if when <= evidence_cutoff
                }
                timeline.update(
                    {instant(r["known_at"]): instant(r["kickoff"]) for r in revisions}
                )
                known = sorted(timeline.items())
                match = replace(
                    match,
                    kickoff=kickoff,
                    schedule_versions=tuple(known),
                    schedule_grades=tuple("recorded_schedule" for _ in known),
                )
            updated.append(match)
        history = updated
    engine = football.FootballEngine(history)
    result, news_pair = _forecast_arms(
        engine,
        fixture["home"],
        fixture["away"],
        kickoff,
        evidence_cutoff,
        fixture["season"],
        recipe,
        context,
        include_pair=purpose == "main",
    )
    proof = paired_proof.make_proof(
        fixture,
        evidence_cutoff,
        snapshot_digest,
        context,
        result["evidence"],
        revisions,
        context_verified=context_verified,
        frame_digest=frame["digest"] if frame else None,
    )
    # The league-frequency comparator is scored from the very same training view,
    # context and cutoff. It is part of this immutable seal for every model version.
    baseline = (
        result
        if result["model"] == "B0"
        else engine.forecast(
            fixture["home"],
            fixture["away"],
            kickoff,
            evidence_cutoff,
            season=fixture["season"],
            lines=recipe["lines"],
            baseline=True,
        )
    )
    frozen_baseline = {
        "model": "B0",
        "probabilities": baseline["probabilities"],
        "input_proof_sha256": proof["sha256"],
        "training_sha256": baseline["evidence"]["input_sha256"],
    }
    overdue = {side: [] for side in ("home", "away")}
    past = sorted(
        (football.history_kickoff(m, evidence_cutoff), m.fixture_id, m)
        for m in history
        if m.season == fixture["season"]
    )
    for side in overdue:
        team = football.team_key(fixture[side])
        recent = [
            m
            for when, _, m in past
            if when + timedelta(hours=48) < evidence_cutoff
            and team in (football.team_key(m.home), football.team_key(m.away))
        ]
        for match in recent[-2:]:
            versions = [
                v for v in match.result_versions if v.available <= evidence_cutoff
            ]
            if not versions or versions[-1].score is None:
                overdue[side].append(match.fixture_id)
    stale = any(len(matches) >= 2 for matches in overdue.values())
    lineups = _lineups(context)
    both = all(v["confirmed"] for v in lineups.values())
    state = (
        "eligible_for_price_check"
        if both and not stale and horizon == "T-60m"
        else "no_paper_order"
    )
    reason = (
        "须再核实同一代币双时点价量及账户风险"
        if state == "eligible_for_price_check"
        else (
            "此时点只记录预测"
            if horizon != "T-60m"
            else "近期已过期的两场球队赛果待确认"
            if stale
            else "两队正式首发未全部及时确认"
        )
    )
    # The DB clock below is sampled after model computation, before append in an immediate transaction.
    with closing(connect(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        existing = _existing(db, fixture, horizon, version, mode)
        if existing:
            old = _verified_seal(existing)
            if replay:
                paired_proof.check_replay_cache(old, proof)
            return old | {"digest": existing["digest"]}
        sealed_at = now_utc()
        if not replay and (
            (horizon != "early" and instant(sealed_at) > deadline)
            or instant(sealed_at) >= kickoff
        ):
            raise ValueError("calculation finished after its sealing deadline")
        schedule = db.execute(
            "SELECT kickoff FROM fixture_versions WHERE fixture_id=? AND known_at<=? "
            "ORDER BY known_at DESC LIMIT 1",
            (fixture_id, sealed_at),
        ).fetchone()
        if schedule is None or schedule["kickoff"] != fixture["kickoff"]:
            raise ValueError("the current schedule changed during computation")
        label_cutoff = sealed_at if horizon == "early" else iso(deadline)
        payload = result | {
            "fixture_id": fixture_id,
            "kickoff": fixture["kickoff"],
            "cutoff": label_cutoff,
            "input_cutoff": iso(evidence_cutoff),
            "comparison_proof": proof,
            "baseline_comparator": frozen_baseline,
            "sealed_at": sealed_at,
            "season": fixture["season"],
            "round_id": fixture["round_id"],
            "mode": mode,
            "horizon": horizon,
            "protocol": PROTOCOL,
            "model_version": version,
            "model_engine": football.MODEL_VERSION,
            "recipe_digest": selected["recipe_digest"],
            "input_manifest_sha256": snapshot_digest,
            "purpose": purpose,
            "decision": {"state": state, "reason": reason},
            "context": context,
            "lineups": lineups,
            "source_health": {"overdue_team_results": overdue, "critical_stale": stale},
            "time_witness": "local_only",
        }
        if news_pair is not None:
            payload["news_pair"] = news_pair
        seal = digest(payload)
        db.execute(
            "INSERT INTO forecast_seals VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                fixture_id,
                fixture["kickoff"],
                label_cutoff,
                horizon,
                version,
                PROTOCOL,
                mode,
                sealed_at,
                seal,
                canonical(payload),
            ),
        )
        published = payload | {"digest": seal}
        if context["evidence_ids"]:
            evidence.link_lineage_db(db, "prediction", seal, context["evidence_ids"])
        put_report(db, "match", seal, published)
    return published


def import_result_and_score(data_dir, outcome):
    result = record_result(data_dir, outcome)
    fixture = fixture_at(data_dir, result["fixture_id"])
    with closing(connect(data_dir)) as db:
        versions = db.execute(
            "SELECT DISTINCT model_version,mode,horizon FROM forecast_seals WHERE fixture_id=?",
            (result["fixture_id"],),
        ).fetchall()
    reports = [
        publish_round(
            data_dir,
            fixture["season"],
            fixture["round_id"],
            version=r[0],
            mode=r[1],
            horizon=r[2],
        )
        for r in versions
    ]
    if (
        not reports
        and instant(fixture["known_at"])
        <= instant(fixture["kickoff"]) - HORIZONS["T-60m"]
    ):
        reports.append(
            publish_round(
                data_dir, fixture["season"], fixture["round_id"], mode="technical"
            )
        )
    reviews = []
    from .jobs import review_due_experiments

    if any(ready_for_experiment_review(r) for r in reports):
        reviews = review_due_experiments(
            data_dir, fixture["season"], fixture["round_id"]
        )
    return {"result": result, "round_reports": reports, "research_reviews": reviews}


def replay_round(
    data_dir,
    root,
    season,
    number,
    *,
    version=None,
    horizon="T-60m",
    evidence_path=None,
    archive_lag_hours=None,
):
    """Exercise the same seal and scoreboard with a fixed historical fixture denominator."""
    version = version or prepare_builtin(data_dir)
    snapshot = paired_proof.capture_history(
        data_dir, root, evidence_path, archive_lag_hours
    )
    root, evidence_path = snapshot["root"], snapshot["evidence_path"]
    all_fixtures = football.load_fixtures(
        root, evidence_path=evidence_path, archive_lag_hours=archive_lag_hours
    )
    selected = [f for f in all_fixtures if f.season == season and f.round == number]
    if not selected:
        raise ValueError("round not present in the supplied season archive")
    if any(f.kickoff >= instant(now_utc()) for f in selected):
        raise ValueError("use replay-round only for an already played round")
    failures = []
    observed = now_utc()
    for f in selected:
        source = f"https://api.github.com/repos/openfootball/football.json/git/blobs/{f.source_blob}"
        try:
            fixture = fixture_at(data_dir, f.fixture_id)
        except ValueError:
            fixture = register_fixture(
                data_dir,
                {
                    "fixture_id": f.fixture_id,
                    "round_id": number,
                    "season": season,
                    "home": f.home,
                    "away": f.away,
                    "kickoff": iso(f.kickoff),
                    "source": "openfootball",
                    "known_at": observed,
                    "locator": source,
                },
            )
        if fixture["kickoff"] != iso(f.kickoff):
            failures.append(
                {"fixture": f.fixture_id, "reason": "historical kickoff conflict"}
            )
            continue
        if f.home_goals is not None:
            with closing(connect(data_dir)) as db:
                old = db.execute(
                    "SELECT home_goals,away_goals FROM result_versions WHERE fixture_id=? "
                    "ORDER BY version DESC LIMIT 1",
                    (f.fixture_id,),
                ).fetchone()
            if old and (old[0], old[1]) != (f.home_goals, f.away_goals):
                failures.append(
                    {"fixture": f.fixture_id, "reason": "final score conflict"}
                )
                continue
            if not old:
                record_result(
                    data_dir,
                    {
                        "fixture_id": f.fixture_id,
                        "home_goals": f.home_goals,
                        "away_goals": f.away_goals,
                        "source": "openfootball",
                        "locator": source,
                        "finalized_by": observed,
                        "published_by": observed,
                        "archive_at": observed,
                    },
                )
        try:
            make_forecast(
                data_dir,
                f.fixture_id,
                root,
                mode="replay",
                horizon=horizon,
                version=version,
                evidence_path=evidence_path,
                archive_lag_hours=archive_lag_hours,
                _snapshot=snapshot,
            )
        except paired_proof.SealedInputConflict:
            raise
        except ValueError as problem:
            failures.append({"fixture": f.fixture_id, "reason": str(problem)})
    return {
        "round_report": publish_round(
            data_dir, season, number, version=version, mode="replay", horizon=horizon
        ),
        "missing_reasons": failures,
        "availability": "lag_sensitivity" if archive_lag_hours else "as_of_evidence",
    }


def _read(file):
    return json.loads(Path(file).read_text(encoding="utf-8"))


def register(subparsers):
    fx = subparsers.add_parser(
        "fixture", help="登记有来源的赛程及独立赛果"
    ).add_subparsers(required=True)
    cmd = fx.add_parser("add")
    cmd.add_argument("json", type=Path)
    cmd.set_defaults(func=lambda a: register_fixture(a.data_dir, _read(a.json)))
    cmd = fx.add_parser("result")
    cmd.add_argument("json", type=Path)
    cmd.set_defaults(func=lambda a: import_result_and_score(a.data_dir, _read(a.json)))
    cmd = fx.add_parser("list")
    cmd.add_argument("--season")
    cmd.set_defaults(func=lambda a: eligible_fixtures(a.data_dir, season=a.season))
    forecasts = subparsers.add_parser(
        "forecast", help="运行模型并封存赛前预测"
    ).add_subparsers(required=True)
    from . import model_study

    model_study.register(forecasts)
    read = forecasts.add_parser(
        "get", help="retrieve and verify the original sealed prediction by SHA-256"
    )
    read.add_argument("digest")
    read.set_defaults(func=lambda a: read_forecast(a.data_dir, a.digest))
    cmd = forecasts.add_parser("make")
    cmd.add_argument("fixture_id")
    cmd.add_argument(
        "root",
        type=Path,
        nargs="?",
        help="已登记的输入目录，真实赛前预测默认使用最近验证的档案",
    )
    cmd.add_argument(
        "--mode", choices=["technical", "formal", "replay"], default="technical"
    )
    cmd.add_argument("--horizon", choices=[*HORIZONS, "early"], default="T-60m")
    cmd.add_argument("--model-version")
    cmd.add_argument("--evidence", type=Path)
    cmd.add_argument("--archive-lag-hours", type=float)
    cmd.set_defaults(
        func=lambda a: make_forecast(
            a.data_dir,
            a.fixture_id,
            a.root,
            mode=a.mode,
            horizon=a.horizon,
            version=a.model_version,
            evidence_path=a.evidence,
            archive_lag_hours=a.archive_lag_hours,
        )
    )
    cmd = forecasts.add_parser("recipe")
    cmd.add_argument("version")
    cmd.add_argument("family", choices=FAMILIES)
    cmd.add_argument("--lines", type=float, nargs="+", default=[0.5, 1.5, 2.5, 3.5])
    cmd.set_defaults(
        func=lambda a: register_recipe(a.data_dir, a.version, a.family, lines=a.lines)
    )
    cmd = forecasts.add_parser(
        "replay-inputs", help="从封存摘要离线恢复研究用源数据及时间旁证"
    )
    cmd.add_argument("digest")
    cmd.set_defaults(
        func=lambda a: paired_proof.exported_replay_inputs(a.data_dir, a.digest)
    )
    cmd = forecasts.add_parser("replay-round")
    cmd.add_argument("root", type=Path)
    cmd.add_argument("season")
    cmd.add_argument("round_id", type=int)
    cmd.add_argument("--model-version")
    cmd.add_argument("--horizon", choices=list(HORIZONS), default="T-60m")
    cmd.add_argument("--evidence", type=Path)
    cmd.add_argument("--archive-lag-hours", type=float)
    cmd.set_defaults(
        func=lambda a: replay_round(
            a.data_dir,
            a.root,
            a.season,
            a.round_id,
            version=a.model_version,
            horizon=a.horizon,
            evidence_path=a.evidence,
            archive_lag_hours=a.archive_lag_hours,
        )
    )
    cmd = forecasts.add_parser("round")
    cmd.add_argument("season")
    cmd.add_argument("round_id", type=int)
    cmd.add_argument("--model-version")
    cmd.add_argument(
        "--mode", choices=["technical", "formal", "replay"], default="formal"
    )
    cmd.add_argument("--horizon", choices=[*HORIZONS, "early"], default="T-60m")
    cmd.set_defaults(
        func=lambda a: publish_round(
            a.data_dir,
            a.season,
            a.round_id,
            version=a.model_version,
            mode=a.mode,
            horizon=a.horizon,
        )
    )
    register_history(forecasts)
    from .qualification import register as register_trial

    register_trial(subparsers)
