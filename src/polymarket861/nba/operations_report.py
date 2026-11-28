"""Immutable result adjudications and NBA Eastern-calendar reporting."""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, timedelta

from ..store import canonical, digest, instant
from . import storage
from .operations import (
    EASTERN,
    HORIZONS,
    _dirty_day,
    _observed_markets,
    _reference,
    _unpack,
)


def _sealed_quotes(seal):
    return _observed_markets(
        seal["market_at_prediction"],
        seal["input_cutoff"],
        seal["game"]["kickoff"],
        seal["horizon"],
        seal.get("source_epoch_at"),
    )


def refresh_pairing(db, root, now):
    """Freeze the price that was possible by the deadline, separately from the seal."""
    windows = db.execute(
        """SELECT w.*
           FROM nba_windows w LEFT JOIN nba_pairing_outcomes p USING(game_id,epoch,horizon)
           WHERE w.active=1 AND w.sealed_at IS NOT NULL AND w.deadline<? AND p.game_id IS NULL""",
        (now,),
    ).fetchall()
    for w in windows:
        # Never admit this look-up to a prediction or scored decision reference.
        # With no win in the original, ask what was already durable at the true
        # cutoff, even if the operator first runs this task after the game.
        seal = _unpack(w)
        epoch = seal.get("source_epoch_at")
        win = _sealed_quotes(seal).get("win")
        if win is None:
            raw = _reference(root, seal["game"], w["horizon"], w["deadline"], epoch)
            win = _observed_markets(
                raw, w["deadline"], w["kickoff"], w["horizon"], epoch
            ).get("win")
        quote = canonical(win) if win else None
        with db:
            changed = db.execute(
                "INSERT OR IGNORE INTO nba_pairing_outcomes VALUES(?,?,?,?,?,?)",
                (
                    w["game_id"],
                    w["epoch"],
                    w["horizon"],
                    now,
                    quote,
                    digest([w["sha256"], now, quote]),
                ),
            ).rowcount
            if changed:
                _dirty_day(db, w["game_day"])


def _new_grade(db, row, body, now):
    key = (row["game_id"], row["epoch"], row["horizon"])
    sha = digest(body)
    last = db.execute(
        """SELECT revision,sha256 FROM nba_grade_versions
                         WHERE game_id=? AND epoch=? AND horizon=? ORDER BY revision DESC LIMIT 1""",
        key,
    ).fetchone()
    if last and last["sha256"] == sha:
        return 0
    revision = last["revision"] + 1 if last else 1
    db.execute(
        "INSERT INTO nba_grade_versions VALUES(?,?,?,?,?,?,?)",
        (*key, revision, now, canonical(body), sha),
    )
    return 1


def _market_errors(models, reference, home, away):
    """Compare the same pregame side and line across model, market and verified close."""
    out = {}
    for kind, ref in reference.items():
        if kind not in ("win", "total", "home_total", "away_total"):
            continue
        target = {
            "win": None,
            "total": home + away,
            "home_total": home,
            "away_total": away,
        }[kind]
        line = ref.get("line")
        if kind != "win" and (line is None or target == line):
            continue
        outcome = float(home > away) if kind == "win" else float(target > line)
        p = ref["probability"]
        scores = {}
        for name, pred in models.items():
            if kind == "win":
                model_p = pred["p_home_win"]
            else:
                field = {"total": "total", "home_total": "home", "away_total": "away"}[
                    kind
                ]
                dist = pred[field]["distribution"]
                model_p = sum(
                    n
                    for v, n in zip(dist["values"], dist["counts"], strict=True)
                    if v > line
                ) / sum(dist["counts"])
            close = ref.get("close_probability")
            scores[name] = {
                "model_brier": (model_p - outcome) ** 2,
                "market_brier": (p - outcome) ** 2,
                "brier_difference": (model_p - outcome) ** 2 - (p - outcome) ** 2,
                "market_vs_close_squared": (p - close) ** 2
                if close is not None
                else None,
                "model_vs_close_squared": (model_p - close) ** 2
                if close is not None
                else None,
            }
        out[kind] = {
            "condition_id": ref["condition_id"],
            "token_id": ref["token_id"],
            "line": line,
            "observed_at": ref["observed_at"],
            "market_probability": p,
            "close_probability": ref.get("close_probability"),
            "close_at": ref.get("close_at"),
            "close_state": "qualified"
            if ref.get("close_at")
            else "no_qualified_scheduled_close",
            "models": scores,
        }
    return out


