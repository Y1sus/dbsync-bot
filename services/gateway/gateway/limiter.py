# =============================================================================
# services/gateway/gateway/limiter.py — Limitador de respuestas 404 por IP con tablas acotadas
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import ipaddress
import math
from collections import OrderedDict, deque


def ip_key(ip):
    """IPv4 mapeada se reduce a su IPv4; IPv6 se agrupa por /64 para que rotar direcciones no evada el límite."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return str(ip)
    addr = getattr(addr, "ipv4_mapped", None) or addr
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False).network_address)
    return str(addr)


class MissLimiter:
    """Aciertos recientes en un LRU y bloqueos en otra tabla: ambas acotadas, desalojo O(1)."""

    def __init__(self, limit, window, block_seconds, max_ips, clock):
        self.limit = limit
        self.window = window
        self.block_seconds = block_seconds
        self.max_ips = max_ips
        self._clock = clock
        self._hits = OrderedDict()
        self._blocked = {}

    def blocked(self, ip):
        """Segundos de espera si la IP está bloqueada; 0 si puede continuar."""
        key = ip_key(ip)
        until = self._blocked.get(key)
        if until is None:
            return 0
        now = self._clock()
        if until <= now:
            del self._blocked[key]
            return 0
        return max(1, math.ceil(until - now))

    def miss(self, ip):
        """Registra un 404. True si con este fallo la IP pasa a estar bloqueada."""
        now = self._clock()
        key = ip_key(ip)
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= self.max_ips:
                self._hits.popitem(last=False)
            hits = self._hits[key] = deque(maxlen=self.limit + 1)
        self._hits.move_to_end(key)
        hits.append(now)
        if sum(1 for stamp in hits if stamp > now - self.window) <= self.limit:
            return False
        del self._hits[key]
        self._blocked.pop(key, None)
        if len(self._blocked) >= self.max_ips:
            del self._blocked[next(iter(self._blocked))]
        self._blocked[key] = now + self.block_seconds
        return True

    def size(self):
        return len(self._hits) + len(self._blocked)
