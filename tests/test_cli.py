"""End-to-end smoke tests running the CLI in a subprocess."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .fixtures import PROJ

SRC = Path(__file__).resolve().parent.parent / "src"


def _env():
    env = os.environ.copy()
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

    def test_init_writes_config(self):
        proc = _run(["init", "--root", str(self.root)], cwd=self.tmp.name)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertTrue((self.root / "codegraph.json").exists())

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
        self.assertEqual(report["files_scanned"], 14)
        self.assertEqual(report["files_changed"], 14)

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
                       "clientInfo": {"name": "smoke"}},
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


if __name__ == "__main__":
    unittest.main()
