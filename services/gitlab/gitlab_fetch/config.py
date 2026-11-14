# =============================================================================
# services/gitlab/gitlab_fetch/config.py — Configuración desde el entorno (URL, token, topes, rutas)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .errors import FetchError

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Config:
    base_url: str
    token_file: str
    projects_file: str
    dumps_dir: str
    max_archive_mb: int
    timeout_s: float
    read_timeout_s: float
    min_free_mb: int
    verify_max_entries: int = 100_000
    verify_ratio: int = 8
    verify_min_bytes: int = 16 * 1024 * 1024

    def read_token(self):
        if not self.token_file:
            raise FetchError("E_CONFIG", "Falta GITLAB_TOKEN_FILE")
        try:
            token = Path(self.token_file).read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            raise FetchError("E_CONFIG", "No se pudo leer GITLAB_TOKEN_FILE")
        if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise FetchError("E_CONFIG", "El token de GitLab está vacío o tiene caracteres inválidos")
        return token


def _num(env, key, default, cast=int, minimum=1):
    raw = env.get(key, "")
    if raw == "":
        return default
    try:
        val = cast(raw)
    except ValueError:
        raise FetchError("E_CONFIG", f"{key} debe ser numérico")
    if val < minimum:
        raise FetchError("E_CONFIG", f"{key} fuera de rango")
    return val


def _base_url(env):
    raw = (env.get("GITLAB_BASE_URL") or "https://gitlab.com").strip()
    try:
        parts = urlsplit(raw)
        parts.port
    except ValueError:
        raise FetchError("E_CONFIG", "GITLAB_BASE_URL inválida")
    allow_http = env.get("GITLAB_ALLOW_HTTP") == "1"
    if parts.scheme != "https" and not (parts.scheme == "http" and allow_http):
        raise FetchError("E_CONFIG", "GITLAB_BASE_URL debe ser https://")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise FetchError("E_CONFIG", "GITLAB_BASE_URL inválida")
    return raw.rstrip("/")


def from_env(env=None):
    env = os.environ if env is None else env
    return Config(
        base_url=_base_url(env),
        token_file=env.get("GITLAB_TOKEN_FILE", ""),
        projects_file=env.get("GITLAB_PROJECTS_FILE") or str(REPO_ROOT / "deploy" / "projects.json"),
        dumps_dir=env.get("DBSYNC_DUMPS_DIR") or str(REPO_ROOT / "dumps"),
        max_archive_mb=_num(env, "GITLAB_MAX_ARCHIVE_MB", 500),
        timeout_s=_num(env, "GITLAB_TIMEOUT_SECONDS", 300.0, float, 0.001),
        read_timeout_s=_num(env, "GITLAB_READ_TIMEOUT_SECONDS", 30.0, float, 0.001),
        min_free_mb=_num(env, "DBSYNC_MIN_FREE_MB", 1024, int, 0),
        verify_max_entries=_num(env, "GITLAB_VERIFY_MAX_ENTRIES", 100_000),
        verify_ratio=_num(env, "GITLAB_VERIFY_RATIO", 8),
        verify_min_bytes=_num(env, "GITLAB_VERIFY_MIN_BYTES", 16 * 1024 * 1024),
    )
