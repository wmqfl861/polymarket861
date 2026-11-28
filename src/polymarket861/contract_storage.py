"""One normalized contract body can support many separately timed Gamma receipts."""

import json

from . import dbscope
from .store import canonical


_SCHEMA = """
CREATE TABLE ma_contract_payloads(
 id INTEGER PRIMARY KEY, derived INTEGER NOT NULL, body TEXT NOT NULL, UNIQUE(derived,body));
CREATE TABLE ma_contract_receipts(
 id INTEGER PRIMARY KEY, fixture TEXT NOT NULL, event_slug TEXT NOT NULL, market_slug TEXT NOT NULL,
 received_at TEXT NOT NULL, sha TEXT NOT NULL, condition_id TEXT, payload_id INTEGER NOT NULL
 REFERENCES ma_contract_payloads(id), UNIQUE(event_slug,market_slug,received_at,sha));
CREATE INDEX ma_market_fixture ON ma_contract_receipts(fixture,received_at);
CREATE INDEX ma_contracts_condition_time ON ma_contract_receipts(condition_id,received_at DESC);
CREATE VIEW ma_contracts AS SELECT r.fixture,r.event_slug,r.market_slug,r.received_at,r.sha,
 CASE p.derived WHEN 1 THEN json_replace(p.body,'$.recorded_at',r.received_at,'$.source_sha256',r.sha)
 ELSE p.body END AS payload,r.id AS rowid,r.condition_id
 FROM ma_contract_receipts r JOIN ma_contract_payloads p ON p.id=r.payload_id;
CREATE TRIGGER ma_correct_contract INSTEAD OF UPDATE OF payload ON ma_contracts BEGIN
 INSERT OR IGNORE INTO ma_contract_payloads(derived,body) VALUES(0,NEW.payload);
 UPDATE ma_contract_receipts SET payload_id=(SELECT id FROM ma_contract_payloads WHERE derived=0 AND body=NEW.payload),
 condition_id=CASE WHEN json_valid(NEW.payload) THEN json_extract(NEW.payload,'$.condition_id') END
 WHERE id=OLD.rowid;
END;
"""


def _record(db, keys, condition, derived, body, rowid=None):
    db.execute("INSERT OR IGNORE INTO ma_contract_payloads(derived,body) VALUES(?,?)", (derived, body))
    db.execute(
        "INSERT OR IGNORE INTO ma_contract_receipts(id,fixture,event_slug,market_slug,received_at,sha,condition_id,payload_id) "
        "SELECT ?,?,?,?,?,?,?,id FROM ma_contract_payloads WHERE derived=? AND body=?",
        (rowid, *keys, condition, derived, body),
    )


def payload_body(contract):
    # Null placeholders preserve the canonical field order when SQLite reconstructs the receipt.
    return canonical(dict(contract, recorded_at=None, source_sha256=None))


def record(db, fixture, event_slug, received, sha, contract):
    _record(db, (fixture, event_slug, contract["slug"], received, sha),
            contract.get("condition_id"), 1, payload_body(contract))


def _migrate(db):
    for row in db.execute("SELECT rowid,* FROM ma_contracts_v020"):
        keys, original = tuple(row)[1:6], row["payload"]
        try:
            contract = json.loads(original)
            canonical_form = canonical(contract)
        except (ValueError, TypeError):
            contract = None
        condition = contract.get("condition_id") if isinstance(contract, dict) else None
        if not isinstance(condition, str):
            condition = None
        if (
            isinstance(contract, dict) and contract.get("recorded_at") == keys[3]
            and contract.get("source_sha256") == keys[4] and canonical_form == original
        ):
            body = canonical(dict(contract, recorded_at=None, source_sha256=None))
            _record(db, keys, condition, 1, body, row[0])
        else:
            _record(db, keys, condition, 0, original, row[0])


def prepare(db):
    def kind():
        row = db.execute("SELECT type FROM sqlite_master WHERE name='ma_contracts'").fetchone()
        return row[0] if row else None

    if kind() == "view":
        return
    # Old receipts move atomically, including their row identifiers and malformed legacy bodies.
    own = not db.in_transaction
    db.execute("BEGIN IMMEDIATE" if own else "SAVEPOINT install_contract_storage")
    try:
        existing = kind()
        if existing != "view":
            if existing == "table":
                db.execute("ALTER TABLE ma_contracts RENAME TO ma_contracts_v020")
                db.execute("DROP INDEX IF EXISTS ma_market_fixture")
                db.execute("DROP INDEX IF EXISTS ma_contracts_condition_time")
            dbscope.prepare(db, "market-contract-storage", _SCHEMA)
            if existing == "table":
                _migrate(db)
                db.execute("DROP TABLE ma_contracts_v020")
        if own:
            db.commit()
        else:
            db.execute("RELEASE install_contract_storage")
    except BaseException:
        if own:
            db.rollback()
        else:
            db.execute("ROLLBACK TO install_contract_storage")
            db.execute("RELEASE install_contract_storage")
        raise
