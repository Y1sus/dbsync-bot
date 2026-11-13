#!/usr/bin/env bash
# shellcheck disable=SC2034
# =============================================================================
# tests/test_scripts.sh — Los scripts remotos no filtran el password ni usan
# comillas simples; el wrapper aplica pipefail (regresión de dumps truncados).
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
log() { :; }; fail() { echo "FAIL: $*" >&2; return 1; }
source "$RUNNER_DIR/lib/remote.sh"
DB_ACCESS_MODE=docker; DB_PASSWORD="$PW"

for engine in mysql postgresql; do
    s="$(script_dump "$engine" mydb root 3306 appdb)"
    assert_not_contains "[$engine] sin comillas simples" "'" "$s"
    assert_not_contains "[$engine] el password no está en el script" "p'a" "$s"
    assert_contains "[$engine] lee el password por stdin" "IFS= read -r DBPW" "$s"
    assert_contains "[$engine] docker hereda la variable sin valor" "docker exec -e " "$s"
    assert_not_contains "[$engine] sin -e VAR=valor" "PWD=" "${s#*export }"
    l="$(script_list_dbs "$engine" mydb root)"
    assert_not_contains "[$engine] listado sin comillas simples" "'" "$l"
done

w="$(wrap_script mysql "$(script_dump mysql mydb root 3306 appdb)")"
assert_contains "wrapper usa pipefail" "bash -o pipefail -c" "$w"

DB_ACCESS_MODE=native+sudo
assert_contains "mysql native+sudo usa sudo -n" "sudo -n bash -o pipefail" "$(wrap_script mysql 'echo hi')"
assert_contains "pg native+sudo corre como postgres" "sudo -n -u postgres bash" "$(wrap_script postgresql 'echo hi')"
DB_ACCESS_MODE=docker

wrap_script mysql "echo 'x'" >/dev/null 2>&1; assert_eq "rechaza scripts con comilla simple" 1 $?

# Mecanismo: sin pipefail el fallo del primer comando se pierde (bug original)
bash -c 'false | gzip -c >/dev/null'; assert_eq "sin pipefail el error se pierde" 0 $?
bash -o pipefail -c 'false | gzip -c >/dev/null'; assert_eq "con pipefail el error se propaga" 1 $?

finish
