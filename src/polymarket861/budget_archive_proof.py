"""Read-only attestation of books and redemptions for the five-league capital replay."""

from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from . import (
    archive_proof,
    big5_market,
    budget_schedule,
    dbscope,
    fee_registry,
    league_registry,
    ledger_rules as rules,
    ledger_terms,
    settlement,
    shadow_market,
)


@contextmanager
def _readonly(archive):
    with closing(sqlite3.connect((archive / "runtime.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        yield db


@contextmanager
def read_archive(data_dir):
    if data_dir is None:
        yield None
        return
    archive = Path(data_dir)
    nested = big5_market.research_root(archive)
    if (nested / "runtime.sqlite3").is_file():
        archive = nested
    db_file = archive / "runtime.sqlite3"
    if not db_file.is_file():
        yield None
        return
    with _readonly(archive) as db:
        with dbscope.bind(archive, db):
            yield (archive, db)


@contextmanager
def football_archive(source):
    # In a deployment, primary football receipts can be a sibling of the dedicated market store.
    if source:
        market = source[0].resolve()
        parent = market.parent
        if market == big5_market.research_root(parent) and (parent / "runtime.sqlite3").is_file():
            with _readonly(parent) as db:
                yield parent, db
            return
    yield None


def _paper_fee(root, market, at):
    # In this opt-in account, an expired, unmatched, ambiguous or unavailable
    # registered policy must fail its usual checks at each individual receipt.
    if market.get("fees_enabled") is not True or any(
        p.protocol == market["protocol"] and market["family"] in p.families
        for p in fee_registry.REVIEWED
    ):
        return ledger_terms._fee(market, root, at)
    if (
        market["venue"] != "polymarket.com" or market["protocol"] not in {"v1", "v2"}
        or rules.instant(market["recorded_at"]) > rules.instant(at)
    ):
        raise ValueError("paper fee estimate requires a timely international Gamma original")
    return shadow_market._fee(root, market, at)


def _scheduled_book_fee(contract, market, *, data_dir, at):
    if contract["fee_schedule"].get("research_assumption") is not True:
        return ledger_terms.verify_book_terms(contract, market, data_dir=data_dir, at=at)
    observed = _paper_fee(data_dir, market, at)
    if (
        market["rule_sha256"] != contract["rule_sha256"]
        or market["seconds_delay"] != contract["seconds_delay"]
        or observed["version"] != contract["fee_schedule"]["version"]
    ):
        raise ValueError("paper fee estimate or frozen rule changed at book capture")


def _terms(root, market, token, at, *, scheduled):
    if not scheduled:
        return ledger_terms.from_original(root, market, token, at)
    return ledger_terms._terms_with_fee(root, market, token, at, None, _paper_fee(root, market, at))


def _fee_basis(fee):
    if not fee["enabled"]:
        return "gamma_fees_disabled"
    if fee.get("research_assumption") is True:
        return "research_assumption"
    return "authoritative"


def _intent_terms(terms):
    return {
        "condition_id": terms["condition_id"], "token_id": terms["token_id"],
        "selection": {k: terms[k] for k in ("family", "selection", "team", "line", "side") if k in terms},
        "rule_sha256": terms["rule_sha256"], "fee": terms["fee_schedule"],
    }


def verify_contract(data_dir, condition, as_of, *, token=None, scheduled=False):
    """Independently inspect a retained Gamma contract even if no timely CLOB pair exists."""
    at = rules.iso(rules.instant(as_of))
    with read_archive(data_dir) as source:
        if source is None:
            raise ValueError("no local market archive")
        root, db = source
        market = archive_proof.original_market(db, root, condition, at)
        league = market["fixture"].split("-", 1)[0]
        native = archive_proof._event_capture(
            db, root, market["source_sha256"], market["recorded_at"], market["event_slug"]
        )
        _, main, _, kickoff, home, away, event_id = big5_market._identity(native, league)
        result = {
            "scope": "archived_gamma_contract", "as_of": at, "league": league,
            "fixture": main, "event_slug": market["event_slug"], "event_id": event_id,
            "kickoff": kickoff, "home_club_id": home, "away_club_id": away,
            "condition_id": condition, "market_slug": market["slug"], "tokens": market["tokens"],
            "source_sha256": market["source_sha256"], "received_at": market["recorded_at"],
            "order_books_checked": 0,
        }
        if token is not None:
            terms = _terms(root, market, token, at, scheduled=scheduled)
            result["intent_terms"] = _intent_terms(terms)
            result["fee_basis"] = _fee_basis(terms["fee_schedule"])
        return result


def _gamma_identity(source, market, fixture, kickoff):
    root, db = source
    league, season, home_id, away_id = fixture.split("/")
    event = archive_proof._event_capture(
        db, root, market["source_sha256"], market["recorded_at"], market["event_slug"]
    )
    _, main, verified_league, when, home, away, _ = big5_market._identity(event, league)
    local_day = kickoff.astimezone(
        ZoneInfo(league_registry.profile(league).timezone)
    ).date()
    first, last = league_registry.season_window(league, season)
    if not (
        first <= local_day < last
        and (verified_league, home, away) == (league, home_id, away_id)
        and main == market["fixture"]
        and rules.instant(when) == kickoff
        and len(market["game_times"]) == 1
        and rules.instant(market["game_times"][0]) == kickoff
    ):
        raise ValueError(
            "entry fixture, ordered Gamma club IDs, league, season or kickoff differs from original"
        )


def entry(source, op, candidates, *, scheduled=False, as_of=None, football=None):
    if source is None:
        raise ValueError("no local market archive")
    root, db = source
    condition, token = op["condition_id"], op["token_id"]
    decision, kickoff = rules.instant(op["decision_at"]), rules.instant(op["kickoff"])
    market = archive_proof.original_market(db, root, condition, decision)
    _gamma_identity(source, market, op["fixture_id"], kickoff)
    terms = _terms(root, market, token, decision, scheduled=scheduled)
    canonical = _intent_terms(terms)
    if rules.digest({k: op.get(k) for k in canonical}) != rules.digest(canonical):
        raise ValueError(
            "entry outcome, direction, rules or fee differ from the verified contract"
        )
    target = decision + timedelta(seconds=max(5, terms["seconds_delay"]))
    snapshots = []
    for stage, at, submitted in zip(
        ("first", "second"), (decision, target), candidates
    ):
        if scheduled:
            seen_by = at + timedelta(seconds=5) if stage == "second" else at
            verified = shadow_market.book(
                db, root, terms, at, kickoff, stage,
                seen_by=seen_by, fee_check=_scheduled_book_fee,
            )
        else:
            verified = archive_proof.eligible_book(db, root, terms, at, kickoff, stage)
        if not verified:
            raise ValueError(
                f"missing freshly captured verified {stage} book or fee authority"
            )
        stage_market = archive_proof.original_market(
            db, root, condition, verified["received_at"]
        )
        _gamma_identity(source, stage_market, op["fixture_id"], kickoff)
        if any(
            stage_market[k] != market[k]
            for k in (
                "fixture",
                "event_id",
                "event_slug",
                "slug",
                "tokens",
                "family",
                "side",
                "line",
                "rule_sha256",
            )
        ):
            raise ValueError(
                "Gamma outcome, event identity or rule changed between entry receipts"
            )
        if (
            submitted.get("source_sha256") != verified["source_sha256"]
            or rules.instant(submitted["received_at"])
            != rules.instant(verified["received_at"])
            or rules.series(submitted, "asks") != rules.series(verified, "asks")
        ):
            raise ValueError(
                "submitted prices, depth, receipt or original SHA differs from eligible local book"
            )
        if rules.number(op["shares"]) < rules.number(verified["min_order_size"]):
            raise ValueError("order is below the verified venue minimum")
        exchange_at = rules.instant(verified["exchange_at"])
        lower = decision - timedelta(seconds=30) if stage == "first" else target
        if not lower <= exchange_at <= rules.instant(verified["received_at"]):
            raise ValueError(
                "native exchange snapshot is stale at the required buying window"
            )
        snapshots.append(verified)
    if snapshots[0]["source_sha256"] == snapshots[1]["source_sha256"] or rules.instant(
        snapshots[1]["observed_at"]
    ) <= rules.instant(snapshots[0]["observed_at"]):
        raise ValueError(
            "execution requires an independently observed fresh venue book"
        )
    result = {
        **market,
        "entry_terms": terms,
        "decision_at": rules.iso(decision),
        "budget_fixture_id": op["fixture_id"],
        "fee_evidence": {
            "basis": _fee_basis(terms["fee_schedule"]),
            "schedule": terms["fee_schedule"],
            "decision_gamma_sha256": market["source_sha256"],
            "book_gamma": [{"source_sha256": b["contract_source_sha256"],
                            "received_at": b["contract_recorded_at"]} for b in snapshots],
        },
    }
    timeline, native_fees = budget_schedule.entry_parents(source, market, snapshots, as_of=as_of)
    result["fee_evidence"]["native_gamma_fields"] = native_fees
    if scheduled:
        result["entry_evidence"] = budget_schedule.attest(
            source, op["fixture_id"], market, kickoff, snapshots, timeline, as_of, football
        )
    return result


def _terminal(source, market, token, kickoff, at):
    root, db = source
    history = settlement.resolution_history(db, root, token, as_of=at, market=market)
    terminal = None
    for record in history:
        if record["status"] != "resolved":
            continue
        settlement._verify_token(market, record, token)
        value = rules.unit(record["payout"])
        if rules.instant(record["resolved_at"]) < kickoff or value not in {
            rules.ZERO,
            rules.ONE,
            rules.D("0.5"),
        }:
            raise ValueError(
                "unsupported payout or terminal original predates verified fixture"
            )
        if terminal is not None and terminal[0] != value:
            raise ValueError("conflicting terminal payouts")
        if terminal is None:
            terminal = (value, set())
        terminal[1].add(record["source_sha256"])
    return terminal


def terminal_pair(source, market, event, kickoff):
    """Attest only the venue's payout vector. This does not authorize any credit."""
    if source is None or market is None:
        return None, "missing_verified_entry"
    token, at = event["token_id"], rules.instant(event["at"])
    ids = [str(t["token_id"]) for t in market["tokens"]]
    if len(ids) != 2 or len(set(ids)) != 2 or token not in ids:
        return None, "unverified_terminal_pair"
    held = _terminal(source, market, token, kickoff, at)
    other = _terminal(source, market, next(t for t in ids if t != token), kickoff, at)
    if held is None or other is None:
        return None, "missing_terminal_pair"
    if event.get("original_sha256") not in held[1] or held[0] + other[0] != rules.ONE:
        return None, "unverified_terminal_pair"
    return held[0], None


def redemption(source, market, event, kickoff):
    if source is None or not market or "entry_terms" not in market:
        return None, "missing_verified_entry"
    amount, gap = terminal_pair(source, market, event, kickoff)
    if gap:
        return None, gap
    root, db = source
    frozen = market["entry_terms"]
    decision = market["decision_at"]
    original = archive_proof.original_market(
        db,
        root,
        event["condition_id"],
        decision,
        receipt_at=frozen["rule_received_at"],
        sha=frozen["rule_source_sha256"],
    )
    if event["fixture_id"] != market["budget_fixture_id"]:
        raise ValueError("redemption fixture differs from sealed entry")
    _gamma_identity(source, original, event["fixture_id"], kickoff)
    terms = _terms(root, original, event["token_id"], decision,
                   scheduled=frozen["fee_schedule"].get("research_assumption") is True)
    if terms != frozen or original["rule_sha256"] != market["rule_sha256"]:
        raise ValueError("redemption rules or terms no longer match the frozen outcome")
    if amount == rules.D("0.5"):
        settlement.require_reviewed_equal_split(terms)
    return amount, None


def audit_redemption(source, market, event, kickoff, cutoff, expected):
    """Even an omitted follow-up event cannot hide a later archived conflict."""
    if cutoff is None:
        _, db = source
        tokens = [item["token_id"] for item in market["tokens"]]
        latest = db.execute(
            "SELECT max(received_at) FROM ma_requests WHERE operation='resolution' AND status='ok' AND selector IN (?,?)",
            tokens,
        ).fetchone()[0]
        cutoff = rules.instant(latest)
    actual, gap = redemption(source, market, dict(event, at=rules.iso(cutoff)), kickoff)
    if gap or actual != expected:
        raise ValueError(
            "disputed terminal pair for previously credited paper position"
        )
