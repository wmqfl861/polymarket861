"""Evidence required to open a new, strictly prospective paper account window."""

from __future__ import annotations

import json
import tempfile
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from . import football, ledger, market_archive, trial_evidence
from .store import (
    PROTOCOL,
    archive_bytes,
    canonical,
    connect,
    digest,
    eligible_fixtures,
    instant,
    iso,
    list_reports,
    now_utc,
    save_report,
)


def study_archive(data_dir, fixtures):
    selected = sorted(set(fixtures))
    if not selected:
        raise ValueError("give at least one finished EPL event slug")
    coverage = market_archive.coverage(
        data_dir, fixtures=selected, scope="operator-selected historical fixtures"
    )
    now = instant(now_utc())
    for row in coverage["fixture_details"]:
        if not row["kickoff_candidates"] or any(
            instant(t) >= now for t in row["kickoff_candidates"]
        ):
            raise ValueError(
                "historical sample requires archived and past kickoff candidates"
            )
    witnesses = market_archive.replay_inputs(data_dir, selected)
    replay = ledger.replay(data_dir, witnesses, source="historical")
    manifest = replay["manifest_sha256"]
    archived = archive_bytes(
        data_dir,
        "trial-market-inputs",
        canonical(witnesses).encode(),
        metadata={
            "fixtures": selected,
            "manifest_sha256": manifest,
            "verifier": ledger.HISTORICAL_VERIFIER,
        },
    )

    report = {
        "venue": "polymarket.com",
        "source": "local_public_archive",
        "protocol": PROTOCOL,
        "fixtures": selected,
        "fixture_count": len(selected),
        "coverage": coverage["groups"],
        "counts": replay["counts"],
        "account": replay["account"],
        "manifest_sha256": replay["manifest_sha256"],
        "input_sha256": archived["sha256"],
        "replay_verifier": replay["replay_verifier"],
        "schedule_conflicts": [
            v["fixture"] for v in coverage["fixture_details"] if v["schedule_conflict"]
        ],
        "historical_roi": replay["roi"],
        "realized_pnl": replay["realized_pnl"],
        "comparison": "B/C are pre-kickoff research observations. Depth and fee evidence are required for A.",
    }
    save_report(data_dir, "trial-market-study", replay["manifest_sha256"], report)
    return report


def _scope(fixtures):
    if (
        not isinstance(fixtures, list)
        or not fixtures
        or not all(isinstance(f, str) for f in fixtures)
    ):
        raise ValueError("market study needs a registered EPL fixture scope")
    if any(not (m := market_archive.FIXTURE.fullmatch(f)) or m[5] for f in fixtures):
        raise ValueError("invalid market study fixture")
    return tuple(sorted(set(fixtures)))


def _restore_legacy_manifest(root, db, account, manifest, fixtures):
    rows = market_archive.replay_inputs(root, fixtures)
    if ledger.replay_fingerprint("historical", rows) == manifest:
        return rows
    # Old A receipts retain the complete, original serialized opportunity. If that
    # opportunity lost its kickoff, assembly now returns a gap with a different football
    # ID. Restore only candidates whose *entire ordered manifest* later rehashes exactly.
    originals = defaultdict(list)
    ids = defaultdict(set)
    for r in db.execute(
        "SELECT w.payload FROM paper_replay_members m JOIN paper_replay_witnesses w "
        "USING(account,opportunity_digest) WHERE m.account=? ORDER BY m.rowid",
        (account,),
    ):
        op = json.loads(r[0])
        slug = op.get("market_fixture") or op.get("match_id")
        originals[(slug, op.get("condition_id"), op.get("token_id"))].append(op)
        ids[slug].add(op.get("match_id"))
    recovered = []
    used = set()
    for op in rows:
        slug = op.get("market_fixture") or op.get("match_id")
        key = (slug, op.get("condition_id"), op.get("token_id"))
        if key in originals:
            if key not in used:
                recovered.extend(originals[key])
                used.add(key)
        else:
            if len(ids[slug]) == 1:
                op = op | {"market_fixture": slug, "match_id": next(iter(ids[slug]))}
            recovered.append(op)
    return recovered


