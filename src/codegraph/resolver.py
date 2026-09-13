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

# JavaScript / TypeScript relative imports may use either ecosystem's
# extensions (require("./util.js") can resolve to util.ts), but must not
# fall through to unrelated languages.
_JS_TS_EXTS = list(dict.fromkeys(
    _EXT_BY_LANG["javascript"] + _EXT_BY_LANG["typescript"]
))
_ALL_EXTS = sorted({ext for exts in _EXT_BY_LANG.values() for ext in exts})

_IDENT_CHAIN = re.compile(r"[A-Za-z_$][\w$]*(?:::[A-Za-z_$][\w$]*)*(?:\.[A-Za-z_$][\w$]*)*")
_SQLITE_PARAM_CHUNK_SIZE = 900
_RUST_CRATE_ROOT_FILES = ("lib.rs", "main.rs")


def _id_chunks(ids):
    ids = tuple(ids)
    for start in range(0, len(ids), _SQLITE_PARAM_CHUNK_SIZE):
        yield ids[start:start + _SQLITE_PARAM_CHUNK_SIZE]


def last_segment(name: str) -> str:
    """Final identifier of a dotted or ::-separated call target."""
    for sep in ("::", "."):
        if sep in name:
            name = name.rsplit(sep, 1)[-1]
    return name


def _root_of(store: IndexStore) -> Path:
    return Path(store.get_meta("root") or ".")


def resolve_callee(store: IndexStore, file_id: int, callee_text: str,
                   blocked_file_ids=()):
    """Return the symbol id a call target refers to, or None.

    ``blocked_file_ids`` prevents fallback to symbols in import targets that
    are not the selected candidate for the import.
    """
    name = last_segment(callee_text)
    if not name:
        return None
    file = store.file_by_id(file_id)
    if file is None:
        return None
    blocked_file_ids = set(blocked_file_ids)

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
    candidates = _imported_files(store, file_id) - blocked_file_ids
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
            "SELECT s.id, s.file_id FROM symbols s JOIN files f ON f.id = s.file_id "
            "WHERE f.module = ? AND f.id != ? AND s.name = ?",
            (file["module"], file_id, name),
        ).fetchall()
        rows = [row for row in rows if row["file_id"] not in blocked_file_ids]
        if len(rows) == 1:
            return rows[0]["id"]

    # 4. globally unique name (last resort heuristic)
    rows = store.conn.execute(
        "SELECT id, file_id FROM symbols WHERE name = ? LIMIT 2", (name,)
    ).fetchall()
    if any(row["file_id"] in blocked_file_ids for row in rows):
        return None
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
            if file["lang"] == "python" and base.startswith("."):
                # Python relative imports resolve against the dotted package.
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


def _rust_crate_dir(store: IndexStore, file_path: Path) -> Path:
    """Return the relative directory containing a Rust crate root.

    Rust's ``crate::`` paths are rooted at the crate source root, not at the
    directory containing the importing file.  The index does not retain
    Cargo metadata, so use the nearest indexed ``lib.rs``/``main.rs`` as the
    crate-root marker and fall back to common source-tree layouts.
    """
    file_dir = file_path.parent

    # Cargo binaries in src/bin are independent crate roots even when the
    # package also has src/lib.rs or src/main.rs.
    if file_dir.name == "bin" and file_path.name != "mod.rs":
        return file_dir

    for directory in (file_dir, *file_dir.parents):
        if any(store.file_by_path((directory / name).as_posix())
               for name in _RUST_CRATE_ROOT_FILES):
            return directory

    parts = file_path.parts[:-1]
    if "src" in parts:
        # Use the deepest source directory when a workspace contains nested
        # source trees.
        src_index = max(index for index, part in enumerate(parts)
                        if part == "src")
        return Path(*parts[:src_index + 1])
    # A project without Cargo's src layout commonly keeps one crate below a
    # top-level directory (as the test fixture does).
    return Path(parts[0]) if parts else Path(".")


def _rust_module_dir(file_path: Path, crate_dir: Path) -> Path:
    """Return the directory in which the file's child modules are defined."""
    if file_path.name == "mod.rs":
        return file_path.parent
    if file_path.name in _RUST_CRATE_ROOT_FILES:
        return file_path.parent
    if file_path.parent == crate_dir and crate_dir.name == "bin":
        return file_path.parent
    return file_path.with_suffix("")


def _rust_module_file_candidates(module_dir: Path):
    """Return the two filesystem forms of a Rust module path."""
    if module_dir == Path("."):
        return []
    return [module_dir.with_suffix(".rs"), module_dir / "mod.rs"]


def _rust_root_file_candidates(crate_dir: Path, importing_file: Path):
    """Return crate-root files, preferring the importing root when known."""
    names = list(_RUST_CRATE_ROOT_FILES)
    if importing_file.parent == crate_dir and importing_file.name in names:
        names.remove(importing_file.name)
        names.insert(0, importing_file.name)
    return [crate_dir / name for name in names]