def _adjudicate(db, root, row, game, now):
    from . import model, reconciliation

    seal = _unpack(row)
    reference = _sealed_quotes(seal)
    body = {
        "forecast_sha256": row["sha256"],
        "state": "awaiting_result",
        "payout_reconciliation": reconciliation.by_reference(
            db, game["game_id"], reference, source_kickoff=seal["game"]["kickoff"]
        ),
    }
    if game["status"] != "scheduled":
        body.update(
            result_version=game["version"], result_receipt_sha256=game["receipt_sha256"]
        )
    if not row["active"]:
        body["state"] = "withdrawn_schedule"
        return body
    if game["status"] != "final":
        return body
    target = seal["game"]
    if any(game[k] != target[k] for k in ("home", "away", "kickoff")):
        body["state"] = "withheld_result_identity"
        return body
    home, away = game["home_score"], game["away_score"]
    if not all(type(n) is int and n > 0 for n in (home, away)) or home == away:
        body["state"] = "withheld_invalid_final"
        return body
    preds = seal["forecast"]["models"]
    # The decision reference must have been inside the immutable forecast. A later
    # qualified observation may supply only the close of that exact contract.
    closing_refs = (
        _reference(root, target, row["horizon"], now, seal.get("source_epoch_at"))
        if reference
        else {}
    )
    for family, ref in reference.items():
        closing = closing_refs.get(family, {})
        if (
            closing.get("close_probability") is not None
            and closing.get("close_at")
            and all(
                ref[k] == closing.get(k) for k in ("condition_id", "token_id", "line")
            )
            and instant(ref["observed_at"])
            <= instant(closing["close_at"])
            < instant(target["kickoff"])
            and instant(closing["close_at"]) <= instant(now)
        ):
            ref.update(
                close_probability=closing["close_probability"],
                close_at=closing["close_at"],
            )
    body.update(
        state="scored",
        final=[home, away],
        models={k: model.score(v, home, away) for k, v in preds.items()},
        benchmarks=_market_errors(preds, reference, home, away),
    )
    return body


def refresh_grades(db, root, now):
    changes, errors = 0, []
    for (gid,) in db.execute(
        "SELECT game_id FROM nba_grade_queue ORDER BY game_id"
    ).fetchall():
        with db:
            db.execute("BEGIN IMMEDIATE")
            game = json.loads(
                db.execute(
                    "SELECT game FROM nba_ops_games WHERE game_id=?", (gid,)
                ).fetchone()[0]
            )
            failed = False
            for row in db.execute(
                "SELECT * FROM nba_windows WHERE game_id=? AND sealed_at IS NOT NULL",
                (gid,),
            ).fetchall():
                try:
                    body = _adjudicate(db, root, row, game, now)
                except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                    failed = True
                    errors.append(
                        {"game_id": gid, "horizon": row["horizon"], "error": str(exc)}
                    )
                    body = {
                        "forecast_sha256": row["sha256"],
                        "state": "unavailable",
                        "error": str(exc),
                    }
                if _new_grade(db, row, body, now):
                    changes += 1
                    _dirty_day(db, row["game_day"])
            if not failed:
                db.execute("DELETE FROM nba_grade_queue WHERE game_id=?", (gid,))
    return changes, errors


def _difference_interval(day_totals):
    """Descriptive percentile interval; resample whole Eastern game dates, then weight games equally."""
    days = len(day_totals)
    info = {
        "interval": None,
        "matched_days": days,
        "minimum_days": 4,
        "method": "paired_eastern_game_day_cluster_percentile",
        "level": 0.95,
        "draws": 2000,
        "seed": 861,
        "scope": "exploratory_period_only",
    }
    if days < info["minimum_days"]:
        return dict(info, reason="too_few_paired_game_days")
    clusters = [day_totals[k] for k in sorted(day_totals)]
    rng = random.Random(info["seed"])
    draws = []
    for _ in range(info["draws"]):
        sampled = rng.choices(clusters, k=days)
        draws.append(sum(s for s, n in sampled) / sum(n for s, n in sampled))
    draws.sort()
    # Nearest-rank percentile limits for 2,000 deterministic replicates.
    info["interval"] = [draws[49], draws[1949]]
    return info


