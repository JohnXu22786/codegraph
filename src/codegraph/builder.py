"""Index builder: discover, parse, store, resolve — incrementally."""

from __future__ import annotations

import hashlib
import json
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


def _scan_config(cfg: ProjectConfig) -> str:
    """Serialize settings that affect per-file scan payloads."""
    return json.dumps(
        {"engine": cfg.engine, "language_map": cfg.language_map},
        sort_keys=True,
        separators=(",", ":"),
    )


def build_index(cfg: ProjectConfig, force: bool = False, quiet: bool = False,
                log=None) -> IndexReport:
    """Index (or refresh) the project described by ``cfg``.

    Incremental mode compares content hashes and scan settings: unchanged
    files are skipped, changed files are re-parsed and their rows replaced
    atomically, files that disappeared are dropped. ``force=True`` rebuilds
    every file.
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
    scan_config = _scan_config(cfg)
    scan_config_changed = store.get_meta("scan_config") != scan_config
    scan_config_complete = True
    root = Path(cfg.root)

    discovered = discover_files(root, cfg)
    known = store.all_file_paths()
    seen = set()
    changed_file_ids = set()
    recheck_call_ids = set()
    recheck_import_ids = set()
    changed_symbol_names = set()
    added_file = False
    resolution_pending = store.get_meta("resolution_pending") == "1"

    try:
        for rel in discovered:
            posix = rel.as_posix()
            seen.add(posix)
            try:
                data = (root / rel).read_bytes()
            except OSError as exc:  # file vanished or is unreadable mid-walk
                scan_config_complete = False
                emit(f"warning: skipping {posix}: {exc}")
                continue
            digest = _digest(data)
            prev = store.file_by_path(posix)

            if not force and cfg.incremental and not scan_config_changed:
                if prev is not None and prev["digest"] == digest:
                    report.files_skipped += 1
                    continue

            lang = languages.lang_for(posix, cfg.language_map)
            if lang is None:  # race with discovery config changes
                scan_config_complete = False
                continue
            text = data.decode("utf-8-sig", errors="replace")
            # Mark before scanning so a later scan failure preserves a retry
            # marker for payloads committed earlier in this run.
            store.set_meta("resolution_pending", "1")
            scan = scan_text(text, lang, posix, cfg.engine)
            # digest update and payload replacement share one transaction so
            # a crash mid-replace can never leave a stale-but-skipped file
            with store.transaction():
                fid = store.upsert_file(posix, lang, len(data), digest,
                                        len(text.splitlines()), scan.module)
                impact = store.replace_file_payload(fid, scan)
            changed_file_ids.add(fid)
            recheck_call_ids.update(impact["call_ids"])
            recheck_import_ids.update(impact["import_ids"])
            changed_symbol_names.update(impact["symbol_names"])
            changed_symbol_names.update(s.name for s in scan.symbols)
            if prev is None:
                added_file = True
            report.files_changed += 1
            report.symbols += len(scan.symbols)
            report.calls += len(scan.calls)
            report.imports += len(scan.imports)

        for path in sorted(known - seen):
            with store.transaction():
                impact = store.remove_file(path)
            recheck_call_ids.update(impact["call_ids"])
            recheck_import_ids.update(impact["import_ids"])
            changed_symbol_names.update(impact["symbol_names"])
            report.files_removed += 1

        needs_resolution = (
            changed_file_ids or recheck_call_ids or recheck_import_ids or
            changed_symbol_names or resolution_pending
        )
        if needs_resolution:
            # Payload transactions commit before this pass. Persist the retry
            # marker first so a failed resolution is retried on the next run
            # instead of being hidden by unchanged file digests.
            store.set_meta("resolution_pending", "1")
            if resolution_pending:
                resolve_all(store)
            else:
                resolve_all(
                    store,
                    file_ids=changed_file_ids,
                    call_ids=recheck_call_ids,
                    import_ids=recheck_import_ids,
                    symbol_names=changed_symbol_names,
                    recheck_all_imports=added_file,
                )
            store.set_meta("resolution_pending", "0")
        # Keep a changed config pending when a discovered file could not be
        # read or mapped, so the next run retries its stale payload.
        if scan_config_complete:
            store.set_meta("scan_config", scan_config)
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
