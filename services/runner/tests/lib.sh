#!/usr/bin/env bash
# shellcheck disable=SC2034
# =============================================================================
# tests/lib.sh — Utilidades de aserción y entorno aislado para las pruebas
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_DIR="$(cd "$TESTS_DIR/.." && pwd)"
RUNNER="$RUNNER_DIR/runner.sh"
SHIMS="$TESTS_DIR/shims"
CONTRACT="$RUNNER_DIR/../../contracts/job-result.schema.json"
PASS=0; FAIL=0

ok()   { echo "  ✓ $1"; PASS=$((PASS + 1)); }
nok()  { echo "  ✗ $1"; [[ -n "${2:-}" ]] && echo "      $2"; FAIL=$((FAIL + 1)); }

assert_eq() {
    if [[ "$2" == "$3" ]]; then ok "$1"; else nok "$1" "esperado [$2] obtenido [$3]"; fi
}
assert_contains() {
    if [[ "$3" == *"$2"* ]]; then ok "$1"; else nok "$1" "no contiene [$2] en [$3]"; fi
}
assert_not_contains() {
    if [[ "$3" != *"$2"* ]]; then ok "$1"; else nok "$1" "contiene [$2]"; fi
}
assert_file()    { if [[ -f "$2" ]]; then ok "$1"; else nok "$1" "no existe $2"; fi; }
assert_no_file() { if [[ ! -e "$2" ]]; then ok "$1"; else nok "$1" "existe $2"; fi; }

# setup_env — crea un entorno temporal con un servidor 'gate'
PW='p'"'"'a"s$(id)`x y'
setup_env() {
    T="$(mktemp -d)"
    trap 'rm -rf "$T"' EXIT
    mkdir -p "$T/servers" "$T/dumps"
    : > "$T/key"; : > "$T/known_hosts"
    {
        echo "SERVER_HOST=127.0.0.1"
        echo "SERVER_USER=tester"
        echo "SERVER_KEY=$T/key"
        echo "DB_USER=root"
        printf 'DB_PASSWORD=%q\n' "$PW"
    } > "$T/servers/gate.env"
    chmod 600 "$T/servers/gate.env"
    : > "$T/ssh.log"
}

# run_runner <args...> — deja OUT (stdout), ERR (stderr) y RC
run_runner() {
    OUT="$(DBSYNC_SERVERS_DIR="$T/servers" DBSYNC_DUMPS_DIR="$T/dumps" \
        DBSYNC_KNOWN_HOSTS="$T/known_hosts" DBSYNC_SSH_BIN="$SHIMS/ssh" \
        SHIM_LOG="$T/ssh.log" SHIM_PATH_PREFIX="$SHIMS" FAKE_ENVLOG="$T/envlog" \
        DBSYNC_MIN_FREE_MB=1 "$RUNNER" "$@" 2>"$T/stderr")"
    RC=$?
    ERR="$(cat "$T/stderr")"
}

jqv() { printf '%s' "$OUT" | jq -r "$1"; }

finish() {
    echo "  -- $PASS ok, $FAIL fallos"
    (( FAIL == 0 ))
}
