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
        self.deleted_files = set()
        self.mode = "ok"  # ok | error500 | slow
        self.existing_files = existing_files  # None = todos existen
        self.app = web.Application(middlewares=[self._mw])
        self.app.router.add_post("/v2/links", self.create)
        self.app.router.add_get("/v2/links", self.list)
        self.app.router.add_get("/v2/links/{id}", self.get)
        self.app.router.add_delete("/v2/links/{id}", self.delete)
        self.app.router.add_get("/v2/health", self.health)

    @web.middleware
    async def _mw(self, request, handler):
        body = None
        if request.can_read_body and request.content_type == "application/json":
            body = await request.json()
        self.requests.append({"method": request.method, "path": request.path, "query": dict(request.query), "auth": request.headers.get("Authorization"), "body": body})
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
        allowed = {"files", "ttl_seconds", "max_downloads", "created_by", "job_id", "label"}
        files = b.get("files")
        if set(b) - allowed or not all(isinstance(b.get(k), str) for k in ("created_by", "job_id")):
            return _err(400, "invalid_request", "campos inválidos")
        if not isinstance(files, list) or not 1 <= len(files) <= 4:
            return _err(400, "invalid_request", "files debe tener de 1 a 4 elementos")
        rels = [f.get("relpath") if isinstance(f, dict) else None for f in files]
        if len(set(rels)) != len(rels):
            return _err(400, "invalid_request", "relpath repetidos")
        for f, rel in zip(files, rels):
            if not isinstance(rel, str) or set(f) - {"relpath", "sha256"} or rel.startswith("/") or ".." in rel:
                return _err(400, "invalid_request", "relpath inválido")
            parts = rel.split("/")
            if len(parts) != 2 or not parts[1] or parts[1].startswith(".") or not rel.endswith((".sql.gz", ".zip", ".tar.gz")):
                return _err(400, "invalid_request", "relpath inválido")
            if f.get("sha256") is not None and not re.fullmatch(r"[0-9a-f]{64}", str(f["sha256"])):
                return _err(400, "invalid_request", "sha256 inválido")
        ttl, mx = b.get("ttl_seconds", 3600), b.get("max_downloads", 1)
        if not (isinstance(ttl, int) and 60 <= ttl <= 604800 and isinstance(mx, int) and 1 <= mx <= 20):
            return _err(400, "invalid_request", "ttl o max fuera de rango")
        if self.existing_files is not None and any(r not in self.existing_files for r in rels):
            return _err(404, "file_not_found", "no existe")
        now = datetime.now(timezone.utc)
        lid = secrets.token_hex(6)
        rec = {
            "id": lid, "created_at": now.strftime(ISO), "expires_at": (now + timedelta(seconds=ttl)).strftime(ISO),
            "max_downloads": mx, "state": "active", "created_by": b["created_by"], "job_id": b["job_id"],
            "label": b.get("label"), "download_in_progress": False,
            "files": [
                {"index": i, "filename": f["relpath"].split("/")[-1], "relpath": f["relpath"],
                 "kind": "db" if f["relpath"].endswith(".sql.gz") else "code", "size_bytes": 1234567 if f["relpath"].endswith(".sql.gz") else 654321,
                 "sha256": f.get("sha256"), "downloads_completed": 0, "state": "active"}
                for i, f in enumerate(files)
            ],
        }
        self.links[lid] = rec
        return web.json_response({**rec, "url": f"https://dumps.test/d/{secrets.token_urlsafe(24)}"}, status=201)

    async def list(self, request):
        state = request.query.get("state", "active")
        items = [r for r in self.links.values() if state == "all" or r["state"] == "active"]
        if self.mode == "nofiles":
            items = [{k: v for k, v in r.items() if k != "files"} for r in items]
        return web.json_response({"links": items})

    async def get(self, request):
        rec = self.links.get(request.match_info["id"])
        return web.json_response(rec) if rec else _err(404, "not_found", "no existe")

    async def delete(self, request):
        rec = self.links.get(request.match_info["id"])
        if rec is None:
            return _err(404, "not_found", "no existe")
        rec["state"] = "revoked"
        if request.query.get("keep_file") != "1":
            self.deleted_files.update(f["relpath"] for f in rec["files"])
        return web.Response(status=204)

    async def health(self, request):
        return web.json_response({"ok": True, "links_active": len(self.links), "disk_free_mb": 100})
