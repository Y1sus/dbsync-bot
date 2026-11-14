# =============================================================================
# services/gitlab/tests/test_cli.py — Pruebas del CLI: catalog, branches, tags, uso, referencias hostiles, códigos de salida y esquema
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import json
import os
import subprocess
import sys

from tests.fakes.gitlab_fake import SHA, TOKEN
from tests.support import CATALOG, ROOT, FakeCase, schema_errors


def branch(name, default=False, protected=False, sha=SHA):
    return {"name": name, "default": default, "protected": protected, "commit": {"id": sha}}


class CatalogTests(FakeCase):
    def test_catalog(self):
        code, doc, _, _ = self.call("catalog")
        self.assertEqual(code, 0)
        self.assertEqual([p["name"] for p in doc["projects"]], ["demo", "My App"])
        self.assertTrue(doc["projects"][0]["has_db"])
        self.assertEqual(doc["projects"][0]["db"]["container"], "mysql8")
        self.assertFalse(doc["projects"][1]["has_db"])
        self.assertEqual(self.fake.requests, [])

    def test_catalogo_invalido_es_e_config(self):
        with open(self.e.projects_file, "w") as fh:
            json.dump({"projects": [{"name": "a", "path": "mal path"}]}, fh)
        for argv in (("catalog",), ("branches", "--project", "a")):
            code, doc, _, _ = self.call(*argv)
            self.assertEqual((code, doc["error"]["code"]), (1, "E_CONFIG"))

    def test_http_sin_permiso(self):
        code, doc, _, _ = self.call("catalog", GITLAB_ALLOW_HTTP="0")
        self.assertEqual((code, doc["error"]["code"]), (1, "E_CONFIG"))
        self.assertEqual(self.fake.requests, [])

    def test_sin_token(self):
        os.unlink(self.e.token_file)
        code, doc, _, _ = self.call("branches", "--project", "demo")
        self.assertEqual((code, doc["error"]["code"]), (1, "E_CONFIG"))


class RefsTests(FakeCase):
    def test_orden_defecto_primero_y_campos(self):
        self.fake.add_project("grp/demo", branches=[branch("zeta"), branch("main", True, True), branch("Alfa"),
                                                    branch("beta", sha="b" * 40)])
        code, doc, _, _ = self.call("branches", "--project", "demo")
        self.assertEqual(code, 0)
        self.assertEqual([r["name"] for r in doc["refs"]], ["main", "Alfa", "beta", "zeta"])
        self.assertEqual(doc["refs"][0], {"name": "main", "default": True, "protected": True, "sha": SHA})
        self.assertEqual(doc["refs"][2]["sha"], "b" * 40)
        self.assertFalse(doc["truncated"])
        self.assertEqual(doc["project"], "demo")

    def test_pagina_hasta_tres_y_marca_truncated(self):
        self.fake.add_project("grp/demo", branches=[branch(f"b{i:04d}", i == 7) for i in range(450)])
        code, doc, _, _ = self.call("branches", "--project", "demo")
        self.assertEqual(len(doc["refs"]), 300)
        self.assertTrue(doc["truncated"])
        self.assertEqual(doc["refs"][0]["name"], "b0007")
        self.assertEqual(self.fake.requests[0]["query"], "per_page=100&page=1")
        self.assertEqual(len(self.fake.requests), 3)

    def test_exactamente_300_no_es_truncated(self):
        self.fake.add_project("grp/demo", tags=[{"name": f"t{i:03d}", "commit": {"id": SHA}} for i in range(300)])
        code, doc, _, _ = self.call("tags", "--project", "demo")
        self.assertEqual((len(doc["refs"]), doc["truncated"], doc["command"]), (300, False, "tags"))

    def test_una_pagina_corta_no_pide_mas(self):
        self.fake.add_project("grp/demo", branches=[branch("a")])
        self.call("branches", "--project", "demo")
        self.assertEqual(len(self.fake.requests), 1)

    def test_search_codificado(self):
        self.fake.add_project("grp/demo", branches=[branch("feat/a"), branch("fix/b")])
        code, doc, _, _ = self.call("branches", "--project", "demo", "--search", "feat/ ñ&x=1")
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.requests[0]["query"], "per_page=100&page=1&search=feat%2F%20%C3%B1%26x%3D1")
        code, doc, _, _ = self.call("tags", "--project", "demo", "--search", "feat")
        self.assertTrue(self.fake.requests[-1]["query"].endswith("&search=feat"))
        self.assertTrue(self.fake.requests[-1]["raw_path"].endswith("/repository/tags"))

    def test_search_con_control_rechazado(self):
        code, doc, _, _ = self.call("branches", "--project", "demo", "--search", "a\nb")
        self.assertEqual((code, doc["error"]["code"]), (1, "E_INVALID_NAME"))
        self.assertEqual(self.fake.requests, [])


