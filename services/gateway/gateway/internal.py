# =============================================================================
# services/gateway/gateway/internal.py — API interna v2 (bearer): enlaces de 1 a 4 archivos
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import hmac
import logging
import re
import shutil

from aiohttp import web

from .context import Context
from . import cleanup, paths, tokens
from .audit import client_info, iso_utc

log = logging.getLogger("gateway.internal")

CTX = web.AppKey("ctx", Context)

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_FIELDS = {"files", "ttl_seconds", "max_downloads", "created_by", "job_id", "label"}
FILE_FIELDS = {"relpath", "sha256"}
MAX_FILES = 4


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _error(status, code, message, headers=None):
    return web.json_response({"error": {"code": code, "message": message}}, status=status, headers=headers)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _text(body, name, max_len, required=False):
    value = body.get(name)
    if value is None:
        if required:
            raise ApiError(400, "invalid_request", f"Falta el campo {name}")
        return None
    if not isinstance(value, str) or len(value) > max_len or (required and not value):
        raise ApiError(400, "invalid_request", f"{name} debe ser texto de hasta {max_len} caracteres")
    return value


def _integer(body, name, default, lo, hi):
    value = body.get(name, default)
    if not _is_int(value) or not lo <= value <= hi:
        raise ApiError(400, "invalid_request", f"{name} debe ser un entero entre {lo} y {hi}")
    return value


def _file_specs(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_FILES:
        raise ApiError(400, "invalid_request", f"files debe ser una lista de 1 a {MAX_FILES} elementos")
    specs = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - FILE_FIELDS:
            raise ApiError(400, "invalid_request", "Cada archivo admite solo relpath y sha256")
        relpath = _text(item, "relpath", 1024, required=True)
        sha = item.get("sha256")
        if sha is not None and (not isinstance(sha, str) or not SHA_RE.match(sha)):
            raise ApiError(400, "invalid_request", "sha256 debe ser hexadecimal en minúsculas de 64 caracteres")
        try:
            paths.validate_relpath(relpath)
        except paths.PathError as exc:
            raise ApiError(400, exc.code, exc.message) from None
        specs.append((relpath, sha, paths.kind_of(relpath)))
    if len({spec[0] for spec in specs}) != len(specs):
        raise ApiError(400, "invalid_request", "Los relpath de un enlace deben ser distintos")
    return specs


def validate_create(body):
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_request", "El cuerpo debe ser un objeto JSON")
    extra = set(body) - ALLOWED_FIELDS
    if extra:
        raise ApiError(400, "invalid_request", f"Campos no permitidos: {', '.join(sorted(extra))}")
    if "files" not in body:
        raise ApiError(400, "invalid_request", "Falta el campo files")
    return {
        "specs": _file_specs(body["files"]),
        "ttl": _integer(body, "ttl_seconds", 3600, 60, 604800),
        "max_downloads": _integer(body, "max_downloads", 1, 1, 20),
        "created_by": _text(body, "created_by", 64, required=True),
        "job_id": _text(body, "job_id", 64, required=True),
        "label": _text(body, "label", 120),
    }


def record(link, ctx):
    return {
        "id": link["id"],
        "files": [
            {
                "index": item["idx"],
                "filename": item["filename"],
                "relpath": item["relpath"],
                "kind": item["kind"],
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
                "downloads_completed": item["downloads_completed"],
                "state": item["state"],
            }
            for item in link["files"]
        ],
        "created_at": iso_utc(link["created_at"]),
        "expires_at": iso_utc(link["expires_at"]),
        "max_downloads": link["max_downloads"],
        "state": link["state"],
        "created_by": link["created_by"],
        "job_id": link["job_id"],
        "label": link["label"],
        "download_in_progress": ctx.active.busy(link["id"]),
    }


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except ApiError as exc:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return _error(exc.status, exc.code, exc.message, headers)
    except web.HTTPException as exc:
        code = "not_found" if exc.status == 404 else "invalid_request"
        return _error(exc.status, code, "Ruta o método no válido")
    except Exception:
        log.exception("Error interno en la API interna")
        return _error(500, "internal", "Error interno del gateway")


@web.middleware
async def auth(request, handler):
    ctx = request.app[CTX]
    header = request.headers.get("Authorization", "")
    scheme, _, supplied = header.partition(" ")
    try:
        ok = scheme.lower() == "bearer" and hmac.compare_digest(
            supplied.strip().encode("utf-8", "surrogateescape"), ctx.api_token.encode("utf-8")
        )
    except UnicodeError:
        ok = False
    if not ok:
        raise ApiError(401, "unauthorized", "Token ausente o incorrecto")
    return await handler(request)


async def create_link(request):
    ctx = request.app[CTX]
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "invalid_request", "El cuerpo no es JSON válido") from None
    params = validate_create(body)

    def validator():
        sizes = []
        for relpath, _, _ in params["specs"]:
            try:
                size = paths.resolve_dump(ctx.config.dumps_root, relpath)[1]
            except paths.PathError as exc:
                status = 404 if exc.code == "file_not_found" else 400
                raise ApiError(status, exc.code, exc.message) from None
            if size < 1:
                raise ApiError(400, "invalid_request", "El archivo está vacío")
            sizes.append(size)
        return sizes

    def create():
        token = tokens.new_token()
        link = ctx.store.create_link(
            validator, tokens.hash_token(token), params["specs"], params["ttl"], params["max_downloads"],
            params["created_by"], params["job_id"], params["label"], ctx.now(),
        )
        return token, link

    token, link = await ctx.db(create)
    ip, ua = client_info(request)
    ctx.audit.log("link_created", link["id"], ip, ua, sum(f["size_bytes"] for f in link["files"]),
                  files=len(link["files"]))
    data = record(link, ctx)
    data["url"] = f"{ctx.config.public_base_url}/d/{token}"
    return web.json_response(data, status=201)


