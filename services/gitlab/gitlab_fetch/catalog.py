# =============================================================================
# services/gitlab/gitlab_fetch/catalog.py — Catálogo cerrado de proyectos (GITLAB_PROJECTS_FILE): carga y validación
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import json
import re

from .errors import FetchError

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
PATH_RE = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+")
SERVER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
DB_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _bad(msg):
    raise FetchError("E_CONFIG", f"Catálogo inválido: {msg}")


def _check_db(name, db):
    if not isinstance(db, dict):
        _bad(f"'db' de {name!r} debe ser un objeto")
    for key, rx in (("server", SERVER_RE), ("container", DB_NAME_RE), ("db", DB_NAME_RE)):
        val = db.get(key)
        if not isinstance(val, str) or not rx.fullmatch(val):
            _bad(f"'db.{key}' de {name!r} inválido")
    return {k: db[k] for k in ("server", "container", "db")}


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise FetchError("E_CONFIG", "No existe el catálogo de proyectos (GITLAB_PROJECTS_FILE)")
    except (OSError, ValueError):
        raise FetchError("E_CONFIG", "El catálogo de proyectos no se pudo leer o no es JSON")
    items = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(items, list):
        _bad("falta la lista 'projects'")
    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            _bad("cada proyecto debe ser un objeto")
        name, gpath = item.get("name"), item.get("path")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            _bad("nombre de proyecto inválido")
        if not isinstance(gpath, str) or not PATH_RE.fullmatch(gpath) or ".." in gpath.split("/"):
            _bad(f"path de {name!r} inválido")
        if name in seen:
            _bad(f"nombre duplicado {name!r}")
        seen.add(name)
        entry = {"name": name, "path": gpath}
        if item.get("db") is not None:
            entry["db"] = _check_db(name, item["db"])
        out.append(entry)
    return out


def find(projects, name):
    for p in projects:
        if p["name"] == name:
            return p
    raise FetchError("E_NOT_ALLOWED", "Proyecto fuera del catálogo")


def public_view(projects):
    out = []
    for p in projects:
        row = {"name": p["name"], "path": p["path"], "has_db": "db" in p}
        if "db" in p:
            row["db"] = dict(p["db"])
        out.append(row)
    return out
