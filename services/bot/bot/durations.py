# =============================================================================
# services/bot/bot/durations.py — Parseo y formato de duraciones (90m, 2h, 1d)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import re

MIN_SECONDS = 60
MAX_SECONDS = 7 * 24 * 3600
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_RE = re.compile(r"^([0-9]{1,9})([smhd])\Z")


class DurationError(ValueError):
    pass


def parse_duration(text: str) -> int:
    m = _RE.match((text or "").strip().lower())
    if not m:
        raise DurationError("Duración inválida. Usa por ejemplo 90m, 2h o 1d.")
    seconds = int(m.group(1)) * _UNITS[m.group(2)]
    if seconds < MIN_SECONDS or seconds > MAX_SECONDS:
        raise DurationError("La duración debe estar entre 60s y 7d.")
    return seconds


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        qty, seconds = divmod(seconds, size)
        if qty:
            parts.append(f"{qty} {unit}")
    return " ".join(parts) or "0 s"
