# =============================================================================
# services/gateway/gateway/public.py — Listener público: páginas, sesiones por archivo, descarga y endurecimiento
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import logging
import os
import re
from urllib.parse import urlsplit

from aiohttp import web

from .context import Context
from . import pages, paths, tokens
from .audit import client_info, iso_utc
from .ranges import RangeNotSatisfiable, parse_range
from .streaming import send

log = logging.getLogger("gateway.public")

CTX = web.AppKey("ctx", Context)

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": pages.CSP,
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Server": "gateway",
}
HSTS = "max-age=31536000"


def _page(kind, status=None, headers=None, **kwargs):
    code, doc = pages.render(kind, **kwargs)
    return web.Response(status=status or code, text=doc, content_type="text/html", charset="utf-8",
                        headers=headers)


def _not_found():
    return _page("notfound")


def _gone(reason):
    return _page(reason)


ORIGIN_RE = re.compile(r"^(https?)://([a-z0-9.-]+|\[[0-9a-f:]+\])(?::([0-9]{1,5}))?$", re.I)
DEFAULT_PORTS = {"http": 80, "https": 443}


def _normalize(origin):
    """(esquema, host, puerto) de un Origin estricto (sin ruta, usuario, barras invertidas ni espacios)."""
    match = ORIGIN_RE.match(origin)
    if match is None:
        return None
    scheme = match.group(1).lower()
    port = int(match.group(3)) if match.group(3) else DEFAULT_PORTS[scheme]
    return scheme, match.group(2).lower(), port


def _base_origin(url):
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port or DEFAULT_PORTS.get(scheme)
    except ValueError:
        return None
    return scheme, (parts.hostname or "").lower(), port


def origin_violation(headers, base_url):
    """Regla del contrato para POST. Devuelve el motivo del rechazo o None si se acepta."""
    site = headers.get("Sec-Fetch-Site")
    if site is not None and site not in ("same-origin", "none"):
        return f"sec-fetch-site={site[:40]}"
    origin = headers.get("Origin")
    if origin is not None and origin != "null":
        parsed = _normalize(origin)
        if parsed is None:
            return "origin=invalido"
        if parsed != _base_origin(base_url):
            return f"origin={parsed[1][:100]}"
    return None


def _expires_text(epoch):
    stamp = iso_utc(epoch)
    return f"{stamp[:10]} {stamp[11:16]} UTC"


async def _lookup(ctx, token):
    if not tokens.is_well_formed(token):
        return None
    link = await ctx.db(ctx.store.get_link_by_hash, tokens.hash_token(token), ctx.now())
    if link is None or not tokens.verify(token, link["token_hash"]):
        return None
    return link


def _file_index(request, link):
    """Índice del archivo pedido; la ruta sin índice solo existe para enlaces de un archivo. None si no existe."""
    raw = request.match_info.get("n")
    if raw is None:
        return 0 if len(link["files"]) == 1 else None
    index = int(raw)
    return index if index < len(link["files"]) else None


async def landing(request):
    ctx = request.app[CTX]
    token = request.match_info["token"]
    link = await _lookup(ctx, token)
    if link is None:
        return _not_found()
    if link["state"] != "active":
        return _gone(link["state"])
    if request.method == "HEAD":
        return web.Response(status=200, content_type="text/html", charset="utf-8")
    ip, ua = client_info(request)
    ctx.audit.log("landing_view", link["id"], ip, ua)
    now = ctx.now()
    root = ctx.config.dumps_root
    availability = await ctx.db(lambda: [paths.available(root, item["relpath"]) for item in link["files"]])
    items = [
        {
            "index": item["idx"], "filename": item["filename"], "kind": item["kind"],
            "size_bytes": item["size_bytes"], "sha256": item["sha256"],
            "downloads_left": link["max_downloads"] - item["downloads_completed"], "available": ok,
        }
        for item, ok in zip(link["files"], availability)
    ]
    status, doc = pages.landing(token, items, _expires_text(link["expires_at"]), link["expires_at"] - now)
    return web.Response(status=status, text=doc, content_type="text/html", charset="utf-8")


async def start_session(request):
    ctx = request.app[CTX]
    reason = origin_violation(request.headers, ctx.config.public_base_url)
    if reason:
        ip, ua = client_info(request)
        ctx.audit.log("origin_rejected", None, ip, ua, reason=reason)
        return _page("forbidden")
    token = request.match_info["token"]
    link = await _lookup(ctx, token)
    if link is None:
        return _not_found()
    index = _file_index(request, link)
    if index is None:
        return _not_found()
    status, session_id = await ctx.db(
        ctx.store.open_or_reuse_session, link["id"], index, ctx.now(), ctx.config.session_ttl,
        ctx.config.post_download_grace
    )
    if status == "not_found":
        return _not_found()
    if status == "exhausted" and len(link["files"]) > 1:
        return _gone("filedone")
    if status != "ok":
        return _gone(status)
    legacy = request.match_info.get("n") is None
    where = f"/d/{token}/f/{session_id}" if legacy else f"/d/{token}/{index}/f/{session_id}"
    return web.Response(status=303, headers={"Location": where})


