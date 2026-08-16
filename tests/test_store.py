"""Tests for the SQLite storage layer."""

import tempfile
import unittest
from pathlib import Path

from codegraph.models import FileScan, ImportRec, SymbolRec
from codegraph.store import IndexStore


def _sample_scan(path):
    """Build a small FileScan for ``replace_file_payload`` round-trips."""
    module = path.rsplit("/", 1)[-1].split(".")[0] if "/" in path else path.split(".")[0]
    return FileScan(
        lang="python",
        symbols=[
            SymbolRec("function", "hello", f"{module}.hello", "", 1, 3, "name", "Say hello."),
        ],
        calls=[],
        imports=[ImportRec("os", [], "module", 5)],
    )


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.sqlite"
        self.store = IndexStore(str(self.db))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_meta_roundtrip(self):
        self.assertIsNone(self.store.get_meta("last_indexed"))
        self.store.set_meta("last_indexed", "2026-01-01T00:00:00")
        self.assertEqual(self.store.get_meta("last_indexed"), "2026-01-01T00:00:00")

    def test_upsert_and_replace_file(self):
        fid = self.store.upsert_file("a.py", "python", 10, "digest1", 3)
        self.assertEqual(fid, 1)
        self.assertEqual(self.store.upsert_file("a.py", "python", 20, "digest2", 5), 1)
        self.assertEqual(self.store.file_by_path("a.py").digest, "digest2")

        with self.store.transaction():
            self.store.replace_file_payload(1, _sample_scan("pkg/a.py"))
        syms = self.store.symbols_for_file(1)
        self.assertEqual(len(syms), 1)
        self.assertEqual(syms[0].qualname, "a.hello")
        imports = self.store.imports_for_file(1)
        self.assertEqual(imports[0].module, "os")

        # replacing again must not duplicate rows
        with self.store.transaction():
            self.store.replace_file_payload(1, _sample_scan("pkg/a.py"))
        self.assertEqual(len(self.store.symbols_for_file(1)), 1)
        self.assertEqual(len(self.store.imports_for_file(1)), 1)
        self.assertEqual(self.store.count_rows("symbols"), 1)

    def test_remove_file(self):
        fid = self.store.upsert_file("a.py", "python", 10, "d", 3)
        with self.store.transaction():
            self.store.replace_file_payload(fid, _sample_scan("a.py"))
        self.assertIsNotNone(self.store.file_by_path("a.py"))
        self.store.remove_file("a.py")
        self.assertIsNone(self.store.file_by_path("a.py"))
        self.assertEqual(self.store.count_rows("symbols"), 0)
        self.assertEqual(self.store.count_rows("files"), 0)

    def test_fts_rows_follow_symbols(self):
        fid = self.store.upsert_file("a.py", "python", 10, "d", 3)
        with self.store.transaction():
            self.store.replace_file_payload(fid, _sample_scan("a.py"))
        hits = self.store.search("hello")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["qualname"], "a.hello")
        self.store.remove_file("a.py")
        self.assertEqual(self.store.search("hello"), [])

    def test_incremental_helpers(self):
        self.store.upsert_file("a.py", "python", 10, "d1", 3)
        self.store.upsert_file("b.py", "python", 5, "d2", 1)
        self.assertEqual(self.store.known_digests(), {"a.py": "d1", "b.py": "d2"})
        self.assertEqual(self.store.all_file_paths(), {"a.py", "b.py"})

    def test_second_open_reuses_data(self):
        self.store.upsert_file("a.py", "python", 10, "d1", 3)
        self.store.close()
        self.store = IndexStore(str(self.db))
        self.assertEqual(self.store.file_by_path("a.py").digest, "d1")


if __name__ == "__main__":
    unittest.main()
