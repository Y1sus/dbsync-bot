#!/usr/bin/env bash
# =============================================================================
# tests/test_validate.sh — Validación de identificadores (anti inyección)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
source "$RUNNER_DIR/lib/validate.sh"

accept() { local fn="$1"; shift; for v in "$@"; do if "$fn" "$v"; then ok "$fn acepta [$v]"; else nok "$fn rechaza válido [$v]"; fi; done; }
reject() { local fn="$1"; shift; for v in "$@"; do if "$fn" "$v"; then nok "$fn acepta peligroso [$v]"; else ok "$fn rechaza [$v]"; fi; done; }

BAD=('' 'a;id' 'a b' 'a$(id)' 'a`id`' "a'b" 'a"b' 'a|b' 'a&b' 'a>b' '../x' 'a/b' $'a\nb' '-rf' '.hid')

accept valid_db appdb app_db app-db app.db _x db123
reject valid_db "${BAD[@]}" 'a$b' 'a*'
accept valid_container mysql8 my_app-db.1
reject valid_container "${BAD[@]}"
accept valid_server prod prod-1 cliente_a
reject valid_server "${BAD[@]}" 'a.b'
accept valid_dbuser root readonly app@host ro-user
reject valid_dbuser "${BAD[@]}"
accept valid_jobid 20260101-1 abc_123
reject valid_jobid "${BAD[@]}"
accept valid_port 22 3306 65535
reject valid_port 0 65536 abc '' '22;id' -1
accept valid_engine mysql postgresql
reject valid_engine mariadb pg ''
accept valid_mode docker native native+sudo
reject valid_mode sudo ''

# Los passwords pueden llevar cualquier carácter salvo saltos de línea
accept valid_password "p'a\"s\$(id)\`x y" 'ñandú*'
reject valid_password '' $'a\nb' $'a\rb'

finish
