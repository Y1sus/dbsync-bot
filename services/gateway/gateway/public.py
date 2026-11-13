# =============================================================================
# services/gateway/gateway/public.py — Listener público: página intermedia, sesiones y descarga
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import html
import logging
import os

from aiohttp import web

from .context import Context
from . import paths, tokens
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
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
}

GONE_MESSAGES = {
    "expired": "Este enlace expiró.",
    "exhausted": "Este enlace ya alcanzó su límite de descargas.",
    "revoked": "Este enlace fue revocado.",
    "session": "La sesión de descarga expiró. Abre de nuevo el enlace si sigue vigente.",
    "file": "El archivo ya no está disponible.",
}

_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;color:#222}"
    "dl{display:grid;grid-template-columns:auto 1fr;gap:.4rem 1rem}dt{font-weight:600}"
    "dd{margin:0;word-break:break-all}button{font-size:1rem;padding:.6rem 1.4rem;margin-top:1rem}"
)


def _page(status, title, body_html):
    doc = (
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main><h1>{html.escape(title)}</h1>{body_html}</main></body></html>"
    )
    return web.Response(status=status, text=doc, content_type="text/html", charset="utf-8")


def _not_found():
    return _page(404, "Enlace no encontrado", "<p>El enlace no existe o es incorrecto.</p>")


def _gone(reason):
    return _page(410, "Enlace no disponible", f"<p>{html.escape(GONE_MESSAGES[reason])}</p>")


def _human_size(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


async def _lookup(ctx, token):
    if not tokens.is_well_formed(token):
        return None
    link = await ctx.db(ctx.store.get_link_by_hash, tokens.hash_token(token), ctx.now())
    if link is None or not tokens.verify(token, link["token_hash"]):
        return None
    return link


async def landing(request):
    ctx = request.app[CTX]
    link = await _lookup(ctx, request.match_info["token"])
    if link is None:
        return _not_found()
    if link["state"] != "active":
        return _gone(link["state"])
    if request.method == "HEAD":
        return web.Response(status=200, content_type="text/html", charset="utf-8")
    ip, ua = client_info(request)
    ctx.audit.log("landing_view", link["id"], ip, ua)
    rows = [
        ("Archivo", link["filename"]),
        ("Tamaño", _human_size(link["size_bytes"])),
        ("SHA-256", link["sha256"] or "no disponible"),
        ("Expira", iso_utc(link["expires_at"]) + " (UTC)"),
        ("Descargas restantes", str(link["max_downloads"] - link["downloads_completed"])),
    ]
    items = "".join(f"<dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd>" for k, v in rows)
    body = f'<dl>{items}</dl><form method="post"><button type="submit">Descargar</button></form>'
    return _page(200, "Descarga de respaldo", body)


async def start_session(request):
    ctx = request.app[CTX]
    token = request.match_info["token"]
    link = await _lookup(ctx, token)
    if link is None:
        return _not_found()
    status, session_id = await ctx.db(
        ctx.store.open_or_reuse_session, link["id"], ctx.now(), ctx.config.session_ttl
    )
    if status == "not_found":
        return _not_found()
    if status != "ok":
        return _gone(status)
    return web.Response(status=303, headers={"Location": f"/d/{token}/f/{session_id}"})


async def download(request):
    ctx = request.app[CTX]
    link = await _lookup(ctx, request.match_info["token"])
    session_id = request.match_info["session"]
    if link is None:
        return _not_found()
    if link["state"] == "revoked":
        return _gone("revoked")
    session = await ctx.db(ctx.store.get_session, session_id, link["id"])
    if session is None:
        return _not_found()
    if session["expires_at"] <= ctx.now():
        return _gone("session")
    try:
        fd, size = await ctx.db(paths.open_dump, ctx.config.dumps_root, link["relpath"])
    except paths.PathError:
        return _gone("file")
    try:
        try:
            rng = parse_range(request.headers.get("Range"), size)
        except RangeNotSatisfiable:
            return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        return await send(request, ctx, link, session_id, fd, size, rng)
    finally:
        os.close(fd)


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except web.HTTPException as exc:
        if exc.status == 404:
            return _not_found()
        if exc.status == 405:
            return _page(405, "Método no permitido", "<p>Esta ruta no admite ese método.</p>")
        return _page(exc.status, "Solicitud no válida", "<p>No se pudo procesar la solicitud.</p>")
    except Exception:
        log.exception("Error interno en el listener público")
        return _page(500, "Error interno", "<p>Ocurrió un error. Intenta más tarde.</p>")


async def _add_headers(request, response):
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value


def create_app(ctx):
    app = web.Application(middlewares=[errors], client_max_size=1024)
    app[CTX] = ctx
    app.on_response_prepare.append(_add_headers)
    tok = r"{token:[A-Za-z0-9_-]{43}}"
    app.router.add_route("GET", f"/d/{tok}", landing)
    app.router.add_route("HEAD", f"/d/{tok}", landing)
    app.router.add_route("POST", f"/d/{tok}", start_session)
    sess = r"{session:[0-9a-f]{32}}"
    app.router.add_route("GET", f"/d/{tok}/f/{sess}", download)
    app.router.add_route("HEAD", f"/d/{tok}/f/{sess}", download)
    return app
