"""Rebuild eligible exchange observations from native Gamma and CLOB responses."""

import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from . import market_provider as provider
from . import dbscope, store
from .ledger_rules import check_book, instant, iso


def _body(root, sha):
    return dbscope.verified_bytes(root, store.blob_path(root, sha), sha, "exchange response hash mismatch")


def original_receipt(db, root, sha, received_at, operations, *, expected_selector=None):
    """A user-uploaded file or free-form source label is not a collector receipt."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='ma_requests'"
    ).fetchone():
        raise ValueError("missing exchange collector request log")
    hits = db.execute(
        "SELECT o.metadata,r.operation,r.selector,r.requested_at FROM observations o "
        "JOIN ma_requests r ON r.sha=o.sha256 AND r.received_at=o.observed_at "
        "WHERE o.source='polymarket.com' AND o.sha256=? AND o.observed_at=? AND r.status='ok'",
        (sha, received_at),
    )
    for row in hits:
        # Two token queries may return identical bytes at the same receive instant.
        if expected_selector is not None and row["selector"] != expected_selector:
            continue
        meta = json.loads(row["metadata"])
        operation = row["operation"]
        if (
            operation not in operations
            or meta.get("operation") != operation
            or meta.get("requested_at") != row["requested_at"]
        ):
            continue
        uri = urlsplit(meta.get("url", ""))
        allowed = {
            "event": ("gamma-api.polymarket.com", "/events/slug/" + row["selector"]),
            "catalog": ("gamma-api.polymarket.com", "/events"),
            "league_event": ("gamma-api.polymarket.com", "/events/slug/" + row["selector"]),
            "league_catalog": ("gamma-api.polymarket.com", "/events"),
            "book": ("clob.polymarket.com", "/book"),
            "books": ("clob.polymarket.com", "/books"),
            "trades": ("data-api.polymarket.com", "/v2/trades"),
            "prices": ("data-api.polymarket.com", "/v2/prices-history"),
            "resolution": ("data-api.polymarket.com", "/v2/tokens"),
        }
        if uri.scheme != "https" or (uri.netloc, uri.path) != allowed[operation]:
            continue
        if operation == "league_catalog":
            entry = provider.SPORT_MARKETS.get(row["selector"])
            if not entry or parse_qs(uri.query).get("tag_id") != [entry[1]]:
                continue
        if operation in ("book","resolution") and parse_qs(uri.query).get("token_id") != [
            row["selector"]
        ]:
            continue
        if operation in {"prices", "trades"} and parse_qs(uri.query).get(
            "token_id" if operation == "prices" else "event_id"
        ) != [row["selector"]]:
            continue
        headers = {
            k: str(v)
            for k, v in {"date": meta.get("http_date"), "age": meta.get("age")}.items()
            if v is not None
        }
        reply = provider.Reply(
            _body(root, sha), row["requested_at"], received_at, headers, meta["url"]
        )
        if instant(reply.requested_at) <= instant(received_at):
            return reply, operation, row["selector"], meta
    raise ValueError(
        "missing authenticated-origin exchange capture at its claimed receipt time"
    )


def _event_capture(db, root, sha, received_at, event_slug):
    reply, operation, selector, _ = original_receipt(
        db, root, sha, received_at, {"event", "catalog", "league_event", "league_catalog"}
    )
    body = json.loads(reply.body)
    if operation in {"event", "league_event"} and isinstance(body, dict) and selector == event_slug:
        events = [body]
    elif operation in {"catalog", "league_catalog"} and isinstance(body, list):
        events = body
    else:
        events = []
    native = [
        event
        for event in events
        if isinstance(event, dict) and event.get("slug") == event_slug
    ]
    if len(native) != 1:
        raise ValueError("Gamma capture does not identify a unique matching domestic event")
    if operation.startswith("league_"):
        from .big5_market import _identity

        sport = event_slug.split("-", 1)[0]
        if sport not in provider.SPORT_FIXTURES:
            raise ValueError("unknown domestic event sport")
        if operation == "league_catalog" and selector != sport:
            raise ValueError("domestic catalogue selector differs from event sport")
        _identity(native[0], sport)
    return native[0]


def original_market(db, root, condition, as_of, *, receipt_at=None, sha=None):
    """Recover one independently keyed original Gamma market at the specified time."""
    key = ("market", condition, store.iso(as_of), receipt_at, sha)
    return dbscope.checked(root, key, lambda: _verified_market(db, root, condition, as_of, receipt_at, sha))


def _gamma_markets(db, root, sha, received_at, slug):
    """Parse a verified Gamma response once for all tokens in this transaction."""
    active = dbscope.current(root)
    key = ("gamma_conditions", sha, received_at, slug)
    if active is not None and key in active.checked:
        return active.checked[key]
    event = _event_capture(db, root, sha, received_at, slug)
    by_condition = {}
    for market in provider.normalized_markets(event, sport=slug.split("-", 1)[0]):
        by_condition.setdefault(market["condition_id"], []).append(market)
    result = (by_condition, event.get("teams") or [], event.get("title"))
    if active is not None:
        active.checked[key] = result
    return result


def _verified_market(db, root, condition, as_of, receipt_at, sha):
    schema = db.execute("SELECT type FROM sqlite_master WHERE name='ma_contracts'").fetchone()
    if not schema:
        raise ValueError("missing captured Gamma contract")
    at = store.iso(as_of)
    condition_field = "condition_id" if schema[0] == "view" else "json_extract(payload,'$.condition_id')"
    rows = db.execute(
        "SELECT event_slug,market_slug,received_at,sha,payload FROM ma_contracts "
        f"WHERE json_valid(payload) AND received_at<=? AND {condition_field}=? ORDER BY received_at DESC",
        (at, condition),
    )
    for row in rows:
        if receipt_at is not None and row["received_at"] != receipt_at:
            continue
        if sha is not None and row["sha"] != sha:
            continue
        slug = row["event_slug"]
        rows_by_condition, teams, title = _gamma_markets(db, root, row["sha"], row["received_at"], slug)
        candidates = rows_by_condition.get(condition, [])
        if (
            len(candidates) != 1
            or candidates[0]["mapping_error"]
            or candidates[0]["slug"] != row["market_slug"]
        ):
            raise ValueError(
                "Gamma capture does not identify a unique matching outcome contract"
            )
        market = dict(
            candidates[0], recorded_at=row["received_at"], source_sha256=row["sha"]
        )
        if market != json.loads(row["payload"]):
            raise ValueError(
                "archived normalized contract diverges from the original Gamma capture"
            )
        # Removing a market creates a fresh event row but no corresponding ma_contracts row.
        latest = db.execute(
            "SELECT received_at,sha FROM ma_events WHERE slug=? AND received_at="
            "(SELECT MAX(received_at) FROM ma_events WHERE slug=? AND received_at<=?)",
            (slug, slug, at),
        ).fetchall()
        if not latest:
            raise ValueError("missing Gamma event receipt for market")
        for head in latest:
            if (head["received_at"], head["sha"]) == (row["received_at"], row["sha"]):
                continue
            latest_markets, latest_teams, _ = _gamma_markets(db, root, head["sha"], head["received_at"], slug)
            if latest_markets.get(condition, []) != candidates or latest_teams != teams:
                raise ValueError(
                    "latest captured Gamma event removed or changed this market"
                )
        return dict(
            market,
            event_teams=teams,
            event_title=title,
        )
    raise ValueError("no original Gamma contract at the requested time")


def _original_book(db, root, contract, row, *, fee_check=None):
    from .ledger_terms import verify_book_terms

    book = json.loads(row["payload"])
    token, condition = str(contract["token_id"]), contract["condition_id"]
    reply, operation, selector, meta = original_receipt(
        db, root, row["sha"], row["received_at"], {"book", "books"}
    )
    payload = json.loads(reply.body)
    if operation == "book":
        native = [payload] if isinstance(payload, dict) and selector == token else []
    else:
        requested = meta.get("request_token_ids")
        if not isinstance(requested, list) or token not in requested:
            raise ValueError("token not included in the native CLOB batch request")
        native = payload if isinstance(payload, list) else []
    exact = [
        v
        for v in native
        if isinstance(v, dict)
        and str(v.get("asset_id")) == token
        and v.get("market") == condition
    ]
    if len(exact) != 1 or exact[0].get("timestamp") is None:
        raise ValueError(
            "native CLOB response needs a unique token, condition and exchange timestamp"
        )
    exchange_at = provider.moment(exact[0]["timestamp"])
    if exchange_at > instant(reply.received_at) or not provider.fresh_observation(
        reply
    ):
        raise ValueError("CLOB exchange timestamp or HTTP observation is inconsistent")
    market = original_market(
        db,
        root,
        condition,
        reply.received_at,
        receipt_at=book.get("contract_recorded_at"),
        sha=book.get("contract_source_sha256"),
    )
    latest = original_market(db, root, condition, reply.received_at)
    market_state = (
        "tokens",
        "game_times",
        "market_open",
        "live",
        "ended",
        "seconds_delay",
        "rule_sha256",
        "fees_enabled",
        "fee_schedule",
    )
    if any(latest[k] != market[k] for k in market_state):
        raise ValueError(
            "a more recent Gamma capture changes the bound rules or market state"
        )
    if market["rule_sha256"] != contract["rule_sha256"] or token not in {
        t["token_id"] for t in market["tokens"]
    }:
        raise ValueError(
            "native contract rule or outcome token disagrees with held contract"
        )
    check_fee = fee_check or verify_book_terms
    check_fee(contract, market, data_dir=root, at=reply.received_at)
    rebuilt = provider.normalize_book(exact[0], reply, market, token, row["sha"])
    if rebuilt != book:
        raise ValueError(
            "archived book diverges from the original CLOB response or Gamma rules"
        )
    return rebuilt, market


def eligible_book(
    db,
    data_dir,
    contract,
    at,
    kickoff,
    stage,
    *,
    observed_by=None,
    verified_actual=None,
):
    """Choose a fresh, independently reconstructed book within the stage's fixed observation window."""
    if stage not in {"first", "second", "close"}:
        raise ValueError("unknown book stage")
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ma_books'").fetchone():
        return None
    at, kickoff = store.instant(at), store.instant(kickoff)
    lower = at if stage == "second" else at - timedelta(seconds=30)
    upper = at + timedelta(seconds=5) if stage == "second" else at
    if observed_by is not None:
        upper = min(upper, store.instant(observed_by))
    delayed = None
    if stage == "close" and verified_actual is not None:
        from . import kickoff_proof

        planned = store.instant(verified_actual["scheduled_kickoff"])
        if (
            contract.get("match_id") == verified_actual.get("fixture_id")
            and planned < kickoff
            and store.instant(verified_actual["actual_kickoff"]) == at == kickoff
        ):
            checked = kickoff_proof.resolve(
                data_dir,
                contract["match_id"],
                planned,
                planned - timedelta(hours=1),
                db=db,
            )
            if (
                checked.get("proof_id") == verified_actual.get("proof_id")
                and store.instant(checked["actual_kickoff"]) == kickoff
            ):
                delayed = checked
    order = "ASC" if stage == "second" else "DESC"
    rows = db.execute(
        "SELECT received_at,sha,payload FROM ma_books WHERE token=? AND received_at>=? "
        f"AND received_at<=? ORDER BY received_at {order},sha {order}",
        (str(contract["token_id"]), iso(lower), iso(upper)),
    )
    for row in rows:
        try:
            book, market = _original_book(db, data_dir, contract, row)
            if (
                delayed
                and not book["pregame"]
                and market["market_open"] is True
                and market["live"] is False
                and market["ended"] is False
                and market["game_times"] == [store.iso(delayed["scheduled_kickoff"])]
                and store.instant(delayed["scheduled_kickoff"])
                <= instant(book["received_at"])
                < kickoff
            ):
                book = dict(
                    book,
                    pregame=True,
                    pregame_basis="verified_actual_kickoff",
                    kickoff_proof_id=delayed["proof_id"],
                )
            check_book(book, contract, at, kickoff, stage)
        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            AttributeError,
            OverflowError,
        ):
            continue
        return book
    return None


