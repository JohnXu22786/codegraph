"""Tests for the SQLite storage layer."""

import sqlite3
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

    def test_remove_missing_file_does_not_take_write_lock(self):
        blocker = sqlite3.connect(str(self.db), timeout=0)
        self.store.conn.execute("PRAGMA busy_timeout = 0")
        blocker.execute("BEGIN IMMEDIATE")
        try:
            self.assertEqual(
                self.store.remove_file("missing.py"),
                {"file_id": None, "symbol_names": set(),
                 "call_ids": set(), "import_ids": set()},
            )
            self.assertFalse(self.store.conn.in_transaction)
        finally:
            blocker.rollback()
            blocker.close()

    def test_standalone_remove_file_rolls_back_on_failure(self):
        target_id = self.store.upsert_file("a.py", "python", 10, "d1", 3)
        other_id = self.store.upsert_file("b.py", "python", 12, "d2", 4)
        target_symbol = self.store.conn.execute(
            "INSERT INTO symbols(file_id, kind, name, qualname, start_line, end_line) "
            "VALUES(?, 'function', 'target', 'a.target', 1, 2)",
            (target_id,),
        ).lastrowid
        other_symbol = self.store.conn.execute(
            "INSERT INTO symbols(file_id, kind, name, qualname, start_line, end_line) "
            "VALUES(?, 'function', 'other', 'b.other', 1, 2)",
            (other_id,),
        ).lastrowid
        self.store.conn.execute(
            "INSERT INTO sym_fts(rowid, qualname, name, doc, signature) "
            "VALUES(?, 'a.target', 'target', '', '')",
            (target_symbol,),
        )
        self.store.conn.execute(
            "INSERT INTO calls(caller_id, caller_name, callee, callee_id, file_id, line) "
            "VALUES(?, 'target', 'other', ?, ?, 3)",
            (target_symbol, other_symbol, target_id),
        )
        self.store.conn.execute(
            "INSERT INTO calls(caller_id, caller_name, callee, callee_id, file_id, line) "
            "VALUES(?, 'other', 'target', ?, ?, 4)",
            (other_symbol, target_symbol, other_id),
        )
        self.store.conn.execute(
            "INSERT INTO imports(file_id, module, target_id, line) VALUES(?, 'b', ?, 5)",
            (target_id, other_id),
        )
        self.store.conn.execute(
            "INSERT INTO imports(file_id, module, target_id, line) VALUES(?, 'a', ?, 6)",
            (other_id, target_id),
        )
        tables = ("files", "symbols", "calls", "imports")
        before = {
            table: [tuple(row) for row in self.store.conn.execute(
                f"SELECT * FROM {table} ORDER BY id"
            )]
            for table in tables
        }
        before["sym_fts"] = [tuple(row) for row in self.store.conn.execute(
            "SELECT rowid, qualname, name, doc, signature FROM sym_fts ORDER BY rowid"
        )]
        self.store.conn.execute(
            "CREATE TRIGGER fail_file_removal BEFORE DELETE ON files "
            "WHEN OLD.path = 'a.py' BEGIN "
            "SELECT RAISE(ABORT, 'forced removal failure'); END"
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced removal failure"):
            self.store.remove_file("a.py")

        after = {
            table: [tuple(row) for row in self.store.conn.execute(
                f"SELECT * FROM {table} ORDER BY id"
            )]
            for table in tables
        }
        after["sym_fts"] = [tuple(row) for row in self.store.conn.execute(
            "SELECT rowid, qualname, name, doc, signature FROM sym_fts ORDER BY rowid"
        )]
        self.assertEqual(after, before)

    def test_standalone_remove_file_rolls_back_on_commit_failure(self):
        fid = self.store.upsert_file("a.py", "python", 10, "d", 3)
        with self.store.transaction():
            self.store.replace_file_payload(fid, _sample_scan("a.py"))
        self.store.conn.execute(
            "CREATE TABLE removal_guard_parent (id INTEGER PRIMARY KEY)"
        )
        self.store.conn.execute(
            "CREATE TABLE removal_guard (parent_id INTEGER REFERENCES "
            "removal_guard_parent(id) DEFERRABLE INITIALLY DEFERRED)"
        )
        self.store.conn.execute(
            "CREATE TRIGGER fail_remove_at_commit AFTER DELETE ON files "
            "WHEN OLD.path = 'a.py' BEGIN "
            "INSERT INTO removal_guard(parent_id) VALUES (999); END"
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
            self.store.remove_file("a.py")

        self.assertFalse(self.store.conn.in_transaction)
        self.assertIsNotNone(self.store.file_by_path("a.py"))
        self.assertEqual(self.store.count_rows("symbols"), 1)
        self.assertEqual(self.store.count_rows("removal_guard"), 0)
        self.assertEqual(len(self.store.search("hello")), 1)

    def test_fts_rows_follow_symbols(self):
        fid = self.store.upsert_file("a.py", "python", 10, "d", 3)
        with self.store.transaction():
            self.store.replace_file_payload(fid, _sample_scan("a.py"))
        hits = self.store.search("hello")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["qualname"], "a.hello")
        self.store.remove_file("a.py")
        self.assertEqual(self.store.search("hello"), [])

    def test_invalid_fts_query_matches_signature(self):
        fid = self.store.upsert_file("a.py", "python", 10, "d", 3)
        scan = FileScan(
            lang="python",
            symbols=[
                SymbolRec(
                    "function", "render", "a.render", "", 1, 3,
                    '(query: "needle")', "",
                ),
            ],
            calls=[],
            imports=[],
        )
        with self.store.transaction():
            self.store.replace_file_payload(fid, scan)

        hits = self.store.search('"needle')

        self.assertEqual([hit["qualname"] for hit in hits], ["a.render"])

    def test_symbols_by_name_rejects_negative_limit(self):
        fid = self.store.upsert_file("a.py", "python", 10, "d", 3)
        with self.store.transaction():
            self.store.replace_file_payload(fid, _sample_scan("a.py"))

        with self.assertRaisesRegex(ValueError, "limit must be non-negative"):
            self.store.symbols_by_name("hello", limit=-1)

    def test_search_rejects_negative_limit(self):
        with self.assertRaisesRegex(ValueError, "limit must be non-negative"):
            self.store.search("hello", limit=-1)

    def test_incremental_helpers(self):
        self.store.upsert_file("a.py", "python", 10, "d1", 3)
        self.store.upsert_file("b.py", "python", 5, "d2", 1)
        self.assertEqual(self.store.known_digests(), {"a.py": "d1", "b.py": "d2"})
        self.assertEqual(self.store.all_file_paths(), {"a.py", "b.py"})

    def test_clear_references_handles_many_symbol_ids(self):
        fid = self.store.upsert_file("large.py", "python", 10, "d", 1)
        # Exceed half of SQLite's standard 32,766-variable limit even when
        # sqlite3.Connection.setlimit is unavailable (Python 3.10).
        symbol_count = 20_000
        previous_limit = None
        if (hasattr(self.store.conn, "setlimit") and
                hasattr(sqlite3, "SQLITE_LIMIT_VARIABLE_NUMBER")):
            previous_limit = self.store.conn.setlimit(
                sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 10
            )
        try:
            self.store.conn.executemany(
                "INSERT INTO symbols(file_id, kind, name, qualname, parent, "
                "start_line, end_line, signature, doc) VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (fid, "function", f"name_{i}", f"large.name_{i}", "", i, i, "", "")
                    for i in range(symbol_count)
                ],
            )
            symbol_ids = [
                row["id"]
                for row in self.store.conn.execute(
                    "SELECT id FROM symbols WHERE file_id = ?", (fid,)
                )
            ]
            self.store.conn.executemany(
                "INSERT INTO calls(caller_id, caller_name, callee, callee_id, "
                "file_id, line) VALUES(?,?,?,?,?,?)",
                [
                    (sid, f"name_{i}", f"target_{i}", sid, fid, i)
                    for i, sid in enumerate(symbol_ids)
                ],
            )

            impact = self.store.clear_references_to_file(fid)
        finally:
            if previous_limit is not None:
                self.store.conn.setlimit(
                    sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous_limit
                )

        self.assertEqual(impact["call_ids"], set(range(1, symbol_count + 1)))
        calls = self.store.conn.execute(
            "SELECT caller_id, callee_id FROM calls"
        ).fetchall()
        self.assertTrue(all(row["caller_id"] is None for row in calls))
        self.assertTrue(all(row["callee_id"] is None for row in calls))

    def test_second_open_reuses_data(self):
        self.store.upsert_file("a.py", "python", 10, "d1", 3)
        self.store.close()
        self.store = IndexStore(str(self.db))
        self.assertEqual(self.store.file_by_path("a.py").digest, "d1")

    def test_sqlite_pragmas_allow_concurrent_access(self):
        self.assertEqual(
            self.store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower(),
            "wal",
        )
        self.assertEqual(
            self.store.conn.execute("PRAGMA synchronous").fetchone()[0],
            1,  # NORMAL
        )
        self.assertGreaterEqual(
            self.store.conn.execute("PRAGMA busy_timeout").fetchone()[0],
            5000,
        )


if __name__ == "__main__":
    unittest.main()
