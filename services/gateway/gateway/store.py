# =============================================================================
# services/gateway/gateway/store.py — Almacenamiento SQLite de enlaces, archivos y sesiones (esquema v4)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import json
import os
import sqlite3
import threading
from contextlib import contextmanager

from . import tokens
from .coverage import covers, merge

SCHEMA_VERSION = 5

_SCHEMA = """
CREATE TABLE links (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    max_downloads INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    job_id TEXT NOT NULL,
    label TEXT,
    revoked_at INTEGER,
    closed_at INTEGER,
    keep_file INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE link_files (
    link_id TEXT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    relpath TEXT NOT NULL,
    filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT,
    kind TEXT NOT NULL,
    downloads_completed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (link_id, idx)
);
CREATE INDEX link_files_relpath ON link_files(relpath);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    counted INTEGER NOT NULL DEFAULT 0,
    ranges TEXT NOT NULL DEFAULT '[]',
    budget_used INTEGER NOT NULL DEFAULT 0,
    sent_total INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX sessions_link ON sessions(link_id, idx);
"""

_V1_TO_V2 = "ALTER TABLE links ADD COLUMN keep_file INTEGER NOT NULL DEFAULT 0;"
_V2_TO_V3 = (
    "ALTER TABLE sessions ADD COLUMN ranges TEXT NOT NULL DEFAULT '[]';"
    " ALTER TABLE sessions ADD COLUMN budget_used INTEGER NOT NULL DEFAULT 0;"
)
# Cada enlace v3 pasa a ser un enlace de un archivo (índice 0); sus sesiones, contadores y estado se conservan.
_V3_TO_V4 = """
CREATE TABLE links_v4 (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    max_downloads INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    job_id TEXT NOT NULL,
    label TEXT,
    revoked_at INTEGER,
    closed_at INTEGER,
    keep_file INTEGER NOT NULL DEFAULT 0
);
INSERT INTO links_v4 SELECT id, token_hash, created_at, expires_at, max_downloads, created_by, job_id, label,
    revoked_at, closed_at, keep_file FROM links;
CREATE TABLE link_files (
    link_id TEXT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    relpath TEXT NOT NULL,
    filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT,
    kind TEXT NOT NULL,
    downloads_completed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (link_id, idx)
);
INSERT INTO link_files SELECT id, 0, relpath, filename, size_bytes, sha256,
    CASE WHEN relpath LIKE '%.sql.gz' THEN 'db' ELSE 'code' END, downloads_completed FROM links;
CREATE TABLE sessions_v4 (
    id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    counted INTEGER NOT NULL DEFAULT 0,
    ranges TEXT NOT NULL DEFAULT '[]',
    budget_used INTEGER NOT NULL DEFAULT 0
);
INSERT INTO sessions_v4 SELECT id, link_id, 0, created_at, expires_at, counted, ranges, budget_used FROM sessions;
DROP TABLE sessions;
DROP TABLE links;
ALTER TABLE links_v4 RENAME TO links;
ALTER TABLE sessions_v4 RENAME TO sessions;
CREATE INDEX link_files_relpath ON link_files(relpath);
CREATE INDEX sessions_link ON sessions(link_id, idx);
"""
# Total de bytes enviados por sesión; en una sesión ya existente (servicio parado) coincide con budget_used.
_V4_TO_V5 = (
    "ALTER TABLE sessions ADD COLUMN sent_total INTEGER NOT NULL DEFAULT 0;"
    " UPDATE sessions SET sent_total=budget_used;"
)
_STEPS = {1: _V1_TO_V2, 2: _V2_TO_V3, 3: _V3_TO_V4, 4: _V4_TO_V5}


def file_state(file_row, max_downloads):
    return "exhausted" if file_row["downloads_completed"] >= max_downloads else "active"


