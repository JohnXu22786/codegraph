"""Language registry: extension mapping, module-id derivation."""

from __future__ import annotations

import re
from pathlib import Path

# extension (lowercase, with dot) -> canonical language id
EXTENSIONS = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
}

_PKG_STMTS = {
    "go": re.compile(
        r"^\s*(?:(?://[^\n]*(?:\n|$)|/\*.*?\*/)\s*)*"
        r"package\s+([\w.]+)\s*;?",
        re.DOTALL,
    ),
    "java": re.compile(
        r"^\s*(?:(?://[^\r\n]*(?:\r\n|\n|\r|$)|/\*.*?\*/)\s*)*"
        r"package\s+([\w.]+)\s*;?",
        re.DOTALL,
    ),
}


def _declared_package(lang, text):
    match = _PKG_STMTS[lang].search(text)
    return match.group(1) if match else ""


def lang_for(rel_path, overrides=None) -> "str | None":
    """Map a relative path to a language id, or None when unsupported."""
    if overrides:
        low = Path(rel_path).suffix.lower()
        if low in overrides:
            return overrides[low]
    return EXTENSIONS.get(Path(rel_path).suffix.lower())


def module_of(rel_path, lang, text="") -> str:
    """Derive the module id of a file.

    Python uses dotted package ids ("pkg.cart", with "__init__" collapsed).
    JavaScript / TypeScript / Rust use the slash-separated relative path
    without extension ("web/index", "rustx/lib"). Go and Java prefer their
    ``package`` declaration when one is present.
    """
    if not rel_path:
        if lang in ("go", "java"):
            return _declared_package(lang, text)
        return ""
    rel = Path(rel_path)
    if lang == "python":
        parts = rel.with_suffix("").as_posix().split("/")
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts) if parts else ""
    if lang == "go":
        package = _declared_package(lang, text)
        return package or rel.with_suffix("").as_posix()
    if lang == "java":
        package = _declared_package(lang, text)
        return package or rel.with_suffix("").as_posix()
    return rel.with_suffix("").as_posix()
