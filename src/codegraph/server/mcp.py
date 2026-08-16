"""Minimal Model Context Protocol server over stdio (standard library only).

Implements the subset of MCP a tool harness needs: initialize handshake,
tools/list, tools/call, ping. Messages are newline-delimited JSON-RPC 2.0
on stdin/stdout; all diagnostics go to stderr. Also speaks plain JSON-RPC,
so any harness that spawns a subprocess tool server can talk to it.
"""

from __future__ import annotations

import json

from .. import __version__
from .handlers import ToolContext, ToolError, execute_tool, tool_definitions

SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26")
DEFAULT_VERSION = "2024-11-05"

_SERVER_NAME = "codegraph"


def _log(log_stream, text: str):
    try:
        log_stream.write(text + "\n")
    except TypeError:
        log_stream.write((text + "\n").encode("utf-8", "replace"))


def _send(output_stream, payload: dict):
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        output_stream.write((line + "\n").encode("utf-8"))
    except BrokenPipeError:
        # the client went away; the caller will observe EOF on stdin
        pass


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def _dispatch(msg: dict, ctx: ToolContext, log_stream) -> "dict | None":
    msg_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _error(msg_id, -32602, "Invalid params: expected an object")

    if method == "initialize":
        requested = params.get("protocolVersion", "")
        version = requested if requested in SUPPORTED_VERSIONS else DEFAULT_VERSION
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": _SERVER_NAME, "version": __version__},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"tools": tool_definitions()}}
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        if name not in {t["name"] for t in tool_definitions()}:
            return _error(msg_id, -32602, f"Unknown tool: {name}")
        try:
            value, text = execute_tool(name, args, ctx)
        except ToolError as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": str(exc)}], "isError": True}}
        except Exception as exc:  # unexpected failures still reach the model
            _log(log_stream, f"tool {name} failed: {exc!r}")
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text",
                             "text": f"internal error while running {name}: {exc}"}],
                "isError": True}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text", "text": text},
                        {"type": "json", "json": value}],
            "isError": False}}
    return _error(msg_id, -32601, f"Method not found: {method}")


def run_stdio(input_stream, output_stream, log_stream, cfg):
    """Serve one process lifetime: read messages until EOF, then exit.

    ``input_stream`` / ``output_stream`` are binary streams; ``log_stream``
    may be binary or text.
    """
    ctx = ToolContext(cfg)
    _log(log_stream, f"{_SERVER_NAME} server v{__version__} ready "
                     f"(root={cfg.root})")
    for raw in input_stream:
        if not raw or not raw.strip():
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            _log(log_stream, f"dropping invalid JSON-RPC message: {exc}")
            continue
        if not isinstance(msg, dict) or "id" not in msg:
            continue  # notification (e.g. notifications/initialized)
        try:
            response = _dispatch(msg, ctx, log_stream)
        except Exception as exc:
            _log(log_stream, f"dispatch failed: {exc!r}")
            response = _error(msg.get("id"), -32603, f"Internal error: {exc}")
        if response is not None:
            _send(output_stream, response)
    try:
        output_stream.flush()
    except (BrokenPipeError, OSError):
        pass  # client already gone
    _log(log_stream, f"{_SERVER_NAME} server exiting (stdin closed)")
