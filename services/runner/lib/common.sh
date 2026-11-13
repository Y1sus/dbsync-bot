#!/usr/bin/env bash
# =============================================================================
# services/runner/lib/common.sh — Logging a stderr y salida JSON del runner
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

RUNNER_SCHEMA_VERSION=1
RUNNER_COMMAND="${RUNNER_COMMAND:-unknown}"

log() { printf '%s [runner] %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

# redact <texto> — quita el password del texto antes de devolverlo en JSON
redact() {
    local text="$1"
    if [[ -n "${DB_PASSWORD:-}" && ${#DB_PASSWORD} -ge 4 ]]; then
        text="${text//"$DB_PASSWORD"/***}"
    fi
    printf '%s' "$text"
}

# fail <codigo> <mensaje> [stderr] [exit=1] — imprime el JSON de error y sale
fail() {
    local code="$1" message="$2" err="${3:-}" status="${4:-1}"
    err="$(redact "$err")"
    (( ${#err} > 2000 )) && err="${err: -2000}"
    jq -cn --arg c "$code" --arg m "$message" --arg s "$err" \
        --arg cmd "$RUNNER_COMMAND" --argjson v "$RUNNER_SCHEMA_VERSION" \
        '{schema_version:$v, ok:false, command:$cmd, error:{code:$c, message:$m, stderr:$s}}'
    log "ERROR $code: $message"
    exit "$status"
}

# tail_lines <archivo> [n] — últimas n líneas de un archivo (vacío si no existe)
tail_lines() {
    local file="$1" n="${2:-20}"
    [[ -f "$file" ]] && tail -n "$n" "$file" || true
}

# Archivos temporales a borrar al salir (el runner instala el trap EXIT)
CLEANUP_FILES=()
cleanup() {
    local f
    for f in "${CLEANUP_FILES[@]:-}"; do
        [[ -n "$f" ]] && rm -f -- "$f"
    done
}
