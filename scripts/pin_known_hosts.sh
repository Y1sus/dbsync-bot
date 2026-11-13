#!/usr/bin/env bash
# =============================================================================
# scripts/pin_known_hosts.sh — Obtiene las llaves de host de los servidores con
# ssh-keyscan para fijar secrets/known_hosts. Solo abre el handshake SSH (no inicia sesión).
# Uso: scripts/pin_known_hosts.sh <servidor>...            muestra las huellas
#      scripts/pin_known_hosts.sh --write <servidor>...    las agrega a secrets/known_hosts
# Compara las huellas con otro canal (consola del proveedor) antes de usar --write:
# ssh-keyscan solo confía en la red de este momento.
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
write=0
[[ "${1:-}" == "--write" ]] && { write=1; shift; }
(( $# )) || { echo "Uso: $0 [--write] <servidor>..." >&2; exit 2; }
SERVERS_DIR="${DBSYNC_SERVERS_DIR:-./servers}"
rc=0
for name in "$@"; do
    [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ && -f "$SERVERS_DIR/$name.env" ]] \
        || { echo "Servidor inválido o inexistente: $name" >&2; rc=1; continue; }
    # shellcheck source=/dev/null
    host="$( ( unset SERVER_HOST; source "$SERVERS_DIR/$name.env" >/dev/null 2>&1; printf '%s' "${SERVER_HOST:-}" ) )"
    # shellcheck source=/dev/null
    port="$( ( unset SERVER_PORT; source "$SERVERS_DIR/$name.env" >/dev/null 2>&1; printf '%s' "${SERVER_PORT:-22}" ) )"
    keys="$(ssh-keyscan -T 8 -p "$port" -t ed25519,ecdsa,rsa "$host" 2>/dev/null)"
    [[ -n "$keys" ]] || { echo "[$name] sin respuesta de $host:$port" >&2; rc=1; continue; }
    echo "== $name ($host:$port)"
    printf '%s\n' "$keys" | ssh-keygen -lf - 2>/dev/null
    if (( write )); then
        printf '%s\n' "$keys" >> secrets/known_hosts
        echo "   agregado a secrets/known_hosts"
    fi
done
(( write )) && sort -u secrets/known_hosts -o secrets/known_hosts && chmod 600 secrets/known_hosts
exit $rc
