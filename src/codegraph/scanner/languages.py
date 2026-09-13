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
    r"^[ \t\f]*package[ \t\n\f]+"
    r"(?P<name>[\w$]+(?:[ \t\n\f]*\.[ \t\n\f]*[\w$]+)*)"
    r"[ \t\f]*(?:;[ \t\f]*|(?=\n|$))",
    re.MULTILINE,
)
_ANNOTATION_NAME = re.compile(r"[\w$]+")


def _mask_non_code(text):
    """Blank comments and literals while preserving source line breaks."""
    masked = list(text)
    length = len(text)

    def blank(start, end):
        for index in range(start, end):
            if masked[index] not in "\r\n":
                masked[index] = " "

    index = 0
    while index < length:
        if text.startswith("//", index):
            end = index + 2
            while end < length and text[end] not in "\r\n":
                end += 1
            blank(index, end)
            index = end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = length if end < 0 else end + 2
            blank(index, end)
            index = end
            continue

        quote = None
        if text.startswith('"""', index):
            quote = '"""'
            end = index + len(quote)
            escaped = False
            while end < length:
                if escaped:
                    escaped = False
                    end += 1
                elif text[end] == "\\":
                    escaped = True
                    end += 1
                elif text.startswith(quote, end):
                    end += len(quote)
                    break
                else:
                    end += 1
        elif text[index] in ('"', "'", "`"):
            quote = text[index]
            end = index + 1
            while end < length:
                if quote != "`" and text[end] == "\\":
                    end = min(length, end + 2)
                elif text[end] == quote:
                    end += 1
                    break
                else:
                    end += 1
        if quote is not None:
            blank(index, end)
            index = end
            continue
        index += 1

    return "".join(masked)


def _mask_java_annotations(text):
    """Blank Java package annotations while preserving source line breaks."""
    masked = list(text)
    length = len(text)
    whitespace = " \t\n\f"

    def blank(start, end):
        for index in range(start, end):
            if masked[index] != "\n":
                masked[index] = " "

    index = 0
    while index < length:
        if text[index] != "@":
            index += 1
            continue

        name = _ANNOTATION_NAME.match(text, index + 1)
        if not name:
            index += 1
            continue
        end = name.end()
        while True:
            dot = end
            while dot < length and text[dot] in whitespace:
                dot += 1
            if dot >= length or text[dot] != ".":
                break
            next_name = _ANNOTATION_NAME.match(text, dot + 1)
            if not next_name:
                break
            end = next_name.end()

        args = end
        while args < length and text[args] in whitespace:
            args += 1
        if args < length and text[args] == "(":
            depth = 1
            args += 1
            while args < length and depth:
                if text[args] == "(":
                    depth += 1
                elif text[args] == ")":
                    depth -= 1
                args += 1
            end = args
        blank(index, end)
        index = end

    return "".join(masked)


def _package_name(text, lang):
    """Return a Go/Java package declaration outside comments and literals."""
    if not text:
        return None
    # Python's multiline anchor only recognizes ``\n`` as a line boundary.
    # Normalize CR-only and CRLF source before masking comments and literals.
    source = text.replace("\r\n", "\n").replace("\r", "\n")
    source = _mask_non_code(source)
    if lang == "java":
        source = _mask_java_annotations(source)
    match = _PKG_STMT.search(source)
    if not match:
        return None
    return re.sub(r"[ \t\n\f]*\.[ \t\n\f]*", ".", match.group("name"))


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
            return _package_name(text, lang) or ""
        return ""
    rel = Path(rel_path)
    if lang == "python":
        parts = rel.with_suffix("").as_posix().split("/")
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts) if parts else ""
    if lang == "go":
        return _package_name(text, lang) or rel.with_suffix("").as_posix()
    if lang == "java":
        return _package_name(text, lang) or rel.with_suffix("").as_posix()
    return rel.with_suffix("").as_posix()
