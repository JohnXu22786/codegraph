"""Scanner package: file discovery and per-file extraction.

``scan_text`` is the single entry point used by the index builder. The
provider is chosen per file via ``engine``:

* ``"auto"``  — tree-sitter when a grammar is installed, regex otherwise;
* ``"deep"``  — tree-sitter only (raises when the grammar is missing);
* ``"quick"`` — regex only, always available.
"""

from __future__ import annotations

from ..models import FileScan
from . import deep, languages, quick, walk  # noqa: F401  (re-exported API)


def provider_for(lang: str, engine: str = "auto"):
    """Return the callable that scans a file of ``lang`` under ``engine``."""
    if engine == "quick":
        return quick.quick_scan
    if engine == "deep":
        if deep.supports(lang):
            return deep.deep_scan
        raise RuntimeError(f"deep scanner has no tree-sitter grammar for {lang!r}")
    # auto
    if deep.supports(lang):
        return deep.deep_scan
    return quick.quick_scan


def scan_text(text: str, lang: str, rel_path=None, engine: str = "auto") -> FileScan:
    """Extract symbols, calls and imports from a source file."""
    provider = provider_for(lang, engine)
    return provider(text, lang, rel_path)
