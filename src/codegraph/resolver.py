"""Cross-file resolution of call targets and module imports.

Resolution is heuristic by design: the index records the raw text of each
call site and each import, then a post-pass tries to connect them to known
symbols and files. Rules run in priority order and the first decisive hit
wins; anything else stays unresolved (external code, stdlib, third-party
packages, ambiguous names).
"""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path

from .scanner.quick import _rust_mask_comments
from .store import IndexStore

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10; use an installed backport if present.
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

_EXT_BY_LANG = {
    "python": [".py", ".pyi"],
    "javascript": [".js", ".jsx", ".mjs", ".cjs"],
    "typescript": [".ts", ".tsx", ".mts", ".cts", ".js", ".jsx"],
    "go": [".go"],
    "java": [".java"],
    "rust": [".rs"],
}
_TS_RUNTIME_EXTENSION_SUBSTITUTIONS = {
    ".js": (".ts", ".tsx", ".d.ts", ".js", ".jsx"),
    ".mjs": (".mts", ".d.mts", ".mjs"),
    ".cjs": (".cts", ".d.cts", ".cjs"),
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

    alias_target = _rust_alias_symbol(store, file_id, callee_text)
    if alias_target is not None:
        return alias_target

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


def _go_package_files(store: IndexStore, package_dir: Path):
    if package_dir == Path("."):
        rows = store.conn.execute(
            "SELECT id, path FROM files WHERE lang = 'go' ORDER BY path"
        )
    else:
        prefix = f"{package_dir.as_posix()}/"
        rows = store.conn.execute(
            "SELECT id, path FROM files WHERE lang = 'go' "
            "AND path >= ? AND path < ? ORDER BY path",
            (prefix, f"{package_dir.as_posix()}0"),
        )
    return [
        (row["id"], Path(row["path"])) for row in rows
        if Path(row["path"]).parent == package_dir
    ]


def _go_module_path(root: Path):
    try:
        lines = (root / "go.mod").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        words = line.split("//", 1)[0].strip().split()
        if len(words) >= 2 and words[0] == "module":
            return words[1].strip('"`')
    return None


def _go_package_dirs(root: Path, module_text: str):
    candidates = []
    module_path = _go_module_path(root)
    if module_path and module_text == module_path:
        candidates.append(Path("."))
    elif module_path and module_text.startswith(module_path + "/"):
        suffix = module_text[len(module_path) + 1:]
        candidates.append(Path(*suffix.split("/")))
    candidates.append(Path(*module_text.split("/")))
    return list(dict.fromkeys(candidates))


def _java_package_files(store: IndexStore, package: str):
    rows = store.conn.execute(
        "SELECT id, path FROM files WHERE lang = 'java' AND module = ? "
        "ORDER BY path",
        (package,),
    )
    return [(row["id"], Path(row["path"])) for row in rows]


def _java_class_files(store: IndexStore, class_names):
    class_names = list(dict.fromkeys(class_names))
    if not class_names:
        return []
    placeholders = ",".join("?" for _ in class_names)
    rows = store.conn.execute(
        "SELECT DISTINCT f.id, f.path, s.qualname FROM symbols s "
        "JOIN files f ON f.id = s.file_id "
        "WHERE f.lang = 'java' AND s.kind IN ('class', 'interface') "
        f"AND s.qualname IN ({placeholders}) ORDER BY f.path",
        class_names,
    )
    by_name = {}
    for row in rows:
        by_name.setdefault(row["qualname"], []).append(
            (row["id"], Path(row["path"]))
        )
    for name in class_names:
        if name in by_name:
            return by_name[name]
    return []


def _imported_files(store: IndexStore, file_id: int):
    """Ids of every file this file imports, plus submodules imported by name."""
    file = store.file_by_id(file_id)
    if file is None:
        return set()
    out = set()
    expanded_go_dirs = set()
    expanded_java_packages = set()
    for imp in store.imports_for_file(file_id):
        if imp["target_id"]:
            out.add(imp["target_id"])
            if file["lang"] == "go":
                target = store.file_by_id(imp["target_id"])
                if target is not None and target["lang"] == "go":
                    package_dir = Path(target["path"]).parent
                    if package_dir not in expanded_go_dirs:
                        expanded_go_dirs.add(package_dir)
                        out.update(
                            package_file_id
                            for package_file_id, _ in
                            _go_package_files(store, package_dir)
                        )
            if file["lang"] == "java" and imp["module"].endswith(".*"):
                package = imp["module"][:-2]
                target = store.file_by_id(imp["target_id"])
                if (target is not None and target["lang"] == "java" and
                        target["module"] == package and
                        package not in expanded_java_packages):
                    expanded_java_packages.add(package)
                    out.update(
                        package_file_id
                        for package_file_id, _ in
                        _java_package_files(store, package)
                    )
        for nm in _names_of(imp):
            base = imp["module"]
            if file["lang"] == "python" and base.startswith("."):
                # Python relative imports resolve against the dotted package.
                level = len(base) - len(base.lstrip("."))
                mod_parts = file["module"].split(".")
                # a file inside pkg/ has module "pkg.cart" (package "pkg");
                # an __init__ file IS the package ("pkg") and keeps its own
                # module as the base for the first relative level
                if Path(file["path"]).name in ("__init__.py", "__init__.pyi"):
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


def _rust_alias_symbol(store: IndexStore, file_id: int, callee_text: str):
    """Resolve a call through a Rust ``use ... as alias`` binding."""
    file = store.file_by_id(file_id)
    if file is None or file["lang"] != "rust":
        return None
    for imp in store.imports_for_file(file_id):
        if imp["kind"] != "use" or not imp["target_id"]:
            continue
        names = _names_of(imp)
        if len(names) != 1:
            continue
        alias = names[0]
        if callee_text == alias:
            source_name = last_segment(imp["module"])
        elif callee_text.startswith(alias + "::"):
            source_name = last_segment(callee_text[len(alias) + 2:])
        elif callee_text.startswith(alias + "."):
            source_name = last_segment(callee_text[len(alias) + 1:])
        else:
            continue
        rows = store.conn.execute(
            "SELECT id FROM symbols WHERE file_id = ? AND name = ?",
            (imp["target_id"], source_name),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
    return None


def _rust_module_dir(file_path: Path, crate_dir: Path, crate_root=None) -> Path:
    """Return the directory in which the file's child modules are defined."""
    if crate_root is not None and file_path == crate_root:
        return file_path.parent
    if file_path.name == "mod.rs":
        return file_path.parent
    if (crate_root is None and file_path.parent == crate_dir and
            file_path.name in _RUST_CRATE_ROOT_FILES):
        return file_path.parent
    if (crate_root is None and file_path.parent == crate_dir and
            crate_dir.name == "bin"):
        return file_path.parent
    return file_path.with_suffix("")


def _rust_module_file_candidates(module_dir: Path):
    """Return the two filesystem forms of a Rust module path."""
    if module_dir == Path("."):
        return []
    return [module_dir.with_suffix(".rs"), module_dir / "mod.rs"]


def _rust_mod_path_overrides(store: IndexStore, file):
    """Return ``#[path]`` overrides for ``mod`` declarations in a file."""
    cache = getattr(store, "_rust_mod_path_cache", None)
    if cache is None:
        cache = {}
        store._rust_mod_path_cache = cache
    file_id = file["id"]
    if file_id in cache:
        return cache[file_id]

    try:
        text = (_root_of(store) / file["path"]).read_text(encoding="utf-8")
    except OSError:
        cache[file_id] = {}
        return cache[file_id]

    text = _rust_mask_comments(text, preserve_path_strings=True)

    def normalize_path_attribute(match):
        path = re.sub(r"\\(?:\r\n|\n)[ \t\r\n]*", "", match.group(1))
        return f'#[path = "{path}"]'

    text = re.sub(
        r'#\[\s*path\s*=\s*"((?:\\.|[^"\\])*)"\s*\]',
        normalize_path_attribute,
        text,
        flags=re.DOTALL,
    )
    pending_path = None
    paths = {}
    for line in text.splitlines():
        stripped = line.strip()
        path_match = re.search(
            r'#\[\s*path\s*=\s*"((?:\\.|[^"\\])*)"\s*\]', line)
        mod_line = re.sub(r"^\s*(?:#\[[^\]]*\]\s*)*", "", line)
        mod_match = re.match(
            r"(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+"
            r"([A-Za-z_]\w*)\s*;", mod_line)
        if mod_match:
            override = path_match.group(1) if path_match else pending_path
            if override is not None:
                paths.setdefault(mod_match.group(1), set()).add(override)
            pending_path = None
            continue
        if stripped.startswith("#["):
            if path_match:
                pending_path = path_match.group(1)
            continue
        if not stripped:
            continue
        pending_path = None

    overrides = {
        name: next(iter(values))
        for name, values in paths.items()
        if len(values) == 1
    }
    cache[file_id] = overrides
    return overrides


def _rust_declared_module_file_candidates(store: IndexStore, file,
                                           module_dir: Path, module: str,
                                           require_declared=False):
    """Return paths for a mod declaration, including a ``#[path]`` override."""
    if require_declared and not any(
            imp["kind"] == "mod" and imp["module"] == module
            for imp in store.imports_for_file(file["id"])):
        return []
    override = _rust_mod_path_overrides(store, file).get(module)
    if override is None:
        return _rust_module_file_candidates(module_dir / module)
    path = Path(override)
    if not path.is_absolute():
        path = Path(file["path"]).parent / path
    return [Path(posixpath.normpath(path.as_posix()))]


def _rust_module_parent(store: IndexStore, root_path: Path, target_path: Path):
    """Return the logical Rust parent file for a reachable module file."""
    if target_path == root_path:
        return None
    return _rust_module_graph(store, root_path).get(target_path)


def _rust_module_graph(store: IndexStore, root_path: Path):
    """Return reachable Rust files and their logical module parents."""
    cache = getattr(store, "_rust_module_graph_cache", None)
    if cache is None:
        cache = {}
        store._rust_module_graph_cache = cache
    if root_path in cache:
        return cache[root_path]

    crate_dir = root_path.parent
    pending = [root_path]
    parents = {root_path: None}
    while pending:
        current = pending.pop()
        row = store.file_by_path(current.as_posix())
        if row is None or row["lang"] != "rust":
            continue
        module_dir = _rust_module_dir(current, crate_dir, root_path)
        for imp in store.imports_for_file(row["id"]):
            if imp["kind"] != "mod":
                continue
            for candidate in _rust_declared_module_file_candidates(
                    store, row, module_dir, imp["module"]):
                if candidate in parents or store.file_by_path(
                        candidate.as_posix()) is None:
                    continue
                parents[candidate] = current
                pending.append(candidate)
    cache[root_path] = parents
    return parents


def _rust_toml_line_without_comment(line: str):
    """Remove a TOML comment without changing a quoted ``#`` character."""
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if quote is not None:
            if quote == '"' and char == "\\" and not escaped:
                escaped = True
                continue
            if char == quote and not escaped:
                quote = None
            escaped = False
        elif char in ('"', "'"):
            quote = char
        elif char == "#":
            return line[:index]
    return line


def _rust_logical_module_file_candidates(store, starts, module_parts,
                                          crate_dir, crate_root,
                                          require_declared=False):
    """Resolve logical module components through declared child modules."""
    found = []
    current = []
    for start in starts:
        row = store.file_by_path(start.as_posix())
        if row is None:
            continue
        module_dir = _rust_module_dir(start, crate_dir, crate_root)
        current.append((row, module_dir))

    for module in module_parts:
        next_files = []
        for parent, module_dir in current:
            for candidate in _rust_declared_module_file_candidates(
                    store, parent, module_dir, module, require_declared):
                row = store.file_by_path(candidate.as_posix())
                if row is None:
                    continue
                found.append(candidate)
                next_files.append((row, _rust_module_dir(
                    candidate, crate_dir, crate_root)))
        current = next_files
        if not current:
            break
    return list(reversed(found))


def _rust_fallback_cargo_targets(text: str):
    """Extract Cargo target paths when no TOML parser is available."""
    sections = {}
    section = None
    pending = None

    def string_value(value):
        value = value.strip()
        if value.startswith(('"""', "'''")):
            delimiter = value[:3]
            if not value.endswith(delimiter) or len(value) < 6:
                return None
            return value[3:-3].lstrip("\n").rstrip("\n")
        if value.startswith('"') and value.endswith('"'):
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return None
        if value.startswith("'") and value.endswith("'"):
            return value[1:-1]
        return None

    def record(key, value, current_section):
        if current_section == "package" and key in (
                "edition", "autobins", "autoexamples", "autotests", "autobenches"):
            if key == "edition":
                parsed = string_value(value)
                if parsed is not None:
                    sections[current_section][key] = parsed
                return
            if value in ("true", "false"):
                sections[current_section][key] = value == "true"
        elif current_section == "package" and key == "edition.workspace":
            if value == "true":
                sections[current_section]["edition"] = {"workspace": True}
        elif current_section == "workspace.package" and key == "edition":
            parsed = string_value(value)
            if parsed is not None:
                sections.setdefault("workspace", {}).setdefault(
                    "package", {})[key] = parsed
        elif current_section == "package" and key == "build":
            if value == "false":
                sections[current_section][key] = False
            else:
                parsed = string_value(value)
                if parsed is not None:
                    sections[current_section][key] = parsed
        elif current_section in ("lib", "bin", "example", "test", "bench"):
            parsed = string_value(value)
            if parsed is None:
                return
            if isinstance(sections[current_section], list):
                sections[current_section][-1][key] = parsed
            else:
                sections[current_section][key] = parsed

    for raw_line in text.splitlines():
        if pending is not None:
            pending[2] += "\n" + raw_line
            if raw_line.rstrip().endswith(pending[2][:3]):
                record(pending[1], pending[2], pending[0])
                pending = None
            continue
        line = _rust_toml_line_without_comment(raw_line).strip()
        match = re.fullmatch(r"(\[\[|\[)([\w.-]+)(\]\]|\])", line)
        if match:
            name = match.group(2)
            if name == "lib":
                section = "lib"
                sections.setdefault(section, {})
            elif name == "bin":
                section = "bin"
                if match.group(1):
                    sections.setdefault(section, []).append({})
                else:
                    sections.setdefault(section, {})
            elif name == "package":
                section = "package"
                sections.setdefault(section, {})
            elif name == "workspace.package":
                section = name
                sections.setdefault("workspace", {}).setdefault(
                    "package", {})
            elif name in ("example", "test", "bench"):
                section = name
                if match.group(1):
                    sections.setdefault(section, []).append({})
                else:
                    sections.setdefault(section, {})
            else:
                section = None
            continue
        match = re.match(r"([\w.-]+)\s*=\s*(.*)$", line)
        if not match or section is None:
            continue
        key, value = match.groups()
        if (value.startswith(('"""', "'''")) and
                not value.rstrip().endswith(value[:3])):
            pending = [section, key, value]
        else:
            record(key, value, section)
    return sections


def _rust_cargo_manifest_data(manifest_path: Path):
    """Load one Cargo manifest with a dependency-free fallback."""
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    if tomllib is not None:
        try:
            return tomllib.loads(text)
        except (OSError, ValueError):
            pass
    return _rust_fallback_cargo_targets(text)


def _rust_cargo_target_paths(manifest_path: Path, data=None):
    """Return target paths declared or implied by one Cargo manifest."""
    if data is None:
        data = _rust_cargo_manifest_data(manifest_path)

    lib = data.get("lib")
    bins = data.get("bin", [])
    has_package = isinstance(data.get("package"), dict)
    has_explicit_targets = isinstance(lib, dict) or bool(bins)
    if not has_package and not has_explicit_targets:
        # A virtual workspace manifest does not define a crate of its own.
        return []

    targets = []

    def add(value):
        if not isinstance(value, str) or not value:
            return
        path = Path(value)
        if not path.is_absolute():
            path = manifest_path.parent / path
        targets.append(path.resolve())

    if isinstance(lib, dict):
        add(lib.get("path", "src/lib.rs"))
    elif (manifest_path.parent / "src" / "lib.rs").is_file():
        add("src/lib.rs")

    package = data.get("package")
    autobins = not isinstance(package, dict) or package.get("autobins", True)
    autoexamples = not isinstance(package, dict) or package.get("autoexamples", True)
    autotests = not isinstance(package, dict) or package.get("autotests", True)
    autobenches = not isinstance(package, dict) or package.get("autobenches", True)
    if autobins and (manifest_path.parent / "src" / "main.rs").is_file():
        add("src/main.rs")

    if isinstance(bins, dict):
        bins = [bins]
    for binary in bins if isinstance(bins, list) else []:
        if not isinstance(binary, dict):
            continue
        path = binary.get("path")
        if path is None and isinstance(binary.get("name"), str):
            path = f"src/bin/{binary['name']}.rs"
        add(path)

    if autobins:
        bin_dir = manifest_path.parent / "src" / "bin"
        if bin_dir.is_dir():
            for path in sorted(bin_dir.glob("*.rs")):
                add(path.as_posix())
            for path in sorted(bin_dir.glob("*/main.rs")):
                add(path.as_posix())

    build = package.get("build") if isinstance(package, dict) else None
    if build is not False:
        add(build if isinstance(build, str) else (
            "build.rs" if (manifest_path.parent / "build.rs").is_file() else None
        ))

    for target_name, target_dir, auto in (
            ("example", "examples", autoexamples),
            ("test", "tests", autotests),
            ("bench", "benches", autobenches)):
        entries = data.get(target_name, [])
        if isinstance(entries, dict):
            entries = [entries]
        for target in entries if isinstance(entries, list) else []:
            if not isinstance(target, dict):
                continue
            path = target.get("path")
            if path is None and isinstance(target.get("name"), str):
                path = f"{target_dir}/{target['name']}.rs"
            add(path)
        if auto:
            directory = manifest_path.parent / target_dir
            if directory.is_dir():
                for path in sorted(directory.glob("*.rs")):
                    add(path.as_posix())
                for path in sorted(directory.glob("*/main.rs")):
                    add(path.as_posix())
    return targets


def _rust_cargo_edition(data, workspace_edition=None):
    """Return a Cargo package edition, defaulting to Rust 2015."""
    package = data.get("package")
    edition = package.get("edition") if isinstance(package, dict) else None
    if isinstance(edition, dict):
        edition = (workspace_edition
                   if edition.get("workspace") is True else None)
    try:
        edition = int(edition)
    except (TypeError, ValueError):
        edition = 2015
    return edition if edition >= 2015 else 2015


def _rust_cargo_workspace_edition(data):
    """Return a workspace's inherited package edition, if declared."""
    workspace = data.get("workspace")
    package = workspace.get("package") if isinstance(workspace, dict) else None
    if not isinstance(package, dict) or "edition" not in package:
        return None
    return _rust_cargo_edition({"package": package})


def _rust_declares_module(store: IndexStore, path: Path, rust_paths):
    """Whether a sibling/module file declares ``path`` with ``mod``."""
    owner_paths = {
        path.parent.parent / f"{path.parent.name}.rs",
        path.parent / "mod.rs",
    }
    owner_paths.update(
        owner for owner in rust_paths
        if owner.parent == path.parent and
        owner.name in _RUST_CRATE_ROOT_FILES and owner != path
    )
    for owner in owner_paths:
        row = store.file_by_path(owner.as_posix())
        if row is None:
            continue
        if any(imp["kind"] == "mod" and imp["module"] == path.stem
               for imp in store.imports_for_file(row["id"])):
            return True
    project_root = _root_of(store).resolve()
    for owner in rust_paths:
        row = store.file_by_path(owner.as_posix())
        if row is None:
            continue
        for override in _rust_mod_path_overrides(store, row).values():
            target = Path(override)
            if not target.is_absolute():
                target = Path(owner).parent / target
            try:
                target = target.resolve().relative_to(project_root)
            except ValueError:
                continue
            if Path(posixpath.normpath(target.as_posix())) == path:
                return True
    return False


def _rust_all_root_paths(store: IndexStore):
    """Discover indexed Rust crate roots from Cargo and source-tree layouts."""
    project_root = _root_of(store).resolve()
    roots = []
    seen = set()
    editions = {}
    store._rust_cargo_editions = editions

    def add(path):
        path = Path(path)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(project_root)
            except ValueError:
                return
        if path in seen:
            return
        row = store.file_by_path(path.as_posix())
        if row is not None and row["lang"] == "rust":
            seen.add(path)
            roots.append(path)
            return path
        return None

    rust_paths = [Path(row["path"]) for row in store.conn.execute(
        "SELECT path FROM files WHERE lang = ? ORDER BY path", ("rust",)
    )]

    # Cargo target paths are authoritative, including roots whose filenames
    # do not follow the conventional lib.rs/main.rs names.
    manifest_meta = store.get_meta("cargo_manifest_paths")
    if manifest_meta is None:
        try:
            manifests = sorted(project_root.rglob("Cargo.toml"))
        except OSError:
            manifests = []
    else:
        try:
            relative_paths = json.loads(manifest_meta)
        except (TypeError, ValueError):
            relative_paths = None
        if isinstance(relative_paths, list):
            manifests = []
            for relative_path in relative_paths:
                if not isinstance(relative_path, str):
                    continue
                manifest = (project_root / relative_path).resolve()
                if manifest.is_file():
                    manifests.append(manifest)
            manifests.sort()
        else:
            try:
                manifests = sorted(project_root.rglob("Cargo.toml"))
            except OSError:
                manifests = []
    manifest_data = {}
    workspace_editions = {}
    for manifest in manifests:
        data = _rust_cargo_manifest_data(manifest)
        manifest_data[manifest] = data
        workspace_edition = _rust_cargo_workspace_edition(data)
        if workspace_edition is not None:
            workspace_editions[manifest] = workspace_edition
    manifest_targets = {
        manifest: _rust_cargo_target_paths(manifest, data)
        for manifest, data in manifest_data.items()
    }
    # Ancestor metadata only disables source-tree roots when its targets are in scope.
    store._rust_cargo_manifests = any(
        manifest.is_relative_to(project_root) or
        any(path.is_relative_to(project_root) for path in target_paths)
        for manifest, target_paths in manifest_targets.items()
    )

    def inherited_workspace_edition(manifest):
        directory = manifest.parent
        while True:
            edition = workspace_editions.get(directory / "Cargo.toml")
            if edition is not None:
                return edition
            if directory == directory.parent:
                return None
            directory = directory.parent

    for manifest, target_paths in manifest_targets.items():
        for path in target_paths:
            root_path = add(path)
            if root_path is not None:
                editions[root_path] = _rust_cargo_edition(
                    manifest_data[manifest],
                    inherited_workspace_edition(manifest))

    if not store._rust_cargo_manifests:
        # In source trees without Cargo metadata, only root-level markers and
        # markers directly below a source directory are conventional roots. A
        # nested foo/main.rs or foo/lib.rs is therefore a normal module file.
        for path in rust_paths:
            if path.name in _RUST_CRATE_ROOT_FILES and (
                path.parent == Path(".") or path.parent.name == "src") and \
                    not _rust_declares_module(
                store, path, rust_paths):
                root_path = add(path)
                if root_path is not None:
                    editions[root_path] = 2015
    return roots


def _rust_root_paths(store: IndexStore, file_path: Path):
    """Return possible crate-root files for an indexed Rust file."""
    roots = getattr(store, "_rust_root_paths_cache", None)
    if roots is None:
        roots = _rust_all_root_paths(store)
        store._rust_root_paths_cache = roots

    # Do not restrict by physical ancestry: #[path] may attach a file outside
    # the root directory. Reachability below is the authoritative association.
    return roots


def _rust_edition(store: IndexStore, crate_root: Path):
    """Return the edition associated with a selected crate root."""
    return getattr(store, "_rust_cargo_editions", {}).get(crate_root, 2015)


def _rust_reaches(store: IndexStore, root_path: Path, target_path: Path):
    """Whether Rust ``mod`` declarations connect root_path to target_path."""
    return target_path in _rust_module_graph(store, root_path)


def _rust_crate_root(store: IndexStore, file_path: Path):
    """Return ``(crate directory, root file)`` for an indexed Rust file."""
    cache = getattr(store, "_rust_crate_root_cache", None)
    if cache is None:
        cache = {}
        store._rust_crate_root_cache = cache
    if file_path in cache:
        return cache[file_path]

    roots = _rust_root_paths(store, file_path)
    if file_path in roots:
        result = file_path.parent, file_path
        cache[file_path] = result
        return result

    reachable = [path for path in roots
                 if _rust_reaches(store, path, file_path)]
    if len(reachable) == 1:
        root_path = reachable[0]
        result = root_path.parent, root_path
        cache[file_path] = result
        return result
    if len(reachable) > 1 or roots or getattr(store, "_rust_cargo_manifests", False):
        result = file_path.parent, None
        cache[file_path] = result
        return result

    # Fall back to a source directory for ordinary module files, but do not
    # infer nested crate roots without Cargo target configuration.
    parts = file_path.parts[:-1]
    if "src" in parts:
        src_index = max(index for index, part in enumerate(parts)
                        if part == "src")
        result = Path(*parts[:src_index + 1]), None
        cache[file_path] = result
        return result
    result = (Path(parts[0]) if parts else Path(".")), None
    cache[file_path] = result
    return result


def _rust_root_file_candidates(crate_dir: Path, importing_file: Path,
                               crate_root=None):
    """Return the selected crate root, or conventional root candidates."""
    if crate_root is not None:
        return [crate_root]
    if (importing_file.parent == crate_dir and
            (importing_file.name in _RUST_CRATE_ROOT_FILES or
             crate_dir.name == "bin")):
        return [importing_file]
    return [crate_dir / name for name in _RUST_CRATE_ROOT_FILES]


def _rust_has_symbol(store: IndexStore, path: Path, name: str):
    row = store.file_by_path(path.as_posix())
    if row is None:
        return False
    return store.conn.execute(
        "SELECT 1 FROM symbols WHERE file_id = ? AND name = ? LIMIT 1",
        (row["id"], name),
    ).fetchone() is not None


def _rust_candidates(store: IndexStore, file: dict, module_text: str,
                     import_kind=None):
    """Build root-relative candidates for a Rust module or use path."""
    file_path = Path(file["path"])
    crate_dir, crate_root = _rust_crate_root(store, file_path)
    module_dir = _rust_module_dir(file_path, crate_dir, crate_root)
    parts = module_text.split("::")
    qualifier = parts[0]
    explicit_relative = qualifier in ("crate", "self", "super")
    starts_override = None

    if crate_root is None and getattr(store, "_rust_cargo_manifests", False):
        return []
    if qualifier == "crate" and crate_root is None:
        return []
    if import_kind == "mod" and not explicit_relative:
        override = _rust_mod_path_overrides(store, file).get(module_text)
        if override is not None:
            path = Path(override)
            if not path.is_absolute():
                path = file_path.parent / path
            return [Path(posixpath.normpath(path.as_posix()))]

    if qualifier == "crate":
        bases = [crate_dir]
        parts = parts[1:]
    elif qualifier in ("self", "super"):
        parts = parts[1:]
        up_levels = 1 if qualifier == "super" else 0
        while parts and parts[0] in ("self", "super"):
            if parts.pop(0) == "super":
                up_levels += 1
        base = module_dir
        if up_levels and crate_root is not None:
            ancestor = file_path
            for _ in range(up_levels):
                ancestor = _rust_module_parent(store, crate_root, ancestor)
                if ancestor is None:
                    return []
            base = _rust_module_dir(ancestor, crate_dir, crate_root)
            starts_override = [ancestor]
        else:
            for _ in range(up_levels):
                if base == crate_dir:
                    return []
                base = base.parent
        bases = [base]
    else:
        # ``mod`` declarations are relative to the current module.  A bare
        # ``use`` path is crate-root-relative in Rust 2015.  In Rust 2018+
        # it starts at the current module.  A crate-root path must be explicit.
        if import_kind in (None, "mod"):
            bases = [module_dir]
        elif crate_root is not None and _rust_edition(store, crate_root) >= 2018:
            bases = [module_dir]
        else:
            bases = [crate_dir]

    candidates = []

    def add(path):
        if path not in candidates:
            candidates.append(path)

    if not parts:
        if qualifier == "self" and bases[0] == module_dir:
            add(file_path)
        elif bases[0] == crate_dir and crate_root is not None:
            for path in _rust_root_file_candidates(
                    crate_dir, file_path, crate_root):
                add(path)
        else:
            for path in _rust_module_file_candidates(bases[0]):
                add(path)
        return candidates

    # The final path component may name either a child module or an item
    # inside the preceding module. Resolve module prefixes through ``mod``
    # declarations so ``#[path]`` overrides and module reachability are kept.
    for base in bases:
        if starts_override is not None:
            starts = starts_override
        elif base == module_dir and (
                import_kind in (None, "mod") or qualifier == "self"):
            starts = [file_path]
        elif base == crate_dir:
            starts = [crate_root] if crate_root is not None else []
        elif base == module_dir:
            starts = [file_path]
        else:
            starts = _rust_module_file_candidates(base)
        paths = _rust_logical_module_file_candidates(
            store, starts, parts, crate_dir, crate_root,
            require_declared=import_kind == "use")
        if import_kind == "use" and len(paths) < max(0, len(parts) - 1):
            continue
        for path in paths:
            add(path)

    # A use path may name an item directly in its base module (for example,
    # ``use helper`` or ``use super::helper``), rather than a child module.
    if import_kind == "use" and len(parts) == 1:
        item_name = parts[-1]
        for base in bases:
            if base == module_dir and (
                    qualifier == "self" or
                    (not explicit_relative and crate_root is not None and
                     _rust_edition(store, crate_root) >= 2018)):
                fallback = [file_path]
            elif base == crate_dir:
                fallback = (_rust_root_file_candidates(
                    crate_dir, file_path, crate_root)
                            if crate_root is not None else [])
            else:
                fallback = _rust_module_file_candidates(base)
            for path in fallback:
                if _rust_has_symbol(store, path, item_name):
                    add(path)
    return candidates


def _module_candidate_paths(store: IndexStore, file_id: int, module_text: str,
                            import_kind=None):
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
        if lang == "typescript" and target.suffix in _TS_RUNTIME_EXTENSION_SUBSTITUTIONS:
            # TypeScript specifiers name emitted files, so resolve source and
            # declaration extensions before the corresponding JS file.
            return [
                rel for ext in _TS_RUNTIME_EXTENSION_SUBSTITUTIONS[target.suffix]
                if (rel := rel_of(target.with_suffix(ext))) is not None
            ]
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
        if lang in ("javascript", "typescript") and not target.suffix:
            candidates.extend(target / f"index{ext}" for ext in ordered)
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
            cands.extend(
                target / f"__init__{ext}" for ext in _EXT_BY_LANG["python"]
            )
            if target.name:
                cands.extend(
                    target.with_suffix(ext) for ext in _EXT_BY_LANG["python"]
                )
        else:
            parts = module_text.split(".")
            cands = []
            # Prefer the configured project root, then fall back to ancestor
            # directories that may be on the caller's import path (for
            # example, ``src`` when indexing a repository root).
            search_dirs = [Path(".")]
            for up in [file_dir, *file_dir.parents]:
                if not _is_within(root / up, root):
                    break
                if up not in search_dirs:
                    search_dirs.append(up)
            for up in search_dirs:
                target = up.joinpath(*parts)
                cands.extend(
                    target / f"__init__{ext}" for ext in _EXT_BY_LANG["python"]
                )
                cands.extend(
                    target.with_suffix(ext) for ext in _EXT_BY_LANG["python"]
                )
        return [rel for cand in cands if (rel := rel_of(cand)) is not None]

    # --- rust: std/core/alloc are external; crate/self/super are internal --
    if lang == "rust":
        if module_text in ("std", "core", "alloc") or \
                module_text.startswith(("std::", "core::", "alloc::")):
            return []
        candidates = _rust_candidates(store, file, module_text, import_kind)
        crate_root = _rust_crate_root(store, Path(file["path"]))[1]
        if crate_root is not None:
            candidates = [
                cand for cand in candidates
                if store.file_by_path(cand.as_posix()) is not None and
                _rust_reaches(store, crate_root, cand)
            ]
        return [rel for cand in candidates
                if (rel := rel_of(cand)) is not None]

    # --- go / java: try the module text as a path under the root ----------
    if lang == "go":
        for target in _go_package_dirs(root, module_text):
            cands = []
            if target.name:
                cands.extend(target.with_name(target.name + ext)
                             for ext in _EXT_BY_LANG[lang])
            cands.append(target / "main.go")
            for cand in cands:
                rel = rel_of(cand)
                row = (
                    store.file_by_path(rel.as_posix())
                    if rel is not None else None
                )
                if row is not None and row["lang"] == "go":
                    return [rel]
            package_dir = rel_of(target)
            if package_dir is not None:
                package_files = _go_package_files(store, package_dir)
                if package_files:
                    return [package_files[0][1]]
        return []

    if module_text.endswith(".*"):
        package_or_type = module_text[:-2]
        class_files = _java_class_files(store, [package_or_type])
        if class_files:
            return [path for _, path in class_files]
        package_files = _java_package_files(store, package_or_type)
        if package_files:
            return [package_files[0][1]]
        module_text = package_or_type
    else:
        parts = module_text.split(".")
        class_names = [
            ".".join(parts[:end]) for end in range(len(parts), 0, -1)
        ]
        class_files = _java_class_files(store, class_names)
        if class_files:
            return [path for _, path in class_files]

    parts = module_text.split(".")
    target = Path(*parts)
    cands = [target.with_suffix(ext) for ext in _EXT_BY_LANG[lang]]
    return [rel for cand in cands if (rel := rel_of(cand)) is not None]


def resolve_module(store: IndexStore, file_id: int, module_text: str,
                   import_kind=None):
    """Return the file id an import statement refers to, or None."""
    for cand in _module_candidate_paths(store, file_id, module_text,
                                        import_kind):
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
        for path in _module_candidate_paths(store, file_id, imp["module"],
                                            imp["kind"]):
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
                "SELECT id, file_id, module, kind FROM imports ORDER BY id"
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
                        f"SELECT id, file_id, module, kind FROM imports "
                        f"WHERE id IN ({placeholders})",
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
            target = resolve_module(
                store, row["file_id"], row["module"], row["kind"])
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
