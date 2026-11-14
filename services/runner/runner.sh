#!/usr/bin/env bash
# =============================================================================
# services/runner/runner.sh — Runner no interactivo de dbsync-bot
# Subcomandos: servers | containers | dbs | dump. Una línea JSON por stdout
# (contrato: contracts/job-result.schema.json), logs por stderr.
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

set -uo pipefail
umask 077

RUNNER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$RUNNER_DIR/../.." && pwd)"
SERVERS_DIR="${DBSYNC_SERVERS_DIR:-$REPO_DIR/servers}"
DUMPS_DIR="${DBSYNC_DUMPS_DIR:-$REPO_DIR/dumps}"
MIN_FREE_MB="${DBSYNC_MIN_FREE_MB:-1024}"

for lib in common validate config remote discovery dump; do
    # shellcheck source=/dev/null
    source "$RUNNER_DIR/lib/$lib.sh"
done
export RUNNER_COMMAND
trap cleanup EXIT

usage() {
    cat >&2 <<'USAGE'
Uso: runner.sh <comando> [opciones]
  servers
  containers --server S
  dbs        --server S --container C [--engine mysql|postgresql]
  dump       --server S --container C --db D [--engine E] [--job-id ID]
Entorno: DBSYNC_SERVERS_DIR, DBSYNC_DUMPS_DIR, DBSYNC_KNOWN_HOSTS,
         DBSYNC_MIN_FREE_MB (default 1024), DBSYNC_SSH_BIN
USAGE
}

OPT_SERVER=""; OPT_CONTAINER=""; OPT_DB=""; OPT_ENGINE=""; OPT_JOBID=""

parse_opts() {
    while (( $# )); do
        case "$1" in
            --server|--container|--db|--engine|--job-id)
                (( $# >= 2 )) || fail E_USAGE "Falta el valor de $1" "" 2
                case "$1" in
                    --server) OPT_SERVER="$2" ;;
                    --container) OPT_CONTAINER="$2" ;;
                    --db) OPT_DB="$2" ;;
                    --engine) OPT_ENGINE="$2" ;;
                    --job-id) OPT_JOBID="$2" ;;
                esac
                shift 2 ;;
            *) fail E_USAGE "Opción desconocida: $1" "" 2 ;;
        esac
    done
}

require_opt() { [[ -n "$2" ]] || fail E_USAGE "Falta $1" "" 2; }

