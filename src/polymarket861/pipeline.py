"""Connect schedules, point-in-time evidence, model forecasts and round reports."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from . import evidence, football, paired_proof
from .round_history import publish_round, register_history
from .store import (
    PROTOCOL,
    canonical,
    connect,
    digest,
    eligible_fixtures,
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
FAMILIES = ("B0", "B1", "B2")


def register_recipe(data_dir, version, family, *, lines=(0.5, 1.5, 2.5, 3.5)):
    if family not in FAMILIES or not isinstance(version, str) or not version.strip():
        raise ValueError("select a version name and a supported B0/B1/B2 family")
    if not lines or any(
        isinstance(x, bool) or float(x) < 0.5 or float(x) % 1 != 0.5 or float(x) > 20
        for x in lines
    ):
        raise ValueError("select half-goal lines between 0.5 and 19.5")
    parts = [
        Path(football.__file__),
        Path(football.__file__).with_name("football_model.py"),
    ]
    code = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in parts}
    recipe = {
        "family": family,
        "algorithm_source": code,
        "lines": sorted(set(map(float, lines)) | {1.5, 2.5}),
    }
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
        "SELECT data_json,digest FROM forecast_seals WHERE fixture_id=? AND kickoff=? "
        "AND horizon=? AND model_version=? AND protocol=? AND mode=?",
        (fixture["fixture_id"], fixture["kickoff"], horizon, version, PROTOCOL, mode),
    ).fetchone()


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
    from .jobs import active_version

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
    version = version or champion
    purpose = "main" if champion == version else "candidate"
    selected = _recipe(data_dir, version)
    recipe = selected["recipe"]
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
    with closing(connect(data_dir)) as db:
        sealed = _existing(db, fixture, horizon, version, mode)
    if sealed and not replay:
        payload = json.loads(sealed["data_json"])
        if digest(payload) != sealed["digest"]:
            raise ValueError("sealed forecast hash mismatch")
        return payload | {"digest": sealed["digest"]}
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
        return evidence.as_of_context(
            data_dir,
            fixture_id,
            fixture["home"],
            fixture["away"],
            iso(cutoff),
            prospective=prospective,
        )

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
    result = engine.forecast(
        fixture["home"],
        fixture["away"],
        kickoff,
        evidence_cutoff,
        season=fixture["season"],
        lines=recipe["lines"],
        baseline=recipe["family"] == "B0",
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
    if recipe["family"] == "B2":
        result = football.apply_context(result, context, lines=recipe["lines"])
    overdue = {side: [] for side in ("home", "away")}
    for side in overdue:
        team = football.team_key(fixture[side])
        past = sorted(
            (
                m
                for m in history
                if m.season == fixture["season"]
                and m.kickoff + timedelta(hours=48) < evidence_cutoff
                and team in (football.team_key(m.home), football.team_key(m.away))
            ),
            key=lambda f: f.kickoff,
        )
        for match in past[-2:]:
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
            old = json.loads(existing["data_json"])
            if digest(old) != existing["digest"]:
                raise ValueError("sealed forecast hash mismatch")
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

    if any(r["mode"] == "formal" and r["completed"] == r["expected"] for r in reports):
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
    version="B1",
    horizon="T-60m",
    evidence_path=None,
    archive_lag_hours=None,
):
    """Exercise the same seal and scoreboard with a fixed historical fixture denominator."""
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
    cmd = forecasts.add_parser("make")
    cmd.add_argument("fixture_id")
    cmd.add_argument("root", type=Path)
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
    cmd.add_argument("--model-version", default="B1")
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
    cmd.add_argument("--model-version", default="B1")
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
