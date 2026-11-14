# =============================================================================
# services/gitlab/tests/test_client.py — Pruebas del cliente HTTP: solo GET, errores, reintentos, redirecciones y secreto del token
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import time

from gitlab_fetch.client import GitLabClient
from gitlab_fetch.errors import FetchError, ReadOnlyError
from tests.fakes.gitlab_fake import SHA, TOKEN, FakeGitLab
from tests.support import FakeCase

SECRET_PARTS = (TOKEN, "TESTtoken_0123456789")
DEMO = "/api/v4/projects/grp%2Fdemo/repository/commits/develop"


class ClientTests(FakeCase):
    def client(self, fake=None, token=TOKEN, **kw):
        sleeps = kw.pop("sleeps", [])
        return GitLabClient((fake or self.fake).url, token, kw.pop("read_timeout", 5), 30,
                            sleep=sleeps.append, **kw), sleeps

    def test_solo_get(self):
        client, _ = self.client()
        for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "get"):
            with self.assertRaises(ReadOnlyError):
                client.request(method, DEMO)
        self.assertEqual(self.fake.requests, [])
        for name in ("post", "put", "delete", "patch"):
            self.assertFalse(hasattr(client, name))

    def test_get_envia_token_solo_en_cabecera(self):
        client, _ = self.client()
        self.assertEqual(client.get_json(DEMO), {"id": SHA})
        req = self.fake.requests[0]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["headers"]["private-token"], TOKEN)
        self.assertNotIn("glpat", req["raw_path"] + req["query"])

    def test_codigos_de_error(self):
        client, _ = self.client(token="glpat" "-otro")
        with self.assertRaises(FetchError) as cm:
            client.get_json(DEMO)
        self.assertEqual(cm.exception.code, "E_AUTH")
        client, _ = self.client()
        for status, code in ((403, "E_FORBIDDEN"), (404, "E_NOT_FOUND"), (400, "E_GITLAB"), (422, "E_GITLAB")):
            self.fake.responder = lambda h, r, s=status: h.reply(s, b'{"message":"x"}') or True
            with self.assertRaises(FetchError) as cm:
                client.get_json(DEMO)
            self.assertEqual(cm.exception.code, code)

    def test_429_con_reintento_exitoso(self):
        calls = []

        def responder(h, req):
            calls.append(1)
            if len(calls) == 1:
                h.send_response(429)
                h.send_header("Retry-After", "7")
                h.send_header("Content-Length", "0")
                h.end_headers()
                return True
        self.fake.responder = responder
        client, sleeps = self.client()
        self.assertEqual(client.get_json(DEMO), {"id": SHA})
        self.assertEqual(sleeps, [7.0])

    def test_429_persistente_y_tope_de_espera(self):
        def responder(h, req):
            h.send_response(429)
            h.send_header("Retry-After", "9999")
            h.send_header("Content-Length", "0")
            h.end_headers()
            return True
        self.fake.responder = responder
        client, sleeps = self.client()
        with self.assertRaises(FetchError) as cm:
            client.get_json(DEMO)
        self.assertEqual(cm.exception.code, "E_RATE_LIMITED")
        self.assertEqual(sleeps, [60.0])
        self.assertEqual(len(self.fake.requests), 2)

    def test_5xx_reintenta_dos_veces(self):
        calls = []

        def responder(h, req):
            calls.append(1)
            if len(calls) < 3:
                return h.reply(503, b"{}") or True
        self.fake.responder = responder
        client, sleeps = self.client()
        self.assertEqual(client.get_json(DEMO), {"id": SHA})
        self.assertEqual(len(sleeps), 2)
        self.assertLess(sleeps[0], sleeps[1])

    def test_5xx_persistente(self):
        self.fake.responder = lambda h, r: h.reply(502, b"{}") or True
        client, sleeps = self.client()
        with self.assertRaises(FetchError) as cm:
            client.get_json(DEMO)
        self.assertEqual(cm.exception.code, "E_GITLAB")
        self.assertEqual(len(self.fake.requests), 3)

    def test_red_caida(self):
        other = FakeGitLab()
        url = other.url
        other.close()
        client = GitLabClient(url, TOKEN, 1, 5, sleep=lambda s: None)
        with self.assertRaises(FetchError) as cm:
            client.get_json(DEMO)
        self.assertEqual(cm.exception.code, "E_NETWORK")

    def test_timeout_de_lectura(self):
        def responder(h, req):
            self.fake.release.wait(3)
            return True
        self.fake.responder = responder
        client, _ = self.client(read_timeout=0.03)
        started = time.monotonic()
        with self.assertRaises(FetchError) as cm:
            client.get_json(DEMO)
        self.assertEqual(cm.exception.code, "E_NETWORK")
        self.assertLess(time.monotonic() - started, 1)
        self.fake.release.set()

    def test_tiempo_total_agotado(self):
        now = [0.0]
        client = GitLabClient(self.fake.url, TOKEN, 5, 10, sleep=lambda s: None, clock=lambda: now[0])
        now[0] = 11.0
        with self.assertRaises(FetchError) as cm:
            client.get_json(DEMO)
        self.assertEqual(cm.exception.code, "E_NETWORK")
        self.assertEqual(self.fake.requests, [])

    def test_redireccion_a_otro_host_no_envia_token(self):
        other = FakeGitLab()
        self.addCleanup(other.close)
        self.fake.responder = lambda h, r: h.redirect(f"{other.url}{DEMO}") or True
        client, _ = self.client()
        with self.assertRaises(FetchError) as cm:
            client.get_json(DEMO)
        self.assertEqual(cm.exception.code, "E_GITLAB")
        self.assertEqual(other.requests, [])

    def test_redireccion_a_otro_puerto_o_esquema_rechazada(self):
        for loc in ("http://127.0.0.1:1/x", "https://127.0.0.1/x", "http://localhost:%d/x" % self.fake.port):
            self.fake.responder = lambda h, r, l=loc: h.redirect(l) or True
            client, _ = self.client()
            with self.assertRaises(FetchError):
                client.get_json(DEMO)

    def test_redireccion_mismo_host_seguida_con_tope(self):
        hops = {"/start": "/elsewhere", "/elsewhere": DEMO}
        self.fake.responder = lambda h, r: h.redirect(hops[r["raw_path"]]) or True if r["raw_path"] in hops else None
        client, _ = self.client()
        conn, resp = client.get("/start")
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertTrue(all(r["headers"].get("private-token") == TOKEN for r in self.fake.requests))
        self.fake.responder = lambda h, r: h.redirect(DEMO) or True
        with self.assertRaises(FetchError) as cm:
            self.client()[0].get_json(DEMO)
        self.assertEqual(cm.exception.code, "E_GITLAB")

    def test_token_redactado_en_cuerpo_de_error(self):
        body = ('{"message":"fallo con %s y glpat" "-OTROSECRETO123"}' % TOKEN).encode()
        self.fake.responder = lambda h, r: h.reply(400, body) or True
        client, _ = self.client()
        with self.assertRaises(FetchError) as cm:
            client.get_json(DEMO)
        from gitlab_fetch import output
        output.register_secret(TOKEN)
        text = output.redact(cm.exception.message)
        for part in SECRET_PARTS + ("OTROSECRETO",):
            self.assertNotIn(part, cm.exception.message)
            self.assertNotIn(part, text)

    def test_respuesta_no_json(self):
        self.fake.responder = lambda h, r: h.reply(200, b"<html>") or True
        with self.assertRaises(FetchError) as cm:
            self.client()[0].get_json(DEMO)
        self.assertEqual(cm.exception.code, "E_GITLAB")