def _rust_candidates(store: IndexStore, file: dict, module_text: str,
                     root: Path):
    """Build root-relative candidates for a Rust module or use path."""
    file_path = Path(file["path"])
    crate_dir = _rust_crate_dir(store, file_path)
    module_dir = _rust_module_dir(file_path, crate_dir)
    parts = module_text.split("::")
    qualifier = parts[0]
    explicit_relative = qualifier in ("crate", "self", "super")

    if qualifier == "crate":
        base = crate_dir
        parts = parts[1:]
    elif qualifier in ("self", "super"):
        base = module_dir
        parts = parts[1:]
        if qualifier == "super":
            if base == crate_dir:
                return []
            base = base.parent
        while parts and parts[0] in ("self", "super"):
            if parts.pop(0) == "super":
                if base == crate_dir:
                    return []
                base = base.parent
    else:
        # A non-qualified Rust module path is relative to the current module.
        # This also fixes ``mod child;`` inside foo.rs (child lives in
        # foo/child.rs, not beside foo.rs).
        base = module_dir

    candidates = []

    def add(path):
        if path not in candidates:
            candidates.append(path)

    if not parts:
        if qualifier == "self":
            add(file_path)
        elif base == crate_dir:
            for path in _rust_root_file_candidates(crate_dir, file_path):
                add(path)
        else:
            for path in _rust_module_file_candidates(base):
                add(path)
        return candidates

    # The final path component may name either a child module or an item
    # inside the preceding module.  Try module-file prefixes from longest to
    # shortest so both ``crate::a::b`` and ``crate::a::function`` work.
    for end in range(len(parts), 0, -1):
        module_path = base.joinpath(*parts[:end])
        for path in _rust_module_file_candidates(module_path):
            add(path)

    # Qualified paths can also import an item defined directly in their base
    # module (for example, ``use super::helper``).  An unqualified external
    # path must not fall back to the current file, or external imports would
    # become falsely resolved.
    if explicit_relative:
        if qualifier == "self" and base == module_dir:
            add(file_path)
        elif base == crate_dir:
            for path in _rust_root_file_candidates(crate_dir, file_path):
                add(path)
        else:
            for path in _rust_module_file_candidates(base):
                add(path)
    return candidates


def _module_candidate_paths(store: IndexStore, file_id: int, module_text: str):
    """Return root-relative file paths considered for a module import."""
    file = store.file_by_id(file_id)
    if file is None or not module_text:
        return []
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
            return []
        target = rel_of(file_dir / module_text)
        if target is None:
            return []
        candidates = []
        if target.suffix:
            candidates.append(target)
        # JS and TS can resolve each other's extensions, but never unrelated
        # language files. Rust retains its broader legacy fallback behavior.
        fallback_exts = _JS_TS_EXTS if lang in ("javascript", "typescript") else _ALL_EXTS
        ordered = _EXT_BY_LANG[lang] + [
            e for e in fallback_exts if e not in _EXT_BY_LANG[lang]
        ]
        candidates.extend(target.with_suffix(ext) for ext in ordered)
        return candidates

    # --- python ------------------------------------------------------------
    if lang == "python":
        if module_text.startswith("."):
            level = len(module_text) - len(module_text.lstrip("."))
            rel_name = module_text.lstrip(".")
            base = file_dir
            for _ in range(level - 1):
                base = base.parent
            parts = rel_name.split(".") if rel_name else []
            target = base.joinpath(*parts)
            cands = []
            if target.name:
                cands.append(target.with_suffix(".py"))
            cands.append(target / "__init__.py")
        else:
            parts = module_text.split(".")
            cands = []
            for up in [file_dir, *file_dir.parents]:
                if not _is_within(root / up, root):
                    break
                cands.append(up.joinpath(*parts).with_suffix(".py"))
                cands.append(up.joinpath(*parts) / "__init__.py")
        return [rel for cand in cands if (rel := rel_of(cand)) is not None]

    # --- rust: std/core/alloc are external; crate/self/super are internal --
    if lang == "rust":
        if module_text in ("std", "core", "alloc") or \
                module_text.startswith(("std::", "core::", "alloc::")):
            return []
        return [rel for cand in _rust_candidates(store, file, module_text, root)
                if (rel := rel_of(cand)) is not None]

    # --- go / java: try the module text as a path under the root ----------
    parts = module_text.split(".")
    cands = []
    for ext in _EXT_BY_LANG[lang]:
        cands.append(Path(*parts).with_suffix(ext))
    if lang == "go":
        cands.append(Path(*parts) / "main.go")
    return [rel for cand in cands if (rel := rel_of(cand)) is not None]


def resolve_module(store: IndexStore, file_id: int, module_text: str):
    """Return the file id an import statement refers to, or None."""
    for cand in _module_candidate_paths(store, file_id, module_text):
        row = store.file_by_path(cand.as_posix())
        if row:
            return row["id"]
    return None


