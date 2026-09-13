"""Index builder: discover, parse, store, resolve — incrementally."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import ProjectConfig
from .resolver import resolve_all
from .scanner import deep, languages, scan_text
from .scanner.walk import discover_files
from .store import IndexStore

_RESOLVER_VERSION = 3


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


def _cargo_manifest_paths(root: Path, source_paths=None) -> list:
    """Return Cargo manifests on the ancestor paths of indexed sources."""
    root = Path(root).resolve()
    if source_paths is None:
        try:
            return sorted(root.rglob("Cargo.toml"))
        except OSError:
            return []

    manifests = set()
    for source in source_paths:
        path = Path(source)
        full_path = path if path.is_absolute() else root / path
        directory = full_path.parent
        while True:
            try:
                directory.relative_to(root)
            except ValueError:
                break
            candidate = directory / "Cargo.toml"
            if candidate.is_file():
                manifests.add(candidate)
            if directory == root:
                break
            directory = directory.parent
    return sorted(manifests)


def _cargo_manifest_state(root: Path, source_paths=None) -> dict:
    """Return content digests for Cargo manifests affecting Rust roots."""
    root = Path(root).resolve()
    manifests = _cargo_manifest_paths(root, source_paths)

    state = {}
    for manifest in manifests:
        if not manifest.is_file():
            continue
        try:
            data = manifest.read_bytes()
        except OSError:
            digest = None
        else:
            digest = _digest(data)
        state[manifest.relative_to(root).as_posix()] = digest
    return state


def _scan_config(cfg: ProjectConfig, include_cargo=True,
                 cargo_manifest_paths=None) -> str:
    """Serialize settings that affect per-file scan payloads."""
    language_ids = set(languages.EXTENSIONS.values())
    language_ids.update(cfg.language_map.values())
    if cfg.engine == "quick":
        providers = {lang: "quick" for lang in language_ids}
    elif cfg.engine == "deep":
        providers = {lang: "deep" for lang in language_ids}
    else:
        providers = {
            lang: "deep" if deep.supports(lang) else "quick"
            for lang in language_ids
        }
    return json.dumps(
        {"engine": cfg.engine, "language_map": cfg.language_map,
         "providers": providers,
         # A resolver change must revisit unchanged payloads in existing DBs.
         "resolver_version": _RESOLVER_VERSION,
        # Cargo roots affect resolution even though they do not affect the
        # per-file scanner payloads.  Including their state invalidates a
        # no-source-change incremental build when a root path moves.
         "cargo_manifests": (
             _cargo_manifest_state(
                 Path(cfg.root), cargo_manifest_paths) if include_cargo else {}
         )},
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
    root = Path(cfg.root)
    discovery_complete = True

    def on_discovery_error(exc):
        nonlocal discovery_complete
        discovery_complete = False
        emit(f"warning: incomplete file discovery: {exc}")

    discovered = discover_files(root, cfg, onerror=on_discovery_error)
    scan_config_complete = discovery_complete
    has_discovered_rust = any(
        languages.lang_for(rel.as_posix(), cfg.language_map) == "rust"
        for rel in discovered
    )

    db = Path(cfg.db_path)
    db.parent.mkdir(parents=True, exist_ok=True)

    # Build forced indexes off to the side so a failed rebuild cannot destroy
    # the last published index.
    temporary_db = tempfile.TemporaryDirectory(
        dir=str(db.parent), prefix=f".{db.name}-"
    ) if force else None
    build_db = (Path(temporary_db.name) / db.name
                if temporary_db is not None else db)
    store = IndexStore(str(build_db))
    build_succeeded = False
    store.set_meta("root", str(Path(cfg.root).resolve()))
    known = store.all_file_paths()
    has_indexed_rust = store.conn.execute(
        "SELECT 1 FROM files WHERE lang = ? LIMIT 1", ("rust",)
    ).fetchone() is not None
    rust_source_paths = [
        rel for rel in discovered
        if languages.lang_for(rel.as_posix(), cfg.language_map) == "rust"
    ]
    if not discovery_complete:
        # An incomplete walk may omit an existing Rust source; retain its
        # ancestor manifests until the next complete discovery.
        rust_source_paths.extend(path for path in known if path.endswith(".rs"))
    cargo_manifest_paths = _cargo_manifest_paths(root, rust_source_paths)
    scan_config = _scan_config(
        cfg, include_cargo=has_indexed_rust or has_discovered_rust,
        cargo_manifest_paths=cargo_manifest_paths)
    store.set_meta(
        "cargo_manifest_paths",
        json.dumps([
            manifest.relative_to(root.resolve()).as_posix()
            for manifest in cargo_manifest_paths
        ]),
    )
    current_scan_config = json.loads(scan_config)
    previous_scan_config = store.get_meta("scan_config")
    try:
        previous_scan_config = json.loads(previous_scan_config or "{}")
    except json.JSONDecodeError:
        previous_scan_config = {}
    if not isinstance(previous_scan_config, dict):
        previous_scan_config = {}
    scan_settings_changed = (
        previous_scan_config.get("engine") != current_scan_config["engine"] or
        previous_scan_config.get("language_map") != current_scan_config["language_map"]
    )
    cargo_metadata_changed = (
        previous_scan_config.get("cargo_manifests") !=
        current_scan_config["cargo_manifests"]
    )
    resolver_version_changed = (
        previous_scan_config.get("resolver_version") !=
        current_scan_config["resolver_version"]
    )
    previous_providers = previous_scan_config.get("providers", {})
    if not isinstance(previous_providers, dict):
        previous_providers = {}
    current_providers = current_scan_config["providers"]
    seen = set()
    changed_file_ids = set()
    recheck_call_ids = set()
    recheck_import_ids = set()
    changed_symbol_names = set()
    added_file = False
    removed_file = False
    rust_structure_changed = False
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

            lang = languages.lang_for(posix, cfg.language_map)
            if lang is None:  # race with discovery config changes
                scan_config_complete = False
                continue

            previous_mods = set()
            if prev is not None and lang == "rust":
                previous_mods = {
                    row["module"] for row in store.imports_for_file(prev["id"])
                    if row["kind"] == "mod"
                }

            if not force and cfg.incremental and not scan_settings_changed:
                if (prev is not None and prev["digest"] == digest and
                        prev["lang"] == lang and
                        previous_providers.get(lang) == current_providers.get(lang)):
                    report.files_skipped += 1
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
            if lang == "rust" and (prev is None or previous_mods or any(
                    item.kind == "mod" for item in scan.imports)):
                rust_structure_changed = True
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

        removed_paths = sorted(known - seen) if discovery_complete else []
        if removed_paths:
            # Persist the retry marker before a removal transaction commits.
            # Otherwise an interruption after removal can leave cleared
            # incoming edges with no signal for the next incremental run.
            store.set_meta("resolution_pending", "1")
        for path in removed_paths:
            old = store.file_by_path(path)
            if old is not None and old["lang"] == "rust":
                rust_structure_changed = True
            with store.transaction():
                impact = store.remove_file(path)
            recheck_call_ids.update(impact["call_ids"])
            recheck_import_ids.update(impact["import_ids"])
            changed_symbol_names.update(impact["symbol_names"])
            removed_file = True
            report.files_removed += 1

        needs_resolution = (
            changed_file_ids or recheck_call_ids or recheck_import_ids or
            changed_symbol_names or removed_file or resolution_pending or
            cargo_metadata_changed or rust_structure_changed or
            resolver_version_changed
        )
        if needs_resolution:
            # Payload transactions commit before this pass. Persist the retry
            # marker first so a failed resolution is retried on the next run
            # instead of being hidden by unchanged file digests.
            store.set_meta("resolution_pending", "1")
            if (resolution_pending or cargo_metadata_changed or
                    rust_structure_changed or resolver_version_changed):
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
            store.set_meta("resolution_pending", "0")
        # Keep a changed config pending when discovery or a discovered file
        # could not complete, so the next run retries its stale payload.
        if scan_config_complete:
            store.set_meta("scan_config", scan_config)
        store.set_meta("last_indexed", store.now_iso())
        store.conn.commit()

        for row in store.conn.execute(
                "SELECT lang, COUNT(*) AS n FROM files GROUP BY lang ORDER BY lang"):
            report.languages[row["lang"]] = row["n"]
        # A force build starts with an empty temporary DB. Do not publish a
        # partial replacement when discovery or a file read was incomplete.
        build_succeeded = scan_config_complete
    finally:
        store.close()
        if temporary_db is not None:
            try:
                if build_succeeded:
                    os.replace(build_db, db)
            finally:
                temporary_db.cleanup()

    report.files_scanned = len(discovered)
    report.elapsed = time.monotonic() - started
    emit(f"indexed {report.files_scanned} files ({report.files_changed} changed, "
         f"{report.files_skipped} skipped, {report.files_removed} removed) "
         f"in {report.elapsed:.2f}s — {report.symbols} symbols, "
         f"{report.calls} calls, {report.imports} imports")
    return report
