#!/usr/bin/env bash
# =============================================================================
# scripts/init_deploy.sh — Prepara carpetas y secretos locales para docker compose
# Idempotente: no pisa nada que ya exista. No toca la red ni ningún servidor.
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
umask 077

mkdir -p data/dumps data/gateway data/bot secrets/ssh
chmod 700 data data/dumps data/gateway data/bot secrets secrets/ssh

if [[ ! -s secrets/gateway_token ]]; then
    head -c 32 /dev/urandom | base64 | tr -d '/+=\n' > secrets/gateway_token
    echo "creado secrets/gateway_token"
fi
[[ -e secrets/known_hosts ]] || { : > secrets/known_hosts; echo "creado secrets/known_hosts (vacío: usa scripts/pin_known_hosts.sh)"; }
[[ -e .env ]] || { cp .env.example .env; echo "creado .env desde .env.example: revísalo"; }
chmod 600 secrets/gateway_token secrets/known_hosts .env

missing=0
for f in secrets/telegram_token secrets/ssh/dbsync_bot_ed25519 secrets/gitlab_token projects.json; do
    [[ -s "$f" ]] || { echo "FALTA $f" >&2; missing=1; }
done
exit $missing