def _miss_reason(note):
    if note.startswith(("scheduled source failed", "latest schedule scan failed")):
        return "latest_schedule_sync_failed"
    if note == "waiting_for_win_quote":
        return "no_retry_before_deadline"
    if note.startswith("stale schedule"):
        return "stale_schedule_proof"
    if note.startswith("missing or incomplete schedule"):
        return "incomplete_schedule_proof"
    if note.startswith("deadline passed"):
        return "deadline_passed_during_prediction"
    if note.startswith("market schedule conflict: "):
        return note.removeprefix("market schedule conflict: ")
    return note or "window_expired"


def _readiness(games, windows, horizon, now):
    """Return unique scheduled opportunities plus both sides of the pairing denominator."""
    by_id = {w["game_id"]: w for w in windows}
    end = instant(now)
    today = str(end.astimezone(EASTERN).date())
    fixtures = []
    for gid in sorted(games.keys() | by_id.keys()):
        w = by_id.get(gid)
        game = games.get(gid) or w
        kickoff = w["kickoff"] if w else game["kickoff"]
        if kickoff:
            due = instant(kickoff) - timedelta(minutes=HORIZONS[horizon]) < end
        else:
            due = game["status"] == "final" or game["game_day"] < today
        sealed = bool(w and w["state"] == "sealed")
        warm = bool(sealed and w["warm"])
        decision = bool(sealed and "win" in w["decision"])
        qualified = bool(sealed and (decision or w["win_by_deadline"]))
        fixtures.append(
            {
                "game_id": gid,
                "game_day": game["game_day"],
                "home_abbr": game["home_abbr"],
                "away_abbr": game["away_abbr"],
                "kickoff": kickoff,
                "venue_event_id": w["venue_id"] if w else None,
                "due": due,
                "sealed": sealed,
                "warm_sealed": warm,
                "qualifying_win_quote": warm and qualified,
                "paired_win": warm and decision,
                "assessed": bool(w and w["assessed_at"]),
                "any_seal_qualifying_win_quote": qualified,
                "any_seal_paired_win": decision,
            }
        )
    due = [f for f in fixtures if f["due"]]
    warm_n = sum(f["warm_sealed"] for f in due)
    sealed_n = sum(f["sealed"] for f in due)
    qualified_n = sum(f["qualifying_win_quote"] for f in due)
    paired_n = sum(f["paired_win"] for f in due)
    all_qualified = sum(f["any_seal_qualifying_win_quote"] for f in due)
    all_paired = sum(f["any_seal_paired_win"] for f in due)
    return {
        "due_games": len(due),
        "sealed_games": sealed_n,
        "warm_sealed_games": warm_n,
        "cold_or_unverified_sealed_games": sealed_n - warm_n,
        "qualifying_win_quote_games": qualified_n,
        "paired_win_games": paired_n,
        "warm_seal_rate": warm_n / len(due) if due else None,
        "win_pairing_rate": paired_n / qualified_n if qualified_n else None,
        "all_seals_qualifying_win_quote_games": all_qualified,
        "all_seals_paired_win_games": all_paired,
        "all_seals_win_pairing_rate": all_paired / all_qualified
        if all_qualified
        else None,
        "fixtures": fixtures,
    }


