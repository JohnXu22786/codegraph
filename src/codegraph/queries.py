"""Read-side query API: callers, callees, dependencies, search, impact."""

from __future__ import annotations

from .store import IndexStore


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


def _resolve_module_arg(store: IndexStore, module: str):
    """Map a user-supplied module (path or module id) to a file row."""
    if not module:
        return None
    row = store.file_by_path(module)
    if row:
        return row
    row = store.file_by_path(module + ".py")  # bare "pkg.cart" style
    if row:
        return row
    return store.file_by_module(module)


def _resolve_module_files(store: IndexStore, module: str):
    """Map a path to one file or a module id to all matching files."""
    if not module:
        return []
    row = store.file_by_path(module)
    if row:
        return [row]
    row = store.file_by_path(module + ".py")
    if row:
        return [row]
    return store.conn.execute(
        "SELECT * FROM files WHERE module = ? ORDER BY id", (module,)
    ).fetchall()


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


def query_deps(store: IndexStore, module: str, limit: int = 200):
    """Modules a file/package imports (its dependencies)."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    files = _resolve_module_files(store, module)
    if not files:
        return []
    placeholders = ", ".join("?" for _ in files)
    rows = store.conn.execute(
        "SELECT i.module, i.names, i.kind, i.line, f.path AS target_path "
        "FROM imports i LEFT JOIN files f ON f.id = i.target_id "
        f"WHERE i.file_id IN ({placeholders}) ORDER BY i.file_id, i.line LIMIT ?",
        [file["id"] for file in files] + [limit],
    )
    return [{"module": r["module"], "kind": r["kind"],
             "target_path": r["target_path"] or "", "line": r["line"]} for r in rows]


def query_dependents(store: IndexStore, module: str, limit: int = 200):
    """Files/packages that import ``module`` (reverse dependencies).

    Two kinds of link count: imports whose resolved target is the module's
    file, and imports that pull the module in by member name
    (``from pkg import pricing`` targets pkg/__init__.py but depends on
    pkg/pricing.py too).
    """
    if limit < 0:
        raise ValueError("limit must be non-negative")
    file = _resolve_module_arg(store, module)
    if file is None:
        return []
    rows = store.conn.execute(
        "SELECT f.path, i.module, i.line "
        "FROM imports i JOIN files f ON f.id = i.file_id "
        "WHERE i.target_id = ? AND NOT EXISTS ("
        "  SELECT 1 FROM imports earlier "
        "  WHERE earlier.file_id = i.file_id "
        "    AND earlier.target_id = i.target_id "
        "    AND (earlier.line < i.line OR "
        "         (earlier.line = i.line AND earlier.id < i.id))"
        ") ORDER BY f.path, i.line LIMIT ?",
        (file["id"], limit),
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
    for hop in range(1, max(0, depth) + 1):
        if not frontier:
            break
        remaining = limit - len(results)
        if remaining <= 0:
            break
        placeholders = ",".join("?" for _ in frontier)
        sql = (f"SELECT DISTINCT s.id, s.qualname, s.kind, f.path "
               f"FROM calls c JOIN symbols s ON s.id = c.caller_id "
               f"JOIN files f ON f.id = c.file_id "
               f"WHERE c.callee_id IN ({placeholders})")
        params = list(frontier)
        excluded = visited | seen
        if excluded:
            excluded_ph = ",".join("?" for _ in excluded)
            sql += f" AND s.id NOT IN ({excluded_ph})"
            params += list(excluded)
        sql += " LIMIT ?"
        params.append(remaining)
        rows = store.conn.execute(sql, params)
        next_frontier = set()
        for r in rows:
            if r["id"] in seen:  # cycles: keep the shallowest occurrence
                continue
            results.append({"depth": hop, "qualname": r["qualname"],
                            "kind": r["kind"], "path": r["path"]})
            seen.add(r["id"])
            next_frontier.add(r["id"])
        visited |= frontier
        frontier = next_frontier
    return results


def query_search(store: IndexStore, text: str, limit: int = 20):
    """Full-text search over symbol names, docs and signatures."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    return store.search(text, limit)


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
