# =============================================================================
# services/gateway/gateway/audit.py — Auditoría JSONL y extracción de IP y user-agent
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import ipaddress
import json
import logging
import os
import threading
import time

log = logging.getLogger("gateway.audit")


def iso_utc(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def client_info(request):
    ip = request.headers.get("X-Real-IP", "").strip()
    try:
        ip = str(ipaddress.ip_address(ip))
    except ValueError:
        ip = request.remote
    return ip, request.headers.get("User-Agent", "")[:200]


class Audit:
    def __init__(self, path, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
        self._fh = os.fdopen(fd, "a", buffering=1, encoding="utf-8")

    def log(self, event, link_id=None, ip=None, ua=None, nbytes=None, **extra):
        entry = {
            "ts": iso_utc(self._clock()),
            "event": event,
            "link_id": link_id,
            "ip": ip,
            "ua": ua,
            "bytes": nbytes,
        }
        entry.update(extra)
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                self._fh.write(line + "\n")
            except (OSError, ValueError):
                log.error("No se pudo escribir la auditoría")

    def close(self):
        with self._lock:
            self._fh.close()
