#!/usr/bin/env bash
# =============================================================================
# scripts/deploy.sh — Actualiza el despliegue en un VPS: construye las imágenes
# aquí, las envía con docker save | load, copia el código del HEAD y reinicia el
# compose sin compilar en el servidor (el VPS tiene 1 vCPU y poca RAM).
# Uso: scripts/deploy.sh [--dry-run] [--allow-dirty] usuario@host [directorio_remoto]
# Solo toca <directorio_remoto> (default dbsync-bot) y los contenedores dbsync-bot-*.
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
set -uo pipefail
REPO_DIR="${DEPLOY_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO_DIR" || exit 1

dry=0; dirty_ok=0
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --dry-run) dry=1 ;;
        --allow-dirty) dirty_ok=1 ;;
        *) echo "Opción desconocida: $1" >&2; exit 2 ;;
    esac
    shift
done
target="${1:-}"; dir="${2:-dbsync-bot}"
[[ "$target" =~ ^[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9.:_-]+$ ]] || { echo "Uso: $0 [--dry-run] [--allow-dirty] usuario@host [dir]" >&2; exit 2; }
[[ "$dir" =~ ^[A-Za-z0-9._/-]+$ && "$dir" != /* && "$dir" != *..* ]] || { echo "Directorio remoto inválido: $dir" >&2; exit 2; }

if (( ! dirty_ok )) && ! git diff --quiet HEAD --; then
    echo "Hay cambios sin commitear: el despliegue usa HEAD. Haz commit o usa --allow-dirty." >&2
    exit 1
fi
head_sha="$(git rev-parse --short HEAD)"

run() { # descripción, comando...
    echo "-> $1"; shift
    (( dry )) && { echo "   [dry-run] $*"; return 0; }
    "$@"
}
SSH=(ssh -o BatchMode=yes "$target")

echo "Despliegue de $head_sha en $target:~/$dir"
if (( dry )); then
    uid=1000; gid=1000
else
    uid="$("${SSH[@]}" id -u)" && gid="$("${SSH[@]}" id -g)" || { echo "No se pudo consultar el UID remoto" >&2; exit 1; }
fi
export DBSYNC_UID="$uid" DBSYNC_GID="$gid"

run "construir imágenes con UID $uid:$gid" docker compose build -q || exit 1
if (( dry )); then
    echo "-> enviar imágenes"; echo "   [dry-run] docker save ... | gzip | ssh $target 'gunzip | docker load'"
    echo "-> copiar código"; echo "   [dry-run] git archive HEAD | ssh $target 'tar -x -C ~/$dir'"
else
    echo "-> enviar imágenes"
    docker save dbsync-bot-gateway:local dbsync-bot-bot:local | gzip | "${SSH[@]}" "gunzip | docker load" || exit 1
    echo "-> copiar código"
    git archive HEAD | "${SSH[@]}" "mkdir -p ~/$dir && tar -x -C ~/$dir" || exit 1
fi
run "reiniciar el compose" "${SSH[@]}" "cd ~/$dir && docker compose up -d --no-build" || exit 1
if (( ! dry )); then
    sleep 8
    "${SSH[@]}" "cd ~/$dir && docker compose ps --format 'table {{.Service}}\t{{.Status}}'"
fi
echo "Listo: $head_sha desplegado en $target"
