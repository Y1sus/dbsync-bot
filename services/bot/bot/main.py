# =============================================================================
# services/bot/bot/main.py — Arranque: configuración, logging, long polling y apagado limpio
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import BotCommand

from .audit import Audit
from .config import ConfigError, load_config, read_secret
from .flow import WizardStore
from .gateway_client import GatewayClient
from .handlers import COMMANDS, App, AuthMiddleware, build_router
from .jobs import JobManager
from .redact import Redactor, install_logging
from .runner_client import RunnerClient

log = logging.getLogger("bot.main")


async def amain(redactor, env=None):
    cfg = load_config(env)
    bot_token = read_secret(cfg.token_file, "BOT_TOKEN_FILE")
    gw_token = read_secret(cfg.gateway_token_file, "BOT_GATEWAY_TOKEN_FILE")
    redactor.add(bot_token)
    redactor.add(gw_token)

    audit = Audit(f"{cfg.data_dir.rstrip('/')}/audit.jsonl", redactor)
    runner = RunnerClient(cfg.runner_path, timeout=cfg.job_timeout, redactor=redactor)
    gateway = GatewayClient(cfg.gateway_url, gw_token, redactor=redactor)
    jobs = JobManager(runner, gateway, audit, cfg.max_jobs)
    app = App(cfg, runner, gateway, jobs, WizardStore(), audit)

    session = AiohttpSession(api=TelegramAPIServer.from_base(cfg.telegram_api_base)) if cfg.telegram_api_base else AiohttpSession()
    bot = Bot(bot_token, session=session)
    dp = Dispatcher()
    dp.update.outer_middleware(AuthMiddleware(app))
    dp.include_router(build_router(app))
    try:
        me = await bot.get_me()
        log.info("Bot iniciado como @%s", me.username)
        try:
            await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
        except Exception as exc:
            log.warning("No se pudieron registrar los comandos (%s)", type(exc).__name__)
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        log.info("Apagando: cancelando jobs")
        await jobs.shutdown()
        await gateway.close()
        await bot.session.close()


def run():
    redactor = Redactor()
    install_logging(redactor)
    try:
        asyncio.run(amain(redactor))
    except ConfigError as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        pass
    except Exception:
        log.critical("El bot terminó por un error no controlado", exc_info=True)
        sys.exit(1)
