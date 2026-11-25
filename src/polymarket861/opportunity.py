"""Assemble paper opportunities from local, time-stamped exchange and forecast evidence."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import timedelta
from decimal import ROUND_FLOOR

from . import dbscope, football, jobs, ledger, market_provider, store
from . import market_archive as archive
from .ledger_rules import (
    MAX_ORDER,
    MIN_EDGE,
    UNIT,
    ZERO,
    check_book,
    fair_values,
    fmt,
    forecast_fair,
    number,
)


def _event(data_dir, slug, at):
    from .archive_proof import original_receipt

    with dbscope.reading(data_dir, archive.database) as db:
        row = db.execute(
            "SELECT * FROM ma_events WHERE slug=? AND received_at<=? ORDER BY received_at DESC,sha DESC LIMIT 1",
            (slug, store.iso(at)),
        ).fetchone()
        if not row:
            raise ValueError(f"no timely Gamma event receipt for {slug}")
        reply, operation, selector, _ = original_receipt(
            db, data_dir, row["sha"], row["received_at"], {"event", "catalog"}
        )
        if operation == "event" and selector != slug:
            raise ValueError(
                "Gamma response selector differs from the requested fixture"
            )
    raw = reply.body
    if hashlib.sha256(raw).hexdigest() != row["sha"]:
        raise ValueError("Gamma event evidence failed its content hash")
    payload = json.loads(raw)
    events = payload if isinstance(payload, list) else [payload]
    matches = [
        e
        for e in events
        if isinstance(e, dict)
        and e.get("slug") == slug
        and str(e.get("id")) == row["event_id"]
    ]
    if (
        len(matches) != 1
        or not matches[0].get("startTime")
        or store.iso(matches[0]["startTime"]) != store.iso(row["kickoff"])
    ):
        raise ValueError(
            "archive index disagrees with the original Gamma event or schedule"
        )
    return matches[0], row["sha"]


def _club(name):
    # Full club labels are compared. Abbreviations and substrings alone never identify a fixture.
    words = football.team_key(str(name or "").replace("&", " and ")).split("-")
    return "-".join("united" if w == "utd" else w for w in words if w and w != "and")


def verified_fixture(data_dir, market_slug, fixture_id, at):
    key = ("fixture_binding", market_slug, fixture_id, store.iso(at))
    return dbscope.checked(data_dir, key, lambda: _verified_fixture(data_dir, market_slug, fixture_id, at))


def _verified_fixture(data_dir, market_slug, fixture_id, at):
    match = market_provider.FIXTURE.fullmatch(str(market_slug))
    if not match or match[5]:
        raise ValueError("supply the main EPL exchange fixture slug")
    primary, sha = _event(data_dir, market_slug, at)
    local = store.fixture_at(data_dir, fixture_id, at=at, prospective=True)
    times = archive.fixture_times(data_dir, market_slug, as_of=store.iso(at))
    expected = store.iso(local["kickoff"])
    if (
        local.get("precision") == "date"
        or times != [expected]
        or store.iso(primary["startTime"]) != expected
    ):
        raise ValueError(
            "exchange and football schedules do not identify a single precise common kickoff"
        )
    teams = primary.get("teams") or []
    ordered = {}
    for side, short in (("home", match[2]), ("away", match[3])):
        items = [t for t in teams if isinstance(t, dict) and t.get("ordering") == side]
        if len(items) != 1:
            raise ValueError(
                "Gamma event lacks a unique explicitly ordered home and away club"
            )
        club = items[0]
        aliases = {_club(club.get(k)) for k in ("name", "alias") if club.get(k)}
        if (
            not _club(local[side])
            or _club(local[side]) not in aliases
            or (
                club.get("abbreviation")
                and str(club["abbreviation"]).casefold() != short
            )
        ):
            raise ValueError(f"{side} club identity does not match the Gamma event")
        ordered[side] = club
    if _club(local["home"]) == _club(local["away"]):
        raise ValueError("ambiguous football clubs")
    return {
        "market_slug": market_slug,
        "fixture_id": fixture_id,
        "kickoff": expected,
        "home": local["home"],
        "away": local["away"],
        "season": local["season"],
        "source_sha256": sha,
        "event_id": str(primary["id"]),
        "game_id": primary.get("gameId"),
    }


def _locate(data_dir, slug, fixture_id, at):
    if fixture_id:
        return verified_fixture(data_dir, slug, fixture_id, at)
    event, _ = _event(data_dir, slug, at)
    with dbscope.reading(data_dir) as db:
        ids = [
            r[0]
            for r in db.execute(
                "SELECT DISTINCT fixture_id FROM fixture_versions WHERE kickoff=? AND known_at<=?",
                (store.iso(event["startTime"]), store.iso(at)),
            )
        ]
    found = []
    for fid in ids:
        try:
            found.append(verified_fixture(data_dir, slug, fid, at))
        except ValueError:
            continue
    if len(found) != 1:
        raise ValueError(
            "no unique match with the same explicitly ordered clubs and kickoff"
        )
    return found[0]


def _check_child(data_dir, fixture, market, at):
    if market["event_slug"] == fixture["market_slug"]:
        return
    if market["event_slug"] != fixture["market_slug"] + "-more-markets":
        raise ValueError("contract is attached to an unrelated exchange event")
    child, _ = _event(data_dir, market["event_slug"], at)
    if store.iso(child["startTime"]) != fixture["kickoff"]:
        raise ValueError("child market has a different kickoff")
    teams = child.get("teams") or []
    if (
        not teams
        and fixture["game_id"]
        and str(child.get("gameId")) == str(fixture["game_id"])
    ):
        return
    for side in ("home", "away"):
        rows = [r for r in teams if isinstance(r, dict) and r.get("ordering") == side]
        if len(rows) != 1 or _club(fixture[side]) not in {
            _club(rows[0].get("name")),
            _club(rows[0].get("alias")),
        }:
            raise ValueError("child event cannot be bound to both ordered clubs")


def _unique_market(rows):
    if not rows:
        raise ValueError("held token absent from the decision catalog")
    keys = (
        "family",
        "side",
        "line",
        "rule_sha256",
        "tokens",
        "protocol",
        "fees_enabled",
        "fee_schedule",
        "seconds_delay",
        "game_times",
    )
    if len({store.digest({k: r.get(k) for k in keys}) for r in rows}) != 1:
        raise ValueError("conflicting listings for one condition and token")
    return min(rows, key=lambda r: (r["event_slug"], r["slug"]))


def _forecast(data_dir, fixture, cutoff, as_of, version):
    with dbscope.reading(data_dir) as db:
        row = db.execute(
            "SELECT digest,sealed_at FROM forecast_seals WHERE fixture_id=? AND kickoff=? AND cutoff=? "
            "AND model_version=? AND mode='formal' AND horizon='T-60m' AND protocol=? AND sealed_at<=?",
            (
                fixture["fixture_id"],
                fixture["kickoff"],
                store.iso(cutoff),
                version,
                store.PROTOCOL,
                store.iso(as_of),
            ),
        ).fetchone()
    if not row:
        raise ValueError("no timely formal T-60m forecast for this match and model")
    seal = store.check_formal_forecast(
        data_dir,
        row["digest"],
        fixture["fixture_id"],
        fixture["kickoff"],
        store.iso(cutoff),
    )
    return seal | {"digest": row["digest"], "sealed_at": row["sealed_at"]}


def _terms(data_dir, fixture, condition, token, at):
    from .ledger_terms import contract_at

    return contract_at(data_dir, condition, token, at, match_id=fixture["fixture_id"])


def _book(data_dir, contract, target, stage, kickoff, seen_by):
    """Use the same raw-receipt verifier and time ordering as the ledger."""
    with dbscope.reading(data_dir, archive.database) as db:
        return ledger.eligible_book(
            db, data_dir, contract, target, kickoff, stage, observed_by=seen_by
        )


def _intent(fixture, contract, seal, book, cutoff):
    probability = forecast_fair(contract, seal)
    fair, _, _ = fair_values(probability, contract)
    tick = number(book["tick_size"])
    quotes = check_book(
        book, contract, cutoff, store.instant(fixture["kickoff"]), "first"
    )
    limit = max(
        tick, ((fair - MIN_EDGE) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    )
    requested = (
        min(sum((q for _, q in quotes["asks"]), ZERO), MAX_ORDER / quotes["asks"][0][0])
        / UNIT
    ).to_integral_value(rounding=ROUND_FLOOR) * UNIT
    identity = [
        store.PROTOCOL,
        fixture["fixture_id"],
        fixture["kickoff"],
        contract["condition_id"],
        contract["token_id"],
        seal["model_version"],
    ]
    return {
        "match_id": fixture["fixture_id"],
        "market_slug": fixture["market_slug"],
        "kickoff": fixture["kickoff"],
        "decision_at": store.iso(cutoff),
        "intent_id": "opp-" + store.digest(identity),
        "forecast_digest": seal["digest"],
        "prediction_at": seal["sealed_at"],
        "model_version": seal["model_version"],
        "purpose": "main",
        "priority": 0,
        "protocol": store.PROTOCOL,
        "lineups": seal["lineups"],
        "contract": contract,
        "normal_fv": fmt(probability),
        "quantity": fmt(requested),
        "budget": fmt(MAX_ORDER),
        "limit_price": fmt(limit),
    }


def _main_rows(data_dir, account=None, fixture=None):
    with ledger.connection(data_dir) as db:
        sql = "SELECT i.*,a.mode,b.role FROM paper_intents i JOIN paper_accounts a ON a.account=i.account JOIN paper_account_bindings b ON b.account=a.account WHERE a.mode='formal' AND b.role='main'"
        if account != "selector.active":
            sql += " AND i.account NOT GLOB 'selector.*'"
        params = []
        for column, value in (("i.account", account), ("i.fixture", fixture)):
            if value is not None:
                sql += " AND " + column + "=?"
                params.append(value)
        return [
            dict(r)
            for r in db.execute(
                sql
                + " ORDER BY i.cutoff,i.priority,i.fixture,i.condition_id,i.token,i.id,i.account",
                params,
            )
        ]


def _actual(data_dir, intent):
    from .kickoff_proof import resolve

    with dbscope.reading(data_dir) as db:
        return resolve(data_dir, intent["match_id"], intent["kickoff"], intent["decision_at"], db=db)


_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunity_log (
    identity TEXT PRIMARY KEY,account TEXT NOT NULL,market_slug TEXT NOT NULL,cutoff TEXT,
    first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS opportunity_history ON opportunity_log(account,market_slug,first_seen,identity);
"""


