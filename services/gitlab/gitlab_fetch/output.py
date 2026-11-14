# =============================================================================
# services/gitlab/gitlab_fetch/output.py — Salida JSON por stdout, logs por stderr y redacción de secretos
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import json
import re
import sys
from urllib.parse import quote

from . import SCHEMA_VERSION

_PATTERN = re.compile(r"glpat-[A-Za-z0-9_.-]*")
_secrets = []


def register_secret(value):
    if value:
        for variant in {value, quote(value, safe=""), json.dumps(value)[1:-1]}:
            if variant not in _secrets:
                _secrets.append(variant)


def redact(text):
    text = _PATTERN.sub("[REDACTADO]", str(text))
    for secret in _secrets:
        text = text.replace(secret, "[REDACTADO]")
    return text


def _walk(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_walk(v) for v in value]
    if isinstance(value, dict):
        return {k: _walk(v) for k, v in value.items()}
    return value


def log(message):
    sys.stderr.write(f"[gitlab] {redact(message)}\n")
    sys.stderr.flush()


def emit(command, ok, **fields):
    doc = _walk({"schema_version": SCHEMA_VERSION, "ok": ok, "command": command, **fields})
    sys.stdout.write(redact(json.dumps(doc, ensure_ascii=True, separators=(",", ":"))) + "\n")
    sys.stdout.flush()


def emit_error(command, code, message):
    emit(command, False, error={"code": code, "message": message})
