# =============================================================================
# services/gitlab/tests/test_limits.py — Límites de robustez: memoria, plazo total, bombas de compresión, .part viejos y Content-Length
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import io
import json
import os
import struct
import tarfile
import time
import unittest
import zipfile
from unittest import mock

from gitlab_fetch import archive, verify
from gitlab_fetch.client import GitLabClient
from gitlab_fetch.errors import FetchError
from gitlab_fetch.verify import Limits
from tests.fakes.gitlab_fake import SHA, TOKEN
from tests.support import FakeCase, make_targz, make_zip

ARCH = ("archive", "--project", "demo", "--ref", "develop", "--job-id", "lim1")
ZEROS = b"\0" * (2 * 1024 * 1024)


def tar_bytes(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def zip_duplicate_cd(data, times):
    eocd = data.rfind(b"PK\x05\x06")
    entries, size, off = struct.unpack_from("<HII", data, eocd + 10)
    cd = data[eocd - size:eocd]
    rec = bytearray(data[eocd:])
    struct.pack_into("<HHII", rec, 8, min(entries * times, 0xFFFF), min(entries * times, 0xFFFF), size * times, off)
    return data[:eocd - size] + cd * times + bytes(rec)


def zip_to_zip64(data):
    eocd = data.rfind(b"PK\x05\x06")
    entries, size, off = struct.unpack_from("<HII", data, eocd + 10)
    rec64 = b"PK\x06\x06" + struct.pack("<QHHIIQQQQ", 44, 45, 45, 0, 0, entries, entries, size, off)
    loc = b"PK\x06\x07" + struct.pack("<IQI", 0, eocd, 1)
    end = b"PK\x05\x06" + struct.pack("<HHHHIIH", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    return data[:eocd] + rec64 + loc + end


class VerifyLimitsTests(unittest.TestCase):
    def check(self, data, fmt, limits=Limits()):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix="." + fmt, delete=False) as fh:
            fh.write(data)
        self.addCleanup(os.unlink, fh.name)
        verify.verify(fh.name, fmt, limits)

    def code(self, data, fmt, limits=Limits()):
        with self.assertRaises(FetchError) as cm:
            self.check(data, fmt, limits)
        return cm.exception.code

    def many(self, n):
        return {f"d/f{i}.txt": b"x" for i in range(n)}

    def test_zip_demasiadas_entradas(self):
        data = make_zip(self.many(50))
        self.assertEqual(self.code(data, "zip", Limits(max_entries=10)), "E_TOO_LARGE")
        self.check(data, "zip", Limits(max_entries=50))

    def test_zip_eocd_que_miente_en_el_conteo(self):
        data = bytearray(make_zip(self.many(50)))
        eocd = data.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", data, eocd + 8, 1, 1)
        self.assertEqual(self.code(bytes(data), "zip", Limits(max_entries=10)), "E_TOO_LARGE")

    def test_zip_zip64_se_lee_el_directorio(self):
        data = zip_to_zip64(make_zip(self.many(5)))
        self.check(data, "zip", Limits(max_entries=5))
        self.assertEqual(self.code(data, "zip", Limits(max_entries=3)), "E_TOO_LARGE")

    def test_zip_bomba_de_compresion(self):
        data = make_zip({"z.bin": ZEROS})
        self.assertLess(len(data), 10000)
        self.assertEqual(self.code(data, "zip", Limits(ratio=8, min_bytes=1)), "E_TOO_LARGE")
        self.check(data, "zip")

    def test_zip_entradas_solapadas(self):
        base = make_zip({"z.bin": ZEROS[:1024 * 1024]})
        data = zip_duplicate_cd(base, 30)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertEqual(len(zf.infolist()), 30)
        self.assertEqual(self.code(data, "zip", Limits(ratio=8, min_bytes=1)), "E_TOO_LARGE")

    def test_zip_crc_corrupto_y_truncado(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("a.txt", b"hola mundo" * 50)
        data = bytearray(buf.getvalue())
        data[60] ^= 0xFF
        self.assertEqual(self.code(bytes(data), "zip"), "E_TRUNCATED")
        good = make_zip({"a": os.urandom(4000)})
        self.assertEqual(self.code(good[:-30], "zip"), "E_TRUNCATED")
        self.assertEqual(self.code(b"PK\x05\x06" + b"\0" * 5, "zip"), "E_TRUNCATED")

    def test_targz_demasiadas_entradas_en_una_pasada(self):
        data = tar_bytes(self.many(50))
        self.assertEqual(self.code(data, "tar.gz", Limits(max_entries=10)), "E_TOO_LARGE")
        self.check(data, "tar.gz", Limits(max_entries=50))
        self.check(tar_bytes(self.many(300)), "tar.gz")

    def test_targz_no_acumula_miembros(self):
        sizes = []
        real_next = tarfile.TarFile.next

        def spy(self):
            sizes.append(len(self.members))
            return real_next(self)
        with mock.patch.object(tarfile.TarFile, "next", spy):
            self.check(tar_bytes(self.many(40)), "tar.gz")
        self.assertLessEqual(max(sizes), 1)

    def test_targz_bomba_y_relleno_posterior(self):
        low = Limits(ratio=8, min_bytes=1)
        self.assertEqual(self.code(tar_bytes({"z.bin": ZEROS}), "tar.gz", low), "E_TOO_LARGE")
        import gzip
        padded = gzip.compress(make_tar_raw({"a": b"x"}) + ZEROS)
        self.assertEqual(self.code(padded, "tar.gz", low), "E_TOO_LARGE")
        self.assertEqual(self.code(tar_bytes({"z.bin": ZEROS}), "tar.gz", Limits(ratio=8, min_bytes=1)), "E_TOO_LARGE")

    def test_targz_truncado_vacio_y_cola(self):
        good = make_targz({"a.bin": os.urandom(100000)})
        for cut in (1, 8, len(good) // 2):
            self.assertEqual(self.code(good[:-cut], "tar.gz"), "E_TRUNCATED")
        self.assertEqual(self.code(make_targz({}), "tar.gz"), "E_TRUNCATED")


def make_tar_raw(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class CliLimitsTests(FakeCase):
    def test_tope_de_entradas_por_entorno_no_deja_archivo(self):
        self.fake.add_project("grp/demo", commits={"develop": SHA},
                              archives={"zip": make_zip({f"f{i}": b"x" for i in range(5)})})
        code, doc, _, _ = self.call(*ARCH, GITLAB_VERIFY_MAX_ENTRIES="2")
        self.assertEqual((code, doc["error"]["code"]), (1, "E_TOO_LARGE"))
        self.assertEqual(self.e.files(), [])

    def test_bomba_por_entorno_no_deja_archivo(self):
        self.fake.add_project("grp/demo", commits={"develop": SHA}, archives={"zip": make_zip({"z": ZEROS})})
        code, doc, _, _ = self.call(*ARCH, GITLAB_VERIFY_MIN_BYTES="1")
        self.assertEqual((code, doc["error"]["code"]), (1, "E_TOO_LARGE"))
        self.assertEqual(self.e.files(), [])


def drip(fake, head, body_byte=None, interval=0.005):
    """Responder que gotea la respuesta byte a byte hasta que la prueba termina."""
    def send(h, data):
        for i in range(len(data)):
            if fake.release.wait(interval):
                return False
            h.wfile.write(data[i:i + 1])
            h.wfile.flush()
        return True

    def responder(h, req):
        if "personal_access_tokens" in req["raw_path"]:
            return None
        if body_byte is None:
            send(h, head + b"X-Pad: " + b"a" * 5000)
        else:
            h.wfile.write(head)
            h.wfile.flush()
            while send(h, body_byte * 50):
                pass
        h.close_connection = True
        return True
    return responder


HEAD = b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: application/json\r\n\r\n"


class DeadlineTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.fake.release.set)

    def timed(self, *argv, limit="0.01", **env):
        started = time.monotonic()
        res = self.call(*argv, GITLAB_TIMEOUT_SECONDS=limit, **env)
        self.assertLess(time.monotonic() - started, 0.5)
        return res

    def test_cabeceras_goteadas(self):
        self.fake.responder = drip(self.fake, b"HTTP/1.1 200 OK\r\n")
        code, doc, _, _ = self.timed("branches", "--project", "demo")
        self.assertEqual((code, doc["error"]["code"]), (1, "E_NETWORK"))

    def test_cuerpo_json_goteado(self):
        self.fake.responder = drip(self.fake, HEAD, b" ")
        code, doc, _, _ = self.timed("branches", "--project", "demo")
        self.assertEqual((code, doc["error"]["code"]), (1, "E_NETWORK"))

    def test_cuerpo_de_archive_goteado_limpia_el_part(self):
        self.fake.responder = drip(self.fake, HEAD.replace(b"json", b"zip"), b"x")
        original = self.fake.responder

        def only_archive(h, req):
            return original(h, req) if "archive" in req["raw_path"] else None
        self.fake.responder = only_archive
        code, doc, _, _ = self.timed(*ARCH)
        self.assertEqual((code, doc["error"]["code"]), (1, "E_NETWORK"))
        self.assertEqual(self.e.files(), [])

    def test_capa_de_lecturas_sin_alarma(self):
        self.fake.responder = drip(self.fake, HEAD, b" ")
        client = GitLabClient(self.fake.url, TOKEN, 5, 0.01, sleep=lambda s: None)
        started = time.monotonic()
        with self.assertRaises(FetchError) as cm:
            client.get_json("/api/v4/projects/x")
        self.assertEqual(cm.exception.code, "E_NETWORK")
        self.assertLess(time.monotonic() - started, 0.5)

    def test_capa_de_lecturas_en_streaming(self):
        self.fake.responder = drip(self.fake, HEAD, b"x")
        client = GitLabClient(self.fake.url, TOKEN, 5, 0.01, sleep=lambda s: None)
        part = os.path.join(self.e.tmp, "s.part")
        with self.assertRaises(FetchError) as cm:
            archive._stream(client, "/api/v4/x", part, 10 ** 9)
        self.assertEqual(cm.exception.code, "E_NETWORK")

    def test_la_alarma_cubre_la_verificacion(self):
        entered = []

        def slow(*args, **kwargs):
            entered.append(1)
            while True:
                time.sleep(0.005)
        with mock.patch.object(archive, "verify", slow):
            code, doc, _, _ = self.timed(*ARCH, limit="0.04")
        self.assertTrue(entered, "la prueba debe llegar a la fase de verificación")
        self.assertEqual((code, doc["error"]["code"]), (1, "E_NETWORK"))
        self.assertEqual(self.e.files(), [])

    def test_sin_alarma_residual_tras_terminar(self):
        import signal
        self.assertEqual(self.call("catalog")[0], 0)
        self.assertEqual(self.call(*ARCH)[0], 0)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0.0)


class MemoryTests(FakeCase):
    def reply_json(self, body, length=True):
        def responder(h, req):
            if "personal_access_tokens" in req["raw_path"] or "branches" not in req["raw_path"]:
                return None
            h.reply(200, body, mode="length" if length else "close")
            return True
        self.fake.responder = responder

    def test_json_mayor_a_2_mib_es_e_gitlab(self):
        big = b'[{"name":"' + b"a" * (3 * 1024 * 1024) + b'"}]'
        for length in (True, False):
            with self.subTest(length=length):
                self.reply_json(big, length)
                code, doc, out, _ = self.call("branches", "--project", "demo")
                self.assertEqual((code, doc["error"]["code"]), (1, "E_GITLAB"))
                self.assertLess(len(out), 1000)

    def test_filtra_campos_nombres_largos_y_shas_invalidos(self):
        items = [
            {"name": "ok", "default": True, "protected": False, "commit": {"id": SHA, "message": "x" * 1000}, "extra": "y" * 1000},
            {"name": "n" * 201, "commit": {"id": SHA}},
            {"name": "n" * 200, "commit": {"id": SHA}},
            {"name": "sin-sha", "commit": {}},
            {"name": "sha-corto", "commit": {"id": "abc"}},
            {"name": "sha-mayus", "commit": {"id": SHA.upper()}},
            {"name": 5, "commit": {"id": SHA}}, "texto", None,
        ]
        self.reply_json(json.dumps(items).encode())
        code, doc, _, _ = self.call("branches", "--project", "demo")
        self.assertEqual(code, 0)
        self.assertEqual([r["name"] for r in doc["refs"]], ["ok", "n" * 200])
        self.assertEqual(set(doc["refs"][0]), {"name", "default", "protected", "sha"})

    def test_tope_de_300_refs_y_salida_acotada(self):
        page = json.dumps([{"name": "r" * 190 + str(i), "commit": {"id": SHA}} for i in range(0, 4000)]).encode()
        calls = []

        def responder(h, req):
            if "personal_access_tokens" in req["raw_path"] or "branches" not in req["raw_path"]:
                return None
            calls.append(1)
            h.reply(200, page, {"X-Next-Page": "9"})
            return True
        self.fake.responder = responder
        code, doc, out, _ = self.call("branches", "--project", "demo")
        self.assertEqual(code, 0)
        self.assertEqual(len(doc["refs"]), 300)
        self.assertLessEqual(len(doc["refs"]), 300)
        self.assertTrue(doc["truncated"])
        self.assertLess(len(out), 200_000)
        self.assertEqual(len(calls), 1)


class ContentLengthTests(FakeCase):
    def raw(self, path_part, values):
        def responder(h, req):
            if path_part not in req["raw_path"]:
                return None
            h.send_response(200)
            for v in values:
                h.send_header("Content-Length", v)
            h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(b"[]")
            h.close_connection = True
            return True
        self.fake.responder = responder

    def test_valores_invalidos_en_archive_y_json(self):
        for values in (["²"], ["5", "5"], ["5, 10"], ["+5"], ["-1"], ["5.0"]):
            for part, argv in (("archive", ARCH), ("branches", ("branches", "--project", "demo"))):
                with self.subTest(values=values, part=part):
                    self.raw(part, values)
                    code, doc, _, _ = self.call(*argv)
                    self.assertEqual((code, doc["error"]["code"]), (1, "E_GITLAB"))
                    self.assertEqual(self.e.files(), [])


class StalePartTests(FakeCase):
    def test_borra_part_viejos_regulares_y_respeta_recientes_y_symlinks(self):
        d = os.path.join(self.e.dumps, "_gitlab")
        os.makedirs(d, mode=0o700)
        old, new, victim = (os.path.join(d, n) for n in ("viejo.zip.part", "nuevo.zip.part", "victima.txt"))
        link, subdir, other = (os.path.join(d, n) for n in ("enlace.zip.part", "dir.part", "otro.zip"))
        for p in (old, new, victim, other):
            open(p, "w").close()
        os.mkdir(subdir)
        os.symlink(victim, link)
        past = time.time() - 7200
        for p in (old, victim, other, subdir):
            os.utime(p, (past, past))
        os.utime(link, (past, past), follow_symlinks=False)
        self.assertEqual(self.call(*ARCH)[0], 0)
        self.assertFalse(os.path.lexists(old))
        for keep in (new, victim, other, subdir):
            self.assertTrue(os.path.lexists(keep), keep)
        self.assertTrue(os.path.islink(link))


if __name__ == "__main__":
    unittest.main()
