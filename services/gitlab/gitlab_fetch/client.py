# =============================================================================
# services/gitlab/gitlab_fetch/client.py — Cliente HTTP de GitLab: solo GET, sin salir del host, con reintentos y mapeo de errores
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import http.client
import json
import ssl
import time
from urllib.parse import urljoin, urlsplit

from .errors import FetchError, ReadOnlyError
from .output import redact

MAX_REDIRECTS = 3
MAX_RETRY_AFTER = 60.0
RETRIES_5XX = 2
API_PREFIX = "/api/v4"
MAX_JSON_BYTES = 2 * 1024 * 1024
READ_BLOCK = 64 * 1024
DETAIL_BYTES = 4096


def declared_length(resp):
    """Content-Length único y ASCII, o None si no viene; cualquier otra forma es respuesta inválida."""
    values = resp.headers.get_all("Content-Length") or []
    if not values:
        return None
    value = values[0].strip()
    if len(values) > 1 or not (value.isascii() and value.isdigit()):
        raise FetchError("E_GITLAB", "GitLab envió un Content-Length inválido o duplicado")
    return int(value)


def _origin(parts):
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, (parts.hostname or "").lower(), port


class GitLabClient:
    def __init__(self, base_url, token, read_timeout=30.0, total_timeout=300.0,
                 sleep=time.sleep, clock=time.monotonic):
        self._base = urlsplit(base_url)
        self._origin = _origin(self._base)
        self._prefix = self._base.path.rstrip("/") + API_PREFIX
        self._token = token
        self._read_timeout = read_timeout
        self._sleep = sleep
        self._clock = clock
        self._deadline = clock() + total_timeout

    def check_deadline(self):
        if self._clock() > self._deadline:
            raise FetchError("E_NETWORK", "Se agotó el tiempo total de la operación con GitLab")

    def api_target(self, path, query=""):
        return f"{self._prefix}{path}" + (f"?{query}" if query else "")

    def request(self, method, target):
        if method != "GET":
            raise ReadOnlyError(method)
        return self.get(target)

    def get(self, target):
        """Devuelve (conexión, respuesta 2xx); el llamador cierra la conexión."""
        redirects = rate_waited = retries = 0
        scheme, host, port = self._origin
        while True:
            self.check_deadline()
            timeout = max(0.001, min(self._read_timeout, self._deadline - self._clock()))
            conn = self._connect(scheme, host, port, timeout)
            try:
                conn.request("GET", target, headers={
                    "PRIVATE-TOKEN": self._token,
                    "Accept": "application/json, */*",
                    "Accept-Encoding": "identity",
                    "User-Agent": "dbsync-bot-gitlab/1",
                })
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                raise FetchError("E_NETWORK", f"No se pudo contactar a GitLab ({type(exc).__name__})")
            status = resp.status
            if 200 <= status < 300:
                try:
                    declared_length(resp)
                except FetchError:
                    conn.close()
                    raise
                return conn, resp
            if 300 <= status < 400:
                location = resp.getheader("Location")
                conn.close()
                redirects += 1
                target = self._follow(target, location, redirects)
                continue
            detail = self._detail(resp)
            retry_after = resp.getheader("Retry-After")
            conn.close()
            if status == 429:
                if rate_waited:
                    raise FetchError("E_RATE_LIMITED", "GitLab limitó las peticiones (429)")
                rate_waited = 1
                self._sleep(self._parse_wait(retry_after))
                continue
            if status >= 500:
                if retries >= RETRIES_5XX:
                    raise FetchError("E_GITLAB", f"GitLab respondió {status}{detail}")
                retries += 1
                self._sleep(2.0 * retries)
                continue
            raise self._map_error(status, detail)

    def get_json(self, target):
        return self.get_json_headers(target)[0]

    def get_json_headers(self, target):
        conn, resp = self.get(target)
        try:
            length = declared_length(resp)
            if length is not None and length > MAX_JSON_BYTES:
                raise FetchError("E_GITLAB", "GitLab devolvió una respuesta demasiado grande")
            return json.loads(self.read_limited(resp, MAX_JSON_BYTES).decode("utf-8")), resp.headers
        except (OSError, http.client.HTTPException):
            raise FetchError("E_NETWORK", "Se cortó la conexión con GitLab")
        except ValueError:
            raise FetchError("E_GITLAB", "GitLab devolvió una respuesta que no es JSON")
        finally:
            conn.close()

    def read_limited(self, resp, limit, strict=True):
        """Lee el cuerpo en bloques revisando el plazo; supera `limit`: error (strict) o se recorta."""
        chunks, total = [], 0
        while True:
            self.check_deadline()
            block = resp.read1(min(READ_BLOCK, limit - total + 1))
            if not block:
                break
            total += len(block)
            if total > limit:
                if strict:
                    raise FetchError("E_GITLAB", "GitLab devolvió una respuesta demasiado grande")
                chunks.append(block[:len(block) - (total - limit)])
                break
            chunks.append(block)
        return b"".join(chunks)

    def _connect(self, scheme, host, port, timeout):
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def _follow(self, target, location, count):
        if not location or count > MAX_REDIRECTS:
            raise FetchError("E_GITLAB", "Redirección inválida o demasiadas redirecciones de GitLab")
        current = urlsplit(f"{self._base.scheme}://{self._base.netloc}{target}")
        new = urlsplit(urljoin(current.geturl(), location))
        try:
            same = _origin(new) == self._origin
        except ValueError:
            same = False
        if not same:
            raise FetchError("E_GITLAB", "GitLab redirigió a otro host; se rechazó sin enviar el token")
        return new.path + (f"?{new.query}" if new.query else "")

    @staticmethod
    def _parse_wait(value):
        try:
            return min(MAX_RETRY_AFTER, max(0.0, float(value)))
        except (TypeError, ValueError):
            return 5.0

    def _detail(self, resp):
        try:
            body = json.loads(self.read_limited(resp, DETAIL_BYTES, strict=False).decode("utf-8", "replace"))
            msg = body.get("message") or body.get("error") if isinstance(body, dict) else None
        except (OSError, http.client.HTTPException, ValueError):
            return ""
        return f": {redact(str(msg))[:200]}" if msg else ""

    @staticmethod
    def _map_error(status, detail):
        table = {
            401: ("E_AUTH", "Token de GitLab inválido o vencido (401)"),
            403: ("E_FORBIDDEN", "El token no tiene permiso para este recurso (403)"),
            404: ("E_NOT_FOUND", "Recurso no encontrado en GitLab (404)"),
        }
        code, msg = table.get(status, ("E_GITLAB", f"GitLab respondió {status}"))
        return FetchError(code, msg + (detail if status not in table else ""))


