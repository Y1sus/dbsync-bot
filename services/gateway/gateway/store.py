# =============================================================================
# services/gateway/gateway/store.py — Almacenamiento SQLite de enlaces y sesiones
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import json
import os
import sqlite3
import threading
from contextlib import contextmanager

from . import tokens
from .coverage import covers, merge

SCHEMA_VERSION = 3

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
    closed_at INTEGER,
    keep_file INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX links_relpath ON links(relpath);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    counted INTEGER NOT NULL DEFAULT 0,
    ranges TEXT NOT NULL DEFAULT '[]',
    budget_used INTEGER NOT NULL DEFAULT 0
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
        os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600))
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
            elif version in (1, 2):
                steps = ["BEGIN;"]
                if version < 2:
                    steps.append("ALTER TABLE links ADD COLUMN keep_file INTEGER NOT NULL DEFAULT 0;")
                steps.append("ALTER TABLE sessions ADD COLUMN ranges TEXT NOT NULL DEFAULT '[]';")
                steps.append("ALTER TABLE sessions ADD COLUMN budget_used INTEGER NOT NULL DEFAULT 0;")
                steps.append(f"PRAGMA user_version={SCHEMA_VERSION}; COMMIT;")
                self._db.executescript(" ".join(steps))
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

    def revoke(self, link_id, now, keep_file=False):
        """Revoca y cierra sesiones. Devuelve (enlace, cambió) o (None, False) si no existe."""
        with self._tx() as db:
            row = db.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone()
            if row is None:
                return None, False
            changed = row["revoked_at"] is None
            if changed:
                db.execute(
                    "UPDATE links SET revoked_at=?, closed_at=COALESCE(closed_at, ?) WHERE id=?",
                    (now, now, link_id),
                )
                db.execute("DELETE FROM sessions WHERE link_id=?", (link_id,))
            db.execute("UPDATE links SET keep_file=? WHERE id=?", (1 if keep_file else 0, link_id))
            return self._link(db.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone(), now), changed

    def open_or_reuse_session(self, link_id, now, ttl, grace):
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
                (session_id, link_id, now, min(now + ttl, link["expires_at"] + grace)),
            )
            return "ok", session_id

    def get_session(self, session_id, link_id):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sessions WHERE id=? AND link_id=?", (session_id, link_id)
            ).fetchone()
            return dict(row) if row else None

    def reserve_bytes(self, session_id, length, size, now):
        """Reserva `length` bytes del presupuesto (2 x size) de la sesión: ok, session o budget."""
        with self._tx() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None or row["expires_at"] <= now:
                return "session"
            if row["budget_used"] + length > 2 * size:
                return "budget"
            db.execute("UPDATE sessions SET budget_used=budget_used+? WHERE id=?", (length, session_id))
            return "ok"

    def record_sent(self, session_id, start, sent, reserved, credited, size, now, grace):
        """Registra bytes enviados, devuelve lo no enviado y cuenta si la unión cubre [0, size)."""
        with self._tx() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                return False
            ranges = merge(json.loads(row["ranges"]), start, start + credited)
            used = max(0, row["budget_used"] - (reserved - sent))
            counted_now = False
            counted, expires = row["counted"], row["expires_at"]
            if not counted and covers(ranges, size):
                counted, expires = 1, min(expires, now + grace)
                cur = db.execute(
                    "UPDATE links SET downloads_completed=downloads_completed+1"
                    " WHERE id=? AND downloads_completed<max_downloads AND revoked_at IS NULL",
                    (row["link_id"],),
                )
                counted_now = cur.rowcount == 1
            db.execute(
                "UPDATE sessions SET ranges=?, budget_used=?, counted=?, expires_at=? WHERE id=?",
                (json.dumps(ranges, separators=(",", ":")), used, counted, expires, session_id),
            )
            return counted_now

    def file_status(self, relpath, now):
        """Resume los enlaces de un relpath para decidir el borrado (llamar bajo locked())."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM links WHERE relpath=?", (relpath,)).fetchall()
            open_sessions = self._db.execute(
                "SELECT COUNT(*) FROM sessions s JOIN links l ON l.id=s.link_id"
                " WHERE l.relpath=? AND s.expires_at>?",
                (relpath, now),
            ).fetchone()[0]
        states = [compute_state(row, now) for row in rows]
        last = None
        for row, state in zip(rows, states):
            if state == "active":
                continue
            if row["revoked_at"] is not None:
                moment = row["revoked_at"]
            else:
                moment = row["closed_at"] if row["closed_at"] is not None else row["expires_at"]
            if last is None or moment >= last[0]:
                last = (moment, row["id"], state)
        return {
            "rows": len(rows),
            "link_ids": [row["id"] for row in rows],
            "all_terminal": bool(rows) and all(state != "active" for state in states),
            "open_sessions": open_sessions > 0,
            "kept": any(row["keep_file"] for row in rows),
            "last": last,
        }

    def close_expired(self, now):
        """Marca enlaces expirados/agotados y purga sesiones vencidas."""
        closed = []
        with self._tx() as db:
            for row in db.execute("SELECT * FROM links WHERE closed_at IS NULL").fetchall():
                state = compute_state(row, now)
                if state in ("expired", "exhausted"):
                    db.execute("UPDATE links SET closed_at=? WHERE id=?", (now, row["id"]))
                    closed.append((row["id"], state))
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        return closed

    def purge_closed(self, now, keep_closed_seconds):
        with self._tx() as db:
            return db.execute(
                "DELETE FROM links WHERE closed_at IS NOT NULL AND closed_at<=?",
                (now - keep_closed_seconds,),
            ).rowcount
