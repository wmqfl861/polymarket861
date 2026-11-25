"""Evidence retained by the separate scheduled-kickoff research account."""

import json
from contextlib import closing
from datetime import timedelta

from . import archive_proof, jobs, ledger_terms, store
from .ledger_rules import check_book, digest, fmt, instant, iso, number, unit


def _fee(root, market, at):
    try:
        return ledger_terms._fee(market, root, at)
    except ValueError:
        # This model is admissible only for a research balance. Fees-disabled contradictions stay gaps.
        s = market.get("fee_schedule")
        if (
            market.get("fees_enabled") is not True
            or not isinstance(s, dict)
            or s.get("takerOnly") is not True
        ):
            raise ValueError("no interpretable archived Gamma taker fee schedule")
        if set(s) - {"takerOnly", "rate", "exponent", "rebateRate"}:
            raise ValueError("unknown feeSchedule properties")
        rate = unit(s.get("rate"))
        exponent = number(s.get("exponent"))
        if rate <= 0 or exponent not in (1, 2) or isinstance(s.get("exponent"), bool):
            raise ValueError("unrecognized Gamma fee rate or exponent")
        if "rebateRate" in s:
            unit(s["rebateRate"])
        profile = {
            "policy": "research-gamma-shares-cent-ceiling-v1",
            "protocol": market["protocol"],
            "gamma_fee_schedule": s,
            "model": "symmetric",
            "rate": fmt(rate),
            "exponent": int(exponent),
            "asset": "shares",
            "quantum": "0.01",
            "round_in": "cash",
            "rounding": "up",
            "rebates": "none",
            "assumption": "taker fee per executed price level, rounded up to one collateral cent, converted to shares and rounded up to 0.000001 share",
        }
        return profile | {
            "version": digest(profile),
            "enabled": True,
            "effective_at": market["recorded_at"],
            "received_at": market["recorded_at"],
            "source_sha256": market["source_sha256"],
            "research_assumption": True,
        }


def terms(db, root, condition, token, at, fixture):
    market = archive_proof.original_market(db, root, condition, at)
    return ledger_terms._terms_with_fee(
        root, market, token, at, fixture, _fee(root, market, at)
    )


def _verify_fee(contract, market, *, data_dir, at):
    current = _fee(data_dir, market, at)
    if (
        market["rule_sha256"] != contract["rule_sha256"]
        or market["seconds_delay"] != contract["seconds_delay"]
        or current["version"] != contract["fee_schedule"]["version"]
    ):
        raise ValueError("research rule or fee profile changed")


def book(db, root, contract, target, kickoff, stage, *, seen_by):
    cutoff, now, planned = map(instant, (target, seen_by, kickoff))
    begin = cutoff if stage == "second" else cutoff - timedelta(seconds=30)
    end = min(now, cutoff + timedelta(seconds=5) if stage == "second" else cutoff)
    direction = "ASC" if stage == "second" else "DESC"
    rows = db.execute(
        "SELECT sha,received_at,payload FROM ma_books WHERE token=? AND received_at>=? AND received_at<=? "
        f"ORDER BY received_at {direction},sha {direction}",
        (contract["token_id"], iso(begin), iso(end)),
    )
    for row in rows:
        try:
            raw, market = archive_proof._original_book(
                db, root, contract, row, fee_check=_verify_fee
            )
            if (
                market["game_times"] != [iso(planned)]
                or market["market_open"] is not True
                or any(
                    market.get(k) is not False and market.get(k) is not None
                    for k in ("live", "ended")
                )
            ):
                continue
            basis = (
                "explicit_pregame"
                if raw.get("pregame") is True
                else "scheduled_with_incomplete_flags"
            )
            if raw.get("pregame") is False or not instant(raw["received_at"]) < planned:
                continue
            # Rebuilding the untouched native receipt precedes attaching this separate research classification.
            sample = raw | {
                "pregame": True,
                "research_pregame_basis": basis,
                "native_pregame": raw.get("pregame"),
            }
            check_book(sample, contract, cutoff, planned, stage)
            return sample
        except (ValueError, KeyError, TypeError, OSError, OverflowError):
            continue
    return None


def forecast(root, fixture, cutoff, seen_by):
    when = iso(instant(seen_by))
    version = jobs.model_for_decision(
        root,
        fixture["fixture_id"],
        fixture["kickoff"],
        "T-60m",
        ("formal", "technical"),
        when,
    )
    with closing(store.connect(root)) as db:
        rows = db.execute(
            "SELECT * FROM forecast_seals WHERE fixture_id=? AND kickoff=? AND cutoff=? AND horizon='T-60m' "
            "AND mode IN ('technical','formal') AND protocol=? AND model_version=? AND sealed_at<=? "
            "ORDER BY sealed_at DESC,mode ASC",
            (
                fixture["fixture_id"],
                fixture["kickoff"],
                iso(instant(cutoff)),
                store.PROTOCOL,
                version,
                when,
            ),
        )
        for row in rows:
            data = json.loads(row["data_json"])
            if (
                store.digest(data) != row["digest"]
                or data.get("model_version") != version
                or data.get("fixture_id") != fixture["fixture_id"]
                or data.get("kickoff") != fixture["kickoff"]
                or data.get("cutoff") != row["cutoff"]
                or data.get("mode") != row["mode"]
                or data.get("sealed_at") != row["sealed_at"]
                or data.get("source_health", {}).get("critical_stale")
            ):
                continue
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='football_input_manifests'"
            ).fetchone():
                continue
            manifest = db.execute(
                "SELECT manifest,observed_at FROM football_input_manifests WHERE digest=?",
                (data.get("input_manifest_sha256"),),
            ).fetchone()
            if (
                not manifest
                or store.digest(json.loads(manifest["manifest"]))
                != data["input_manifest_sha256"]
                or manifest["observed_at"] > row["sealed_at"]
            ):
                continue
            labels = {}
            for side in ("home", "away"):
                lineup = (data.get("lineups") or {}).get(side) or {}
                labels[side] = bool(
                    lineup.get("confirmed") is True
                    and lineup.get("received_at")
                    and instant(lineup["received_at"]) <= instant(row["sealed_at"])
                )
            return data | {"digest": row["digest"], "lineup_stratum": labels}
    raise ValueError("no timely authenticated technical or formal T-60 forecast")
