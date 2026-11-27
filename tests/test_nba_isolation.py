import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from polymarket861 import cli, store
from polymarket861.nba import storage


def file_digests(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


class NbaStorageIsolation(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.root = self.base / "state"

    def test_read_only_before_and_after_initialization(self):
        self.assertIsNone(storage.open_existing(self.root))
        self.assertFalse(self.root.exists())
        with closing(storage.connect(self.root)) as db:
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertIsInstance(db.execute("SELECT 1").fetchone(), sqlite3.Row)
        before = file_digests(self.root)
        self.assertEqual(set(before), {storage.MARKER, storage.DATABASE})
        with closing(storage.open_existing(self.root)) as db:
            self.assertIsInstance(db.execute("SELECT 1").fetchone(), sqlite3.Row)
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                db.execute("CREATE TABLE unexpected(x)")
        self.assertEqual(file_digests(self.root), before)

    def test_football_cli_and_store_refuse_without_writing(self):
        storage.connect(self.root).close()
        before = file_digests(self.root)
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["--data-dir", str(self.root), "storage", "status"]), 2)
        self.assertIn("football cannot use NBA", err.getvalue())
        for path in (self.root, self.root / "nested"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "NBA"):
                store.connect(path)
        self.assertFalse((self.root / "nested").exists())
        self.assertEqual(file_digests(self.root), before)

    def test_nba_refuses_unmarked_existing_football_database_including_descendants(self):
        self.root.mkdir()
        with closing(sqlite3.connect(self.root / "runtime.sqlite3")) as db:
            db.execute("CREATE TABLE existing_football(x)")
        before = file_digests(self.root)
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["--data-dir", str(self.root), "nba", "status"]), 2)
        self.assertIn("football data directory", err.getvalue())
        with self.assertRaisesRegex(ValueError, "football"):
            storage.connect(self.root / "child")
        self.assertFalse((self.root / "child").exists())
        self.assertEqual(file_digests(self.root), before)
        with closing(store.connect(self.root)) as db:
            self.assertIsNone(db.execute("SELECT x FROM existing_football").fetchone())
        self.assertFalse((self.root / storage.MARKER).exists())

    def test_nba_requires_empty_or_owned_directory(self):
        self.root.mkdir()
        # Deployment directories with account, notification or recommendation
        # files need protection even when they contain no football runtime DB.
        for name in ("account-status.json", "notifications.sqlite3", "feed.json", "other"):
            with self.subTest(name=name):
                receipt = self.root / name
                receipt.write_text(json.dumps({"fixture": name}))
                before = file_digests(self.root)
                for fn in (storage.connect, storage.open_existing):
                    with self.assertRaisesRegex(ValueError, "dedicated"):
                        fn(self.root)
                self.assertEqual(file_digests(self.root), before)
                receipt.unlink()
        (self.root / storage.MARKER).write_text("unknown\n")
        for fn in (storage.connect, store.connect):
            with self.assertRaisesRegex(ValueError, "marker"):
                fn(self.root)

    def test_linked_or_incorrect_database_is_not_adopted(self):
        foreign = self.base / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as db:
            db.execute("CREATE TABLE football_receipts(x)")
        self.root.mkdir()
        (self.root / storage.MARKER).write_text("nba\n")
        target = self.root / storage.DATABASE
        before = foreign.read_bytes()
        target.symlink_to(foreign)
        with self.assertRaisesRegex(ValueError, "linked"):
            storage.connect(self.root)
        target.unlink()
        os.link(foreign, target)
        with self.assertRaisesRegex(ValueError, "linked"):
            storage.connect(self.root)
        target.unlink()
        target.write_bytes(before)
        for fn in (storage.connect, storage.open_existing):
            with self.assertRaisesRegex(ValueError, "database"):
                fn(self.root)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(foreign.read_bytes(), before)

    def test_football_refuses_renamed_or_linked_nba_database(self):
        original = self.base / "basketball"
        storage.connect(original).close()
        before = file_digests(original)
        self.root.mkdir()
        database = original / storage.DATABASE
        runtime = self.root / "runtime.sqlite3"
        for mode in ("copy", "symlink", "hardlink"):
            with self.subTest(mode=mode):
                if mode == "copy":
                    runtime.write_bytes(database.read_bytes())
                elif mode == "symlink":
                    runtime.symlink_to(database)
                else:
                    os.link(database, runtime)
                with self.assertRaisesRegex(ValueError, "NBA database"):
                    store.connect(self.root)
                self.assertEqual(file_digests(original), before)
                runtime.unlink()

    def test_concurrent_ownership_allows_exactly_one_sport(self):
        for attempt in range(8):
            working = self.root / str(attempt)
            start = Barrier(2)

            def claim(fn):
                start.wait(timeout=10)
                try:
                    fn(working).close()
                except ValueError:
                    return False
                return True

            with ThreadPoolExecutor(max_workers=2) as pool:
                nba, football = pool.map(claim, (storage.connect, store.connect))
            self.assertEqual(nba + football, 1)
            owner = "nba" if nba else "football"
            self.assertEqual((working / storage.MARKER).read_text().strip(), owner)
            self.assertEqual((working / storage.DATABASE).exists(), nba)
            self.assertEqual((working / "runtime.sqlite3").is_file(), football)
            self.assertEqual((working / "runtime.sqlite3").is_dir(), nba)

    def test_old_football_sqlite_open_fails_without_touching_initialized_nba(self):
        storage.connect(self.root).close()
        runtime = self.root / "runtime.sqlite3"
        self.assertTrue(runtime.is_dir())
        self.assertEqual(list(runtime.iterdir()), [])
        before = file_digests(self.root)
        with self.assertRaises(sqlite3.OperationalError):
            sqlite3.connect(runtime).close()
        with closing(storage.connect(self.root)) as db:
            self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], storage.APPLICATION_ID)
        self.assertEqual(file_digests(self.root), before)
        for path in (runtime, runtime / "child"):
            with self.assertRaisesRegex(ValueError, "reserved"):
                storage.connect(path)
        self.assertEqual(list(runtime.iterdir()), [])

    def test_unreserved_older_nba_is_upgraded_only_on_writable_open(self):
        storage.connect(self.root).close()
        runtime = self.root / "runtime.sqlite3"
        runtime.rmdir()
        before = file_digests(self.root)
        with closing(storage.open_existing(self.root)) as db:
            self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], storage.APPLICATION_ID)
        self.assertFalse(runtime.exists())
        storage.connect(self.root).close()
        self.assertTrue(runtime.is_dir())
        self.assertEqual(file_digests(self.root), before)

    def test_foreign_legacy_paths_are_preserved(self):
        self.root.mkdir()
        target = self.base / "unrelated"
        target.mkdir()
        runtime = self.root / "runtime.sqlite3"
        marker = self.root / storage.MARKER
        for kind in ("unmarked_dir", "link_to_dir", "dangling_link", "occupied_dir", "football_file"):
            with self.subTest(kind=kind):
                if kind != "unmarked_dir":
                    marker.write_text("nba\n")
                if kind == "link_to_dir":
                    runtime.symlink_to(target, target_is_directory=True)
                elif kind == "dangling_link":
                    runtime.symlink_to(target / "missing")
                elif kind == "football_file":
                    runtime.write_bytes(b"existing foreign file")
                else:
                    runtime.mkdir()
                    if kind == "occupied_dir":
                        (runtime / "existing").write_text("preserve")
                before = file_digests(self.root)
                for fn in (storage.connect, storage.open_existing):
                    with self.assertRaisesRegex(ValueError, "football"):
                        fn(self.root)
                self.assertEqual(file_digests(self.root), before)
                self.assertFalse((self.root / storage.DATABASE).exists())
                if runtime.is_symlink() or runtime.is_file():
                    runtime.unlink()
                else:
                    (runtime / "existing").unlink(missing_ok=True)
                    runtime.rmdir()
                marker.unlink(missing_ok=True)

    def test_legacy_football_wins_race_after_domain_publication(self):
        # Exercise the otherwise tiny window in which the old wheel ignores
        # our just-published domain marker and opens its own database first.
        real_claim = storage._claim
        runtime = self.root / "runtime.sqlite3"

        def concurrent_legacy_open(root, domain):
            published = real_claim(root, domain)
            with closing(sqlite3.connect(runtime)) as db:
                db.execute("CREATE TABLE existing_legacy_football(x)")
            return published

        with patch.object(storage, "_claim", side_effect=concurrent_legacy_open):
            with self.assertRaisesRegex(ValueError, "football"):
                storage.connect(self.root)
        self.assertEqual(list(self.root.iterdir()), [runtime])
        with closing(store.connect(self.root)) as db:
            self.assertIsNone(db.execute("SELECT x FROM existing_legacy_football").fetchone())
        self.assertFalse((self.root / storage.MARKER).exists())

    def test_concurrent_nba_initializers_share_one_valid_database(self):
        start = Barrier(6)

        def initialize(_):
            start.wait(timeout=10)
            with closing(storage.connect(self.root)) as db:
                return db.execute("PRAGMA application_id").fetchone()[0]

        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(list(pool.map(initialize, range(6))), [storage.APPLICATION_ID] * 6)
