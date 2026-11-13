# =============================================================================
# services/bot/bot/audit.py — Auditoría JSONL de comandos, jobs, enlaces y updates rechazados
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import json
import os
import threading
import time


class Audit:
    def __init__(self, path, redactor=str):
        self.path = path
        self._redact = redactor
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, event: str, **fields) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
        for k, v in fields.items():
            rec[k] = self._redact(v) if isinstance(v, str) else v
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (line + "\n").encode())
            finally:
                os.close(fd)
