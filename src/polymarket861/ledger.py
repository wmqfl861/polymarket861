"""Paper-only order intents, execution, exchange settlement and chronological replay."""

import hashlib
import json
from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import timedelta
from decimal import ROUND_DOWN
from pathlib import Path

from . import dbscope, kickoff_proof, store
from .archive_proof import eligible_book, research_tier
from .ledger_rules import (
    MAX_GAP,
    MAX_MATCH,
    MAX_ORDER,
    ONE,
    UNIT,
    ZERO,
    D,
    check_book,
    check_contract,
    digest,
    fair_values,
    fit,
    forecast_fair,
    fmt,
    instant,
    iso,
    number,
    packed,
    pair_depth,
    scenario_payout,
    unit,
)

PROTOCOL = store.PROTOCOL
HISTORICAL_VERIFIER = "native-exchange-formal-seal-v3"
SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_accounts(
 account TEXT PRIMARY KEY, mode TEXT NOT NULL, initial TEXT NOT NULL, cash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_account_bindings(
 account TEXT PRIMARY KEY REFERENCES paper_accounts, role TEXT NOT NULL,
 candidate_key TEXT, candidate_version TEXT, registration_digest TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS paper_candidate_account ON paper_account_bindings(candidate_key)
 WHERE role='candidate';
CREATE TABLE IF NOT EXISTS paper_intents(
 account TEXT NOT NULL REFERENCES paper_accounts, id TEXT NOT NULL, fixture TEXT NOT NULL,
 condition_id TEXT NOT NULL, token TEXT NOT NULL, cutoff TEXT NOT NULL, priority INTEGER NOT NULL,
 status TEXT NOT NULL, fingerprint TEXT NOT NULL, original TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(account,id), UNIQUE(account,fixture,condition_id,token,cutoff));
CREATE TABLE IF NOT EXISTS paper_replay_members(
 account TEXT NOT NULL REFERENCES paper_accounts, opportunity_digest TEXT NOT NULL,
 PRIMARY KEY(account,opportunity_digest));
CREATE TABLE IF NOT EXISTS paper_replay_verifiers(
 account TEXT PRIMARY KEY REFERENCES paper_accounts, verifier TEXT NOT NULL, fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_replay_witnesses(
 account TEXT NOT NULL, intent_id TEXT NOT NULL, opportunity_digest TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(account,intent_id), FOREIGN KEY(account,opportunity_digest)
 REFERENCES paper_replay_members(account,opportunity_digest));
CREATE INDEX IF NOT EXISTS paper_intents_pending ON paper_intents(account,status,cutoff,priority,fixture,condition_id,token,id);
CREATE TABLE IF NOT EXISTS paper_depth(
 account TEXT NOT NULL, condition_id TEXT NOT NULL, token TEXT NOT NULL, snapshot TEXT NOT NULL,
 price TEXT NOT NULL, consumed TEXT NOT NULL, PRIMARY KEY(account,condition_id,token,snapshot,price));
CREATE TABLE IF NOT EXISTS paper_resolutions(
 account TEXT NOT NULL, condition_id TEXT NOT NULL, token TEXT NOT NULL, event_id TEXT NOT NULL,
 terminal INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account,condition_id,token,event_id));
CREATE UNIQUE INDEX IF NOT EXISTS paper_terminal ON paper_resolutions(account,condition_id,token) WHERE terminal=1;
"""


class DecisionWindowClosed(ValueError):
    """No new paper intent in this transaction was committed before T−60."""


@contextmanager
def connection(root, write=False):
    active = dbscope.current(root)
    if active is not None:
        db = active.db
        if not write:
            yield db
            return
        pending, deadline = active.dirty.copy(), active.deadline
        recovery = active.recovered_until.copy()
        db.execute("SAVEPOINT ledger_action")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK TO ledger_action")
            active.dirty, active.deadline, active.recovered_until = pending, deadline, recovery
            raise
        finally:
            db.execute("RELEASE ledger_action")
        return
    with closing(store.connect(root)) as db:
        dbscope.prepare(db, "paper-ledger", SCHEMA)
        with db:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db


@contextmanager
def fixture_batch(data_dir):
    """One local writer and commit for a fixture tick. External captures precede this scope."""
    if dbscope.current(data_dir) is not None:
        with connection(data_dir, write=True) as db:
            yield db
        return
    from . import jobs_research, market_archive

    with closing(store.connect(data_dir)) as db:
        for name, schema in (
            ("paper-ledger", SCHEMA),
            ("market-archive", market_archive._SCHEMA),
            ("research", jobs_research._SCHEMA),
        ):
            dbscope.prepare(db, name, schema)
        with db:
            db.execute("BEGIN IMMEDIATE")
            with dbscope.bind(data_dir, db) as batch:
                yield db
                for account in sorted(batch.dirty):
                    _publish_compact(db, account)
                dbscope.recheck_originals(batch)
                if batch.deadline is not None and instant(store.now_utc()) > batch.deadline:
                    raise DecisionWindowClosed("fixture batch missed the decision deadline; no new intents committed")


def account_row(db, account):
    row = db.execute(
        "SELECT * FROM paper_accounts WHERE account=?", (account,)
    ).fetchone()
    if row is None:
        raise ValueError("unknown paper account")
    return row


def _binding(db, account):
    row = db.execute(
        "SELECT * FROM paper_account_bindings WHERE account=?", (account,)
    ).fetchone()
    if row:
        return {
            k: row[k]
            for k in (
                "role",
                "candidate_key",
                "candidate_version",
                "registration_digest",
            )
        }
    # Pre-binding accounts retain their cash and all booked events. Formal accounts may
    # continue solely under the incumbent policy; a candidate needs its own new account.
    mode = account_row(db, account)["mode"]
    return {
        "role": "main" if mode == "formal" else "research",
        "candidate_key": None,
        "candidate_version": None,
        "registration_digest": None,
    }


def _registered_candidate(data_dir, account, key, version=None):
    from . import jobs

    if not isinstance(key, str) or not key.strip():
        raise ValueError(
            "a candidate paper account requires its preregistered candidate key"
        )
    c = jobs.get_candidate(data_dir, key)
    if (
        c["kind"] != "model"
        or c["scope"] != "epl"
        or c["manifest"].get("paper_account", c["version"]) != account
        or (version is not None and c["version"] != version)
    ):
        raise ValueError(
            "candidate registration does not bind this EPL account and model version"
        )
    return c


def open_account(
    data_dir,
    account,
    mode="shadow",
    initial_cash="10000",
    *,
    role=None,
    candidate_key=None,
    version=None,
):
    initial = number(initial_cash)
    if (
        not isinstance(account, str)
        or not account.strip()
        or mode not in ("formal", "shadow", "historical", "demo")
    ):
        raise ValueError("invalid paper account or mode")
    if initial <= 0 or initial % UNIT:
        raise ValueError("initial cash must be positive at accounting precision")
    if mode == "formal":
        store.require_formal(data_dir)
    if role not in (None, "main", "candidate", "research"):
        raise ValueError("invalid paper account role")
    default_role = "main" if mode == "formal" else "research"
    chosen = role or ("candidate" if candidate_key else default_role)
    if (mode == "formal" and chosen == "research") or (
        mode != "formal" and chosen != "research"
    ):
        raise ValueError(
            "main and candidate bindings apply only to formal paper accounts"
        )
    if chosen != "candidate" and (candidate_key is not None or version is not None):
        raise ValueError("a model may only be pinned to a registered candidate account")
    candidate = (
        _registered_candidate(data_dir, account, candidate_key, version)
        if chosen == "candidate"
        else None
    )
    desired = {
        "role": chosen,
        "candidate_key": candidate_key,
        "candidate_version": candidate["version"] if candidate else None,
        "registration_digest": candidate["digest"] if candidate else None,
    }
    with connection(data_dir, True) as db:
        existing = db.execute(
            "SELECT 1 FROM paper_accounts WHERE account=?", (account,)
        ).fetchone()
        if existing:
            row = account_row(db, account)
            current = _binding(db, account)
            explicit = (
                role is not None or version is not None or candidate_key is not None
            )
            if (
                row["mode"] != mode
                or number(row["initial"]) != initial
                or (explicit and desired != current)
            ):
                raise ValueError(
                    "existing account has different immutable mode, funds or role/version binding"
                )
            desired = current
        else:
            db.execute(
                "INSERT INTO paper_accounts VALUES (?,?,?,?)",
                (account, mode, fmt(initial), fmt(initial)),
            )
            row = account_row(db, account)
        db.execute(
            "INSERT OR IGNORE INTO paper_account_bindings VALUES (?,?,?,?,?)",
            (
                account,
                desired["role"],
                desired["candidate_key"],
                desired["candidate_version"],
                desired["registration_digest"],
            ),
        )
        if _binding(db, account) != desired:
            raise ValueError("candidate already bound to another account")
        return dict(row) | desired


def _formal_authority(data_dir, account, intent, seal, cutoff):
    from . import jobs

    with connection(data_dir) as db:
        binding = _binding(db, account)
    role = binding["role"]
    version = seal.get("model_version")
    if version != intent.get("model_version"):
        raise ValueError("submitted model differs from sealed model")
    for declared in (
        intent.get("purpose"),
        intent.get("account_role"),
        seal.get("purpose"),
        seal.get("forecast_purpose"),
    ):
        if declared is None:
            continue
        expected = (
            {"main", "champion", "incumbent", "production"}
            if role == "main"
            else {"candidate", "candidate_shadow", "shadow"}
        )
        if declared not in expected:
            raise ValueError(
                "forecast or intent purpose is incompatible with this account"
            )
    if any(
        intent[k] != PROTOCOL
        for k in ("entry_policy", "strategy_version", "strategy_id", "strategy")
        if k in intent
    ):
        raise ValueError("unregistered entry strategy for the formal account")
    if any(
        x is not None and x != binding["candidate_key"]
        for x in (intent.get("candidate_key"), seal.get("candidate_key"))
    ):
        raise ValueError("candidate key does not match account role")
    if role == "main":
        incumbent = jobs.model_for_decision(
            data_dir,
            seal.get("fixture_id"),
            seal.get("kickoff"),
            "T-60m",
            ("formal",),
            iso(cutoff),
        )
        if version != incumbent:
            raise ValueError(
                "only the active EPL model at the decision cutoff can enter the main account"
            )
    elif role == "candidate":
        c = _registered_candidate(data_dir, account, binding["candidate_key"], version)
        if (
            c["digest"] != binding["registration_digest"]
            or c["version"] != binding["candidate_version"]
        ):
            raise ValueError("immutable candidate registration/version mismatch")
        if instant(c["registered_at"]) > min(
            cutoff, instant(intent.get("prediction_at"))
        ):
            raise ValueError("candidate registration postdates its forecast")
        fixture = store.fixture_at(data_dir, intent["match_id"], at=iso(cutoff))
        season = fixture["season"].replace("/", "-")
        start, end = season.split("-", 1)
        if (
            f"{start}-{end[-2:]}" != c["season"]
            or not c["first"] <= fixture["round_id"] < c["first"] + c["span"]
        ):
            raise ValueError("fixture outside the preregistered candidate window")
        expected_recipe = c["manifest"].get("recipe_digest")
        if not expected_recipe or seal.get("recipe_digest") != expected_recipe:
            raise ValueError(
                "forecast recipe differs from preregistered candidate recipe"
            )
    else:
        raise ValueError("formal account has no authorized entry purpose")
    return binding


def _load(db, account, intent_id):
    row = db.execute(
        "SELECT * FROM paper_intents WHERE account=? AND id=?", (account, intent_id)
    ).fetchone()
    if not row:
        raise ValueError("unknown frozen intent")
    return row, json.loads(row["original"]), json.loads(row["result"])


def _put(db, account, obj):
    db.execute(
        "UPDATE paper_intents SET status=?,result=? WHERE account=? AND id=?",
        (obj["status"], packed(obj), account, obj["intent_id"]),
    )


def _replay_version(db, account):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='paper_replays'"
    ).fetchone():
        return None
    row = db.execute(
        "SELECT v.verifier FROM paper_replay_verifiers v JOIN paper_replays r "
        "ON r.account=v.account AND r.fingerprint=v.fingerprint WHERE v.account=?",
        (account,),
    ).fetchone()
    return row[0] if row else None


def _require_current_replay(db, account):
    if _replay_version(db, account) != HISTORICAL_VERIFIER:
        raise ValueError(
            "historical account is read-only; rerun market-replay on a new account with a registered replay manifest"
        )


def _check_history_action(
    root,
    account,
    *,
    intent_id=None,
    token=None,
    settlement=None,
    execution=False,
    book=None,
):
    """Re-admit the exact stored manifest opportunity before any historical account movement."""
    with connection(root) as db:
        if account_row(db, account)["mode"] != "historical":
            return
        _require_current_replay(db, account)
        query = (
            "SELECT i.id,i.original,i.fingerprint,i.result,w.opportunity_digest,w.payload,m.opportunity_digest AS member "
            "FROM paper_intents i LEFT JOIN paper_replay_witnesses w ON w.account=i.account AND w.intent_id=i.id "
            "LEFT JOIN paper_replay_members m ON m.account=i.account AND m.opportunity_digest=w.opportunity_digest "
            "WHERE i.account=?"
        )
        if intent_id is not None:
            rows = db.execute(query + " AND i.id=?", (account, intent_id)).fetchall()
        else:
            rows = db.execute(
                query + " AND i.condition_id=? AND i.token=?", (account, *token)
            ).fetchall()
        if not rows:
            raise ValueError("historical action has no bound replay order")
        for row in rows:
            if not row["member"]:
                raise ValueError(
                    "legacy historical order is read-only; no immutable manifest witness"
                )
            op = json.loads(row["payload"])
            original, decision = json.loads(row["original"]), json.loads(row["result"])
            if (
                digest(op) != row["opportunity_digest"]
                or digest(original) != row["fingerprint"]
                or decision.get("decision_sha256") != row["fingerprint"]
                or op["intent"]["intent_id"] != row["id"]
                or digest({"intent": op["intent"], "book": op["first_book"]})
                != row["fingerprint"]
                or decision.get("source_account") != op.get("paper_account")
                or decision.get("source_intent_id") != op.get("intent_id")
                or decision.get("source_decision_sha256") != op.get("decision_sha256")
                or decision.get("sealed_at") != op.get("decision_sealed_at")
            ):
                raise ValueError(
                    "historical order or source no longer matches its registered manifest witness"
                )
            tier, reason = _evidence_tier(root, op, "historical", account=account)
            if tier != "A":
                raise ValueError(
                    "historical action requires current A evidence: " + str(reason)
                )
            if execution and digest(book) != digest(op["second_book"]):
                raise ValueError(
                    "historical execution book differs from its verified manifest"
                )
            if settlement is not None:
                events = op.get("settlements", [op.get("settlement")])
                if not any(digest(event) == digest(settlement) for event in events):
                    raise ValueError(
                        "historical settlement is outside the order's replay manifest"
                    )


def _clock_gate(data_dir, account):
    with connection(data_dir) as db:
        mode = account_row(db, account)["mode"]
        if mode == "historical":
            _require_current_replay(db, account)
    if mode == "formal":
        store.require_formal(data_dir)
    return mode


def _ordered(db, account):
    return db.execute(
        "SELECT * FROM paper_intents WHERE account=? AND status='frozen' "
        "ORDER BY cutoff,priority,fixture,condition_id,token,id",
        (account,),
    )


def reservations(db, account):
    available = number(account_row(db, account)["cash"])
    return reserve_budgets(
        available,
        (
            (row["id"], number(json.loads(row["result"])["budget"]))
            for row in _ordered(db, account)
        ),
    )


def reserve_budgets(available, ordered_budgets):
    result = {}
    for ident, budget in ordered_budgets:
        amount = min(available, budget)
        result[ident] = amount
        available -= amount
    return result


def _publish_compact(db, account):
    acc = account_row(db, account)
    reserved = sum(reservations(db, account).values(), ZERO)
    store.put_report(db, "ledger", account, {
        "account": account,
        "mode": acc["mode"],
        **_binding(db, account),
        "protocol": PROTOCOL,
        "initial_cash": acc["initial"],
        "cash": acc["cash"],
        "reserved_cash": fmt(reserved),
        "available_cash": fmt(number(acc["cash"]) - reserved),
        "detail_view": "on_demand",
    })


def _publish(db, account):
    if account_row(db, account)["mode"] == "historical":
        return
    batch = dbscope.for_connection(db)
    if batch is not None:
        batch.dirty.add(account)
    else:
        _publish_compact(db, account)


def freeze(data_dir, account, intent, book, *, replay_op=None):
    """Seal the first observation only. Repeated keys cannot revise an earlier decision."""
    ident, fixture = (str(intent.get(k) or "") for k in ("intent_id", "match_id"))
    if not ident or not fixture:
        raise ValueError("intent_id and match_id required")
    cutoff, planned = instant(intent.get("decision_at")), instant(intent.get("kickoff"))
    if planned - cutoff != timedelta(minutes=60):
        raise ValueError("main paper decision must be T-60")
    mode = _clock_gate(data_dir, account)
    if mode in ("formal", "shadow"):
        recover(data_dir, account)
    seal = authority = origin = None
    if mode == "historical":
        if (
            not isinstance(replay_op, dict)
            or replay_op.get("intent") != intent
            or replay_op.get("first_book") != book
        ):
            raise ValueError(
                "historical orders require their registered replay manifest and original formal seal; use market-replay"
            )
        with connection(data_dir) as db:
            member = db.execute(
                "SELECT 1 FROM paper_replay_members WHERE account=? AND opportunity_digest=?",
                (account, digest(replay_op)),
            ).fetchone()
            if not member:
                raise ValueError(
                    "historical opportunity is outside this account's replay manifest"
                )
            origin = _historical_origin(data_dir, replay_op, account=account, db=db)
            prior = db.execute(
                "SELECT 1 FROM paper_intents WHERE account=? AND id=?", (account, ident)
            ).fetchone()
        if prior:
            _check_history_action(data_dir, account, intent_id=ident)
        else:
            tier, reason = _evidence_tier(data_dir, replay_op, "historical", account=account)
            if tier != "A":
                raise ValueError("historical reservation requires current A evidence: " + str(reason))
    if mode == "formal":
        store.require_formal(data_dir, at=iso(cutoff))
        if not intent.get("forecast_digest"):
            raise ValueError("formal intent requires a frozen forecast digest")
        seal = store.check_formal_forecast(
            data_dir, intent["forecast_digest"], fixture, iso(planned), iso(cutoff)
        )
        authority = _formal_authority(data_dir, account, intent, seal, cutoff)
    original = {"intent": intent, "book": book}
    fingerprint = digest(original)
    c = intent.get("contract") or {}
    priority = intent.get("priority", 0)
    if type(priority) is not int:
        raise ValueError("priority must be a predeclared integer")
    with connection(data_dir, True) as db:
        if account.startswith("selector."):
            from .strategy import authorize_ledger

            authorize_ledger(db, account, intent, book)
        existing = db.execute(
            "SELECT fingerprint,result FROM paper_intents WHERE account=? AND id=?",
            (account, ident),
        ).fetchone()
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise ValueError("sealed intent conflicts with an earlier decision")
            if origin:
                witness = db.execute(
                    "SELECT payload,opportunity_digest FROM paper_replay_witnesses WHERE account=? AND intent_id=?",
                    (account, ident),
                ).fetchone()
                if (
                    not witness
                    or witness["opportunity_digest"] != digest(replay_op)
                    or digest(json.loads(witness["payload"])) != digest(replay_op)
                ):
                    raise ValueError(
                        "historical order is read-only; recorded witness differs from the registered replay manifest"
                    )
            return json.loads(existing["result"])
        cash = number(account_row(db, account)["cash"])
        obj = dict.fromkeys(
            (
                "reason",
                "spent",
                "net_shares",
                "unit_cost",
                "fill_price",
                "fee_cash_equivalent",
                "payout",
                "proceeds",
                "pnl",
                "clv",
                "close_mid",
            )
        )
        obj.update(
            intent_id=ident,
            match_id=fixture,
            condition_id=str(c.get("condition_id") or ""),
            token_id=str(c.get("token_id") or ""),
            market_family=_market_family(c),
            contract_family=c.get("family"),
            selection=c.get("selection") or c.get("side"),
            team=c.get("team"),
            side=c.get("side"),
            line=c.get("line"),
            decision_at=iso(cutoff),
            kickoff=iso(planned),
            status="frozen",
            protocol=PROTOCOL,
            model_version=intent.get("model_version"),
            cash_at_decision=fmt(cash),
            sealed_at=store.now_utc(),
            decision_sha256=fingerprint,
        )
        if origin:
            obj.update(
                sealed_at=origin["decision_sealed_at"],
                source_account=origin["paper_account"],
                source_intent_id=origin["intent_id"],
                source_decision_sha256=origin["decision_sha256"],
            )
        if authority:
            obj.update(
                account_role=authority["role"],
                candidate_key=authority["candidate_key"],
                candidate_version=authority["candidate_version"],
                registration_digest=authority["registration_digest"],
            )
        if (
            mode in ("formal", "shadow")
            and not cutoff - timedelta(seconds=30)
            <= instant(obj["sealed_at"])
            <= cutoff
        ):
            raise ValueError(
                "formal decision must actually be sealed in the 30 seconds before T-60"
            )
        try:
            check_contract(c, intent, cutoff)
            if (
                not intent.get("model_version")
                or instant(intent.get("prediction_at")) > cutoff
            ):
                raise ValueError("missing or late prediction")
            if mode in ("formal", "shadow") and instant(
                intent["prediction_at"]
            ) > instant(obj["sealed_at"]):
                raise ValueError("prediction from the future")
            if intent.get("protocol", PROTOCOL) != PROTOCOL:
                raise ValueError("protocol mismatch")
            if (
                "second_book" in intent
                or "settlement" in intent
                or "close_book" in intent
            ):
                raise ValueError("future execution or settlement evidence in decision")
            quoted = check_book(book, c, cutoff, planned, "first")
            if mode in ("formal", "historical"):
                seen_by = obj["sealed_at"]
                _attest_contract(db, data_dir, c, instant(seen_by), planned)
                if (
                    eligible_book(
                        db, data_dir, c, cutoff, planned, "first", observed_by=seen_by
                    )
                    != book
                ):
                    raise ValueError("first book lacks an eligible exchange capture")
            if mode in ("formal", "shadow") and quoted["received"] > instant(
                obj["sealed_at"]
            ):
                raise ValueError("book has not been received at seal time")
            if seal:
                if seal["model_version"] != intent["model_version"] or seal.get(
                    "lineups"
                ) != intent.get("lineups"):
                    raise ValueError(
                        "lineup or model does not match the sealed forecast"
                    )
                if abs(forecast_fair(c, seal) - unit(intent["normal_fv"])) > D(
                    "0.00000001"
                ):
                    raise ValueError(
                        "token fair value does not match the sealed score matrix"
                    )
            for team in ("home", "away"):
                lineup = (intent.get("lineups") or {}).get(team) or {}
                timestamp = (
                    instant(lineup["received_at"])
                    if lineup.get("confirmed") is True
                    else None
                )
                if (
                    timestamp is None
                    or timestamp > cutoff
                    or (
                        mode in ("formal", "shadow")
                        and timestamp > instant(obj["sealed_at"])
                    )
                ):
                    raise ValueError(f"{team} confirmed lineup unavailable at decision")
            fair, upper, worst = fair_values(intent.get("normal_fv"), c)
            limit, qty = (
                unit(intent.get("limit_price")),
                number(intent.get("quantity"), "requested size"),
            )
            requested_budget = number(intent.get("budget", MAX_ORDER))
            if requested_budget <= 0 or qty <= 0 or qty % UNIT or limit <= 0:
                raise ValueError("invalid size, price or available cash")
            if cash <= 0:
                raise ValueError("insufficient cash")
            budget = min(MAX_ORDER, requested_budget, cash)
            obj.update(
                fair_low=fmt(fair),
                fair_high=fmt(upper),
                decision_mid=fmt(quoted["mid"]),
                budget=fmt(budget),
                limit_price=fmt(limit),
            )
            if abs(fair - quoted["mid"]) > MAX_GAP:
                raise ValueError("market disagreement exceeds 0.08")
            depth = pair_depth(quoted["asks"], quoted["asks"], limit)
            candidate = fit(depth, qty, c["fee_schedule"], budget, fair)
            if candidate["gross"] < quoted["minimum"] or candidate["net"] <= 0:
                raise ValueError("insufficient edge, depth, budget or minimum size")
            if db.execute(
                "SELECT 1 FROM paper_resolutions WHERE account=? AND condition_id=? AND token=? AND terminal=1",
                (account, c["condition_id"], c["token_id"]),
            ).fetchone():
                raise ValueError("token already resolved")
            obj.update(
                locked_quantity=fmt(candidate["gross"]),
                execution_target=iso(
                    cutoff + timedelta(seconds=max(5, int(c["seconds_delay"])))
                ),
                break_even_normal_fv_at_one_percent=fmt(
                    (candidate["cost"] - D(".01") * worst) / D(".99")
                ),
            )
        except (ValueError, TypeError, KeyError) as exc:
            obj.update(status="skipped", reason=str(exc))
        if db.execute(
            "SELECT 1 FROM paper_intents WHERE account=? AND fixture=? AND condition_id=? "
            "AND token=? AND cutoff=?",
            (account, fixture, obj["condition_id"], obj["token_id"], iso(cutoff)),
        ).fetchone():
            raise ValueError("same account opportunity already sealed")
        db.execute(
            "INSERT INTO paper_intents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                account,
                ident,
                fixture,
                obj["condition_id"],
                obj["token_id"],
                iso(cutoff),
                priority,
                obj["status"],
                fingerprint,
                packed(original),
                packed(obj),
            ),
        )
        if origin:
            db.execute(
                "INSERT INTO paper_replay_witnesses VALUES (?,?,?,?)",
                (account, ident, digest(replay_op), packed(replay_op)),
            )
        _publish(db, account)
        if mode in ("formal", "shadow"):
            if instant(store.now_utc()) > cutoff:
                raise DecisionWindowClosed("decision deadline elapsed before saving the new intent")
            batch = dbscope.current(data_dir)
            if batch is not None:
                batch.deadline = min(batch.deadline, cutoff) if batch.deadline else cutoff
                if obj["status"] == "frozen" and account in batch.recovered_until:
                    expires = instant(obj["execution_target"]) + timedelta(seconds=5)
                    seen = batch.recovered_until[account]
                    batch.recovered_until[account] = min(expires, seen) if seen else expires
        return obj


def _remaining(db, account, condition, token, snapshot, *, research=False):
    table = "research_shadow_depth" if research else "paper_depth"
    used = {
        number(r["price"]): number(r["consumed"])
        for r in db.execute(
            f"SELECT price,consumed FROM {table} WHERE account=? AND condition_id=? AND token=? AND snapshot=?",
            (account, condition, token, snapshot["key"]),
        )
    }
    return [
        (p, q - used.get(p, ZERO)) for p, q in snapshot["asks"] if q > used.get(p, ZERO)
    ]


def _consume(db, account, condition, token, first, second, used, *, research=False):
    table = "research_shadow_depth" if research else "paper_depth"
    totals = defaultdict(lambda: ZERO)
    for a, b, q in used:
        totals[first["key"], a] += q
        if first["key"] != second["key"]:
            totals[second["key"], b] += q
    for (key, p), q in totals.items():
        params = (account, condition, token, key, fmt(p.normalize()))
        row = db.execute(
            f"SELECT consumed FROM {table} WHERE account=? AND condition_id=? AND token=? "
            "AND snapshot=? AND price=?",
            params,
        ).fetchone()
        consumed = q + (number(row[0]) if row else ZERO)
        db.execute(
            f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?)",
            (*params, fmt(consumed)),
        )


def _risk_scenarios(db, account, fixture, contract, *, research=False):
    query = (
        "SELECT plan AS original,result FROM research_shadow_orders "
        "WHERE account=? AND fixture=? AND status IN ('held','settled')"
        if research
        else "SELECT original,result FROM paper_intents WHERE account=? AND fixture=? AND status IN ('filled','settled')"
    )
    held = []
    for row in db.execute(query, (account, fixture)):
        original, out = json.loads(row["original"]), json.loads(row["result"])
        held.append(
            (
                original["intent"]["contract"],
                number(out["net_shares"]),
                number(out["spent"]),
                number(out["proceeds"]) if out.get("proceeds") is not None else None,
            )
        )
    all_contracts = [c for c, *_ in held] + [contract]
    bound = max([int(number(c.get("line", 0))) for c in all_contracts] + [1]) + 3
    scores = [(h, a) for h in range(bound + 1) for a in range(bound + 1)]
    cases = [("normal", score) for score in scores]
    for state in sorted(set().union(*(c["special"] for c in all_contracts))):
        conditional = any(
            isinstance(c["special"].get(state), dict) for c in all_contracts
        )
        cases.extend((state, s) for s in (scores if conditional else [None]))

    def payout(c, state, score):
        if state != "normal" and state not in c["special"]:
            # An unverified rule cannot be counted as a hedge.
            return ZERO
        return scenario_payout(c, state, score)

    possible = {
        tuple(payout(c, state, score) for c in all_contracts) for state, score in cases
    }
    groups = set()
    for vector in possible:
        if None not in vector:
            groups.add(vector)
        else:
            # Waiting tokens share one eventual fixture outcome. A further unresolved exception
            # provides no guaranteed asset value until all relevant deadlines have passed.
            for future in possible:
                if future == vector:
                    continue
                groups.add(
                    tuple(
                        (continuation or ZERO) if value is None else value
                        for value, continuation in zip(vector, future)
                    )
                )
    slopes = {}
    for vector in groups:
        loss = sum(
            (
                cost - (proceeds if proceeds is not None else shares * rate)
                for (_, shares, cost, proceeds), rate in zip(held, vector)
            ),
            ZERO,
        )
        pay = vector[-1]
        slopes[pay] = max(loss, slopes.get(pay, loss))

    def worst(fill):
        return max(
            ZERO,
            max(
                loss + fill["spent"] - fill["net"] * pay for pay, loss in slopes.items()
            ),
        )

    return worst


def _execution_order(row):
    return (json.loads(row["result"])["execution_target"], *reservation_order(row))


def reservation_order(row):
    return (
        row["cutoff"],
        row["priority"],
        row["fixture"],
        row["condition_id"],
        row["token"],
        row["id"],
    )


def recover(data_dir, account, *, before_id=None):
    """Finish expired live observation windows in order, using only originally captured books."""
    with connection(data_dir) as db:
        mode = account_row(db, account)["mode"]
        if mode == "historical":
            _require_current_replay(db, account)
        if mode not in ("formal", "shadow"):
            return {"account": account, "recovered": []}
        now = instant(store.now_utc())
        batch = dbscope.current(data_dir)
        if batch is not None and account in batch.recovered_until:
            expires = batch.recovered_until[account]
            if expires is None or now <= expires:
                return {"account": account, "recovered": []}
        next_expiry = None
        jobs = []
        for row in sorted(_ordered(db, account), key=_execution_order):
            out = json.loads(row["result"])
            at = instant(out["execution_target"])
            if row["id"] == before_id or now <= at + timedelta(seconds=5):
                next_expiry = at + timedelta(seconds=5)
                break
            intent = json.loads(row["original"])["intent"]
            jobs.append(
                (
                    row["id"],
                    eligible_book(
                        db,
                        data_dir,
                        intent["contract"],
                        at,
                        intent["kickoff"],
                        "second",
                        observed_by=now,
                    ),
                )
            )
    done = []
    for ident, book in jobs:
        result = evaluate(data_dir, account, ident, book, _recover=False)
        done.append({"intent_id": ident, "status": result["status"]})
    if batch is not None and before_id is None:
        batch.recovered_until[account] = next_expiry
    return {"account": account, "recovered": done}


def _closing_book(root, db, contract, proof):
    from .archive_proof import eligible_book

    kickoff = instant(proof["actual_kickoff"])
    return eligible_book(
        db, root, contract, kickoff, kickoff, "close", verified_actual=proof
    )


def _proven_kickoff(
    root, intent, *, db=None, prospective=False, claimed=None, proof_id=None
):
    proof = kickoff_proof.resolve(
        root,
        intent["match_id"],
        intent["kickoff"],
        intent["decision_at"],
        db=db,
        prospective=prospective,
    )
    if claimed is not None and instant(claimed) != instant(proof["actual_kickoff"]):
        raise ValueError(
            "provided actual kickoff contradicts independent first-half event"
        )
    if proof_id is not None and proof_id != proof["proof_id"]:
        raise ValueError(
            "kickoff proof ID differs from the authenticated fixture evidence"
        )
    return proof


def evaluate(data_dir, account, intent_id, book, *, actual_kickoff=None, _recover=True):
    """Attempt one execution against the locked decision, once, including an explicit timeout."""
    mode = _clock_gate(data_dir, account)
    if mode == "historical":
        _check_history_action(
            data_dir, account, intent_id=intent_id, execution=True, book=book
        )
    # A caller cannot force an otherwise eligible frozen order to skip by moving kickoff.
    # At T−60 the as-of schedule and pregame market receipt bound the live fill. A verified
    # final timeline may subsequently audit an early start; it cannot add an order.
    proof_time = None
    if mode != "demo":
        with connection(data_dir) as reader:
            _, prior, _ = _load(reader, account, intent_id)
            try:
                proof_time = _proven_kickoff(
                    data_dir,
                    prior["intent"],
                    db=reader,
                    prospective=mode in ("formal", "shadow"),
                    claimed=actual_kickoff,
                )["actual_kickoff"]
            except kickoff_proof.MissingKickoffProof:
                if actual_kickoff is not None or mode == "historical":
                    raise
        # A final report arriving between two calls must not change the original attempt key.
        actual_kickoff = None
    if mode in ("formal", "shadow") and _recover:
        recover(data_dir, account, before_id=intent_id)
    authority_error = None
    if mode == "formal":
        with connection(data_dir) as reader:
            _, prior, entry = _load(reader, account, intent_id)
        if entry["status"] == "frozen":
            order = prior["intent"]
            try:
                seal = store.check_formal_forecast(
                    data_dir,
                    order.get("forecast_digest"),
                    order["match_id"],
                    order["kickoff"],
                    order["decision_at"],
                )
                _formal_authority(
                    data_dir, account, order, seal, instant(order["decision_at"])
                )
            except ValueError as exc:
                authority_error = str(exc)
    with connection(data_dir, True) as db:
        _row, original, obj = _load(db, account, intent_id)
        attempt = digest({"book": book, "actual_kickoff": actual_kickoff})
        if obj["status"] != "frozen":
            if mode == "formal" and book is None and actual_kickoff is None:
                return obj
            if obj.get("attempt_sha256") and obj["attempt_sha256"] != attempt:
                raise ValueError(
                    "execution attempt already finalized with different evidence"
                )
            return obj
        intent, c = original["intent"], original["intent"]["contract"]
        target = instant(obj["execution_target"])
        clock = instant(store.now_utc())
        if mode in ("formal", "shadow") and (
            clock < target or (book is None and clock < target + timedelta(seconds=5))
        ):
            raise ValueError("execution observation window has not elapsed")
        funds = reservations(db, account)
        pending_rows = list(_ordered(db, account))
        pending = min(pending_rows, key=_execution_order)["id"]
        if pending != intent_id:
            raise ValueError(
                f"earlier frozen intent must execute or expire first: {pending}"
            )
        if mode == "formal":
            captured = eligible_book(
                db, data_dir, c, target, intent["kickoff"], "second", observed_by=clock
            )
            if captured is None and clock < target + timedelta(seconds=5):
                raise ValueError("targeted exchange book has not arrived")
            if book is not None and (
                captured is None or digest(book) != digest(captured)
            ):
                raise ValueError(
                    "execution evidence differs from the targeted exchange capture"
                )
            book = captured
            attempt = digest({"book": book, "actual_kickoff": actual_kickoff})
        obj["attempt_sha256"] = attempt
        obj["execution_evidence"] = book
        try:
            if authority_error:
                raise ValueError(authority_error)
            planned = instant(intent["kickoff"])
            if mode in ("formal", "historical"):
                _attest_contract(db, data_dir, c, instant(obj["decision_at"]), planned)
            kickoff = (
                min(planned, instant(proof_time or actual_kickoff))
                if proof_time or actual_kickoff
                else planned
            )
            first = check_book(
                original["book"], c, instant(obj["decision_at"]), kickoff, "first"
            )
            second = check_book(book, c, target, kickoff, "second")
            if first["key"] == second["key"] and (
                first["asks"] != second["asks"] or first["mid"] != second["mid"]
            ):
                raise ValueError("identical book identifier has conflicting depth")
            if (
                mode == "historical"
                and eligible_book(db, data_dir, c, target, kickoff, "second") != book
            ):
                raise ValueError("second book lacks an eligible exchange capture")
            if mode == "shadow" and clock > target + timedelta(seconds=5):
                if (
                    eligible_book(
                        db, data_dir, c, target, kickoff, "second", observed_by=clock
                    )
                    != book
                ):
                    raise ValueError(
                        "shadow recovery lacks an original exchange capture"
                    )
            if mode in ("formal", "shadow") and second["received"] > clock:
                raise ValueError("execution receipt is in the future")

            condition, token = obj["condition_id"], obj["token_id"]
            available_first = _remaining(db, account, condition, token, first)
            available_second = _remaining(db, account, condition, token, second)
            paired = pair_depth(
                available_first, available_second, number(obj["limit_price"])
            )
            worst = _risk_scenarios(db, account, obj["match_id"], c)
            fill = fit(
                paired,
                number(obj["locked_quantity"]),
                c["fee_schedule"],
                funds[intent_id],
                number(obj["fair_low"]),
                risk_ok=lambda f: worst(f) <= MAX_MATCH,
            )
            if (
                fill["gross"] < max(first["minimum"], second["minimum"])
                or fill["net"] <= 0
            ):
                raise ValueError(
                    "remaining depth, price, cash or fixture risk insufficient"
                )
            spent, net = fill["spent"], fill["net"]
            cash = number(account_row(db, account)["cash"]) - spent
            if cash < 0:
                raise ValueError("insufficient cash")
            db.execute(
                "UPDATE paper_accounts SET cash=? WHERE account=?", (fmt(cash), account)
            )
            _consume(db, account, condition, token, first, second, fill["used"])
            obj.update(
                status="filled",
                filled_at=iso(second["received"]),
                spent=fmt(spent),
                net_shares=fmt(net),
                gross_shares=fmt(fill["gross"]),
                fee_cash_equivalent=fmt(fill["fees"]),
                fill_price=fmt(fill["notional"] / fill["gross"]),
                unit_cost=fmt(fill["cost"]),
                decimal_odds=fmt(ONE / fill["cost"]),
                fill_levels=fill["levels"],
                worst_match_loss_after=fmt(worst(fill)),
                cash_after_fill=fmt(cash),
            )
        except (ValueError, KeyError, TypeError) as exc:
            obj.update(status="skipped", reason=str(exc))
        _put(db, account, obj)
        _publish(db, account)
        return obj


def settle(data_dir, account, settlement):
    """Reconcile an official terminal token payout; sports results alone cannot release cash."""
    ids = tuple(
        str(settlement.get(k) or "")
        for k in ("condition_id", "token_id", "resolution_id")
    )
    if not all(ids) or not settlement.get("source"):
        raise ValueError("resolution requires condition, token, event and source")
    event_time = instant(settlement.get("resolved_at"))
    status, state = settlement.get("status"), settlement.get("scenario")
    if status not in ("resolved", "pending") or not state:
        raise ValueError("missing resolution status or contract scenario")
    final = status == "resolved"
    amount = unit(settlement.get("payout")) if final else None
    if not final and settlement.get("payout") is not None:
        raise ValueError("pending resolution cannot pay out")
    doc = packed(settlement)
    _check_history_action(data_dir, account, token=ids[:2], settlement=settlement)
    recover(data_dir, account)
    with connection(data_dir, True) as db:
        acc = account_row(db, account)
        if acc["mode"] in ("formal", "shadow") and event_time > instant(
            store.now_utc()
        ):
            raise ValueError("resolution from the future")
        if final and acc["mode"] in ("formal", "historical"):
            _attest_resolution(db, data_dir, settlement, amount)
        if acc["mode"] == "formal":
            from urllib.parse import urlparse

            uri = urlparse(settlement["source"])
            if uri.scheme != "https" or uri.hostname not in (
                "data-api.polymarket.com",
                "gamma-api.polymarket.com",
                "polymarket.com",
                "clob.polymarket.com",
            ):
                raise ValueError(
                    "formal settlement requires an international exchange resolution source"
                )
        old = db.execute(
            "SELECT * FROM paper_resolutions WHERE account=? AND condition_id=? AND token=? AND event_id=?",
            (account, *ids),
        ).fetchone()
        if old:
            if old["payload"] != doc:
                raise ValueError("conflicting repeated resolution event")
            return dict(settlement)
        previous = db.execute(
            "SELECT * FROM paper_resolutions WHERE account=? AND condition_id=? AND token=? "
            "AND terminal=1",
            (account, *ids[:2]),
        ).fetchone()
        if previous:
            prior = json.loads(previous["payload"])
            if not final or number(prior["payout"]) != amount:
                raise ValueError("conflict with previously recorded terminal payout")
            return prior
        rows = list(
            db.execute(
                "SELECT * FROM paper_intents WHERE account=? AND condition_id=? AND token=? "
                "AND status IN ('filled','frozen')",
                (account, *ids[:2]),
            )
        )
        if not rows:
            raise ValueError("no frozen intent or open position for this token")
        cash = number(acc["cash"])
        for row in rows:
            obj = json.loads(row["result"])
            contract = json.loads(row["original"])["intent"]["contract"]
            if event_time < instant(obj.get("filled_at") or obj["decision_at"]):
                raise ValueError("resolution predates intent or fill")
            if final:
                expected = scenario_payout(contract, state, settlement.get("score"))
                if expected is None or expected != amount:
                    raise ValueError(
                        "exchange payout conflicts with archived contract rule; investigate before credit"
                    )
                if row["status"] == "filled":
                    proceeds = (number(obj["net_shares"]) * amount).quantize(
                        UNIT, rounding=ROUND_DOWN
                    )
                    cash += proceeds
                    obj.update(
                        status="settled",
                        payout=fmt(amount),
                        proceeds=fmt(proceeds),
                        pnl=fmt(proceeds - number(obj["spent"])),
                        resolved_at=iso(event_time),
                        resolution_id=ids[2],
                        resolution_scenario=state,
                        resolution_source=settlement["source"],
                    )
                else:
                    obj.update(
                        status="skipped", reason="token resolved before execution"
                    )
            else:
                if state not in contract["special"]:
                    raise ValueError("unknown pending scenario")
                obj.update(last_pending_at=iso(event_time), pending_scenario=state)
            _put(db, account, obj)
        db.execute(
            "INSERT INTO paper_resolutions VALUES (?,?,?,?,?,?)",
            (account, *ids, int(final), doc),
        )
        db.execute(
            "UPDATE paper_accounts SET cash=? WHERE account=?", (fmt(cash), account)
        )
        _publish(db, account)
        return dict(settlement)


def _clear_close(position, reason):
    position.update(
        close_mid=None,
        clv=None,
        close_received_at=None,
        close_sha256=None,
        actual_kickoff=None,
        kickoff_proof_id=None,
        kickoff_source_sha256=None,
        close_grade="missing",
        close_missing_reason=str(reason),
    )
    return position


def record_close(data_dir, account, intent_id, book=None, actual_kickoff=None):
    """Finalize the last eligible pregame mark independently of cash and resolution."""
    _check_history_action(data_dir, account, intent_id=intent_id)
    with connection(data_dir, True) as db:
        row, original, obj = _load(db, account, intent_id)
        if row["status"] not in ("filled", "settled"):
            raise ValueError("CLV requires a paper fill")
        mode = account_row(db, account)["mode"]
        intent = original["intent"]
        proof = None
        if mode == "demo":
            kickoff = instant(actual_kickoff)
        else:
            try:
                proof = _proven_kickoff(
                    data_dir, intent, db=db, prospective=mode in ("formal", "shadow")
                )
            except ValueError as exc:
                _clear_close(obj, exc)
                _put(db, account, obj)
                _publish(db, account)
                return obj
            kickoff = instant(proof["actual_kickoff"])
            if actual_kickoff is not None and instant(actual_kickoff) != kickoff:
                raise ValueError(
                    "provided actual kickoff contradicts independent first-half event"
                )
            # Select from verified captured depth. An input file cannot omit the true last
            # qualifying observation or replace it with a more convenient snapshot.
            book = _closing_book(data_dir, db, intent["contract"], proof)
        try:
            close = check_book(book, intent["contract"], kickoff, kickoff, "close")
            if close["received"] < instant(obj["filled_at"]):
                raise ValueError("close predates entry")
        except (ValueError, KeyError, TypeError) as exc:
            if mode != "demo" or obj.get("close_mid") is None:
                _clear_close(obj, exc)
                _put(db, account, obj)
                _publish(db, account)
            return obj
        when = iso(close["received"])
        if (
            mode == "demo"
            and obj.get("close_received_at")
            and when < obj["close_received_at"]
        ):
            return obj
        if obj.get("close_received_at") == when and obj.get("close_sha256") != digest(
            book
        ):
            raise ValueError("conflicting close at the same observation instant")
        obj.update(
            close_mid=fmt(close["mid"]),
            clv=fmt(close["mid"] - number(obj["unit_cost"])),
            close_received_at=when,
            close_sha256=digest(book),
            actual_kickoff=iso(kickoff),
            close_missing_reason=None,
            close_grade="verified_first_half_event" if proof else "synthetic",
            kickoff_proof_id=proof["proof_id"] if proof else None,
            kickoff_source_sha256=proof["source_sha256"] if proof else None,
        )
        _put(db, account, obj)
        _publish(db, account)
        return obj


def _summary(db, account):
    acc = account_row(db, account)
    intents = [
        json.loads(r["result"])
        for r in db.execute(
            "SELECT result FROM paper_intents WHERE account=? ORDER BY cutoff,priority,fixture,condition_id,token,id",
            (account,),
        )
    ]
    if acc["mode"] != "demo":
        # Hide legacy manual marks, removed original files and newly conflicting source
        # revisions even on read-only reports. Neither positions nor settlement cash changes.
        root = Path(db.execute("PRAGMA database_list").fetchone()["file"]).parent
        for position in intents:
            if position.get("clv") is None:
                continue
            try:
                if not position.get("kickoff_proof_id"):
                    raise ValueError(
                        "legacy close lacks independent actual kickoff proof"
                    )
                _, original, _ = _load(db, account, position["intent_id"])
                intent = original["intent"]
                proof = _proven_kickoff(
                    root,
                    intent,
                    db=db,
                    prospective=acc["mode"] in ("formal", "shadow"),
                    claimed=position.get("actual_kickoff"),
                    proof_id=position["kickoff_proof_id"],
                )
                book = _closing_book(root, db, intent["contract"], proof)
                if book is None or digest(book) != position.get("close_sha256"):
                    raise ValueError(
                        "archived last qualifying close no longer verifies"
                    )
            except (ValueError, KeyError, TypeError, OSError) as exc:
                _clear_close(position, exc)
    # Raw execution evidence remains attached to its immutable order in SQLite.
    for intent in intents:
        intent.pop("execution_evidence", None)
    fills = [i for i in intents if i["status"] in ("filled", "settled")]
    settled = [i for i in fills if i["status"] == "settled"]
    active = [i for i in fills if i["status"] == "filled"]
    cost = sum((number(i["spent"]) for i in fills), ZERO)
    resolved_cost = sum((number(i["spent"]) for i in settled), ZERO)
    pnl = sum((number(i["pnl"]) for i in settled), ZERO)
    initial, cash = number(acc["initial"]), number(acc["cash"])
    equity = high = initial
    drawdown = ZERO
    by_resolution = defaultdict(lambda: ZERO)
    for i in settled:
        by_resolution[i["resolved_at"]] += number(i["pnl"])
    for when in sorted(by_resolution):
        equity += by_resolution[when]
        high = max(high, equity)
        drawdown = max(drawdown, (high - equity) / high)
    committed = sum(reservations(db, account).values(), ZERO)
    verification = {}
    if acc["mode"] == "historical":
        version = _replay_version(db, account)
        verification = {
            "replay_verifier": version,
            "legacy_read_only": version != HISTORICAL_VERIFIER,
        }
    return {
        "account": account,
        "mode": acc["mode"],
        **verification,
        **_binding(db, account),
        "protocol": PROTOCOL,
        "initial_cash": fmt(initial),
        "cash": fmt(cash),
        "reserved_cash": fmt(committed),
        "available_cash": fmt(cash - committed),
        "fills": len(fills),
        "open_positions": len(active),
        "settled_positions": len(settled),
        "total_spent": fmt(cost),
        "settled_cost": fmt(resolved_cost),
        "realized_pnl": fmt(pnl),
        "realized_roi": fmt(pnl / resolved_cost) if resolved_cost else None,
        "total_fees_cash_equivalent": fmt(
            sum((number(i["fee_cash_equivalent"]) for i in fills), ZERO)
        ),
        "equity_at_cost": fmt(equity),
        "max_drawdown_at_cost": fmt(drawdown),
        "valuation": "open positions at entry cost; drawdowns change on exchange settlement",
        "clv_covered_fills": sum(i["clv"] is not None for i in fills),
        "intents": intents,
    }


def ledger_details(data_dir, account):
    """Assemble a consistent current audit without writing to the ledger."""
    with connection(data_dir) as db:
        if not db.in_transaction:
            db.execute("BEGIN")
        return _summary(db, account)


def ledger_report(data_dir, account):
    # Full evidence revalidation can be slow. Release the write lock before reading it.
    with connection(data_dir, True) as db:
        batch = dbscope.current(data_dir)
        if batch is None:
            _publish_compact(db, account)
        else:
            batch.dirty.add(account)
    return ledger_details(data_dir, account)


def audit_risk(
    data_dir,
    version,
    *,
    account=None,
    window_start=None,
    window_end=None,
    protocol=None,
    fixture_ids=None,
    mode=None,
):
    start = instant(window_start) if window_start else None
    end = instant(window_end) if window_end else None
    fixtures = set(fixture_ids) if fixture_ids is not None else None
    audited, checked, fills, failures = [], 0, 0, []
    with connection(data_dir) as db:
        accounts = list(db.execute("SELECT * FROM paper_accounts ORDER BY account"))
        for acc in accounts:
            if (
                acc["mode"] == "demo"
                or (mode is not None and acc["mode"] != mode)
                or (account is not None and account != acc["account"])
            ):
                continue
            if (
                acc["mode"] == "historical"
                and _replay_version(db, acc["account"]) != HISTORICAL_VERIFIER
            ):
                continue
            report = _summary(db, acc["account"])
            positions = [
                i
                for i in report["intents"]
                if i["model_version"] == version
                and (fixtures is None or i["match_id"] in fixtures)
                and (protocol is None or protocol == i["protocol"])
                and (start is None or instant(i["decision_at"]) >= start)
                and (end is None or instant(i["decision_at"]) < end)
            ]
            if not positions:
                continue
            audited.append(
                {
                    "account": acc["account"],
                    "mode": acc["mode"],
                    **_binding(db, acc["account"]),
                }
            )
            checked += len(positions)
            opened = [i for i in positions if i["status"] in ("filled", "settled")]
            fills += len(opened)
            if any(
                number(i["spent"]) > MAX_ORDER
                or number(i["worst_match_loss_after"]) > MAX_MATCH
                or number(i["cash_after_fill"]) < 0
                for i in opened
            ):
                failures.append("risk invariant failed")
            receipts = sum(
                (
                    number(i["proceeds"])
                    for i in report["intents"]
                    if i["proceeds"] is not None
                ),
                ZERO,
            )
            if (
                number(acc["cash"])
                != number(acc["initial"]) - number(report["total_spent"]) + receipts
            ):
                failures.append("cash reconciliation failed")
    reason = (
        "no non-demo fills in the registered scope"
        if not fills
        else "; ".join(failures) or None
    )
    return {
        "passed": reason is None,
        "reason": reason,
        "version": version,
        "protocol": protocol,
        "window_start": window_start,
        "window_end": window_end,
        "accounts": audited,
        "checked_intents": checked,
        "checked_fills": fills,
    }


def _blob(root, key):
    path = store.blob_path(root, key or "")
    if not path.is_file():
        raise ValueError("missing archived evidence blob")
    body = store.read_blob(root, key)
    if hashlib.sha256(body).hexdigest() != key:
        raise ValueError("archived evidence hash mismatch")
    return body


def _market_family(c):
    if c.get("family") == "result":
        return "moneyline"
    if c.get("family") == "team_total" and c.get("team") in ("home", "away"):
        return c["team"] + "_total"
    return c.get("family")


def _attest_contract(db, data_dir, c, at, kickoff):
    from .ledger_terms import attest

    attest(db, data_dir, c, at, kickoff)


def capture_resolution(data_dir, token):
    """Read the public settled status of one token; book cash only in settle()."""
    from .market_archive import _fetch

    if not str(token).isdigit():
        raise ValueError("token id must be a decimal exchange token id")
    url = "https://data-api.polymarket.com/v2/tokens"
    raw, reply, sha = _fetch(
        data_dir, "resolution", str(token), url, {"token_id": str(token)}
    )
    rows = [r for r in raw.get("data", []) if str(r.get("token_id")) == str(token)]
    if len(rows) != 1:
        raise ValueError("token not found or ambiguous in exchange response")
    r = rows[0]
    if not isinstance(r.get("resolved"), bool) or (
        r["resolved"] != (r.get("final_price") is not None)
    ):
        raise ValueError("unrecognized exchange token resolution")
    return {
        "condition_id": r["condition_id"],
        "token_id": str(token),
        "resolution_id": sha,
        "source": reply.url,
        "source_sha256": sha,
        "resolved_at": reply.received_at,
        "time_basis": "first verified by this capture",
        "status": "resolved" if r["resolved"] else "pending",
        "payout": fmt(unit(r["final_price"])) if r["resolved"] else None,
    }


def _attest_resolution(db, data_dir, event, amount):
    sha = event.get("source_sha256")
    doc = json.loads(_blob(data_dir, sha))
    obs = db.execute(
        "SELECT observed_at,metadata FROM observations WHERE source='polymarket.com' AND sha256=? "
        "AND observed_at<=? ORDER BY observed_at LIMIT 1",
        (sha, iso(instant(event["resolved_at"]))),
    ).fetchone()
    if not obs:
        raise ValueError("no timely exchange token resolution receipt")
    url = json.loads(obs["metadata"]).get("url", "")
    if (
        url != event["source"]
        or url.split("?")[0] != "https://data-api.polymarket.com/v2/tokens"
    ):
        raise ValueError("unverified international token resolution source")
    values = [
        r
        for r in doc.get("data", [])
        if str(r.get("token_id")) == str(event["token_id"])
        and r.get("condition_id") == event["condition_id"]
    ]
    if (
        len(values) != 1
        or values[0].get("resolved") is not True
        or unit(values[0].get("final_price")) != amount
    ):
        raise ValueError("payout not confirmed by archived exchange token")


def _formal_receipt(data_dir, db, row):
    """Read the original paper decision and independently verify its model and time of arrival."""
    original, result = json.loads(row["original"]), json.loads(row["result"])
    intent = original["intent"]
    cutoff, planned = instant(intent["decision_at"]), instant(intent["kickoff"])
    sealed = instant(result["sealed_at"])
    if (
        row["mode"] != "formal"
        or row["role"] != "main"
        or digest(original) != row["fingerprint"]
        or result.get("decision_sha256") != row["fingerprint"]
        or row["id"] != intent["intent_id"]
        or row["fixture"] != intent["match_id"]
        or row["condition_id"] != intent["contract"]["condition_id"]
        or row["token"] != intent["contract"]["token_id"]
        or instant(row["cutoff"]) != cutoff
        or planned != cutoff + timedelta(hours=1)
        or not cutoff - timedelta(seconds=30) <= sealed <= cutoff
    ):
        raise ValueError(
            "invalid original formal decision identity, plan digest or seal receipt"
        )
    forecast = store.check_formal_forecast(
        data_dir, intent["forecast_digest"], row["fixture"], iso(planned), iso(cutoff)
    )
    model_receipt = db.execute(
        "SELECT sealed_at,model_version FROM forecast_seals WHERE digest=?",
        (intent["forecast_digest"],),
    ).fetchone()
    if (
        not model_receipt
        or model_receipt["model_version"] != intent["model_version"]
        or instant(model_receipt["sealed_at"]) != instant(intent["prediction_at"])
        or instant(model_receipt["sealed_at"]) > sealed
        or forecast.get("fixture_id") != row["fixture"]
        or instant(forecast["kickoff"]) != planned
        or instant(forecast["cutoff"]) != cutoff
        or (
            "sealed_at" in forecast
            and instant(forecast["sealed_at"]) != instant(model_receipt["sealed_at"])
        )
        or forecast.get("lineups") != intent.get("lineups")
        or any(
            instant(forecast["lineups"][side]["received_at"]) > sealed
            for side in ("home", "away")
        )
    ):
        raise ValueError(
            "forecast seal or lineups were unavailable at the original decision"
        )
    if not db.execute(
        "SELECT 1 FROM trial_activations WHERE protocol=? AND starts_at<=? AND recorded_at<=?",
        (PROTOCOL, iso(sealed), iso(sealed)),
    ).fetchone():
        raise ValueError("original formal decision predates trial activation")
    _formal_authority(data_dir, row["account"], intent, forecast, cutoff)
    if abs(forecast_fair(intent["contract"], forecast) - unit(intent["normal_fv"])) > D(
        "0.00000001"
    ):
        raise ValueError("original fair value differs from the sealed forecast matrix")
    if row["account"].startswith("selector."):
        if row["account"] != "selector.active":
            raise ValueError(
                "experimental selection arms are not independent main-account originals"
            )
        from .strategy import attest_active_order

        attest_active_order(db, intent, original["book"], sealed)
    return {
        "paper_account": row["account"],
        "intent_id": row["id"],
        "decision_sealed_at": iso(sealed),
        "decision_sha256": row["fingerprint"],
        "forecast_digest": intent["forecast_digest"],
    }, original


def _historical_origin(data_dir, op, *, account=None, db=None):
    if db is None:
        with connection(data_dir) as reader:
            return _historical_origin(data_dir, op, account=account, db=reader)
    row = db.execute(
        "SELECT i.*,a.mode,b.role FROM paper_intents i JOIN paper_accounts a ON a.account=i.account "
        "JOIN paper_account_bindings b ON b.account=i.account WHERE i.account=? AND i.id=?",
        (op.get("paper_account"), op.get("intent_id")),
    ).fetchone()
    if not row:
        raise ValueError(
            "historical A requires the original formal account and sealed intent"
        )
    receipt, original = _formal_receipt(data_dir, db, row)
    if any(op.get(k) != v for k, v in receipt.items()) or digest(
        op.get("first_book")
    ) != digest(original["book"]):
        raise ValueError(
            "submitted references or first book differ from the original formal seal"
        )
    if account and account.startswith("selector."):
        from .strategy import attest_history_order

        attest_history_order(db, account, op, original)
    elif digest(op["intent"]) != digest(original["intent"]):
        raise ValueError(
            "submitted historical plan differs from the complete sealed formal intent"
        )
    return receipt


def _evidence_tier(data_dir, op, source, *, account=None):
    reason = None
    try:
        intent = op["intent"]
        contract = intent["contract"]
        cutoff = instant(intent["decision_at"])
        origin = (
            _historical_origin(data_dir, op, account=account)
            if source == "historical"
            else None
        )
        actual = instant(
            op["actual_kickoff"]
            if source == "synthetic"
            else _proven_kickoff(
                data_dir,
                intent,
                claimed=op.get("actual_kickoff"),
                proof_id=op.get("kickoff_proof_id"),
            )["actual_kickoff"]
        )
        planned = instant(intent["kickoff"])
        if not cutoff + timedelta(minutes=60) == planned:
            raise ValueError("not a T-60 decision")
        check_contract(contract, intent, cutoff)
        target = cutoff + timedelta(seconds=max(5, int(contract["seconds_delay"])))
        for key, at, stage in (
            ("first_book", cutoff, "first"),
            ("second_book", target, "second"),
        ):
            book = op[key]
            check_book(book, contract, at, min(planned, actual), stage)
            if source != "synthetic":
                if op.get("depth_provenance") != "self_archive":
                    raise ValueError(
                        "historical executable depth requires native exchange captures with verified receipts"
                    )
                with closing(store.connect(data_dir)) as db:
                    sealed = instant(origin["decision_sealed_at"])
                    _attest_contract(db, data_dir, contract, sealed, planned)
                    if (
                        eligible_book(
                            db,
                            data_dir,
                            contract,
                            at,
                            min(planned, actual),
                            stage,
                            observed_by=sealed
                            if stage == "first"
                            else target + timedelta(seconds=5),
                        )
                        != book
                    ):
                        raise ValueError(
                            "historical book disagrees with the eligible original exchange capture"
                        )
        return "A", None
    except (ValueError, KeyError, TypeError, OSError) as exc:
        reason = str(exc)
    if source == "historical":
        with closing(store.connect(data_dir)) as db:
            return research_tier(db, data_dir, op), reason
    if op.get("trades"):
        return "B", reason
    if op.get("prices") or op.get("price_history"):
        return "C", reason
    return "missing", reason


def replay_fingerprint(source, opportunities):
    # Old hash-only historical replay accounts remain accessible as prior artifacts.
    return digest(
        [source, opportunities, HISTORICAL_VERIFIER]
        if source == "historical"
        else [source, opportunities]
    )


def replay(data_dir, opportunities, *, account=None, source="historical"):
    """Replay distinct opportunities on one cash timeline; B/C observations cannot create fills."""
    if source not in ("historical", "synthetic"):
        raise ValueError("replay source must be historical or synthetic")
    ops = list(opportunities)
    fingerprint = replay_fingerprint(source, ops)
    account = account or ("replay-" + fingerprint[:16])
    if account.startswith("selector.") and source == "historical":
        from .strategy import authorize_history_manifest

        with connection(data_dir) as db:
            authorize_history_manifest(db, account, fingerprint)
    open_account(data_dir, account, "demo" if source == "synthetic" else "historical")
    with connection(data_dir, True) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS paper_replays(account TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)"
        )
        old = db.execute(
            "SELECT fingerprint FROM paper_replays WHERE account=?", (account,)
        ).fetchone()
        if old and old[0] != fingerprint:
            raise ValueError(
                "replay account already tied to a different input manifest"
            )
        if (
            source == "historical"
            and not old
            and db.execute(
                "SELECT 1 FROM paper_intents WHERE account=? LIMIT 1", (account,)
            ).fetchone()
        ):
            raise ValueError(
                "historical account already contains orders outside this replay manifest"
            )
        db.execute(
            "INSERT OR IGNORE INTO paper_replays VALUES (?,?)", (account, fingerprint)
        )
        if source == "historical":
            db.execute(
                "INSERT OR IGNORE INTO paper_replay_verifiers VALUES (?,?,?)",
                (account, HISTORICAL_VERIFIER, fingerprint),
            )
            _require_current_replay(db, account)
            db.executemany(
                "INSERT OR IGNORE INTO paper_replay_members VALUES (?,?)",
                ((account, digest(op)) for op in ops if op.get("intent")),
            )
    details, timeline, opportunities_seen = [], [], set()
    for i, op in enumerate(ops):
        tier, why = _evidence_tier(data_dir, op, source, account=account)
        intent = op.get("intent", {})
        c = intent.get("contract", {})
        fixture = op.get("match_id") or intent.get("match_id")
        family = op.get("family") or _market_family(c)
        if c and family == c.get("family"):
            family = _market_family(c)
        condition = op.get("condition_id") or c.get("condition_id")
        token = op.get("token_id") or c.get("token_id")
        if c and (
            fixture != intent.get("match_id")
            or family != _market_family(c)
            or condition != c.get("condition_id")
            or token != c.get("token_id")
        ):
            raise ValueError("opportunity and intent market identities disagree")
        key = (
            fixture,
            family,
            condition,
            token,
            intent.get("decision_at") or op.get("decision_at"),
        )
        if not fixture or not family or key in opportunities_seen:
            raise ValueError(
                "duplicate or unidentified opportunity in replay denominator"
            )
        opportunities_seen.add(key)
        listed = op.get("listed") is True or bool(condition and token)
        details.append(
            {
                "match_id": fixture,
                "condition_id": condition,
                "token_id": token,
                "family": family,
                "season": op.get("season", "unknown"),
                "liquidity": op.get("liquidity", "unknown"),
                "listed": listed,
                "tier": tier,
                "status": "research_only" if tier != "A" else "eligible",
                "reason": why,
                "priced": tier == "A" or op.get("priced") is True,
                "signal": False,
                "filled": False,
                "settled": False,
            }
        )
        if tier != "A":
            continue
        rank = (
            intent.get("priority", 0),
            fixture,
            condition,
            token,
            intent["intent_id"],
        )
        cutoff = instant(intent["decision_at"])
        target = cutoff + timedelta(seconds=max(5, int(c["seconds_delay"])))
        original_seal = (
            instant(op["decision_sealed_at"]) if source == "historical" else cutoff
        )
        for k, stamp in (
            ("freeze", original_seal),
            ("execute", target + timedelta(seconds=5)),
        ):
            timeline.append((stamp, 0 if k == "freeze" else 1, rank, i, k))
        if source == "historical" or op.get("close_book"):
            closing_at = (
                op["actual_kickoff"]
                if source == "synthetic"
                else _proven_kickoff(data_dir, intent)["actual_kickoff"]
            )
            timeline.append((instant(closing_at), 2, rank, i, "close"))
        for settlement in op.get(
            "settlements", [] if op.get("settlement") is None else [op["settlement"]]
        ):
            timeline.append(
                (instant(settlement["resolved_at"]), 3, rank, i, settlement)
            )
    confirmed_resolutions = {}
    for _, _, _, i, action in sorted(timeline, key=lambda e: e[:4]):
        op, detail = ops[i], details[i]
        intent, ident = op["intent"], op["intent"]["intent_id"]
        if action == "freeze":
            out = freeze(data_dir, account, intent, op["first_book"], replay_op=op)
            detail["signal"] = out.get("locked_quantity") is not None
        elif action == "execute":
            out = evaluate(
                data_dir,
                account,
                ident,
                op["second_book"],
                actual_kickoff=op.get("actual_kickoff"),
            )
            detail["filled"] = out["status"] in ("filled", "settled")
        elif not detail["filled"]:
            continue
        elif action == "close":
            out = record_close(
                data_dir,
                account,
                ident,
                op.get("close_book") if source == "synthetic" else None,
                op.get("actual_kickoff"),
            )
        else:
            try:
                if (str(action.get("condition_id")), str(action.get("token_id"))) != (
                    str(detail["condition_id"]),
                    str(detail["token_id"]),
                ):
                    raise ValueError(
                        "resolution belongs to a different opportunity token"
                    )
                settle(data_dir, account, action)
                # settle authenticates the current original even on idempotent retries.
                # A past transaction alone cannot certify this invocation's payout.
                if action["status"] == "resolved":
                    confirmed_resolutions[
                        (
                            str(detail["condition_id"]),
                            str(detail["token_id"]),
                            str(action["resolution_id"]),
                        )
                    ] = unit(action["payout"])
            except (ValueError, KeyError, TypeError, OSError) as exc:
                detail["settlement_error"] = str(exc)
            continue
        detail.update(status=out["status"], reason=out.get("reason"))
    report = ledger_report(data_dir, account)
    positions = {i["intent_id"]: i for i in report["intents"]}
    groups = {}
    group_rows = defaultdict(list)
    counts = dict.fromkeys(
        (
            "expected",
            "listed",
            "priced",
            "A",
            "B",
            "C",
            "missing",
            "signal",
            "filled",
            "settled",
        ),
        0,
    )
    for op, d in zip(ops, details):
        if d["tier"] == "A":
            position = positions[op["intent"]["intent_id"]]
            d.update(status=position["status"], pnl=None, clv=None)
            if d["filled"]:
                d.update(
                    status="filled",
                    spent=position["spent"],
                    fee_cash_equivalent=position["fee_cash_equivalent"],
                    pnl=None,
                    proceeds=None,
                    clv=position["clv"],
                )
                resolution = (
                    str(d["condition_id"]),
                    str(d["token_id"]),
                    position.get("resolution_id"),
                )
                payout = confirmed_resolutions.get(resolution)
                if position["status"] == "settled" and payout is not None:
                    proceeds = (number(position["net_shares"]) * payout).quantize(
                        UNIT, rounding=ROUND_DOWN
                    )
                    pnl = proceeds - number(position["spent"])
                    if (payout, proceeds, pnl) == (
                        number(position["payout"]),
                        number(position["proceeds"]),
                        number(position["pnl"]),
                    ):
                        d.update(
                            status="settled",
                            settled=True,
                            proceeds=fmt(proceeds),
                            pnl=fmt(pnl),
                        )
                    else:
                        d["settlement_error"] = (
                            "recorded cash flow disagrees with the confirmed exchange payout"
                        )
        key = (d["season"], d["family"], d["liquidity"])
        group = groups.setdefault(key, dict.fromkeys(counts, 0))
        group_rows[key].append(d)
        for counter in (counts, group):
            counter["expected"] += 1
            counter[d["tier"]] += 1
            for k in ("listed", "priced", "signal", "filled", "settled"):
                counter[k] += int(d[k])
    result = {
        "source": source,
        "account": account,
        "protocol": PROTOCOL,
        "manifest_sha256": fingerprint,
        **({"replay_verifier": HISTORICAL_VERIFIER} if source == "historical" else {}),
        "counts": counts,
        "groups": [
            {
                "season": k[0],
                "family": k[1],
                "liquidity": k[2],
                **v,
                **_replay_returns(group_rows[k]),
            }
            for k, v in sorted(groups.items())
        ],
        "opportunities": details,
        **_replay_returns(details),
        "paper_only": True,
    }
    store.save_report(data_dir, "market-replay", account, result)
    return result


def _replay_returns(opportunities):
    """Aggregate only currently eligible fills and their verified cash flows."""
    fills = [row for row in opportunities if row["tier"] == "A" and row["filled"]]
    settled = [row for row in fills if row["settled"]]
    total_cost = sum((number(row["spent"]) for row in fills), ZERO)
    settled_cost = sum((number(row["spent"]) for row in settled), ZERO)
    proceeds = sum((number(row["proceeds"]) for row in settled), ZERO)
    pnl = proceeds - settled_cost
    matched_close = sum(row.get("clv") is not None for row in fills)
    return {
        "total_spent": fmt(total_cost),
        "open_cost": fmt(total_cost - settled_cost),
        "settled_cost": fmt(settled_cost),
        "settled_proceeds": fmt(proceeds),
        "total_fees_cash_equivalent": fmt(
            sum((number(row["fee_cash_equivalent"]) for row in fills), ZERO)
        ),
        "settled_fees_cash_equivalent": fmt(
            sum((number(row["fee_cash_equivalent"]) for row in settled), ZERO)
        ),
        "realized_pnl": fmt(pnl),
        "roi": fmt(pnl / settled_cost) if settled_cost else None,
        "clv_covered_fills": matched_close,
        "clv_eligible_fills": len(fills),
        "clv_coverage": fmt(D(matched_close) / len(fills)) if fills else None,
    }


def example():
    """A labelled, wholly hypothetical contract bundle, usable as a test or a CLI template."""
    t = instant("2025-02-01T12:00:00Z")
    at = lambda seconds: iso(t + timedelta(seconds=seconds))
    rule_hash = digest("synthetic full-time match total with documented exceptions")
    fee = {
        "enabled": True,
        "version": "synthetic-1",
        "received_at": at(-7200),
        "effective_at": at(-7200),
        "model": "symmetric",
        "asset": "cash",
        "rate": "0.05",
        "exponent": 1,
        "quantum": "0.00001",
        "rounding": "half_up",
        "round_in": "cash",
    }
    c = {
        "venue": "polymarket.com",
        "match_id": "demo-home-away",
        "condition_id": "mock-condition",
        "token_id": "mock-over",
        "family": "total",
        "side": "over",
        "line": "2.5",
        "period": "full_time",
        "rule_sha256": rule_hash,
        "rule_received_at": at(-7200),
        "seconds_delay": 0,
        "special": {
            "no_result": "0.5",
            "postponed": "pending",
            "abandoned": {"if_decided": True, "otherwise": "0.5"},
        },
        "fee_schedule": fee,
    }
    intent = {
        "intent_id": "demo-intent",
        "match_id": c["match_id"],
        "decision_at": at(0),
        "kickoff": at(3600),
        "prediction_at": at(-1),
        "model_version": "demo-only",
        "normal_fv": ".465",
        "limit_price": ".45",
        "quantity": "10",
        "budget": "50",
        "contract": c,
        "lineups": {
            side: {"confirmed": True, "received_at": at(-300)}
            for side in ("home", "away")
        },
    }

    def book(s, bid, ask):
        return {
            "venue": c["venue"],
            "token_id": c["token_id"],
            "condition_id": c["condition_id"],
            "requested_at": at(s),
            "received_at": at(s),
            "observed_at": at(s),
            "market_open": True,
            "pregame": True,
            "rule_sha256": rule_hash,
            "fee_version": fee["version"],
            "bids": [{"price": bid, "size": "100"}],
            "asks": [{"price": ask, "size": "100"}],
            "tick_size": ".01",
            "min_order_size": "1",
        }

    return {
        "intent": intent,
        "first_book": book(-1, ".39", ".41"),
        "second_book": book(5, ".40", ".42"),
        "close_book": book(3595, ".45", ".47"),
        "actual_kickoff": at(3600),
        "season": "demo",
        "settlement": {
            "condition_id": c["condition_id"],
            "token_id": c["token_id"],
            "source": "synthetic-exchange",
            "resolution_id": "demo-resolution",
            "resolved_at": at(12500),
            "status": "resolved",
            "scenario": "no_result",
            "payout": ".5",
        },
    }


def demo(data_dir, account="demo-ledger"):
    sample = example()
    rows = [
        sample,
        {
            "match_id": "demo-trade",
            "family": "result",
            "listed": True,
            "season": "demo",
            "trades": [{"price": ".4", "size": "3"}],
        },
        {
            "match_id": "demo-curve",
            "family": "team_total",
            "listed": True,
            "season": "demo",
            "prices": [{"price": ".4"}],
        },
        {"match_id": "demo-missing", "family": "team_total", "season": "demo"},
    ]
    replay_report = replay(data_dir, rows, account=account, source="synthetic")
    return {
        "source": "synthetic",
        "paper_only": True,
        "ledger": ledger_report(data_dir, account),
        "market_replay": replay_report,
    }


def _cli(args):
    root = args.data_dir
    if args.command == "ledger-capture-kickoff":
        return kickoff_proof.capture(
            root, args.fixture, args.event, schedule_version=args.schedule_version
        )
    if args.command == "ledger-import-open-kickoff":
        return kickoff_proof.import_open(
            root,
            args.fixture,
            args.event,
            args.release,
            args.timeline,
            args.licence,
            schedule_version=args.schedule_version,
        )
    if args.command == "ledger-demo":
        return demo(root, args.account)
    if args.command == "ledger-capture-resolution":
        return capture_resolution(root, args.token)
    if args.command == "ledger-recover":
        return recover(root, args.account)
    if args.command == "ledger-example":
        return example()
    if args.command == "ledger-account":
        return open_account(
            root,
            args.account,
            args.mode,
            args.cash,
            role=args.role,
            candidate_key=args.candidate_key,
            version=args.model_version,
        )
    if args.command == "ledger-report":
        return ledger_report(root, args.account)
    item = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if args.command == "market-replay":
        return replay(
            root,
            item if isinstance(item, list) else item["opportunities"],
            account=args.account,
            source=args.source,
        )
    if args.command == "ledger-freeze":
        return freeze(root, args.account, item["intent"], item.get("first_book"))
    if args.command == "ledger-evaluate":
        return evaluate(
            root,
            args.account,
            args.intent_id,
            item.get("second_book"),
            actual_kickoff=item.get("actual_kickoff"),
        )
    if args.command == "ledger-close":
        return record_close(
            root,
            args.account,
            args.intent_id,
            item.get("close_book"),
            item.get("actual_kickoff"),
        )
    return settle(root, args.account, item.get("settlement", item))


def register(subparsers):
    from .fee_registry import register as register_fee_policies

    register_fee_policies(subparsers)
    for name in ("ledger-capture-kickoff", "ledger-import-open-kickoff"):
        parser = subparsers.add_parser(name)
        parser.set_defaults(func=_cli, command=name)
        parser.add_argument("--fixture", required=True)
        parser.add_argument("--schedule-version", required=True, type=int)
        parser.add_argument("--event", required=True)
        if name == "ledger-import-open-kickoff":
            parser.add_argument("--release", required=True)
            parser.add_argument("--timeline", required=True, type=Path)
            parser.add_argument("--licence", required=True, type=Path)
    for name in (
        "ledger-demo",
        "ledger-example",
        "ledger-account",
        "ledger-freeze",
        "ledger-evaluate",
        "ledger-settle",
        "ledger-close",
        "ledger-report",
        "ledger-capture-resolution",
        "ledger-recover",
        "market-replay",
    ):
        parser = subparsers.add_parser(name)
        parser.set_defaults(func=_cli, command=name)
        if name == "ledger-capture-resolution":
            parser.add_argument("--token", required=True)
        if name not in ("ledger-example", "ledger-capture-resolution"):
            if name in ("ledger-demo", "market-replay"):
                parser.add_argument(
                    "--account",
                    default="demo-ledger" if name == "ledger-demo" else None,
                )
            else:
                parser.add_argument("--account", required=True)
        if name == "ledger-account":
            parser.add_argument("--role", choices=("main", "candidate", "research"))
            parser.add_argument("--candidate-key")
            parser.add_argument("--model-version")
            parser.add_argument(
                "--mode",
                choices=("shadow", "historical", "formal", "demo"),
                default="shadow",
            )
            parser.add_argument("--cash", default="10000")
        if name in (
            "ledger-freeze",
            "ledger-evaluate",
            "ledger-settle",
            "ledger-close",
            "market-replay",
        ):
            parser.add_argument("--input", required=True)
        if name in ("ledger-evaluate", "ledger-close"):
            parser.add_argument("--intent-id", required=True)
        if name == "market-replay":
            parser.add_argument(
                "--source", choices=("historical", "synthetic"), default="historical"
            )
