"""Tests for DOT/JSON export."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from codegraph.builder import build_index
from codegraph.config import load_config
from codegraph.exporter import export_dot, export_json
from codegraph.store import IndexStore

from .fixtures import PROJ


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        shutil.copytree(PROJ, self.root)
        cfg = load_config(root=str(self.root))
        cfg.engine = "quick"
        build_index(cfg)
        self.store = IndexStore(str(cfg.db_path))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_dot_structure(self):
        dot = export_dot(self.store)
        self.assertTrue(dot.startswith("digraph"))
        self.assertIn("->", dot)
        # resolved call edge: app.main -> create_cart
        self.assertIn("create_cart", dot)
        self.assertIn('label="app.main"', dot)

    def test_dot_is_parseable_shape(self):
        dot = export_dot(self.store)
        # each statement ends with ;
        statements = [s for s in dot.splitlines() if s.strip().endswith(";")]
        self.assertGreater(len(statements), 10)

    def test_dot_escapes_newline_in_file_path_labels(self):
        path = "src/line\nbreak.py"
        file_path = self.root / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("\n", encoding="utf-8")

        row = self.store.conn.execute(
            "SELECT id FROM files ORDER BY path LIMIT 1"
        ).fetchone()
        self.store.conn.execute(
            "UPDATE files SET path = ? WHERE id = ?", (path, row["id"])
        )

        dot = export_dot(self.store)
        escaped_path = r"src/line\nbreak.py"
        self.assertIn(f'  f{row["id"]} [label="{escaped_path}"];', dot)
        self.assertIn(
            f'  subgraph cluster_{row["id"]} {{ label="{escaped_path}";', dot
        )

    def test_dot_import_file_nodes_are_declared_and_labeled(self):
        dot = export_dot(self.store)
        file_paths = {
            row["id"]: row["path"]
            for row in self.store.conn.execute("SELECT id, path FROM files")
        }
        imports = self.store.conn.execute(
            "SELECT file_id, target_id FROM imports WHERE target_id IS NOT NULL"
        ).fetchall()
        self.assertGreater(len(imports), 0)

        for row in imports:
            for file_id in (row["file_id"], row["target_id"]):
                self.assertIn(
                    f'  f{file_id} [label="{file_paths[file_id]}"];',
                    dot,
                )

    def test_dot_unresolved_call_target_has_a_distinct_node_id(self):
        call = self.store.conn.execute(
            "SELECT id, caller_id FROM calls WHERE callee_id IS NULL "
            "AND caller_id IS NOT NULL LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(call)

        self.store.conn.execute(
            "UPDATE calls SET callee = 'n1' WHERE id = ?", (call["id"],)
        )
        dot = export_dot(self.store)
        target = f'u{call["id"]}'

        # The fixture's first symbol is the real generated n1 node.
        self.assertIn("n1 [", dot)
        self.assertIn(f'  {target} [label="n1"];', dot)
        self.assertIn(
            f'  n{call["caller_id"]} -> {target} [style=dashed];', dot
        )
        self.assertNotIn(f'-> "n1" [style=dashed];', dot)

    def test_json_structure(self):
        data = export_json(self.store)
        self.assertEqual(
            sorted(data.keys()), ["calls", "files", "imports", "meta", "symbols"]
        )
        self.assertEqual(len(data["files"]), 14)
        self.assertGreater(len(data["symbols"]), 0)
        self.assertGreater(len(data["calls"]), 0)
        self.assertGreater(len(data["imports"]), 0)
        sample = data["calls"][0]
        self.assertEqual(sorted(sample.keys()), ["callee", "callee_id", "caller", "file", "line"])
        # round-trips as JSON
        json.dumps(data)

    def test_json_imports_include_member_names(self):
        data = export_json(self.store)
        member_import = next(
            item for item in data["imports"] if item["module"] == "pkg"
        )
        self.assertEqual(member_import["names"], ["pricing"])

    def test_json_symbol_fields(self):
        data = export_json(self.store)
        sym = next(s for s in data["symbols"] if s["qualname"] == "pkg.pricing.price")
        self.assertEqual(
            sorted(sym.keys()),
            ["end_line", "file", "kind", "name", "parent", "qualname", "signature", "start_line"],
        )

    def test_json_export_uses_one_read_snapshot(self):
        first_file = self.store.conn.execute(
            "SELECT path, lines FROM files ORDER BY path LIMIT 1"
        ).fetchone()
        first_symbol = self.store.conn.execute(
            "SELECT s.id, s.qualname FROM symbols s JOIN files f "
            "ON f.id = s.file_id WHERE f.path = ? ORDER BY s.id LIMIT 1",
            (first_file["path"],),
        ).fetchone()
        self.assertIsNotNone(first_symbol)

        writer = IndexStore(str(self.store.db_path))
        connection = self.store.conn
        triggered = False

        class InterleavingConnection:
            def __getattr__(self, name):
                return getattr(connection, name)

            def execute(self, sql, *args):
                nonlocal triggered
                if sql.startswith("SELECT path, lang, module, lines FROM files") \
                        and not triggered:
                    rows = connection.execute(sql, *args).fetchall()
                    triggered = True
                    with writer.transaction():
                        writer.conn.execute(
                            "UPDATE files SET lines = lines + 100 WHERE path = ?",
                            (first_file["path"],),
                        )
                        writer.conn.execute(
                            "UPDATE symbols SET qualname = ?, name = ? WHERE id = ?",
                            ("interleaved_new_symbol", "interleaved_new_symbol",
                             first_symbol["id"]),
                        )
                    return iter(rows)
                return connection.execute(sql, *args)

        self.store.conn = InterleavingConnection()
        try:
            data = export_json(self.store)
        finally:
            self.store.conn = connection
            writer.close()

        exported_file = next(
            row for row in data["files"] if row["path"] == first_file["path"]
        )
        self.assertEqual(exported_file["lines"], first_file["lines"])
        self.assertIn(
            first_symbol["qualname"],
            {row["qualname"] for row in data["symbols"]},
        )
        self.assertNotIn(
            "interleaved_new_symbol",
            {row["qualname"] for row in data["symbols"]},
        )

    def test_dot_export_uses_one_read_snapshot(self):
        first_symbol = self.store.conn.execute(
            "SELECT s.id, s.file_id, s.qualname, f.path FROM symbols s "
            "JOIN files f ON f.id = s.file_id ORDER BY s.id LIMIT 1"
        ).fetchone()
        new_path = "interleaved_export_path.py"
        writer = IndexStore(str(self.store.db_path))
        connection = self.store.conn
        triggered = False

        class InterleavingConnection:
            def __getattr__(self, name):
                return getattr(connection, name)

            def execute(self, sql, *args):
                nonlocal triggered
                if sql.startswith(
                    "SELECT id, file_id, qualname, kind FROM symbols"
                ) and not triggered:
                    rows = connection.execute(sql, *args).fetchall()
                    triggered = True
                    with writer.transaction():
                        writer.conn.execute(
                            "UPDATE files SET path = ? WHERE id = ?",
                            (new_path, first_symbol["file_id"]),
                        )
                        writer.conn.execute(
                            "UPDATE symbols SET qualname = ?, name = ? WHERE id = ?",
                            ("interleaved_new_symbol", "interleaved_new_symbol",
                             first_symbol["id"]),
                        )
                    return iter(rows)
                return connection.execute(sql, *args)

        self.store.conn = InterleavingConnection()
        try:
            dot = export_dot(self.store)
        finally:
            self.store.conn = connection
            writer.close()

        self.assertIn(first_symbol["path"], dot)
        self.assertNotIn(new_path, dot)
        self.assertIn(first_symbol["qualname"], dot)
        self.assertNotIn("interleaved_new_symbol", dot)


if __name__ == "__main__":
    unittest.main()
