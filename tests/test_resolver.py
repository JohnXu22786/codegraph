"""Tests for symbol/module resolution (resolver)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from codegraph.builder import build_index
from codegraph.config import load_config
from codegraph.resolver import resolve_callee, resolve_module
from codegraph.store import IndexStore

from .fixtures import PROJ


class ResolverTest(unittest.TestCase):
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

    def _file_id(self, rel):
        return self.store.file_by_path(rel).id

    def _callee(self, rel, text):
        return resolve_callee(self.store, self._file_id(rel), text)

    def test_same_file(self):
        # price() called from within pricing.py itself
        pid = self._callee("pkg/pricing.py", "price")
        self.assertIsNotNone(pid)
        self.assertEqual(
            self.store.symbol_by_id(pid).qualname, "pkg.pricing.price"
        )

    def test_cross_file_via_import(self):
        cid = self._callee("app.py", "create_cart")
        self.assertIsNotNone(cid)
        self.assertEqual(self.store.symbol_by_id(cid).qualname, "pkg.cart.create_cart")

    def test_attribute_call_via_imported_submodule(self):
        # cart.py: from pkg import pricing -> pricing.discount
        sid = self._callee("pkg/cart.py", "pricing.discount")
        self.assertIsNotNone(sid)
        self.assertEqual(self.store.symbol_by_id(sid).qualname, "pkg.pricing.discount")

    def test_local_variable_heuristic_unique_global(self):
        # app.py: cart.add(...) -> unique global method add
        aid = self._callee("app.py", "cart.add")
        self.assertIsNotNone(aid)
        self.assertEqual(self.store.symbol_by_id(aid).qualname, "pkg.cart.Cart.add")

    def test_constructor_call(self):
        cid = self._callee("pkg/cart.py", "Cart")
        self.assertIsNotNone(cid)
        self.assertEqual(self.store.symbol_by_id(cid).qualname, "pkg.cart.Cart")

    def test_js_relative_import_resolution(self):
        fid = self._callee("web/index.ts", "fmt")
        self.assertIsNotNone(fid)
        self.assertEqual(self.store.symbol_by_id(fid).qualname, "web/util.fmt")

    def test_js_require_with_different_extension(self):
        # app.js requires "./util.js" but the file on disk is util.ts
        fid = self._callee("web/app.js", "fmt")
        self.assertIsNotNone(fid)
        self.assertEqual(self.store.symbol_by_id(fid).qualname, "web/util.fmt")

    def test_go_imported_function(self):
        gid = self._callee("main.go", "helper.Greet")
        self.assertIsNotNone(gid)
        self.assertEqual(self.store.symbol_by_id(gid).qualname, "helper.Greet")

    def test_java_same_package_class(self):
        jid = self._callee("Runner.java", "Calc.sum")
        self.assertIsNotNone(jid)
        self.assertEqual(self.store.symbol_by_id(jid).qualname, "com.demo.Calc.sum")

    def test_rust_mod_and_path_calls(self):
        rid = self._callee("rustx/main.rs", "lib::dist")
        self.assertIsNotNone(rid)
        self.assertEqual(self.store.symbol_by_id(rid).qualname, "rustx/lib.dist")
        oid = self._callee("rustx/main.rs", "lib::Point::origin")
        self.assertIsNotNone(oid)
        self.assertEqual(self.store.symbol_by_id(oid).qualname, "rustx/lib.Point.origin")

    def test_bare_relative_import_resolves_on_first_build(self):
        """from . import x must resolve to pkg/x.py on a fresh index."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "ship.py").write_text(
                "def deliver(item):\n    return item\n", encoding="utf-8")
            (pkg / "cart.py").write_text(
                "from . import ship\n\n"
                "def send(item):\n    return ship.deliver(item)\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                fid = store.file_by_path("pkg/cart.py")["id"]
                self.assertEqual(
                    resolve_module(store, fid, "."), store.file_by_path("pkg/__init__.py")["id"]
                )
                # ship.deliver reaches through the relative import
                sid = resolve_callee(store, fid, "ship.deliver")
                self.assertIsNotNone(sid)
                self.assertEqual(store.symbol_by_id(sid).qualname, "pkg.ship.deliver")
            finally:
                store.close()

    def test_init_file_relative_import_base(self):
        """from . import ship inside pkg/__init__.py must resolve like pkg.ship."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text(
                "from . import ship\n\n"
                "def expose(item):\n    return ship.deliver(item)\n",
                encoding="utf-8",
            )
            (pkg / "ship.py").write_text(
                "def deliver(item):\n    return item\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                fid = store.file_by_path("pkg/__init__.py")["id"]
                sid = resolve_callee(store, fid, "ship.deliver")
                self.assertIsNotNone(sid)
                self.assertEqual(store.symbol_by_id(sid).qualname, "pkg.ship.deliver")
            finally:
                store.close()

    def test_unresolved_external(self):
        self.assertIsNone(self._callee("main.go", "fmt.Println"))
        self.assertIsNone(self._callee("pkg/cart.py", "os.getcwd"))
        self.assertIsNone(self._callee("pkg/pricing.py", "PRICES.get"))

    def test_module_resolution(self):
        fid = self._file_id("app.py")
        self.assertEqual(
            resolve_module(self.store, fid, "pkg.cart"), self._file_id("pkg/cart.py")
        )
        self.assertEqual(
            resolve_module(self.store, fid, "pkg.pricing"), self._file_id("pkg/pricing.py")
        )
        self.assertIsNone(resolve_module(self.store, fid, "os"))
        # js relative import with extension mismatch
        jfid = self._file_id("web/app.js")
        self.assertEqual(
            resolve_module(self.store, jfid, "./util.js"), self._file_id("web/util.ts")
        )
        # rust mod declaration
        rfid = self._file_id("rustx/main.rs")
        self.assertEqual(resolve_module(self.store, rfid, "lib"), self._file_id("rustx/lib.rs"))


if __name__ == "__main__":
    unittest.main()