def _research_market(db, root, op, condition, token):
    received = op.get("market_recorded_at")
    sha = op.get("market_source_sha256")
    if (received is None) != (sha is None):
        raise ValueError("incomplete original Gamma archive reference")
    now = store.now_utc()
    if received is not None and store.instant(received) > store.instant(now):
        raise ValueError("future Gamma archive receipt")
    at = store.iso(received) if received is not None else now
    market = original_market(
        db, root, condition, at,
        receipt_at=store.iso(received) if received is not None else None,
        sha=sha,
    )
    contract = op.get("intent", {}).get("contract", {})
    fixture = op.get("match_id") or contract.get("match_id")
    slug = op.get("market_fixture") or fixture
    outcomes = [r["outcome"] for r in market["tokens"] if r["token_id"] == token]
    if (
        slug != market["fixture"]
        or len(outcomes) != 1
        or len(market["game_times"]) != 1
        or (op.get("family") is not None and op["family"] != market["family"])
        or (op.get("market_slug") is not None and op["market_slug"] != market["slug"])
        or (op.get("outcome") is not None and op["outcome"] != outcomes[0])
        or (contract and (
            contract.get("match_id") != fixture or contract.get("condition_id") != condition
            or str(contract.get("token_id")) != token
        ))
    ):
        raise ValueError("research row does not match the original event, market and token")
    return market, fixture, slug


