"""Source file discovery: walk, ignore rules, size limits."""

from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path

from .languages import lang_for


def _excluded(rel: Path, cfg, is_dir: bool) -> bool:
    posix = rel.as_posix()
    for pat in cfg.exclude:
        if not pat:
            continue
        if "/" in pat:
            base = pat.rstrip("/")
            if fnmatch(posix, pat) or posix.startswith(base + "/"):
                return True
        else:
            if fnmatch(rel.name, pat) or fnmatch(posix, pat):
                return True
    return False


def _included(rel: Path, cfg) -> bool:
    if not cfg.include:
        return True
    posix = rel.as_posix()
    for pat in cfg.include:
        if not pat:
            continue
        base = pat.rstrip("/")
        if posix == base or posix.startswith(base + "/") or fnmatch(posix, pat):
            return True
    return False


def discover_files(root: Path, cfg) -> list:
    """Return the relative POSIX paths of all indexable source files.

    Directories are pruned as they are walked, so ignore rules on directory
    names are cheap and correct. Files are filtered by language registry,
    include/exclude rules and the size cap.
    """
    root = Path(root)
    max_bytes = cfg.max_file_kb * 1024
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        kept = []
        for d in sorted(dirnames):
            rel = rel_dir / d if str(rel_dir) != "." else Path(d)
            if _excluded(rel, cfg, is_dir=True):
                continue
            kept.append(d)
        dirnames[:] = kept
        for fn in sorted(filenames):
            rel = rel_dir / fn if str(rel_dir) != "." else Path(fn)
            if _excluded(rel, cfg, is_dir=False):
                continue
            if not _included(rel, cfg):
                continue
            if lang_for(rel.as_posix(), cfg.language_map) is None:
                continue
            full = root / rel
            if not full.is_file():
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            if max_bytes and size > max_bytes:
                continue
            found.append(rel)
    return sorted(found, key=lambda p: p.as_posix())