async def list_links(request):
    ctx = request.app[CTX]
    state = request.query.get("state", "active")
    if state not in ("active", "all"):
        raise ApiError(400, "invalid_request", "state debe ser active o all")
    links = await ctx.db(ctx.store.list_links, ctx.now(), state == "active")
    return web.json_response({"links": [record(item, ctx) for item in links]})


def _link_id(request):
    link_id = request.match_info["id"]
    if not tokens.LINK_ID_RE.match(link_id):
        raise ApiError(404, "not_found", "Enlace no encontrado")
    return link_id


async def get_link(request):
    ctx = request.app[CTX]
    link = await ctx.db(ctx.store.get_link, _link_id(request), ctx.now())
    if link is None:
        raise ApiError(404, "not_found", "Enlace no encontrado")
    return web.json_response(record(link, ctx))


async def revoke_link(request):
    ctx = request.app[CTX]
    now = ctx.now()
    keep = request.query.get("keep_file") == "1"
    link, changed = await ctx.db(ctx.store.revoke, _link_id(request), now, keep)
    if link is None:
        raise ApiError(404, "not_found", "Enlace no encontrado")
    if ctx.active.busy(link["id"]):
        ctx.revoked.add(link["id"])
    if changed:
        ip, ua = client_info(request)
        ctx.audit.log("link_revoked", link["id"], ip, ua)
    if not keep:
        for item in link["files"]:
            await ctx.db(cleanup.delete_finished, ctx, item["relpath"], now, "link_revoked")
    return web.Response(status=204)


async def health(request):
    ctx = request.app[CTX]
    active = await ctx.db(ctx.store.count_active, ctx.now())
    free = shutil.disk_usage(ctx.config.dumps_root).free // (1024 * 1024)
    return web.json_response({"ok": True, "links_active": active, "disk_free_mb": free})


async def _server_header(request, response):
    response.headers["Server"] = "gateway"


def create_app(ctx):
    app = web.Application(middlewares=[errors, auth], client_max_size=16 * 1024)
    app[CTX] = ctx
    app.on_response_prepare.append(_server_header)
    app.router.add_post("/v2/links", create_link)
    app.router.add_get("/v2/links", list_links)
    app.router.add_get("/v2/links/{id}", get_link)
    app.router.add_delete("/v2/links/{id}", revoke_link)
    app.router.add_get("/v2/health", health)
    return app
