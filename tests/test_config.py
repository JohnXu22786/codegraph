"""Tests for config loading: file merging, env overrides, validation."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codegraph.config import (
    DEFAULT_EXCLUDES,
    default_config,
    load_config,
    write_default_config,
)
from codegraph.scanner.walk import discover_files


class DefaultConfigTest(unittest.TestCase):
    def test_defaults_shape(self):
        cfg = default_config("C:/proj")
        self.assertEqual(cfg.root, "C:\\proj" if os.name == "nt" else "/C:/proj")
        self.assertIn(".cg", cfg.exclude)
        self.assertEqual(cfg.max_file_kb, 512)
        self.assertTrue(cfg.incremental)
        self.assertEqual(cfg.engine, "auto")
        self.assertEqual(cfg.include, [])

    def test_default_excludes_count_is_documented(self):
        # README says "17 defaults" — keep the claim in sync with the code
        self.assertEqual(len(DEFAULT_EXCLUDES), 17)


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_file_no_env(self):
        cfg = load_config(root=str(self.root))
        self.assertEqual(cfg.root, str(self.root))
        self.assertEqual(cfg.exclude, DEFAULT_EXCLUDES)

    def test_file_values_apply(self):
        (self.root / "codegraph.json").write_text(
            json.dumps({"include": ["src"], "max_file_kb": 128, "engine": "deep"}),
            encoding="utf-8",
        )
        cfg = load_config(root=str(self.root))
        self.assertEqual(cfg.include, ["src"])
        self.assertEqual(cfg.max_file_kb, 128)
        self.assertEqual(cfg.engine, "deep")

    def test_env_overrides_file(self):
        (self.root / "codegraph.json").write_text(
            json.dumps({"engine": "deep", "max_file_kb": 128}), encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ,
            {"CODEGRAPH_ENGINE": "quick", "CODEGRAPH_MAX_FILE_KB": "64"},
        ):
            cfg = load_config(root=str(self.root))
        self.assertEqual(cfg.engine, "quick")
        self.assertEqual(cfg.max_file_kb, 64)

    def test_include_as_string_is_rejected(self):
        # a string include would be iterated char-by-char by the walker
        (self.root / "codegraph.json").write_text(
            json.dumps({"include": "src"}), encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            load_config(root=str(self.root))

    def test_exclude_as_string_is_rejected(self):
        (self.root / "codegraph.json").write_text(
            json.dumps({"exclude": ".git"}), encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            load_config(root=str(self.root))

    def test_bad_engine_falls_back_to_auto(self):
        with mock.patch.dict(os.environ, {"CODEGRAPH_ENGINE": "bogus"}):
            cfg = load_config(root=str(self.root))
        self.assertEqual(cfg.engine, "auto")

    def test_nonpositive_max_file_kb_falls_back(self):
        with mock.patch.dict(os.environ, {"CODEGRAPH_MAX_FILE_KB": "-5"}):
            cfg = load_config(root=str(self.root))
        self.assertEqual(cfg.max_file_kb, 512)

    def test_relative_db_anchored_at_root(self):
        (self.root / "codegraph.json").write_text(
            json.dumps({"db_path": "idx/cg.sqlite"}), encoding="utf-8",
        )
        cfg = load_config(root=str(self.root))
        self.assertEqual(Path(cfg.db_path).resolve(),
                         (self.root / "idx" / "cg.sqlite").resolve())


class WriteDefaultConfigTest(unittest.TestCase):
    def test_writes_parseable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_default_config(root)
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["root"], ".")
            self.assertEqual(len(data["exclude"]), len(DEFAULT_EXCLUDES))
            # a freshly written config must load and discover files normally
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            found = discover_files(root, cfg)
            self.assertEqual([p.name for p in found], ["a.py"])


if __name__ == "__main__":
    unittest.main()