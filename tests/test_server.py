"""Tests for the stdio MCP server (server.mcp + server.handlers)."""

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from codegraph import __version__
from codegraph.config import load_config
from codegraph.server.mcp import run_stdio

from .fixtures import PROJ


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        shutil.copytree(PROJ, self.root)
        self.cfg = load_config(root=str(self.root))
        self.cfg.engine = "quick"
        # index the project so the server can answer queries
        from codegraph.builder import build_index

        build_index(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, messages):
        """Feed JSON-RPC messages (dicts or strings) through the stdio loop."""
        payload = "\n".join(
            m if isinstance(m, str) else json.dumps(m) for m in messages
        ).encode("utf-8")
        out = io.BytesIO()
        log = io.BytesIO()
        run_stdio(io.BytesIO(payload), out, log, self.cfg)
        return [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines() if line]

    def _msg(self, mid, method, params=None):
        return {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}

    def test_handshake_and_tools_list(self):
        replies = self._run(
            [
                self._msg(1, "initialize", {"protocolVersion": "2024-11-05",
                                            "capabilities": {}, "clientInfo": {"name": "t"}}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                self._msg(2, "tools/list"),
            ]
        )
        self.assertEqual(replies[0]["id"], 1)
        result = replies[0]["result"]
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertEqual(result["serverInfo"]["name"], "codegraph")
        self.assertEqual(result["serverInfo"]["version"], __version__)
        self.assertIn("tools", result["capabilities"])
        names = [t["name"] for t in replies[1]["result"]["tools"]]
        self.assertEqual(
            sorted(names),
            ["callees", "callers", "dependents", "deps", "impact", "overview", "reindex", "search"],
        )
        # every tool declares an inputSchema
        for t in replies[1]["result"]["tools"]:
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_protocol_version_negotiation(self):
        replies = self._run([self._msg(1, "initialize", {"protocolVersion": "2030-01-01"})])
        self.assertIn(replies[0]["result"]["protocolVersion"], ("2024-11-05", "2025-03-26"))

    def test_callers_tool(self):
        replies = self._run([self._msg(1, "tools/call", {
            "name": "callers", "arguments": {"symbol": "pkg.pricing.price"}})])
        result = replies[0]["result"]
        self.assertFalse(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("pkg.pricing.discount", text)
        self.assertIn("pkg.cart.Cart.total", text)
        # canonical JSON value carries structured rows
        rows = result["content"][1]["json"]
        self.assertEqual(len(rows), 2)

    def test_search_tool_and_overview(self):
        replies = self._run([
            self._msg(1, "tools/call", {"name": "search", "arguments": {"query": "discount"}}),
            self._msg(2, "tools/call", {"name": "overview", "arguments": {}}),
        ])
        self.assertIn("pkg.pricing.discount", replies[0]["result"]["content"][0]["text"])
        stats = replies[1]["result"]["content"][1]["json"]
        self.assertEqual(stats["files"], 14)

    def test_unknown_tool_is_protocol_error(self):
        replies = self._run([self._msg(1, "tools/call", {"name": "ghost", "arguments": {}})])
        self.assertIn("error", replies[0])
        self.assertEqual(replies[0]["error"]["code"], -32602)

    def test_invalid_arguments_are_tool_errors(self):
        replies = self._run([self._msg(1, "tools/call", {
            "name": "callers", "arguments": {}})])
        result = replies[0]["result"]
        self.assertTrue(result["isError"])
        self.assertIn("symbol", result["content"][0]["text"])

    def test_unknown_method(self):
        replies = self._run([self._msg(1, "mystery/method")])
        self.assertEqual(replies[0]["error"]["code"], -32601)

    def test_malformed_json_does_not_kill_server(self):
        payload = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\nnot-json\n'
        out = io.BytesIO()
        log = io.BytesIO()
        run_stdio(io.BytesIO(payload), out, log, self.cfg)
        lines = out.getvalue().decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1)  # only the valid request answered
        self.assertEqual(json.loads(lines[0])["result"]["tools"][0]["name"], "callers")

    def test_ping(self):
        replies = self._run([self._msg(1, "ping")])
        self.assertEqual(replies[0]["result"], {})

    def test_deps_tool_works(self):
        """Regression: deps used to crash with a KeyError in its renderer."""
        replies = self._run([self._msg(1, "tools/call", {
            "name": "deps", "arguments": {"module": "pkg.cart"}})])
        result = replies[0]["result"]
        self.assertFalse(result["isError"], result)
        text = result["content"][0]["text"]
        self.assertIn("pkg/__init__.py", text)
        self.assertIn("(external)", text)
        rows = result["content"][1]["json"]
        self.assertEqual(len(rows), 2)

    def test_unindexed_project_gets_friendly_error(self):
        """Regression: a fresh root used to surface a raw sqlite error."""
        fresh = Path(self.tmp.name) / "fresh"
        fresh.mkdir()
        (fresh / "a.py").write_text("x = 1\n", encoding="utf-8")
        cfg2 = load_config(root=str(fresh))
        cfg2.engine = "quick"
        msg = self._msg(1, "tools/call", {"name": "overview", "arguments": {}})
        out = io.BytesIO()
        log = io.BytesIO()
        run_stdio(io.BytesIO(json.dumps(msg).encode("utf-8")), out, log, cfg2)
        reply = json.loads(out.getvalue().decode("utf-8"))
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("codegraph index", reply["result"]["content"][0]["text"])

    def test_reindex_invalidates_query_cache(self):
        from codegraph.server.handlers import ToolContext, execute_tool

        ctx = ToolContext(self.cfg)
        value, _ = execute_tool("callers", {"symbol": "pkg.pricing.price"}, ctx)
        self.assertEqual(len(value), 2)  # primed the cache

        target = self.root / "pkg" / "pricing.py"
        target.write_text(
            target.read_text(encoding="utf-8")
            + "\ndef vat(x):\n    return price(x)\n",
            encoding="utf-8",
        )
        execute_tool("reindex", {}, ctx)
        value, _ = execute_tool("callers", {"symbol": "pkg.pricing.price"}, ctx)
        qualnames = [r["qualname"] for r in value]
        self.assertIn("pkg.pricing.vat", qualnames)
        self.assertEqual(len(value), 3)

    def test_output_has_no_embedded_newlines(self):
        replies = self._run([self._msg(1, "tools/list")])
        out = io.BytesIO()
        log = io.BytesIO()
        run_stdio(io.BytesIO(json.dumps(self._msg(1, "tools/list")).encode()), out, log, self.cfg)
        raw = out.getvalue().decode("utf-8")
        self.assertNotIn("\r", raw)
        for line in raw.splitlines():
            self.assertEqual(len(line), len(line.rstrip("\n")))


if __name__ == "__main__":
    unittest.main()
