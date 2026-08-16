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

    def test_json_symbol_fields(self):
        data = export_json(self.store)
        sym = next(s for s in data["symbols"] if s["qualname"] == "pkg.pricing.price")
        self.assertEqual(
            sorted(sym.keys()),
            ["end_line", "file", "kind", "name", "parent", "qualname", "signature", "start_line"],
        )


if __name__ == "__main__":
    unittest.main()
