# =============================================================================
# services/bot/bot/gateway_client.py — Cliente HTTP de la API interna del gateway (aiohttp)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import json
import re

import aiohttp

LINK_ID_RE = re.compile(r"^[0-9a-f]{12}\Z")


class GatewayError(Exception):
    def __init__(self, status, code, message=""):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


KINDS = ("db", "code")


def _check_file(f):
    ok = (
        isinstance(f, dict) and isinstance(f.get("index"), int) and not isinstance(f["index"], bool)
        and 0 <= f["index"] <= 3 and isinstance(f.get("filename"), str) and f.get("kind") in KINDS
        and isinstance(f.get("size_bytes"), int) and not isinstance(f["size_bytes"], bool)
        and isinstance(f.get("downloads_completed"), int) and not isinstance(f["downloads_completed"], bool)
    )
    if not ok:
        raise GatewayError(None, "bad_response", "Respuesta del gateway inválida.")


def _check_record(rec, need_url=False):
    if not isinstance(rec, dict) or not isinstance(rec.get("id"), str) or not LINK_ID_RE.match(rec["id"]):
        raise GatewayError(None, "bad_response", "Respuesta del gateway inválida.")
    files = rec.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 4:
        raise GatewayError(None, "bad_response", "Respuesta del gateway inválida.")
    for f in files:
        _check_file(f)
    if need_url and not isinstance(rec.get("url"), str):
        raise GatewayError(None, "bad_response", "El gateway no devolvió la URL.")
    return rec


class GatewayClient:
    def __init__(self, base_url, token, timeout=15, redactor=str):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout, connect=5)
        self._redact = redactor
        self._session = None

    def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        return self._session

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _request(self, method, path, ok_statuses, **kwargs):
        try:
            async with self._get_session().request(method, self.base_url + path, **kwargs) as resp:
                raw = await resp.read()
                status = resp.status
                ctype = resp.content_type
        except asyncio.TimeoutError:
            raise GatewayError(None, "timeout", "El gateway no respondió a tiempo.") from None
        except aiohttp.ClientError as exc:
            raise GatewayError(None, "unreachable", self._redact(f"No se pudo conectar con el gateway ({type(exc).__name__}).")) from None
        data = None
        if raw and ctype == "application/json":
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
        if status not in ok_statuses:
            err = data.get("error") if isinstance(data, dict) else None
            code = err.get("code") if isinstance(err, dict) and isinstance(err.get("code"), str) else f"http_{status}"
            msg = err.get("message") if isinstance(err, dict) and isinstance(err.get("message"), str) else ""
            raise GatewayError(status, code, self._redact(msg))
        return data

    async def create_link(self, files, created_by, job_id, ttl_seconds, max_downloads, label=None):
        """files: lista de {relpath, sha256} (1 a 4). max_downloads aplica a cada archivo."""
        body = {
            "files": [{"relpath": f["relpath"], "sha256": f["sha256"]} for f in files],
            "created_by": str(created_by), "job_id": job_id,
            "ttl_seconds": ttl_seconds, "max_downloads": max_downloads,
        }
        if label:
            body["label"] = label[:120]
        return _check_record(await self._request("POST", "/v2/links", (201,), json=body), need_url=True)

    async def list_links(self, state="active"):
        data = await self._request("GET", "/v2/links", (200,), params={"state": state})
        links = data.get("links") if isinstance(data, dict) else None
        if not isinstance(links, list):
            raise GatewayError(None, "bad_response", "Respuesta del gateway inválida.")
        return [_check_record(r) for r in links]

    async def revoke(self, link_id, keep_file=False):
        """Revoca el enlace; por defecto el gateway también borra el dump (keep_file=True lo conserva)."""
        if not LINK_ID_RE.match(link_id):
            raise GatewayError(None, "invalid_request", "Id de enlace inválido.")
        params = {"keep_file": "1"} if keep_file else None
        await self._request("DELETE", f"/v2/links/{link_id}", (204,), params=params)

    async def health(self):
        return await self._request("GET", "/v2/health", (200,))
