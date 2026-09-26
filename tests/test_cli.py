"""End-to-end smoke tests running the CLI in a subprocess."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from .fixtures import PROJ

SRC = Path(__file__).resolve().parent.parent / "src"


def _env():
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("CODEGRAPH_")
    }
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run(args, cwd=None, input_bytes=None):
    return subprocess.run(
        [sys.executable, "-m", "codegraph", *args],
        cwd=cwd,
        env=_env(),
        input=input_bytes,
        capture_output=True,
        timeout=120,
    )


class CliSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        shutil.copytree(PROJ, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_subprocess_environment_drops_codegraph_overrides(self):
        with mock.patch.dict(
            os.environ,
            {
                "CODEGRAPH_ROOT": "/external/project",
                "CODEGRAPH_DB": "/external/index.sqlite",
                "CODEGRAPH_ENGINE": "deep",
                "CODEGRAPH_SENTINEL": "remove-me",
                "KEEP_ME": "preserved",
                "PYTHONPATH": "existing-path",
            },
        ):
            with mock.patch("subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
                _run(["--version"], cwd=self.tmp.name)
            env = run.call_args.kwargs["env"]

        self.assertFalse(any(key.startswith("CODEGRAPH_") for key in env))
        self.assertEqual(env["KEEP_ME"], "preserved")
        self.assertEqual(env["PYTHONPATH"], str(SRC) + os.pathsep + "existing-path")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")

    def test_init_writes_config(self):
        proc = _run(["init", "--root", str(self.root)], cwd=self.tmp.name)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertTrue((self.root / "codegraph.json").exists())

    def test_init_does_not_overwrite_existing_config(self):
        config_path = self.root / "codegraph.json"
        original = '{"engine": "deep", "include": ["src"]}\n'
        config_path.write_text(original, encoding="utf-8")

        proc = _run(["init", "--root", str(self.root)], cwd=self.tmp.name)

        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            "configuration already exists",
            proc.stderr.decode("utf-8", "replace"),
        )
        self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_common_options_before_subcommand(self):
        proc = _run(["index", "--root", str(self.root)], cwd=self.tmp.name)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

        proc = _run(["--root", str(self.root), "status"], cwd=self.tmp.name)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn(f"root: {self.root}", proc.stdout.decode("utf-8", "replace"))

    def test_index_then_queries(self):
        proc = _run(["index", "--root", str(self.root)], cwd=self.tmp.name)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn(b"14 files", proc.stdout)

        proc = _run(["callers", "pkg.pricing.price", "--root", str(self.root)])
        self.assertEqual(proc.returncode, 0)
        out = proc.stdout.decode("utf-8", "replace")
        self.assertIn("pkg.pricing.discount", out)
        self.assertIn("pkg.cart.Cart.total", out)

        proc = _run(["deps", "pkg.cart", "--root", str(self.root)])
        self.assertEqual(proc.returncode, 0)
        out = proc.stdout.decode("utf-8", "replace")
        self.assertIn("pkg -> pkg/__init__.py", out)
        self.assertIn("os -> (external)", out)

        proc = _run(["search", "shopping cart", "--root", str(self.root)])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("pkg.cart.Cart", proc.stdout.decode("utf-8", "replace"))

        proc = _run(["status", "--root", str(self.root)])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("files", proc.stdout.decode("utf-8", "replace"))

    def test_index_json_output(self):
        proc = _run(["index", "--root", str(self.root), "--json"], cwd=self.tmp.name)
        self.assertEqual(proc.returncode, 0)
        report = json.loads(proc.stdout.decode("utf-8"))
        self.assertTrue(report["complete"])
        self.assertEqual(report["files_scanned"], 14)
        self.assertEqual(report["files_changed"], 14)

    def test_incomplete_forced_index_exits_with_failure(self):
        from codegraph.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch(
            "codegraph.builder.build_index",
            return_value=mock.Mock(complete=False),
        ):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = main([
                    "index", "--root", str(self.root), "--force", "--json"
                ])

        self.assertEqual(result, 1)
        self.assertIn("forced index rebuild incomplete", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")

    def test_export_dot(self):
        _run(["index", "--root", str(self.root)], cwd=self.tmp.name)
        proc = _run(["export", "dot", "--root", str(self.root), "-o", str(self.tmp.name) + "/g.dot"])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        dot = Path(self.tmp.name, "g.dot").read_text(encoding="utf-8")
        self.assertIn("digraph", dot)
        self.assertIn("->", dot)

    def test_serve_stdio_subprocess(self):
        _run(["index", "--root", str(self.root)], cwd=self.tmp.name)
        init = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "smoke", "version": "1"}},
        })
        call = json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "callers", "arguments": {"symbol": "helper.Greet"}},
        })
        payload = f"{init}\n{call}\n".encode("utf-8")
        proc = _run(["serve", "--root", str(self.root)], cwd=self.tmp.name, input_bytes=payload)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        lines = proc.stdout.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["result"]["serverInfo"]["name"], "codegraph")
        second = json.loads(lines[1])
        self.assertIn("main.main", second["result"]["content"][0]["text"])

    def test_errors_to_stderr_and_exit_code(self):
        proc = _run(["callers", "pkg.pricing.price", "--root", str(self.root)])
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(proc.stderr.decode("utf-8", "replace"))

    def test_missing_explicit_config_is_reported(self):
        config_path = self.root.parent / "missing.json"
        proc = _run(["status", "--root", str(self.root), "--config", str(config_path)])
        self.assertEqual(proc.returncode, 1)
        stderr = proc.stderr.decode("utf-8", "replace")
        self.assertIn("cannot load configuration", stderr)
        self.assertIn(str(config_path), stderr)

    def test_invalid_config_field_types_are_reported_without_traceback(self):
        config_path = self.root.parent / "invalid.json"
        for payload in (
            {"root": None},
            {"db_path": None},
            {"db_path": ""},
            {"language_map": []},
        ):
            with self.subTest(payload=payload):
                config_path.write_text(json.dumps(payload), encoding="utf-8")
                with mock.patch.dict(
                    os.environ, {"CODEGRAPH_ROOT": "", "CODEGRAPH_DB": ""}
                ):
                    proc = _run(
                        ["status", "--config", str(config_path)],
                        cwd=self.tmp.name,
                    )
                self.assertEqual(proc.returncode, 1)
                stderr = proc.stderr.decode("utf-8", "replace")
                self.assertIn("cannot load configuration", stderr)
                self.assertNotIn("Traceback", stderr)

    def test_db_flag_overrides_invalid_config_value(self):
        config_path = self.root.parent / "invalid-db.json"
        config_path.write_text(json.dumps({"db_path": None}), encoding="utf-8")
        db_path = self.root.parent / "override.sqlite"

        proc = _run(
            ["index", "--config", str(config_path), "--db", str(db_path)],
            cwd=self.tmp.name,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertTrue(db_path.exists())

    def test_db_flag_overrides_env_and_empty_config_value(self):
        config_path = self.root.parent / "empty-db.json"
        config_path.write_text(json.dumps({"db_path": ""}), encoding="utf-8")
        env_db_path = self.root.parent / "env-override.sqlite"
        explicit_db_path = self.root.parent / "cli-override.sqlite"

        with mock.patch.dict(os.environ, {"CODEGRAPH_DB": str(env_db_path)}):
            proc = _run(
                [
                    "index", "--config", str(config_path), "--db",
                    str(explicit_db_path), "--root", str(self.root),
                ],
                cwd=self.tmp.name,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertTrue(explicit_db_path.exists())
        self.assertFalse(env_db_path.exists())


if __name__ == "__main__":
    unittest.main()
