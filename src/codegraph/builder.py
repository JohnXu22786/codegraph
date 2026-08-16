"""Index builder: discover, parse, store, resolve — incrementally."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import ProjectConfig
from .resolver import resolve_all
from .scanner import languages, scan_text
from .scanner.walk import discover_files
from .store import IndexStore


@dataclass
class IndexReport:
    files_scanned: int = 0
    files_changed: int = 0
    files_skipped: int = 0
    files_removed: int = 0
    symbols: int = 0
    calls: int = 0
    imports: int = 0
    languages: dict = field(default_factory=dict)
    elapsed: float = 0.0


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_index(cfg: ProjectConfig, force: bool = False, quiet: bool = False,
                log=None) -> IndexReport:
    """Index (or refresh) the project described by ``cfg``.

    Incremental mode compares content hashes: unchanged files are skipped,
    changed files are re-parsed and their rows replaced atomically, files
    that disappeared are dropped. ``force=True`` rebuilds every file.
    """
    started = time.monotonic()
    report = IndexReport()
    emit = (lambda msg: None) if quiet else (log or print)

    db = Path(cfg.db_path)
    if force and db.exists():
        db.unlink()
    db.parent.mkdir(parents=True, exist_ok=True)

    store = IndexStore(str(db))
    store.set_meta("root", str(Path(cfg.root).resolve()))
    root = Path(cfg.root)

    discovered = discover_files(root, cfg)
    known = store.all_file_paths()
    seen = set()

    try:
        for rel in discovered:
            posix = rel.as_posix()
            seen.add(posix)
            try:
                data = (root / rel).read_bytes()
            except OSError as exc:  # file vanished or is unreadable mid-walk
                emit(f"warning: skipping {posix}: {exc}")
                continue
            digest = _digest(data)

            if not force and cfg.incremental:
                prev = store.file_by_path(posix)
                if prev is not None and prev["digest"] == digest:
                    report.files_skipped += 1
                    continue

            lang = languages.lang_for(posix, cfg.language_map)
            if lang is None:  # race with discovery config changes
                continue
            text = data.decode("utf-8-sig", errors="replace")
            scan = scan_text(text, lang, posix, cfg.engine)
            # digest update and payload replacement share one transaction so
            # a crash mid-replace can never leave a stale-but-skipped file
            with store.transaction():
                fid = store.upsert_file(posix, lang, len(data), digest,
                                        len(text.splitlines()), scan.module)
                store.replace_file_payload(fid, scan)
            report.files_changed += 1
            report.symbols += len(scan.symbols)
            report.calls += len(scan.calls)
            report.imports += len(scan.imports)

        for path in sorted(known - seen):
            with store.transaction():
                store.remove_file(path)
            report.files_removed += 1

        resolve_all(store)
        store.set_meta("last_indexed", store.now_iso())
        store.conn.commit()

        for row in store.conn.execute(
                "SELECT lang, COUNT(*) AS n FROM files GROUP BY lang ORDER BY lang"):
            report.languages[row["lang"]] = row["n"]
    finally:
        store.close()

    report.files_scanned = len(discovered)
    report.elapsed = time.monotonic() - started
    emit(f"indexed {report.files_scanned} files ({report.files_changed} changed, "
         f"{report.files_skipped} skipped, {report.files_removed} removed) "
         f"in {report.elapsed:.2f}s — {report.symbols} symbols, "
         f"{report.calls} calls, {report.imports} imports")
    return report
