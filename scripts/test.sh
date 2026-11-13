#!/usr/bin/env bash
# =============================================================================
# scripts/test.sh — Pruebas de puerta de todos los servicios (+ shellcheck)
# Uso: scripts/test.sh            (puerta, <2 s, sin red ni Docker)
#      scripts/test.sh --eval     (agrega evals con Docker real, ~30 s)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
rc=0
if command -v shellcheck >/dev/null; then
    echo "== shellcheck"
    shellcheck -x -S warning services/runner/runner.sh services/runner/lib/*.sh \
        services/runner/tests/*.sh services/runner/tests/shims/* services/runner/evals/*.sh scripts/*.sh .githooks/* \
        || rc=1
else
    echo "WARN shellcheck no instalado: se omite el lint"
fi
bash services/runner/tests/run.sh || rc=1
if [[ "${1:-}" == "--eval" ]]; then
    echo "== evals"
    bash services/runner/evals/e2e_dump.sh || rc=1
fi
exit $rc