def _registered_inputs(root, study, *, key=None):
    """Authenticate what was actually enrolled. Cached counts and returns confer no eligibility."""
    if study.get("replay_verifier") != ledger.HISTORICAL_VERIFIER:
        raise ValueError("obsolete historical verifier")
    manifest = study["manifest_sha256"]
    account = study["account"]
    if key is not None and key != manifest:
        raise ValueError("study key disagrees with the registered manifest")
    adopt_legacy = False
    with ledger.connection(root) as db:
        if ledger._replay_version(db, account) != ledger.HISTORICAL_VERIFIER:
            raise ValueError("unregistered historical replay")
        stored = db.execute(
            "SELECT fingerprint FROM paper_replay_verifiers WHERE account=?", (account,)
        ).fetchone()
        if not stored or stored[0] != manifest:
            raise ValueError("historical account manifest mismatch")
        direct = study.get("source") == "historical"
        if direct:
            # For the low-level replay checker, only an exactly reconstructible input sequence qualifies.
            ops = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT w.payload FROM paper_replay_members m JOIN "
                    "paper_replay_witnesses w USING(account,opportunity_digest) WHERE m.account=? ORDER BY m.rowid",
                    (account,),
                )
            ]
        else:
            if (
                study.get("source") != "local_public_archive"
                or study.get("venue") != "polymarket.com"
                or study.get("protocol") != PROTOCOL
                or account != "replay-" + manifest[:16]
            ):
                raise ValueError("unverified trial market study scope")
            fixtures = _scope(study.get("fixtures"))
            if list(fixtures) != study["fixtures"] or study.get("fixture_count") != len(
                fixtures
            ):
                raise ValueError("study fixture count mismatch")
            expected_metadata = canonical(
                {
                    "fixtures": list(fixtures),
                    "manifest_sha256": manifest,
                    "verifier": ledger.HISTORICAL_VERIFIER,
                }
            )
            observed = db.execute(
                "SELECT DISTINCT sha256 FROM observations WHERE source='trial-market-inputs' AND metadata=?",
                (expected_metadata,),
            ).fetchall()
            if observed or "input_sha256" in study:
                if len(observed) != 1 or (
                    "input_sha256" in study and observed[0][0] != study["input_sha256"]
                ):
                    raise ValueError(
                        "study input manifest archive reference is missing or inconsistent"
                    )
                ops = json.loads(ledger._blob(root, observed[0][0]))
            else:
                # Pre-upgrade enrollments had only a fingerprint. Admit them if original bytes
                # can be reconstructed exactly, never on the strength of a cached score.
                ops = _restore_legacy_manifest(root, db, account, manifest, fixtures)
                adopt_legacy = True
    if (
        not isinstance(ops, list)
        or not ops
        or ledger.replay_fingerprint("historical", ops) != manifest
    ):
        raise ValueError(
            "original market inputs do not authenticate the enrolled manifest"
        )
    if not direct and study.get("counts", {}).get("expected") != len(ops):
        raise ValueError(
            "original market denominator differs from the published sample count"
        )
    if not direct and {
        r.get("market_fixture") or r.get("match_id") for r in ops
    } != set(fixtures):
        raise ValueError(
            "original market denominator differs from the registered scope"
        )
    if adopt_legacy:
        archive_bytes(
            root,
            "trial-market-inputs",
            canonical(ops).encode(),
            metadata=json.loads(expected_metadata),
        )
    return ops


def _has_market_evidence(replay):
    counts = replay["counts"]
    return (
        counts["expected"] > 0
        and counts["listed"] > 0
        and any(counts[k] > 0 for k in ("A", "B", "C"))
    )


def _current_market_study(data_dir, study):
    """Check a particular past projection against the current original evidence."""
    try:
        ops = _registered_inputs(data_dir, study)
        if study.get("source") != "historical":
            ops = market_archive.replay_inputs(data_dir, _scope(study["fixtures"]))
        current = ledger.replay(data_dir, ops, source="historical")
        return (
            _has_market_evidence(current)
            and current["manifest_sha256"] == study["manifest_sha256"]
            and current["counts"] == study.get("counts")
            and current["roi"] == study.get("historical_roi", study.get("roi"))
            and current["realized_pnl"] == study.get("realized_pnl")
        )
    except (KeyError, ValueError, TypeError, OSError):
        return False


