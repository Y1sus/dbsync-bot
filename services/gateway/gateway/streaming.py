# =============================================================================
# services/gateway/gateway/streaming.py — Entrega por bloques con Range y conteo del último byte
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


def safe_filename(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".") or "dump.sql.gz"
    return cleaned


async def send(request, ctx, link, session_id, fd, size, rng):
    ip, ua = client_info(request)
    start, end = rng if rng else (0, size - 1)
    length = end - start + 1
    resp = web.StreamResponse(status=206 if rng else 200)
    resp.content_length = length
    resp.content_type = "application/octet-stream"
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_filename(link["filename"])}"'
    resp.headers["Accept-Ranges"] = "bytes"
    if rng:
        resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    await resp.prepare(request)
    if request.method == "HEAD":
        await resp.write_eof()
        return resp

    link_id = link["id"]
    loop = asyncio.get_running_loop()
    ctx.active.enter(link_id)
    ctx.audit.log("download_started", link_id, ip, ua, length, range=f"{start}-{end}")
    sent = 0
    try:
        while sent < length:
            want = min(ctx.config.block_size, length - sent)
            chunk = await loop.run_in_executor(None, os.pread, fd, want, start + sent)
            if not chunk:
                raise _Truncated()
            await resp.write(chunk)
            sent += len(chunk)
        await resp.write_eof()
    except asyncio.CancelledError:
        ctx.audit.log("download_aborted", link_id, ip, ua, sent)
        raise
    except (OSError, _Truncated) as exc:
        ctx.audit.log("download_aborted", link_id, ip, ua, sent, reason=type(exc).__name__)
        if request.transport is not None:
            request.transport.close()
        return resp
    finally:
        ctx.active.exit(link_id)

    transport = request.transport
    if end == size - 1 and transport is not None and not transport.is_closing():
        if await ctx.db(ctx.store.mark_counted, session_id, ctx.now()):
            ctx.audit.log("download_completed", link_id, ip, ua, size)
    return resp
