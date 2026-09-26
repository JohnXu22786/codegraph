"""Read-side query API: callers, callees, dependencies, search, impact."""

from __future__ import annotations

import sqlite3
from functools import wraps

from .store import IndexStore


def _consistent_snapshot(func):
    @wraps(func)
    def wrapped(store, *args, **kwargs):
        with store.read_snapshot():
            return func(store, *args, **kwargs)
    return wrapped


def _find_symbol(store: IndexStore, symbol: str):
    """Resolve a user-supplied symbol name to a row (qualname first, then
    a unique bare name). Returns None when ambiguous or unknown."""
    if not symbol:
        return None
    row = store.symbol_by_qualname(symbol)
    if row:
        return row
    rows = store.symbols_by_name(symbol, limit=2)
    if len(rows) == 1:
        return rows[0]
    return None


def _resolve_module_targets(store: IndexStore, module: str):
    """Resolve a file path to one target or a module id to all its files."""
    if not module:
        return [], "module", module
    row = store.file_by_path(module)
    if row:
        return [row], "file", row["id"]
    row = store.file_by_path(module + ".py")  # bare "pkg.cart" style
    if row:
        return [row], "file", row["id"]
    rows = store.conn.execute(
        "SELECT * FROM files WHERE module = ? ORDER BY id", (module,)
    ).fetchall()
    return rows, "module", module


def _resolve_module_files(store: IndexStore, module: str):
    """Map a path to one file or a module id to all matching files."""
    return _resolve_module_targets(store, module)[0]


@_consistent_snapshot
def query_callers(store: IndexStore, symbol: str, limit: int = 100):
    """Symbols that call ``symbol`` directly (callers of callers via impact)."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    sym = _find_symbol(store, symbol)
    if sym is None:
        return []
    rows = store.conn.execute(
        "SELECT s.qualname, s.kind, s.start_line, s.end_line, f.path, "
        "       c.callee, c.line "
        "FROM calls c JOIN symbols s ON s.id = c.caller_id "
        "JOIN files f ON f.id = c.file_id "
        "WHERE c.callee_id = ? ORDER BY s.qualname, c.line LIMIT ?",
        (sym["id"], limit),
    )
    return [{"qualname": r["qualname"], "kind": r["kind"], "path": r["path"],
             "symbol_line": r["start_line"], "call_site": f"{r['path']}:{r['line']}",
             "callee": r["callee"], "line": r["line"]} for r in rows]


@_consistent_snapshot
def query_callees(store: IndexStore, symbol: str, limit: int = 100):
    """Everything ``symbol`` calls, resolved or not."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    sym = _find_symbol(store, symbol)
    if sym is None:
        return []
    rows = store.conn.execute(
        "SELECT c.callee, c.callee_id, c.line, f.path, s.qualname AS target "
        "FROM calls c JOIN files f ON f.id = c.file_id "
        "LEFT JOIN symbols s ON s.id = c.callee_id "
        "WHERE c.caller_id = ? ORDER BY c.callee, c.line LIMIT ?",
        (sym["id"], limit),
    )
    return [{"callee": r["callee"], "callee_id": r["callee_id"],
             "resolved": r["callee_id"] is not None,
             "target": r["target"] or "", "path": r["path"], "line": r["line"]}
            for r in rows]


