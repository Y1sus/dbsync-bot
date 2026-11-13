#!/usr/bin/env bash
# shellcheck disable=SC2034
# =============================================================================
# services/runner/lib/discovery.sh — Descubre contenedores/servicios de BD y
# sus bases (no interactivo). Llena arrays globales y no imprime nada.
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

DISC_NAMES=()
DISC_ENGINES=()
DISC_IMAGES=()
DISC_DBS=()

_is_system_db() {
    case "$1" in
        information_schema|performance_schema|mysql|sys|postgres) return 0 ;;
        *) return 1 ;;
    esac
}

# discover_containers — llena DISC_NAMES/ENGINES/IMAGES (sin conteo de BDs)
discover_containers() {
    DISC_NAMES=(); DISC_ENGINES=(); DISC_IMAGES=()
    local errf out name image lower engine
    errf="$(mktemp)"

    if [[ "$DB_ACCESS_MODE" == "docker" ]]; then
        out="$(remote_exec mysql "$(script_docker_ps)" "$errf")" || {
            local e; e="$(tail_lines "$errf")"; rm -f "$errf"
            fail E_SSH "No se pudo listar contenedores en $SERVER_ID" "$e"
        }
        while IFS='|' read -r name image; do
            [[ -n "$name" ]] || continue
            valid_container "$name" || { log "WARN contenedor omitido por nombre no válido"; continue; }
            lower="${image,,}"
            if [[ "$lower" == *mysql* || "$lower" == *mariadb* ]]; then engine="mysql"
            elif [[ "$lower" == *postgres* ]]; then engine="postgresql"
            else continue; fi
            DISC_NAMES+=("$name"); DISC_ENGINES+=("$engine"); DISC_IMAGES+=("$image")
        done <<< "$out"
    else
        out="$(remote_exec mysql "$(script_native_probe)" "$errf")" || {
            local e; e="$(tail_lines "$errf")"; rm -f "$errf"
            fail E_SSH "No se pudo inspeccionar $SERVER_ID" "$e"
        }
        while IFS= read -r engine; do
            [[ "$engine" == "mysql" || "$engine" == "postgresql" ]] || continue
            DISC_NAMES+=("native"); DISC_ENGINES+=("$engine"); DISC_IMAGES+=("$engine (nativo)")
        done <<< "$out"
    fi
    rm -f "$errf"
}

# try_list_databases <engine> <container> — llena DISC_DBS sin BDs de sistema.
# No aborta: devuelve 1 y deja el motivo en DISC_ERR (lo usa `containers`, donde
# un contenedor con otras credenciales no debe tumbar todo el listado).
DISC_ERR=""
try_list_databases() {
    local engine="$1" container="$2" errf out db user
    DISC_DBS=(); DISC_ERR=""
    user="$(effective_db_user "$engine")"
    errf="$(mktemp)"
    if ! out="$(remote_exec "$engine" "$(script_list_dbs "$engine" "$container" "$user")" "$errf")"; then
        DISC_ERR="$(redact "$(tail_lines "$errf" 5)")"
        rm -f "$errf"
        return 1
    fi
    rm -f "$errf"
    while IFS= read -r db; do
        db="${db//[$'\r\t ']/}"
        [[ -n "$db" ]] || continue
        _is_system_db "$db" && continue
        DISC_DBS+=("$db")
    done <<< "$out"
}

# list_databases <engine> <container> — igual, pero aborta con E_SSH si falla
list_databases() {
    try_list_databases "$1" "$2" || fail E_SSH "No se pudieron listar las bases de $2" "$DISC_ERR"
}

# resolve_engine <container> [engine] — deja el motor en RESOLVED_ENGINE; falla
# si el contenedor no existe o es ambiguo (ej. 'native' con ambos motores).
# Se llama directo (no en $(...)) para que fail pueda imprimir su JSON.
RESOLVED_ENGINE=""
resolve_engine() {
    local container="$1" wanted="${2:-}" i found="" count=0
    for i in "${!DISC_NAMES[@]}"; do
        [[ "${DISC_NAMES[$i]}" == "$container" ]] || continue
        if [[ -n "$wanted" && "${DISC_ENGINES[$i]}" != "$wanted" ]]; then continue; fi
        found="${DISC_ENGINES[$i]}"; count=$((count + 1))
    done
    (( count > 0 )) || fail E_NOT_DISCOVERED "El contenedor '$container' no existe en $SERVER_ID"
    (( count == 1 )) || fail E_USAGE "Contenedor ambiguo: indica --engine" "" 2
    RESOLVED_ENGINE="$found"
}
