"""Cross-file resolution of call targets and module imports.

Resolution is heuristic by design: the index records the raw text of each
call site and each import, then a post-pass tries to connect them to known
symbols and files. Rules run in priority order and the first decisive hit
wins; anything else stays unresolved (external code, stdlib, third-party
packages, ambiguous names).
"""

from __future__ import annotations

import re
from pathlib import Path

from .store import IndexStore

_EXT_BY_LANG = {
    "python": [".py", ".pyi"],
    "javascript": [".js", ".jsx", ".mjs", ".cjs"],
    "typescript": [".ts", ".tsx", ".mts", ".cts", ".js", ".jsx"],
    "go": [".go"],
    "java": [".java"],
    "rust": [".rs"],
}

# relative imports may point at any language's file (require("./util.js")
# can resolve to util.ts), so fall back to the union of all extensions
_ALL_EXTS = sorted({ext for exts in _EXT_BY_LANG.values() for ext in exts})

_IDENT_CHAIN = re.compile(r"[A-Za-z_$][\w$]*(?:::[A-Za-z_$][\w$]*)*(?:\.[A-Za-z_$][\w$]*)*")


def last_segment(name: str) -> str:
    """Final identifier of a dotted or ::-separated call target."""
    for sep in ("::", "."):
        if sep in name:
            name = name.rsplit(sep, 1)[-1]
    return name


def _root_of(store: IndexStore) -> Path:
    return Path(store.get_meta("root") or ".")


