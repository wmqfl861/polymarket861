import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from polymarket861 import league_openfootball_refresh as source, league_registry, store


class CurrentSeasonReceipts(unittest.TestCase):
    def test_reversion_keeps_first_observation_clock_and_immutable_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stamps = [f"2026-11-27T{h:02d}:00:00.000000Z" for h in (1, 3, 5)]
            for i, stamp in enumerate(stamps):
                content = (b"old", b"new", b"old")[i]
                data = (str(i) * 40, league_registry.git_blob(content), content)
                with patch.object(source, "_download", return_value=data) as fetch, patch.object(store, "now_utc", return_value=stamp):
                    for _ in range(2):
                        self.assertEqual(source.refresh(root, "sea", "2026-27")["state"], "ok")
                    fetch.assert_called_once()
            rows = list(source.snapshots(root, "sea", "2026-27", stamps[-1]))
            self.assertEqual([r[0] for r in rows], stamps)
            self.assertEqual([r[3].read_bytes() for r in rows], [b"old", b"new", b"old"])
            self.assertEqual(list(source.snapshots(root, "sea", "2026-27", stamps[0])), rows[:1])
            rows[0][3].write_bytes(b"altered")
            with self.assertRaisesRegex(ValueError, "verification"):
                list(source.snapshots(root, "sea", "2026-27", stamps[0]))

    def test_reject_git_blob_mismatch_before_publication(self):
        file = {"type": "file", "path": "2026-27/de.1.json", "encoding": "base64", "size": 2,
                "content": base64.b64encode(b"{}").decode(), "sha": "0" * 40}
        with tempfile.TemporaryDirectory() as td, patch.object(source.market_provider, "get", side_effect=[
                Mock(body=b'[{"sha":"' + b"1" * 40 + b'"}]'), Mock(body=json.dumps(file).encode())]) as get:
            self.assertEqual(source.refresh(td, "bun", "2026-27")["state"], "error")
            self.assertEqual(source.refresh(td, "bun", "2026-27")["state"], "error")
            self.assertEqual(get.call_count, 2)
            self.assertEqual(list(source.snapshots(td, "bun", "2026-27", store.now_utc())), [])