def _save_tick(data_dir, report):
    # Keep separate evidence for missing, frozen, and executed opportunities.
    with ledger.connection(data_dir, write=True) as db:
        dbscope.prepare(db, "opportunity-log", _LOG_SCHEMA)
        for stage, entries in (
            ("decision", report["decisions"]),
            ("gap", report["gaps"]),
        ):
            for entry in entries:
                item = {
                    "stage": stage,
                    "account": report["account"],
                    "market_slug": report["market_slug"],
                    "fixture_id": report.get("fixture_id"),
                    "decision_at": report.get("decision_at"),
                    "detail": entry,
                }
                db.execute(
                    "INSERT INTO opportunity_log VALUES(?,?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE "
                    "SET first_seen=min(first_seen,excluded.first_seen),last_seen=max(last_seen,excluded.last_seen)",
                    (
                        store.digest(item),
                        report["account"],
                        report["market_slug"],
                        report.get("decision_at"),
                        report["observed_at"],
                        report["observed_at"],
                        store.canonical(item),
                    ),
                )
        key = report["market_slug"] + "/" + report["account"]
        last = db.execute(
            "SELECT json_extract(payload,'$.observed_at') FROM reports WHERE kind='opportunity' AND key=?",
            (key,),
        ).fetchone()
        if last is None or not last[0] or last[0] <= report["observed_at"]:
            store.put_report(db, "opportunity", key, report)