def _competing_import_files(store: IndexStore, file_id: int):
    """Return candidates that are not selected by any import in the file."""
    blocked = set()
    imports = store.imports_for_file(file_id)
    selected = {imp["target_id"] for imp in imports if imp["target_id"] is not None}
    for imp in imports:
        target_id = imp["target_id"]
        if target_id is None:
            continue
        for path in _module_candidate_paths(store, file_id, imp["module"]):
            row = store.file_by_path(path.as_posix())
            if row and row["id"] != target_id and row["id"] not in selected:
                blocked.add(row["id"])
    return blocked


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_all(store: IndexStore, file_ids=None, call_ids=(), import_ids=(),
                symbol_names=(), recheck_all_imports=False):
    """Post-pass: fill target_id, then caller_id / callee_id for selected edges.

    Imports are resolved before calls so that first-build resolution can
    already follow import edges between files.  ``file_ids=None`` preserves
    the full-graph behavior for callers that explicitly request it; an
    iterable scopes work to the changed files and invalidated incoming edges.
    """
    with store.transaction():
        if file_ids is None:
            import_rows = store.conn.execute(
                "SELECT id, file_id, module FROM imports ORDER BY id"
            ).fetchall()
            call_rows = store.conn.execute(
                "SELECT id, file_id, caller_name, callee FROM calls ORDER BY id"
            ).fetchall()
        else:
            file_ids = set(file_ids)
            import_ids = set(import_ids)
            call_ids = set(call_ids)
            if file_ids:
                for chunk in _id_chunks(file_ids):
                    placeholders = ", ".join("?" for _ in chunk)
                    import_ids.update(
                        row["id"] for row in store.conn.execute(
                            f"SELECT id FROM imports WHERE file_id IN ({placeholders})",
                            chunk,
                        )
                    )
                    call_ids.update(
                        row["id"] for row in store.conn.execute(
                            f"SELECT id FROM calls WHERE file_id IN ({placeholders})",
                            chunk,
                        )
                    )
            if recheck_all_imports:
                # A new file can be a higher-priority candidate for an import
                # that already has a target, so retry resolved imports too.
                import_ids.update(
                    row["id"] for row in store.conn.execute(
                        "SELECT id FROM imports"
                    )
                )
                # Import target changes can invalidate calls without clearing
                # their old callee_id, so revisit calls in importing files.
                call_ids.update(
                    row["id"] for row in store.conn.execute(
                        "SELECT c.id FROM calls c "
                        "WHERE EXISTS ("
                        "SELECT 1 FROM imports i WHERE i.file_id = c.file_id"
                        ")"
                    )
                )
            if symbol_names:
                names = set(symbol_names)
                call_ids.update(
                    row["id"] for row in store.conn.execute(
                        "SELECT id, callee FROM calls"
                    ).fetchall()
                    if last_segment(row["callee"]) in names
                )
            if import_ids:
                import_rows = []
                for chunk in _id_chunks(import_ids):
                    placeholders = ", ".join("?" for _ in chunk)
                    import_rows.extend(store.conn.execute(
                        f"SELECT id, file_id, module FROM imports WHERE id IN ({placeholders})",
                        chunk,
                    ).fetchall())
                import_rows.sort(key=lambda row: row["id"])
            else:
                import_rows = []
            if call_ids:
                call_rows = []
                for chunk in _id_chunks(call_ids):
                    placeholders = ", ".join("?" for _ in chunk)
                    call_rows.extend(store.conn.execute(
                        f"SELECT id, file_id, caller_name, callee FROM calls "
                        f"WHERE id IN ({placeholders})",
                        chunk,
                    ).fetchall())
                call_rows.sort(key=lambda row: row["id"])
            else:
                call_rows = []

        previous_imported_files = {
            file_id: _imported_files(store, file_id)
            for file_id in {row["file_id"] for row in call_rows}
        }

        for row in import_rows:
            target = resolve_module(store, row["file_id"], row["module"])
            store.conn.execute(
                "UPDATE imports SET target_id = ? WHERE id = ?", (target, row["id"])
            )

        blocked_import_files = {}
        for file_id in previous_imported_files:
            blocked = previous_imported_files[file_id] - _imported_files(store, file_id)
            blocked.update(_competing_import_files(store, file_id))
            blocked_import_files[file_id] = blocked

        for row in call_rows:
            caller_id = None
            if row["caller_name"]:
                sym = store.conn.execute(
                    "SELECT id FROM symbols WHERE file_id = ? AND qualname = ? "
                    "ORDER BY id LIMIT 1",
                    (row["file_id"], row["caller_name"]),
                ).fetchone()
                caller_id = sym["id"] if sym else None
            callee_id = resolve_callee(
                store,
                row["file_id"],
                row["callee"],
                blocked_file_ids=blocked_import_files.get(row["file_id"], ()),
            )
            store.conn.execute(
                "UPDATE calls SET caller_id = ?, callee_id = ? WHERE id = ?",
                (caller_id, callee_id, row["id"]),
            )
