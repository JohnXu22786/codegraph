"""Tests for file discovery, ignore rules and language detection."""

import tempfile
import unittest
from pathlib import Path

from codegraph.config import ProjectConfig, load_config
from codegraph.scanner import languages
from codegraph.scanner.walk import discover_files

from .fixtures import PROJ


class LanguageRegistryTest(unittest.TestCase):
    def test_known_extensions(self):
        cases = {
            ".py": "python",
            ".js": "javascript",
            ".jsx": "javascript",
            ".mjs": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".go": "go",
            ".java": "java",
            ".rs": "rust",
        }
        for ext, lang in cases.items():
            self.assertEqual(languages.lang_for("x" + ext), lang)

    def test_unknown_extension(self):
        self.assertIsNone(languages.lang_for("README.md"))
        self.assertIsNone(languages.lang_for("archive.tar.gz"))
        self.assertIsNone(languages.lang_for("noext"))

    def test_module_id_uses_package_after_leading_comments(self):
        cases = (
            ("service.go", "go", "// Package service handles requests.\n\npackage svc\n", "svc"),
            (
                "example.go",
                "go",
                "// Documentation mentions package fake.\npackage actual\n",
                "actual",
            ),
            (
                "example.go",
                "go",
                "// Documentation\rpackage fake\npackage actual\n",
                "actual",
            ),
            (
                "Calc.java",
                "java",
                "// Leading comment\rpackage com.example.calc;\rclass Calc {}\r",
                "com.example.calc",
            ),
            (
                "Calc.java",
                "java",
                "/**\n * Calculator API.\n */\npackage com.example.calc;\n",
                "com.example.calc",
            ),
            (
                "package-info.java",
                "java",
                "@Deprecated\npackage com.example.api;\n",
                "com.example.api",
            ),
            (
                "package-info.java",
                "java",
                "@org /* qualifier */ . example . Meta\n"
                "package com.example.api;\n",
                "com.example.api",
            ),
            (
                "package-info.java",
                "java",
                '@SuppressWarnings("deprecation")\n'
                '@org.example.Metadata(value = @Nested(" ) "))\n'
                "package com.example.api;\n",
                "com.example.api",
            ),
            (
                "package-info.java",
                "java",
                '@Meta("""\nquote: \\"""\n) still text\n""")\n'
                "package com.example.api;\n",
                "com.example.api",
            ),
            (
                "package-info.java",
                "java",
                '@Meta("""\nquote: \\""""\n)\n'
                "package com.example.api;\n",
                "com.example.api",
            ),
            ("src/Calc.java", "java", "class Calc {}\n", "src/Calc"),
        )
        for rel_path, lang, text, expected in cases:
            with self.subTest(rel_path=rel_path):
                self.assertEqual(languages.module_of(rel_path, lang, text), expected)

    def test_language_override(self):
        cfg = load_config(root=str(PROJ))
        cfg.language_map = {".md": "markdown"}
        self.assertEqual(languages.lang_for("notes.md", cfg.language_map), "markdown")
        self.assertIsNone(languages.lang_for("notes.md"))


class WalkTest(unittest.TestCase):
    def test_default_discovery(self):
        cfg = load_config(root=str(PROJ))
        found = discover_files(PROJ, cfg)
        rel = {p.as_posix() for p in found}
        expected = {
            "app.py",
            "pkg/__init__.py",
            "pkg/pricing.py",
            "pkg/cart.py",
            "web/util.ts",
            "web/logger.ts",
            "web/index.ts",
            "web/app.js",
            "main.go",
            "helper.go",
            "Calc.java",
            "Runner.java",
            "rustx/lib.rs",
            "rustx/main.rs",
        }
        self.assertEqual(rel, expected)

    def test_exclude_pattern(self):
        cfg = load_config(root=str(PROJ))
        cfg.exclude = list(cfg.exclude) + ["web/*"]
        found = discover_files(PROJ, cfg)
        self.assertFalse(any("web/" in p.as_posix() for p in found))
        self.assertTrue(any(p.as_posix() == "app.py" for p in found))

    def test_include_filter(self):
        cfg = load_config(root=str(PROJ))
        cfg.include = ["pkg", "web"]
        found = discover_files(PROJ, cfg)
        rel = {p.as_posix() for p in found}
        self.assertIn("pkg/cart.py", rel)
        self.assertIn("web/index.ts", rel)
        self.assertNotIn("app.py", rel)
        self.assertNotIn("main.go", rel)

    def test_max_file_size_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "small.py").write_text("x = 1\n", encoding="utf-8")
            (root / "big.py").write_text("#" * 5000 + "\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            cfg.max_file_kb = 1  # 5 KB file must be skipped
            found = discover_files(root, cfg)
            rel = {p.name for p in found}
            self.assertEqual(rel, {"small.py"})

    def test_db_dir_excluded_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            (root / ".cg").mkdir()
            (root / ".cg" / "cg.sqlite").write_bytes(b"sqlite")
            cfg = load_config(root=str(root))
            found = discover_files(root, cfg)
            rel = {p.name for p in found}
            self.assertEqual(rel, {"a.py"})

    def test_non_file_entries_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dir.py").mkdir()  # directory named like a source file
            (root / "ok.py").write_text("y = 2\n", encoding="utf-8")
            cfg = load_config(root=str(root))
            found = discover_files(root, cfg)
            self.assertEqual([p.name for p in found], ["ok.py"])


if __name__ == "__main__":
    unittest.main()