@_consistent_snapshot
def query_deps(store: IndexStore, module: str, limit: int = 200):
    """Modules a file/package imports (its dependencies)."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    files = _resolve_module_files(store, module)
    if not files or limit == 0:
        return []
    variable_limit = 999
    limit_id = getattr(sqlite3, "SQLITE_LIMIT_VARIABLE_NUMBER", None)
    if hasattr(store.conn, "getlimit") and limit_id is not None:
        variable_limit = store.conn.getlimit(limit_id)
    chunk_size = max(1, variable_limit - 1)
    file_ids = [file["id"] for file in files]
    rows = []
    for start in range(0, len(file_ids), chunk_size):
        remaining = limit - len(rows)
        if remaining == 0:
            break
        chunk = file_ids[start:start + chunk_size]
        placeholders = ", ".join("?" for _ in chunk)
        rows.extend(store.conn.execute(
            "SELECT i.module, i.names, i.kind, i.line, f.path AS target_path "
            "FROM imports i LEFT JOIN files f ON f.id = i.target_id "
            f"WHERE i.file_id IN ({placeholders}) "
            "ORDER BY i.file_id, i.line LIMIT ?",
            chunk + [remaining],
        ))
    return [{"module": r["module"], "kind": r["kind"],
             "target_path": r["target_path"] or "", "line": r["line"]} for r in rows]


@_consistent_snapshot
def query_dependents(store: IndexStore, module: str, limit: int = 200):
    """Files/packages that import ``module`` (reverse dependencies).

    Two kinds of link count: imports whose resolved target is one of the
    module's files, and imports that pull the module in by member name
    (``from pkg import pricing`` targets pkg/__init__.py but depends on
    pkg/pricing.py too).
    """
    if limit < 0:
        raise ValueError("limit must be non-negative")
    files, target_kind, target_value = _resolve_module_targets(store, module)
    if not files:
        return []
    target_query = (
        "SELECT id FROM files WHERE id = ?"
        if target_kind == "file"
        else "SELECT id FROM files WHERE module = ?"
    )
    file = files[0]
    rows = store.conn.execute(
        f"WITH targets AS ({target_query}), matched AS ("
        "  SELECT i.id, i.file_id, i.module, i.line, f.path "
        "  FROM imports i JOIN files f ON f.id = i.file_id "
        "  WHERE i.target_id IN (SELECT id FROM targets)"
        ") "
        "SELECT m.path, m.module, m.line "
        "FROM matched m WHERE NOT EXISTS ("
        "  SELECT 1 FROM matched earlier "
        "  WHERE earlier.file_id = m.file_id "
        "    AND (earlier.line < m.line OR "
        "         (earlier.line = m.line AND earlier.id < m.id))"
        ") ORDER BY m.path, m.line, m.id LIMIT ?",
        (target_value, limit),
    )
    results = [{"path": r["path"], "module": r["module"], "line": r["line"]}
               for r in rows]
    seen = {r["path"] for r in results}
    mod = file["module"] or ""
    if "." in mod:
        base, name = mod.rsplit(".", 1)
        # Match plain JSON tokens with instr(); for aliases, normalize JSON's
        # escaped tabs and use GLOB so arbitrary spaces/tabs around ``as``
        # work. Relative imports (module ".") count too when the importing
        # file lives in the same package.
        extra = store.conn.execute(
            "WITH matched AS ("
            "  SELECT i.id, i.file_id, i.module, i.line "
            "  FROM imports i JOIN files impf ON impf.id = i.file_id "
            "  WHERE (instr(i.names, ?) > 0 OR "
            "  replace(i.names, char(92) || 't', ' ') GLOB ?) AND ("
            "    i.module = ? "
            "    OR (i.module GLOB '.*' AND (impf.module = ? OR ("
            "      substr(impf.module, 1, length(?) + 1) = ? || '.'"
            "    )))"
            "  )"
            ") "
            "SELECT f.path, m.module, m.line "
            "FROM matched m JOIN files f ON f.id = m.file_id "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM matched earlier "
            "  WHERE earlier.file_id = m.file_id "
            "    AND (earlier.line < m.line OR "
            "         (earlier.line = m.line AND earlier.id < m.id))"
            ") ORDER BY f.path, m.line LIMIT ?",
            (f'"{name}"', f'*"{name}[ ]*as[ ]*"*', base, base, base, base, limit),
        )
        for r in extra:
            if r["path"] not in seen:
                results.append({"path": r["path"], "module": r["module"],
                                "line": r["line"]})
                seen.add(r["path"])
    # the member-import pass appends past the first LIMIT; cap the union
    return results[:limit]


@_consistent_snapshot
def query_impact(store: IndexStore, symbol: str, depth: int = 3, limit: int = 200):
    """Transitive callers up to ``depth`` hops — who breaks if this changes.

    Each symbol appears once, at its shallowest reachable depth.
    """
    if limit < 0:
        raise ValueError("limit must be non-negative")
    sym = _find_symbol(store, symbol)
    if sym is None:
        return []
    frontier = {sym["id"]}
    visited = set()
    seen = set()
    results = []
    variable_limit = 999
    limit_id = getattr(sqlite3, "SQLITE_LIMIT_VARIABLE_NUMBER", None)
    if hasattr(store.conn, "getlimit") and limit_id is not None:
        variable_limit = store.conn.getlimit(limit_id)
    frontier_chunk_size = max(1, variable_limit - 2)
    for hop in range(1, max(0, depth) + 1):
        if not frontier:
            break
        if len(results) >= limit:
            break
        next_frontier = set()
        frontier_ids = sorted(frontier)
        for start in range(0, len(frontier_ids), frontier_chunk_size):
            chunk = frontier_ids[start:start + frontier_chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            sql = (f"SELECT DISTINCT s.id, s.qualname, s.kind, f.path "
                   f"FROM calls c JOIN symbols s ON s.id = c.caller_id "
                   f"JOIN files f ON f.id = c.file_id "
                   f"WHERE c.callee_id IN ({placeholders}) "
                   "ORDER BY s.id LIMIT ? OFFSET ?")
            offset = 0
            # Continue past already-seen callers when chunks overlap.
            while len(results) < limit:
                remaining = limit - len(results)
                page_size = min(remaining, 1000)
                rows = store.conn.execute(
                    sql, chunk + [page_size, offset]
                ).fetchall()
                if not rows:
                    break
                offset += len(rows)
                for r in rows:
                    if r["id"] in visited or r["id"] in seen:
                        continue
                    results.append({"depth": hop, "qualname": r["qualname"],
                                    "kind": r["kind"], "path": r["path"]})
                    seen.add(r["id"])
                    next_frontier.add(r["id"])
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break
        visited |= frontier
        frontier = next_frontier
    return results


@_consistent_snapshot
def query_search(store: IndexStore, text: str, limit: int = 20):
    """Full-text search over symbol names, docs and signatures."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    return store.search(text, limit)


@_consistent_snapshot
def query_stats(store: IndexStore):
    """Aggregate counts for status / overview."""
    counts = {
        table: store.count_rows(table)
        for table in ("files", "symbols", "calls", "imports")
    }
    langs = {
        r["lang"]: r["n"] for r in store.conn.execute(
            "SELECT lang, COUNT(*) AS n FROM files GROUP BY lang ORDER BY lang")
    }
    unresolved = store.conn.execute(
        "SELECT COUNT(*) AS n FROM calls WHERE callee_id IS NULL"
    ).fetchone()["n"]
    resolved = store.conn.execute(
        "SELECT COUNT(*) AS n FROM imports WHERE target_id IS NOT NULL"
    ).fetchone()["n"]
    return {
        "files": counts["files"],
        "symbols": counts["symbols"],
        "calls": counts["calls"],
        "imports": counts["imports"],
        "calls_resolved": counts["calls"] - unresolved,
        "calls_unresolved": unresolved,
        "imports_resolved": resolved,
        "imports_unresolved": counts["imports"] - resolved,
        "languages": langs,
        "root": store.get_meta("root", ""),
        "last_indexed": store.get_meta("last_indexed"),
    }
