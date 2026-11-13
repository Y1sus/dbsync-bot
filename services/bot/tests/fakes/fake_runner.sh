#!/usr/bin/env bash
# =============================================================================
# services/bot/tests/fakes/fake_runner.sh — Runner falso que imita el contrato, guiado por FAKE_MODE
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
# FAKE_MODE: ok | err | usage | empty | garbage | hang | badjson | slow
# FAKE_MODE_FILE: si existe, su contenido reemplaza a FAKE_MODE (permite cambiar el modo en caliente)
# FAKE_ARGS_FILE: si existe, registra los argumentos recibidos
# FAKE_PID_FILE: en modo hang, escribe aquí el pid del hijo (como ssh)
set -u
[[ -n "${FAKE_ARGS_FILE:-}" ]] && printf '%s\n' "$*" >> "$FAKE_ARGS_FILE"
cmd="${1:-}"; shift || true
declare -A a=()
while [[ $# -gt 1 ]]; do a["${1#--}"]="$2"; shift 2; done
SHA=$(printf 'a%.0s' {1..64})
mode="${FAKE_MODE:-ok}"
[[ -n "${FAKE_MODE_FILE:-}" && -f "$FAKE_MODE_FILE" ]] && mode="$(<"$FAKE_MODE_FILE")"
case "$mode" in
  empty) exit 1 ;;
  garbage) echo "esto no es json"; exit 0 ;;
  badjson) echo '{"schema_version":1,"ok":true,"command":"dump","job_id":"x"}'; exit 0 ;;
  usage) echo '{"schema_version":1,"ok":false,"command":"dump","error":{"code":"E_USAGE","message":"uso","stderr":""}}'; exit 2 ;;
  err)
    echo "ssh: connect to host x port 22: Connection refused" >&2
    echo '{"schema_version":1,"ok":false,"command":"'"$cmd"'","error":{"code":"E_SSH","message":"ssh falló","stderr":"ssh: connect to host x port 22: Connection refused"}}'
    exit 1 ;;
  hang)
    sleep 300 &
    [[ -n "${FAKE_PID_FILE:-}" ]] && echo $! > "$FAKE_PID_FILE"
    wait ;;
  slow) sleep "${FAKE_SLEEP:-0.3}" ;;
esac
case "$cmd" in
  servers) echo '{"schema_version":1,"ok":true,"command":"servers","servers":[{"name":"alpha","host":"10.0.0.1","access_mode":"docker"},{"name":"beta","host":"10.0.0.2","access_mode":"native"}]}' ;;
  containers) echo '{"schema_version":1,"ok":true,"command":"containers","server":"'"${a[server]:-}"'","containers":[{"name":"mysql8","engine":"mysql","image":"mysql:8","db_count":2},{"name":"pg","engine":"postgresql","image":"postgres:16","db_count":null,"list_error":"x"}]}' ;;
  dbs) echo '{"schema_version":1,"ok":true,"command":"dbs","server":"'"${a[server]:-}"'","container":"'"${a[container]:-}"'","engine":"mysql","databases":["appdb","<b>evil&db</b>"]}' ;;
  dump) echo '{"schema_version":1,"ok":true,"command":"dump","job_id":"'"${a[job-id]:-}"'","server":"'"${a[server]:-}"'","container":"'"${a[container]:-}"'","engine":"mysql","db":"'"${a[db]:-}"'","file":"/dumps/'"${a[server]:-}"'/f.sql.gz","relpath":"'"${a[server]:-}"'/f.sql.gz","filename":"f.sql.gz","size_bytes":1234567,"sha256":"'"$SHA"'","duration_s":3,"verified":{"gzip":true,"trailer":true}}' ;;
esac
