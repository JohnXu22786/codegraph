"""Index builder: discover, parse, store, resolve — incrementally."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .config import ProjectConfig
from .resolver import resolve_all
from .scanner import languages, scan_text
from .scanner.walk import discover_files
from .store import IndexStore

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses msvcrt below
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows uses fcntl above
    msvcrt = None


_ROOT_LOCKS = {}
_ROOT_LOCKS_GUARD = threading.Lock()


class _StaleBuildError(RuntimeError):
    """Raised when source contents changed before a build could commit."""


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


def _source_snapshot(root: Path, cfg: ProjectConfig, discovered=None) -> dict:
    """Return content digests for the files visible to this build."""
    if discovered is None:
        discovered = discover_files(root, cfg)
    snapshot = {}
    for rel in discovered:
        posix = rel.as_posix()
        try:
            data = (root / rel).read_bytes()
        except OSError:
            snapshot[posix] = None
        else:
            snapshot[posix] = _digest(data)
    return snapshot


def _assert_file_fresh(root: Path, path: str, digest: str):
    """Reject a payload whose source changed while it was being scanned."""
    try:
        current = _digest((root / path).read_bytes())
    except OSError as exc:
        raise _StaleBuildError(
            f"source changed during index build ({path}); retry"
        ) from exc
    if current != digest:
        raise _StaleBuildError(
            f"source changed during index build ({path}); retry"
        )


def _assert_project_fresh(root: Path, cfg: ProjectConfig, expected: dict,
                          expected_paths):
    """Reject a build whose discovered source set or contents changed."""
    discovered = discover_files(root, cfg)
    current_paths = {rel.as_posix() for rel in discovered}
    if current_paths != expected_paths:
        raise _StaleBuildError("project sources changed during index build; retry")
    current = _source_snapshot(root, cfg, discovered)
    if any(current.get(path) != digest for path, digest in expected.items()):
        raise _StaleBuildError("project sources changed during index build; retry")


@contextmanager
def _root_build_lock(root: Path, db_path: str):
    """Serialize builds for one project across threads and processes."""
    root = Path(root).resolve()
    key = str(root)
    with _ROOT_LOCKS_GUARD:
        thread_lock = _ROOT_LOCKS.setdefault(key, threading.Lock())

    with thread_lock:
        lock_parent = Path(db_path).resolve().parent
        lock_name = f".codegraph-build-{_digest(str(root).encode())[:16]}.lock"
        lock_path = lock_parent / lock_name
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:  # pragma: no cover - Windows only
                lock_file.seek(0)
                if not lock_file.read(1):
                    lock_file.seek(0)
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                while True:
                    try:
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows only
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UN, 1)


def build_index(cfg: ProjectConfig, force: bool = False, quiet: bool = False,
                log=None) -> IndexReport:
    """Index (or refresh) the project described by ``cfg``.

    Incremental mode compares content hashes and scan settings: unchanged
    files are skipped, changed files are re-parsed and their rows replaced
    atomically, files that disappeared are dropped. ``force=True`` rebuilds
    every file. Builds for the same root are serialized, and a source change
    during a build raises ``RuntimeError`` before the stale payload is
    committed.
    """
    with _root_build_lock(Path(cfg.root), cfg.db_path):
        return _build_index(cfg, force=force, quiet=quiet, log=log)


def _build_index(cfg: ProjectConfig, force: bool = False, quiet: bool = False,
                 log=None) -> IndexReport:
    """Build an index while the caller holds the per-root build lock."""
    started = time.monotonic()
    report = IndexReport()
    emit = (lambda msg: None) if quiet else (log or print)

    db = Path(cfg.db_path)
    prebuild_db = sqlite3.connect(":memory:")
    snapshot_taken = False
    if force and db.exists():
        existing_db = sqlite3.connect(str(db))
        try:
            existing_db.backup(prebuild_db)
            snapshot_taken = True
        finally:
            existing_db.close()
    if force and db.exists():
        db.unlink()
    db.parent.mkdir(parents=True, exist_ok=True)

    store = IndexStore(str(db))
    if not snapshot_taken:
        store.conn.backup(prebuild_db)
    store.set_meta("root", str(Path(cfg.root).resolve()))
    scan_config = _scan_config(cfg)
    scan_config_changed = store.get_meta("scan_config") != scan_config
    root = Path(cfg.root).resolve()

    discovery_complete = True

    def on_discovery_error(exc):
        nonlocal discovery_complete
        discovery_complete = False
        emit(f"warning: incomplete file discovery: {exc}")

    discovered = discover_files(root, cfg, onerror=on_discovery_error)
    scan_config_complete = discovery_complete
    source_snapshot = {}
    source_paths = {rel.as_posix() for rel in discovered}
    known = store.all_file_paths()
    seen = set()
    changed_file_ids = set()
    recheck_call_ids = set()
    recheck_import_ids = set()
    changed_symbol_names = set()
    added_file = False
    removed_file = False
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
            source_snapshot[posix] = digest
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
                _assert_file_fresh(root, posix, digest)
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

        if discovery_complete:
            _assert_project_fresh(root, cfg, source_snapshot, source_paths)
        removed_paths = sorted(known - seen) if discovery_complete else []
        if removed_paths:
            # Persist the retry marker before a removal transaction commits.
            # Otherwise an interruption after removal can leave cleared
            # incoming edges with no signal for the next incremental run.
            store.set_meta("resolution_pending", "1")
        for path in removed_paths:
            with store.transaction():
                impact = store.remove_file(path)
            recheck_call_ids.update(impact["call_ids"])
            recheck_import_ids.update(impact["import_ids"])
            changed_symbol_names.update(impact["symbol_names"])
            removed_file = True
            report.files_removed += 1

        if discovery_complete:
            _assert_project_fresh(root, cfg, source_snapshot, source_paths)
        needs_resolution = (
            changed_file_ids or recheck_call_ids or recheck_import_ids or
            changed_symbol_names or removed_file or resolution_pending
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
                    recheck_all_imports=added_file or removed_file,
                )
            # Keep the retry marker set until the final freshness check and
            # metadata commit succeed.
        # Keep a changed config pending when a discovered file could not be
        # read or mapped, so the next run retries its stale payload.
        with store.transaction():
            if discovery_complete:
                _assert_project_fresh(root, cfg, source_snapshot, source_paths)
            if needs_resolution:
                store.set_meta("resolution_pending", "0")
            if scan_config_complete:
                store.set_meta("scan_config", scan_config)
            store.set_meta("last_indexed", store.now_iso())

        for row in store.conn.execute(
                "SELECT lang, COUNT(*) AS n FROM files GROUP BY lang ORDER BY lang"):
            report.languages[row["lang"]] = row["n"]
    except _StaleBuildError:
        # Earlier payload transactions are intentionally committed so a
        # failed scan can be retried. A freshness failure is different: the
        # whole build saw a mixed source snapshot, so restore its starting DB.
        store.conn.rollback()
        prebuild_db.backup(store.conn)
        raise
    finally:
        store.close()
        prebuild_db.close()

    report.files_scanned = len(discovered)
    report.elapsed = time.monotonic() - started
    emit(f"indexed {report.files_scanned} files ({report.files_changed} changed, "
         f"{report.files_skipped} skipped, {report.files_removed} removed) "
         f"in {report.elapsed:.2f}s — {report.symbols} symbols, "
         f"{report.calls} calls, {report.imports} imports")
    return report
