#!/usr/bin/env bash
# =============================================================================
# scripts/smoke_telegram.sh — Smoke local con Telegram real: gateway y bot reales,
# runner con ssh/docker simulados (no toca ningún servidor ni base de datos).
# Hace polling con el token de secrets/telegram_token hasta Ctrl-C.
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
TOKEN_FILE="${BOT_TOKEN_FILE:-$ROOT/secrets/telegram_token}"
ALLOWED="${BOT_ALLOWED_USER_IDS:?Define BOT_ALLOWED_USER_IDS (tu ID de Telegram)}"
PUB="${SMOKE_PUBLIC_PORT:-18080}"; INT="${SMOKE_INTERNAL_PORT:-18081}"
[[ -r "$TOKEN_FILE" ]] || { echo "No se puede leer $TOKEN_FILE" >&2; exit 1; }

T="$(mktemp -d)"
PIDS=()
cleanup() { kill "${PIDS[@]}" 2>/dev/null; wait 2>/dev/null; rm -rf "$T"; echo "smoke detenido"; }
trap cleanup EXIT INT TERM

mkdir -p "$T"/{servers,dumps,gw-data,bot-data}; chmod 700 "$T"/{dumps,gw-data,bot-data}
: > "$T/key"; : > "$T/known_hosts"
printf 'SERVER_HOST=127.0.0.1\nSERVER_USER=smoke\nSERVER_KEY=%s\nDB_USER=root\nDB_PASSWORD=smoke-pw\n' "$T/key" > "$T/servers/smoke.env"
chmod 600 "$T/servers/smoke.env"
head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$T/gw_token"; chmod 600 "$T/gw_token"

GATEWAY_PUBLIC_BASE_URL="http://127.0.0.1:$PUB" GATEWAY_DUMPS_ROOT="$T/dumps" GATEWAY_DATA_DIR="$T/gw-data" \
GATEWAY_API_TOKEN_FILE="$T/gw_token" GATEWAY_PUBLIC_PORT="$PUB" GATEWAY_INTERNAL_PORT="$INT" GATEWAY_BIND_HOST=127.0.0.1 \
    bash -c "cd services/gateway && exec '$PY' -m gateway" > "$T/gateway.log" 2>&1 &
PIDS+=($!)
sleep 1

SHIMS="$ROOT/services/runner/tests/shims"
BOT_TOKEN_FILE="$TOKEN_FILE" BOT_ALLOWED_USER_IDS="$ALLOWED" BOT_GATEWAY_URL="http://127.0.0.1:$INT" \
BOT_GATEWAY_TOKEN_FILE="$T/gw_token" BOT_DATA_DIR="$T/bot-data" BOT_RUNNER_PATH="$ROOT/services/runner/runner.sh" \
DBSYNC_SERVERS_DIR="$T/servers" DBSYNC_DUMPS_DIR="$T/dumps" DBSYNC_KNOWN_HOSTS="$T/known_hosts" \
DBSYNC_SSH_BIN="$SHIMS/ssh" SHIM_LOG="$T/ssh.log" SHIM_PATH_PREFIX="$SHIMS" DBSYNC_MIN_FREE_MB=1 \
    bash -c "cd services/bot && exec '$PY' -m bot" > "$T/bot.log" 2>&1 &
PIDS+=($!)

cat <<MSG
Smoke en marcha (Ctrl-C para detener).
  Servidor simulado: smoke  -> contenedores fakemysql y fakepg, base appdb / pgapp
  Enlaces:           http://127.0.0.1:$PUB/d/<token> (solo válidos en este equipo)
  Logs:              $T/{bot,gateway}.log   Auditoría: $T/bot-data/audit.jsonl
En Telegram, abre el bot y prueba /start, /servers y /dump.
MSG
sleep infinity & wait $!
