# =============================================================================
# services/bot/bot/notify.py — Reporta un job: progreso editado y aviso final como mensaje nuevo con push
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import logging

from . import flow

log = logging.getLogger("bot.notify")
CANCEL_NOTICE_TIMEOUT = 3
RETRY_DELAYS = (1.0, 3.0)


async def _attempt(coro, what):
    try:
        await coro
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("No se pudo %s (%s)", what, type(exc).__name__)
        return False


async def _send_with_retry(sink, view):
    """Un fallo de red no debe perder el único aviso con el enlace: se reintenta con espera."""
    for delay in (0.0, *RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        if await _attempt(sink.send(view.text, view.keyboard), "enviar el aviso final"):
            return True
    return False


async def _finish(sink, kind, req, view):
    """Aviso nuevo (genera push) y progreso editado a un estado corto, sin enlace ni detalle aunque el aviso falle."""
    sent = await _send_with_retry(sink, view)
    await _attempt(sink.edit(flow.fmt_progress_done(kind, req, sent)), "editar el progreso")


async def report_job(jobs, req, job_id, sink, now=None):
    """Corre el job y avisa. `sink.edit(text, keyboard=None)` edita el progreso; `sink.send(text, keyboard)` envía un mensaje nuevo."""

    async def queued(position):
        await sink.edit(flow.fmt_queued(position))

    async def started():
        await sink.edit(flow.fmt_started(req))

    try:
        outcome = await jobs.run(req, job_id, on_queued=queued, on_start=started)
    except asyncio.CancelledError:
        view = flow.fmt_job_cancelled(req)
        with_timeout = asyncio.wait_for(_finish(sink, "cancelled", req, view), CANCEL_NOTICE_TIMEOUT)
        await _attempt(with_timeout, "avisar la cancelación")
        raise
    if outcome.kind == "ok":
        view = flow.fmt_job_success(outcome, req, now)
    elif outcome.kind == "link_failed":
        view = flow.fmt_job_link_failure(outcome)
    else:
        view = flow.fmt_job_failure(outcome, req)
    kind = "cancelled" if outcome.code == "E_CANCELLED" else outcome.kind
    await _finish(sink, kind, req, view)
    return outcome