class ValidationTests(FakeCase):
    def test_proyecto_fuera_del_catalogo(self):
        self.fake.add_project("otro/secreto", commits={"main": SHA})
        for name in ("otro/secreto", "Demo", "grp/demo", "nope"):
            for argv in (("branches", "--project", name), ("archive", "--project", name, "--ref", "main", "--job-id", "j")):
                code, doc, _, _ = self.call(*argv)
                self.assertEqual((code, doc["error"]["code"]), (1, "E_NOT_ALLOWED"), argv)
        self.assertEqual(self.fake.requests, [])

    def test_referencias_hostiles_no_llegan_a_gitlab(self):
        hostiles = ["--upload-pack=x", "../x", "a b", "a\nb", "%2e%2e", "a\x00b", "ñandú", "a//b", "x.lock", "a/"]
        for ref in hostiles:
            with self.subTest(ref=ref):
                code, doc, _, _ = self.call("archive", "--project", "demo", f"--ref={ref}", "--job-id", "j1")
                self.assertEqual((code, doc["error"]["code"]), (1, "E_INVALID_NAME"))
        self.assertEqual(self.fake.requests, [])

    def test_ref_que_parece_opcion_sin_igual_es_uso_incorrecto(self):
        code, doc, _, _ = self.call("archive", "--project", "demo", "--ref", "--upload-pack=x", "--job-id", "j")
        self.assertEqual((code, doc["error"]["code"]), (2, "E_USAGE"))
        self.assertEqual(self.fake.requests, [])

    def test_referencia_legitima_con_barra_llega_codificada(self):
        self.call("archive", "--project", "demo", "--ref", "feat/x", "--job-id", "j1")
        self.assertEqual(self.fake.requests[0]["raw_path"], "/api/v4/projects/grp%2Fdemo/repository/commits/feat%2Fx")

    def test_formato_y_job_id_invalidos(self):
        base = ["archive", "--project", "demo", "--ref", "develop"]
        for extra in (["--job-id", "j1", "--format", "rar"], ["--job-id", "../x"], ["--job-id=-a"], ["--job-id", "a b"]):
            code, doc, _, _ = self.call(*base, *extra)
            self.assertEqual((code, doc["error"]["code"]), (1, "E_INVALID_NAME"), extra)
        self.assertEqual(self.fake.requests, [])


class UsageTests(FakeCase):
    def test_codigos_de_uso(self):
        cases = [(), ("nada",), ("branches",), ("archive", "--project", "demo"), ("catalog", "--x"),
                 ("archive", "--project", "demo", "--ref", "a")]
        for argv in cases:
            with self.subTest(argv=argv):
                code, doc, out, err = self.call(*argv)
                self.assertEqual((code, doc["error"]["code"]), (2, "E_USAGE"))
                self.assertEqual(len(out.splitlines()), 1)

    def test_help_no_rompe_el_contrato_de_stdout(self):
        for flag in ("-h", "--help"):
            code, doc, out, _ = self.call(flag)
            self.assertEqual((code, doc["error"]["code"], len(out.splitlines())), (2, "E_USAGE", 1))

    def test_comando_reportado_en_uso_incorrecto(self):
        code, doc, _, _ = self.call("archive", "--project", "demo")
        self.assertEqual((code, doc["command"]), (2, "archive"))
        self.assertEqual(self.call("zzz")[1]["command"], "catalog")

    def test_exit_codes_y_salida_en_subproceso(self):
        env = {**os.environ, **self.e.env, "PYTHONDONTWRITEBYTECODE": "1"}
        fetch = str(ROOT / "fetch.py")
        run = lambda *a: subprocess.run([sys.executable, fetch, *a], env=env, capture_output=True, text=True, timeout=20)
        res = run("archive", "--project", "demo", "--ref", "develop", "--job-id", "sp1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(schema_errors(json.loads(res.stdout)), [])
        self.assertEqual(len(res.stdout.splitlines()), 1)
        self.assertNotIn(TOKEN, res.stdout + res.stderr)
        self.assertTrue(os.access(fetch, os.X_OK))
        with open(fetch) as fh:
            self.assertTrue(fh.readline().startswith("#!"))


class SecretTests(FakeCase):
    def test_token_sin_prefijo_glpat_redactado_por_valor(self):
        token = "plain-secret-Value_42"
        with open(self.e.token_file, "w") as fh:
            fh.write(token)
        self.fake.responder = lambda h, r: (h.reply_json(200, {"scopes": ["read_api"]}) or True) if r["raw_path"].endswith("/personal_access_tokens/self") else (h.reply(400, ('{"message":"eco %s %s"}' % (token, token[::-1])).encode()) or True)
        code, doc, out, err = self.call("branches", "--project", "demo")
        self.assertEqual((code, doc["error"]["code"]), (1, "E_GITLAB"))
        self.assertNotIn(token, out + err)
        self.assertIn("[REDACTADO]", doc["error"]["message"])


class SchemaTests(FakeCase):
    def test_todas_las_salidas_cumplen_el_esquema(self):
        self.fake.add_project("grp/demo", commits={"develop": SHA}, archives={"zip": self.fake.projects["grp/demo"]["archives"]["zip"]},
                              branches=[branch("main", True)], tags=[{"name": "v1", "commit": {"id": SHA}}])
        runs = [("catalog",), ("branches", "--project", "demo"), ("tags", "--project", "demo"),
                ("archive", "--project", "demo", "--ref", "develop", "--job-id", "s1"),
                ("archive", "--project", "demo", "--ref", "nope", "--job-id", "s2"), ("branches", "--project", "x"), ("zzz",)]
        for argv in runs:
            code, doc, _, _ = self.e.run(*argv)
            self.assertEqual(schema_errors(doc), [], (argv, doc))
            self.assertEqual(doc["schema_version"], 1)

    def test_validador_detecta_incumplimientos(self):
        self.assertNotEqual(schema_errors({"schema_version": 1, "ok": True, "command": "archive"}), [])
        self.assertNotEqual(schema_errors({"schema_version": 2, "ok": False, "command": "catalog",
                                           "error": {"code": "E_USAGE", "message": "x"}}), [])
        self.assertNotEqual(schema_errors({"schema_version": 1, "ok": False, "command": "catalog",
                                           "error": {"code": "E_RARO", "message": "x"}}), [])
