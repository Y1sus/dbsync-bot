# =============================================================================
# services/gateway/gateway/ranges.py — Parseo de la cabecera Range (solo rangos simples)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import re


class RangeNotSatisfiable(Exception):
    pass


_SPEC = re.compile(r"^([0-9]{0,18})-([0-9]{0,18})$")


def parse_range(header, size):
    """Devuelve None (sin rango o multi-rango: se sirve completo) o (inicio, fin) inclusivos."""
    if header is None:
        return None
    value = header.strip()
    if not value.lower().startswith("bytes="):
        raise RangeNotSatisfiable("unidad no soportada")
    spec = value[6:].strip()
    if "," in spec:
        return None
    match = _SPEC.match(spec)
    if not match or (not match.group(1) and not match.group(2)):
        raise RangeNotSatisfiable("rango mal formado")
    first, last = match.groups()
    if not first:
        count = int(last)
        if count == 0:
            raise RangeNotSatisfiable("sufijo vacío")
        return (max(size - count, 0), size - 1)
    start = int(first)
    if start >= size:
        raise RangeNotSatisfiable("inicio fuera de límites")
    if not last:
        return (start, size - 1)
    end = int(last)
    if end < start:
        raise RangeNotSatisfiable("rango invertido")
    return (start, min(end, size - 1))
