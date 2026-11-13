#!/usr/bin/env bash
# =============================================================================
# scripts/test.sh — Pruebas de puerta de todos los servicios (+ shellcheck)
# Las suites corren en paralelo. Uso:
#   scripts/test.sh            puerta: runner, gateway, bot e integración (~2 s, sin red ni Docker)
#   scripts/test.sh --eval     agrega los evals de cada servicio (Docker real para el runner)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
rc=0

suites=()
run_suite() { # nombre comando...
    local name="$1"; shift
    suites+=("$name")
    ( "$@" ) > "$TMP/$name.out" 2>&1 && echo 0 > "$TMP/$name.rc" || echo 1 > "$TMP/$name.rc" &
}
wait_suites() {
    wait
    local s
    for s in "${suites[@]}"; do
        echo "== $s"
        tail -n 4 "$TMP/$s.out"
        if [[ "$(cat "$TMP/$s.rc")" != 0 ]]; then
            echo "-- salida completa de $s --"; cat "$TMP/$s.out"; rc=1
        fi
    done
    suites=()
}

if command -v shellcheck >/dev/null; then
    run_suite shellcheck shellcheck -x -S warning services/runner/runner.sh services/runner/lib/*.sh \
        services/runner/tests/*.sh services/runner/tests/shims/* services/runner/evals/*.sh scripts/*.sh .githooks/*
else
    echo "WARN shellcheck no instalado: se omite el lint"
fi
run_suite runner bash services/runner/tests/run.sh
run_suite gateway bash -c "cd services/gateway && '$PY' -m unittest discover -s tests -t ."
run_suite bot bash -c "cd services/bot && '$PY' -m unittest discover -s tests -t ."
run_suite integracion "$PY" integration/e2e_stack.py
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
    run_suite compose env GATEWAY_PUBLIC_BASE_URL=https://dumps.example BOT_ALLOWED_USER_IDS=1 docker compose config -q
fi
wait_suites

if [[ "${1:-}" == "--eval" ]]; then
    echo "######## evals"
    run_suite eval-runner bash services/runner/evals/e2e_dump.sh
    run_suite eval-gateway bash -c "cd services/gateway && '$PY' evals/e2e_gateway.py"
    run_suite eval-bot bash -c "cd services/bot && '$PY' evals/e2e_bot.py"
    wait_suites
fi
(( rc == 0 )) && echo "TODO OK" || echo "HAY FALLOS"
exit $rc
