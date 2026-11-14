# =============================================================================
# services/gateway/tests/test_audit.py — Pruebas de la auditoría: rotación, límite por IP y permisos
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import glob
import json
import os
import stat
import tempfile
import unittest

from gateway.audit import Audit

from .support import GatewayCase


class FakeTime:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def read_all(path):
    lines = []
    for name in sorted(glob.glob(path + "*")):
        with open(name, encoding="utf-8") as fh:
            lines += [json.loads(line) for line in fh]
    return lines


def disk_bytes(path):
    return sum(os.path.getsize(name) for name in glob.glob(path + "*"))


class AuditoriaUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "audit.jsonl")
        self.clock = FakeTime()

    def tearDown(self):
        self.tmp.cleanup()

    def test_5000_eventos_hostiles_no_superan_el_tope_en_disco(self):
        audit = Audit(self.path, self.clock, max_bytes=20_000, keep=2)
        for i in range(5000):
            audit.log("origin_rejected", None, f"10.{i // 256 % 256}.{i % 256}.1", "ua" * 300, reason="x" * 100)
        audit.close()
        self.assertLessEqual(disk_bytes(self.path), 3 * 20_000 + 1000)
        self.assertLessEqual(len(glob.glob(self.path + "*")), 3)
        for name in glob.glob(self.path + "*"):
            self.assertEqual(stat.S_IMODE(os.stat(name).st_mode), 0o600)

    def test_5000_eventos_de_una_sola_ip_comparten_un_tope_de_5(self):
        audit = Audit(self.path, self.clock, max_bytes=10_000_000, keep=2)
        for _ in range(5000):
            audit.log("origin_rejected", None, "198.51.100.7", "ua")
            audit.log("rate_limited", None, "198.51.100.7", "ua")
        audit.close()
        events = read_all(self.path)
        self.assertEqual(len(events), 5)
        self.assertEqual({e["event"] for e in events}, {"origin_rejected", "rate_limited"})
        self.assertLess(disk_bytes(self.path), 5000)

    def test_rotar_direcciones_de_un_mismo_slash_64_o_ipv4_mapeada_no_evade_el_tope(self):
        audit = Audit(self.path, self.clock, max_bytes=10_000_000, keep=2)
        for i in range(5000):
            audit.log("origin_rejected", None, f"2001:db8:0:1::{i:x}", "ua")
            audit.log("origin_rejected", None, "::ffff:203.0.113.9" if i % 2 else "203.0.113.9", "ua")
        audit.close()
        events = read_all(self.path)
        self.assertEqual(len(events), 10)

    def test_eventos_normales_nunca_se_suprimen_aunque_haya_ataque(self):
        audit = Audit(self.path, self.clock, max_bytes=10_000_000, keep=2)
        normales = ["link_created", "link_revoked", "landing_view", "download_started", "download_completed",
                    "download_aborted", "file_deleted", "link_expired"]
        for i in range(5000):
            audit.log("origin_rejected", None, "198.51.100.7", "ua")
            audit.log(normales[i % len(normales)], "aaaaaaaaaaaa", "198.51.100.7", "ua")
        audit.close()
        events = read_all(self.path)
        self.assertEqual(sum(1 for e in events if e["event"] in normales), 5000)
        self.assertEqual(sum(1 for e in events if e["event"] == "origin_rejected"), 5)
        self.assertTrue(all("suppressed" not in e for e in events if e["event"] in normales))

    def test_resumen_suppressed_en_el_siguiente_evento_permitido(self):
        audit = Audit(self.path, self.clock, max_bytes=10_000_000, keep=2)
        for _ in range(12):
            audit.log("rate_limited", None, "198.51.100.7", "ua")
        self.clock.t += 61
        audit.log("rate_limited", None, "198.51.100.7", "ua")
        audit.log("rate_limited", None, "198.51.100.8", "ua")
        audit.close()
        events = read_all(self.path)
        self.assertEqual(len(events), 7)
        self.assertEqual(events[5].get("suppressed"), 7)
        self.assertNotIn("suppressed", events[6])
        self.assertTrue(all("suppressed" not in e for e in events[:5]))

    def test_ua_truncado_a_120_y_tabla_de_ips_acotada(self):
        audit = Audit(self.path, self.clock, max_bytes=10_000_000, keep=2)
        audit.log("landing_view", "a", "1.1.1.1", "u" * 500)
        for i in range(12000):
            audit.log("origin_rejected", None, f"ip-{i}", "ua")
        self.assertLessEqual(len(audit._throttle), 10000)
        audit.close()
        self.assertEqual(len(read_all(self.path)[0]["ua"]), 120)

    def test_rotacion_conserva_keep_archivos_y_reabre_en_0600(self):
        audit = Audit(self.path, self.clock, max_bytes=1000, keep=3)
        for i in range(200):
            audit.log("download_started", "aaaaaaaaaaaa", "1.1.1.1", "ua", i)
        audit.close()
        names = sorted(os.path.basename(n) for n in glob.glob(self.path + "*"))
        self.assertEqual(names, ["audit.jsonl", "audit.jsonl.1", "audit.jsonl.2", "audit.jsonl.3"])
        newest = read_all(self.path)
        self.assertEqual(max(e["bytes"] for e in newest), 199)

    def test_reabrir_continua_el_tamano_del_archivo_existente(self):
        audit = Audit(self.path, self.clock, max_bytes=500, keep=1)
        for i in range(5):
            audit.log("download_started", "a", "1.1.1.1", "ua", i)
        audit.close()
        again = Audit(self.path, self.clock, max_bytes=500, keep=1)
        for i in range(20):
            again.log("download_started", "a", "1.1.1.1", "ua", i)
        again.close()
        self.assertLessEqual(disk_bytes(self.path), 2 * 500 + 300)


class AuditoriaHttpTests(GatewayCase):
    async def test_post_hostil_repetido_no_llena_la_auditoria(self):
        link = await self.create_link()
        before = os.path.getsize(os.path.join(self.data, "audit.jsonl"))
        for _ in range(60):
            status, _ = await self.post(link, {"Origin": "http://evil.test", "User-Agent": "x" * 400})
            self.assertEqual(status, 403)
        events = [e for e in self.audit_events() if e["event"] == "origin_rejected"]
        self.assertEqual(len(events), 5)
        self.assertLessEqual(len(events[0]["ua"]), 120)
        self.assertLess(os.path.getsize(os.path.join(self.data, "audit.jsonl")) - before, 5 * 600)
        self.assertEqual((await self.post(link))[0], 303)
        self.assertIn("download_started", [e["event"] for e in await self._full_download(link)])

    async def _full_download(self, link):
        loc = await self.open_session(link)
        await self.fetch(loc)
        await self.counted(link)
        return self.audit_events()


class PermisosTests(GatewayCase):
    async def test_data_dir_0700_y_umask_077(self):
        self.assertEqual(os.umask(0o077), 0o077)
        self.assertEqual(stat.S_IMODE(os.stat(self.data).st_mode), 0o700)
        for name in os.listdir(self.data):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.data, name)).st_mode), 0o600, name)
        self.assertTrue(any(n.endswith("-wal") for n in os.listdir(self.data)))

    async def test_build_context_endurece_un_data_dir_existente(self):
        from gateway.context import build_context
        import dataclasses
        loose = os.path.join(self.tmp.name, "otro-data")
        os.makedirs(loose, mode=0o755)
        os.chmod(loose, 0o755)
        ctx = build_context(dataclasses.replace(self.config, data_dir=loose))
        try:
            self.assertEqual(stat.S_IMODE(os.stat(loose).st_mode), 0o700)
        finally:
            ctx.close()
