"""Pre-registered selection experiments. The only inputs to performance are paper fills and exchange books."""

import json
import re
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from datetime import timedelta
from decimal import ROUND_DOWN
from pathlib import Path

import numpy as np

from . import ledger as L
from . import store, strategy_scope
from .ledger_rules import (
    MAX_ORDER,
    UNIT,
    ZERO,
    check_book,
    digest,
    fmt,
    instant,
    iso,
    number,
    packed,
    pair_depth,
    price_fill,
)

FAMILIES = ("moneyline", "total", "home_total", "away_total")
BASE = {"families": list(FAMILIES), "rank": "priority", "max_orders": None}
PROTOCOL = store.PROTOCOL
SCHEMA = """
CREATE TABLE IF NOT EXISTS selection_trials(
 key TEXT PRIMARY KEY, start TEXT NOT NULL, spec TEXT NOT NULL, digest TEXT NOT NULL, registered TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'shadow', result TEXT);
CREATE TABLE IF NOT EXISTS selection_accounts(account TEXT PRIMARY KEY, trial TEXT NOT NULL,phase TEXT NOT NULL,arm TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS selection_sheets(trial TEXT NOT NULL,phase TEXT NOT NULL,fixture TEXT NOT NULL,
 cutoff TEXT NOT NULL,created TEXT NOT NULL,sheet TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(trial,phase,fixture));
CREATE TABLE IF NOT EXISTS selection_orders(account TEXT NOT NULL,intent_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
 PRIMARY KEY(account,intent_id));
CREATE TABLE IF NOT EXISTS selection_history_manifests(account TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS selection_events(id INTEGER PRIMARY KEY,trial TEXT NOT NULL,action TEXT NOT NULL,
 at TEXT NOT NULL,recipe TEXT NOT NULL,fingerprint TEXT NOT NULL);
"""


@contextmanager
def database(root, write=False):
    with closing(store.connect(root)) as db:
        db.executescript(SCHEMA)
        with db:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db


def recipe(values):
    if not isinstance(values, dict) or set(values) - set(BASE):
        raise ValueError("a selector supports only families, rank and max_orders")
    value = BASE | values
    families = value["families"]
    if not isinstance(families, list) or not all(
        isinstance(f, str) and f in FAMILIES for f in families
    ):
        raise ValueError("unknown selection market family")
    if value["rank"] not in ("priority", "fair_gap") or (
        value["max_orders"] is not None
        and (
            type(value["max_orders"]) is not int or not 0 <= value["max_orders"] <= 200
        )
    ):
        raise ValueError("invalid fixed priority rule or match limit")
    return {
        "families": sorted(set(families)),
        "rank": value["rank"],
        "max_orders": value["max_orders"],
    }


def _trial(db, key):
    row = db.execute("SELECT * FROM selection_trials WHERE key=?", (key,)).fetchone()
    if not row or digest(json.loads(row["spec"])) != row["digest"]:
        raise ValueError("unknown or altered selection registration")
    return dict(row) | {"spec": json.loads(row["spec"])}


def _active(db, at):
    row = db.execute(
        "SELECT recipe FROM selection_events WHERE at<=? ORDER BY at DESC,id DESC LIMIT 1",
        (at,),
    ).fetchone()
    return json.loads(row[0]) if row else recipe({})


def _deployed_trial(db, at):
    stack = []
    for r in db.execute(
        "SELECT trial,action FROM selection_events WHERE at<=? ORDER BY at,id", (at,)
    ):
        if r["action"] == "promoted":
            stack.append(r["trial"])
        elif stack and stack[-1] == r["trial"]:
            stack.pop()
    return stack[-1] if stack else None


def active(data_dir, at=None):
    with database(data_dir) as db:
        return _active(db, iso(instant(at or store.now_utc())))


def _arm_account(key, phase, arm):
    return "selector.active" if phase == "active" else f"selector.{key}.{phase}.{arm}"


def _season(value):
    if not re.fullmatch(r"\d{4}[-/](?:\d{2}|\d{4})", str(value)):
        raise ValueError("expected EPL season in YYYY-YY form")
    year = int(value[:4])
    if int(value[-2:]) != (year + 1) % 100:
        raise ValueError("invalid season")
    return f"{year}-{(year + 1) % 100:02}"


