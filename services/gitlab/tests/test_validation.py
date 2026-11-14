# =============================================================================
# services/gitlab/tests/test_validation.py — Pruebas de validación: referencias, job_id, formato, slugs, catálogo y configuración
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import json
import os
import tempfile
import unittest

from gitlab_fetch import catalog, names
from gitlab_fetch.config import from_env
from gitlab_fetch.errors import FetchError

HOSTILE = ["--upload-pack=x", "-x", "../x", "a/../b", "a b", "a\nb", "a\n", "%2e%2e", "a\x00b", "ñandú", "a//b",
           "a/", "x.lock", "a/.hidden", "a@{1}", "a;b", "a$b", "", ".x", "/abs", "a" * 201, "a\\b", "a?b", "a#b"]
LEGIT = ["develop", "feat/x", "v1.0", "release/2024-01/x_y", "a", "A" * 200]


class RefTests(unittest.TestCase):
    def test_hostiles_rechazadas(self):
        for ref in HOSTILE:
            with self.subTest(ref=ref):
                with self.assertRaises(FetchError) as cm:
                    names.check_ref(ref)
                self.assertEqual(cm.exception.code, "E_INVALID_NAME")

    def test_legitimas_aceptadas(self):
        for ref in LEGIT:
            with self.subTest(ref=ref):
                self.assertEqual(names.check_ref(ref), ref)

    def test_job_id_y_formato(self):
        for ok in ("tg123-abcd", "A", "a_b-c", "x" * 64):
            names.check_job_id(ok)
        for bad in ("", "-a", "_a", "a b", "a/b", "x" * 65, "a\n", "../x"):
            with self.assertRaises(FetchError):
                names.check_job_id(bad)
        for fmt in ("zip", "tar.gz"):
            names.check_format(fmt)
        for bad in ("tar", "ZIP", "7z", "", "zip;"):
            with self.assertRaises(FetchError):
                names.check_format(bad)

    def test_nombre_de_archivo_solo_de_datos_validados(self):
        sha = "ad91449c" + "0" * 32
        self.assertEqual(names.archive_filename("maruichimex", "develop", sha, "tg123-abcd", "zip"),
                         "maruichimex-develop-ad91449c-tg123-abcd.zip")
        name = names.archive_filename("My App", "feat/x", sha, "j1", "tar.gz")
        self.assertEqual(name, "my-app-feat-x-ad91449c-j1.tar.gz")
        long = names.archive_filename("p" * 64, "r" * 200, sha, "j" * 64, "tar.gz")
        self.assertLessEqual(len(long), 255)
        self.assertNotIn("/", long)

    def test_busqueda(self):
        self.assertEqual(names.check_search("feat"), "feat")
        self.assertIsNone(names.check_search(None))
        for bad in ("a\nb", "a\x00", "x" * 101):
            with self.assertRaises(FetchError):
                names.check_search(bad)


