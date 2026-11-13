# =============================================================================
# services/bot/bot/config.py — Configuración del bot desde variables de entorno
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from .durations import MAX_SECONDS, MIN_SECONDS


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    token_file: str
    allowed_user_ids: frozenset
    runner_path: str
    gateway_url: str
    gateway_token_file: str
    data_dir: str
    default_ttl: int
    default_max_downloads: int
    job_timeout: int
    max_jobs: int
    telegram_api_base: str | None


def _int(env, name, default, lo, hi):
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} debe ser un entero.") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{name} debe estar entre {lo} y {hi}.")
    return value


def _required(env, name):
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"Falta la variable {name}.")
    return value


def parse_user_ids(raw: str) -> frozenset:
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not (part.isdigit() and part.isascii()) or int(part) <= 0:
            raise ConfigError("BOT_ALLOWED_USER_IDS debe ser una lista de ids numéricos separados por coma.")
        ids.add(int(part))
    if not ids:
        raise ConfigError("BOT_ALLOWED_USER_IDS no tiene ningún id.")
    return frozenset(ids)


def load_config(env=None) -> Config:
    env = os.environ if env is None else env
    gateway_url = _required(env, "BOT_GATEWAY_URL").rstrip("/")
    parsed = urlparse(gateway_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError("BOT_GATEWAY_URL debe ser una URL http(s) válida.")
    api_base = env.get("BOT_TELEGRAM_API_BASE", "").strip() or None
    return Config(
        token_file=_required(env, "BOT_TOKEN_FILE"),
        allowed_user_ids=parse_user_ids(_required(env, "BOT_ALLOWED_USER_IDS")),
        runner_path=env.get("BOT_RUNNER_PATH", "").strip() or "/app/runner/runner.sh",
        gateway_url=gateway_url,
        gateway_token_file=_required(env, "BOT_GATEWAY_TOKEN_FILE"),
        data_dir=env.get("BOT_DATA_DIR", "").strip() or "/data",
        default_ttl=_int(env, "BOT_DEFAULT_TTL_SECONDS", 3600, MIN_SECONDS, MAX_SECONDS),
        default_max_downloads=_int(env, "BOT_DEFAULT_MAX_DOWNLOADS", 1, 1, 20),
        job_timeout=_int(env, "BOT_JOB_TIMEOUT_SECONDS", 1800, 10, 86400),
        max_jobs=_int(env, "BOT_MAX_JOBS", 1, 1, 8),
        telegram_api_base=api_base,
    )


def read_secret(path: str, label: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError as exc:
        raise ConfigError(f"No se pudo leer el archivo de {label}: {exc.strerror}.") from None
    if not value:
        raise ConfigError(f"El archivo de {label} está vacío.")
    return value
