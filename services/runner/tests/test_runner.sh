#!/usr/bin/env bash
# =============================================================================
# tests/test_runner.sh — Escenarios del runner con ssh/docker falsos:
# éxito, dump truncado, fallo a mitad, inyección, lock, password, host key.
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
setup_env

# --- discovery ---------------------------------------------------------------
run_runner servers
assert_eq "servers: ok" true "$(jqv .ok)"
assert_eq "servers: lista gate" gate "$(jqv '.servers[0].name')"
cp "$T/servers/gate.env" "$T/servers/example.env"
run_runner servers
assert_eq "servers: ignora example" 1 "$(jqv '.servers|length')"
rm "$T/servers/example.env"

run_runner containers --server gate
assert_eq "containers: ok" true "$(jqv .ok)"
assert_eq "containers: solo motores de BD (ignora redis)" 2 "$(jqv '.containers|length')"
assert_eq "containers: cuenta sin BDs de sistema" 2 "$(jqv '.containers[]|select(.name=="fakemysql").db_count')"

# un contenedor con otras credenciales no tumba el listado
FAKE_FAIL_CONTAINER=fakepg run_runner containers --server gate
assert_eq "containers con un contenedor inaccesible: sigue ok" true "$(jqv .ok)"
assert_eq "containers: el accesible conserva su conteo" 2 "$(jqv '.containers[]|select(.name=="fakemysql").db_count')"
assert_eq "containers: el inaccesible trae db_count null" null "$(jqv '.containers[]|select(.name=="fakepg").db_count')"
assert_contains "containers: el inaccesible explica el motivo" "Access denied" "$(jqv '.containers[]|select(.name=="fakepg").list_error')"
FAKE_FAIL_CONTAINER=fakepg run_runner dbs --server gate --container fakepg
assert_eq "dbs sobre contenedor inaccesible sí falla (E_SSH)" E_SSH "$(jqv .error.code)"

run_runner dbs --server gate --container fakemysql
assert_eq "dbs mysql" '["appdb","other_db"]' "$(jqv '.databases|tojson')"
run_runner dbs --server gate --container fakepg
assert_eq "dbs pg (sin postgres)" '["pgapp"]' "$(jqv '.databases|tojson')"

# --- dump OK -----------------------------------------------------------------
: > "$T/ssh.log"; : > "$T/envlog"
run_runner dump --server gate --container fakemysql --db appdb --job-id job1
assert_eq "dump mysql: ok" true "$(jqv .ok)"
assert_eq "dump mysql: exit 0" 0 "$RC"
F="$(jqv .file)"
assert_file "dump mysql: archivo final" "$F"
assert_eq "dump mysql: permisos 600" 600 "$(stat -c %a "$F")"
assert_eq "dump mysql: gzip válido" 0 "$(gzip -t "$F" 2>/dev/null; echo $?)"
assert_eq "dump mysql: sha256 coincide" "$(sha256sum "$F" | cut -d' ' -f1)" "$(jqv .sha256)"
assert_eq "dump mysql: job_id" job1 "$(jqv .job_id)"
assert_eq "dump mysql: relpath relativo a la raíz de dumps" "gate/$(basename "$F")" "$(jqv .relpath)"
assert_eq "dump mysql: verificado trailer" true "$(jqv .verified.trailer)"
assert_eq "dump mysql: sin .part residual" 0 "$(find "$T/dumps" -name '*.part' | wc -l)"
assert_eq "dump mysql: dir 700" 700 "$(stat -c %a "$T/dumps/gate")"

# El password llega por stdin y NUNCA por argv
assert_contains "password en stdin del ssh" "STDIN: $PW" "$(cat "$T/ssh.log")"
assert_not_contains "password ausente de argv" "p'a" "$(grep '^ARGV' "$T/ssh.log")"
# La variable llega íntegra al proceso (comillas, $ y backticks incluidos)
assert_contains "MYSQL_PWD heredado intacto" "MYSQL_PWD=$PW" "$(cat "$T/envlog")"
assert_contains "ssh con host key estricta" "StrictHostKeyChecking=yes" "$(grep '^ARGV' "$T/ssh.log" | head -1)"

run_runner dump --server gate --container fakepg --db pgapp
assert_eq "dump pg: ok" true "$(jqv .ok)"
assert_eq "dump pg: motor" postgresql "$(jqv .engine)"
assert_contains "PGPASSWORD heredado intacto" "PGPASSWORD=$PW" "$(cat "$T/envlog")"

