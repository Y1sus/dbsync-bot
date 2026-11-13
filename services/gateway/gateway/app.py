# =============================================================================
# services/gateway/gateway/app.py — Arranque y parada de los dos listeners y la limpieza
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import contextlib
import logging

from aiohttp import web

from . import cleanup, internal, public

log = logging.getLogger("gateway")


class Gateway:
    def __init__(self, ctx):
        self.ctx = ctx
        self.public_port = None
        self.internal_port = None
        self._runners = []
        self._task = None

    async def _listen(self, app, port):
        runner = web.AppRunner(app, access_log=None, handle_signals=False, shutdown_timeout=10)
        await runner.setup()
        await web.TCPSite(runner, self.ctx.config.bind_host, port).start()
        self._runners.append(runner)
        return runner.addresses[0][1]

    async def start(self):
        cfg = self.ctx.config
        self.public_port = await self._listen(public.create_app(self.ctx), cfg.public_port)
        self.internal_port = await self._listen(internal.create_app(self.ctx), cfg.internal_port)
        self._task = asyncio.create_task(cleanup.loop(self.ctx))
        log.info("Gateway activo: público :%s, interno :%s", self.public_port, self.internal_port)

    async def stop(self):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for runner in self._runners:
            await runner.cleanup()
        self._runners.clear()
        log.info("Gateway detenido")
