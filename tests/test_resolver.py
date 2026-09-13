"""Tests for symbol/module resolution (resolver)."""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codegraph.builder import build_index
from codegraph.config import load_config
from codegraph.resolver import resolve_all, resolve_callee, resolve_module
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

    def test_absolute_python_import_falls_back_to_source_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            (src / "util.py").write_text(
                "def target():\n    return 'src'\n", encoding="utf-8")
            (src / "app.py").write_text(
                "import util\n\n"
                "def invoke():\n    return util.target()\n",
                encoding="utf-8",
            )

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                fid = store.file_by_path("src/app.py")["id"]
                self.assertEqual(
                    resolve_module(store, fid, "util"),
                    store.file_by_path("src/util.py")["id"],
                )
                call = store.find_call(callee="util.target", file_id=fid)
                self.assertIsNotNone(call)
                self.assertEqual(
                    store.symbol_by_id(call["callee_id"]).file_id,
                    store.file_by_path("src/util.py")["id"],
                )
            finally:
                store.close()

    def test_absolute_python_import_prefers_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "util.py").write_text(
                "def target():\n    return 'root'\n", encoding="utf-8")
            src = root / "src"
            src.mkdir()
            (src / "util.py").write_text(
                "def target():\n    return 'src'\n", encoding="utf-8")
            (src / "app.py").write_text(
                "import util\n\n"
                "def invoke():\n    return util.target()\n",
                encoding="utf-8",
            )

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                fid = store.file_by_path("src/app.py")["id"]
                self.assertEqual(
                    resolve_module(store, fid, "util"),
                    store.file_by_path("util.py")["id"],
                )
                call = store.find_call(callee="util.target", file_id=fid)
                self.assertIsNotNone(call)
                self.assertEqual(
                    store.symbol_by_id(call["callee_id"]).file_id,
                    store.file_by_path("util.py")["id"],
                )
            finally:
                store.close()

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

    def test_js_relative_import_does_not_resolve_python_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.js").write_text(
                'const { target } = require("./util");\n'
                "target();\n",
                encoding="utf-8",
            )
            (root / "util.py").write_text(
                "def target():\n    return 1\n", encoding="utf-8")

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                fid = store.file_by_path("app.js")["id"]
                self.assertIsNone(resolve_module(store, fid, "./util"))
            finally:
                store.close()

    def test_non_python_relative_import_does_not_add_root_module(self):
        """A JS/TS relative path must not be treated as a Python package id."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.ts").write_text(
                'import { helper } from "./util";\n\n'
                "export function caller() {\n"
                "  return target();\n"
                "}\n",
                encoding="utf-8",
            )
            (root / "util.ts").write_text(
                "export function helper() { return 1; }\n", encoding="utf-8")
            # The Python-style adjustment incorrectly treats this as the
            # imported module, making its unrelated target() visible to app.ts.
            (root / "helper.ts").write_text(
                "export function target() { return 2; }\n", encoding="utf-8")
            (root / "other.ts").write_text(
                "export function target() { return 3; }\n", encoding="utf-8")

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                fid = store.file_by_path("app.ts")["id"]
                self.assertIsNone(resolve_callee(store, fid, "target"))
            finally:
                store.close()

    def test_go_imported_function(self):
        gid = self._callee("main.go", "helper.Greet")
        self.assertIsNotNone(gid)
        self.assertEqual(self.store.symbol_by_id(gid).qualname, "helper.Greet")

    def test_go_import_path_with_dotted_domain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_dir = root / "example.com" / "acme" / "internal"
            package_dir.mkdir(parents=True)
            (root / "main.go").write_text(
                'package main\n\nimport "example.com/acme/internal/math"\n',
                encoding="utf-8",
            )
            (package_dir / "math.go").write_text(
                "package math\n\nfunc Add(a, b int) int { return a + b }\n",
                encoding="utf-8",
            )

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                fid = store.file_by_path("main.go")["id"]
                self.assertEqual(
                    resolve_module(store, fid, "example.com/acme/internal/math"),
                    store.file_by_path("example.com/acme/internal/math.go")["id"],
                )
            finally:
                store.close()

    def test_go_import_path_with_dotted_final_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_dir = root / "example.com" / "acme"
            package_dir.mkdir(parents=True)
            (root / "main.go").write_text(
                'package main\n\nimport "example.com/acme/foo.bar"\n',
                encoding="utf-8",
            )
            (package_dir / "foo.bar.go").write_text(
                "package foobar\n\nfunc Add(a, b int) int { return a + b }\n",
                encoding="utf-8",
            )

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                fid = store.file_by_path("main.go")["id"]
                self.assertEqual(
                    resolve_module(store, fid, "example.com/acme/foo.bar"),
                    store.file_by_path("example.com/acme/foo.bar.go")["id"],
                )
            finally:
                store.close()

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

    def test_rust_internal_import_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            (src / "child").mkdir(parents=True)
            (src / "lib.rs").write_text(
                "pub mod child;\n"
                "pub mod shared;\n"
                "pub use crate::shared::{shared_fn};\n"
                "pub(crate) use self::child::{child_fn};\n"
                "use crate::root_fn;\n"
                "use root_fn;\n"
                "fn root_fn() {}\n"
                "fn call() { shared_fn(); child_fn(); root_fn(); }\n",
                encoding="utf-8",
            )
            (src / "child.rs").write_text(
                "pub mod nested;\n"
                "use shared::other_fn;\n"
                "use super::shared::shared_fn;\n"
                "use self::nested::nested_fn;\n"
                "fn child_fn() { shared_fn(); other_fn(); nested_fn(); }\n",
                encoding="utf-8",
            )
            (src / "shared.rs").write_text(
                "pub fn shared_fn() {}\n"
                "pub fn other_fn() {}\n", encoding="utf-8")
            (src / "child" / "nested.rs").write_text(
                "pub fn nested_fn() {}\n", encoding="utf-8")
            (src / "main.rs").write_text(
                "pub mod main_child;\n"
                "use crate::main_fn;\n"
                "fn main_fn() {}\n"
                "fn main() { main_fn(); }\n",
                encoding="utf-8",
            )
            (src / "main_child.rs").write_text(
                "use crate::main_fn;\n"
                "fn call() { main_fn(); }\n",
                encoding="utf-8",
            )
            bin_dir = src / "bin"
            bin_dir.mkdir()
            (bin_dir / "tool.rs").write_text(
                "use crate::tool_fn;\n"
                "fn tool_fn() {}\n"
                "fn main() { tool_fn(); }\n",
                encoding="utf-8",
            )

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                lib_id = store.file_by_path("src/lib.rs")["id"]
                child_id = store.file_by_path("src/child.rs")["id"]
                tool_id = store.file_by_path("src/bin/tool.rs")["id"]
                imports = {
                    (store.file_by_id(row["file_id"])["path"], row["module"]):
                    store.file_by_id(row["target_id"])["path"]
                    for row in store.conn.execute(
                        "SELECT file_id, module, target_id FROM imports "
                        "WHERE target_id IS NOT NULL"
                    )
                }
                self.assertEqual(
                    imports[("src/lib.rs", "crate::shared::shared_fn")],
                    "src/shared.rs",
                )
                self.assertEqual(
                    imports[("src/lib.rs", "self::child::child_fn")],
                    "src/child.rs",
                )
                self.assertEqual(
                    imports[("src/lib.rs", "crate::root_fn")], "src/lib.rs")
                self.assertEqual(
                    imports[("src/lib.rs", "root_fn")], "src/lib.rs")
                self.assertEqual(
                    imports[("src/child.rs", "super::shared::shared_fn")],
                    "src/shared.rs",
                )
                self.assertEqual(
                    imports[("src/child.rs", "shared::other_fn")],
                    "src/shared.rs",
                )
                self.assertEqual(
                    imports[("src/child.rs", "self::nested::nested_fn")],
                    "src/child/nested.rs",
                )
                self.assertEqual(
                    imports[("src/child.rs", "nested")],
                    "src/child/nested.rs",
                )
                self.assertEqual(
                    imports[("src/main_child.rs", "crate::main_fn")],
                    "src/main.rs",
                )
                self.assertIsNone(
                    store.find_import(
                        module="crate::tool_fn", file_id=tool_id,
                    )["target_id"]
                )

                call = resolve_callee(store, lib_id, "shared_fn")
                self.assertEqual(
                    store.symbol_by_id(call)["qualname"],
                    "src/shared.shared_fn",
                )
                call = resolve_callee(store, child_id, "nested_fn")
                self.assertEqual(
                    store.symbol_by_id(call)["qualname"],
                    "src/child/nested.nested_fn",
                )
            finally:
                store.close()

    def test_rust_nested_main_and_lib_files_are_modules(self):
        """Only the actual crate root owns crate-relative paths."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            (src / "foo" / "main").mkdir(parents=True)
            (src / "bar" / "lib").mkdir(parents=True)
            (src / "lib.rs").write_text(
                "mod foo;\n"
                "mod bar;\n"
                "fn root_fn() {}\n",
                encoding="utf-8",
            )
            (src / "foo.rs").write_text("mod main;\n", encoding="utf-8")
            (src / "foo" / "main.rs").write_text(
                "mod child;\n"
                "use crate::root_fn;\n"
                "fn nested_main_fn() { root_fn(); }\n",
                encoding="utf-8",
            )
            (src / "foo" / "main" / "child.rs").write_text(
                "use crate::root_fn;\n", encoding="utf-8")
            (src / "bar.rs").write_text("mod lib;\n", encoding="utf-8")
            (src / "bar" / "lib.rs").write_text(
                "mod child;\n"
                "use crate::root_fn;\n",
                encoding="utf-8",
            )
            (src / "bar" / "lib" / "child.rs").write_text(
                "use crate::root_fn;\n", encoding="utf-8")
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "main.rs").write_text(
                "mod lib;\nfn root_fn() {}\n", encoding="utf-8")
            (legacy / "lib.rs").write_text(
                "use crate::root_fn;\n", encoding="utf-8")
            auto_bin = src / "bin" / "auto"
            auto_bin.mkdir(parents=True)
            (auto_bin / "main.rs").write_text(
                "mod child;\n"
                "use crate::bin_fn;\n"
                "fn bin_fn() {}\n",
                encoding="utf-8",
            )
            (auto_bin / "child.rs").write_text(
                "use crate::bin_fn;\n", encoding="utf-8")

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                auto_bin_id = store.file_by_path("src/bin/auto/main.rs")["id"]
                imports = {
                    (store.file_by_id(row["file_id"])["path"], row["module"]):
                    store.file_by_id(row["target_id"])["path"]
                    for row in store.conn.execute(
                        "SELECT file_id, module, target_id FROM imports "
                        "WHERE target_id IS NOT NULL"
                    )
                }
                self.assertEqual(
                    imports[("src/foo/main.rs", "child")],
                    "src/foo/main/child.rs",
                )
                self.assertEqual(
                    imports[("src/foo/main.rs", "crate::root_fn")],
                    "src/lib.rs",
                )
                self.assertEqual(
                    imports[("src/bar/lib.rs", "child")],
                    "src/bar/lib/child.rs",
                )
                self.assertEqual(
                    imports[("src/bar/lib.rs", "crate::root_fn")],
                    "src/lib.rs",
                )
                legacy_id = store.file_by_path("legacy/lib.rs")["id"]
                self.assertIsNone(
                    store.find_import(
                        module="crate::root_fn", file_id=legacy_id,
                    )["target_id"]
                )
                self.assertEqual(
                    imports[("src/bin/auto/main.rs", "child")],
                    "src/bin/auto/child.rs",
                )
                self.assertIsNone(
                    store.find_import(
                        module="crate::bin_fn", file_id=auto_bin_id,
                    )["target_id"]
                )
            finally:
                store.close()

    def test_rust_cargo_custom_roots_resolve_modules_and_items(self):
        """Cargo path overrides define roots even outside conventional names."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(parents=True)
            (root / "src" / "bin" / "tools").mkdir(parents=True)
            (root / "examples" / "nested").mkdir(parents=True)
            (root / "Cargo.toml").write_text(
                "[package]\nname = \"demo\"\nversion = \"0.1.0\"\n"
                "autobins = false\n\n"
                "[lib]\npath = \"src/entry.rs\"\n\n"
                "[[bin]]\nname = \"tool\"\n"
                "path = \"src/bin/tools/custom.rs\"\n\n"
                "[[bin]]\nname = \"flat\"\n"
                "path = \"src/bin/flat.rs\"\n\n"
                "[[example]]\nname = \"demo-example\"\n"
                "path = \"examples/nested/demo.rs\"\n",
                encoding="utf-8",
            )
            (root / "src" / "entry.rs").write_text(
                "mod child;\n"
                "use crate::lib_fn;\n"
                "fn lib_fn() {}\n",
                encoding="utf-8",
            )
            (root / "src" / "child.rs").write_text(
                "use crate::lib_fn;\n", encoding="utf-8")
            (root / "src" / "bin" / "tools" / "custom.rs").write_text(
                "mod nested;\n"
                "use crate::bin_fn;\n"
                "fn bin_fn() {}\n",
                encoding="utf-8",
            )
            (root / "src" / "bin" / "tools" / "nested.rs").write_text(
                "use crate::bin_fn;\n", encoding="utf-8")
            (root / "src" / "bin" / "flat.rs").write_text(
                "mod child;\n", encoding="utf-8")
            (root / "src" / "bin" / "child.rs").write_text(
                "mod nested;\n", encoding="utf-8")
            (root / "src" / "bin" / "child" / "nested.rs").parent.mkdir()
            (root / "src" / "bin" / "child" / "nested.rs").write_text(
                "fn nested() {}\n", encoding="utf-8")
            (root / "src" / "main.rs").write_text(
                "mod main_child;\n"
                "use crate::unconfigured;\nfn unconfigured() {}\n",
                encoding="utf-8",
            )
            (root / "src" / "main_child.rs").write_text(
                "fn child_fn() {}\n", encoding="utf-8")
            (root / "examples" / "nested" / "demo.rs").write_text(
                "use crate::example_fn;\nfn example_fn() {}\n",
                encoding="utf-8",
            )

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                imports = {
                    (store.file_by_id(row["file_id"])["path"], row["module"]):
                    store.file_by_id(row["target_id"])["path"]
                    for row in store.conn.execute(
                        "SELECT file_id, module, target_id FROM imports "
                        "WHERE target_id IS NOT NULL"
                    )
                }
                self.assertEqual(
                    imports[("src/entry.rs", "child")],
                    "src/child.rs",
                )
                self.assertEqual(
                    imports[("src/entry.rs", "crate::lib_fn")],
                    "src/entry.rs",
                )
                self.assertEqual(
                    imports[("src/bin/tools/custom.rs", "nested")],
                    "src/bin/tools/nested.rs",
                )
                self.assertEqual(
                    imports[("src/bin/tools/custom.rs", "crate::bin_fn")],
                    "src/bin/tools/custom.rs",
                )
                self.assertEqual(
                    imports[("src/bin/flat.rs", "child")],
                    "src/bin/child.rs",
                )
                self.assertEqual(
                    imports[("src/bin/child.rs", "nested")],
                    "src/bin/child/nested.rs",
                )
                self.assertIsNone(
                    store.find_import(module="crate::unconfigured")["target_id"]
                )
                main_id = store.file_by_path("src/main.rs")["id"]
                row = store.conn.execute(
                    "SELECT target_id FROM imports WHERE file_id = ? "
                    "AND module = ?", (main_id, "main_child")
                ).fetchone()
                self.assertIsNone(row["target_id"])
                example = store.find_import(module="crate::example_fn")
                self.assertEqual(
                    store.file_by_id(example["target_id"])["path"],
                    "examples/nested/demo.rs",
                )
            finally:
                store.close()

    def test_rust_no_cargo_nested_binary_files_are_not_crate_roots(self):
        """Nested binary-like files need Cargo target configuration."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "src" / "bin"
            (bin_dir / "nested").mkdir(parents=True)
            (bin_dir / "tool.rs").write_text(
                "use crate::tool_fn;\nfn tool_fn() {}\n",
                encoding="utf-8",
            )
            (bin_dir / "nested" / "main.rs").write_text(
                "use crate::nested_fn;\nfn nested_fn() {}\n",
                encoding="utf-8",
            )

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                for path, module in (
                        ("src/bin/tool.rs", "crate::tool_fn"),
                        ("src/bin/nested/main.rs", "crate::nested_fn")):
                    file_id = store.file_by_path(path)["id"]
                    self.assertIsNone(
                        store.find_import(
                            module=module, file_id=file_id,
                        )["target_id"]
                    )
            finally:
                store.close()

    def test_rust_cargo_fallback_reads_multiline_custom_root(self):
        """The Python 3.10 fallback handles valid multiline TOML strings."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            (root / "Cargo.toml").write_text(
                "[package]\nname = \"demo\"\nversion = \"0.1.0\"\n\n"
                "[lib]\npath = \"\"\"src/entry.rs\"\"\"\n",
                encoding="utf-8",
            )
            src.joinpath("entry.rs").write_text(
                "use crate::root_fn;\nfn root_fn() {}\n", encoding="utf-8")

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            with patch("codegraph.resolver.tomllib", None):
                build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                row = store.find_import(module="crate::root_fn")
                self.assertEqual(
                    store.file_by_id(row["target_id"])["path"],
                    "src/entry.rs",
                )
            finally:
                store.close()

    def test_rust_virtual_workspace_does_not_define_roots(self):
        """A workspace-only Cargo manifest is not a package target."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "Cargo.toml").write_text(
                "[workspace]\nmembers = [\"member\"]\n",
                encoding="utf-8",
            )
            (root / "src" / "lib.rs").write_text(
                "use crate::unconfigured;\nfn unconfigured() {}\n",
                encoding="utf-8",
            )

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                self.assertIsNone(
                    store.find_import(module="crate::unconfigured")["target_id"]
                )
            finally:
                store.close()

    def test_rust_cargo_root_changes_re_resolve_incrementally(self):
        """Changing Cargo root metadata must invalidate old import edges."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            manifest = root / "Cargo.toml"
            manifest.write_text(
                "[package]\nname = \"demo\"\nversion = \"0.1.0\"\n\n"
                "[lib]\npath = \"src/entry.rs\"\n",
                encoding="utf-8",
            )
            (src / "entry.rs").write_text(
                "use crate::root_fn;\nfn root_fn() {}\n", encoding="utf-8")

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                row = store.find_import(module="crate::root_fn")
                self.assertEqual(store.file_by_id(row["target_id"])["path"],
                                 "src/entry.rs")
            finally:
                store.close()

            (src / "other.rs").write_text(
                "mod entry;\nfn root_fn() {}\n", encoding="utf-8")
            manifest.write_text(
                "[package]\nname = \"demo\"\nversion = \"0.1.0\"\n\n"
                "[lib]\npath = \"src/other.rs\"\n",
                encoding="utf-8",
            )
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                row = store.find_import(module="crate::root_fn")
                self.assertEqual(store.file_by_id(row["target_id"])["path"],
                                 "src/other.rs")
            finally:
                store.close()

    def test_rust_path_attribute_resolves_custom_module_file(self):
        """A path attribute changes the file selected by a mod declaration."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            (root / "legacy").mkdir()
            (root / "legacy" / "main.rs").write_text(
                "fn internal_fn() {}\n", encoding="utf-8")
            (src / "parent.rs").write_text(
                "#[path = \"alt/child.rs\"] mod child;\n",
                encoding="utf-8")
            (src / "alt").mkdir()
            (src / "alt" / "child.rs").write_text(
                "use super::super::root_fn;\n", encoding="utf-8")
            (src / "lib.rs").write_text(
                "#[path = \"../legacy/main.rs\"] mod implementation;\n"
                "mod parent;\n"
                "use crate::implementation::internal_fn;\n"
                "fn root_fn() {}\n",
                encoding="utf-8",
            )

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                imports = {
                    (store.file_by_id(row["file_id"])["path"], row["module"]):
                    store.file_by_id(row["target_id"])["path"]
                    for row in store.conn.execute(
                        "SELECT file_id, module, target_id FROM imports "
                        "WHERE target_id IS NOT NULL"
                    )
                }
                self.assertEqual(
                    imports[("src/lib.rs", "implementation")],
                    "legacy/main.rs",
                )
                self.assertEqual(
                    imports[("src/lib.rs", "crate::implementation::internal_fn")],
                    "legacy/main.rs",
                )
                self.assertEqual(
                    imports[("src/parent.rs", "child")],
                    "src/alt/child.rs",
                )
                self.assertEqual(
                    imports[("src/alt/child.rs", "super::super::root_fn")],
                    "src/lib.rs",
                )
            finally:
                store.close()

    def test_rust_path_attribute_survives_comments_and_blank_lines(self):
        """Comments cannot cancel or invent a pending path attribute."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            (src / "alt").mkdir(parents=True)
            src.joinpath("lib.rs").write_text(
                "/* #[path = \"wrong.rs\"] */\n"
                "#[path = \"alt/child.rs\"]\n"
                "\n"
                "// the attribute applies across comments\n"
                "mod child;\n",
                encoding="utf-8",
            )
            src.joinpath("alt", "child.rs").write_text(
                "fn child_fn() {}\n", encoding="utf-8")

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                row = store.find_import(module="child")
                self.assertEqual(
                    store.file_by_id(row["target_id"])["path"],
                    "src/alt/child.rs",
                )
            finally:
                store.close()

    def test_rust_edition_2021_bare_use_prefers_current_module(self):
        """Rust 2018+ resolves a bare use path from the current module."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            (src / "child" / "inner").mkdir(parents=True)
            (root / "Cargo.toml").write_text(
                "[package]\nname = \"demo\"\nversion = \"0.1.0\"\n"
                "edition = \"2021\"\n",
                encoding="utf-8",
            )
            src.joinpath("lib.rs").write_text(
                "mod child;\nmod inner;\nmod root_only;\n", encoding="utf-8")
            src.joinpath("child.rs").write_text(
                "mod inner;\n"
                "use inner::foo;\n"
                "use root_only::foo;\n"
                "use local_item;\n"
                "fn local_item() {}\n"
                "fn call() { foo(); }\n",
                encoding="utf-8",
            )
            src.joinpath("inner.rs").write_text(
                "fn foo() {}\n", encoding="utf-8")
            src.joinpath("child", "inner.rs").write_text(
                "fn foo() {}\n", encoding="utf-8")
            src.joinpath("root_only.rs").write_text(
                "fn foo() {}\n", encoding="utf-8")

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                row = store.find_import(module="inner::foo")
                self.assertEqual(
                    store.file_by_id(row["target_id"])["path"],
                    "src/child/inner.rs",
                )
                self.assertIsNone(
                    store.find_import(module="root_only::foo")["target_id"]
                )
                self.assertEqual(
                    store.find_import(module="local_item")["target_id"],
                    store.file_by_path("src/child.rs")["id"],
                )
            finally:
                store.close()

    def test_rust_2015_bare_use_does_not_fallback_to_current_module(self):
        """Rust 2015 bare use paths stay crate-relative without a root."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            (src / "parent").mkdir(parents=True)
            src.joinpath("lib.rs").write_text(
                "fn unrelated_root_fn() {}\n", encoding="utf-8")
            src.joinpath("parent.rs").write_text(
                "mod inner;\n"
                "use inner::foo;\n"
                "use self::inner::foo;\n",
                encoding="utf-8",
            )
            src.joinpath("parent", "inner.rs").write_text(
                "fn foo() {}\n", encoding="utf-8")
            src.joinpath("orphan.rs").write_text(
                "use super::unrelated_root_fn;\n", encoding="utf-8")

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                parent_id = store.file_by_path("src/parent.rs")["id"]
                self.assertEqual(
                    resolve_module(store, parent_id, "inner"),
                    store.file_by_path("src/parent/inner.rs")["id"],
                )
                bare = store.find_import(module="inner::foo")
                self.assertIsNone(bare["target_id"])
                explicit = store.find_import(module="self::inner::foo")
                self.assertEqual(
                    store.file_by_id(explicit["target_id"])['path'],
                    "src/parent/inner.rs",
                )
                orphan_id = store.file_by_path("src/orphan.rs")["id"]
                self.assertIsNone(
                    store.find_import(
                        module="super::unrelated_root_fn", file_id=orphan_id,
                    )["target_id"]
                )
            finally:
                store.close()

    def test_rust_use_alias_resolves_call_to_source_symbol(self):
        """An aliased use binds the alias to the source item."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            src.joinpath("lib.rs").write_text(
                "mod a;\nuse crate::a::foo as bar;\n"
                "fn call() { bar(); }\n",
                encoding="utf-8",
            )
            src.joinpath("a.rs").write_text(
                "fn foo() {}\n", encoding="utf-8")

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                file_id = store.file_by_path("src/lib.rs")["id"]
                symbol_id = resolve_callee(store, file_id, "bar")
                self.assertEqual(
                    store.symbol_by_id(symbol_id)["qualname"],
                    "src/a.foo",
                )
            finally:
                store.close()

    def test_ignored_nested_cargo_manifest_does_not_hide_conventional_root(self):
        """Excluded package metadata must not change in-scope Rust roots."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            ignored = root / "ignored"
            src.mkdir()
            ignored.mkdir()
            src.joinpath("lib.rs").write_text(
                "use crate::root_fn;\nfn root_fn() {}\n",
                encoding="utf-8",
            )
            ignored.joinpath("Cargo.toml").write_text(
                "[package]\nname = \"ignored\"\nversion = \"0.1.0\"\n"
                "[lib]\npath = \"entry.rs\"\n",
                encoding="utf-8",
            )
            ignored.joinpath("entry.rs").write_text(
                "fn root_fn() {}\n", encoding="utf-8")

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            cfg.exclude = list(cfg.exclude) + ["ignored"]
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                row = store.find_import(module="crate::root_fn")
                self.assertEqual(
                    store.file_by_id(row["target_id"])["path"],
                    "src/lib.rs",
                )
            finally:
                store.close()

    def test_rust_use_does_not_resolve_undeclared_module_file(self):
        """A matching file is not a module until a mod declaration reaches it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            (src / "lib.rs").write_text(
                "use crate::orphan::orphan_fn;\n"
                "use crate::missing::root_fn;\n"
                "fn root_fn() {}\n",
                encoding="utf-8",
            )
            (src / "orphan.rs").write_text(
                "fn orphan_fn() {}\n", encoding="utf-8")

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                self.assertIsNone(
                    store.find_import(module="crate::orphan::orphan_fn")["target_id"]
                )
                self.assertIsNone(
                    store.find_import(module="crate::missing::root_fn")["target_id"]
                )
            finally:
                store.close()

    def test_rust_mod_graph_changes_re_resolve_unchanged_descendants(self):
        """Changing mod reachability refreshes imports in unchanged modules."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            manifest = root / "Cargo.toml"
            manifest.write_text(
                "[package]\nname = \"demo\"\nversion = \"0.1.0\"\n\n"
                "[lib]\npath = \"src/lib.rs\"\n",
                encoding="utf-8",
            )
            lib = src / "lib.rs"
            lib.write_text("fn root_fn() {}\n", encoding="utf-8")
            child = src / "child.rs"
            child.write_text(
                "use crate::root_fn;\n", encoding="utf-8")

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                self.assertIsNone(
                    store.find_import(module="crate::root_fn")["target_id"]
                )
            finally:
                store.close()

            lib.write_text("mod child;\nfn root_fn() {}\n", encoding="utf-8")
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                row = store.find_import(module="crate::root_fn")
                self.assertEqual(store.file_by_id(row["target_id"])["path"],
                                 "src/lib.rs")
            finally:
                store.close()

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

    def test_root_package_relative_import(self):
        """from . import x inside a root __init__.py must resolve to x.py."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "__init__.py").write_text(
                "from . import sibling\n\n"
                "def expose(item):\n    return sibling.deliver(item)\n",
                encoding="utf-8",
            )
            (root / "sibling.py").write_text(
                "def deliver(item):\n    return item\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                fid = store.file_by_path("__init__.py")["id"]
                sid = resolve_callee(store, fid, "sibling.deliver")
                self.assertIsNotNone(sid)
                self.assertEqual(
                    store.symbol_by_id(sid).qualname, "sibling.deliver"
                )
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

    def test_python_stub_module_resolution(self):
        """Python imports resolve modules and packages that only have .pyi files."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text(
                "import api\n"
                "from stubs import models\n"
                "api.fetch()\n"
                "models.User()\n",
                encoding="utf-8",
            )
            (root / "api.pyi").write_text(
                "def fetch() -> None: ...\n", encoding="utf-8")
            (root / "ambiguous.pyi").write_text(
                "def from_stub() -> None: ...\n", encoding="utf-8")
            ambiguous = root / "ambiguous"
            ambiguous.mkdir()
            (ambiguous / "__init__.py").write_text(
                "def from_package() -> None: ...\n", encoding="utf-8")
            stubs = root / "stubs"
            stubs.mkdir()
            (stubs / "__init__.pyi").write_text(
                "from . import models\n", encoding="utf-8")
            (stubs / "models.pyi").write_text(
                "class User: ...\n", encoding="utf-8")
            pkg = root / "pkg"
            pkg.mkdir()
            (pkg / "foo__init__.pyi").write_text(
                "from . import sibling\n\n"
                "def expose() -> None: sibling.deliver()\n",
                encoding="utf-8",
            )
            (pkg / "sibling.pyi").write_text(
                "def deliver() -> None: ...\n", encoding="utf-8")

            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)
            store = IndexStore(str(cfg.db_path))
            try:
                fid = store.file_by_path("app.py")["id"]
                self.assertEqual(
                    resolve_module(store, fid, "api"),
                    store.file_by_path("api.pyi")["id"],
                )
                self.assertEqual(
                    resolve_module(store, fid, "stubs"),
                    store.file_by_path("stubs/__init__.pyi")["id"],
                )
                self.assertEqual(
                    resolve_module(store, fid, "ambiguous"),
                    store.file_by_path("ambiguous/__init__.py")["id"],
                )
                api_call = resolve_callee(store, fid, "api.fetch")
                self.assertEqual(
                    store.symbol_by_id(api_call).qualname, "api.fetch"
                )
                models_call = resolve_callee(store, fid, "models.User")
                self.assertEqual(
                    store.symbol_by_id(models_call).qualname, "stubs.models.User"
                )
                foo_id = store.file_by_path("pkg/foo__init__.pyi")["id"]
                sibling_call = resolve_callee(store, foo_id, "sibling.deliver")
                self.assertEqual(
                    store.symbol_by_id(sibling_call).qualname,
                    "pkg.sibling.deliver",
                )
            finally:
                store.close()

    def test_scoped_resolution_is_atomic(self):
        statements = []
        self.store.conn.set_trace_callback(statements.append)
        try:
            resolve_all(self.store, file_ids={self._file_id("app.py")})
        finally:
            self.store.conn.set_trace_callback(None)
        self.assertIsNotNone(self.store.find_call(callee="create_cart")["callee_id"])
        self.assertIn("BEGIN IMMEDIATE", statements)
        self.assertEqual(statements.count("COMMIT"), 1)

    def test_scoped_resolution_chunks_large_id_sets(self):
        """Large scoped ID sets must not exceed SQLite's bind limit."""
        if not hasattr(self.store.conn, "setlimit"):
            self.skipTest("sqlite3.Connection.setlimit is unavailable")

        limit = sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER
        previous_limit = self.store.conn.setlimit(limit, 999)
        try:
            large_ids = set(range(1, 1025))
            resolve_all(
                self.store,
                file_ids=large_ids,
                call_ids=large_ids,
                import_ids=large_ids,
            )
        finally:
            self.store.conn.setlimit(limit, previous_limit)

        self.assertIsNotNone(self.store.find_call(callee="create_cart")["callee_id"])
        self.assertIsNotNone(self.store.find_import(module="pkg.cart")["target_id"])


if __name__ == "__main__":
    unittest.main()