def history(data_dir, slug, *, account="main"):
    with dbscope.reading(data_dir) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_log'"
        ).fetchone():
            return {"events": []}
        rows = db.execute(
            "SELECT first_seen,last_seen,detail FROM opportunity_log WHERE account=? AND market_slug=? ORDER BY first_seen,identity",
            (account, slug),
        )
        return {
            "events": [
                json.loads(r["detail"])
                | {"first_seen": r["first_seen"], "last_seen": r["last_seen"]}
                for r in rows
            ]
        }


def process(data_dir, slug, *, account="main", fixture_id=None):
    """One local foreground tick. Prices are public archived books; all balances are simulated."""
    report = {"market_slug": slug, "account": account, "decisions": [], "gaps": []}
    try:
        with ledger.fixture_batch(data_dir):
            _process_fixture(data_dir, slug, account, fixture_id, report)
    except (ledger.DecisionWindowClosed, dbscope.EvidenceChanged) as exc:
        # Every change in the failed tick rolled back. Keep only its actual outcome.
        report.update(observed_at=store.now_utc(), decisions=[], gaps=[{"reason": str(exc)}])
        _save_tick(data_dir, report)
    return report


def _process_fixture(data_dir, slug, account, fixture_id, report):
    clock = store.instant(store.now_utc())
    report["observed_at"] = store.iso(clock)

    def gap(reason, **fields):
        report["gaps"].append(fields | {"reason": str(reason)})

    try:
        if not market_provider.FIXTURE.fullmatch(slug) or slug.endswith(
            "-more-markets"
        ):
            raise ValueError("a main EPL event slug is required")
        fixture = _locate(data_dir, slug, fixture_id, clock)
        kickoff = store.instant(fixture["kickoff"])
        cutoff = kickoff - timedelta(hours=1)
        report.update(fixture_id=fixture["fixture_id"], decision_at=store.iso(cutoff))
        if cutoff - timedelta(seconds=30) <= clock <= cutoff:
            store.require_formal(data_dir, at=store.iso(clock))
            version = jobs.model_for_decision(
                data_dir,
                fixture["fixture_id"],
                kickoff,
                "T-60m",
                ("formal",),
                store.iso(clock),
            )
            seal = _forecast(data_dir, fixture, cutoff, clock, version)
            entries = archive.contracts(data_dir, slug, as_of=store.iso(clock))
            ledger.open_account(data_dir, account, "formal", role="main")
            existing = {
                (r["condition_id"], r["token"]): r
                for r in _main_rows(data_dir, account, fixture["fixture_id"])
                if r["cutoff"] == store.iso(cutoff)
            }
            supported = {m.get("family") for m in entries if not m.get("mapping_error")}
            listings = {}
            for m in entries:
                if (
                    not m.get("mapping_error")
                    and m["family"] in market_provider.FAMILIES
                ):
                    for token_row in m["tokens"]:
                        listings.setdefault(
                            (m["condition_id"], token_row["token_id"]), []
                        ).append(m)
            processed = set()
            for family in market_provider.FAMILIES:
                if family not in supported:
                    gap("no verified listing at decision", family=family)
            for m in sorted(entries, key=lambda x: (x["condition_id"], x["slug"])):
                if (
                    m.get("mapping_error")
                    or m["family"] not in market_provider.FAMILIES
                ):
                    continue
                for token_row in sorted(m["tokens"], key=lambda t: t["token_id"]):
                    token = token_row["token_id"]
                    key = m["condition_id"], token
                    if key in processed:
                        continue
                    processed.add(key)
                    fields = {
                        "family": m["family"],
                        "condition_id": m["condition_id"],
                        "token_id": token,
                    }
                    try:
                        old = existing.get(key)
                        if old:
                            report["decisions"].append(
                                fields
                                | {"state": old["status"], "intent_id": old["id"],
                                   "reason": json.loads(old["result"]).get("reason"),
                                   "repeated": True}
                            )
                            continue
                        _unique_market(listings[key])
                        for listing in listings[key]:
                            _check_child(data_dir, fixture, listing, clock)
                        contract = _terms(
                            data_dir, fixture, m["condition_id"], token, clock
                        )
                        first = _book(
                            data_dir, contract, cutoff, "first", kickoff, clock
                        )
                        if not first:
                            raise ValueError(
                                "no qualifying first book available by freeze time"
                            )
                        intent = _intent(fixture, contract, seal, first, cutoff)
                        out = ledger.freeze(data_dir, account, intent, first)
                        report["decisions"].append(
                            fields
                            | {
                                "state": out["status"],
                                "intent_id": out["intent_id"],
                                "reason": out.get("reason"),
                            }
                        )
                    except (ledger.DecisionWindowClosed, dbscope.EvidenceChanged):
                        raise
                    except (ValueError, KeyError, TypeError, OSError) as exc:
                        gap(exc, **fields)
        else:
            report["decision_window"] = "upcoming" if clock < cutoff else "closed"
        formal_rows = _main_rows(data_dir, account, fixture["fixture_id"])
        if clock > cutoff and not any(
            r["cutoff"] == store.iso(cutoff) for r in formal_rows
        ):
            gap("decision window elapsed without a frozen main-account intent")
        for row in formal_rows:
            obj = json.loads(row["result"])
            order = json.loads(row["original"])["intent"]
            if order["kickoff"] != fixture["kickoff"]:
                continue
            c = order["contract"]
            try:
                if row["status"] == "frozen":
                    target = store.instant(obj["execution_target"])
                    if clock < target:
                        continue
                    second = _book(data_dir, c, target, "second", kickoff, clock)
                    if not second and clock < target + timedelta(seconds=5):
                        continue
                    out = ledger.evaluate(data_dir, account, row["id"], second)
                    report["decisions"].append(
                        {
                            "intent_id": row["id"],
                            "state": out["status"],
                            "reason": out.get("reason"),
                        }
                    )
                if (
                    clock >= kickoff
                    and row["status"] in {"filled", "settled"}
                    and obj.get("close_mid") is None
                ):
                    proof = _actual(data_dir, order)
                    actual = store.instant(proof["actual_kickoff"])
                    if actual > clock:
                        continue
                    close = _book(data_dir, c, actual, "close", actual, clock)
                    out = ledger.record_close(
                        data_dir, account, row["id"], close, proof["actual_kickoff"]
                    )
                    report["decisions"].append(
                        {
                            "intent_id": row["id"],
                            "state": out["status"],
                            "clv": out.get("clv"),
                            "reason": out.get("close_missing_reason"),
                        }
                    )
            except dbscope.EvidenceChanged:
                raise
            except (KeyError, TypeError, ValueError, OSError) as exc:
                gap(exc, intent_id=row["id"])
    except (ledger.DecisionWindowClosed, dbscope.EvidenceChanged):
        raise
    except (KeyError, TypeError, ValueError, OSError) as exc:
        gap(exc)
    _save_tick(data_dir, report)
    return report


