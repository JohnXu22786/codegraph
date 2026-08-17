"""Tests for the read-side query API (queries)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from codegraph.builder import build_index
from codegraph.config import load_config
from codegraph.queries import (
    query_callers,
    query_callees,
    query_dependents,
    query_deps,
    query_impact,
    query_search,
    query_stats,
)
from codegraph.store import IndexStore

from .fixtures import PROJ


class QueryTest(unittest.TestCase):
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

    def test_callers_of_price(self):
        rows = query_callers(self.store, "pkg.pricing.price")
        got = sorted(r["qualname"] for r in rows)
        self.assertEqual(got, ["pkg.cart.Cart.total", "pkg.pricing.discount"])

    def test_callers_by_plain_name(self):
        rows = query_callers(self.store, "create_cart")
        got = sorted(r["qualname"] for r in rows)
        self.assertEqual(got, ["app.main"])

    def test_callers_nonexistent(self):
        self.assertEqual(query_callers(self.store, "nope.nope"), [])

    def test_callees_of_main(self):
        rows = query_callees(self.store, "app.main")
        callees = sorted(
            (r["callee"], r["callee_id"] is not None) for r in rows
        )
        self.assertIn(("cart.add", True), callees)
        self.assertIn(("cart.total", True), callees)
        self.assertIn(("create_cart", True), callees)

    def test_callees_of_go_main(self):
        rows = query_callees(self.store, "main.main")
        got = sorted((r["callee"], r["callee_id"] is not None) for r in rows)
        self.assertIn(("fmt.Println", False), got)  # external, unresolved
        self.assertIn(("helper.Greet", True), got)

    def test_callees_of_java_method(self):
        rows = query_callees(self.store, "com.demo.Runner.main")
        got = sorted(r["callee"] for r in rows)
        self.assertIn("Calc.sum", got)
        self.assertIn("System.out.println", got)

    def test_deps_of_cart_module(self):
        rows = query_deps(self.store, "pkg.cart")
        got = sorted((r["module"], r["target_path"] or "") for r in rows)
        self.assertIn(("os", ""), got)
        # "from pkg import pricing" records the package import with its
        # member names; the package __init__ file is the resolved target
        self.assertIn(("pkg", "pkg/__init__.py"), got)

    def test_deps_by_file_path(self):
        rows = query_deps(self.store, "web/index.ts")
        got = sorted((r["module"], r["target_path"]) for r in rows)
        self.assertEqual(got, [("./logger", "web/logger.ts"), ("./util", "web/util.ts")])

    def test_dependents(self):
        rows = query_dependents(self.store, "pkg.pricing")
        got = sorted(r["path"] for r in rows)
        self.assertEqual(got, ["app.py", "pkg/cart.py"])
        rows2 = query_dependents(self.store, "web/util.ts")
        got2 = sorted(r["path"] for r in rows2)
        self.assertEqual(got2, ["web/app.js", "web/index.ts"])

    def test_dependents_limit_caps_the_union(self):
        """The member-import pass must not push results past the limit."""
        rows = query_dependents(self.store, "pkg.pricing", limit=1)
        self.assertEqual([r["path"] for r in rows], ["app.py"])
        rows = query_dependents(self.store, "pkg.pricing", limit=0)
        self.assertEqual(rows, [])

    def test_impact_transitive_callers(self):
        rows = query_impact(self.store, "pkg.cart.Cart", depth=3)
        got = sorted((r["depth"], r["qualname"]) for r in rows)
        self.assertIn((1, "pkg.cart.create_cart"), got)
        self.assertIn((2, "app.main"), got)
        self.assertEqual([d for d, _ in got], [1, 2])

    def test_impact_respects_depth(self):
        rows = query_impact(self.store, "pkg.cart.Cart", depth=1)
        got = sorted(r["qualname"] for r in rows)
        self.assertEqual(got, ["pkg.cart.create_cart"])

    def test_impact_dedupes_cyclic_callers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.py").write_text(
                "def leaf():\n"
                "    return 1\n"
                "def rec(n):\n"
                "    if n:\n"
                "        return rec(n - 1)\n"
                "    return leaf()\n"
                "def top():\n"
                "    return rec(5)\n",
                encoding="utf-8",
            )
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                rows = query_impact(store, "x.rec", depth=3)
                quals = [r["qualname"] for r in rows]
                self.assertEqual(len(quals), len(set(quals)))  # no dupes
                # rec (self-call) and top are both direct callers -> depth 1
                self.assertEqual(
                    {(r["qualname"], r["depth"]) for r in rows},
                    {("x.rec", 1), ("x.top", 1)},
                )
            finally:
                store.close()

    def test_search_symbol_and_doc(self):
        hits = query_search(self.store, "discount")
        qualnames = {h["qualname"] for h in hits}
        self.assertIn("pkg.pricing.discount", qualnames)
        # FTS5 also matches docstring content
        hits2 = query_search(self.store, "shopping cart")
        self.assertTrue(any(h["qualname"] == "pkg.cart.Cart" for h in hits2))

    def test_search_empty_query(self):
        self.assertEqual(query_search(self.store, "  "), [])

    def test_dependents_via_relative_import(self):
        """dependents() must see files importing the module with ``from .``."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text(
                "from . import ship\n", encoding="utf-8")
            (pkg / "ship.py").write_text(
                "def deliver(item):\n    return item\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                rows = query_dependents(store, "pkg.ship")
                got = sorted(r["path"] for r in rows)
                self.assertEqual(got, ["pkg/__init__.py"])
            finally:
                store.close()

    def test_stats(self):
        stats = query_stats(self.store)
        self.assertEqual(stats["files"], 14)
        self.assertGreater(stats["symbols"], 0)
        self.assertGreater(stats["calls"], 0)
        self.assertGreater(stats["imports"], 0)
        self.assertEqual(stats["languages"]["python"], 4)
        self.assertEqual(stats["languages"]["typescript"], 3)
        self.assertIsNotNone(stats["last_indexed"])


if __name__ == "__main__":
    unittest.main()
