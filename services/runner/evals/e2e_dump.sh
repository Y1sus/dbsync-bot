#!/usr/bin/env bash
# shellcheck disable=SC2034
# =============================================================================
# evals/e2e_dump.sh — Eval periódico con Docker real: levanta MySQL 8.0 y
# PostgreSQL 17/18 desechables, corre el runner (ssh simulado en local) y
# verifica trailer real, herencia de `docker exec -e VAR`, y restauración.
# No es prueba de puerta: tarda ~1 min. Umbral: 0 fallos.
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
source "$(dirname "${BASH_SOURCE[0]}")/../tests/lib.sh"
PW="${PW}"                      # password con comillas, $ y backticks
TAG="dbsyncbot-e2e-$$"
CONTAINERS=()
cleanup_all() { for c in "${CONTAINERS[@]}"; do docker rm -f "$c" >/dev/null 2>&1; done; rm -rf "$T"; }
setup_env; trap cleanup_all EXIT
MYSQL_IMAGE="${E2E_MYSQL_IMAGE:-mysql:8.0}"
PG_IMAGES="${E2E_PG_IMAGES:-postgres:17 postgres:18}"

start() { # nombre imagen args...
    local name="$1" image="$2"; shift 2
    docker run -d --name "$name" "$@" "$image" >/dev/null || { nok "arranca $name"; return 1; }
    CONTAINERS+=("$name")
}
wait_for() { # intentos cmd...
    local n="$1"; shift
    for _ in $(seq "$n"); do "$@" >/dev/null 2>&1 && return 0; sleep 2; done; return 1
}

run_dump() { # container db
    OUT="$(DBSYNC_SERVERS_DIR="$T/servers" DBSYNC_DUMPS_DIR="$T/dumps" DBSYNC_KNOWN_HOSTS="$T/known_hosts" \
        DBSYNC_SSH_BIN="$SHIMS/ssh" SHIM_LOG="$T/ssh.log" DBSYNC_MIN_FREE_MB=1 \
        "$RUNNER" dump --server gate --container "$1" --db "$2" 2>"$T/stderr")"
    RC=$?
}

# --- MySQL -------------------------------------------------------------------
echo "== MySQL ($MYSQL_IMAGE)"
MY="$TAG-my"
# El entrypoint de la imagen rompe con ' en MYSQL_ROOT_PASSWORD: se arranca con
# un password simple y se cambia por el especial con ALTER USER.
start "$MY" "$MYSQL_IMAGE" -e "MYSQL_ROOT_PASSWORD=boot" || exit 1
wait_for 60 docker exec -e MYSQL_PWD=boot "$MY" mysqladmin -h127.0.0.1 -uroot ping || nok "mysql listo"
ESC="${PW//\'/\\\'}"
docker exec -i -e MYSQL_PWD=boot "$MY" mysql -h127.0.0.1 -uroot -e \
    "ALTER USER 'root'@'%' IDENTIFIED BY '$ESC'; ALTER USER 'root'@'localhost' IDENTIFIED BY '$ESC';" \
    || nok "mysql: cambia el password a uno con comillas"
docker exec -i -e MYSQL_PWD="$PW" "$MY" mysql -h127.0.0.1 -uroot <<'SQL'
CREATE DATABASE appdb CHARACTER SET utf8mb4;
CREATE DATABASE restoredb CHARACTER SET utf8mb4;
USE appdb;
CREATE TABLE t (id INT PRIMARY KEY AUTO_INCREMENT, v VARCHAR(100));
INSERT INTO t (v) SELECT CONCAT('fila ñandú "comillas" ''simples'' ', seq) FROM
  (SELECT a.n + b.n*10 + c.n*100 AS seq FROM (SELECT 0 n UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) a,
  (SELECT 0 n UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) b,
  (SELECT 0 n UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) c) s;
CREATE VIEW v AS SELECT id FROM t;
SQL
sed -i "/^DB_USER=/d;/^DB_PASSWORD=/d" "$T/servers/gate.env"
{ echo "DB_USER=root"; printf 'DB_PASSWORD=%q\n' "$PW"; } >> "$T/servers/gate.env"

run_runner containers --server gate 2>/dev/null
OUT="$(DBSYNC_SERVERS_DIR="$T/servers" DBSYNC_DUMPS_DIR="$T/dumps" DBSYNC_KNOWN_HOSTS="$T/known_hosts" \
    DBSYNC_SSH_BIN="$SHIMS/ssh" SHIM_LOG="$T/ssh.log" "$RUNNER" dbs --server gate --container "$MY" 2>/dev/null)"