def _market_sample(data_dir):
    # Registration establishes the scope. It does not select a winning historical snapshot.
    # Rebuild each main event once, even when several previously enrolled scopes overlap.
    registered = {}
    unavailable = []
    scopes = set()
    for r in list_reports(data_dir, "trial-market-study"):
        try:
            study = r["payload"]
            if study.get("source") != "local_public_archive":
                raise ValueError(
                    "trial market study has no registered public archive scope"
                )
            ops = _registered_inputs(data_dir, study, key=r["key"])
            for f in _scope(study["fixtures"]):
                registered.setdefault(
                    f,
                    [
                        op
                        for op in ops
                        if (op.get("market_fixture") or op.get("match_id")) == f
                    ],
                )
            scopes.add(_scope(study["fixtures"]))
        except (KeyError, ValueError, TypeError, OSError):
            unavailable.append(r["key"])
    if not registered:
        return None, unavailable
    ops = []
    gaps = {}
    for fixture, old in sorted(registered.items()):
        try:
            schedules = market_archive.fixture_schedule(data_dir, fixture)
            if len(schedules) != 1 or instant(schedules[0]) >= instant(now_utc()):
                raise ValueError("unconfirmed or future fixture schedule")
            current = market_archive.replay_inputs(data_dir, [fixture])
            if not current or any(
                (row.get("market_fixture") or row.get("match_id")) != fixture
                for row in current
            ):
                raise ValueError("reconstructed scope mismatch")
            ops.extend(current)
        except (KeyError, ValueError, TypeError, OSError) as err:
            # Retain the original market/token denominator even if a new collector index points
            # to a missing page. Such a fixture cannot supply a stale return or new grade.
            gaps[fixture] = str(err)
            for row in old:
                c = row.get("intent", {}).get("contract", {})
                ops.append(
                    {
                        "match_id": fixture,
                        "market_fixture": fixture,
                        "family": row.get("family") or ledger._market_family(c),
                        "condition_id": row.get("condition_id")
                        or c.get("condition_id"),
                        "token_id": row.get("token_id") or c.get("token_id"),
                        "decision_at": row.get("decision_at")
                        or row.get("intent", {}).get("decision_at"),
                        "season": row.get("season", "unknown"),
                        "liquidity": row.get("liquidity", "unknown"),
                        "listed": row.get("listed") is True,
                        "schedule_conflict": True,
                        "trades": [],
                        "prices": [],
                    }
                )
    replay = ledger.replay(data_dir, ops, source="historical")
    return {
        "fixtures": sorted(registered),
        "fixture_count": len(registered),
        "registered_scopes": len(scopes),
        "manifest_sha256": replay["manifest_sha256"],
        "account": replay["account"],
        "counts": replay["counts"],
        "settled_cost": replay["settled_cost"],
        "realized_pnl": replay["realized_pnl"],
        "historical_roi": replay["roi"],
        "qualified": _has_market_evidence(replay),
        "fixture_gaps": gaps,
    }, unavailable


def _valid_backtest(report):
    if (
        report["input_provenance"]["study_type"] != "sensitivity"
        or report["protocol"].get("archive_lag_hours") != 24
    ):
        return False
    if report.get("protocol", {}).get("bootstrap_resamples", 0) < 2000:
        return False
    for name in ("T-1440", "T-60"):
        horizon = report["horizons"][name]
        if any(row.get("n") != 380 for row in horizon["by_season"].values()):
            return False
        if horizon["summary"].get("paired_round_ci95") is None:
            return False
    return True


