#!/usr/bin/env bash
# =============================================================================
# scripts/tests/eval_nginx.sh — Valida la sintaxis de deploy/nginx/ con nginx real (Docker)
# y comprueba que el log de acceso redacta el token del enlace.
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
command -v docker >/dev/null || { echo "docker no disponible: se omite"; exit 0; }
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/conf.d" "$T/sites"
cp "$ROOT/deploy/nginx/dumps-limits.conf" "$T/conf.d/"; cp "$ROOT/deploy/nginx/dumps.synkra.app" "$T/sites/"
printf 'user nginx;\nevents {}\nhttp {\n    include /etc/nginx/conf.d/*.conf;\n    include /etc/nginx/sites/*;\n}\n' > "$T/nginx.conf"
rc=0
run() { docker run --rm -v "$T/nginx.conf:/etc/nginx/nginx.conf:ro" -v "$T/conf.d:/etc/nginx/conf.d:ro" -v "$T/sites:/etc/nginx/sites:ro" nginx:alpine sh -c "$1"; }

out="$(run 'mkdir -p /var/log/nginx && nginx -t' 2>&1)"
[[ "$out" == *"test is successful"* ]] && echo "  ✓ nginx -t acepta el vhost y los límites" || { echo "  ✗ nginx -t falla"; echo "$out" | tail -4; rc=1; }

printf 'user nginx;\nevents {}\nhttp {\n    include /etc/nginx/conf.d/*.conf;\n    server { listen 8088; access_log /var/log/nginx/t.log dumps_safe; location / { return 200 ok; } }\n}\n' > "$T/nginx.conf"
log="$(run 'mkdir -p /var/log/nginx; nginx; sleep 1; wget -qO- http://127.0.0.1:8088/d/SECRETOSECRETOSECRETO >/dev/null; cat /var/log/nginx/t.log' 2>&1)"
if [[ "$log" == *"/d/<redactado>"* && "$log" != *SECRETO* ]]; then echo "  ✓ el log de acceso no contiene el token"; else echo "  ✗ el log filtra el token"; rc=1; fi
exit $rc
