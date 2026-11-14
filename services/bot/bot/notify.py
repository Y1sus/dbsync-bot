# =============================================================================
# services/bot/bot/notify.py — Reporta un job: progreso editado y aviso final como mensaje nuevo con push
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import logging

from . import flow
from .packages import PackageOutcome

log = logging.getLogger("bot.notify")
CANCEL_NOTICE_TIMEOUT = 2
RETRY_DELAYS = (1, 3)
CANCEL_RETRY_DELAYS = (0.3, 0.6)


async def _attempt(coro, what):
    try:
        await coro
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("No se pudo %s (%s)", what, type(exc).__name__)
        return False


async def _send_with_retries(sink, view, delays):
    """Un intento más un reintento por cada espera de `delays`; el aviso con el enlace es único."""
    for attempt in range(len(delays) + 1):
        if attempt:
            await asyncio.sleep(delays[attempt - 1])
        if await _attempt(sink.send(view.text, view.keyboard), "enviar el aviso final"):
            return True
    return False


async def _finish(sink, kind, req, view, delays):
    """Aviso nuevo (genera push) y progreso editado a un estado corto, sin enlace ni detalle aunque el aviso falle."""
    sent = await _send_with_retries(sink, view, delays)
    await _attempt(sink.edit(flow.fmt_progress_done(kind, req, sent)), "editar el progreso")


def _view_for(outcome, req, now):
    if isinstance(outcome, PackageOutcome):
        return flow.fmt_package_result(outcome, req, now), outcome.kind
    if outcome.kind == "ok":
        view = flow.fmt_job_success(outcome, req, now)
    elif outcome.kind == "link_failed":
        view = flow.fmt_job_link_failure(outcome)
    else:
        view = flow.fmt_job_failure(outcome, req)
    return view, ("cancelled" if outcome.code == "E_CANCELLED" else outcome.kind)


async def report_job(jobs, req, job_id, sink, now=None, delays=None):
    """Corre el job y avisa. `sink.edit(text, keyboard=None)` edita el progreso; `sink.send(text, keyboard)` envía un mensaje nuevo."""

    delays = RETRY_DELAYS if delays is None else delays

    async def queued(position):
        await sink.edit(flow.fmt_queued(position))

    async def started():
        await sink.edit(flow.fmt_started(req))

    try:
        outcome = await jobs.run(req, job_id, on_queued=queued, on_start=started)
    except asyncio.CancelledError:
        cancelled = _finish(sink, "cancelled", req, flow.fmt_job_cancelled(req), CANCEL_RETRY_DELAYS if delays else ())
        await _attempt(asyncio.wait_for(cancelled, CANCEL_NOTICE_TIMEOUT), "avisar la cancelación")
        raise
    view, kind = _view_for(outcome, req, now)
    await _finish(sink, kind, req, view, delays)
    return outcome
