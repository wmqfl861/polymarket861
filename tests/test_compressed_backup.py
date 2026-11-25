import json
import tempfile
import unittest
from pathlib import Path

from polymarket861 import operations, store


class CompressedBackups(unittest.TestCase):
    def test_format_three_restores_original_blobs_and_rejects_truncation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = b"a repeated exchange response" * 2500
            ref = store.archive_bytes(root / "state", "test", raw)
            name = store.blob_path(Path("."), ref["sha256"])
            for prefix in (root / "state", root / "state/season-catalog"):
                store.archive_bytes(prefix, "test", raw)
            backup = root / "packed"
            operations.backup_state(root / "state", backup, compress=True)
            self.assertEqual(
                json.loads((backup / "manifest.json").read_text())["format"], 3
            )
            packed = (backup / name).with_suffix(".gz")
            self.assertLess(packed.stat().st_size, len(raw) // 5)
            operations.restore_state(backup, root / "restored")
            self.assertEqual((root / "restored" / name).read_bytes(), raw)
            self.assertEqual(
                (root / "restored/season-catalog" / name).read_bytes(), raw
            )
            packed.write_bytes(packed.read_bytes()[:-7])
            with self.assertRaises(ValueError):
                operations.restore_state(backup, root / "invalid")
            self.assertFalse((root / "invalid").exists())
