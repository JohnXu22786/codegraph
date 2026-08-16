"""Export the index as JSON or Graphviz DOT for external visualisation."""

from __future__ import annotations

from .store import IndexStore


def export_json(store: IndexStore) -> dict:
    """Dump the whole index as one JSON-serialisable dictionary."""
    files = [dict(r) for r in store.conn.execute(
        "SELECT path, lang, module, lines FROM files ORDER BY path")]
    symbols = [dict(r) for r in store.conn.execute(
        "SELECT s.qualname, s.name, s.kind, s.parent, s.start_line, s.end_line, "
        "s.signature, f.path AS file "
        "FROM symbols s JOIN files f ON f.id = s.file_id ORDER BY s.qualname")]
    calls = [dict(r) for r in store.conn.execute(
        "SELECT c.caller_name AS caller, c.callee, c.callee_id, c.line, "
        "f.path AS file FROM calls c JOIN files f ON f.id = c.file_id "
        "ORDER BY f.path, c.line")]
    imports = [dict(r) for r in store.conn.execute(
        "SELECT i.module, i.kind, i.line, f.path AS file, t.path AS target_path "
        "FROM imports i JOIN files f ON f.id = i.file_id "
        "LEFT JOIN files t ON t.id = i.target_id ORDER BY f.path, i.line")]
    meta = {
        "root": store.get_meta("root", ""),
        "last_indexed": store.get_meta("last_indexed"),
    }
    return {"meta": meta, "files": files, "symbols": symbols,
            "calls": calls, "imports": imports}


def _esc(text) -> str:
    """Escape a string for use inside a DOT double-quoted label."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def export_dot(store: IndexStore) -> str:
    """Render the graph as Graphviz DOT source.

    Symbol nodes are labelled with their qualified names; resolved calls are
    solid edges, unresolved calls are dashed edges to a pseudo-node, and
    module imports appear as file-level edges.
    """
    lines = ["digraph codegraph {"]

    symbol_files = {}
    for r in store.conn.execute("SELECT id, file_id, qualname, kind FROM symbols"):
        symbol_files[r["id"]] = (r["file_id"], r["qualname"], r["kind"])

    file_labels = {}
    for r in store.conn.execute("SELECT id, path FROM files"):
        file_labels[r["id"]] = r["path"]

    # nodes grouped per file for visual clustering
    by_file = {}
    for sid, (fid, qualname, kind) in symbol_files.items():
        by_file.setdefault(fid, []).append((sid, qualname, kind))

    for fid, members in by_file.items():
        safe = _esc(file_labels.get(fid, str(fid)))
        lines.append(f'  subgraph cluster_{fid} {{ label="{safe}";')
        for sid, qualname, kind in members:
            lines.append(f'    n{sid} [label="{_esc(qualname)}" kind="{_esc(kind)}"];')
        lines.append("  }")

    for sid, (fid, qualname, kind) in symbol_files.items():
        if fid not in by_file:  # defensive: symbol without a file node group
            lines.append(f'  n{sid} [label="{_esc(qualname)}" kind="{_esc(kind)}"];')

    for r in store.conn.execute("SELECT caller_id, callee_id, callee, file_id FROM calls"):
        if r["caller_id"] and r["callee_id"]:
            lines.append(f'  n{r["caller_id"]} -> n{r["callee_id"]};')
        elif r["caller_id"]:
            target = f'"{_esc(r["callee"])}"'
            lines.append(f'  n{r["caller_id"]} -> {target} [style=dashed];')

    for r in store.conn.execute(
            "SELECT i.file_id, i.target_id, i.module FROM imports i WHERE i.target_id IS NOT NULL"):
        lines.append(
            f'  f{r["file_id"]} -> f{r["target_id"]} '
            f'[label="{_esc(r["module"])}" style=dotted];')

    lines.append("}")
    return "\n".join(lines) + "\n"