def resolve_callee(store: IndexStore, file_id: int, callee_text: str):
    """Return the symbol id a call target refers to, or None."""
    name = last_segment(callee_text)
    if not name:
        return None
    file = store.file_by_id(file_id)
    if file is None:
        return None

    # 1. same file: exact qualname, then unique name
    row = store.conn.execute(
        "SELECT id FROM symbols WHERE file_id = ? AND qualname = ? ORDER BY id LIMIT 1",
        (file_id, callee_text),
    ).fetchone()
    if row:
        return row["id"]
    rows = store.conn.execute(
        "SELECT id FROM symbols WHERE file_id = ? AND name = ?", (file_id, name)
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]

    # 2. files reachable through this file's imports
    candidates = _imported_files(store, file_id)
    for cid in candidates:
        row = store.conn.execute(
            "SELECT id FROM symbols WHERE file_id = ? AND qualname = ? ORDER BY id LIMIT 1",
            (cid, callee_text),
        ).fetchone()
        if row:
            return row["id"]
    named = []
    for cid in candidates:
        rows = store.conn.execute(
            "SELECT id FROM symbols WHERE file_id = ? AND name = ?", (cid, name)
        ).fetchall()
        named.extend(r["id"] for r in rows)
    if len(named) == 1:
        return named[0]

    # 3. same module family (java package, go package, ts barrel files)
    if file["lang"] in ("java", "go", "javascript", "typescript"):
        rows = store.conn.execute(
            "SELECT s.id FROM symbols s JOIN files f ON f.id = s.file_id "
            "WHERE f.module = ? AND f.id != ? AND s.name = ?",
            (file["module"], file_id, name),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]

    # 4. globally unique name (last resort heuristic)
    rows = store.conn.execute(
        "SELECT id FROM symbols WHERE name = ? LIMIT 2", (name,)
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    return None


def _imported_files(store: IndexStore, file_id: int):
    """Ids of every file this file imports, plus submodules imported by name."""
    file = store.file_by_id(file_id)
    if file is None:
        return set()
    out = set()
    for imp in store.imports_for_file(file_id):
        if imp["target_id"]:
            out.add(imp["target_id"])
        for nm in _names_of(imp):
            base = imp["module"]
            if base.startswith("."):  # relative: resolve against our package
                level = len(base) - len(base.lstrip("."))
                mod_parts = file["module"].split(".")
                # a file inside pkg/ has module "pkg.cart" (package "pkg");
                # an __init__ file IS the package ("pkg") and keeps its own
                # module as the base for the first relative level
                if file["path"].endswith("__init__.py"):
                    base_parts = mod_parts
                else:
                    base_parts = mod_parts[:-1]
                for _ in range(level - 1):
                    if base_parts:
                        base_parts = base_parts[:-1]
                base = ".".join(base_parts)
            for suffix in (nm, nm + ".__init__"):
                full = f"{base}.{suffix}" if base else suffix
                row = store.conn.execute(
                    "SELECT id FROM files WHERE module = ? ORDER BY id LIMIT 1",
                    (full,),
                ).fetchone()
                if row:
                    out.add(row["id"])
    return out


def _names_of(imp) -> list:
    import json

    try:
        return json.loads(imp["names"] or "[]")
    except (ValueError, TypeError):
        return []


def resolve_module(store: IndexStore, file_id: int, module_text: str):
    """Return the file id an import statement refers to, or None."""
    file = store.file_by_id(file_id)
    if file is None or not module_text:
        return None
    root = _root_of(store).resolve()
    lang = file["lang"]
    file_dir = Path(file["path"]).parent  # relative to root

    def rel_of(rel_path: Path):
        """Normalize a root-relative candidate path; None if it escapes root."""
        norm = (root / rel_path).resolve()
        if not _is_within(norm, root):
            return None
        return norm.relative_to(root)

    # --- relative paths (js/ts: "./x", "../y") ----------------------------
    if module_text.startswith("./") or module_text.startswith("../"):
        if lang not in ("javascript", "typescript", "rust"):
            return None
        target = rel_of(file_dir / module_text)
        if target is None:
            return None
        candidates = []
        if target.suffix:
            candidates.append(target)
        # same-language extensions first, then the rest
        ordered = _EXT_BY_LANG[lang] + [e for e in _ALL_EXTS if e not in _EXT_BY_LANG[lang]]
        if target.suffix:
            candidates.extend(target.with_suffix(ext) for ext in ordered)
        else:
            candidates.extend(target.with_suffix(ext) for ext in ordered)
        for cand in candidates:
            row = store.file_by_path(cand.as_posix())
            if row:
                return row["id"]
        return None

    # --- python ------------------------------------------------------------
    if lang == "python":
        if module_text.startswith("."):
            level = len(module_text) - len(module_text.lstrip("."))
            rel_name = module_text.lstrip(".")
            base = file_dir
            for _ in range(level - 1):
                base = base.parent
            parts = rel_name.split(".") if rel_name else []
            cands = [base.joinpath(*parts).with_suffix(".py")]
            cands.append(base.joinpath(*parts) / "__init__.py")
        else:
            parts = module_text.split(".")
            cands = []
            for up in [file_dir, *file_dir.parents]:
                if not _is_within(root / up, root):
                    break
                cands.append(up.joinpath(*parts).with_suffix(".py"))
                cands.append(up.joinpath(*parts) / "__init__.py")
        for cand in cands:
            rel = rel_of(cand)
            if rel is not None:
                row = store.file_by_path(rel.as_posix())
                if row:
                    return row["id"]
        return None

    # --- rust: crate/super/std are external, mod items map to files -------
    if lang == "rust":
        if module_text in ("std", "core", "alloc") or \
                module_text.startswith(("std::", "core::", "alloc::")):
            return None
        base = module_text.split("::")[0]
        for cand in (file_dir / f"{base}.rs", file_dir / base / "mod.rs"):
            rel = rel_of(cand)
            if rel is not None:
                row = store.file_by_path(rel.as_posix())
                if row:
                    return row["id"]
        return None

    # --- go / java: try the module text as a path under the root ----------
    parts = module_text.split(".")
    cands = []
    for ext in _EXT_BY_LANG[lang]:
        cands.append(Path(*parts).with_suffix(ext))
    if lang == "go":
        cands.append(Path(*parts) / "main.go")
    for cand in cands:
        rel = rel_of(cand)
        if rel is not None:
            row = store.file_by_path(rel.as_posix())
            if row:
                return row["id"]
    return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_all(store: IndexStore):
    """Post-pass: fill target_id, then caller_id / callee_id for every edge.

    Imports are resolved before calls so that first-build resolution can
    already follow import edges between files.
    """
    for row in store.conn.execute("SELECT id, file_id, module FROM imports"):
        target = resolve_module(store, row["file_id"], row["module"])
        if target:
            store.conn.execute(
                "UPDATE imports SET target_id = ? WHERE id = ?", (target, row["id"])
            )

    for row in store.conn.execute("SELECT id, file_id, caller_name, callee FROM calls"):
        updates = []
        if row["caller_name"]:
            sym = store.conn.execute(
                "SELECT id FROM symbols WHERE file_id = ? AND qualname = ? "
                "ORDER BY id LIMIT 1",
                (row["file_id"], row["caller_name"]),
            ).fetchone()
            updates.append(("caller_id", sym["id"] if sym else None))
        callee_id = resolve_callee(store, row["file_id"], row["callee"])
        updates.append(("callee_id", callee_id))
        for col, val in updates:
            store.conn.execute(
                f"UPDATE calls SET {col} = ? WHERE id = ?", (val, row["id"])
            )
