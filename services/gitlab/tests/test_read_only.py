# =============================================================================
# services/gitlab/tests/test_read_only.py — El servicio solo LEE de GitLab: alcances del token, métodos HTTP y código fuente
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import ast
import os
import unittest

from tests.fakes.gitlab_fake import SHA, TOKEN
from tests.support import FakeCase

PKG = os.path.join(os.path.dirname(__file__), "..", "gitlab_fetch")
SELF_PATH = "/personal_access_tokens/self"


def _scope_responder(status, body):
    def responder(handler, req):
        if req["raw_path"].endswith(SELF_PATH):
            handler.reply(status, body)
            return True
        return None
    return responder


class TokenScopeTests(FakeCase):
    """El token real puede tener alcance de escritura: el servicio se niega a usarlo."""

    def args(self):
        return ("archive", "--project", "demo", "--ref", "develop", "--job-id", "j1")

    def test_alcances_de_escritura_se_rechazan_sin_descargar_nada(self):
        for scopes in (["api"], ["read_api", "api"], ["read_api", "write_repository"], ["read_repository", "create_runner"],
                       ["read_api", "read_user"], ["sudo"], ["ai_features"]):
            with self.subTest(scopes=scopes):
                self.fake.reset()
                self.fake.add_project("grp/demo", commits={"develop": SHA}, archives={"zip": b"x"})
                self.fake.token_scopes = scopes
                code, doc, out, err = self.call(*self.args())
                self.assertEqual((code, doc["error"]["code"]), (1, "E_CONFIG"), scopes)
                self.assertIn("read_api", doc["error"]["message"])
                self.assertEqual(self.fake.requests, [], "no debe tocar ningún recurso del proyecto")
                self.assertEqual(self.e.files(), [])
                self.assertNotIn(TOKEN, out + err)

    def test_solo_alcances_de_lectura_funcionan(self):
        for scopes in (["read_api"], ["read_repository"], ["read_api", "read_repository"]):
            with self.subTest(scopes=scopes):
                self.fake.reset()
                self.fake.add_project("grp/demo", commits={"develop": SHA}, archives={"zip": self.zip_bytes()})
                self.fake.token_scopes = scopes
                code, doc, _, _ = self.call(*self.args()[:-1], "j-" + "".join(s[0] for s in scopes))
                self.assertEqual((code, doc["ok"]), (0, True), doc)

    def zip_bytes(self):
        from tests.support import make_zip
        return make_zip()

    def test_la_comprobacion_tambien_protege_ramas_y_tags(self):
        self.fake.token_scopes = ["api"]
        for cmd in ("branches", "tags"):
            code, doc, _, _ = self.call(cmd, "--project", "demo")
            self.assertEqual((code, doc["error"]["code"]), (1, "E_CONFIG"), cmd)
        self.assertEqual(self.fake.requests, [])

    def test_si_no_se_puede_comprobar_se_rechaza_por_seguridad(self):
        cases = (("404", _scope_responder(404, b'{"message":"404 Not Found"}')),
                 ("sin_scopes", _scope_responder(200, b'{"name":"x"}')),
                 ("scopes_vacios", _scope_responder(200, b'{"scopes":[]}')),
                 ("scopes_no_lista", _scope_responder(200, b'{"scopes":"api"}')),
                 ("no_json", _scope_responder(200, b"<html>")),
                 ("500", _scope_responder(500, b"{}")))
        for name, responder in cases:
            with self.subTest(caso=name):
                self.fake.reset()
                self.fake.add_project("grp/demo", commits={"develop": SHA}, archives={"zip": b"x"})
                self.fake.responder = responder
                code, doc, _, _ = self.call(*self.args())
                self.assertEqual((code, doc["error"]["code"]), (1, "E_CONFIG"), name)
                self.assertEqual(self.fake.requests, [])

    def test_token_invalido_da_e_auth_no_e_config(self):
        self.fake.responder = _scope_responder(401, b'{"message":"401 Unauthorized"}')
        code, doc, _, _ = self.call(*self.args())
        self.assertEqual((code, doc["error"]["code"]), (1, "E_AUTH"))


class OnlyGetTests(FakeCase):
    def test_todas_las_peticiones_de_todos_los_comandos_son_get(self):
        from tests.support import make_zip
        self.fake.add_project("grp/demo", commits={"develop": SHA}, archives={"zip": make_zip()},
                              branches=[{"name": "main", "default": True, "protected": False, "commit": {"id": SHA}}])
        self.call("branches", "--project", "demo")
        self.call("tags", "--project", "demo")
        self.call("archive", "--project", "demo", "--ref", "develop", "--job-id", "j9")
        methods = self.fake.methods()
        self.assertGreaterEqual(len(methods), 6)
        self.assertEqual(set(methods), {"GET"})


class SourceStaticTests(unittest.TestCase):
    """Verificación estructural del código: no existe ningún camino que escriba, cree o borre en GitLab."""

    def sources(self):
        for name in sorted(os.listdir(PKG)):
            if name.endswith(".py"):
                path = os.path.join(PKG, name)
                with open(path, encoding="utf-8") as fh:
                    yield name, ast.parse(fh.read(), path)

    def test_ninguna_biblioteca_de_red_alternativa(self):
        forbidden = {"requests", "httpx", "aiohttp", "urllib3", "socket", "subprocess", "ftplib", "smtplib", "urllib.request"}
        for name, tree in self.sources():
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    mods = [node.module or ""]
                for mod in mods:
                    self.assertNotIn(mod, forbidden, f"{name} importa {mod}")

    def test_toda_llamada_request_usa_get_literal(self):
        found = 0
        for name, tree in self.sources():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("request", "putrequest"):
                    if name == "client.py" and isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
                        continue  # GitLabClient.request(method, ...): rechaza todo lo que no sea GET (probado en test_client)
                    found += 1
                    first = node.args[0] if node.args else None
                    self.assertTrue(isinstance(first, ast.Constant) and first.value == "GET", f"{name}:{node.lineno}")
        self.assertGreaterEqual(found, 1)

    def test_no_aparece_ningun_verbo_de_escritura(self):
        verbs = {"POST", "PUT", "PATCH", "DELETE", "post", "put", "patch", "delete"}
        for name, tree in self.sources():
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn(node.value, verbs, f"{name}:{node.lineno}")

    def test_unicas_rutas_de_api_permitidas(self):
        """Todo lo que se pide a la API es de lectura: lista fija de recursos."""
        allowed = ("/api/v4", "/personal_access_tokens/self", "/projects/", "/repository", "/commits/", "/archive.", "/branches", "/tags")
        for name, tree in self.sources():
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/") and len(node.value) > 3:
                    if any(h in node.value for h in ("/api/v4", "/projects", "/repository", "/personal_access", "/commits", "/archive", "/branches", "/tags")):
                        self.assertTrue(any(a in node.value for a in allowed), f"{name}:{node.lineno}: {node.value}")


if __name__ == "__main__":
    unittest.main()
