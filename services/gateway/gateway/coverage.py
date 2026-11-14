# =============================================================================
# services/gateway/gateway/coverage.py — Unión de intervalos de bytes enviados por sesión
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

MAX_INTERVALS = 64


def merge(ranges, start, end):
    """Une [start, end) a una lista de intervalos [a, b). Acota el tamaño descartando el más corto."""
    if end <= start:
        return [list(item) for item in ranges]
    out = []
    for first, last in sorted([list(item) for item in ranges] + [[start, end]]):
        if out and first <= out[-1][1]:
            out[-1][1] = max(out[-1][1], last)
        else:
            out.append([first, last])
    while len(out) > MAX_INTERVALS:
        out.remove(min(out, key=lambda item: item[1] - item[0]))
    return out


def covers(ranges, size):
    return len(ranges) == 1 and ranges[0][0] <= 0 and ranges[0][1] >= size
