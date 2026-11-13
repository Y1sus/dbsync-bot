#!/usr/bin/env bash
# =============================================================================
# services/runner/lib/remote.sh — Ejecución remota por SSH con el password por
# stdin (nunca en argv), host key fijada y scripts sin comillas simples.
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

SSH_BIN="${DBSYNC_SSH_BIN:-ssh}"
SSH_ARGS=()

# build_ssh_args — requiere known_hosts fijado salvo DBSYNC_INSECURE_HOSTKEY=1
build_ssh_args() {
    SSH_ARGS=(
        -i "$SERVER_KEY" -p "$SERVER_PORT"
        -o BatchMode=yes -o IdentitiesOnly=yes
        -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4
    )
    if [[ "${DBSYNC_INSECURE_HOSTKEY:-0}" == "1" ]]; then
        log "WARN host key sin verificar (DBSYNC_INSECURE_HOSTKEY=1)"
        SSH_ARGS+=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)
        return 0
    fi
    local kh="${DBSYNC_KNOWN_HOSTS:-}"
    [[ -n "$kh" && -r "$kh" ]] || fail E_CONFIG \
        "Define DBSYNC_KNOWN_HOSTS con un known_hosts legible (o DBSYNC_INSECURE_HOSTKEY=1 solo para pruebas)"
    SSH_ARGS+=(-o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$kh")
}

# wrap_script <engine> <script> — envuelve en bash con pipefail (y sudo si aplica).
# El script no debe contener comillas simples: se valida aquí.
wrap_script() {
    local engine="$1" script="$2"
    if [[ "$script" == *"'"* ]]; then
        log "ERROR el script remoto contiene comillas simples; se aborta"
        return 1
    fi
    case "$DB_ACCESS_MODE" in
        native+sudo)
            if [[ "$engine" == "postgresql" ]]; then
                printf "sudo -n -u postgres bash -o pipefail -c '%s'" "$script"
            else
                printf "sudo -n bash -o pipefail -c '%s'" "$script"
            fi
            ;;
        *)
            printf "bash -o pipefail -c '%s'" "$script"
            ;;
    esac
}

# remote_exec <engine> <script> <archivo_stderr>
# El password se envía como primera línea de stdin; el script la lee con
# `IFS= read -r DBPW`. stdout del comando remoto pasa a stdout de la función.
remote_exec() {
    local engine="$1" script="$2" errfile="$3" wrapped
    wrapped="$(wrap_script "$engine" "$script")" || { echo "script remoto inválido" > "$errfile"; return 1; }
    printf '%s\n' "$DB_PASSWORD" \
        | "$SSH_BIN" "${SSH_ARGS[@]}" "$SERVER_USER@$SERVER_HOST" "$wrapped" 2>"$errfile"
}

# _pw_prelude <engine> — lee el password de stdin y lo exporta como variable
# de entorno del motor (docker exec -e VAR la hereda sin exponer el valor).
_pw_prelude() {
    if [[ "$1" == "mysql" ]]; then
        printf 'IFS= read -r DBPW; MYSQL_PWD="$DBPW"; export MYSQL_PWD; unset DBPW; '
    else
        printf 'IFS= read -r DBPW; PGPASSWORD="$DBPW"; export PGPASSWORD; unset DBPW; '
    fi
}

# _exec_prefix <engine> <container> — prefijo según el modo de acceso
_exec_prefix() {
    local engine="$1" container="$2" var
    [[ "$engine" == "mysql" ]] && var="MYSQL_PWD" || var="PGPASSWORD"
    if [[ "$DB_ACCESS_MODE" == "docker" ]]; then
        printf 'docker exec -e %s %s ' "$var" "$container"
    fi
}

script_docker_ps() {
    printf 'IFS= read -r DBPW; unset DBPW; docker ps --format "{{.Names}}|{{.Image}}"'
}

script_native_probe() {
    printf 'IFS= read -r DBPW; unset DBPW; command -v mysql >/dev/null 2>&1 && echo mysql; command -v psql >/dev/null 2>&1 && echo postgresql; true'
}

# script_list_dbs <engine> <container> <user>
script_list_dbs() {
    local engine="$1" container="$2" user="$3" prefix
    prefix="$(_exec_prefix "$engine" "$container")"
    _pw_prelude "$engine"
    if [[ "$engine" == "mysql" ]]; then
        printf '%smysql -u %s -N -e "SHOW DATABASES"' "$prefix" "$user"
    else
        printf '%spsql -U %s -t -A -c "SELECT datname FROM pg_database WHERE datistemplate = false"' "$prefix" "$user"
    fi
}

# script_dump <engine> <container> <user> <port> <db>
script_dump() {
    local engine="$1" container="$2" user="$3" port="$4" db="$5" prefix
    prefix="$(_exec_prefix "$engine" "$container")"
    _pw_prelude "$engine"
    if [[ "$engine" == "mysql" ]]; then
        printf '%smysqldump -h 127.0.0.1 -P %s -u %s --single-transaction --quick --lock-tables=false --routines --triggers %s | gzip -c' \
            "$prefix" "$port" "$user" "$db"
    else
        printf '%spg_dump -h 127.0.0.1 -p %s -U %s --no-password --format=plain --clean --if-exists %s | gzip -c' \
            "$prefix" "$port" "$user" "$db"
    fi
}
