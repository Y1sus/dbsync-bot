#!/usr/bin/env bash
# =============================================================================
# tests/run.sh — Ejecuta todas las pruebas de puerta del runner (<2 s)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
rc=0
for t in test_*.sh; do
    echo "== $t"
    bash "$t" || rc=1
done
(( rc == 0 )) && echo "TODAS LAS PRUEBAS OK" || echo "HAY FALLOS"
exit $rc
