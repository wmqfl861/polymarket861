"""Read-only attestation of books and redemptions for the five-league capital replay."""

from contextlib import closing, contextmanager
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from . import (
    archive_proof,
    big5_market,
    dbscope,
    league_registry,
    ledger_rules as rules,
    ledger_terms,
    settlement,
)


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
    with closing(
        sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        with dbscope.bind(archive, db):
            yield (archive, db)


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


def entry(source, op, candidates):
    if source is None:
        raise ValueError("no local market archive")
    root, db = source
    condition, token = op["condition_id"], op["token_id"]
    decision, kickoff = rules.instant(op["decision_at"]), rules.instant(op["kickoff"])
    market = archive_proof.original_market(db, root, condition, decision)
    _gamma_identity(source, market, op["fixture_id"], kickoff)
    terms = ledger_terms.from_original(root, market, token, decision)
    selection = {
        k: terms[k]
        for k in ("family", "selection", "team", "line", "side")
        if k in terms
    }
    if (
        op.get("selection") != selection
        or op.get("rule_sha256") != terms["rule_sha256"]
        or op.get("fee") != terms["fee_schedule"]
    ):
        raise ValueError(
            "entry outcome, direction, rules or fee differ from the verified contract"
        )
    target = decision + timedelta(seconds=max(5, terms["seconds_delay"]))
    snapshots = []
    for stage, at, submitted in zip(
        ("first", "second"), (decision, target), candidates
    ):
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
    return market


def _terminal(source, market, token, kickoff, at):
    root, db = source
    rows = db.execute(
        "SELECT sha,received_at FROM ma_requests WHERE operation='resolution' AND selector=? AND status='ok' "
        "AND received_at<=? ORDER BY received_at,sha",
        (token, rules.iso(at)),
    )
    terminal = None
    for record in rows:
        reply, _, selector, _ = archive_proof.original_receipt(
            db, root, record["sha"], record["received_at"], {"resolution"}
        )
        if selector != token:
            raise ValueError("resolution capture requested a different token")
        payload = json.loads(reply.body)
        native = [r for r in payload["data"] if str(r.get("token_id")) == token]
        if len(native) != 1:
            raise ValueError("ambiguous resolution response")
        row = native[0]
        if row.get("resolved") is False and row.get("final_price") is None:
            if terminal:
                raise ValueError("venue withdrew an observed terminal state")
            continue
        value = rules.unit(row.get("final_price"))
        official = {"status": "resolved", "row": row}
        settlement._verify_token(market, official, token)
        if rules.instant(record["received_at"]) < kickoff or value not in {
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
        terminal[1].add(record["sha"])
    return terminal


def redemption(source, market, event, kickoff):
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
