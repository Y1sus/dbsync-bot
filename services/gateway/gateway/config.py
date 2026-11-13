# =============================================================================
# services/gateway/gateway/config.py — Configuración del gateway desde variables de entorno
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import os
from dataclasses import dataclass


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    dumps_root: str
    data_dir: str
    public_base_url: str
    api_token_file: str
    public_port: int = 8080
    internal_port: int = 8081
    bind_host: str = "0.0.0.0"
    retention_hours: float = 24.0
    max_dumps_gb: float = 10.0
    cleanup_interval: float = 60.0
    session_ttl: int = 7200
    block_size: int = 256 * 1024
    closed_rows_days: int = 7


def _number(env, name, default, kind, lo, hi):
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = kind(raw)
    except ValueError:
        raise ConfigError(f"{name} debe ser numérico (recibido: {raw!r})") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{name} debe estar entre {lo} y {hi}")
    return value


def _required(env, name):
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"Falta la variable obligatoria {name}")
    return value


def from_env(env=None):
    env = os.environ if env is None else env
    base = _required(env, "GATEWAY_PUBLIC_BASE_URL").rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ConfigError("GATEWAY_PUBLIC_BASE_URL debe empezar con http:// o https://")
    return Config(
        dumps_root=env.get("GATEWAY_DUMPS_ROOT", "").strip() or "/dumps",
        data_dir=env.get("GATEWAY_DATA_DIR", "").strip() or "/data",
        public_base_url=base,
        api_token_file=_required(env, "GATEWAY_API_TOKEN_FILE"),
        public_port=_number(env, "GATEWAY_PUBLIC_PORT", 8080, int, 1, 65535),
        internal_port=_number(env, "GATEWAY_INTERNAL_PORT", 8081, int, 1, 65535),
        bind_host=env.get("GATEWAY_BIND_HOST", "").strip() or "0.0.0.0",
        retention_hours=_number(env, "GATEWAY_RETENTION_HOURS", 24.0, float, 0.01, 8760),
        max_dumps_gb=_number(env, "GATEWAY_MAX_DUMPS_GB", 10.0, float, 0.001, 100000),
        cleanup_interval=_number(env, "GATEWAY_CLEANUP_INTERVAL_SECONDS", 60.0, float, 1, 86400),
    )


def load_api_token(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as exc:
        raise ConfigError(f"No se pudo leer GATEWAY_API_TOKEN_FILE: {exc.strerror}") from None
    if len(token) < 16:
        raise ConfigError("El token del gateway debe tener al menos 16 caracteres")
    return token