def _reconstruct(data_dir, slug, row):
    with ledger.connection(data_dir) as db:
        receipt, original = ledger._formal_receipt(data_dir, db, row)
    result = json.loads(row["result"])
    intent = original["intent"]
    cutoff = store.instant(intent["decision_at"])
    sealed = store.instant(receipt["decision_sealed_at"])
    fixture = verified_fixture(data_dir, slug, row["fixture"], sealed)
    kickoff = store.instant(fixture["kickoff"])
    if store.instant(intent["kickoff"]) != kickoff or cutoff != kickoff - timedelta(
        hours=1
    ):
        raise ValueError(
            "original decision is not tied to this fixture schedule version"
        )
    market = [
        m
        for m in archive.contracts(data_dir, slug, as_of=store.iso(sealed))
        if not m["mapping_error"]
        and m["condition_id"] == row["condition_id"]
        and any(t["token_id"] == row["token"] for t in m["tokens"])
    ]
    contract_market = _unique_market(market)
    for m in market:
        _check_child(data_dir, fixture, m, sealed)
    c = _terms(data_dir, fixture, row["condition_id"], row["token"], sealed)
    if c != intent["contract"]:
        raise ValueError(
            "original decision contract differs from its contemporaneous verified terms"
        )
    proof = _actual(data_dir, intent)
    actual = store.instant(proof["actual_kickoff"])
    live_kickoff = min(actual, kickoff)
    first = _book(data_dir, c, cutoff, "first", live_kickoff, sealed)
    if not first or ledger.digest(first) != ledger.digest(original["book"]):
        raise ValueError(
            "original first book differs from the book admissible when the decision was frozen"
        )
    target = cutoff + timedelta(seconds=max(5, int(c["seconds_delay"])))
    second = _book(
        data_dir, c, target, "second", live_kickoff, target + timedelta(seconds=5)
    )
    if not second:
        raise ValueError(
            "no verified same-token execution book in the exchange delay window"
        )
    if result.get("execution_evidence") and ledger.digest(second) != ledger.digest(
        result["execution_evidence"]
    ):
        raise ValueError(
            "original execution record differs from the earliest admissible second book"
        )
    close = _book(data_dir, c, actual, "close", actual, actual)
    with ledger.connection(data_dir) as db:
        settlements = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT payload FROM paper_resolutions WHERE account=? AND condition_id=? AND token=? ORDER BY rowid",
                (row["account"], row["condition_id"], row["token"]),
            )
        ]
    return {
        "market_fixture": slug,
        "match_id": intent["match_id"],
        "family": contract_market["family"],
        "season": fixture["season"],
        "listed": True,
        "venue": market_provider.VENUE,
        "condition_id": row["condition_id"],
        "token_id": row["token"],
        "decision_at": store.iso(cutoff),
        "depth_provenance": "self_archive",
        "kickoff_proof_id": proof["proof_id"],
        "kickoff_source_sha256": proof["source_sha256"],
        "kickoff_source": proof["source_sha256"],
        "actual_kickoff": store.iso(actual),
        **receipt,
        "intent": intent,
        "first_book": first,
        "second_book": second,
        "close_book": close,
        "settlements": settlements,
        "liquidity": "verified two-sided depth",
    }


