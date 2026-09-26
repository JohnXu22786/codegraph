"""Tests for config loading: file merging, env overrides, validation."""

import errno
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
        cfg = default_config("C:\\proj" if os.name == "nt" else "/C:/proj")
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

    def test_explicit_missing_config_file_is_an_error(self):
        config_path = self.root / "missing.json"
        with self.assertRaisesRegex(FileNotFoundError, str(config_path)):
            load_config(root=str(self.root), config_path=str(config_path))

    def test_file_values_apply(self):
        (self.root / "codegraph.json").write_text(
            json.dumps({"include": ["src"], "max_file_kb": 128, "engine": "deep"}),
            encoding="utf-8",
        )
        cfg = load_config(root=str(self.root))
        self.assertEqual(cfg.include, ["src"])
        self.assertEqual(cfg.max_file_kb, 128)
        self.assertEqual(cfg.engine, "deep")

    def test_non_object_top_level_json_is_rejected(self):
        config_path = self.root / "codegraph.json"
        for payload in (None, [], "config", 0, False):
            with self.subTest(payload=payload):
                config_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "must contain a JSON object"):
                    load_config(root=str(self.root))

    def test_root_must_be_a_string(self):
        config_path = self.root / "codegraph.json"
        for value in (None, [], 0, False):
            with self.subTest(value=value):
                config_path.write_text(
                    json.dumps({"root": value}), encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, '"root" must be a string'):
                    with mock.patch.dict(os.environ, {"CODEGRAPH_ROOT": ""}):
                        load_config(config_path=str(config_path))

    def test_scoped_root_ignores_invalid_file_root(self):
        config_path = self.root / "codegraph.json"
        config_path.write_text(json.dumps({"root": None}), encoding="utf-8")

        explicit = load_config(root=str(self.root))
        self.assertEqual(explicit.root, str(self.root.resolve()))

        with mock.patch.dict(os.environ, {"CODEGRAPH_ROOT": str(self.root)}):
            environment = load_config()
        self.assertEqual(environment.root, str(self.root.resolve()))

    def test_db_path_must_be_a_string(self):
        config_path = self.root / "codegraph.json"
        for value in (None, [], 0, False):
            with self.subTest(value=value):
                config_path.write_text(
                    json.dumps({"db_path": value}), encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    ValueError, '"db_path" must be a string'
                ):
                    with mock.patch.dict(os.environ, {"CODEGRAPH_DB": ""}):
                        load_config(root=str(self.root))

    def test_empty_effective_db_path_is_rejected(self):
        (self.root / "codegraph.json").write_text(
            json.dumps({"db_path": ""}), encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"CODEGRAPH_DB": ""}):
            with self.assertRaisesRegex(
                ValueError, '"db_path" must not be empty'
            ):
                load_config(root=str(self.root))

    def test_env_db_overrides_invalid_file_value(self):
        db_path = self.root / "override.sqlite"
        for value in (None, ""):
            with self.subTest(value=value):
                (self.root / "codegraph.json").write_text(
                    json.dumps({"db_path": value}), encoding="utf-8",
                )
                with mock.patch.dict(os.environ, {"CODEGRAPH_DB": str(db_path)}):
                    cfg = load_config(root=str(self.root))
                self.assertEqual(cfg.db_path, str(db_path.resolve()))

    def test_explicit_db_path_overrides_invalid_file_value(self):
        env_db_path = self.root / "env.sqlite"
        explicit_db_path = self.root / "explicit.sqlite"
        for value in (None, ""):
            with self.subTest(value=value):
                (self.root / "codegraph.json").write_text(
                    json.dumps({"db_path": value}), encoding="utf-8",
                )
                with mock.patch.dict(
                    os.environ, {"CODEGRAPH_DB": str(env_db_path)}
                ):
                    cfg = load_config(
                        root=str(self.root), db_path=str(explicit_db_path)
                    )
                self.assertEqual(
                    cfg.db_path, str(explicit_db_path.resolve())
                )

    def test_language_map_must_map_strings_to_strings(self):
        config_path = self.root / "codegraph.json"
        for value in (None, [], 0, False):
            with self.subTest(value=value):
                config_path.write_text(
                    json.dumps({"language_map": value}), encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    ValueError, '"language_map" must be an object'
                ):
                    load_config(root=str(self.root))

        config_path.write_text(
            json.dumps({"language_map": {".x": []}}), encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ValueError, '"language_map" must map strings to strings'
        ):
            load_config(root=str(self.root))

    def test_incremental_false_is_preserved(self):
        (self.root / "codegraph.json").write_text(
            json.dumps({"incremental": False}), encoding="utf-8",
        )
        self.assertFalse(load_config(root=str(self.root)).incremental)

    def test_incremental_string_false_is_rejected(self):
        (self.root / "codegraph.json").write_text(
            json.dumps({"incremental": "false"}), encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, '"incremental" must be a boolean'):
            load_config(root=str(self.root))

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

    def test_scoped_root_precedes_file_root(self):
        explicit_root = self.root / "explicit"
        environment_root = self.root / "environment"
        for selected_root in (explicit_root, environment_root):
            selected_root.mkdir()
            (selected_root / "codegraph.json").write_text(
                json.dumps({"root": "file-root"}), encoding="utf-8",
            )

        cases = (
            (str(explicit_root), ""),
            (None, str(environment_root)),
        )
        for requested_root, environment_value in cases:
            with self.subTest(requested_root=requested_root):
                with mock.patch.dict(
                    os.environ, {"CODEGRAPH_ROOT": environment_value}
                ):
                    cfg = load_config(root=requested_root)
                expected_root = Path(requested_root or environment_value).resolve()
                self.assertEqual(cfg.root, str(expected_root))

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

    def test_non_integer_max_file_kb_is_rejected(self):
        config_path = self.root / "codegraph.json"
        for value in ("512", 1.5, True, False, None, [], {}):
            with self.subTest(value=value):
                config_path.write_text(
                    json.dumps({"max_file_kb": value}), encoding="utf-8",
                )
                with mock.patch.dict(os.environ, {"CODEGRAPH_MAX_FILE_KB": ""}):
                    with self.assertRaisesRegex(
                        ValueError, '"max_file_kb" must be an integer'
                    ):
                        load_config(root=str(self.root))

    def test_env_max_file_kb_overrides_invalid_file_value(self):
        (self.root / "codegraph.json").write_text(
            json.dumps({"max_file_kb": "invalid"}), encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"CODEGRAPH_MAX_FILE_KB": "64"}):
            cfg = load_config(root=str(self.root))
        self.assertEqual(cfg.max_file_kb, 64)

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

    def test_does_not_overwrite_existing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "codegraph.json"
            original = '{"engine": "deep"}\n'
            path.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(
                FileExistsError, "configuration already exists"
            ):
                write_default_config(root)

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(root.iterdir()), [path])

    def test_failed_publish_leaves_no_partial_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "codegraph.json"

            with mock.patch(
                "codegraph.config.os.link",
                side_effect=OSError(errno.EIO, "link failed"),
            ):
                with self.assertRaisesRegex(OSError, "link failed"):
                    write_default_config(root)

            self.assertFalse(path.exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_unsupported_links_fail_safely_or_use_windows_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "codegraph.json"

            with mock.patch(
                "codegraph.config.os.link",
                side_effect=OSError(
                    getattr(errno, "EOPNOTSUPP", errno.EPERM),
                    "links unsupported",
                ),
            ):
                if os.name == "nt":
                    self.assertEqual(write_default_config(root), path)
                else:
                    with self.assertRaisesRegex(
                        OSError, "cannot safely create configuration"
                    ):
                        write_default_config(root)

            if os.name == "nt":
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["root"], ".")
                self.assertEqual(list(root.iterdir()), [path])
            else:
                self.assertFalse(path.exists())
                self.assertEqual(list(root.iterdir()), [])

    def test_unsupported_links_do_not_overwrite_existing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "codegraph.json"
            original = '{"engine": "deep"}\n'
            path.write_text(original, encoding="utf-8")

            with mock.patch(
                "codegraph.config.os.link",
                side_effect=OSError(
                    getattr(errno, "EOPNOTSUPP", errno.EPERM),
                    "links unsupported",
                ),
            ):
                with self.assertRaisesRegex(
                    FileExistsError, "configuration already exists"
                ):
                    write_default_config(root)

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(root.iterdir()), [path])

    def test_windows_rename_collision_preserves_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "codegraph.json"
            original = '{"engine": "deep"}\n'

            def create_config_then_raise(_source, destination):
                destination.write_text(original, encoding="utf-8")
                raise FileExistsError("destination created concurrently")

            with mock.patch(
                "codegraph.config.os.link",
                side_effect=OSError(errno.EPERM, "links unsupported"),
            ), mock.patch("codegraph.config.IS_WINDOWS", True), mock.patch(
                "codegraph.config.os.rename",
                side_effect=create_config_then_raise,
            ):
                with self.assertRaisesRegex(
                    FileExistsError, "configuration already exists"
                ):
                    write_default_config(root)

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(root.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