def _period(db, start, end, now):
    from . import reconciliation

    windows = []
    for row in db.execute(
        """SELECT w.*,p.win_quote,p.assessed_at,p.sha256 AS assessment_sha
           FROM nba_windows w LEFT JOIN nba_pairing_outcomes p USING(game_id,epoch,horizon)
           WHERE w.game_day>=? AND w.game_day<?""",
        (str(start), str(end)),
    ):
        seal = _unpack(row) if row["sealed_at"] and row["active"] else {}
        if (
            row["assessed_at"]
            and digest([row["sha256"], row["assessed_at"], row["win_quote"]])
            != row["assessment_sha"]
        ):
            raise ValueError("NBA deadline quote assessment checksum mismatch")
        schedule = seal.get("market_schedule", {})
        decision = _sealed_quotes(seal) if seal else {}
        window = {
            key: row[key]
            for key in (
                "game_id",
                "game_day",
                "horizon",
                "state",
                "active",
                "note",
                "kickoff",
                "assessed_at",
            )
        }
        window.update(
            venue_id=schedule.get("venue_event_id"),
            match_status=schedule.get("status"),
            quote_state=seal.get("decision_quote_state", "unrecorded"),
            home_abbr=seal.get("game", {}).get("home_abbr"),
            away_abbr=seal.get("game", {}).get("away_abbr"),
            decision=decision.keys(),
            warm=seal.get("warmup", {}).get("eligible") is True,
            win_by_deadline=row["win_quote"] is not None,
        )
        windows.append(window)
    ids = {r["game_id"] for r in windows if r["active"]}
    games = {
        row["game_id"]: row
        for row in db.execute(
            """SELECT game_id,game_day,status,json_extract(game,'$.kickoff') AS kickoff,
                      json_extract(game,'$.home_abbr') AS home_abbr,json_extract(game,'$.away_abbr') AS away_abbr
               FROM nba_ops_games WHERE stage='regular' AND game_day>=? AND game_day<?""",
            (str(start), str(end)),
        )
    }
    relevant = games.keys()
    grades = db.execute(
        """SELECT w.horizon,w.game_day,g.payload,g.sha256 FROM nba_windows w JOIN nba_grade_versions g
        USING (game_id,epoch,horizon) WHERE w.active=1 AND w.game_day>=? AND w.game_day<?
        AND g.revision=(SELECT MAX(p.revision) FROM nba_grade_versions p
            WHERE p.game_id=w.game_id AND p.epoch=w.epoch AND p.horizon=w.horizon)""",
        (str(start), str(end)),
    ).fetchall()
    by_horizon = {}
    for horizon in HORIZONS:
        wr = [w for w in windows if w["horizon"] == horizon and w["active"]]
        superseded = Counter(
            w["state"] for w in windows if w["horizon"] == horizon and not w["active"]
        )
        counts = Counter(w["state"] for w in wr)
        states = Counter()
        market_counts, closing_counts = Counter(), Counter()
        sums = defaultdict(Counter)
        paired = defaultdict(lambda: defaultdict(Counter))
        by_day = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0.0, 0])))
        for raw in grades:
            if raw["horizon"] != horizon:
                continue
            g = _unpack(raw)
            states[g["state"]] += 1
            if g["state"] != "scored":
                continue
            for name, score in g["models"].items():
                sums[name].update(score)
                sums[name]["games"] += 1
            for family, ref in g["benchmarks"].items():
                market_counts[family] += 1
                closing_counts[family] += int(ref["close_probability"] is not None)
                for name, scores in ref["models"].items():
                    acc = paired[family][name]
                    acc["games"] += 1
                    cluster = by_day[family][name][raw["game_day"]]
                    cluster[0] += scores["brier_difference"]
                    cluster[1] += 1
                    for key in ("model_brier", "market_brier", "brier_difference"):
                        acc[key] += scores[key]
                    if scores["model_vs_close_squared"] is not None:
                        acc["closing_games"] += 1
                        acc["close_squared_sum"] += scores["model_vs_close_squared"]
                        acc["market_close_squared_sum"] += scores[
                            "market_vs_close_squared"
                        ]
        comp = {k: {} for k in ("win", "total", "home_total", "away_total")}
        for family, arms in paired.items():
            comp[family] = {}
            for name, a in arms.items():
                comp[family][name] = {
                    k: a[k] / a["games"]
                    for k in ("model_brier", "market_brier", "brier_difference")
                }
                comp[family][name].update(
                    games=a["games"],
                    closing_games=a["closing_games"],
                    brier_difference_interval=_difference_interval(
                        by_day[family][name]
                    ),
                    market_vs_close_mse=a["market_close_squared_sum"]
                    / a["closing_games"]
                    if a["closing_games"]
                    else None,
                    model_vs_close_mse=a["close_squared_sum"] / a["closing_games"]
                    if a["closing_games"]
                    else None,
                )
        by_horizon[horizon] = {
            "states": dict(counts),
            "missed_by_reason": dict(
                Counter(_miss_reason(w["note"]) for w in wr if w["state"] == "missed")
            ),
            "withheld_by_reason": dict(
                Counter(_miss_reason(w["note"]) for w in wr if w["state"] == "withheld")
            ),
            "unpaired_win_seals_by_schedule_match": dict(
                Counter(
                    w["match_status"] or "unrecorded"
                    for w in wr
                    if w["state"] == "sealed" and "win" not in w["decision"]
                )
            ),
            "unpaired_win_seals_by_reason": dict(
                Counter(
                    w["quote_state"]
                    for w in wr
                    if w["state"] == "sealed" and "win" not in w["decision"]
                )
            ),
            "readiness": _readiness(games, wr, horizon, now),
            "opportunities": len(ids | relevant),
            "registered_windows": len(wr),
            "without_registered_window": len(relevant - ids),
            "superseded_states": dict(superseded),
            "grades": dict(states),
            "models": {
                k: {
                    "games": v["games"],
                    **{m: t / v["games"] for m, t in v.items() if m != "games"},
                }
                for k, v in sums.items()
            },
            "market_comparisons": comp,
            "market_coverage": {
                k: {
                    "scored": states["scored"],
                    "interval_unavailable_reason": "no_paired_decision_quotes"
                    if not market_counts[k]
                    else None,
                    "with_decision_quote": market_counts[k],
                    "sealed_with_decision_quote": sum(
                        k in w["decision"] for w in wr if w["state"] == "sealed"
                    ),
                    "missing_decision_quote": states["scored"] - market_counts[k],
                    "with_same_token_close": closing_counts[k],
                    "decision_without_close": market_counts[k] - closing_counts[k],
                }
                for k in comp
            },
        }
    start_week = start - timedelta(days=start.weekday())
    return {
        "start": str(start),
        "end_exclusive": str(end),
        "timezone": "America/New_York",
        "week_start": str(start_week),
        "schedule_games": len(ids | relevant),
        "without_registered_windows": len(relevant - ids),
        "horizons": by_horizon,
        "payout_reconciliation": reconciliation.summary(db, start, end),
        "denominator": "locally observed NBA regular-season schedule; upstream coverage is not guaranteed",
        "score_scope": "final_including_overtime",
        "paper_trading": False,
    }


