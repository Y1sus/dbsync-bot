# =============================================================================
# services/gateway/tests/test_units.py — Pruebas unitarias: rangos, tokens, rutas, configuración y esquema
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import os
import tempfile
import unittest

from gateway import paths, tokens
from gateway.config import ConfigError, from_env, load_api_token
from gateway.ranges import RangeNotSatisfiable, parse_range
from gateway.store import Store, compute_state
from gateway.streaming import safe_filename


class RangeTests(unittest.TestCase):
    def test_validos(self):
        casos = {
            "bytes=0-0": (0, 0), "bytes=-1": (99, 99), "bytes=5-": (5, 99), "bytes=0-": (0, 99),
            "bytes=10-19": (10, 19), "bytes=90-500": (90, 99), "bytes=-500": (0, 99),
            "bytes=99-99": (99, 99), " bytes=1-2 ": (1, 2), "BYTES=1-2": (1, 2),
        }
        for header, esperado in casos.items():
            self.assertEqual(parse_range(header, 100), esperado, header)

    def test_sin_rango_y_multirango(self):
        self.assertIsNone(parse_range(None, 100))
        self.assertIsNone(parse_range("bytes=0-1,5-6", 100))

    def test_invalidos(self):
        for header in ["bytes=100-", "bytes=500-600", "bytes=9-3", "bytes=-0", "bytes=-", "bytes=",
                       "bytes=abc", "bytes=a-b", "items=0-1", "0-1", "bytes=1-2-3", "bytes=--5",
                       "bytes=+1-2", "bytes=" + "9" * 40 + "-"]:
            with self.assertRaises(RangeNotSatisfiable, msg=header):
                parse_range(header, 100)


class TokenTests(unittest.TestCase):
    def test_token_y_hash(self):
        token = tokens.new_token()
        self.assertEqual(len(token), 43)
        self.assertTrue(tokens.is_well_formed(token))
        digest = tokens.hash_token(token)
        self.assertEqual(len(digest), 64)
        self.assertNotIn(token, digest)
        self.assertTrue(tokens.verify(token, digest))
        self.assertFalse(tokens.verify(tokens.new_token(), digest))
        self.assertNotEqual(tokens.new_token(), tokens.new_token())

    def test_ids(self):
        self.assertRegex(tokens.new_link_id(), r"^[0-9a-f]{12}$")
        self.assertRegex(tokens.new_session_id(), r"^[0-9a-f]{32}$")

    def test_mal_formados(self):
        for bad in ["", "corto", "a" * 44, "a" * 42 + "/", None, 5]:
            self.assertFalse(tokens.is_well_formed(bad))


class PathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "dumps")
        os.makedirs(os.path.join(self.root, "srv"))
        os.makedirs(os.path.join(self.root, ".locks"))
        self.good = os.path.join(self.root, "srv", "a.sql.gz")
        for name in ("srv/a.sql.gz", "srv/b.sql.gz.part", "srv/c.txt", ".locks/x.sql.gz"):
            with open(os.path.join(self.root, name), "wb") as fh:
                fh.write(b"datos")
        self.outside = os.path.join(self.tmp.name, "secreto.sql.gz")
        with open(self.outside, "wb") as fh:
            fh.write(b"secreto")
        os.symlink(self.outside, os.path.join(self.root, "srv", "link.sql.gz"))
        os.symlink(self.tmp.name, os.path.join(self.root, "escape"))
        os.makedirs(os.path.join(self.root, "srv", "dir.sql.gz"))

    def tearDown(self):
        self.tmp.cleanup()

    def code(self, relpath):
        with self.assertRaises(paths.PathError) as ctx:
            paths.resolve_dump(self.root, relpath)
        return ctx.exception.code

    def test_valido(self):
        path, size = paths.resolve_dump(self.root, "srv/a.sql.gz")
        self.assertEqual(size, 5)
        self.assertTrue(path.endswith("srv/a.sql.gz"))

    def test_traversal_y_sintaxis(self):
        for rel in ["../secreto.sql.gz", "srv/../../secreto.sql.gz", "/etc/passwd.sql.gz", "srv//a.sql.gz",
                    "srv/./a.sql.gz", "srv/a\x00.sql.gz", "", None, "srv\\a.sql.gz", "..", ".locks/x.sql.gz",
                    "srv/.oculto.sql.gz", "x" * 2000 + ".sql.gz"]:
            self.assertEqual(self.code(rel), "invalid_request", repr(rel))

    def test_extension(self):
        self.assertEqual(self.code("srv/b.sql.gz.part"), "invalid_request")
        self.assertEqual(self.code("srv/c.txt"), "invalid_request")

    def test_symlink_inexistente_y_no_regular(self):
        self.assertEqual(self.code("srv/link.sql.gz"), "file_not_found")
        self.assertEqual(self.code("escape/secreto.sql.gz"), "file_not_found")
        self.assertEqual(self.code("srv/nada.sql.gz"), "file_not_found")
        self.assertEqual(self.code("srv/dir.sql.gz"), "file_not_found")

    def test_open_dump_rechaza_symlink(self):
        with self.assertRaises(paths.PathError):
            paths.open_dump(self.root, "srv/link.sql.gz")
        fd, size = paths.open_dump(self.root, "srv/a.sql.gz")
        os.close(fd)
        self.assertEqual(size, 5)


class MiscTests(unittest.TestCase):
    def test_nombre_saneado(self):
        self.assertEqual(safe_filename('a"b<c>\r\n.sql.gz'), "a_b_c___.sql.gz")
        self.assertEqual(safe_filename("..."), "dump.sql.gz")
        self.assertEqual(safe_filename("año.sql.gz"), "a_o.sql.gz")

    def test_config(self):
        base = {"GATEWAY_PUBLIC_BASE_URL": "https://x.test/", "GATEWAY_API_TOKEN_FILE": "/t"}
        cfg = from_env(base)
        self.assertEqual(cfg.public_base_url, "https://x.test")
        self.assertEqual((cfg.public_port, cfg.internal_port, cfg.retention_hours), (8080, 8081, 24.0))
        self.assertEqual(from_env({**base, "GATEWAY_PUBLIC_PORT": "9000"}).public_port, 9000)
        for extra in [{"GATEWAY_PUBLIC_PORT": "abc"}, {"GATEWAY_INTERNAL_PORT": "70000"},
                      {"GATEWAY_MAX_DUMPS_GB": "-1"}, {"GATEWAY_PUBLIC_BASE_URL": "ftp://x"}]:
            with self.assertRaises(ConfigError):
                from_env({**base, **extra})
        with self.assertRaises(ConfigError):
            from_env({})

    def test_token_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t")
            with self.assertRaises(ConfigError):
                load_api_token(path)
            with open(path, "w") as fh:
                fh.write("corto\n")
            with self.assertRaises(ConfigError):
                load_api_token(path)
            with open(path, "w") as fh:
                fh.write("x" * 20 + "\n")
            self.assertEqual(load_api_token(path), "x" * 20)

    def test_estado_y_esquema_sqlite(self):
        row = {"revoked_at": None, "downloads_completed": 0, "max_downloads": 1, "expires_at": 100}
        self.assertEqual(compute_state(row, 99), "active")
        self.assertEqual(compute_state(row, 100), "expired")
        self.assertEqual(compute_state({**row, "downloads_completed": 1}, 99), "exhausted")
        self.assertEqual(compute_state({**row, "downloads_completed": 1, "revoked_at": 5}, 99), "revoked")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "g.db")
            store = Store(path)
            self.assertEqual(store._db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(store._db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            store.close()
            self.assertEqual(Store(path)._db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
