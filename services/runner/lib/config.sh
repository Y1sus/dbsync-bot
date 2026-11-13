#!/usr/bin/env bash
# =============================================================================
# services/runner/lib/config.sh — Carga y validación de servers/<nombre>.env
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

# shellcheck disable=SC2034  # las variables las consumen remote.sh, discovery.sh y dump.sh
# load_server <nombre> — carga el .env del servidor y deja las variables
# SERVER_ID, SERVER_HOST, SERVER_USER, SERVER_KEY, SERVER_PORT,
# DB_ACCESS_MODE, DB_USER, DB_PASSWORD y DB_PORT_OVERRIDE listas.
load_server() {
    local name="$1"
    valid_server "$name" || fail E_INVALID_NAME "Nombre de servidor inválido: $name"
    [[ "$name" != "example" ]] || fail E_SERVER_NOT_FOUND "El servidor 'example' es solo plantilla"

    local file="$SERVERS_DIR/$name.env"
    [[ -f "$file" ]] || fail E_SERVER_NOT_FOUND "No existe el servidor: $name"

    local perms
    perms="$(stat -c '%a' "$file" 2>/dev/null || echo 600)"
    if [[ "${perms: -2}" != "00" ]]; then
        log "WARN $file tiene permisos $perms; se recomienda chmod 600"
    fi

    unset SERVER_HOST SERVER_USER SERVER_KEY SERVER_PORT DB_ACCESS_MODE DB_USER DB_PASSWORD DB_PORT
    # shellcheck source=/dev/null
    source "$file"

    SERVER_ID="$name"
    SERVER_PORT="${SERVER_PORT:-22}"
    DB_ACCESS_MODE="${DB_ACCESS_MODE:-docker}"
    DB_USER="${DB_USER:-root}"
    DB_PORT_OVERRIDE="${DB_PORT:-}"

    local var
    for var in SERVER_HOST SERVER_USER SERVER_KEY DB_PASSWORD; do
        [[ -n "${!var:-}" ]] || fail E_CONFIG "Falta la variable $var en servers/$name.env"
    done

    # shellcheck disable=SC2153
    valid_host "$SERVER_HOST"     || fail E_CONFIG "SERVER_HOST inválido en servers/$name.env"
    valid_sshuser "$SERVER_USER"  || fail E_CONFIG "SERVER_USER inválido en servers/$name.env"
    valid_port "$SERVER_PORT"     || fail E_CONFIG "SERVER_PORT inválido en servers/$name.env"
    valid_mode "$DB_ACCESS_MODE"  || fail E_CONFIG "DB_ACCESS_MODE inválido en servers/$name.env"
    valid_dbuser "$DB_USER"       || fail E_CONFIG "DB_USER inválido en servers/$name.env"
    valid_password "$DB_PASSWORD" || fail E_CONFIG "DB_PASSWORD vacío, muy largo o con salto de línea en servers/$name.env"
    if [[ -n "$DB_PORT_OVERRIDE" ]]; then
        valid_port "$DB_PORT_OVERRIDE" || fail E_CONFIG "DB_PORT inválido en servers/$name.env"
    fi

    SERVER_KEY="${SERVER_KEY/#\~/$HOME}"
    [[ -r "$SERVER_KEY" ]] || fail E_CONFIG "No se puede leer SERVER_KEY: $SERVER_KEY"
}

# effective_db_user <engine> — en PostgreSQL 'root' se traduce a 'postgres'
effective_db_user() {
    if [[ "$1" == "postgresql" && "$DB_USER" == "root" ]]; then
        printf 'postgres'
    else
        printf '%s' "$DB_USER"
    fi
}

# effective_db_port <engine>
effective_db_port() {
    if [[ -n "$DB_PORT_OVERRIDE" ]]; then
        printf '%s' "$DB_PORT_OVERRIDE"
    elif [[ "$1" == "mysql" ]]; then
        printf '3306'
    else
        printf '5432'
    fi
}