assert_eq "mysql: lista appdb y restoredb (sin sistema)" '["appdb","restoredb"]' "$(jqv '.databases|tojson')"

run_dump "$MY" appdb
assert_eq "mysql: dump ok (password con comillas, \$ y backticks)" true "$(jqv .ok)"
F="$(jqv .file)"
assert_eq "mysql: trailer real verificado" true "$(jqv .verified.trailer)"
assert_contains "mysql: el archivo termina con -- Dump completed" "-- Dump completed" "$(zcat "$F" | tail -n 3)"
assert_not_contains "mysql: password fuera del argv del ssh" "p'a" "$(grep '^ARGV' "$T/ssh.log")"
zcat "$F" | docker exec -i -e MYSQL_PWD="$PW" "$MY" mysql -h127.0.0.1 -uroot restoredb
assert_eq "mysql: restaura con las mismas filas" 1000 \
    "$(docker exec -e MYSQL_PWD="$PW" "$MY" mysql -h127.0.0.1 -uroot -N -e 'SELECT COUNT(*) FROM restoredb.t')"

# Usuario sin privilegios suficientes: el fallo debe propagarse (pipefail real)
docker exec -e MYSQL_PWD="$PW" "$MY" mysql -h127.0.0.1 -uroot -e "CREATE USER 'lim'@'%' IDENTIFIED BY 'x'; GRANT SELECT ON appdb.* TO 'lim'@'%';"
sed -i "/^DB_USER=/d;/^DB_PASSWORD=/d" "$T/servers/gate.env"
{ echo "DB_USER=lim"; echo "DB_PASSWORD=x"; } >> "$T/servers/gate.env"
before="$(find "$T/dumps" -type f | wc -l)"
run_dump "$MY" appdb
# Sin SHOW VIEW, mysqldump aborta; antes del fix el exit de gzip lo ocultaba
assert_eq "mysql: usuario sin SHOW VIEW falla con E_DUMP_FAILED (pipefail real)" E_DUMP_FAILED "$(jqv .error.code)"
assert_eq "mysql: el fallo no deja dump parcial" "$before" "$(find "$T/dumps" -type f | wc -l)"
assert_contains "mysql: el error real de mysqldump llega en el JSON" "SHOW VIEW" "$(jqv .error.stderr)" 

# --- PostgreSQL --------------------------------------------------------------
for img in $PG_IMAGES; do
    echo "== PostgreSQL ($img)"
    PG="$TAG-pg-${img//[:.]/-}"
    start "$PG" "$img" -e "POSTGRES_PASSWORD=$PW" || continue
    wait_for 60 docker exec "$PG" pg_isready -h 127.0.0.1 || nok "pg listo"
    sleep 2
    docker exec -e PGPASSWORD="$PW" "$PG" psql -h 127.0.0.1 -U postgres -v ON_ERROR_STOP=1 \
        -c "CREATE DATABASE pgapp" -c "CREATE DATABASE restoredb" >/dev/null
    docker exec -e PGPASSWORD="$PW" "$PG" psql -h 127.0.0.1 -U postgres -d pgapp -v ON_ERROR_STOP=1 \
        -c "CREATE TABLE t (id serial primary key, v text)" \
        -c "INSERT INTO t (v) SELECT 'fila ñandú \"c\" ''s'' ' || g FROM generate_series(1,1000) g" >/dev/null
    sed -i "/^DB_USER=/d;/^DB_PASSWORD=/d" "$T/servers/gate.env"
    { echo "DB_USER=root"; printf 'DB_PASSWORD=%q\n' "$PW"; } >> "$T/servers/gate.env"
    run_dump "$PG" pgapp
    assert_eq "pg ($img): dump ok (root se traduce a postgres)" true "$(jqv .ok)"
    F="$(jqv .file)"
    assert_contains "pg ($img): trailer real presente" "PostgreSQL database dump complete" "$(zcat "$F" | tail -n 12)"
    zcat "$F" | docker exec -i -e PGPASSWORD="$PW" "$PG" psql -h 127.0.0.1 -U postgres -d restoredb -v ON_ERROR_STOP=0 >/dev/null 2>&1
    assert_eq "pg ($img): restaura con las mismas filas" 1000 \
        "$(docker exec -e PGPASSWORD="$PW" "$PG" psql -h 127.0.0.1 -U postgres -d restoredb -tAc 'SELECT COUNT(*) FROM t')"
done

finish
