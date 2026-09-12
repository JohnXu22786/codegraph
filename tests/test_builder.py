"""Tests for incremental index building (builder.build_index)."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codegraph.builder import build_index
from codegraph.config import load_config
from codegraph.store import IndexStore

from .fixtures import PROJ

ALL_FILES = 14  # files under fixtures/proj recognised as source code


class BuilderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        shutil.copytree(PROJ, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, **kw):
        cfg = load_config(root=str(self.root))
        cfg.engine = "quick"  # deterministic provider for these tests
        for k, v in kw.items():
            setattr(cfg, k, v)
        return cfg

    def test_first_index_counts(self):
        report = build_index(self._cfg())
        self.assertEqual(report.files_scanned, ALL_FILES)
        self.assertEqual(report.files_changed, ALL_FILES)
        self.assertEqual(report.files_skipped, 0)
        self.assertEqual(report.files_removed, 0)
        self.assertGreater(report.symbols, 0)
        self.assertGreater(report.calls, 0)
        self.assertGreater(report.imports, 0)
        self.assertEqual(sum(report.languages.values()), ALL_FILES)

    def test_unchanged_second_run_skips_everything(self):
        build_index(self._cfg())
        report = build_index(self._cfg())
        self.assertEqual(report.files_skipped, ALL_FILES)
        self.assertEqual(report.files_changed, 0)
        self.assertEqual(report.files_removed, 0)
        self.assertEqual(report.symbols, 0)

    def test_unchanged_incremental_run_skips_resolution_pass(self):
        build_index(self._cfg())
        with patch("codegraph.builder.resolve_all") as resolve:
            report = build_index(self._cfg())
        self.assertEqual(report.files_skipped, ALL_FILES)
        resolve.assert_not_called()

    def test_changed_file_reparsed_only(self):
        build_index(self._cfg())
        target = self.root / "pkg" / "pricing.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n\ndef vat(x):\n    return x\n",
                          encoding="utf-8")
        report = build_index(self._cfg())
        self.assertEqual(report.files_changed, 1)
        self.assertEqual(report.files_skipped, ALL_FILES - 1)
        store = IndexStore(str(self._cfg().db_path))
        self.assertIsNotNone(store.symbol_by_qualname("pkg.pricing.vat"))
        store.close()

    def test_changed_file_clears_incoming_edges(self):
        build_index(self._cfg())
        target = self.root / "pkg" / "pricing.py"
        target.write_text(
            '"""Price lookups for the demo shop."""\n\n'
            'def discount(sku):\n    return 0\n',
            encoding="utf-8",
        )
        build_index(self._cfg())
        store = IndexStore(str(self._cfg().db_path))
        try:
            incoming = store.find_call(callee="pricing.price")
            self.assertIsNotNone(incoming)
            self.assertIsNone(incoming["callee_id"])
        finally:
            store.close()

    def test_deleted_file_removed(self):
        build_index(self._cfg())
        (self.root / "helper.go").unlink()
        report = build_index(self._cfg())
        self.assertEqual(report.files_removed, 1)
        self.assertEqual(report.files_changed, 0)
        store = IndexStore(str(self._cfg().db_path))
        self.assertIsNone(store.file_by_path("helper.go"))
        store.close()

    def test_force_reparses_all(self):
        build_index(self._cfg())
        report = build_index(self._cfg(), force=True)
        self.assertEqual(report.files_changed, ALL_FILES)
        self.assertEqual(report.files_skipped, 0)

    def test_last_indexed_meta_written(self):
        build_index(self._cfg())
        store = IndexStore(str(self._cfg().db_path))
        self.assertIsNotNone(store.get_meta("last_indexed"))
        store.close()

    def test_resolution_fills_edges(self):
        build_index(self._cfg())
        store = IndexStore(str(self._cfg().db_path))
        try:
            # a call that resolves cross-file: app.main -> create_cart
            row = store.find_call(callee="create_cart")
            self.assertIsNotNone(row)
            self.assertIsNotNone(row["callee_id"])
            # module import resolves: app.py -> pkg/pricing.py
            imp = store.find_import(module="pkg.pricing")
            self.assertIsNotNone(imp["target_id"])
            # stdlib import stays unresolved
            imp_os = store.find_import(module="os")
            self.assertIsNone(imp_os["target_id"])
        finally:
            store.close()

    def test_failed_resolution_is_retried_on_next_incremental_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            caller = root / "caller.py"
            caller.write_text(
                "def invoke():\n    return missing()\n", encoding="utf-8")
            (root / "target.py").write_text(
                "def target():\n    return 1\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)

            caller.write_text(
                "def invoke():\n    return target()\n", encoding="utf-8")
            with patch(
                    "codegraph.builder.resolve_all",
                    side_effect=RuntimeError("temporary resolution failure")):
                with self.assertRaises(RuntimeError):
                    build_index(cfg)

            report = build_index(cfg)
            self.assertEqual(report.files_changed, 0)
            self.assertEqual(report.files_skipped, 2)

            store = IndexStore(str(cfg.db_path))
            try:
                call = store.find_call(callee="target")
                self.assertIsNotNone(call["callee_id"])
                self.assertEqual(store.get_meta("resolution_pending"), "0")
                self.assertEqual(
                    store.symbol_by_id(call["callee_id"]).qualname, "target.target"
                )
            finally:
                store.close()

    def test_new_higher_priority_import_candidate_re_resolves_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "entry.ts").write_text(
                'import { value } from "./util";\n', encoding="utf-8")
            (root / "util.js").write_text(
                "export const value = 'js';\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)

            store = IndexStore(str(cfg.db_path))
            try:
                imp = store.find_import(module="./util")
                self.assertEqual(imp["target_id"], store.file_by_path("util.js")["id"])
            finally:
                store.close()

            (root / "util.ts").write_text(
                "export const value = 'ts';\n", encoding="utf-8")
            build_index(cfg)

            store = IndexStore(str(cfg.db_path))
            try:
                imp = store.find_import(module="./util")
                self.assertEqual(imp["target_id"], store.file_by_path("util.ts")["id"])
            finally:
                store.close()

    def test_new_duplicate_symbol_invalidates_resolved_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "caller.py").write_text(
                "def invoke():\n    return target()\n", encoding="utf-8")
            (root / "first.py").write_text(
                "def target():\n    return 1\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)

            store = IndexStore(str(cfg.db_path))
            try:
                call = store.find_call(callee="target")
                self.assertEqual(
                    store.symbol_by_id(call["callee_id"]).qualname, "first.target"
                )
            finally:
                store.close()

            (root / "second.py").write_text(
                "def target():\n    return 2\n", encoding="utf-8")
            build_index(cfg)

            store = IndexStore(str(cfg.db_path))
            try:
                self.assertIsNone(store.find_call(callee="target")["callee_id"])
            finally:
                store.close()

    def test_removed_duplicate_symbol_resolves_calls_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "caller.py").write_text(
                "def invoke():\n    return target()\n", encoding="utf-8")
            (root / "first.py").write_text(
                "def target():\n    return 1\n", encoding="utf-8")
            (root / "second.py").write_text(
                "def target():\n    return 2\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)

            store = IndexStore(str(cfg.db_path))
            try:
                self.assertIsNone(store.find_call(callee="target")["callee_id"])
            finally:
                store.close()

            (root / "second.py").unlink()
            build_index(cfg)

            store = IndexStore(str(cfg.db_path))
            try:
                call = store.find_call(callee="target")
                self.assertIsNotNone(call["callee_id"])
                self.assertEqual(
                    store.symbol_by_id(call["callee_id"]).qualname, "first.target"
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
