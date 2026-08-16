"""Tool definitions for the MCP server: schemas, execution, rendering."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..builder import build_index
from ..cache import TTLCache
from ..queries import (
    query_callers,
    query_callees,
    query_dependents,
    query_deps,
    query_impact,
    query_search,
    query_stats,
)
from ..store import IndexStore


class ToolError(Exception):
    """A tool-call failure that the model can see and recover from."""


@dataclass
class ToolContext:
    cfg: object
    cache: TTLCache = None

    def __post_init__(self):
        if self.cache is None:
            self.cache = TTLCache(ttl=30.0, max_size=128)

    def store(self) -> IndexStore:
        # read-only tools must not create the database file as a side effect
        if not Path(self.cfg.db_path).exists():
            raise ToolError(
                f"no index found at {self.cfg.db_path} — run 'codegraph index' "
                f"on the project root first"
            )
        return IndexStore(self.cfg.db_path)

    def require_indexed(self, store: IndexStore):
        if store.get_meta("last_indexed") is None:
            raise ToolError(
                f"no index found at {self.cfg.db_path} — run 'codegraph index' "
                f"(or the reindex tool) on the project root first"
            )


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def _require(store, symbol):
    from ..queries import _find_symbol

    sym = _find_symbol(store, symbol)
    if sym is None:
        raise ToolError(f"unknown symbol: {symbol!r} (try the search tool first)")
    return sym


def _require_arg(args, key):
    if not args.get(key):
        raise ToolError(f"missing required argument {key!r}")


def _exec_callers(args, ctx: ToolContext):
    _require_arg(args, "symbol")
    store = ctx.store()
    try:
        ctx.require_indexed(store)
        _require(store, args["symbol"])
        return query_callers(store, args["symbol"], limit=args.get("limit", 100))
    finally:
        store.close()


def _exec_callees(args, ctx: ToolContext):
    _require_arg(args, "symbol")
    store = ctx.store()
    try:
        ctx.require_indexed(store)
        _require(store, args["symbol"])
        return query_callees(store, args["symbol"], limit=args.get("limit", 100))
    finally:
        store.close()


def _exec_deps(args, ctx: ToolContext):
    _require_arg(args, "module")
    store = ctx.store()
    try:
        ctx.require_indexed(store)
        return query_deps(store, args["module"], limit=args.get("limit", 200))
    finally:
        store.close()


def _exec_dependents(args, ctx: ToolContext):
    _require_arg(args, "module")
    store = ctx.store()
    try:
        ctx.require_indexed(store)
        return query_dependents(store, args["module"], limit=args.get("limit", 200))
    finally:
        store.close()


def _exec_search(args, ctx: ToolContext):
    _require_arg(args, "query")
    store = ctx.store()
    try:
        ctx.require_indexed(store)
        return query_search(store, args["query"], limit=args.get("limit", 20))
    finally:
        store.close()


def _exec_impact(args, ctx: ToolContext):
    _require_arg(args, "symbol")
    store = ctx.store()
    try:
        ctx.require_indexed(store)
        _require(store, args["symbol"])
        return query_impact(store, args["symbol"],
                            depth=args.get("depth", 3), limit=args.get("limit", 200))
    finally:
        store.close()


def _exec_overview(args, ctx: ToolContext):
    store = ctx.store()
    try:
        ctx.require_indexed(store)
        return query_stats(store)
    finally:
        store.close()


def _exec_reindex(args, ctx: ToolContext):
    force = bool(args.get("force", False))
    report = build_index(ctx.cfg, force=force, quiet=True)
    ctx.cache.clear()  # stale read results must not outlive a refresh
    return {
        "files_scanned": report.files_scanned,
        "files_changed": report.files_changed,
        "files_skipped": report.files_skipped,
        "files_removed": report.files_removed,
        "symbols": report.symbols,
        "calls": report.calls,
        "imports": report.imports,
    }


# --------------------------------------------------------------------------
# rendering (model-facing text)
# --------------------------------------------------------------------------

def _render_callers(rows):
    if not rows:
        return "no callers found"
    head = "callers"
    lines = [head]
    for r in rows:
        lines.append(f"  {r['qualname']}  ({r['kind']})  {r['call_site']}")
    return "\n".join(lines)


def _render_callees(rows):
    if not rows:
        return "no callees found"
    lines = ["callees"]
    for r in rows:
        target = f" -> {r['target']}" if r["resolved"] else "  (unresolved)"
        lines.append(f"  {r['callee']}{target}  {r['path']}:{r['line']}")
    return "\n".join(lines)


def _render_deps(rows):
    if not rows:
        return "no dependencies found"
    lines = ["dependencies"]
    for r in rows:
        target = f" -> {r['target_path']}" if r["target_path"] else "  (external)"
        lines.append(f"  {r['module']}{target}  (line {r['line']})")
    return "\n".join(lines)


def _render_dependents(rows):
    if not rows:
        return "no dependents found"
    lines = ["dependents"]
    for r in rows:
        lines.append(f"  {r['path']}  (imports {r['module']} at line {r['line']})")
    return "\n".join(lines)


def _render_search(hits):
    if not hits:
        return "no matches"
    lines = ["search results"]
    for h in hits:
        doc = f"  {h['doc']}" if h.get("doc") else ""
        lines.append(f"  {h['qualname']}  ({h['kind']})  {h['path']}:{h['start_line']}{doc}")
    return "\n".join(lines)


def _render_impact(rows):
    if not rows:
        return "no transitive callers"
    lines = ["transitive callers"]
    for r in rows:
        lines.append(f"  d{r['depth']} {r['qualname']}  ({r['kind']})  {r['path']}")
    return "\n".join(lines)


def _render_overview(stats):
    langs = ", ".join(f"{k}:{v}" for k, v in sorted(stats["languages"].items())) or "none"
    return (
        f"index overview\n"
        f"  files {stats['files']} | symbols {stats['symbols']} | "
        f"calls {stats['calls']} | imports {stats['imports']}\n"
        f"  calls resolved {stats['calls_resolved']} / unresolved "
        f"{stats['calls_unresolved']}\n"
        f"  imports resolved {stats['imports_resolved']} / unresolved "
        f"{stats['imports_unresolved']}\n"
        f"  languages: {langs}\n"
        f"  root {stats['root']}\n"
        f"  last indexed {stats['last_indexed']}"
    )


def _render_reindex(report):
    return (
        f"reindex done: {report['files_changed']} changed, "
        f"{report['files_skipped']} skipped, {report['files_removed']} removed "
        f"— {report['symbols']} symbols, {report['calls']} calls, "
        f"{report['imports']} imports"
    )


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


TOOLS = [
    {
        "name": "callers",
        "description": "List every symbol that calls the given symbol directly. "
                       "Use impact for the transitive caller set.",
        "inputSchema": _schema(
            {"symbol": {"type": "string",
                        "description": "qualified name, e.g. pkg.cart.Cart.add"},
             "limit": {"type": "integer"}},
            required=["symbol"],
        ),
        "execute": _exec_callers,
        "render": _render_callers,
    },
    {
        "name": "callees",
        "description": "List everything the given symbol calls, with resolution "
                       "status for each call site.",
        "inputSchema": _schema(
            {"symbol": {"type": "string",
                        "description": "qualified name of the caller symbol"},
             "limit": {"type": "integer"}},
            required=["symbol"],
        ),
        "execute": _exec_callees,
        "render": _render_callees,
    },
    {
        "name": "deps",
        "description": "List the modules a file or package imports (its "
                       "dependencies), marking resolved vs external.",
        "inputSchema": _schema(
            {"module": {"type": "string",
                        "description": "file path (web/util.ts) or module id "
                                       "(pkg.cart)"},
             "limit": {"type": "integer"}},
            required=["module"],
        ),
        "execute": _exec_deps,
        "render": _render_deps,
    },
    {
        "name": "dependents",
        "description": "List the files/packages that import the given module "
                       "(reverse dependencies).",
        "inputSchema": _schema(
            {"module": {"type": "string",
                        "description": "file path or module id of the target"},
             "limit": {"type": "integer"}},
            required=["module"],
        ),
        "execute": _exec_dependents,
        "render": _render_dependents,
    },
    {
        "name": "search",
        "description": "Full-text search over symbol names, docstrings and "
                       "signatures stored in the index.",
        "inputSchema": _schema(
            {"query": {"type": "string", "description": "free-text query"},
             "limit": {"type": "integer"}},
            required=["query"],
        ),
        "execute": _exec_search,
        "render": _render_search,
    },
    {
        "name": "impact",
        "description": "Transitive callers up to a depth: everything that "
                       "would be affected if the symbol changed.",
        "inputSchema": _schema(
            {"symbol": {"type": "string", "description": "qualified symbol name"},
             "depth": {"type": "integer", "default": 3},
             "limit": {"type": "integer"}},
            required=["symbol"],
        ),
        "execute": _exec_impact,
        "render": _render_impact,
    },
    {
        "name": "overview",
        "description": "Index statistics: file/symbol/call/import counts, "
                       "resolution rates, per-language breakdown.",
        "inputSchema": _schema({}),
        "execute": _exec_overview,
        "render": _render_overview,
    },
    {
        "name": "reindex",
        "description": "Refresh the index (incremental by default). Use "
                       "force=true to re-parse every file.",
        "inputSchema": _schema(
            {"force": {"type": "boolean", "default": False}},
        ),
        "execute": _exec_reindex,
        "render": _render_reindex,
        "read_only": False,
    },
]

_TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}
_READ_ONLY = {name for name, t in _TOOLS_BY_NAME.items() if t.get("read_only", True)}


def tool_definitions():
    """MCP tools/list payload (schemas only, no implementation)."""
    return [
        {"name": t["name"], "description": t["description"],
         "inputSchema": t["inputSchema"]}
        for t in TOOLS
    ]


def execute_tool(name: str, args: dict, ctx: ToolContext):
    """Run a tool; returns (value, rendered_text). Raises ToolError."""
    tool = _TOOLS_BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"unknown tool: {name}")
    if name in _READ_ONLY:
        key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
        cached = ctx.cache.get(key)
        if cached is not None:
            value, text = cached
            return value, text
        value = tool["execute"](args, ctx)
        text = tool["render"](value)
        ctx.cache.put(key, (value, text))
        return value, text
    value = tool["execute"](args, ctx)
    return value, tool["render"](value)
