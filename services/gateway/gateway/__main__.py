# =============================================================================
# services/gateway/gateway/__main__.py — Punto de entrada: python -m gateway
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import logging
import signal
import sys

from .app import Gateway
from .config import ConfigError, from_env
from .context import build_context


async def _run(config):
    ctx = build_context(config)
    gateway = Gateway(ctx)
    await gateway.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await gateway.stop()
    ctx.close()


def main():
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO,
        format="%(asctime)s [gateway] %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = from_env()
        asyncio.run(_run(config))
    except ConfigError as exc:
        print(f"Configuración inválida: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
