# =============================================================================
# services/bot/tests/fakes/gateway.py — Gateway falso en proceso que implementa contracts/gateway-api.md
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import re
import secrets
from datetime import datetime, timedelta, timezone

from aiohttp import web

TOKEN = "gw-test-token"
ISO = "%Y-%m-%dT%H:%M:%SZ"


def _err(status, code, message):
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


class FakeGateway:
    def __init__(self, token=TOKEN, existing_files=None):
        self.token = token
        self.links = {}
        self.requests = []
        self.mode = "ok"  # ok | error500 | slow
        self.existing_files = existing_files  # None = todos existen
        self.app = web.Application(middlewares=[self._mw])
        self.app.router.add_post("/v1/links", self.create)
        self.app.router.add_get("/v1/links", self.list)
        self.app.router.add_delete("/v1/links/{id}", self.delete)
        self.app.router.add_get("/v1/health", self.health)

    @web.middleware
    async def _mw(self, request, handler):
        body = None
        if request.can_read_body and request.content_type == "application/json":
            body = await request.json()
        self.requests.append({"method": request.method, "path": request.path, "auth": request.headers.get("Authorization"), "body": body})
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return _err(401, "unauthorized", "token inválido")
        if self.mode == "error500":
            return _err(500, "internal", "boom")
        if self.mode == "slow":

            await asyncio.sleep(5)
        return await handler(request)

    async def create(self, request):
        b = await request.json()
        if not isinstance(b, dict):
            return _err(400, "invalid_request", "cuerpo inválido")
        allowed = {"relpath", "sha256", "ttl_seconds", "max_downloads", "created_by", "job_id", "label"}
        if set(b) - allowed or not all(isinstance(b.get(k), str) for k in ("relpath", "created_by", "job_id")):
            return _err(400, "invalid_request", "campos inválidos")
        rel = b["relpath"]
        if rel.startswith("/") or ".." in rel or not rel.endswith(".sql.gz"):
            return _err(400, "invalid_request", "relpath inválido")
        ttl, mx = b.get("ttl_seconds", 3600), b.get("max_downloads", 1)
        if not (isinstance(ttl, int) and 60 <= ttl <= 604800 and isinstance(mx, int) and 1 <= mx <= 20):
            return _err(400, "invalid_request", "ttl o max fuera de rango")
        if b.get("sha256") is not None and not re.fullmatch(r"[0-9a-f]{64}", b["sha256"]):
            return _err(400, "invalid_request", "sha256 inválido")
        if self.existing_files is not None and rel not in self.existing_files:
            return _err(404, "file_not_found", "no existe")
        now = datetime.now(timezone.utc)
        lid = secrets.token_hex(6)
        rec = {
            "id": lid, "filename": rel.split("/")[-1], "relpath": rel, "size_bytes": 1234567,
            "sha256": b.get("sha256"), "created_at": now.strftime(ISO),
            "expires_at": (now + timedelta(seconds=ttl)).strftime(ISO), "max_downloads": mx,
            "downloads_completed": 0, "state": "active", "created_by": b["created_by"],
            "job_id": b["job_id"], "label": b.get("label"), "download_in_progress": False,
        }
        self.links[lid] = rec
        return web.json_response({**rec, "url": f"https://dumps.test/d/{secrets.token_urlsafe(24)}"}, status=201)

    async def list(self, request):
        state = request.query.get("state", "active")
        items = [r for r in self.links.values() if state == "all" or r["state"] == "active"]
        return web.json_response({"links": items})

    async def delete(self, request):
        rec = self.links.get(request.match_info["id"])
        if rec is None:
            return _err(404, "not_found", "no existe")
        rec["state"] = "revoked"
        return web.Response(status=204)

    async def health(self, request):
        return web.json_response({"ok": True, "links_active": len(self.links), "disk_free_mb": 100})