def qualification(data_dir):
    all_studies = [
        r
        for r in list_reports(data_dir, "backtest")
        if trial_evidence.verified_study(data_dir, r["payload"])
    ]
    scores = [r for r in all_studies if _valid_backtest(r["payload"])]
    # Historical availability can legitimately be zero. Report that denominator.
    # A measured zero does not turn an assumed delay into a verified pregame observation.
    strict = [
        r
        for r in all_studies
        if (
            not scores
            or any(
                trial_evidence.scope(r["payload"]) == trial_evidence.scope(s["payload"])
                for s in scores
            )
        )
        and len(r["payload"].get("seasons", [])) >= 2
        and r["payload"]["protocol"].get("archive_lag_hours") is None
        and all(key in r["payload"]["horizons"] for key in ("T-60", "T-1440"))
        and not r["payload"]
        .get("source", {})
        .get("result_version_grades", {})
        .get("lag_assumption")
    ]
    verified_count = 0
    if strict:
        summary = strict[0]["payload"]["horizons"]["T-60"]["summary"]
        verified_count = summary["verified_cutoff_n"]
    fixtures = eligible_fixtures(data_dir)
    by_round = defaultdict(list)
    for fixture in fixtures:
        by_round[(fixture["season"], fixture["round_id"])].append(fixture)
    observed = trial_evidence.qualified_seals(data_dir, fixtures)
    matchday_counts = []
    completed_rounds = set()
    for (season, rnd), group in sorted(by_round.items()):
        if not any(
            (f["fixture_id"], f["kickoff"], h) in observed
            for f in group
            for h in ("T-24h", "T-60m")
        ):
            continue
        coverage = {
            h: sum((f["fixture_id"], f["kickoff"], h) in observed for f in group)
            for h in ("T-24h", "T-60m")
        }
        final = sum(f["home_goals"] is not None for f in group)
        matchday_counts.append(
            {
                "season": season,
                "round": rnd,
                "expected": 10,
                "final": final,
                "sealed": coverage,
            }
        )
        clubs = {football.team_key(f[side]) for f in group for side in ("home", "away")}
        if (
            len(clubs) == 20
            and len(group) == 10
            and final == 10
            and coverage == {"T-24h": 10, "T-60m": 10}
        ):
            completed_rounds.add((season, rnd))
    two_rounds = any(
        (season, rnd - 1) in completed_rounds for season, rnd in completed_rounds
    )
    market, unverified_studies = _market_sample(data_dir)
    # Exercise exact production accounting paths without adding demonstration balances to user accounts.
    with tempfile.TemporaryDirectory(prefix="pm861-readiness-") as temp:
        first = ledger.demo(Path(temp))
        second = ledger.demo(Path(temp))
        counts = first["market_replay"]["counts"]
        ledger_ok = (
            all(counts[k] == 1 for k in ("A", "B", "C", "missing", "filled", "settled"))
            and first["ledger"]["cash"] == second["ledger"]["cash"]
        )
    checks = {
        "multi_season_probability_report": bool(scores),
        "strict_historical_coverage_measured": bool(strict),
        "two_consecutive_observed_technical_rounds": two_rounds,
        "archive_linked_market_replay": bool(market and market["qualified"]),
        "paper_ledger_idempotency": ledger_ok,
    }
    output = {
        "protocol": PROTOCOL,
        "ready": all(checks.values()),
        "checks": checks,
        "backtest": scores[0]["key"] if scores else None,
        "strict_coverage_report": strict[0]["key"] if strict else None,
        "verified_historical_T60": verified_count,
        "completed_technical_rounds": [list(k) for k in sorted(completed_rounds)],
        "observed_matchdays": matchday_counts,
        "market_study": market["manifest_sha256"]
        if market and market["qualified"]
        else None,
        "market_sample": market,
        "unverified_market_studies": unverified_studies,
        "historical_executable_roi": market["historical_roi"]
        if market and market["qualified"]
        else None,
        "pending": [k for k, v in checks.items() if not v],
    }
    save_report(data_dir, "qualification", "latest", output)
    return output


def activate(data_dir, starts_at):
    start = iso(starts_at)
    ready = qualification(data_dir)
    if not ready["ready"]:
        raise ValueError("paper trial is not qualified: " + ", ".join(ready["pending"]))
    if start <= now_utc():
        raise ValueError("a formal trial must begin in a new future window")
    receipt = {
        "protocol": PROTOCOL,
        "starts_at": start,
        "qualification": ready,
        "evidence_sha256": digest(ready),
    }
    with closing(connect(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute(
            "SELECT manifest FROM trial_activations WHERE protocol=? AND starts_at=?",
            (PROTOCOL, start),
        ).fetchone()
        if prior:
            return json.loads(prior["manifest"])
        db.execute(
            "INSERT INTO trial_activations VALUES (?,?,?,?)",
            (PROTOCOL, start, now_utc(), canonical(receipt)),
        )
    return receipt


def register(subparsers):
    trial = subparsers.add_parser(
        "trial", help="从历史证据检查正式前向模拟的启用条件"
    ).add_subparsers(required=True)
    step = trial.add_parser("study-archive")
    step.add_argument(
        "--fixture",
        required=True,
        action="append",
        help="已结束的国际站英超主赛事 slug，可重复",
    )
    step.set_defaults(func=lambda a: study_archive(a.data_dir, a.fixture))
    trial.add_parser("readiness").set_defaults(func=lambda a: qualification(a.data_dir))
    step = trial.add_parser("activate")
    step.add_argument("starts_at")
    step.set_defaults(func=lambda a: activate(a.data_dir, a.starts_at))
