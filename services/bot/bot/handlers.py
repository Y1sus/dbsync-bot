# =============================================================================
# services/bot/bot/handlers.py — Adaptador aiogram: middleware de autorización, comandos y callbacks
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from aiogram import BaseMiddleware, Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup,
    LinkPreviewOptions, Message, Update,
)

from . import flow
from .auth import RejectLimiter, decide
from .gateway_client import GatewayError
from .jobs import DumpRequest, new_job_id
from .runner_client import RunnerError

log = logging.getLogger("bot.handlers")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
COMMANDS = [
    ("dump", "Generar un dump"), ("servers", "Servidores"), ("links", "Enlaces activos"),
    ("revoke", "Revocar un enlace"), ("status", "Estado de los jobs"), ("help", "Ayuda"),
]


@dataclass
class App:
    config: object
    runner: object
    gateway: object
    jobs: object
    store: object
    audit: object


def markup(rows):
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=b.text, callback_data=b.data) for b in row] for row in rows
    ])


async def send(message: Message, view: flow.View) -> Message:
    return await message.answer(view.text, parse_mode="HTML", reply_markup=markup(view.keyboard),
                                link_preview_options=NO_PREVIEW)


async def edit(bot: Bot, chat_id, message_id, text, keyboard=None):
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML",
                                    reply_markup=markup(keyboard), link_preview_options=NO_PREVIEW)
    except TelegramAPIError as exc:
        if "message is not modified" in str(exc):
            return
        raise


class AuthMiddleware(BaseMiddleware):
    def __init__(self, app: App):
        self.app = app
        self.limiter = RejectLimiter()

    async def __call__(self, handler, event: Update, data):
        kind = event.event_type
        inner = getattr(event, kind, None) if kind != "unknown" else None
        user = getattr(inner, "from_user", None)
        user_id = user.id if user is not None and not user.is_bot else None
        chat = getattr(inner, "chat", None)
        if kind == "callback_query" and inner is not None:
            chat = getattr(inner.message, "chat", None)
        decision = decide(kind, user_id, getattr(chat, "type", None), self.app.config.allowed_user_ids)
        if not decision.ok:
            if self.limiter.should_log((user_id, kind)):
                self.app.audit.write("update_rejected", user_id=user_id, kind=kind, reason=decision.reason)
            return None
        if kind == "message":
            cmd = flow.command_name(inner.text)
            if cmd:
                self.app.audit.write("command", user_id=user_id, command=cmd)
        return await handler(event, data)


