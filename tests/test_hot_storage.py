import json
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from polymarket861 import contract_storage, dbscope, market_archive, operations, store


class HotStorageTests(unittest.TestCase):
    def test_large_original_remains_verifiable_across_encodings_and_backup_formats(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "state"
            raw = b'{"data":"' + b"repeatable evidence " * 4000 + b'"}'
            ref = store.archive_bytes(root, "public", raw, observed_at="2026-11-25T10:00:00Z")
            sha = ref["sha256"]
            path = store.blob_path(root, sha)
            self.assertEqual(ref["encoding"], "gzip")
            self.assertLess(ref["stored_size"], len(raw) // 8)
            self.assertEqual(store.read_blob(root, sha), raw)
            self.assertEqual(dbscope.verified_bytes(root, path, sha, "bad hash"), raw)
            store.archive_bytes(root, "public", raw, observed_at="2026-11-26T00:00:00Z")
            info = operations.storage_status(root)["stores"]["runtime"]
            self.assertEqual(info["first_received_utc_day"], {
                "2026-11-25": {"originals": 1, "logical_bytes": len(raw), "stored_bytes": path.stat().st_size}
            })
            for compressed in (False, True):
                dest = base / f"backup-{compressed}"
                operations.backup_state(root, dest, compress=compressed)
                restored = base / f"restored-{compressed}"
                operations.restore_state(dest, restored)
                self.assertEqual(store.read_blob(restored, sha), raw)
            with closing(store.connect(root)) as db, dbscope.bind(root, db) as batch:
                dbscope.verified_bytes(root, path, sha, "bad hash")
                path.write_bytes(path.read_bytes()[:-8])
                with self.assertRaises(dbscope.EvidenceChanged):
                    dbscope.recheck_originals(batch)
            with self.assertRaises(ValueError):
                store.archive_bytes(root, "retry", raw)

    def test_shared_terms_rehydrate_exact_receipt_and_isolate_revisions(self):
        with TemporaryDirectory() as root, closing(market_archive.database(root)) as db, db:
            first = {"slug": "m", "condition_id": "c", "text": "Abandonment & 比分", "active": True}
            revised = dict(first, active=False)
            for i, terms in enumerate([first] * 100 + [revised, first]):
                contract_storage.record(db, "fix", "event", str(i), f"sha-{i}", terms)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ma_contract_payloads").fetchone()[0], 2)
            rows = list(db.execute("SELECT received_at,sha,payload FROM ma_contracts ORDER BY rowid"))
            self.assertEqual(len(rows), 102)
            for idx, (ts, sha, payload) in enumerate(rows):
                self.assertEqual(payload, store.canonical(dict(revised if idx == 100 else first, recorded_at=ts, source_sha256=sha)))
            db.execute("UPDATE ma_contracts SET payload=? WHERE received_at='0'", (json.dumps({"broken": True}),))
            self.assertIn("broken", db.execute("SELECT payload FROM ma_contracts WHERE received_at='0'").fetchone()[0])
            self.assertEqual(db.execute("SELECT payload FROM ma_contracts WHERE received_at='1'").fetchone()[0], rows[1][2])

    def test_migrates_existing_receipts_without_reserializing_noncanonical_data(self):
        with TemporaryDirectory() as root:
            with closing(store.connect(root)) as db, db:
                db.execute("CREATE TABLE ma_contracts(fixture,event_slug,market_slug,received_at,sha,payload)")
                for ts in ("t1", "t2"):
                    payload = store.canonical(dict(slug="m", condition_id="c", recorded_at=ts, source_sha256="s"))
                    db.execute("INSERT INTO ma_contracts VALUES('f','e','m',?,'s',?)", (ts, payload))
                db.execute("INSERT INTO ma_contracts VALUES('f','e','bad','3','s','broken')")
                original = db.execute("SELECT rowid,payload FROM ma_contracts ORDER BY rowid").fetchall()
            with closing(market_archive.database(root)) as db:
                self.assertEqual(original, db.execute("SELECT rowid,payload FROM ma_contracts ORDER BY rowid").fetchall())
                self.assertEqual(db.execute("SELECT count(*) FROM ma_contract_payloads").fetchone()[0], 2)
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