# --- contrato ----------------------------------------------------------------
req="$(jq -r '."$defs".dump.required|.[]' "$CONTRACT")"
miss=""; for k in $req; do [[ "$(jqv "has(\"$k\")")" == true ]] || miss+="$k "; done
assert_eq "contrato: dump trae todos los campos requeridos" "" "$miss"

# --- fallos que NO deben reportar éxito --------------------------------------
export FAKE_MODE=fail
run_runner dump --server gate --container fakemysql --db appdb
assert_eq "mysqldump falla: ok=false" false "$(jqv .ok)"
assert_eq "mysqldump falla: código" E_DUMP_FAILED "$(jqv .error.code)"
assert_eq "mysqldump falla: exit 1" 1 "$RC"
assert_contains "mysqldump falla: stderr capturado" "Access denied" "$(jqv .error.stderr)"
assert_eq "mysqldump falla: sin .part ni dump nuevo (quedan solo los 2 buenos)" 2 "$(find "$T/dumps/gate" -type f | wc -l | tr -d ' ')"
export FAKE_MODE=truncated
run_runner dump --server gate --container fakemysql --db appdb
assert_eq "dump sin trailer: E_TRUNCATED" E_TRUNCATED "$(jqv .error.code)"
unset FAKE_MODE
n_before="$(find "$T/dumps/gate" -type f | wc -l)"
assert_eq "dump sin trailer: no deja archivo ni .part" 0 "$(find "$T/dumps/gate" -name '*.part' | wc -l)"
assert_eq "dump sin trailer: sin archivo nuevo" 2 "$n_before"

# --- inyección y nombres inválidos: no debe haber NINGUNA llamada ssh --------
: > "$T/ssh.log"
for bad in 'appdb;id' 'a$(id)' 'a`id`' "a'b" 'a b' '../x'; do
    run_runner dump --server gate --container fakemysql --db "$bad"
    assert_eq "inyección [$bad]: E_INVALID_NAME" E_INVALID_NAME "$(jqv .error.code)"
done
run_runner dump --server '../gate' --container fakemysql --db appdb
assert_eq "server traversal: E_INVALID_NAME" E_INVALID_NAME "$(jqv .error.code)"
run_runner dump --server gate --container 'x;id' --db appdb
assert_eq "container inyectado: E_INVALID_NAME" E_INVALID_NAME "$(jqv .error.code)"
assert_eq "nombres inválidos: cero llamadas ssh" 0 "$(grep -c '^ARGV' "$T/ssh.log")"

run_runner dump --server gate --container fakemysql --db no_existe
assert_eq "db fuera del descubrimiento: E_NOT_DISCOVERED" E_NOT_DISCOVERED "$(jqv .error.code)"
run_runner dump --server gate --container nope --db appdb
assert_eq "contenedor desconocido: E_NOT_DISCOVERED" E_NOT_DISCOVERED "$(jqv .error.code)"
run_runner dump --server nope --container fakemysql --db appdb
assert_eq "servidor inexistente: E_SERVER_NOT_FOUND" E_SERVER_NOT_FOUND "$(jqv .error.code)"
run_runner dump --server gate --container fakemysql
assert_eq "falta --db: E_USAGE" E_USAGE "$(jqv .error.code)"
assert_eq "falta --db: exit 2" 2 "$RC"
run_runner nada
assert_eq "comando desconocido: E_USAGE" E_USAGE "$(jqv .error.code)"

# --- lock por servidor -------------------------------------------------------
mkdir -p "$T/dumps/.locks"
( exec 8>"$T/dumps/.locks/gate.lock"; flock -n 8; sleep 3 ) &
holder=$!; sleep 0.3
run_runner dump --server gate --container fakemysql --db appdb
assert_eq "lock ocupado: E_LOCKED" E_LOCKED "$(jqv .error.code)"
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null

# --- host key ----------------------------------------------------------------
OUT="$(DBSYNC_SERVERS_DIR="$T/servers" DBSYNC_DUMPS_DIR="$T/dumps" DBSYNC_SSH_BIN="$SHIMS/ssh" \
    SHIM_LOG="$T/ssh.log" "$RUNNER" dump --server gate --container fakemysql --db appdb 2>/dev/null)"
assert_eq "sin known_hosts: E_CONFIG" E_CONFIG "$(jqv .error.code)"

# --- password nunca en el JSON de error --------------------------------------
cat > "$T/servers/leak.env" <<EOF2
SERVER_HOST=127.0.0.1
SERVER_USER=tester
SERVER_KEY=$T/key
DB_PASSWORD=SuperSecreto123
EOF2
chmod 600 "$T/servers/leak.env"
cat > "$T/leakssh" <<'EOF2'
#!/usr/bin/env bash
cat >/dev/null
echo "Access denied for password SuperSecreto123" >&2
exit 3
EOF2
chmod +x "$T/leakssh"
OUT="$(DBSYNC_SERVERS_DIR="$T/servers" DBSYNC_DUMPS_DIR="$T/dumps" DBSYNC_KNOWN_HOSTS="$T/known_hosts" \
    DBSYNC_SSH_BIN="$T/leakssh" "$RUNNER" containers --server leak 2>/dev/null)"
assert_eq "ssh falla: E_SSH" E_SSH "$(jqv .error.code)"
assert_not_contains "el password se redacta del stderr devuelto" "SuperSecreto123" "$OUT"
rm -f "$T/servers/leak.env"

# --- llave dedicada por entorno ----------------------------------------------
printf 'SERVER_HOST=127.0.0.1\nSERVER_USER=u\nSERVER_KEY=/no/existe\nDB_PASSWORD=pw\n' > "$T/servers/keyenv.env"
chmod 600 "$T/servers/keyenv.env"
run_runner containers --server keyenv
assert_eq "SERVER_KEY inexistente sin override: E_CONFIG" E_CONFIG "$(jqv .error.code)"
: > "$T/ssh.log"
DBSYNC_SSH_KEY="$T/key" run_runner containers --server keyenv
assert_eq "DBSYNC_SSH_KEY reemplaza SERVER_KEY del .env" true "$(jqv .ok)"
assert_contains "ssh usa la llave dedicada" "[-i] [$T/key]" "$(grep '^ARGV' "$T/ssh.log" | head -1)"
rm -f "$T/servers/keyenv.env"

# --- configuración inválida --------------------------------------------------
printf 'SERVER_HOST=x\nSERVER_USER=u\nSERVER_KEY=%s\nDB_PASSWORD=a\nDB_USER="ro;ot"\n' "$T/key" > "$T/servers/bad.env"
run_runner containers --server bad
assert_eq "DB_USER inyectado en .env: E_CONFIG" E_CONFIG "$(jqv .error.code)"

finish