def build_router(app: App) -> Router:
    router = Router()
    cfg = app.config

    async def runner_failure(message, exc, what):
        await send(message, flow.View(flow.fmt_failure(exc.code, exc.message, exc.stderr, what)))

    async def report(bot, chat_id, message_id, req, job_id):
        async def queued(position):
            await edit(bot, chat_id, message_id, flow.fmt_queued(position))

        async def started():
            await edit(bot, chat_id, message_id, flow.fmt_started(req))

        try:
            outcome = await app.jobs.run(req, job_id, on_queued=queued, on_start=started)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(edit(bot, chat_id, message_id, "El dump se canceló porque el bot se reinició."), 3)
            raise
        if outcome.kind == "ok":
            text = flow.fmt_success(outcome, req)
        elif outcome.kind == "link_failed":
            text = flow.fmt_link_failure(outcome)
        else:
            text = flow.fmt_failure(outcome.code, outcome.message, outcome.stderr)
        try:
            await edit(bot, chat_id, message_id, text)
        except TelegramAPIError:
            log.warning("No se pudo editar el mensaje del job; se envía uno nuevo")
            with contextlib.suppress(TelegramAPIError):
                await bot.send_message(chat_id, text, parse_mode="HTML", link_preview_options=NO_PREVIEW)

    def start_job(bot, chat_id, message_id, req, update_id):
        job_id = new_job_id(update_id)
        app.jobs.spawn(report(bot, chat_id, message_id, req, job_id))

    @router.message(Command("start", "help"))
    async def cmd_help(message: Message):
        await send(message, flow.fmt_help())

    @router.message(Command("servers"))
    async def cmd_servers(message: Message):
        try:
            servers = await app.runner.servers()
        except RunnerError as exc:
            return await runner_failure(message, exc, "la consulta")
        await send(message, flow.fmt_servers(servers))

    @router.message(Command("dump"))
    async def cmd_dump(message: Message, command: CommandObject, bot: Bot, event_update: Update):
        if command.args:
            try:
                args = flow.parse_dump_args(command.args, cfg.default_ttl, cfg.default_max_downloads)
            except flow.UsageError as exc:
                return await send(message, flow.View(str(exc)))
            req = DumpRequest(message.from_user.id, args.server, args.container, args.db, args.ttl, args.max_downloads)
            sent = await message.answer(flow.fmt_started(req), parse_mode="HTML", link_preview_options=NO_PREVIEW)
            return start_job(bot, sent.chat.id, sent.message_id, req, event_update.update_id)
        try:
            servers = await app.runner.servers()
        except RunnerError as exc:
            return await runner_failure(message, exc, "la consulta")
        w = app.store.create(message.from_user.id, cfg.default_ttl, cfg.default_max_downloads)
        flow.set_servers(w, [s["name"] for s in servers])
        await send(message, flow.render(w))

    @router.message(Command("links"))
    async def cmd_links(message: Message):
        try:
            links = await app.gateway.list_links()
        except GatewayError as exc:
            return await send(message, flow.View(flow.fmt_gateway_error(exc)))
        await send(message, flow.fmt_links(links))

    @router.message(Command("revoke"))
    async def cmd_revoke(message: Message, command: CommandObject):
        try:
            link_id = flow.parse_revoke_arg(command.args)
            await app.gateway.revoke(link_id)
        except flow.UsageError as exc:
            return await send(message, flow.View(str(exc)))
        except GatewayError as exc:
            text = "Ese enlace no existe." if exc.code == "not_found" else flow.fmt_gateway_error(exc)
            return await send(message, flow.View(text))
        app.audit.write("link_revoked", user_id=message.from_user.id, link_id=link_id)
        await send(message, flow.View(f"Enlace <code>{link_id}</code> revocado."))

    @router.message(Command("status"))
    async def cmd_status(message: Message):
        running, waiting = app.jobs.status()
        await send(message, flow.fmt_status(running, waiting))

    @router.callback_query()
    async def on_callback(cb: CallbackQuery, bot: Bot, event_update: Update):
        msg = cb.message
        parsed = flow.parse_callback(cb.data)
        if parsed is None or msg is None:
            return await cb.answer("Opción no válida.")
        chat_id, message_id = msg.chat.id, msg.message_id
        if parsed[0] == "r":
            return await on_revoke(cb, bot, chat_id, message_id, parsed[1])
        _, wid, action, arg = parsed
        w = app.store.get(wid, cb.from_user.id)
        if w is None:
            await cb.answer("La sesión expiró. Usa /dump de nuevo.")
            with contextlib.suppress(TelegramAPIError):
                await edit(bot, chat_id, message_id, "La sesión expiró. Usa /dump de nuevo.")
            return
        step = flow.advance(w, action, arg)
        if step.kind == "invalid":
            return await cb.answer(step.notice)
        await cb.answer()
        if step.kind == "cancel":
            app.store.drop(w.id)
            return await edit(bot, chat_id, message_id, "Dump cancelado.")
        if step.kind == "run":
            app.store.drop(w.id)
            req = DumpRequest(cb.from_user.id, w.server, w.container, w.db, w.ttl, w.max_downloads, w.engine or None)
            await edit(bot, chat_id, message_id, flow.fmt_started(req))
            return start_job(bot, chat_id, message_id, req, event_update.update_id)
        if step.kind == "fetch_containers":
            try:
                flow.set_containers(w, await app.runner.containers(w.server))
            except RunnerError as exc:
                flow.release(w)
                w.step = "server"
                view = flow.render_error(w, flow.fmt_failure(exc.code, exc.message, exc.stderr, "la consulta"))
                return await edit(bot, chat_id, message_id, view.text, view.keyboard)
        elif step.kind == "fetch_dbs":
            try:
                res = await app.runner.dbs(w.server, w.container)
                flow.set_dbs(w, res["engine"], res["databases"])
            except RunnerError as exc:
                flow.release(w)
                w.step = "container"
                view = flow.render_error(w, flow.fmt_failure(exc.code, exc.message, exc.stderr, "la consulta"))
                return await edit(bot, chat_id, message_id, view.text, view.keyboard)
        view = flow.render(w)
        await edit(bot, chat_id, message_id, view.text, view.keyboard)

    async def on_revoke(cb, bot, chat_id, message_id, link_id):
        try:
            await app.gateway.revoke(link_id)
        except GatewayError as exc:
            if exc.code != "not_found":
                return await cb.answer(flow.fmt_gateway_error(exc)[:150], show_alert=True)
        else:
            app.audit.write("link_revoked", user_id=cb.from_user.id, link_id=link_id)
        await cb.answer("Enlace revocado.")
        try:
            view = flow.fmt_links(await app.gateway.list_links())
        except GatewayError:
            view = flow.View(f"Enlace <code>{link_id}</code> revocado.")
        await edit(bot, chat_id, message_id, view.text, view.keyboard)

    @router.errors()
    async def on_error(event: ErrorEvent):
        log.error("Error no controlado en un handler", exc_info=event.exception)
        update = event.update
        with contextlib.suppress(Exception):
            if update.callback_query:
                await update.callback_query.answer("Error interno del bot.")
            elif update.message:
                await update.message.answer("Error interno del bot. Intenta de nuevo.")
        return True

    return router