def _claim(ctx, link, idx, session_id, stream):
    """Valida la sesión y reserva el stream bajo el candado de borrado (cierra la carrera con la limpieza)."""
    with ctx.store.locked():
        session = ctx.store.get_session(session_id, link["id"], idx)
        if session is None:
            current = ctx.store.get_link(link["id"], ctx.now())
            return "revoked" if current and current["state"] == "revoked" else "notfound"
        if session["expires_at"] <= ctx.now():
            return "session"
        if stream:
            cfg = ctx.config
            if ctx.active.total >= cfg.max_streams or ctx.active.count(link["id"]) >= cfg.max_streams_per_link:
                return "busy"
            ctx.active.enter(link["id"], idx)
    return "ok"


async def download(request):
    ctx = request.app[CTX]
    link = await _lookup(ctx, request.match_info["token"])
    session_id = request.match_info["session"]
    if link is None:
        return _not_found()
    idx = _file_index(request, link)
    if idx is None:
        return _not_found()
    if link["state"] == "revoked":
        return _gone("revoked")
    item = link["files"][idx]
    stream = request.method != "HEAD"
    verdict = await ctx.db(_claim, ctx, link, idx, session_id, stream)
    if verdict == "notfound":
        return _not_found()
    if verdict in ("session", "revoked"):
        return _gone(verdict)
    if verdict == "busy":
        return _page("busy", headers={"Retry-After": "30"})
    try:
        try:
            fd, size = await ctx.db(paths.open_dump, ctx.config.dumps_root, item["relpath"])
        except paths.PathError:
            return _gone("file")
        try:
            try:
                rng = parse_range(request.headers.get("Range"), size)
            except RangeNotSatisfiable:
                return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})
            if stream:
                start, end = rng if rng else (0, size - 1)
                reserved = await ctx.db(ctx.store.reserve_bytes, session_id, end - start + 1, size, ctx.now())
                if reserved != "ok":
                    return _gone("session" if reserved == "session" else "budget")
            return await send(request, ctx, link, item, session_id, fd, size, rng)
        finally:
            os.close(fd)
    finally:
        if stream:
            ctx.active.exit(link["id"], idx)
            if not ctx.active.busy(link["id"]):
                ctx.revoked.discard(link["id"])


@web.middleware
async def guard(request, handler):
    """Limitador de 404 por IP y página de error uniforme para cualquier excepción."""
    ctx = request.app[CTX]
    ip, ua = client_info(request)
    wait = ctx.limiter.blocked(ip)
    if wait:
        return _page("limited", headers={"Retry-After": str(wait)}, retry_after=wait)
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        if exc.status == 404:
            response = _not_found()
        elif exc.status == 405:
            allow = exc.headers.get("Allow")
            response = _page("method", headers={"Allow": allow} if allow else None)
        else:
            response = _page("bad", status=exc.status if 400 <= exc.status < 600 else 400)
    except Exception:
        log.exception("Error interno en el listener público")
        response = _page("error")
    if response.status == 404 and ctx.limiter.miss(ip):
        ctx.audit.log("rate_limited", None, ip, ua, block_seconds=ctx.config.block_seconds)
    return response


async def _add_headers(request, response):
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    if request.headers.get("X-Forwarded-Proto", "").strip().lower() == "https":
        response.headers["Strict-Transport-Security"] = HSTS


def create_app(ctx):
    app = web.Application(middlewares=[guard], client_max_size=1024)
    app[CTX] = ctx
    app.on_response_prepare.append(_add_headers)
    tok = r"{token:[A-Za-z0-9_-]{43}}"
    idx = r"{n:[0-3]}"
    sess = r"{session:[0-9a-f]{32}}"
    app.router.add_route("GET", f"/d/{tok}", landing)
    app.router.add_route("HEAD", f"/d/{tok}", landing)
    app.router.add_route("POST", f"/d/{tok}", start_session)
    app.router.add_route("POST", f"/d/{tok}/{idx}", start_session)
    for method in ("GET", "HEAD"):
        app.router.add_route(method, f"/d/{tok}/f/{sess}", download)
        app.router.add_route(method, f"/d/{tok}/{idx}/f/{sess}", download)
    return app
