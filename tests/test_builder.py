"""Tests for incremental index building (builder.build_index)."""

import shutil
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