def register_trial(
    data_dir,
    key,
    selector,
    *,
    source_account,
    history_account,
    history_inputs,
    history_source_account=None,
    season,
    first_round,
    budget,
):
    if not re.fullmatch("[A-Za-z0-9_-]{1,64}", key):
        raise ValueError("invalid selection key")
    candidate = recipe(selector)
    season = _season(season)
    amount = number(budget, "fixed fixture budget")
    if (
        amount <= 0
        or amount % UNIT
        or type(first_round) is not int
        or not 1 <= first_round <= 27
    ):
        raise ValueError(
            "require a positive fixed match budget and a 12-round EPL window"
        )
    inputs = list(history_inputs)
    fp = L.replay_fingerprint("historical", inputs)
    with L.connection(data_dir) as db:
        hist = L.account_row(db, history_account)
        if (
            hist["mode"] != "historical"
            or not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='paper_replays'"
            ).fetchone()
        ):
            raise ValueError("source must be a real historical ledger replay")
        known = db.execute(
            "SELECT fingerprint FROM paper_replays WHERE account=?", (history_account,)
        ).fetchone()
        if not known or known[0] != fp:
            raise ValueError(
                "history inputs do not reproduce the original historical ledger manifest"
            )
        L._require_current_replay(db, history_account)
        if db.execute(
            "SELECT 1 FROM paper_accounts WHERE account=?", (source_account,)
        ).fetchone() and (
            L.account_row(db, source_account)["mode"] != "formal"
            or L._binding(db, source_account)["role"] != "main"
        ):
            raise ValueError(
                "forward opportunity source must be a formal incumbent-model account"
            )
    if not source_account or source_account.startswith("selector."):
        raise ValueError("an independent formal opportunity source is required")
    if any(op.get("season") and _season(op["season"]) >= season for op in inputs):
        raise ValueError("history seasons must precede forward season")
    scope = strategy_scope.scope(
        data_dir, inputs, source_account, source=history_source_account
    )
    strategy_scope.require_complete(scope)
    training = store.archive_bytes(
        data_dir, "selection-history-inputs", packed(inputs).encode()
    )
    stamp = store.now_utc()
    schedule = _window(data_dir, season, first_round)
    with database(data_dir, True) as db:
        old = db.execute(
            "SELECT spec FROM selection_trials WHERE key=?", (key,)
        ).fetchone()
        baseline = json.loads(old[0])["baseline"] if old else _active(db, stamp)
        spec = {
            "key": key,
            "protocol": PROTOCOL,
            "baseline": baseline,
            "candidate": candidate,
            "budget": fmt(amount),
            "season": season,
            "first_round": first_round,
            "rounds": 12,
            "source_account": source_account,
            "history_account": history_account,
            "history_manifest": fp,
            "history_scope": {
                k: scope[k] for k in ("source_account", "fixture_labels", "parents")
            },
            "input_blob": training["sha256"],
        }
        if old and "history_scope" not in json.loads(old[0]):
            # Retain the original immutable registration; the live account reconciliation still applies.
            spec.pop("history_scope")
        identity = digest(spec)
        if old:
            if digest(json.loads(old[0])) != identity:
                raise ValueError("registered selection protocol is immutable")
            return _trial(db, key)
        if baseline == candidate:
            raise ValueError("candidate must change an allowed selection decision")
        if any(
            instant(f["kickoff"]) - timedelta(hours=1) <= instant(stamp)
            for f in schedule
        ):
            raise ValueError(
                "selection registration is later than the first known decision"
            )
        cohort = []
        for r in db.execute("SELECT spec FROM selection_trials"):
            p = json.loads(r[0])
            if (
                p["season"] != season
                or first_round >= p["first_round"] + 12
                or p["first_round"] >= first_round + 12
            ):
                continue
            if any(
                p[k] != spec[k]
                for k in (
                    "first_round",
                    "baseline",
                    "history_manifest",
                    "source_account",
                    "budget",
                )
            ):
                raise ValueError(
                    "overlapping selection experiments require the same predeclared comparisons"
                )
            cohort.append(p)
        if len(cohort) >= 3:
            raise ValueError("at most three selection candidates per reveal window")
        for phase in ("history", "forward"):
            for arm in ("baseline", "candidate"):
                account = _arm_account(key, phase, arm)
                if db.execute(
                    "SELECT 1 FROM paper_accounts WHERE account=?", (account,)
                ).fetchone():
                    raise ValueError(
                        "selection arms must start with independent empty accounts"
                    )
                db.execute(
                    "INSERT INTO selection_accounts VALUES (?,?,?,?)",
                    (account, key, phase, arm),
                )
        db.execute(
            "INSERT INTO selection_trials(key,start,spec,digest,registered) VALUES(?,?,?,?,?)",
            (key, f"{season}/{first_round}", packed(spec), identity, stamp),
        )
        return _trial(db, key)


def _window(root, season, first):
    return [
        f
        for f in store.eligible_fixtures(root)
        if _season(f["season"]) == season and first <= f["round_id"] < first + 12
    ]


def _rows(db, account, fixture, cutoff):
    rows = []
    for row in db.execute(
        "SELECT * FROM paper_intents WHERE account=? AND fixture=? AND cutoff=? "
        "ORDER BY priority,condition_id,token,id",
        (account, fixture, cutoff),
    ):
        out = json.loads(row["result"])
        original = json.loads(row["original"])
        if not out.get("locked_quantity"):
            continue
        if (
            digest(original) != row["fingerprint"]
            or out["decision_sha256"] != row["fingerprint"]
        ):
            raise ValueError("altered reference decision")
        # These values already existed when the source intent was frozen. No fill/close fields enter the selector.
        rows.append(
            {
                "original": original,
                "source_id": row["id"],
                "source_sha": row["fingerprint"],
                "family": out["market_family"],
                "priority": row["priority"],
                "gap": fmt(number(out["fair_low"]) - number(out["decision_mid"])),
                "quantity": out["locked_quantity"],
                "budget": out["budget"],
            }
        )
    return rows


def _choose(values, rule):
    elig = [v for v in values if v["family"] in rule["families"]]

    def order(v):
        c = v["original"]["intent"]["contract"]
        gap = -number(v["gap"]) if rule["rank"] == "fair_gap" else ZERO
        return (
            gap,
            v["priority"],
            str(c["condition_id"]),
            str(c["token_id"]),
            v["source_id"],
        )

    return sorted(elig, key=order)[: rule["max_orders"]]


def _sheet(spec, phase, fixture, season, rnd, cutoff, values):
    amount = number(spec["budget"])
    allocation = (amount / max(1, len(values))).quantize(UNIT, rounding=ROUND_DOWN)
    decisions = {}
    for arm in ("baseline", "candidate"):
        selected = _choose(values, spec[arm])
        plans = []
        for priority, v in enumerate(selected):
            original = v["original"]
            account = _arm_account(spec["key"], phase, arm)
            ident = digest([account, fixture, cutoff, v["source_sha"]])
            order = original["intent"] | {
                "intent_id": ident,
                "quantity": v["quantity"],
                "budget": fmt(min(allocation, MAX_ORDER, number(v["budget"]))),
                "priority": priority,
            }
            plans.append(
                {"source_id": v["source_id"], "intent": order, "book": original["book"]}
            )
        decisions[arm] = plans
    return {
        "fixture": fixture,
        "season": season,
        "round": rnd,
        "cutoff": cutoff,
        "budget": spec["budget"],
        "pool": values,
        "arms": decisions,
    }


def _save_sheet(db, key, phase, sheet, created):
    identity = digest(sheet)
    row = db.execute(
        "SELECT digest FROM selection_sheets WHERE trial=? AND phase=? AND fixture=?",
        (key, phase, sheet["fixture"]),
    ).fetchone()
    if row:
        if row[0] != identity:
            raise ValueError("the shared decision sheet is immutable")
        return
    db.execute(
        "INSERT INTO selection_sheets VALUES(?,?,?,?,?,?,?)",
        (
            key,
            phase,
            sheet["fixture"],
            sheet["cutoff"],
            created,
            packed(sheet),
            identity,
        ),
    )
    for arm, orders in sheet["arms"].items():
        account = _arm_account(key, phase, arm)
        for o in orders:
            db.execute(
                "INSERT INTO selection_orders VALUES(?,?,?)",
                (
                    account,
                    o["intent"]["intent_id"],
                    digest({"intent": o["intent"], "book": o["book"]}),
                ),
            )


