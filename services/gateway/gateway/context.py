# =============================================================================
# services/gateway/gateway/context.py — Estado compartido entre listeners y tareas
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import os
import threading
import time
from collections import Counter

from .audit import Audit
from .config import load_api_token
from .limiter import MissLimiter
from .store import Store


class ActiveStreams:
    """Streams en curso por (enlace, archivo); el tope por enlace cuenta el total de sus archivos."""

    def __init__(self):
        self._files = Counter()
        self._links = Counter()
        self._total = 0
        self._lock = threading.Lock()

    @property
    def total(self):
        return self._total

    def enter(self, link_id, idx=0):
        with self._lock:
            self._files[(link_id, idx)] += 1
            self._links[link_id] += 1
            self._total += 1

    def exit(self, link_id, idx=0):
        with self._lock:
            self._total -= 1
            for counter, key in ((self._files, (link_id, idx)), (self._links, link_id)):
                counter[key] -= 1
                if counter[key] <= 0:
                    del counter[key]

    def count(self, link_id, idx=None):
        """Streams del enlace completo (idx None) o de un archivo."""
        with self._lock:
            if idx is None:
                return self._links.get(link_id, 0)
            return self._files.get((link_id, idx), 0)

    def busy(self, link_id, idx=None):
        with self._lock:
            if idx is None:
                return self._links.get(link_id, 0) > 0
            return self._files.get((link_id, idx), 0) > 0


class Context:
    def __init__(self, config, store, audit, api_token, clock=time.time):
        self.config = config
        self.store = store
        self.audit = audit
        self.api_token = api_token
        self.clock = clock
        self.active = ActiveStreams()
        self.revoked = set()
        self.limiter = MissLimiter(config.miss_limit, config.miss_window, config.block_seconds,
                                   config.limiter_max_ips, clock)

    def now(self):
        return int(self.clock())

    async def db(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    def close(self):
        self.store.close()
        self.audit.close()


def build_context(config, clock=time.time):
    os.umask(0o077)
    os.makedirs(config.data_dir, mode=0o700, exist_ok=True)
    os.chmod(config.data_dir, 0o700)
    token = load_api_token(config.api_token_file)
    store = Store(os.path.join(config.data_dir, "gateway.db"))
    audit = Audit(os.path.join(config.data_dir, "audit.jsonl"), clock,
                  int(config.audit_max_mb * 1024 * 1024), config.audit_keep)
    return Context(config, store, audit, token, clock)
