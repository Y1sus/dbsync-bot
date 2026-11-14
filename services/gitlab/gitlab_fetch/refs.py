# =============================================================================
# services/gitlab/gitlab_fetch/refs.py — Listado de ramas y tags (hasta 3 páginas, defecto primero)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import re
from urllib.parse import quote

from .archive import project_target
from .errors import FetchError

PER_PAGE = 100
MAX_PAGES = 3
MAX_REFS = PER_PAGE * MAX_PAGES
MAX_NAME = 200
SHA_RE = re.compile(r"[0-9a-f]{40}")


def _slim(item):
    """Conserva solo los campos que el contrato usa; descarta nombres largos y shas inválidos."""
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    commit = item.get("commit")
    sha = commit.get("id") if isinstance(commit, dict) else None
    if not isinstance(name, str) or not 0 < len(name) <= MAX_NAME:
        return None
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        return None
    return {"name": name, "default": item.get("default") is True, "protected": item.get("protected") is True, "sha": sha}


def list_refs(client, project, kind, search=None):
    refs, truncated = [], False
    for page in range(1, MAX_PAGES + 1):
        query = f"per_page={PER_PAGE}&page={page}"
        if search:
            query += f"&search={quote(search, safe='')}"
        batch, headers = client.get_json_headers(project_target(client, project, f"/{kind}", query))
        if not isinstance(batch, list):
            raise FetchError("E_GITLAB", "GitLab devolvió un listado inesperado")
        size = len(batch)
        for item in batch:
            slim = _slim(item)
            if slim is None:
                continue
            if len(refs) >= MAX_REFS:
                truncated = True
                break
            refs.append(slim)
        del batch
        nxt = headers.get("X-Next-Page")
        more = size >= PER_PAGE if nxt is None else bool(nxt.strip())
        if truncated or not more:
            break
        truncated = page == MAX_PAGES
    refs.sort(key=lambda r: (not r["default"], r["name"].lower(), r["name"]))
    return refs, truncated
