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
from collections import OrderedDict

from .limiter import ip_key

log = logging.getLogger("gateway.audit")

UA_MAX = 120
LIMITED_EVENTS = {"origin_rejected", "rate_limited"}
LIMIT_PER_WINDOW = 5
LIMIT_WINDOW = 60
LIMIT_TABLE = 10000


def iso_utc(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def client_info(request):
    ip = request.headers.get("X-Real-IP", "").strip()
    try:
        ip = str(ipaddress.ip_address(ip))
    except ValueError:
        ip = request.remote
    return ip, request.headers.get("User-Agent", "")[:UA_MAX]


class Audit:
    def __init__(self, path, clock=time.time, max_bytes=50 * 1024 * 1024, keep=5):
        self._path = path
        self._clock = clock
        self._max = max_bytes
        self._keep = keep
        self._lock = threading.Lock()
        self._throttle = OrderedDict()
        self._open()

    def _open(self):
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
        self._fh = os.fdopen(fd, "a", buffering=1, encoding="utf-8")
        self._size = os.fstat(fd).st_size

    def _rotate(self):
        self._fh.close()
        for index in range(self._keep, 0, -1):
            source = self._path if index == 1 else f"{self._path}.{index - 1}"
            if os.path.exists(source):
                os.replace(source, f"{self._path}.{index}")
        self._open()

    def _allowed(self, event, ip, now):
        """Limita a 5/min por IP los eventos que un anónimo puede disparar; devuelve (permitido, suprimidos)."""
        if event not in LIMITED_EVENTS:
            return True, 0
        ip = ip_key(ip)
        entry = self._throttle.get(ip)
        if entry is None:
            if len(self._throttle) >= LIMIT_TABLE:
                self._throttle.popitem(last=False)
            entry = self._throttle[ip] = {"start": now, "count": 0, "suppressed": 0}
        self._throttle.move_to_end(ip)
        if now - entry["start"] >= LIMIT_WINDOW:
            entry["start"], entry["count"] = now, 0
        if entry["count"] >= LIMIT_PER_WINDOW:
            entry["suppressed"] += 1
            return False, 0
        entry["count"] += 1
        pending, entry["suppressed"] = entry["suppressed"], 0
        return True, pending

    def log(self, event, link_id=None, ip=None, ua=None, nbytes=None, **extra):
        now = self._clock()
        entry = {
            "ts": iso_utc(now),
            "event": event,
            "link_id": link_id,
            "ip": ip,
            "ua": ua[:UA_MAX] if isinstance(ua, str) else ua,
            "bytes": nbytes,
        }
        entry.update(extra)
        with self._lock:
            allowed, suppressed = self._allowed(event, ip, now)
            if not allowed:
                return
            if suppressed:
                entry["suppressed"] = suppressed
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
            try:
                size = len(line.encode("utf-8"))
                if self._size and self._size + size > self._max:
                    self._rotate()
                self._fh.write(line)
                self._size += size
            except (OSError, ValueError):
                log.error("No se pudo escribir la auditoría")

    def close(self):
        with self._lock:
            self._fh.close()
