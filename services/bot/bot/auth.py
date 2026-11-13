# =============================================================================
# services/bot/bot/auth.py — Decisión pura de autorización de updates (sin aiogram)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import time
from dataclasses import dataclass

ALLOWED_KINDS = ("message", "callback_query")


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str


def decide(kind, user_id, chat_type, allowed_ids) -> Decision:
    if kind not in ALLOWED_KINDS:
        return Decision(False, "tipo_no_permitido")
    if user_id is None or user_id not in allowed_ids:
        return Decision(False, "usuario_no_autorizado")
    if chat_type != "private":
        return Decision(False, "chat_no_privado")
    return Decision(True, "ok")


class RejectLimiter:
    """Evita inundar la auditoría si un extraño insiste: 1 línea por usuario por ventana."""

    def __init__(self, window=60.0, clock=time.monotonic, max_keys=1000):
        self.window = window
        self.clock = clock
        self.max_keys = max_keys
        self._last = {}

    def should_log(self, key) -> bool:
        now = self.clock()
        last = self._last.get(key)
        if last is not None and now - last < self.window:
            return False
        if len(self._last) >= self.max_keys:
            self._last.clear()
        self._last[key] = now
        return True
