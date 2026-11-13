# =============================================================================
# services/gateway/gateway/store.py — Almacenamiento SQLite de enlaces y sesiones
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import os
import sqlite3
import threading
from contextlib import contextmanager

from . import tokens

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE links (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    relpath TEXT NOT NULL,
    filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    max_downloads INTEGER NOT NULL,
    downloads_completed INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    job_id TEXT NOT NULL,
    label TEXT,
    revoked_at INTEGER,
    closed_at INTEGER
);
CREATE INDEX links_relpath ON links(relpath);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    counted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX sessions_link ON sessions(link_id);
"""


def compute_state(row, now):
    if row["revoked_at"] is not None:
        return "revoked"
    if row["downloads_completed"] >= row["max_downloads"]:
        return "exhausted"
    if now >= row["expires_at"]:
        return "expired"
    return "active"


class Store:
    def __init__(self, path):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        os.chmod(path, 0o600)

    def _migrate(self):
        with self._lock:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self._db.executescript(
                    f"BEGIN; {_SCHEMA} PRAGMA user_version={SCHEMA_VERSION}; COMMIT;"
                )
            elif version != SCHEMA_VERSION:
                raise RuntimeError(f"Versión de esquema no soportada: {version}")

    def locked(self):
        return self._lock

    @contextmanager
    def _tx(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def close(self):
        with self._lock:
            self._db.close()

    def _link(self, row, now):
        if row is None:
            return None
        data = dict(row)
        data["state"] = compute_state(row, now)
        return data

    def create_link(self, validator, token_hash, relpath, ttl, max_downloads, sha256,
                    created_by, job_id, label, now):
        with self._tx() as db:
            size = validator()
            link_id = tokens.new_link_id()
            db.execute(
                "INSERT INTO links (id, token_hash, relpath, filename, size_bytes, sha256,"
                " created_at, expires_at, max_downloads, created_by, job_id, label)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (link_id, token_hash, relpath, os.path.basename(relpath), size, sha256,
                 now, now + ttl, max_downloads, created_by, job_id, label),
            )
            return self._link(db.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone(), now)

    def get_link(self, link_id, now):
        with self._lock:
            return self._link(self._db.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone(), now)

    def get_link_by_hash(self, token_hash, now):
        with self._lock:
            row = self._db.execute("SELECT * FROM links WHERE token_hash=?", (token_hash,)).fetchone()
            return self._link(row, now)

    def list_links(self, now, only_active):
        with self._lock:
            rows = self._db.execute("SELECT * FROM links ORDER BY created_at DESC, rowid DESC").fetchall()
        links = [self._link(row, now) for row in rows]
        return [item for item in links if item["state"] == "active"] if only_active else links

    def count_active(self, now):
        return len(self.list_links(now, True))

    def revoke(self, link_id, now):
        with self._tx() as db:
            row = db.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone()
            if row is None:
                return None
            if row["revoked_at"] is None:
                db.execute(
                    "UPDATE links SET revoked_at=?, closed_at=COALESCE(closed_at, ?) WHERE id=?",
                    (now, now, link_id),
                )
                db.execute("DELETE FROM sessions WHERE link_id=?", (link_id,))
            return self._link(db.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone(), now)

    def open_or_reuse_session(self, link_id, now, ttl):
        """Devuelve (estado, id_sesion). estado: ok, not_found, revoked, exhausted, expired."""
        with self._tx() as db:
            link = db.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone()
            if link is None:
                return "not_found", None
            if link["revoked_at"] is not None:
                return "revoked", None
            if link["downloads_completed"] >= link["max_downloads"]:
                return "exhausted", None
            if now >= link["expires_at"]:
                return "expired", None
            open_row = db.execute(
                "SELECT id FROM sessions WHERE link_id=? AND expires_at>? ORDER BY created_at DESC, rowid DESC",
                (link_id, now),
            ).fetchone()
            if open_row:
                return "ok", open_row["id"]
            session_id = tokens.new_session_id()
            db.execute(
                "INSERT INTO sessions (id, link_id, created_at, expires_at) VALUES (?,?,?,?)",
                (session_id, link_id, now, now + ttl),
            )
            return "ok", session_id

    def get_session(self, session_id, link_id):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sessions WHERE id=? AND link_id=?", (session_id, link_id)
            ).fetchone()
            return dict(row) if row else None

    def mark_counted(self, session_id, now):
        """Cuenta la descarga una sola vez por sesión. True si esta llamada la contó."""
        with self._tx() as db:
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if session is None or session["counted"]:
                return False
            db.execute("UPDATE sessions SET counted=1 WHERE id=?", (session_id,))
            cur = db.execute(
                "UPDATE links SET downloads_completed=downloads_completed+1"
                " WHERE id=? AND downloads_completed<max_downloads AND revoked_at IS NULL",
                (session["link_id"],),
            )
            return cur.rowcount == 1

    def referenced_relpaths(self, now):
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT relpath FROM links WHERE revoked_at IS NULL AND ("
                " (downloads_completed<max_downloads AND expires_at>?)"
                " OR id IN (SELECT link_id FROM sessions WHERE expires_at>?))",
                (now, now),
            ).fetchall()
        return {row["relpath"] for row in rows}

    def sweep(self, now, keep_closed_seconds):
        """Marca enlaces cerrados, purga sesiones vencidas y filas viejas."""
        closed = []
        with self._tx() as db:
            for row in db.execute("SELECT * FROM links WHERE closed_at IS NULL").fetchall():
                state = compute_state(row, now)
                if state in ("expired", "exhausted"):
                    db.execute("UPDATE links SET closed_at=? WHERE id=?", (now, row["id"]))
                    closed.append((row["id"], state))
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            deleted = db.execute(
                "DELETE FROM links WHERE closed_at IS NOT NULL AND closed_at<=?",
                (now - keep_closed_seconds,),
            ).rowcount
        return closed, deleted
