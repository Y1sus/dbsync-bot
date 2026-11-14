# =============================================================================
# services/gitlab/tests/test_archive.py — Pruebas de archive: commit fijado, streaming, tope, verificación, permisos y errores
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import hashlib
import os
import stat
import tarfile
import zipfile

from tests.fakes.gitlab_fake import SHA, TOKEN
from tests.support import FakeCase, make_targz, make_zip

ARCH = ("archive", "--project", "demo", "--job-id", "tg123-abcd")


class ArchiveTests(FakeCase):
    def archive(self, ref="develop", fmt="zip", **overrides):
        return self.call(*ARCH, f"--ref={ref}", "--format", fmt, **overrides)

    def test_exito_zip(self):
        code, doc, out, err = self.archive()
        self.assertEqual(code, 0, doc)
        self.assertEqual(doc["filename"], f"demo-develop-{SHA[:8]}-tg123-abcd.zip")
        self.assertEqual(doc["relpath"], "_gitlab/" + doc["filename"])
        self.assertEqual(doc["commit"], SHA)
        self.assertEqual(doc["verified"], {"archive": True})
        self.assertEqual(doc["file"], os.path.join(self.e.dumps, "_gitlab", doc["filename"]))
        with open(doc["file"], "rb") as fh:
            data = fh.read()
        self.assertEqual(doc["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(doc["size_bytes"], len(data))
        self.assertTrue(zipfile.is_zipfile(doc["file"]))
        self.assertEqual(self.e.files(), [doc["filename"]])
        self.assertEqual(len(out.splitlines()), 1)

    def test_exito_targz_y_permisos(self):
        code, doc, _, _ = self.archive(fmt="tar.gz")
        self.assertEqual(code, 0, doc)
        self.assertTrue(doc["filename"].endswith(".tar.gz"))
        with tarfile.open(doc["file"]) as tf:
            self.assertGreaterEqual(len(tf.getnames()), 1)
        self.assertEqual(stat.S_IMODE(os.stat(doc["file"]).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(doc["file"])).st_mode), 0o700)
        self.assertFalse([f for f in self.e.files() if f.endswith(".part")])

    def test_commit_fijado_y_nombre_independiente_de_content_disposition(self):
        code, doc, _, _ = self.archive(ref="feat/x")
        self.assertEqual(code, 0, doc)
        paths = self.fake.paths()
        self.assertEqual(paths[0], "/api/v4/projects/grp%2Fdemo/repository/commits/feat%2Fx")
        self.assertEqual(paths[1], f"/api/v4/projects/grp%2Fdemo/repository/archive.zip?sha={SHA}")
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(r["method"] == "GET" for r in self.fake.requests))
        self.assertEqual(doc["filename"], f"demo-feat-x-{SHA[:8]}-tg123-abcd.zip")
        self.assertNotIn("evil", doc["filename"])

    def test_proyecto_con_espacios_y_subgrupos(self):
        self.fake.add_project("grp/sub/my-app", commits={"v1.0": SHA}, archives={"zip": make_zip()})
        code, doc, _, _ = self.call("archive", "--project", "My App", "--ref", "v1.0", "--job-id", "j1")
        self.assertEqual(code, 0, doc)
        self.assertEqual(doc["filename"], f"my-app-v1.0-{SHA[:8]}-j1.zip")
        self.assertIn("/projects/grp%2Fsub%2Fmy-app/", self.fake.paths()[0])

    def test_modos_de_respuesta_sin_content_length(self):
        for mode in ("chunked", "close", "length"):
            with self.subTest(mode=mode):
                self.fake.archive_mode = mode
                code, doc, _, _ = self.archive()
                self.assertEqual(code, 0, doc)

    def test_dos_jobs_no_se_pisan(self):
        a = self.call(*ARCH, "--ref=develop")[1]
        b = self.call("archive", "--project", "demo", "--ref=develop", "--job-id", "otro")[1]
        self.assertNotEqual(a["filename"], b["filename"])
        self.assertEqual(len(self.e.files()), 2)

    def test_tope_sin_content_length_aborta_y_no_deja_archivo(self):
        big = os.urandom(1024 * 1024 + 4096)
        self.fake.add_project("grp/demo", commits={"develop": SHA}, archives={"zip": big})
        for mode in ("chunked", "close"):
            self.fake.archive_mode = mode
            code, doc, _, _ = self.archive(GITLAB_MAX_ARCHIVE_MB="1")
            self.assertEqual((code, doc["error"]["code"]), (1, "E_TOO_LARGE"))
            self.assertEqual(self.e.files(), [])

    def test_tope_con_content_length_aborta_antes_de_escribir(self):
        self.fake.add_project("grp/demo", commits={"develop": SHA}, archives={"zip": b"x" * (1024 * 1024 + 1)})
        self.fake.archive_mode = "length"
        code, doc, _, _ = self.archive(GITLAB_MAX_ARCHIVE_MB="1")
        self.assertEqual(doc["error"]["code"], "E_TOO_LARGE")
        self.assertEqual(self.e.files(), [])

    def test_targz_sin_cola_del_gzip(self):
        good = make_targz()
        for cut in (8, 1):
            with self.subTest(cut=cut):
                self.fake.add_project("grp/demo", commits={"develop": SHA}, archives={"tar.gz": good[:-cut]})
                code, doc, _, _ = self.archive(fmt="tar.gz")
                self.assertEqual((code, doc["error"]["code"]), (1, "E_TRUNCATED"))
                self.assertEqual(self.e.files(), [])

    def test_tiempo_total_durante_la_descarga(self):
        from gitlab_fetch import archive
        from gitlab_fetch.client import GitLabClient
        from gitlab_fetch.errors import FetchError
        ticks = [0.0]

        def clock():
            ticks[0] += 1.0
            return ticks[0]
        client = GitLabClient(self.fake.url, TOKEN, 5, 3, sleep=lambda s: None, clock=clock)
        part = os.path.join(self.e.tmp, "x.part")
        target = "/api/v4/projects/grp%2Fdemo/repository/archive.zip?sha=" + SHA
        with self.assertRaises(FetchError) as cm:
            archive._stream(client, target, part, 10 ** 9)
        self.assertEqual(cm.exception.code, "E_NETWORK")

    def test_zip_truncado_y_vacio(self):
        good = make_zip({"a.txt": os.urandom(5000)})
        for label, body in (("truncado", good[:len(good) // 2]), ("vacio", make_zip({})), ("basura", b"no soy zip"),
                            ("cero", b"")):
            with self.subTest(label):
                self.fake.add_project("grp/demo", commits={"develop": SHA}, archives={"zip": body})
                code, doc, _, _ = self.archive()
                self.assertEqual((code, doc["error"]["code"]), (1, "E_TRUNCATED"))
                self.assertEqual(self.e.files(), [])

    def test_targz_truncado_y_vacio(self):
        good = make_targz({"a.bin": os.urandom(200000)})
        for label, body in (("truncado", good[:len(good) // 2]), ("vacio", make_targz({})), ("basura", b"x" * 100)):
            with self.subTest(label):
                self.fake.add_project("grp/demo", commits={"develop": SHA}, archives={"tar.gz": body})
                code, doc, _, _ = self.archive(fmt="tar.gz")
                self.assertEqual((code, doc["error"]["code"]), (1, "E_TRUNCATED"))
                self.assertEqual(self.e.files(), [])

    def test_ref_inexistente_y_proyecto_inexistente_en_gitlab(self):
        code, doc, _, _ = self.archive(ref="no-existe")
        self.assertEqual(doc["error"]["code"], "E_NOT_FOUND")
        self.fake.projects.clear()
        code, doc, _, _ = self.archive()
        self.assertEqual(doc["error"]["code"], "E_NOT_FOUND")
        self.assertEqual(self.e.files(), [])

    def test_errores_http_de_gitlab(self):
        Path = __import__("pathlib").Path
        Path(self.e.token_file).write_text("glpat" "-incorrecto")
        code, doc, _, _ = self.archive()
        self.assertEqual((code, doc["error"]["code"]), (1, "E_AUTH"))
        for status, expected in ((403, "E_FORBIDDEN"), (429, "E_RATE_LIMITED"), (500, "E_GITLAB")):
            self.fake.responder = lambda h, r, s=status: (h.reply_json(200, {"scopes": ["read_api"]}) or True) if r["raw_path"].endswith("/personal_access_tokens/self") else (h.reply(s, b"{}") or True)
            code, doc, _, _ = self.archive()
            self.assertEqual(doc["error"]["code"], expected)
        self.assertTrue(self.e.sleeps)

    def test_429_con_reintento_exitoso_en_cli(self):
        calls = []

        def responder(h, req):
            calls.append(1)
            if len(calls) == 1:
                h.send_response(429)
                h.send_header("Retry-After", "2")
                h.send_header("Content-Length", "0")
                h.end_headers()
                return True
        self.fake.responder = responder
        code, doc, _, _ = self.archive()
        self.assertEqual(code, 0, doc)
        self.assertEqual(self.e.sleeps, [2.0])

    def test_red_caida(self):
        code, doc, _, _ = self.archive(GITLAB_BASE_URL="http://127.0.0.1:1")
        self.assertEqual((code, doc["error"]["code"]), (1, "E_NETWORK"))

    def test_corte_a_mitad_de_descarga(self):
        def responder(h, req):
            if "archive" in req["raw_path"]:
                h.send_response(200)
                h.send_header("Content-Length", "100000")
                h.end_headers()
                h.wfile.write(b"x" * 10)
                h.wfile.flush()
                h.close_connection = True
                return True
        self.fake.responder = responder
        code, doc, _, _ = self.archive()
        self.assertEqual((code, doc["error"]["code"]), (1, "E_TRUNCATED"))
        self.assertEqual(self.e.files(), [])

    def test_corte_en_chunked(self):
        def responder(h, req):
            if "archive" in req["raw_path"]:
                h.send_response(200)
                h.send_header("Transfer-Encoding", "chunked")
                h.end_headers()
                h.wfile.write(b"1000\r\nxx")
                h.wfile.flush()
                h.close_connection = True
                return True
        self.fake.responder = responder
        code, doc, _, _ = self.archive()
        self.assertEqual((code, doc["error"]["code"]), (1, "E_NETWORK"))
        self.assertEqual(self.e.files(), [])

    def test_espacio_insuficiente(self):
        code, doc, _, _ = self.archive(DBSYNC_MIN_FREE_MB=str(10 ** 9))
        self.assertEqual((code, doc["error"]["code"]), (1, "E_DISK"))
        self.assertEqual(self.fake.requests, [])

    def test_directorio_existente_se_endurece(self):
        d = os.path.join(self.e.dumps, "_gitlab")
        os.makedirs(d, mode=0o755)
        os.chmod(d, 0o755)
        self.assertEqual(self.archive()[0], 0)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)

    def test_token_nunca_en_salidas_ni_archivo(self):
        body = ('{"message":"mal %s glpat" "-AAAA"}' % TOKEN).encode()
        self.fake.responder = lambda h, r: h.reply(400, body) or True
        code, doc, out, err = self.archive()
        self.assertEqual(code, 1)
        self.assertNotIn("TESTtoken", out + err)
        self.assertNotIn("glpat", out + err.replace("[REDACTADO]", ""))
        self.fake.responder = None
        code, doc, out, err = self.archive()
        self.assertEqual(code, 0)
        with open(doc["file"], "rb") as fh:
            blob = out + err + fh.read().decode("latin-1")
        self.assertNotIn("TESTtoken", blob)
