"""SQLite storage: schema, transactional payload replacement, FTS search."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .models import FileScan

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY,
    path       TEXT NOT NULL UNIQUE,
    lang       TEXT NOT NULL,
    module     TEXT NOT NULL DEFAULT '',
    size       INTEGER NOT NULL DEFAULT 0,
    digest     TEXT NOT NULL DEFAULT '',
    lines      INTEGER NOT NULL DEFAULT 0,
    indexed_at REAL
);

CREATE TABLE IF NOT EXISTS symbols (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,
    name      TEXT NOT NULL,
    qualname  TEXT NOT NULL,
    parent    TEXT NOT NULL DEFAULT '',
    start_line INTEGER NOT NULL,
    end_line  INTEGER NOT NULL,
    signature TEXT NOT NULL DEFAULT '',
    doc       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS calls (
    id         INTEGER PRIMARY KEY,
    caller_id  INTEGER,
    caller_name TEXT NOT NULL DEFAULT '',
    callee     TEXT NOT NULL,
    callee_id  INTEGER,
    file_id    INTEGER NOT NULL,
    line       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL,
    module     TEXT NOT NULL,
    target_id  INTEGER,
    names      TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT '',
    line       INTEGER NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS sym_fts USING fts5(
    qualname, name, doc, signature
);

CREATE INDEX IF NOT EXISTS idx_sym_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_sym_qual ON symbols(qualname);
CREATE INDEX IF NOT EXISTS idx_sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee);
CREATE INDEX IF NOT EXISTS idx_calls_callee_id ON calls(callee_id);
CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_id);
CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(module);
CREATE INDEX IF NOT EXISTS idx_imports_target ON imports(target_id);
"""


class _Row(sqlite3.Row):
    """Row with attribute access (``row.digest``) in addition to indexing."""

    def __getattr__(self, name):
        try:
            return self[name]
        except IndexError as exc:
            raise AttributeError(name) from exc