class CatalogTests(unittest.TestCase):
    def load(self, data):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))
        self.addCleanup(os.unlink, fh.name)
        return catalog.load(fh.name)

    def assertConfigError(self, data):
        with self.assertRaises(FetchError) as cm:
            self.load(data)
        self.assertEqual(cm.exception.code, "E_CONFIG")

    def test_valido_y_vista_publica(self):
        projects = self.load({"projects": [
            {"name": "a", "path": "g/a", "db": {"server": "s", "container": "c", "db": "d"}},
            {"name": "b c", "path": "g/s/b"}]})
        view = catalog.public_view(projects)
        self.assertTrue(view[0]["has_db"])
        self.assertEqual(view[0]["db"], {"server": "s", "container": "c", "db": "d"})
        self.assertFalse(view[1]["has_db"])
        self.assertNotIn("db", view[1])

    def test_ejemplo_del_repositorio(self):
        example = os.path.join(os.path.dirname(__file__), "..", "..", "..", "deploy", "projects.example.json")
        if os.path.exists(example):
            self.assertGreater(len(catalog.load(example)), 0)

    def test_reglas_invalidas(self):
        def p(**kw):
            return {"projects": [{"name": "a", "path": "g/a", **kw}]}
        cases = [
            "no es json", "[]", {"projects": "x"}, {"projects": ["x"]},
            p(name=""), p(name="-a"), p(name="a" * 65), p(name="a/b"), p(name=3),
            p(path="solo"), p(path="g/a b"), p(path="g/../a"), p(path="/g/a"), p(path="g/a/"), p(path=None),
            p(db="x"), p(db={"server": "s", "container": "c"}),
            p(db={"server": "-s", "container": "c", "db": "d"}),
            p(db={"server": "s.x", "container": "c", "db": "d"}),
            p(db={"server": "s", "container": "c c", "db": "d"}),
            p(db={"server": "s", "container": "c", "db": "d;x"}),
            p(db={"server": "s", "container": "c", "db": "d" * 129}),
            {"projects": [{"name": "a", "path": "g/a"}, {"name": "a", "path": "g/b"}]},
        ]
        for data in cases:
            with self.subTest(data=data):
                self.assertConfigError(data)

    def test_archivo_inexistente(self):
        with self.assertRaises(FetchError) as cm:
            catalog.load("/no/existe/projects.json")
        self.assertEqual(cm.exception.code, "E_CONFIG")

    def test_proyecto_fuera_del_catalogo(self):
        projects = [{"name": "a", "path": "g/a"}]
        self.assertEqual(catalog.find(projects, "a")["path"], "g/a")
        for bad in ("g/a", "b", "A", ""):
            with self.assertRaises(FetchError) as cm:
                catalog.find(projects, bad)
            self.assertEqual(cm.exception.code, "E_NOT_ALLOWED")


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = from_env({})
        self.assertEqual(cfg.base_url, "https://gitlab.com")
        self.assertEqual((cfg.max_archive_mb, cfg.timeout_s, cfg.read_timeout_s, cfg.min_free_mb), (500, 300, 30, 1024))

    def test_http_requiere_permiso(self):
        with self.assertRaises(FetchError) as cm:
            from_env({"GITLAB_BASE_URL": "http://127.0.0.1:9"})
        self.assertEqual(cm.exception.code, "E_CONFIG")
        self.assertEqual(from_env({"GITLAB_BASE_URL": "http://127.0.0.1:9/", "GITLAB_ALLOW_HTTP": "1"}).base_url,
                         "http://127.0.0.1:9")

    def test_urls_invalidas(self):
        for url in ("ftp://x", "gitlab.com", "https://", "https://u:p@gitlab.com", "https://x?y=1", "file:///etc"):
            with self.subTest(url=url):
                with self.assertRaises(FetchError):
                    from_env({"GITLAB_BASE_URL": url, "GITLAB_ALLOW_HTTP": "1"})

    def test_numeros_invalidos(self):
        for key in ("GITLAB_MAX_ARCHIVE_MB", "GITLAB_TIMEOUT_SECONDS", "DBSYNC_MIN_FREE_MB"):
            for val in ("abc", "-1"):
                with self.assertRaises(FetchError):
                    from_env({key: val})

    def test_token(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("  glpat-abc \n")
        self.addCleanup(os.unlink, fh.name)
        self.assertEqual(from_env({"GITLAB_TOKEN_FILE": fh.name}).read_token(), "glpat-abc")
        for bad in ("", "a b", "a\r\nX-Evil: 1"):
            with open(fh.name, "w") as out:
                out.write(bad)
            with self.assertRaises(FetchError):
                from_env({"GITLAB_TOKEN_FILE": fh.name}).read_token()
        with self.assertRaises(FetchError):
            from_env({}).read_token()
        with self.assertRaises(FetchError):
            from_env({"GITLAB_TOKEN_FILE": "/no/existe"}).read_token()


if __name__ == "__main__":
    unittest.main()