def refresh_reports(db, now):
    count = 0
    pending = db.execute(
        "SELECT kind,period FROM nba_report_queue ORDER BY kind,period"
    ).fetchall()
    for kind, period in pending:
        with db:
            db.execute("BEGIN IMMEDIATE")
            start = date.fromisoformat(period)
            span = 1 if kind == "day" else 7
            payload = _period(db, start, start + timedelta(days=span), now)
            sha = digest(payload)
            db.execute(
                "DELETE FROM nba_report_queue WHERE kind=? AND period=?", (kind, period)
            )
            prev = db.execute(
                """SELECT revision,sha256 FROM nba_period_reports WHERE kind=? AND period=?
                               ORDER BY revision DESC LIMIT 1""",
                (kind, str(start)),
            ).fetchone()
            if prev and prev["sha256"] == sha:
                continue
            db.execute(
                "INSERT INTO nba_period_reports VALUES(?,?,?,?,?,?)",
                (
                    kind,
                    str(start),
                    prev["revision"] + 1 if prev else 1,
                    now,
                    canonical(payload),
                    sha,
                ),
            )
            count += 1
    return count


def report(root, kind, start, revision=None):
    start = date.fromisoformat(start)
    if kind == "week" and start.weekday():
        raise ValueError("--week requires a Monday (Eastern game date)")
    db = storage.open_existing(root)
    if db is not None:
        with closing(db):
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='nba_period_reports'"
            ).fetchone():
                base = "SELECT * FROM nba_period_reports WHERE kind=? AND period=?"
                row = (
                    db.execute(
                        base + " ORDER BY revision DESC LIMIT 1", (kind, str(start))
                    ).fetchone()
                    if revision is None
                    else db.execute(
                        base + " AND revision=?", (kind, str(start), revision)
                    ).fetchone()
                )
                if row:
                    return {
                        "state": "available",
                        "revision": row["revision"],
                        "generated_at": row["generated_at"],
                        "sha256": row["sha256"],
                        "report": _unpack(row),
                    }
    return {
        "state": "unavailable",
        "period": str(start),
        "kind": kind,
        "revision": revision,
    }