def compute_state(link, files, now):
    """revoked > exhausted (todos los archivos agotados) > expired > active."""
    if link["revoked_at"] is not None:
        return "revoked"
    if files and all(item["downloads_completed"] >= link["max_downloads"] for item in files):
        return "exhausted"
    if now >= link["expires_at"]:
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
                return
            if version not in _STEPS and version != SCHEMA_VERSION:
                raise RuntimeError(f"Versión de esquema no soportada: {version}")
            while version < SCHEMA_VERSION:
                self._db.execute("PRAGMA foreign_keys=OFF")
                self._db.executescript(
                    f"BEGIN; {_STEPS[version]} PRAGMA user_version={version + 1}; COMMIT;"
                )
                self._db.execute("PRAGMA foreign_keys=ON")
                version += 1

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

    def _files(self, link_ids):
        marks = ",".join("?" * len(link_ids))
        rows = self._db.execute(
            f"SELECT * FROM link_files WHERE link_id IN ({marks}) ORDER BY link_id, idx", list(link_ids)
        ).fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault(row["link_id"], []).append(dict(row))
        return grouped

    def _links(self, rows, now):
        if not rows:
            return []
        grouped = self._files([row["id"] for row in rows])
        links = []
        for row in rows:
            data = dict(row)
            files = grouped.get(row["id"], [])
            for item in files:
                item["state"] = file_state(item, row["max_downloads"])
            data["files"] = files
            data["state"] = compute_state(row, files, now)
            links.append(data)
        return links

    def _one(self, row, now):
        return self._links([row], now)[0] if row is not None else None

    def create_link(self, validator, token_hash, specs, ttl, max_downloads, created_by, job_id, label, now):
        """`specs` es una lista de (relpath, sha256, kind); `validator()` devuelve los tamaños bajo el candado."""
        with self._tx() as db:
            sizes = validator()
            link_id = tokens.new_link_id()
            db.execute(
                "INSERT INTO links (id, token_hash, created_at, expires_at, max_downloads, created_by, job_id, label)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (link_id, token_hash, now, now + ttl, max_downloads, created_by, job_id, label),
            )
            for index, ((relpath, sha256, kind), size) in enumerate(zip(specs, sizes)):
                db.execute(
                    "INSERT INTO link_files (link_id, idx, relpath, filename, size_bytes, sha256, kind)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (link_id, index, relpath, os.path.basename(relpath), size, sha256, kind),
                )
            return self._one(db.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone(), now)

    def get_link(self, link_id, now):
        with self._lock:
            return self._one(self._db.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone(), now)

    def get_link_by_hash(self, token_hash, now):
        with self._lock:
            row = self._db.execute("SELECT * FROM links WHERE token_hash=?", (token_hash,)).fetchone()
            return self._one(row, now)

    def list_links(self, now, only_active):
        with self._lock:
            rows = self._db.execute("SELECT * FROM links ORDER BY created_at DESC, rowid DESC").fetchall()
            links = self._links(rows, now)
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
            return self._one(db.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone(), now), changed

    def open_or_reuse_session(self, link_id, idx, now, ttl, grace):
        """Devuelve (estado, id_sesion). estado: ok, not_found, revoked, exhausted, expired."""
        with self._tx() as db:
            link = db.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone()
            item = db.execute("SELECT * FROM link_files WHERE link_id=? AND idx=?", (link_id, idx)).fetchone()
            if link is None or item is None:
                return "not_found", None
            if link["revoked_at"] is not None:
                return "revoked", None
            if item["downloads_completed"] >= link["max_downloads"]:
                return "exhausted", None
            if now >= link["expires_at"]:
                return "expired", None
            open_row = db.execute(
                "SELECT id FROM sessions WHERE link_id=? AND idx=? AND expires_at>?"
                " ORDER BY created_at DESC, rowid DESC",
                (link_id, idx, now),
            ).fetchone()
            if open_row:
                return "ok", open_row["id"]
            session_id = tokens.new_session_id()
            db.execute(
                "INSERT INTO sessions (id, link_id, idx, created_at, expires_at) VALUES (?,?,?,?,?)",
                (session_id, link_id, idx, now, min(now + ttl, link["expires_at"] + grace)),
            )
            return "ok", session_id

    def get_session(self, session_id, link_id, idx):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sessions WHERE id=? AND link_id=? AND idx=?", (session_id, link_id, idx)
            ).fetchone()
            return dict(row) if row else None

    def reserve_bytes(self, session_id, length, size, now):
        """Reserva `length` bytes del presupuesto (2 x size del archivo): ok, session o budget."""
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
            total = row["sent_total"] + credited
            counted_now = False
            counted, expires = row["counted"], row["expires_at"]
            if not counted and (covers(ranges, size) or total >= size):
                counted, expires = 1, min(expires, now + grace)
                cur = db.execute(
                    "UPDATE link_files SET downloads_completed=downloads_completed+1"
                    " WHERE link_id=? AND idx=?"
                    " AND downloads_completed<(SELECT max_downloads FROM links WHERE id=?)"
                    " AND (SELECT revoked_at FROM links WHERE id=?) IS NULL",
                    (row["link_id"], row["idx"], row["link_id"], row["link_id"]),
                )
                counted_now = cur.rowcount == 1
            db.execute(
                "UPDATE sessions SET ranges=?, budget_used=?, sent_total=?, counted=?, expires_at=? WHERE id=?",
                (json.dumps(ranges, separators=(",", ":")), used, total, counted, expires, session_id),
            )
            return counted_now

    def file_status(self, relpath, now):
        """Resume los usos de un relpath para decidir el borrado (llamar bajo locked()).

        Un uso está vigente si su enlace no está revocado ni vencido y al archivo le quedan descargas.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT f.link_id, f.idx, f.downloads_completed, l.max_downloads, l.expires_at, l.revoked_at,"
                " l.closed_at, l.keep_file FROM link_files f JOIN links l ON l.id=f.link_id WHERE f.relpath=?",
                (relpath,),
            ).fetchall()
            open_sessions = self._db.execute(
                "SELECT COUNT(*) FROM sessions s JOIN link_files f ON f.link_id=s.link_id AND f.idx=s.idx"
                " WHERE f.relpath=? AND s.expires_at>?",
                (relpath, now),
            ).fetchone()[0]
        live, last = False, None
        for row in rows:
            if row["revoked_at"] is not None:
                moment, reason = row["revoked_at"], "link_revoked"
            elif now >= row["expires_at"]:
                moment, reason = row["expires_at"], "link_expired"
            elif row["downloads_completed"] >= row["max_downloads"]:
                moment, reason = now, "link_exhausted"
            else:
                live = True
                continue
            if last is None or moment >= last[0]:
                last = (moment, row["link_id"], reason)
        return {
            "rows": len(rows),
            "refs": [(row["link_id"], row["idx"]) for row in rows],
            "all_terminal": bool(rows) and not live,
            "open_sessions": open_sessions > 0,
            "kept": any(row["keep_file"] for row in rows),
            "last": last,
        }

    def close_expired(self, now):
        """Marca enlaces expirados/agotados y purga sesiones vencidas."""
        closed = []
        with self._tx() as db:
            rows = db.execute("SELECT * FROM links WHERE closed_at IS NULL").fetchall()
            for link in self._links(rows, now):
                if link["state"] in ("expired", "exhausted"):
                    db.execute("UPDATE links SET closed_at=? WHERE id=?", (now, link["id"]))
                    closed.append((link["id"], link["state"]))
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        return closed

    def purge_closed(self, now, keep_closed_seconds):
        with self._tx() as db:
            return db.execute(
                "DELETE FROM links WHERE closed_at IS NOT NULL AND closed_at<=?",
                (now - keep_closed_seconds,),
            ).rowcount
