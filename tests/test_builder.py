"""Tests for incremental index building (builder.build_index)."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codegraph.builder import build_index
from codegraph.config import load_config
from codegraph.models import FileScan, SymbolRec
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

    def test_inaccessible_directory_preserves_indexed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hidden = root / "hidden"
            target = hidden / "keep.py"
            hidden.mkdir()
            target.write_text("def keep():\n    return 1\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)

            def incomplete_walk(path, followlinks=False, onerror=None):
                if onerror is not None:
                    onerror(PermissionError(13, "Permission denied", str(hidden)))
                yield str(root), [], []

            with patch("codegraph.scanner.walk.os.walk", incomplete_walk):
                report = build_index(cfg, quiet=True)

            self.assertEqual(report.files_removed, 0)
            store = IndexStore(str(cfg.db_path))
            try:
                self.assertIsNotNone(store.file_by_path("hidden/keep.py"))
            finally:
                store.close()

    def test_transient_stat_failure_preserves_indexed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "keep.py"
            target.write_text("def keep():\n    return 1\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)

            original_stat = Path.stat
            failed = False

            def fail_target_once(path, *args, **kwargs):
                nonlocal failed
                if path == target and not failed:
                    failed = True
                    raise OSError("temporary stat failure")
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", fail_target_once):
                report = build_index(cfg, quiet=True)

            self.assertTrue(failed)
            self.assertEqual(report.files_removed, 0)
            store = IndexStore(str(cfg.db_path))
            try:
                self.assertIsNotNone(store.file_by_path("keep.py"))
            finally:
                store.close()

    def test_deleted_file_interruption_keeps_resolution_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / "entry.ts"
            entry.write_text(
                'import { replacement } from "./util";\n'
                "export function invoke() {\n"
                "  return replacement();\n"
                "}\n",
                encoding="utf-8",
            )
            preferred = root / "util.ts"
            preferred.write_text(
                "export const selected = true;\n", encoding="utf-8")
            (root / "util.js").write_text(
                "export function replacement() { return 1; }\n",
                encoding="utf-8",
            )
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)

            preferred.unlink()
            original_remove_file = IndexStore.remove_file
            original_set_meta = IndexStore.set_meta
            removed = False

            def remove_file(store, path):
                nonlocal removed
                impact = original_remove_file(store, path)
                removed = True
                return impact

            def set_meta(store, key, value):
                if removed and key == "resolution_pending" and value == "1":
                    raise RuntimeError("interrupted after file removal")
                return original_set_meta(store, key, value)

            with patch.object(IndexStore, "remove_file", remove_file), \
                    patch.object(IndexStore, "set_meta", set_meta):
                with self.assertRaises(RuntimeError):
                    build_index(cfg)

            store = IndexStore(str(cfg.db_path))
            try:
                self.assertEqual(store.get_meta("resolution_pending"), "1")
                self.assertIsNone(store.find_import(module="./util")["target_id"])
            finally:
                store.close()

            build_index(cfg)

            store = IndexStore(str(cfg.db_path))
            try:
                self.assertEqual(
                    store.find_import(module="./util")["target_id"],
                    store.file_by_path("util.js")["id"],
                )
                call = store.find_call(callee="replacement")
                self.assertIsNotNone(call["callee_id"])
                self.assertEqual(
                    store.file_by_id(call["callee_id"])["path"], "util.js"
                )
                self.assertEqual(store.get_meta("resolution_pending"), "0")
            finally:
                store.close()

    def test_deleted_empty_file_clears_resolution_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = root / "empty.py"
            empty.write_text("# no symbols or references\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)

            empty.unlink()
            build_index(cfg)

            store = IndexStore(str(cfg.db_path))
            try:
                self.assertEqual(store.get_meta("resolution_pending"), "0")
            finally:
                store.close()

            with patch("codegraph.builder.resolve_all") as resolve:
                build_index(cfg)
            resolve.assert_not_called()

    def test_force_reparses_all(self):
        build_index(self._cfg())
        report = build_index(self._cfg(), force=True)
        self.assertEqual(report.files_changed, ALL_FILES)
        self.assertEqual(report.files_skipped, 0)

    def test_force_scan_failure_preserves_existing_index(self):
        cfg = self._cfg()
        build_index(cfg)

        with patch("codegraph.builder.scan_text",
                   side_effect=RuntimeError("temporary scan failure")):
            with self.assertRaises(RuntimeError):
                build_index(cfg, force=True)

        store = IndexStore(str(cfg.db_path))
        try:
            self.assertIsNotNone(store.file_by_path("app.py"))
            self.assertIsNotNone(store.symbol_by_qualname("app.main"))
        finally:
            store.close()

    def test_language_map_change_replaces_unchanged_file_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "module.py").write_text(
                "def target():\n    return 1\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)

            cfg.language_map = {".py": "javascript"}
            report = build_index(cfg)

            self.assertEqual(report.files_changed, 1)
            self.assertEqual(report.files_skipped, 0)
            store = IndexStore(str(cfg.db_path))
            try:
                self.assertEqual(store.file_by_path("module.py")["lang"], "javascript")
                self.assertIsNone(store.symbol_by_qualname("module.target"))
            finally:
                store.close()

    def test_engine_change_replaces_unchanged_file_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "module.py").write_text(
                "def target():\n    return 1\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)

            cfg.engine = "auto"
            replacement = FileScan(
                "python", "module",
                [SymbolRec("function", "auto_marker", "module.auto_marker", "", 1, 1, "")],
            )
            with patch("codegraph.builder.scan_text", return_value=replacement) as scan:
                report = build_index(cfg)

            self.assertEqual(report.files_changed, 1)
            self.assertEqual(report.files_skipped, 0)
            scan.assert_called_once_with("def target():\n    return 1\n", "python",
                                         "module.py", "auto")
            store = IndexStore(str(cfg.db_path))
            try:
                self.assertIsNotNone(store.symbol_by_qualname("module.auto_marker"))
                self.assertIsNone(store.symbol_by_qualname("module.target"))
            finally:
                store.close()

    def test_config_change_retries_file_after_transient_read_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.py"
            other = root / "other.py"
            target.write_text("def target():\n    return 1\n", encoding="utf-8")
            other.write_text("def other():\n    return 2\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)

            cfg.language_map = {".py": "javascript"}
            original_read_bytes = Path.read_bytes
            failed = False

            def fail_target_once(path):
                nonlocal failed
                if path == target and not failed:
                    failed = True
                    raise OSError("temporary read failure")
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", fail_target_once):
                first = build_index(cfg, quiet=True)

            self.assertTrue(failed)
            self.assertEqual(first.files_changed, 1)
            report = build_index(cfg, quiet=True)
            self.assertEqual(report.files_changed, 2)
            self.assertEqual(report.files_skipped, 0)

            store = IndexStore(str(cfg.db_path))
            try:
                self.assertEqual(store.file_by_path("target.py")["lang"], "javascript")
            finally:
                store.close()

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

    def test_scan_failure_after_payload_commit_is_retried_on_next_incremental_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a_target.py").write_text(
                "def target():\n    return 1\n", encoding="utf-8")
            (root / "m_caller.py").write_text(
                "def invoke():\n    return target()\n", encoding="utf-8")
            (root / "z_later.py").write_text(
                "value = 1\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)

            (root / "a_target.py").write_text(
                "def target():\n    return 2\n", encoding="utf-8")
            (root / "z_later.py").write_text(
                "value = 2\n", encoding="utf-8")
            from codegraph.scanner import scan_text as real_scan_text

            def fail_later(text, lang, rel_path, engine):
                if rel_path == "z_later.py":
                    raise RuntimeError("temporary scan failure")
                return real_scan_text(text, lang, rel_path, engine)

            with patch("codegraph.builder.scan_text", side_effect=fail_later):
                with self.assertRaises(RuntimeError):
                    build_index(cfg)

            store = IndexStore(str(cfg.db_path))
            try:
                self.assertEqual(store.get_meta("resolution_pending"), "1")
                self.assertIsNone(store.find_call(callee="target")["callee_id"])
            finally:
                store.close()

            report = build_index(cfg)
            self.assertEqual(report.files_changed, 1)
            self.assertEqual(report.files_skipped, 2)

            store = IndexStore(str(cfg.db_path))
            try:
                call = store.find_call(callee="target")
                self.assertIsNotNone(call["callee_id"])
                self.assertEqual(store.get_meta("resolution_pending"), "0")
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

    def test_deleted_import_candidate_rechecks_calls_in_importing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "entry.ts").write_text(
                'import { replacement } from "./util";\n'
                "export function invoke() {\n"
                "  return replacement();\n"
                "}\n",
                encoding="utf-8",
            )
            (root / "util.ts").write_text(
                "export const selected = true;\n", encoding="utf-8")
            (root / "util.js").write_text(
                "export function replacement() { return 1; }\n",
                encoding="utf-8",
            )
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)

            store = IndexStore(str(cfg.db_path))
            try:
                self.assertEqual(
                    store.find_import(module="./util")["target_id"],
                    store.file_by_path("util.ts")["id"],
                )
                self.assertIsNone(store.find_call(callee="replacement")["callee_id"])
            finally:
                store.close()

            (root / "util.ts").unlink()
            report = build_index(cfg)

            store = IndexStore(str(cfg.db_path))
            try:
                self.assertEqual(report.files_removed, 1)
                self.assertEqual(
                    store.find_import(module="./util")["target_id"],
                    store.file_by_path("util.js")["id"],
                )
                call = store.find_call(callee="replacement")
                self.assertIsNotNone(call["callee_id"])
                self.assertEqual(
                    store.file_by_id(call["callee_id"])["path"], "util.js"
                )
            finally:
                store.close()

    def test_import_target_change_does_not_fallback_to_obsolete_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "entry.ts").write_text(
                'import { old } from "./util";\n'
                "export function invoke() {\n"
                "  return old();\n"
                "}\n",
                encoding="utf-8",
            )
            (root / "util.js").write_text(
                "export function old() { return 1; }\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.engine = "quick"
            build_index(cfg)

            store = IndexStore(str(cfg.db_path))
            try:
                call = store.find_call(callee="old")
                self.assertEqual(
                    store.file_by_id(call["callee_id"])["path"], "util.js"
                )
            finally:
                store.close()

            (root / "util.ts").write_text(
                "export function replacement() { return 2; }\n", encoding="utf-8")
            build_index(cfg)

            store = IndexStore(str(cfg.db_path))
            try:
                self.assertEqual(
                    store.find_import(module="./util")["target_id"],
                    store.file_by_path("util.ts")["id"],
                )
                self.assertIsNone(store.find_call(callee="old")["callee_id"])
            finally:
                store.close()

            build_index(cfg, force=True)

            store = IndexStore(str(cfg.db_path))
            try:
                self.assertIsNone(store.find_call(callee="old")["callee_id"])
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
