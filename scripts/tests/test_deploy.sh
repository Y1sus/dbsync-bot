#!/usr/bin/env bash
# =============================================================================
# scripts/tests/test_deploy.sh — Pruebas de scripts/deploy.sh sin tocar ningún servidor
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$ROOT/scripts/deploy.sh"
PASS=0; FAIL=0
ok()  { echo "  ✓ $1"; PASS=$((PASS + 1)); }
nok() { echo "  ✗ $1"; FAIL=$((FAIL + 1)); }
check() { if [[ "$2" == "$3" ]]; then ok "$1"; else nok "$1 (esperado [$2], obtenido [$3])"; fi; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
git -C "$T" init -q && git -C "$T" -c user.email=t@t -c user.name=t commit -q --allow-empty -m base
echo a > "$T/f"; git -C "$T" add f && git -C "$T" -c user.email=t@t -c user.name=t commit -q -m f
mkdir -p "$T/bin"
printf '#!/bin/sh\necho "STUB $*" >> "%s/calls"\nexit 0\n' "$T" > "$T/bin/docker"; chmod +x "$T/bin/docker"
cp "$T/bin/docker" "$T/bin/ssh"

out="$(PATH="$T/bin:$PATH" DEPLOY_REPO_DIR="$T" "$SCRIPT" --dry-run u@h.example 2>&1)"; rc=$?
check "dry-run: exit 0" 0 "$rc"
[[ "$out" == *"construir imágenes"* && "$out" == *"docker load"* && "$out" == *"git archive HEAD"* && "$out" == *"up -d --no-build"* ]] && ok "dry-run lista los pasos principales" || nok "dry-run lista los pasos principales"
[[ "$out" == *":previous"* && "$out" == *"gateway.db.bak"* ]] && ok "dry-run incluye los respaldos (imagen previa y gateway.db)" || nok "dry-run incluye los respaldos"
check "dry-run no ejecuta ssh" 0 "$(grep -c 'STUB ssh' "$T/calls" 2>/dev/null || echo 0)"

echo b > "$T/f"
PATH="$T/bin:$PATH" DEPLOY_REPO_DIR="$T" "$SCRIPT" --dry-run u@h.example >/dev/null 2>&1; check "árbol sucio: se rechaza (exit 1)" 1 "$?"
PATH="$T/bin:$PATH" DEPLOY_REPO_DIR="$T" "$SCRIPT" --dry-run --allow-dirty u@h.example >/dev/null 2>&1; check "--allow-dirty lo permite" 0 "$?"
git -C "$T" checkout -q f

for bad in 'x' 'u@h;id' 'u@h h' '@h' 'u@'; do
    PATH="$T/bin:$PATH" DEPLOY_REPO_DIR="$T" "$SCRIPT" --dry-run "$bad" >/dev/null 2>&1; check "destino inválido [$bad] (exit 2)" 2 "$?"
done
for bad in '/abs' '../x' 'a;b' 'a b'; do
    PATH="$T/bin:$PATH" DEPLOY_REPO_DIR="$T" "$SCRIPT" --dry-run u@h "$bad" >/dev/null 2>&1; check "directorio remoto inválido [$bad] (exit 2)" 2 "$?"
done
echo "  -- $PASS ok, $FAIL fallos"
(( FAIL == 0 ))