def research_tier(db, root, op):
    """Trade/price evidence retains its research grade only with its genuine upstream observation."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ma_history'").fetchone():
        return "missing"
    contract = op.get("intent", {}).get("contract", {})
    token = str(op.get("token_id") or contract.get("token_id") or "")
    condition = op.get("condition_id") or contract.get("condition_id")
    cutoff = (
        op.get("research_cutoff")
        or op.get("actual_kickoff")
        or op.get("intent", {}).get("kickoff")
    )
    if not token or not condition or not cutoff or op.get("schedule_conflict"):
        return "missing"
    try:
        market, fixture, slug = _research_market(db, root, op, condition, token)
        cutoff = min(store.iso(cutoff), market["game_times"][0])
    except (ValueError, TypeError, KeyError, OSError, AttributeError, OverflowError):
        return "missing"
    for kind, tier in (("trades", "B"), ("prices", "C")):
        hints = op.get(kind, op.get("price_history") if kind == "prices" else [])
        if not isinstance(hints, list):
            continue
        for hint in hints:
            if not isinstance(hint, dict) or str(hint.get("token_id")) != token:
                continue
            try:
                stamp = store.iso(hint["timestamp"])
                if stamp >= store.iso(cutoff):
                    continue
                found = db.execute(
                    "SELECT r.received_at FROM ma_history h JOIN ma_requests r ON "
                    "r.sha=h.sha AND r.operation=h.kind AND r.status='ok' WHERE h.kind=? AND h.token=? "
                    "AND h.timestamp=? AND h.sha=? ORDER BY r.received_at",
                    (kind, token, stamp, hint.get("source_sha256")),
                ).fetchone()
                if not found:
                    continue
                if store.instant(found[0]) > store.instant(store.now_utc()):
                    continue
                if fixture != slug:
                    from .opportunity import _check_child, _locate

                    # Research can use a later archive. Bind the football identity when both
                    # original exchange records existed, without moving the pregame event cutoff.
                    binding_at = max(store.instant(found[0]), store.instant(market["recorded_at"]))
                    confirmed = _locate(root, slug, None, binding_at)
                    if confirmed["fixture_id"] != fixture or store.iso(confirmed["kickoff"]) != market["game_times"][0]:
                        continue
                    _check_child(root, confirmed, market, binding_at)
                reply, _, selector, _ = original_receipt(
                    db, root, hint["source_sha256"], found[0], {kind}
                )
                for item in json.loads(reply.body)["data"]:
                    if (
                        store.iso(provider.moment(item["timestamp"])) != stamp
                        or not 0 < provider.number(item["price"]) < 1
                    ):
                        continue
                    if kind == "trades" and (
                        str(item.get("token_id")) != token
                        or item.get("condition_id") != condition
                        or provider.number(item["size"]) <= 0
                    ):
                        continue
                    if kind == "prices" and (
                        selector != token or int(item["resolution_seconds"]) <= 0
                    ):
                        continue
                    if all(
                        k in item
                        and provider.number(item[k]) == provider.number(hint[k])
                        for k in (
                            ("price", "size")
                            if kind == "trades"
                            else ("price", "resolution_seconds")
                        )
                    ):
                        return tier
            except (
                ValueError,
                TypeError,
                KeyError,
                OSError,
                AttributeError,
                OverflowError,
            ):
                continue
    return "missing"