class IndexStore:
    """Thin persistence layer over a single SQLite database file."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = _Row
        self.conn.isolation_level = None  # autocommit; explicit BEGIN in transaction()
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # -- meta ---------------------------------------------------------------

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # -- transactions ---------------------------------------------------------

    @contextmanager
    def transaction(self):
        """Atomic block for payload replacement / removal."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    # -- files ----------------------------------------------------------------

    def upsert_file(self, path, lang, size, digest, lines, module="") -> int:
        self.conn.execute(
            "INSERT INTO files(path, lang, module, size, digest, lines, indexed_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET lang = excluded.lang, "
            "module = excluded.module, size = excluded.size, "
            "digest = excluded.digest, lines = excluded.lines, "
            "indexed_at = excluded.indexed_at",
            (path, lang, module, size, digest, lines, self.now_iso()),
        )
        row = self.conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
        return row["id"]

    def file_by_path(self, path):
        return self.conn.execute(
            "SELECT * FROM files WHERE path = ?", (path,)
        ).fetchone()

    def file_by_id(self, file_id):
        return self.conn.execute(
            "SELECT * FROM files WHERE id = ?", (file_id,)
        ).fetchone()

    def file_by_module(self, module):
        return self.conn.execute(
            "SELECT * FROM files WHERE module = ? ORDER BY id LIMIT 1", (module,)
        ).fetchone()

    def all_file_paths(self):
        return {r["path"] for r in self.conn.execute("SELECT path FROM files")}

    def known_digests(self):
        return {r["path"]: r["digest"] for r in self.conn.execute("SELECT path, digest FROM files")}

    def remove_file(self, path):
        row = self.conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
        if row is None:
            return
        fid = row["id"]
        sym_ids = [r["id"] for r in
                   self.conn.execute("SELECT id FROM symbols WHERE file_id = ?", (fid,))]
        for sid in sym_ids:
            self.conn.execute("DELETE FROM sym_fts WHERE rowid = ?", (sid,))
        self.conn.execute("DELETE FROM calls WHERE file_id = ?", (fid,))
        self.conn.execute("DELETE FROM imports WHERE file_id = ?", (fid,))
        self.conn.execute("DELETE FROM symbols WHERE file_id = ?", (fid,))
        self.conn.execute("DELETE FROM files WHERE id = ?", (fid,))

    # -- payloads -------------------------------------------------------------

    def replace_file_payload(self, file_id: int, scan: FileScan):
        """Replace every row derived from one file (call inside a transaction)."""
        self.conn.execute("DELETE FROM calls WHERE file_id = ?", (file_id,))
        self.conn.execute("DELETE FROM imports WHERE file_id = ?", (file_id,))
        old = [r["id"] for r in
               self.conn.execute("SELECT id FROM symbols WHERE file_id = ?", (file_id,))]
        for sid in old:
            self.conn.execute("DELETE FROM sym_fts WHERE rowid = ?", (sid,))
        self.conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))

        for s in scan.symbols:
            cur = self.conn.execute(
                "INSERT INTO symbols(file_id, kind, name, qualname, parent, "
                "start_line, end_line, signature, doc) VALUES(?,?,?,?,?,?,?,?,?)",
                (file_id, s.kind, s.name, s.qualname, s.parent,
                 s.start, s.end, s.signature, s.doc),
            )
            sid = cur.lastrowid
            self.conn.execute(
                "INSERT INTO sym_fts(rowid, qualname, name, doc, signature) "
                "VALUES(?,?,?,?,?)",
                (sid, s.qualname, s.name, s.doc, s.signature),
            )
        for c in scan.calls:
            self.conn.execute(
                "INSERT INTO calls(file_id, caller_name, callee, line) VALUES(?,?,?,?)",
                (file_id, c.caller, c.callee, c.line),
            )
        for i in scan.imports:
            self.conn.execute(
                "INSERT INTO imports(file_id, module, names, kind, line) VALUES(?,?,?,?,?)",
                (file_id, i.module, json.dumps(i.names, ensure_ascii=False), i.kind, i.line),
            )

    # -- reads -----------------------------------------------------------------

    def symbols_for_file(self, file_id):
        return self.conn.execute(
            "SELECT * FROM symbols WHERE file_id = ? ORDER BY start_line", (file_id,)
        ).fetchall()

    def imports_for_file(self, file_id):
        return self.conn.execute(
            "SELECT * FROM imports WHERE file_id = ? ORDER BY line", (file_id,)
        ).fetchall()

    def symbol_by_qualname(self, qualname):
        return self.conn.execute(
            "SELECT * FROM symbols WHERE qualname = ? ORDER BY id LIMIT 1", (qualname,)
        ).fetchone()

    def symbol_by_id(self, symbol_id):
        return self.conn.execute(
            "SELECT * FROM symbols WHERE id = ?", (symbol_id,)
        ).fetchone()

    def symbols_by_name(self, name, limit=10):
        return self.conn.execute(
            "SELECT * FROM symbols WHERE name = ? ORDER BY id LIMIT ?", (name, limit)
        ).fetchall()

    def find_call(self, callee=None, callee_id=None, file_id=None):
        """First call row matching the given columns (used by tests/CLI)."""
        where, params = [], []
        for col, val in (("callee", callee), ("callee_id", callee_id),
                         ("file_id", file_id)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        if not where:
            return None
        return self.conn.execute(
            f"SELECT * FROM calls WHERE {' AND '.join(where)} ORDER BY id LIMIT 1",
            params,
        ).fetchone()

    def find_import(self, **kwargs):
        where = " AND ".join(f"{k} = ?" for k in kwargs)
        return self.conn.execute(
            f"SELECT * FROM imports WHERE {where} ORDER BY id LIMIT 1",
            tuple(kwargs.values()),
        ).fetchone()

    def count_rows(self, table):
        return self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    # -- full text search --------------------------------------------------------

    def search(self, text: str, limit: int = 20):
        """FTS5 lookup over symbols; falls back to LIKE when the query is
        not valid FTS5 syntax (e.g. stray quotes from the model)."""
        text = text.strip()
        if not text:
            return []
        try:
            rows = self.conn.execute(
                "SELECT s.id, s.kind, s.qualname, s.name, s.doc, s.signature, "
                "       s.start_line, s.end_line, f.path "
                "FROM sym_fts JOIN symbols s ON s.id = sym_fts.rowid "
                "JOIN files f ON f.id = s.file_id "
                "WHERE sym_fts MATCH ? "
                "ORDER BY bm25(sym_fts) LIMIT ?",
                (text, limit),
            )
            out = [dict(r) for r in rows]
        except sqlite3.OperationalError:
            like = f"%{text}%"
            rows = self.conn.execute(
                "SELECT s.id, s.kind, s.qualname, s.name, s.doc, s.signature, "
                "       s.start_line, s.end_line, f.path "
                "FROM symbols s JOIN files f ON f.id = s.file_id "
                "WHERE s.qualname LIKE ? OR s.name LIKE ? OR s.doc LIKE ? "
                "ORDER BY s.qualname LIMIT ?",
                (like, like, like, limit),
            )
            out = [dict(r) for r in rows]
        return out
