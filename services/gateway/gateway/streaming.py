# =============================================================================
# services/gateway/gateway/streaming.py — Entrega por bloques con Range, cortes de seguridad y conteo por cobertura de bytes
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import logging
import os
import re

from aiohttp import web

from .audit import client_info

log = logging.getLogger("gateway.streaming")


class _Truncated(Exception):
    pass


class _Abort(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def safe_filename(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".") or "dump.sql.gz"
    return cleaned


def _check(ctx, link_id, began, sent):
    """Cortes entre bloques: revocación, duración máxima y velocidad mínima."""
    if link_id in ctx.revoked:
        raise _Abort("revoked")
    cfg = ctx.config
    elapsed = ctx.clock() - began
    if elapsed > cfg.max_stream_seconds:
        raise _Abort("max_duration")
    if elapsed >= cfg.min_rate_after and sent < cfg.min_rate_bytes * elapsed:
        raise _Abort("slow_client")


async def send(request, ctx, link, item, session_id, fd, size, rng):
    ip, ua = client_info(request)
    start, end = rng if rng else (0, size - 1)
    length = end - start + 1
    resp = web.StreamResponse(status=206 if rng else 200)
    resp.content_length = length
    resp.content_type = "application/octet-stream"
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_filename(item["filename"])}"'
    resp.headers["Accept-Ranges"] = "bytes"
    if rng:
        resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if request.method == "HEAD":
        await resp.prepare(request)
        await resp.write_eof()
        return resp

    link_id = link["id"]
    tag = {"file_index": item["idx"], "kind": item["kind"]}
    loop = asyncio.get_running_loop()
    began = ctx.clock()
    sent = last = 0
    finished = False
    try:
        await resp.prepare(request)
        ctx.audit.log("download_started", link_id, ip, ua, length, range=f"{start}-{end}", **tag)
        while sent < length:
            _check(ctx, link_id, began, sent)
            want = min(ctx.config.block_size, length - sent)
            chunk = await loop.run_in_executor(None, os.pread, fd, want, start + sent)
            if not chunk:
                raise _Truncated()
            await asyncio.wait_for(resp.write(chunk), ctx.config.write_timeout)
            sent += len(chunk)
            last = len(chunk)
        await resp.write_eof()
        finished = True
    except asyncio.CancelledError:
        ctx.audit.log("download_aborted", link_id, ip, ua, sent, **tag)
        raise
    except (OSError, _Truncated, TimeoutError, _Abort) as exc:
        reason = exc.reason if isinstance(exc, _Abort) else type(exc).__name__
        ctx.audit.log("download_aborted", link_id, ip, ua, sent, reason=reason, **tag)
        if request.transport is not None:
            request.transport.close()
    finally:
        transport = request.transport
        credited = sent
        if finished and (transport is None or transport.is_closing()):
            credited = sent - last
        counted = await asyncio.shield(ctx.db(
            ctx.store.record_sent, session_id, start, sent, length, credited, size, ctx.now(),
            ctx.config.post_download_grace,
        ))
    if counted:
        ctx.audit.log("download_completed", link_id, ip, ua, size, **tag)
    return resp
