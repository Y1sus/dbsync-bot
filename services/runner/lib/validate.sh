#!/usr/bin/env bash
# =============================================================================
# services/runner/lib/validate.sh — Validación estricta de identificadores
# Todo valor que termina dentro de un comando remoto pasa por aquí primero.
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

# Los nombres no admiten comillas, $, backticks, espacios ni saltos de línea.
valid_server()    { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]]; }
valid_container() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; }
valid_db()        { [[ "$1" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; }
valid_dbuser()    { [[ "$1" =~ ^[A-Za-z0-9_][A-Za-z0-9_.@-]{0,63}$ ]]; }
valid_jobid()     { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]]; }
valid_host()      { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9.:_-]{0,252}$ ]]; }
valid_sshuser()   { [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_.-]{0,31}$ ]]; }
valid_port()      { [[ "$1" =~ ^[0-9]{1,5}$ ]] && (( 10#$1 >= 1 && 10#$1 <= 65535 )); }
valid_engine()    { [[ "$1" == "mysql" || "$1" == "postgresql" ]]; }
valid_mode()      { [[ "$1" == "docker" || "$1" == "native" || "$1" == "native+sudo" ]]; }

# El password viaja por stdin, así que solo se prohíben los caracteres que
# romperían el protocolo de una línea (salto de línea, retorno de carro, NUL).
valid_password() {
    [[ -n "$1" && ${#1} -le 256 && "$1" != *$'\n'* && "$1" != *$'\r'* ]]
}