def authorize_ledger(db, account, intent, book):
    """Called inside ledger's write transaction. No arbitrary order can consume a trial arm's cash."""
    if not account.startswith("selector."):
        return
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='selection_orders'"
    ).fetchone():
        raise ValueError("selection account requires a registered experiment")
    expected = db.execute(
        "SELECT fingerprint FROM selection_orders WHERE account=? AND intent_id=?",
        (account, intent["intent_id"]),
    ).fetchone()
    if not expected or expected[0] != digest({"intent": intent, "book": book}):
        raise ValueError("order is not selected on this account's frozen common sheet")


def authorize_history_manifest(db, account, fingerprint):
    """Only history() may assemble a complete registered arm from the frozen source list."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='selection_history_manifests'"
    ).fetchone():
        raise ValueError("selection account has no registered history manifest")
    row = db.execute(
        "SELECT fingerprint FROM selection_history_manifests WHERE account=?",
        (account,),
    ).fetchone()
    if not row or row[0] != fingerprint:
        raise ValueError(
            "replay differs from the complete registered selection arm manifest"
        )


def attest_active_order(db, intent, book, sealed):
    authorize_ledger(db, "selector.active", intent, book)
    key = _deployed_trial(db, iso(sealed))
    if not key:
        raise ValueError("selection strategy was not deployed at the formal seal")
    row = db.execute(
        "SELECT sheet,digest FROM selection_sheets WHERE trial=? AND phase='active' AND fixture=? AND cutoff=? AND created<=?",
        (key, intent["match_id"], iso(instant(intent["decision_at"])), iso(sealed)),
    ).fetchone()
    if row:
        sheet = json.loads(row["sheet"])
        if digest(sheet) == row["digest"] and any(
            x["intent"] == intent and x["book"] == book
            for x in sheet["arms"]["candidate"]
        ):
            return
    raise ValueError(
        "promoted order does not match the active plan at the original seal"
    )


def attest_history_order(db, account, op, original):
    """Verify the parent, destination and exact derived allocation against the common decision sheet."""
    intent = op["intent"]
    authorize_ledger(db, account, intent, op["first_book"])
    arm = db.execute(
        "SELECT trial,arm FROM selection_accounts WHERE account=? AND phase='history'",
        (account,),
    ).fetchone()
    if not arm or account != _arm_account(arm["trial"], "history", arm["arm"]):
        raise ValueError("destination is not a registered historical selection arm")
    spec = _trial(db, arm["trial"])["spec"]
    source_op = op | {"intent": original["intent"]}
    parent = db.execute(
        "SELECT 1 FROM paper_replay_members m JOIN paper_replays r ON r.account=m.account "
        "WHERE m.account=? AND m.opportunity_digest=? AND r.fingerprint=?",
        (spec["history_account"], digest(source_op), spec["history_manifest"]),
    ).fetchone()
    if not parent:
        raise ValueError(
            "formal parent opportunity is outside the registered source manifest"
        )
    fixture, cutoff = (
        original["intent"]["match_id"],
        iso(instant(original["intent"]["decision_at"])),
    )
    row = db.execute(
        "SELECT sheet,digest FROM selection_sheets WHERE trial=? AND phase='history' AND fixture=? AND cutoff=?",
        (arm["trial"], fixture, cutoff),
    ).fetchone()
    if not row:
        raise ValueError("missing historical common decision sheet")
    sheet = json.loads(row["sheet"])
    pool = _rows(db, spec["history_account"], fixture, cutoff)
    rebuilt = _sheet(
        spec, "history", fixture, sheet["season"], sheet["round"], cutoff, pool
    )
    if digest(sheet) != row["digest"] or digest(rebuilt) != row["digest"]:
        raise ValueError(
            "historical common decision sheet does not reproduce from its source account"
        )
    plan = {"source_id": op["intent_id"], "intent": intent, "book": original["book"]}
    if plan not in rebuilt["arms"][arm["arm"]] or not any(
        v["source_id"] == op["intent_id"] and v["source_sha"] == digest(original)
        for v in pool
    ):
        raise ValueError(
            "historical order is not the authorized derivative of its formal parent"
        )


def _forward_accounts(root, spec):
    for arm in ("baseline", "candidate"):
        L.open_account(
            root, _arm_account(spec["key"], "forward", arm), "formal", role="main"
        )


def freeze_fixture(data_dir, key, fixture_id):
    with database(data_dir) as db:
        trial = _trial(db, key)
    spec = trial["spec"]
    now = store.now_utc()
    fixture = store.fixture_at(data_dir, fixture_id, at=now, prospective=True)
    cutoff = instant(fixture["kickoff"]) - timedelta(hours=1)
    if (
        _season(fixture["season"]) != spec["season"]
        or not spec["first_round"] <= fixture["round_id"] < spec["first_round"] + 12
        or not cutoff - timedelta(seconds=30) <= instant(now) <= cutoff
        or instant(trial["registered"]) >= cutoff
    ):
        raise ValueError("outside the registered prospective fixture decision")
    store.require_formal(data_dir, at=iso(cutoff))
    _forward_accounts(data_dir, spec)
    with database(data_dir, True) as db:
        if (
            L.account_row(db, spec["source_account"])["mode"] != "formal"
            or L._binding(db, spec["source_account"])["role"] != "main"
        ):
            raise ValueError("source is not an incumbent-model account")
        values = _rows(db, spec["source_account"], fixture_id, iso(cutoff))
        sheet = _sheet(
            spec,
            "forward",
            fixture_id,
            spec["season"],
            fixture["round_id"],
            iso(cutoff),
            values,
        )
        created = store.now_utc()
        if instant(created) > cutoff:
            raise ValueError("common opportunity freeze missed T-60")
        _save_sheet(db, key, "forward", sheet, created)
    for arm, orders in sheet["arms"].items():
        for op in orders:
            L.freeze(
                data_dir, _arm_account(key, "forward", arm), op["intent"], op["book"]
            )
    return {
        "fixture": fixture_id,
        "sha256": digest(sheet),
        "opportunities": len(values),
        "selected": {a: len(x) for a, x in sheet["arms"].items()},
    }


def execute(data_dir, key):
    """Use the canonical delayed books, then copy independently confirmed exchange resolutions."""
    with database(data_dir) as db:
        spec = _trial(db, key)["spec"]
        source = spec["source_account"]
        resolutions = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT payload FROM paper_resolutions WHERE account=? "
                "AND terminal=1 ORDER BY json_extract(payload,'$.resolved_at')",
                (source,),
            )
        ]
    result = {}
    for arm in ("baseline", "candidate", "active"):
        account = _arm_account(key, "forward" if arm != "active" else "active", arm)
        with L.connection(data_dir) as db:
            if not db.execute(
                "SELECT 1 FROM paper_accounts WHERE account=?", (account,)
            ).fetchone():
                continue
        updates = L.recover(data_dir, account)["recovered"]
        with L.connection(data_dir) as db:
            pending = sorted(L._ordered(db, account), key=L._execution_order)
        now = instant(store.now_utc())
        for row in pending:
            item = json.loads(row["result"])
            target = instant(item["execution_target"])
            if now < target:
                break
            with L.connection(data_dir) as db:
                intent = json.loads(row["original"])["intent"]
                book = L.eligible_book(db,data_dir,intent["contract"],target,intent["kickoff"],"second",observed_by=now)
            if not book:
                break
            out = L.evaluate(data_dir, account, row["id"], book)
            updates.append({"intent_id": row["id"], "status": out["status"]})
        for event in resolutions:
            with L.connection(data_dir) as db:
                held = db.execute(
                    "SELECT 1 FROM paper_intents WHERE account=? AND condition_id=? AND token=? "
                    "AND status='filled'",
                    (account, event["condition_id"], event["token_id"]),
                ).fetchone()
            if held:
                L.settle(data_dir, account, event)
        result[arm] = {"updates": updates, "account": account}
    return {"trial": key, "arms": result}


def apply_active(data_dir, fixture_id):
    """Operate the promoted paper filter on new opportunities, using one continuing funded account."""
    now = store.now_utc()
    fx = store.fixture_at(data_dir, fixture_id, at=now, prospective=True)
    cut = instant(fx["kickoff"]) - timedelta(hours=1)
    if not cut - timedelta(seconds=30) <= instant(now) <= cut:
        raise ValueError(
            "active selection must be sealed in the original decision window"
        )
    with database(data_dir) as db:
        pointer = db.execute(
            "SELECT * FROM selection_events WHERE at<=? ORDER BY at DESC,id DESC LIMIT 1",
            (now,),
        ).fetchone()
        if pointer is None:
            raise ValueError(
                "the default incumbent is in use; no promoted selection deployment yet"
            )
        record = _trial(db, _deployed_trial(db, now) or pointer["trial"])
        spec = record["spec"] | {"candidate": json.loads(pointer["recipe"])}
    account = _arm_account(spec["key"], "active", "candidate")
    L.open_account(data_dir, account, "formal", role="main")
    with database(data_dir, True) as db:
        if _active(db, store.now_utc()) != spec["candidate"]:
            raise ValueError("selection pointer changed before freeze")
        values = _rows(db, spec["source_account"], fixture_id, iso(cut))
        sheet = _sheet(
            spec,
            "active",
            fixture_id,
            _season(fx["season"]),
            fx["round_id"],
            iso(cut),
            values,
        )
        sheet["arms"].pop("baseline")
        created = store.now_utc()
        if instant(created) > cut:
            raise ValueError("missed live selection deadline")
        _save_sheet(db, spec["key"], "active", sheet, created)
    for plan in sheet["arms"]["candidate"]:
        L.freeze(data_dir, account, plan["intent"], plan["book"])
    return {
        "trial": spec["key"],
        "account": account,
        "selection": spec["candidate"],
        "opportunities": len(values),
        "selected": len(sheet["arms"]["candidate"]),
        "sheet_sha256": digest(sheet),
    }


def _inputs(root, spec):
    ops = json.loads(L._blob(root, spec["input_blob"]))
    if L.replay_fingerprint("historical", ops) != spec["history_manifest"]:
        raise ValueError("historical replay does not match registration")
    return ops


def history(data_dir, key):
    with database(data_dir) as db:
        spec = _trial(db, key)["spec"]
    ops = _inputs(data_dir, spec)
    scope = strategy_scope.scope(
        data_dir, ops, spec["source_account"], registered=spec.get("history_scope")
    )
    strategy_scope.require_complete(scope)
    indexed = {op["intent"]["intent_id"]: op for op in ops if op.get("intent")}
    fixtures = {}
    for op in indexed.values():
        i = op["intent"]
        # Include only historical opportunities supported by the independently archived M3 replay.
        if L._evidence_tier(data_dir, op, "historical")[0] == "A":
            rnd = store.fixture_at(data_dir, i["match_id"])
            if _season(rnd["season"]) >= spec["season"] or instant(
                i["kickoff"]
            ) != instant(rnd["kickoff"]):
                raise ValueError("historical fixture identity or schedule mismatch")
            fixtures[i["match_id"]] = (rnd, iso(instant(i["decision_at"])))
    selected = {"baseline": [], "candidate": []}
    with database(data_dir, True) as db:
        for fid, (fx, cutoff) in sorted(fixtures.items()):
            rows = _rows(db, spec["history_account"], fid, cutoff)
            if any(r["source_id"] not in indexed for r in rows):
                raise ValueError(
                    "historical opportunity missing from registered source"
                )
            sheet = _sheet(
                spec,
                "history",
                fid,
                _season(fx["season"]),
                fx["round_id"],
                cutoff,
                rows,
            )
            _save_sheet(db, key, "history", sheet, store.now_utc())
            for arm, plans in sheet["arms"].items():
                for p in plans:
                    selected[arm].append(
                        indexed[p["source_id"]] | {"intent": p["intent"]}
                    )
        if strategy_scope.sheet_gaps(db, key, scope):
            raise ValueError(
                "incomplete historical selection sheets for the verified formal parent scope"
            )
        for arm, items in selected.items():
            account = _arm_account(key, "history", arm)
            fingerprint = L.replay_fingerprint("historical", items)
            db.execute(
                "INSERT OR IGNORE INTO selection_history_manifests VALUES (?,?)",
                (account, fingerprint),
            )
            authorize_history_manifest(db, account, fingerprint)
    reports = {}
    for arm, items in selected.items():
        account = _arm_account(key, "history", arm)
        reports[arm] = L.replay(data_dir, items, account=account, source="historical")[
            "counts"
        ]
    return {"trial": key, "historical_A_fixtures": len(fixtures), "arms": reports}


def _close_with_proof(root, db, contract, proof):
    kickoff = instant(proof["actual_kickoff"])
    book = L._closing_book(root, db, contract, proof)
    mark = check_book(book, contract, kickoff, kickoff, "close")
    return mark["mid"], digest([proof["proof_id"], digest(book)])


def _close(root, db, phase, reference, fixture, v, hist, actual=None):
    intent = v["original"]["intent"]
    if intent["match_id"] != fixture:
        raise ValueError(
            "closing fixture differs from the frozen selection opportunity"
        )
    op = hist.get(v.get("source_id"), {}) if phase == "history" else {}
    proof = L._proven_kickoff(
        root,
        intent,
        db=db,
        prospective=phase == "forward",
        claimed=op.get("actual_kickoff"),
        proof_id=op.get("kickoff_proof_id"),
    )
    return _close_with_proof(root, db, intent["contract"], proof)


def _entry_evidence(root, db, phase, reference, fixture, value, history_ops):
    """The same formal forecast and two genuinely available books must support both arms."""
    original = value["original"]
    intent, first = original["intent"], original["book"]
    if intent["match_id"] != fixture:
        raise ValueError("entry does not belong to the frozen fixture")
    cutoff, planned = instant(intent["decision_at"]), instant(intent["kickoff"])
    c = intent["contract"]
    target = cutoff + timedelta(seconds=max(5, int(c["seconds_delay"])))
    if phase == "history":
        op = history_ops.get(value["source_id"]) or {}
        if (
            digest({"intent": op.get("intent"), "book": op.get("first_book")})
            != value["source_sha"]
            or L._evidence_tier(root, op, "historical")[0] != "A"
        ):
            raise ValueError(
                "formal origin or executable historical books no longer verify"
            )
        second, sealed = op["second_book"], instant(op["decision_sealed_at"])
    else:
        row = db.execute(
            "SELECT i.*,a.mode,b.role FROM paper_intents i JOIN paper_accounts a ON a.account=i.account "
            "JOIN paper_account_bindings b ON b.account=i.account WHERE i.account=? AND i.id=?",
            (reference, value["source_id"]),
        ).fetchone()
        if not row or row["fingerprint"] != value["source_sha"]:
            raise ValueError("missing original formal opportunity")
        receipt, source = L._formal_receipt(root, db, row)
        if digest(source) != digest(original):
            raise ValueError("forward source differs from the frozen common pool")
        sealed = instant(receipt["decision_sealed_at"])
    proof = L._proven_kickoff(
        root,
        intent,
        db=db,
        prospective=phase == "forward",
        claimed=op.get("actual_kickoff") if phase == "history" else None,
        proof_id=op.get("kickoff_proof_id") if phase == "history" else None,
    )
    kickoff = min(planned, instant(proof["actual_kickoff"]))
    if phase == "forward":
        L._attest_contract(db, root, c, sealed, planned)
        if (
            L.eligible_book(db, root, c, cutoff, kickoff, "first", observed_by=sealed)
            != first
        ):
            raise ValueError("original first book no longer verifies")
        second = L.eligible_book(db, root, c, target, kickoff, "second")
        if second is None:
            raise ValueError("no original delay-window book")
    return {
        "proof": proof,
        "first": first,
        "second": second,
        "sealed": sealed,
        "kickoff": kickoff,
        "q1": check_book(first, c, cutoff, kickoff, "first"),
        "q2": check_book(second, c, target, kickoff, "second"),
        "identity": digest(
            [
                value["source_sha"],
                iso(sealed),
                digest(first),
                digest(second),
                proof["proof_id"],
            ]
        ),
    }


def _arm_execution(root, db, phase, account, plan, opened, entry):
    """Re-price the booked size and fees using the original held-token depth on both observations."""
    order, book = plan["intent"], plan["book"]
    position = json.loads(opened["result"])
    ident = order["intent_id"]
    original = {"intent": order, "book": book}
    fp = digest(original)
    if (
        digest(json.loads(opened["original"])) != fp
        or opened["fingerprint"] != fp
        or position.get("decision_sha256") != fp
        or position.get("status") != opened["status"]
        or position.get("intent_id") != ident
        or digest(book) != digest(entry["first"])
    ):
        raise ValueError("selection order differs from its frozen plan")
    authorize_ledger(db, account, order, book)
    c = order["contract"]
    cutoff, sealed = instant(order["decision_at"]), instant(position["sealed_at"])
    if phase == "history":
        L._check_history_action(
            root, account, intent_id=ident, execution=True, book=entry["second"]
        )
    elif not entry["sealed"] <= sealed <= cutoff or (
        sealed != entry["sealed"]
        and L.eligible_book(
            db, root, c, cutoff, entry["kickoff"], "first", observed_by=sealed
        )
        != book
    ):
        raise ValueError("arm's first book does not match its own formal seal")
    second = entry["second"]
    if digest(position.get("execution_evidence")) != digest(second) or position.get(
        "attempt_sha256"
    ) != digest({"book": second, "actual_kickoff": None}):
        raise ValueError("arm lacks its matching original execution receipt")
    if position["status"] == "skipped" and position.get("reason") in {
        "remaining depth, price, cash or fixture risk insufficient",
        "insufficient cash",
    }:
        return None
    if (
        position["status"] not in ("filled", "settled")
        or instant(position["filled_at"]) != entry["q2"]["received"]
    ):
        raise ValueError("unverified execution time or status")
    quantity = number(position["gross_shares"])
    if (
        not max(entry["q1"]["minimum"], entry["q2"]["minimum"])
        <= quantity
        <= min(number(order["quantity"]), number(position["locked_quantity"]))
    ):
        raise ValueError(
            "filled size differs from the frozen quantity and market minimum"
        )
    depth = pair_depth(
        entry["q1"]["asks"], entry["q2"]["asks"], number(order["limit_price"])
    )
    priced = price_fill(depth, quantity, c["fee_schedule"])
    if (
        priced["gross"] != quantity
        or priced["net"] <= 0
        or priced["spent"] > number(order["budget"])
        or priced["net"] != number(position["net_shares"])
        or priced["spent"] != number(position["spent"])
        or priced["fees"] != number(position["fee_cash_equivalent"])
        or priced["cost"] != number(position["unit_cost"])
        or digest(priced["levels"]) != digest(position["fill_levels"])
    ):
        raise ValueError(
            "booked cost, shares or fees differ from the original executable depth"
        )
    return priced["spent"], priced["net"]


def _phase(root, trial, phase, history_ops, *, cohort, scope=None):
    spec, key = trial["spec"], trial["spec"]["key"]
    reference = (
        spec["source_account"] if phase == "forward" else spec["history_account"]
    )
    counts = Counter()
    grouped = defaultdict(list)
    causes = Counter()
    trace = []
    paired_cost = {"baseline": ZERO, "candidate": ZERO}
    paired_contribution = paired_cost.copy()
    compounded_risk = True
    with database(root) as db:
        saved = list(
            db.execute(
                "SELECT * FROM selection_sheets WHERE trial=? AND phase=? ORDER BY fixture",
                (key, phase),
            )
        )
        gaps = strategy_scope.sheet_gaps(db, key, scope) if scope is not None else {}
        no_signal = defaultdict(list)
        for fixture, cutoff, parent, fingerprint, signal in (scope or {}).get(
            "parents", []
        ):
            if not signal:
                op = history_ops.get(parent, {})
                no_signal[(fixture, cutoff)].append(
                    {
                        "source_id": parent,
                        "source_sha": fingerprint,
                        "source_no_signal": True,
                        "original": {
                            "intent": op.get("intent", {}),
                            "book": op.get("first_book"),
                        },
                    }
                )
        acct_info = {}
        for arm in ("baseline", "candidate"):
            account = _arm_account(key, phase, arm)
            row = db.execute(
                "SELECT * FROM paper_accounts WHERE account=?", (account,)
            ).fetchone()
            if row is None:
                acct_info[arm] = None
                continue
            if row["mode"] != ("formal" if phase == "forward" else "historical"):
                raise ValueError("incompatible selection account")
            positions = list(
                db.execute(
                    "SELECT original,result,fingerprint,id,status FROM paper_intents WHERE account=?",
                    (account,),
                )
            )
            acct_info[arm] = {p["id"]: p for p in positions}
            cash = number(row["initial"])
            for p in positions:
                out = json.loads(p["result"])
                if out.get("spent") is not None:
                    cash -= number(out["spent"])
                    compounded_risk &= (
                        number(out["spent"]) <= MAX_ORDER
                        and number(out["worst_match_loss_after"]) <= L.MAX_MATCH
                        and number(out["cash_after_fill"]) >= ZERO
                    )
                if out.get("proceeds") is not None:
                    cash += number(out["proceeds"])
            compounded_risk &= cash == number(row["cash"]) and cash >= ZERO
        expected_orders = {a: set() for a in acct_info}
        for recorded in saved:
            sheet = json.loads(recorded["sheet"])
            if digest(sheet) != recorded["digest"]:
                raise ValueError("corrupt shared opportunity sheet")
            for a, plans in sheet["arms"].items():
                expected_orders[a].update(p["intent"]["intent_id"] for p in plans)
            fid, cutoff = sheet["fixture"], sheet["cutoff"]
            values = sheet["pool"]
            counts["frozen_fixtures"] += 1
            if fid in gaps:
                counts["opportunities"] += max(len(values), gaps.pop(fid))
                causes["incomplete_historical_scope"] += 1
                continue
            counts["opportunities"] += len(values) + len(no_signal[(fid, cutoff)])
            pool = _rows(db, reference, fid, cutoff)
            if (
                digest(pool) != digest(values)
                or digest(
                    _sheet(
                        spec, phase, fid, sheet["season"], sheet["round"], cutoff, pool
                    )
                )
                != recorded["digest"]
            ):
                causes["incomplete_reference_pool"] += 1
                continue
            if (
                phase == "forward"
                and not trial["registered"] <= recorded["created"] <= cutoff
            ):
                causes["untimely_common_sheet"] += 1
                continue
            values = values + no_signal[(fid, cutoff)]
            if not values:
                continue
            choices = {
                arm: {p["source_id"]: p for p in plans}
                for arm, plans in sheet["arms"].items()
            }
            total = {"baseline": ZERO, "candidate": ZERO}
            spent_by_arm = total.copy()
            all_paired = True
            traded = False
            fixture_trace = []
            for v in values:
                try:
                    entry = _entry_evidence(
                        root, db, phase, reference, fid, v, history_ops
                    )
                except (ValueError, TypeError, KeyError, OSError):
                    all_paired = False
                    causes["entry_evidence_unverified"] += 1
                    continue
                try:
                    mid, close_hash = _close_with_proof(
                        root, db, v["original"]["intent"]["contract"], entry["proof"]
                    )
                except (ValueError, TypeError, KeyError, OSError):
                    all_paired = False
                    causes["missing_close"] += 1
                    continue
                counts["closing_opportunities"] += 1
                verified_arms = 0
                evidence = [v["source_sha"], entry["identity"], close_hash]
                for arm, mappings in choices.items():
                    plan = mappings.get(v["source_id"])
                    if plan is None:
                        counts[
                            arm
                            + (
                                "_source_no_signal"
                                if v.get("source_no_signal")
                                else "_voluntary_skip"
                            )
                        ] += 1
                        verified_arms += 1
                        counts[arm + "_covered_opportunities"] += 1
                        evidence.append("filtered")
                        continue
                    order = plan["intent"]
                    ident = order["intent_id"]
                    opened = (acct_info[arm] or {}).get(ident)
                    if opened is None or opened["fingerprint"] != digest(
                        {"intent": order, "book": plan["book"]}
                    ):
                        all_paired = False
                        causes["selected_action_missing"] += 1
                        continue
                    try:
                        fill = _arm_execution(
                            root,
                            db,
                            phase,
                            _arm_account(key, phase, arm),
                            plan,
                            opened,
                            entry,
                        )
                    except (ValueError, TypeError, KeyError, OSError):
                        all_paired = False
                        causes["execution_unverified"] += 1
                        continue
                    if fill is None:
                        verified_arms += 1
                        counts[arm + "_covered_opportunities"] += 1
                        evidence.append("unfilled")
                        continue
                    cost, shares = fill
                    total[arm] += shares * mid - cost
                    spent_by_arm[arm] += cost
                    verified_arms += 1
                    counts[arm + "_covered_opportunities"] += 1
                    traded = True
                    evidence.append([ident, fmt(shares), fmt(cost)])
                counts["joint_covered_opportunities"] += int(verified_arms == 2)
                if verified_arms == 2:
                    fixture_trace.append(evidence)
            if max(spent_by_arm.values()) > number(sheet["budget"]):
                compounded_risk = False
            if all_paired:
                counts["paired_fixtures"] += 1
                counts["traded_fixtures"] += int(traded)
                counts["paired_return_opportunities"] += len(fixture_trace)
                trace.append([fid, recorded["digest"], fixture_trace])
                for arm in choices:
                    paired_cost[arm] += spent_by_arm[arm]
                    paired_contribution[arm] += total[arm]
                delta = (total["candidate"] - total["baseline"]) / number(
                    sheet["budget"]
                )
                grouped[(sheet["season"], sheet["round"])].append(float(delta))
        counts["opportunities"] += sum(gaps.values())
        causes["incomplete_historical_scope"] += len(gaps)
        if any(
            set(rows or {}) - expected_orders[arm] for arm, rows in acct_info.items()
        ):
            compounded_risk = False
    coverage = (
        counts["closing_opportunities"] / counts["opportunities"]
        if counts["opportunities"]
        else 0
    )
    joint_coverage = (
        counts["joint_covered_opportunities"] / counts["opportunities"]
        if counts["opportunities"]
        else 0
    )
    interval = _interval(grouped, cohort)
    good = (
        counts["paired_fixtures"] >= 100
        and counts["traded_fixtures"] >= 20
        and joint_coverage >= 0.95
        and compounded_risk
        and interval is not None
        and interval["lower"] > 0
        and not causes["incomplete_reference_pool"]
        and not causes["incomplete_historical_scope"]
    )
    return {
        "counts": dict(counts),
        "close_coverage": coverage,
        "joint_coverage": joint_coverage,
        "risk_passed": bool(compounded_risk),
        "missing": dict(causes),
        "interval": interval,
        "qualified": bool(good),
        "paired_evidence_sha256": digest(trace),
        "paired_spent": {arm: fmt(x) for arm, x in paired_cost.items()},
        "paired_contribution": {arm: fmt(x) for arm, x in paired_contribution.items()},
    }


def _interval(grouped, cohort):
    groups = [v for _, v in sorted(grouped.items()) if v]
    if len(groups) < 2:
        return None
    sums = np.array([sum(v) for v in groups])
    sizes = np.array([len(v) for v in groups])
    draws = np.random.default_rng(861).integers(
        0, len(groups), size=(4000, len(groups))
    )
    means = sums[draws].sum(axis=1) / sizes[draws].sum(axis=1)
    low, high = np.quantile(means, [0.025 / cohort, 1 - 0.025 / cohort])
    return {
        "difference": float(sums.sum() / sizes.sum()),
        "lower": float(low),
        "upper": float(high),
        "rounds": len(groups),
        "resamples": 4000,
        "family_confidence": 0.95,
        "candidates": cohort,
    }


def _window_closed(root, spec, stamp):
    fixtures = _window(root, spec["season"], spec["first_round"])
    rounds = Counter(f["round_id"] for f in fixtures)
    closed = all(
        rounds[i] == 10 for i in range(spec["first_round"], spec["first_round"] + 12)
    )
    closed &= all(
        f.get("received_at")
        and f.get("home_goals") is not None
        and f.get("away_goals") is not None
        and instant(f["received_at"]) <= instant(stamp)
        for f in fixtures
    )
    return bool(closed), fixtures


def score(data_dir, key):
    stamp = store.now_utc()
    with database(data_dir) as db:
        trial = _trial(db, key)
        spec = trial["spec"]
        peers = [
            r[0]
            for r in db.execute(
                "SELECT key FROM selection_trials WHERE start=?", (trial["start"],)
            )
        ]
    inputs = _inputs(data_dir, spec)
    known = {o["intent"]["intent_id"]: o for o in inputs if o.get("intent")}
    scope = strategy_scope.scope(
        data_dir, inputs, spec["source_account"], registered=spec.get("history_scope")
    )
    h = _phase(data_dir, trial, "history", known, cohort=len(peers), scope=scope)
    h["parent_scope"] = {
        "source_account": scope["source_account"],
        "fixture_count": len({r[0] for r in scope["parents"]}),
        "verified_or_registered_parents": len(scope["parents"]),
        "missing_parents": scope["missing_parents"],
    }
    closed, fixtures = _window_closed(data_dir, spec, stamp)
    f = {
        "qualified": False,
        "reason": "forward_window_unfinished",
        "expected_fixtures": len(fixtures),
    }
    if closed:
        for arm in ("baseline", "candidate"):
            account = _arm_account(key, "forward", arm)
            with L.connection(data_dir) as db:
                exists = db.execute(
                    "SELECT 1 FROM paper_accounts WHERE account=?", (account,)
                ).fetchone()
            if exists:
                L.recover(data_dir, account)
        f = _phase(data_dir, trial, "forward", {}, cohort=len(peers))
        with database(data_dir) as db:
            recorded = {
                r["fixture"]: r["cutoff"]
                for r in db.execute(
                    "SELECT fixture,cutoff FROM selection_sheets WHERE trial=? AND phase='forward'",
                    (key,),
                )
            }
        expected = {
            x["fixture_id"]: iso(instant(x["kickoff"]) - timedelta(hours=1))
            for x in fixtures
        }
        missing = sum(recorded.get(fid) != cutoff for fid, cutoff in expected.items())
        early = min(instant(x["kickoff"]) - timedelta(hours=1) for x in fixtures)
        complete = (
            not missing
            and recorded == expected
            and instant(trial["registered"]) < early
        )
        if not complete:
            f["qualified"] = False
        f.update(
            expected_fixtures=len(expected),
            missing_fixture_sheets=missing,
            preregistered_complete_window=complete,
        )
    result = {
        "trial": key,
        "status": trial["status"],
        "qualified": bool(h["qualified"] and f["qualified"]),
        "history": h,
        "forward": f,
        "budget": spec["budget"],
        "baseline": spec["baseline"],
        "candidate": spec["candidate"],
        "preregistered_candidates": len(peers),
        "kind": "paper_selector",
        "real_cash": False,
    }
    store.save_report(data_dir, "strategy", key, result)
    return result


def promote(data_dir, key):
    evidence = score(data_dir, key)
    if not evidence["qualified"]:
        return evidence | {"decision": "remain_shadow"}
    with database(data_dir) as db:
        trial = _trial(db, key)
        peers = [
            r[0]
            for r in db.execute(
                "SELECT key FROM selection_trials WHERE start=?", (trial["start"],)
            )
        ]
    # Same official reveal and deterministic ranking for all candidates, independent of command order.
    outcomes = [evidence if k == key else score(data_dir, k) for k in peers]
    winner = min(
        (r for r in outcomes if r["qualified"]),
        key=lambda r: (-r["forward"]["interval"]["difference"], r["trial"]),
    )
    if winner["trial"] != key:
        return evidence | {
            "decision": "another_registered_candidate_won",
            "winner": winner["trial"],
        }
    with database(data_dir, True) as db:
        record = _trial(db, key)
        spec = record["spec"]
        now = store.now_utc()
        if record["status"] == "promoted":
            return evidence | {"status": "promoted", "decision": "already_promoted"}
        if record["status"] != "shadow" or _active(db, now) != spec["baseline"]:
            raise ValueError(
                "selection baseline changed or this revealed trial was rolled back"
            )
        db.execute(
            "INSERT INTO selection_events(trial,action,at,recipe,fingerprint) VALUES(?,?,?,?,?)",
            (key, "promoted", now, packed(spec["candidate"]), digest(evidence)),
        )
        db.execute(
            "UPDATE selection_trials SET status='promoted',result=? WHERE key=?",
            (packed(evidence), key),
        )
    result = evidence | {"decision": "promoted", "status": "promoted"}
    store.save_report(data_dir, "strategy", key, result)
    return result


def rollback(data_dir, key, reason):
    if not str(reason).strip():
        raise ValueError("a rollback requires a reason")
    with database(data_dir, True) as db:
        trial = _trial(db, key)
        now = store.now_utc()
        if _deployed_trial(db, now) != key or trial["status"] != "promoted":
            raise ValueError(
                "only the current promoted selection trial can be rolled back"
            )
        db.execute(
            "INSERT INTO selection_events(trial,action,at,recipe,fingerprint) VALUES(?,?,?,?,?)",
            (key, "rolled_back", now, packed(trial["spec"]["baseline"]), str(reason)),
        )
        db.execute(
            "UPDATE selection_trials SET status='rolled_back' WHERE key=?", (key,)
        )
        return {
            "trial": key,
            "status": "rolled_back",
            "active": trial["spec"]["baseline"],
            "reason": reason,
        }


def status(data_dir):
    with database(data_dir) as db:
        return {
            "active": _active(db, store.now_utc()),
            "trials": [
                dict(r)
                for r in db.execute(
                    "SELECT key,start,status,registered,digest FROM selection_trials ORDER BY registered,key"
                )
            ],
            "events": [
                dict(r)
                for r in db.execute("SELECT * FROM selection_events ORDER BY at,id")
            ],
        }


def register(subparsers):
    parent = subparsers.add_parser(
        "strategy", help="compare and operate registered paper selection policies"
    )
    subs = parent.add_subparsers(dest="selection_action", required=True)
    p = subs.add_parser("register")
    p.add_argument("key")
    p.add_argument("--selector", type=Path, required=True)
    p.add_argument("--source-account", required=True)
    p.add_argument("--history-account", required=True)
    p.add_argument(
        "--history-source-account",
        help="历史比赛的原始正式主账户，未指定时从完整父单推断",
    )
    p.add_argument("--history-input", type=Path, required=True)
    p.add_argument("--season", required=True)
    p.add_argument("--first-round", type=int, required=True)
    p.add_argument("--budget", required=True)
    p.set_defaults(func=_register_cli)
    for name, fn in (("history", history), ("score", score), ("promote", promote)):
        p = subs.add_parser(name)
        p.add_argument("key")
        p.set_defaults(func=lambda a, f=fn: f(a.data_dir, a.key))
    p = subs.add_parser(
        "execute",
        help="execute both frozen arms and reconcile available exchange settlements",
    )
    p.add_argument("key")
    p.set_defaults(func=lambda a: execute(a.data_dir, a.key))
    p = subs.add_parser(
        "apply", help="apply the active paper selection rule to a new fixture"
    )
    p.add_argument("fixture")
    p.set_defaults(func=lambda a: apply_active(a.data_dir, a.fixture))
    p = subs.add_parser("freeze")
    p.add_argument("key")
    p.add_argument("fixture")
    p.set_defaults(func=lambda a: freeze_fixture(a.data_dir, a.key, a.fixture))
    p = subs.add_parser("rollback")
    p.add_argument("key")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=lambda a: rollback(a.data_dir, a.key, a.reason))
    p = subs.add_parser("status")
    p.set_defaults(func=lambda a: status(a.data_dir))


def _register_cli(a):
    manifest = json.loads(a.history_input.read_text(encoding="utf-8"))
    return register_trial(
        a.data_dir,
        a.key,
        json.loads(a.selector.read_text(encoding="utf-8")),
        source_account=a.source_account,
        history_account=a.history_account,
        history_source_account=a.history_source_account,
        history_inputs=manifest
        if isinstance(manifest, list)
        else manifest["opportunities"],
        season=a.season,
        first_round=a.first_round,
        budget=a.budget,
    )
