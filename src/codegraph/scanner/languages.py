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

_PKG_STMT = re.compile(
    r"^[ \t]*package[ \t]+([\w.]+)[ \t]*;?",
    re.MULTILINE,
)
_COMMENTS = re.compile(r"//[^\r\n]*|/\*.*?\*/", re.DOTALL)


def _package_name(text):
    """Return a Go/Java package declaration, ignoring leading comments."""
    if not text:
        return None
    # Keep line breaks intact so the multiline package anchor still works.
    source = _COMMENTS.sub(
        lambda m: "".join("\n" if c == "\n" else "\r" if c == "\r" else " "
                           for c in m.group(0)),
        text,
    )
    match = _PKG_STMT.search(source)
    return match.group(1) if match else None


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
            return _package_name(text) or ""
        return ""
    rel = Path(rel_path)
    if lang == "python":
        parts = rel.with_suffix("").as_posix().split("/")
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts) if parts else ""
    if lang == "go":
        return _package_name(text) or rel.with_suffix("").as_posix()
    if lang == "java":
        return _package_name(text) or rel.with_suffix("").as_posix()
    return rel.with_suffix("").as_posix()
