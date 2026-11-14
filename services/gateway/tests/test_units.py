# =============================================================================
# services/gateway/tests/test_units.py — Pruebas unitarias: rangos, tokens, rutas, configuración y esquema
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import os
import tempfile
import unittest

from gateway import paths, tokens
from gateway.config import ConfigError, from_env, load_api_token
from gateway.limiter import MissLimiter, ip_key
from gateway.ranges import RangeNotSatisfiable, parse_range
from gateway.store import Store, _V3_TO_V4, compute_state
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

    def test_relpath_anidado_rechazado(self):
        os.makedirs(os.path.join(self.root, "srv", "sub"))
        with open(os.path.join(self.root, "srv", "sub", "x.sql.gz"), "wb") as fh:
            fh.write(b"datos")
        for rel in ["srv/sub/x.sql.gz", "a/b/c.sql.gz", "solo.sql.gz"]:
            self.assertEqual(self.code(rel), "invalid_request", rel)

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
        row = {"revoked_at": None, "max_downloads": 1, "expires_at": 100}
        one = [{"downloads_completed": 0}]
        self.assertEqual(compute_state(row, one, 99), "active")
        self.assertEqual(compute_state(row, one, 100), "expired")
        self.assertEqual(compute_state(row, [{"downloads_completed": 1}], 99), "exhausted")
        self.assertEqual(compute_state(row, [{"downloads_completed": 1}, {"downloads_completed": 0}], 99), "active")
        self.assertEqual(compute_state(row, [{"downloads_completed": 1}, {"downloads_completed": 1}], 99), "exhausted")
        self.assertEqual(compute_state({**row, "revoked_at": 5}, [{"downloads_completed": 1}], 99), "revoked")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "g.db")
            store = Store(path)
            self.assertEqual(store._db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(store._db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            store.close()
            self.assertEqual(Store(path)._db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")


V1_SCHEMA = """CREATE TABLE links (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, relpath TEXT NOT NULL,
filename TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT, created_at INTEGER NOT NULL,
expires_at INTEGER NOT NULL, max_downloads INTEGER NOT NULL, downloads_completed INTEGER NOT NULL DEFAULT 0,
created_by TEXT NOT NULL, job_id TEXT NOT NULL, label TEXT, revoked_at INTEGER, closed_at INTEGER%s);
CREATE INDEX links_relpath ON links(relpath);
CREATE TABLE sessions (id TEXT PRIMARY KEY, link_id TEXT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, counted INTEGER NOT NULL DEFAULT 0%s);
CREATE INDEX sessions_link ON sessions(link_id);
INSERT INTO links (id, token_hash, relpath, filename, size_bytes, sha256, created_at, expires_at, max_downloads,
downloads_completed, created_by, job_id) VALUES ('aaaaaaaaaaaa','h1','srv/a.sql.gz','a.sql.gz',5,'%s',1,100,2,1,'t','j');
INSERT INTO links (id, token_hash, relpath, filename, size_bytes, created_at, expires_at, max_downloads,
created_by, job_id, label, revoked_at, closed_at) VALUES ('bbbbbbbbbbbb','h2','srv/b.sql.gz','b.sql.gz',7,1,50,1,'t2','j2','rotulo',20,20);
INSERT INTO sessions (id, link_id, created_at, expires_at, counted%s) VALUES ('s1','aaaaaaaaaaaa',1,50,1%s);
PRAGMA user_version=%d;"""
V2_EXTRA = ", keep_file INTEGER NOT NULL DEFAULT 0"
V3_EXTRA = ", ranges TEXT NOT NULL DEFAULT '[]', budget_used INTEGER NOT NULL DEFAULT 0"


class MigracionTests(unittest.TestCase):
    def build(self, path, version):
        import sqlite3
        db = sqlite3.connect(path)
        base = min(version, 3)
        links_extra = V2_EXTRA if base >= 2 else ""
        sessions_extra, cols, vals = (V3_EXTRA, ", ranges, budget_used", ", '[[0,5]]', 10") if base == 3 else ("", "", "")
        db.executescript(V1_SCHEMA % (links_extra, sessions_extra, "c" * 64, cols, vals, base))
        if version == 4:
            db.executescript("BEGIN; " + _V3_TO_V4 + " PRAGMA user_version=4; COMMIT;")
        db.close()

    def migrate(self, version):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "g.db")
            self.build(path, version)
            store = Store(path)
            self.assertEqual(store._db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(store._db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            cols = {r["name"] for r in store._db.execute("PRAGMA table_info(links)")}
            self.assertFalse({"relpath", "filename", "size_bytes", "sha256", "downloads_completed"} & cols)
            link = store.get_link("aaaaaaaaaaaa", 50)
            self.assertEqual((link["keep_file"], link["state"], link["max_downloads"], len(link["files"])),
                             (0, "active", 2, 1))
            item = link["files"][0]
            self.assertEqual((item["idx"], item["relpath"], item["filename"], item["size_bytes"], item["sha256"]),
                             (0, "srv/a.sql.gz", "a.sql.gz", 5, "c" * 64))
            self.assertEqual((item["kind"], item["downloads_completed"], item["state"]), ("db", 1, "active"))
            revoked = store.get_link("bbbbbbbbbbbb", 50)
            self.assertEqual((revoked["state"], revoked["label"], revoked["files"][0]["sha256"]),
                             ("revoked", "rotulo", None))
            session = store.get_session("s1", "aaaaaaaaaaaa", 0)
            self.assertEqual((session["counted"], session["expires_at"], session["idx"]), (1, 50, 0))
            expected = ("[[0,5]]", 10) if version >= 3 else ("[]", 0)
            self.assertEqual((session["ranges"], session["budget_used"]), expected)
            self.assertEqual(session["sent_total"], expected[1])
            names = {r["name"] for r in store._db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertTrue({"link_files_relpath", "sessions_link"} <= names)
            _, sid = store.open_or_reuse_session("aaaaaaaaaaaa", 0, 10, 7200, 900)
            self.assertEqual(sid, "s1")
            made = store.create_link(lambda: [3, 4], "h3", [("srv/x.zip", None, "code"), ("srv/y.sql.gz", None, "db")],
                                     60, 1, "t", "j", None, 1)
            self.assertEqual([f["idx"] for f in made["files"]], [0, 1])
            store._db.execute("DELETE FROM links WHERE id='aaaaaaaaaaaa'")
            self.assertEqual(store._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(store._db.execute(
                "SELECT COUNT(*) FROM link_files WHERE link_id='aaaaaaaaaaaa'").fetchone()[0], 0)
            store.close()
            again = Store(path)
            self.assertEqual(again._db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertIsNotNone(again.get_link("bbbbbbbbbbbb", 50))
            again.close()

    def test_v1_a_v5(self):
        self.migrate(1)

    def test_v2_a_v5(self):
        self.migrate(2)

    def test_v3_a_v5_con_datos_reales(self):
        self.migrate(3)

    def test_v4_a_v5_con_datos_reales(self):
        self.migrate(4)

    def test_v3_conserva_el_estado_agotado_y_la_cobertura(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "g.db")
            self.build(path, 3)
            db = sqlite3.connect(path)
            db.execute("UPDATE links SET downloads_completed=2 WHERE id='aaaaaaaaaaaa'")
            db.commit()
            db.close()
            store = Store(path)
            link = store.get_link("aaaaaaaaaaaa", 50)
            self.assertEqual((link["state"], link["files"][0]["state"]), ("exhausted", "exhausted"))
            store.close()

    def test_version_desconocida_falla(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "g.db")
            db = sqlite3.connect(path)
            db.execute("PRAGMA user_version=9")
            db.close()
            with self.assertRaises(RuntimeError):
                Store(path)

    def test_archivos_wal_y_shm_son_0600_aun_con_umask_flojo(self):
        old = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "g.db")
                store = Store(path)
                store.create_link(lambda: [5], "h", [("srv/a.sql.gz", None, "db")], 60, 1, "t", "j", None, 1)
                for suffix in ("", "-wal", "-shm"):
                    if os.path.exists(path + suffix):
                        self.assertEqual(os.stat(path + suffix).st_mode & 0o777, 0o600, suffix)
                store.close()
        finally:
            os.umask(old)


class CoberturaTests(unittest.TestCase):
    def test_union_y_cobertura(self):
        from gateway.coverage import MAX_INTERVALS, covers, merge
        r = merge([], 500, 1000)
        self.assertFalse(covers(r, 1000))
        r = merge(r, 0, 500)
        self.assertEqual(r, [[0, 1000]])
        self.assertTrue(covers(r, 1000))
        self.assertEqual(merge([[0, 10]], 10, 20), [[0, 20]])
        self.assertEqual(merge([[0, 10]], 11, 20), [[0, 10], [11, 20]])
        self.assertEqual(merge([[0, 10]], 5, 5), [[0, 10]])
        self.assertFalse(covers([[0, 10], [11, 20]], 20))
        self.assertFalse(covers([[1, 20]], 20))
        r = []
        for i in range(MAX_INTERVALS * 3):
            r = merge(r, i * 10, i * 10 + 1 + (i % 3))
        self.assertLessEqual(len(r), MAX_INTERVALS)

    def test_reserva_y_devolucion_atomicas(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "g.db"))
            link = store.create_link(lambda: [100], "h", [("srv/a.sql.gz", None, "db")], 3600, 5, "t", "j", None, 1000)
            status, sid = store.open_or_reuse_session(link["id"], 0, 1000, 7200, 900)
            self.assertEqual(status, "ok")
            self.assertEqual(store.reserve_bytes(sid, 100, 100, 1000), "ok")
            self.assertEqual(store.reserve_bytes(sid, 100, 100, 1000), "ok")
            self.assertEqual(store.reserve_bytes(sid, 1, 100, 1000), "budget")
            store.record_sent(sid, 0, 40, 100, 40, 100, 1000, 900)
            self.assertEqual(store.get_session(sid, link["id"], 0)["budget_used"], 140)
            self.assertEqual(store.reserve_bytes(sid, 60, 100, 1000), "ok")
            self.assertEqual(store.reserve_bytes(sid, 1, 100, 1000), "budget")
            self.assertEqual(store.reserve_bytes(sid, 1, 100, 99999), "session")
            self.assertEqual(store.reserve_bytes("nope", 1, 100, 1000), "session")
            store.close()

    def test_vencimiento_de_sesion_acotado_por_el_enlace(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "g.db"))
            link = store.create_link(lambda: [100], "h", [("srv/a.sql.gz", None, "db")], 60, 1, "t", "j", None, 1000)
            _, sid = store.open_or_reuse_session(link["id"], 0, 1000, 7200, 900)
            self.assertEqual(store.get_session(sid, link["id"], 0)["expires_at"], 1000 + 60 + 900)
            long = store.create_link(lambda: [100], "h2", [("srv/b.sql.gz", None, "db")], 604800, 1, "t", "j", None, 1000)
            _, sid2 = store.open_or_reuse_session(long["id"], 0, 1000, 7200, 900)
            self.assertEqual(store.get_session(sid2, long["id"], 0)["expires_at"], 1000 + 7200)
            store.close()


class LimitadorTests(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.lim = MissLimiter(3, 60, 300, 5, lambda: self.t[0])

    def test_bloquea_al_superar_el_limite_y_desbloquea_por_tiempo(self):
        for _ in range(3):
            self.assertFalse(self.lim.miss("1.1.1.1"))
        self.assertEqual(self.lim.blocked("1.1.1.1"), 0)
        self.assertTrue(self.lim.miss("1.1.1.1"))
        self.assertEqual(self.lim.blocked("1.1.1.1"), 300)
        self.assertEqual(self.lim.blocked("2.2.2.2"), 0)
        self.t[0] += 299
        self.assertEqual(self.lim.blocked("1.1.1.1"), 1)
        self.t[0] += 1
        self.assertEqual(self.lim.blocked("1.1.1.1"), 0)

    def test_ventana_deslizante(self):
        for _ in range(3):
            self.lim.miss("1.1.1.1")
        self.t[0] += 61
        for _ in range(3):
            self.assertFalse(self.lim.miss("1.1.1.1"))
        self.assertEqual(self.lim.blocked("1.1.1.1"), 0)

    def test_tabla_acotada_y_bloqueados_sobreviven_al_desalojo(self):
        for _ in range(4):
            self.lim.miss("9.9.9.9")
        for i in range(50):
            self.lim.miss(f"10.0.0.{i}")
        self.assertLessEqual(self.lim.size(), 10)
        self.assertGreater(self.lim.blocked("9.9.9.9"), 0)
        self.t[0] += 400
        self.assertEqual(self.lim.blocked("9.9.9.9"), 0)
        self.assertEqual(self.lim.size(), 5)

    def test_bloqueos_tambien_acotados(self):
        for i in range(30):
            for _ in range(4):
                self.lim.miss(f"10.1.0.{i}")
        self.assertLessEqual(len(self.lim._blocked), 5)
        self.assertGreater(self.lim.blocked("10.1.0.29"), 0)

    def test_desalojo_amortizado_con_tabla_grande(self):
        import time
        big = MissLimiter(20, 60, 300, 10000, lambda: 1000.0)
        for i in range(10000):
            big.miss(f"10.{i // 65536 % 256}.{i // 256 % 256}.{i % 256}")
        started = time.perf_counter()
        for i in range(5000):
            big.miss(f"172.16.{i // 256}.{i % 256}")
        self.assertLess((time.perf_counter() - started) / 5000, 0.0002)
        self.assertEqual(len(big._hits), 10000)

    def test_ipv4_mapeada_comparte_clave_con_su_ipv4(self):
        self.assertEqual(ip_key("::ffff:203.0.113.9"), "203.0.113.9")
        for _ in range(4):
            self.lim.miss("::ffff:203.0.113.9")
        self.assertGreater(self.lim.blocked("203.0.113.9"), 0)

    def test_ipv6_se_agrupa_por_prefijo_64(self):
        self.assertEqual(ip_key("2001:db8::1"), ip_key("2001:db8::ffff"))
        self.assertNotEqual(ip_key("2001:db8:0:1::1"), ip_key("2001:db8::1"))
        self.assertEqual(ip_key("203.0.113.9"), "203.0.113.9")
        for i in range(4):
            self.lim.miss(f"2001:db8::{i + 1}")
        self.assertGreater(self.lim.blocked("2001:db8::99"), 0)


class ConfigNuevaTests(unittest.TestCase):
    def test_variables_nuevas(self):
        base = {"GATEWAY_PUBLIC_BASE_URL": "https://x.test", "GATEWAY_API_TOKEN_FILE": "/t"}
        cfg = from_env(base)
        self.assertEqual((cfg.post_download_grace, cfg.miss_limit, cfg.block_seconds, cfg.max_streams),
                         (900, 20, 300, 6))
        self.assertEqual((cfg.max_streams_per_link, cfg.min_rate_bytes, cfg.max_stream_seconds, cfg.write_timeout),
                         (2, 32768, 7200, 10.0))
        self.assertEqual((cfg.audit_max_mb, cfg.audit_keep), (50.0, 5))
        cfg = from_env({**base, "GATEWAY_POST_DOWNLOAD_GRACE_SECONDS": "2", "GATEWAY_MISS_LIMIT": "3",
                        "GATEWAY_BLOCK_SECONDS": "7", "GATEWAY_MAX_STREAMS": "9",
                        "GATEWAY_CLEANUP_INTERVAL_SECONDS": "1"})
        self.assertEqual((cfg.post_download_grace, cfg.miss_limit, cfg.block_seconds, cfg.max_streams,
                          cfg.cleanup_interval), (2, 3, 7, 9, 1))
        cfg = from_env({**base, "GATEWAY_MAX_STREAMS_PER_LINK": "1", "GATEWAY_MIN_RATE_BYTES": "5",
                        "GATEWAY_MAX_STREAM_SECONDS": "9", "GATEWAY_AUDIT_MAX_MB": "0.5", "GATEWAY_AUDIT_KEEP": "2"})
        self.assertEqual((cfg.max_streams_per_link, cfg.min_rate_bytes, cfg.max_stream_seconds, cfg.audit_max_mb,
                          cfg.audit_keep), (1, 5, 9, 0.5, 2))
        for name in ("GATEWAY_MISS_LIMIT", "GATEWAY_MAX_STREAMS", "GATEWAY_BLOCK_SECONDS",
                     "GATEWAY_MAX_STREAMS_PER_LINK", "GATEWAY_AUDIT_KEEP", "GATEWAY_MAX_STREAM_SECONDS"):
            with self.assertRaises(ConfigError):
                from_env({**base, name: "0"})
        with self.assertRaises(ConfigError):
            from_env({**base, "GATEWAY_POST_DOWNLOAD_GRACE_SECONDS": "-1"})
