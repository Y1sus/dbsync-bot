#!/usr/bin/env bash
# =============================================================================
# services/runner/lib/dump.sh — Dump en streaming al disco local, verificado
# El dump nunca toca el disco de producción: ssh -> tee -> gzip -dc -> tail.
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

# trailer_ok <engine> <archivo_tail> — marcador de fin de dump presente
trailer_ok() {
    local engine="$1" file="$2"
    if [[ "$engine" == "mysql" ]]; then
        grep -q -- '^-- Dump completed' "$file"
    else
        grep -q -- '^-- PostgreSQL database dump complete' "$file"
    fi
}

# disk_free_mb <dir>
disk_free_mb() {
    local kb
    kb="$(df -Pk "$1" 2>/dev/null | awk 'NR==2 {print $4}')"
    [[ "$kb" =~ ^[0-9]+$ ]] || return 1
    echo $(( kb / 1024 ))
}

# stream_dump <engine> <container> <db> <out_file>
# Escribe <out_file>.part, verifica gzip + trailer y lo mueve a <out_file>.
# Usa las variables STREAM_* para devolver estado al llamador.
stream_dump() {
    local engine="$1" container="$2" db="$3" out="$4"
    local user port script part errf tailf
    user="$(effective_db_user "$engine")"
    port="$(effective_db_port "$engine")"
    script="$(script_dump "$engine" "$container" "$user" "$port" "$db")"
    part="$out.part"
    errf="$(mktemp)"; tailf="$(mktemp)"
    CLEANUP_FILES+=("$part" "$errf" "$tailf")

    remote_exec "$engine" "$script" "$errf" | tee "$part" | gzip -dc | tail -n 40 > "$tailf"
    local st=("${PIPESTATUS[@]}")
    local ssh_st="${st[0]}" tee_st="${st[1]}" gz_st="${st[2]}"
    local err; err="$(tail_lines "$errf" 20)"

    if (( ssh_st == 255 )); then
        fail E_SSH "Falló la conexión SSH con $SERVER_ID" "$err"
    elif (( ssh_st != 0 )); then
        fail E_DUMP_FAILED "El dump remoto terminó con código $ssh_st" "$err"
    elif (( tee_st != 0 )); then
        fail E_DISK "No se pudo escribir el dump en disco local" "$err"
    elif (( gz_st != 0 )); then
        fail E_TRUNCATED "El archivo gzip está truncado o corrupto" "$err"
    fi
    [[ -s "$part" ]] || fail E_DUMP_FAILED "El dump salió vacío" "$err"
    trailer_ok "$engine" "$tailf" || fail E_TRUNCATED "El dump no termina con el marcador de fin esperado" "$err"

    chmod 600 "$part"
    mv -f "$part" "$out"
}