cmd_servers() {
    local f name host mode items=()
    shopt -s nullglob
    for f in "$SERVERS_DIR"/*.env; do
        name="$(basename "${f%.env}")"
        [[ "$name" == "example" ]] && continue
        valid_server "$name" || continue
        # shellcheck source=/dev/null
        host="$( ( unset SERVER_HOST; source "$f" >/dev/null 2>&1; printf '%s' "${SERVER_HOST:-}" ) )"
        # shellcheck source=/dev/null
        mode="$( ( unset DB_ACCESS_MODE; source "$f" >/dev/null 2>&1; printf '%s' "${DB_ACCESS_MODE:-docker}" ) )"
        items+=("$(jq -cn --arg n "$name" --arg h "$host" --arg m "$mode" '{name:$n, host:$h, access_mode:$m}')")
    done
    printf '%s\n' "${items[@]:-}" | jq -cs --argjson v "$RUNNER_SCHEMA_VERSION" \
        '{schema_version:$v, ok:true, command:"servers", servers:(map(select(. != null)))}' 2>/dev/null \
        || jq -cn --argjson v "$RUNNER_SCHEMA_VERSION" '{schema_version:$v, ok:true, command:"servers", servers:[]}'
}

setup_server() {
    require_opt --server "$OPT_SERVER"
    load_server "$OPT_SERVER"
    build_ssh_args
}

cmd_containers() {
    setup_server
    discover_containers
    local i count items=()
    for i in "${!DISC_NAMES[@]}"; do
        if try_list_databases "${DISC_ENGINES[$i]}" "${DISC_NAMES[$i]}"; then
            count="${#DISC_DBS[@]}"
        else
            count="null"
            log "WARN no se pudo listar ${DISC_NAMES[$i]}: ${DISC_ERR//$'\n'/ }"
        fi
        items+=("$(jq -cn --arg n "${DISC_NAMES[$i]}" --arg e "${DISC_ENGINES[$i]}" \
            --arg im "${DISC_IMAGES[$i]}" --argjson c "$count" --arg le "${DISC_ERR:-}" \
            '{name:$n, engine:$e, image:$im, db_count:$c} + (if $le != "" then {list_error:$le} else {} end)')")
    done
    printf '%s\n' "${items[@]:-}" | jq -cs --arg s "$SERVER_ID" --argjson v "$RUNNER_SCHEMA_VERSION" \
        '{schema_version:$v, ok:true, command:"containers", server:$s, containers:(map(select(. != null)))}'
}

check_container_opts() {
    require_opt --container "$OPT_CONTAINER"
    valid_container "$OPT_CONTAINER" || fail E_INVALID_NAME "Nombre de contenedor inválido"
    if [[ -n "$OPT_ENGINE" ]]; then
        valid_engine "$OPT_ENGINE" || fail E_INVALID_NAME "Motor inválido (mysql|postgresql)"
    fi
}

cmd_dbs() {
    check_container_opts
    setup_server
    discover_containers
    resolve_engine "$OPT_CONTAINER" "$OPT_ENGINE"
    local engine="$RESOLVED_ENGINE"
    list_databases "$engine" "$OPT_CONTAINER"
    printf '%s\n' "${DISC_DBS[@]:-}" | jq -cRs --arg s "$SERVER_ID" --arg c "$OPT_CONTAINER" \
        --arg e "$engine" --argjson v "$RUNNER_SCHEMA_VERSION" \
        '{schema_version:$v, ok:true, command:"dbs", server:$s, container:$c, engine:$e,
          databases:(split("\n") | map(select(length > 0)))}'
}

cmd_dump() {
    check_container_opts
    require_opt --db "$OPT_DB"
    valid_db "$OPT_DB" || fail E_INVALID_NAME "Nombre de base inválido (solo letras, números, _ . -)"
    local job_id="${OPT_JOBID:-$(date +%Y%m%d%H%M%S)-$$}"
    valid_jobid "$job_id" || fail E_INVALID_NAME "job-id inválido"

    setup_server

    mkdir -p "$DUMPS_DIR/.locks" "$DUMPS_DIR/$SERVER_ID"
    chmod 700 "$DUMPS_DIR" "$DUMPS_DIR/$SERVER_ID" 2>/dev/null || true
    exec 9>"$DUMPS_DIR/.locks/$SERVER_ID.lock"
    flock -n 9 || fail E_LOCKED "Ya hay un dump en curso para $SERVER_ID"

    discover_containers
    resolve_engine "$OPT_CONTAINER" "$OPT_ENGINE"
    local engine="$RESOLVED_ENGINE"
    list_databases "$engine" "$OPT_CONTAINER"
    local db found=0
    for db in "${DISC_DBS[@]:-}"; do [[ "$db" == "$OPT_DB" ]] && found=1; done
    (( found )) || fail E_NOT_DISCOVERED "La base '$OPT_DB' no existe en $OPT_CONTAINER"

    local free; free="$(disk_free_mb "$DUMPS_DIR/$SERVER_ID")" || fail E_INTERNAL "No se pudo medir el espacio libre"
    (( free >= MIN_FREE_MB )) || fail E_DISK "Espacio libre insuficiente (${free} MB < ${MIN_FREE_MB} MB)"

    local filename out start end
    filename="${SERVER_ID}_${OPT_DB}_$(date +%Y%m%d_%H%M%S)_${job_id}.sql.gz"
    out="$DUMPS_DIR/$SERVER_ID/$filename"
    start="$(date +%s)"
    stream_dump "$engine" "$OPT_CONTAINER" "$OPT_DB" "$out"
    end="$(date +%s)"

    local size sha
    size="$(stat -c '%s' "$out")"
    sha="$(sha256sum "$out" | cut -d' ' -f1)"
    log "OK $filename ${size}B"
    jq -cn --argjson v "$RUNNER_SCHEMA_VERSION" --arg j "$job_id" --arg s "$SERVER_ID" \
        --arg c "$OPT_CONTAINER" --arg e "$engine" --arg d "$OPT_DB" --arg f "$out" \
        --arg fn "$filename" --arg rp "$SERVER_ID/$filename" --argjson sz "$size" --arg sha "$sha" --argjson dur "$((end - start))" \
        '{schema_version:$v, ok:true, command:"dump", job_id:$j, server:$s, container:$c,
          engine:$e, db:$d, file:$f, relpath:$rp, filename:$fn, size_bytes:$sz, sha256:$sha, duration_s:$dur,
          verified:{gzip:true, trailer:true}}'
}

main() {
    local cmd="${1:-}"
    [[ -n "$cmd" ]] || { usage; RUNNER_COMMAND=unknown fail E_USAGE "Falta el comando" "" 2; }
    shift
    case "$cmd" in
        servers|containers|dbs|dump) RUNNER_COMMAND="$cmd" ;;
        -h|--help) usage; exit 0 ;;
        *) usage; RUNNER_COMMAND=unknown fail E_USAGE "Comando desconocido: $cmd" "" 2 ;;
    esac
    parse_opts "$@"
    command -v jq >/dev/null || { echo '{"ok":false}'; exit 1; }
    "cmd_$cmd"
}

main "$@"