def archived_inputs(data_dir, slugs, research_rows, *, account=None):
    """Use real formal originals when available and preserve B/C and unlisted denominators."""
    originals = _main_rows(data_dir, account)
    if not originals:
        return research_rows
    requested = set(slugs)
    with dbscope.reading(data_dir, archive.database) as db:
        conditions = {}
        for slug in requested:
            for r in db.execute(
                "SELECT DISTINCT json_extract(payload,'$.condition_id') FROM ma_contracts WHERE fixture=?",
                (slug,),
            ):
                conditions.setdefault(r[0], set()).add(slug)
    grouped = {}
    for row in originals:
        for slug in conditions.get(row["condition_id"], ()):
            key = (slug, row["condition_id"], row["token"], row["cutoff"])
            grouped.setdefault(key, []).append(row)
    if not grouped:
        return research_rows
    replacements = {}
    for (slug, condition, token, cutoff), rows in sorted(grouped.items()):
        result = {
            "market_fixture": slug,
            "condition_id": condition,
            "token_id": token,
            "decision_at": cutoff,
        }
        try:
            if len(rows) != 1:
                raise ValueError(
                    "multiple formal originals; select the predeclared paper account"
                )
            result.update(_reconstruct(data_dir, slug, rows[0]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["archive_gap"] = str(exc)
        replacements.setdefault((slug, condition, token), []).append(result)
    assembled = []
    identities = {}
    for values in replacements.values():
        for item in values:
            if "intent" in item:
                identities.setdefault(item["market_fixture"], set()).add(
                    item["match_id"]
                )

    def append(base, more):
        group = identities.get(base["match_id"], set())
        if len(group) == 1:
            base = base | {
                "market_fixture": base["match_id"],
                "match_id": next(iter(group)),
            }
        assembled.append(base | more)

    for base in research_rows:
        key = (base["match_id"], base.get("condition_id"), base.get("token_id"))
        for item in replacements.pop(key, [{}]):
            append(base, item)
    for values in replacements.values():
        for item in values:
            if "intent" in item:
                assembled.append(item | {"trades": [], "prices": []})
    return assembled


def register(subparsers):
    actions = subparsers.add_parser(
        "opportunity", help="从已封存赛前预测、国际站规则和双边订单簿装配模拟机会"
    ).add_subparsers(dest="opportunity_action", required=True)
    cmd = actions.add_parser(
        "run", help="按本机时钟冻结、执行并登记收盘；行情来自 market watch 归档"
    )
    cmd.add_argument("market_slug", help="国际站英超主赛事 slug")
    cmd.add_argument("--fixture-id", help="联赛 fixture_id，仍需验证主客双方和赛程")
    cmd.add_argument("--account", default="main", help="正式模拟主账户")
    cmd.add_argument("--repeat", type=float, help="前台检查间隔秒数，例如 1")

    def run(args):
        if args.repeat is not None and not 0 < args.repeat < float("inf"):
            raise ValueError("repeat must be positive and finite")
        while True:
            out = process(
                args.data_dir,
                args.market_slug,
                account=args.account,
                fixture_id=args.fixture_id,
            )
            if args.repeat is None:
                return out
            print(json.dumps(out, ensure_ascii=False), flush=True)
            time.sleep(args.repeat)

    cmd.set_defaults(func=run)
    log = actions.add_parser("history", help="回查对应账户各时点的冻结、成交及缺口")
    log.add_argument("market_slug")
    log.add_argument("--account", default="main")
    log.set_defaults(
        func=lambda a: history(a.data_dir, a.market_slug, account=a.account)
    )
