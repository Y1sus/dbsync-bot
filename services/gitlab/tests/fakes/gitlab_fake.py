# =============================================================================
# services/gitlab/tests/fakes/gitlab_fake.py — GitLab falso local (ThreadingHTTPServer) que registra cada petición
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

TOKEN = "glpat" "-TESTtoken_0123456789"
SHA = "ad91449c" + "0123456789abcdef" * 2


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle(self):
        fake = self.server.fake
        parts = urlsplit(self.path)
        req = {"method": self.command, "raw_path": parts.path, "query": parts.query,
               "headers": {k.lower(): v for k, v in self.headers.items()}}
        with fake.lock:
            # La comprobación de alcances del token va aparte: los recursos del proyecto se cuentan en `requests`
            (fake.scope_requests if parts.path == "/api/v4/personal_access_tokens/self" else fake.requests).append(req)
        if self.command != "GET":
            return self.reply(405, b'{"message":"405 Method Not Allowed"}')
        custom = fake.responder(self, req) if fake.responder else None
        if custom is not None:
            return
        if req["headers"].get("private-token") != TOKEN:
            return self.reply(401, b'{"message":"401 Unauthorized"}')
        segs = parts.path.split("/")
        if parts.path == "/api/v4/personal_access_tokens/self":
            return self.reply_json(200, {"name": "dbsync-bot", "scopes": list(fake.token_scopes), "active": True})
        if len(segs) < 7 or segs[:3] != ["", "api", "v4"] or segs[3] != "projects":
            return self.reply(404, b'{"message":"404 Not Found"}')
        project, kind = unquote(segs[4]), segs[6]
        if project not in fake.projects:
            return self.reply(404, b'{"message":"404 Project Not Found"}')
        proj = fake.projects[project]
        if kind == "commits":
            ref = unquote(segs[7])
            sha = proj["commits"].get(ref)
            if sha is None:
                return self.reply(404, b'{"message":"404 Commit Not Found"}')
            return self.reply_json(200, {"id": sha})
        if kind == "archive.zip" or kind == "archive.tar.gz":
            body = proj["archives"][kind.split(".", 1)[1]]
            return self.reply(200, body, {"Content-Disposition": 'attachment; filename="../../etc/evil.zip"'},
                              mode=fake.archive_mode)
        if kind in ("branches", "tags"):
            q = dict(p.split("=", 1) for p in parts.query.split("&") if "=" in p)
            page, per = int(q.get("page", "1")), int(q.get("per_page", "100"))
            items = proj[kind]
            if "search" in q:
                items = [i for i in items if unquote(q["search"]) in i["name"]]
            nxt = str(page + 1) if page * per < len(items) else ""
            return self.reply_json(200, items[(page - 1) * per: page * per], {"X-Next-Page": nxt})
        return self.reply(404, b"{}")

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

    def reply_json(self, status, obj, headers=None):
        self.reply(status, json.dumps(obj).encode(), headers)

    def reply(self, status, body, headers=None, mode="length"):
        self.send_response(status)
        self.send_header("Content-Type", "application/json" if status != 200 or mode == "length" else "application/zip")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if mode == "length":
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif mode == "chunked":
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(body), 65536):
                chunk = body[i:i + 65536]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
        self.wfile.flush()

    def redirect(self, location, status=302):
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


class FakeGitLab:
    def __init__(self):
        self.requests = []
        self.scope_requests = []
        self.lock = threading.Lock()
        self.projects = {}
        self.responder = None
        self.archive_mode = "chunked"
        self.token_scopes = ["read_api"]
        self.release = threading.Event()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.fake = self
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.001}, daemon=True)
        self.thread.start()

    def reset(self):
        with self.lock:
            self.requests.clear()
            self.scope_requests.clear()
        self.responder = None
        self.archive_mode = "chunked"
        self.token_scopes = ["read_api"]
        self.release.clear()
        self.projects = {}

    def add_project(self, path, commits=None, archives=None, branches=(), tags=()):
        self.projects[path] = {"commits": commits or {}, "archives": archives or {},
                               "branches": list(branches), "tags": list(tags)}

    def paths(self):
        """Rutas pedidas a los recursos del proyecto (sin la comprobación de alcances del token)."""
        return [r["raw_path"] + ("?" + r["query"] if r["query"] else "") for r in self.requests]

    def methods(self):
        """Métodos HTTP de TODAS las peticiones recibidas (proyecto y comprobación de alcances)."""
        return [r["method"] for r in self.requests + self.scope_requests]

    def close(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()
