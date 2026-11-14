# =============================================================================
# services/gateway/tests/test_cleanup.py — Pruebas de la limpieza: borrado al terminar el enlace, retención y cuota
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import dataclasses
import os

from gateway import cleanup

from .support import GatewayCase

HORA = 3600


class LimpiezaTests(GatewayCase):
    def make(self, relpath, age_hours, size=100):
        path = self.write_dump(relpath, b"x" * size)
        stamp = self.clock() - age_hours * HORA
        os.utime(path, (stamp, stamp))
        return path

    def run_cleanup(self):
        return cleanup.run_once(self.ctx)

    def deleted_reasons(self):
        return [e["reason"] for e in self.audit_events() if e["event"] == "file_deleted"]

    async def test_borra_viejos_sin_enlace_por_retencion(self):
        viejo = self.make("srv/viejo.sql.gz", 30)
        nuevo = self.make("srv/nuevo.sql.gz", 1)
        os.unlink(self.dump_path)
        result = self.run_cleanup()
        self.assertFalse(os.path.exists(viejo))
        self.assertTrue(os.path.exists(nuevo))
        self.assertEqual(result["files_deleted"], 1)
        self.assertEqual(self.deleted_reasons(), ["retention"])

    async def test_nunca_toca_part_locks_symlinks_ni_otros(self):
        part = self.make("srv/x.sql.gz.part", 100)
        lock = self.make(".locks/y.sql.gz", 100)
        texto = self.make("srv/z.txt", 100)
        oculto = self.make("srv/.h.sql.gz", 100)
        fuera = os.path.join(self.tmp.name, "fuera.sql.gz")
        with open(fuera, "wb") as fh:
            fh.write(b"datos")
        stamp = self.clock() - 100 * HORA
        os.utime(fuera, (stamp, stamp))
        os.symlink(fuera, os.path.join(self.dumps, "srv", "ln.sql.gz"))
        os.symlink(self.tmp.name, os.path.join(self.dumps, "dirln"))
        raiz = os.path.join(self.dumps, "suelto.sql.gz")
        with open(raiz, "wb") as fh:
            fh.write(b"x")
        os.utime(raiz, (stamp, stamp))
        os.unlink(self.dump_path)
        self.run_cleanup()
        for path in (part, lock, texto, oculto, fuera, raiz):
            self.assertTrue(os.path.exists(path), path)
        self.assertTrue(os.path.islink(os.path.join(self.dumps, "srv", "ln.sql.gz")))

    async def test_symlink_con_enlace_terminal_no_se_borra(self):
        fuera = os.path.join(self.tmp.name, "fuera.sql.gz")
        with open(fuera, "wb") as fh:
            fh.write(b"datos")
        link = await self.create_link()
        os.unlink(self.dump_path)
        os.symlink(fuera, self.dump_path)
        await self.api("DELETE", f"/v1/links/{link['id']}")
        self.run_cleanup()
        self.assertTrue(os.path.exists(fuera))
        self.assertTrue(os.path.islink(self.dump_path))

    async def test_cuota_evicta_el_mas_viejo_sin_tocar_con_enlace(self):
        mb = 1024 * 1024
        self.ctx.config = dataclasses.replace(self.ctx.config, max_dumps_gb=2.5 * mb / 1024 ** 3)
        os.unlink(self.dump_path)
        a = self.make("srv/a1.sql.gz", 5, mb)
        b = self.make("srv/b1.sql.gz", 4, mb)
        c = self.make("srv/c1.sql.gz", 3, mb)
        d = self.make("srv/d1.sql.gz", 2, mb)
        await self.create_link(relpath="srv/a1.sql.gz")
        self.run_cleanup()
        self.assertTrue(os.path.exists(a))
        self.assertFalse(os.path.exists(b))
        self.assertFalse(os.path.exists(c))
        self.assertTrue(os.path.exists(d))
        self.assertEqual(self.deleted_reasons(), ["quota", "quota"])

    async def test_cuota_no_borra_con_enlace_activo_aunque_exceda(self):
        self.ctx.config = dataclasses.replace(self.ctx.config, max_dumps_gb=0.000001)
        await self.create_link()
        self.run_cleanup()
        self.assertTrue(os.path.exists(self.dump_path))

    async def test_marca_expirados_y_purga_filas_viejas(self):
        link = await self.create_link(ttl_seconds=60)
        otro = await self.create_link(max_downloads=1)
        loc = await self.open_session(otro)
        await self.fetch(loc)
        await self.counted(otro)
        self.clock.advance(61)
        result = self.run_cleanup()
        self.assertEqual(result["links_closed"], 2)
        events = [(e["event"], e["link_id"]) for e in self.audit_events()]
        self.assertIn(("link_expired", link["id"]), events)
        self.assertIn(("link_expired", otro["id"]), events)
        self.assertEqual(self.run_cleanup()["links_closed"], 0)
        self.assertEqual(len((await self.api("GET", "/v1/links?state=all"))[1]["links"]), 2)
        self.clock.advance(7 * 86400 + 1)
        self.assertEqual(self.run_cleanup()["rows_purged"], 2)
        self.assertEqual((await self.api("GET", "/v1/links?state=all"))[1]["links"], [])
        self.assertEqual(self.ctx.store._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)


class BorradoAlTerminarElEnlaceTests(GatewayCase):
    def run_cleanup(self):
        return cleanup.run_once(self.ctx)

    def reasons(self):
        return [e["reason"] for e in self.audit_events() if e["event"] == "file_deleted"]

    async def test_borra_tras_expirar_sin_haber_tenido_sesion(self):
        await self.create_link(ttl_seconds=60)
        self.run_cleanup()
        self.assertTrue(os.path.exists(self.dump_path))
        self.clock.advance(61)
        self.run_cleanup()
        self.assertFalse(os.path.exists(self.dump_path))
        self.assertEqual(self.reasons(), ["link_expired"])

    async def test_expirar_con_sesion_abierta_espera_a_que_venza(self):
        link = await self.create_link(ttl_seconds=60)
        await self.open_session(link)
        self.clock.advance(61)
        self.run_cleanup()
        self.assertTrue(os.path.exists(self.dump_path))
        self.clock.advance(7200)
        self.run_cleanup()
        self.assertFalse(os.path.exists(self.dump_path))
        self.assertEqual(self.reasons(), ["link_expired"])

    async def test_agotar_espera_la_gracia_posterior_al_conteo(self):
        grace = self.ctx.config.post_download_grace
        link = await self.create_link()
        loc = await self.open_session(link)
        status, _, body = await self.fetch(loc)
        self.assertEqual((status, body), (200, self.payload))
        await self.counted(link)
        self.run_cleanup()
        self.assertTrue(os.path.exists(self.dump_path))
        self.clock.advance(grace - 1)
        status, _, _ = await self.fetch(loc, "bytes=0-9")
        self.assertEqual(status, 206)
        await self.idle()
        self.run_cleanup()
        self.assertTrue(os.path.exists(self.dump_path))
        self.clock.advance(1)
        self.run_cleanup()
        self.assertFalse(os.path.exists(self.dump_path))
        self.assertEqual(self.reasons(), ["link_exhausted"])
        status, _, _ = await self.fetch(loc, "bytes=0-9")
        self.assertEqual(status, 404)

    async def test_gracia_configurable_y_nunca_pasa_del_ttl_de_sesion(self):
        self.ctx.config = dataclasses.replace(self.ctx.config, post_download_grace=5, session_ttl=3)
        link = await self.create_link()
        loc = await self.open_session(link)
        await self.fetch(loc)
        await self.counted(link)
        row = self.ctx.store._db.execute("SELECT created_at, expires_at FROM sessions").fetchone()
        self.assertEqual(row["expires_at"] - row["created_at"], 3)
        self.ctx.config = dataclasses.replace(self.ctx.config, post_download_grace=5, session_ttl=7200)
        other = await self.create_link(ttl_seconds=7200)
        loc2 = await self.open_session(other)
        self.clock.advance(10)
        await self.fetch(loc2)
        await self.counted(other)
        row = self.ctx.store._db.execute(
            "SELECT s.expires_at FROM sessions s JOIN links l ON l.id=s.link_id WHERE l.id=?", (other["id"],)
        ).fetchone()
        self.assertEqual(row["expires_at"], self.clock() + 5)

    async def test_revocar_borra_de_inmediato(self):
        link = await self.create_link()
        await self.open_session(link)
        self.assertEqual((await self.api("DELETE", f"/v1/links/{link['id']}"))[0], 204)
        self.assertFalse(os.path.exists(self.dump_path))
        self.assertEqual(self.reasons(), ["link_revoked"])

    async def test_revocar_con_keep_file_conserva_incluso_tras_la_pasada(self):
        link = await self.create_link()
        self.assertEqual((await self.api("DELETE", f"/v1/links/{link['id']}?keep_file=1"))[0], 204)
        self.run_cleanup()
        self.assertTrue(os.path.exists(self.dump_path))
        self.assertEqual(self.reasons(), [])

    async def test_keep_file_cae_en_retencion_cuando_se_purgan_las_filas(self):
        link = await self.create_link()
        await self.api("DELETE", f"/v1/links/{link['id']}?keep_file=1")
        self.clock.advance(7 * 86400 + 1)
        os.utime(self.dump_path, (self.clock() - 30 * HORA, self.clock() - 30 * HORA))
        self.run_cleanup()
        self.assertFalse(os.path.exists(self.dump_path))
        self.assertEqual(self.reasons(), ["retention"])

    async def test_delete_sin_keep_file_despues_de_conservar_borra(self):
        link = await self.create_link()
        await self.api("DELETE", f"/v1/links/{link['id']}?keep_file=1")
        self.assertTrue(os.path.exists(self.dump_path))
        self.assertEqual((await self.api("DELETE", f"/v1/links/{link['id']}"))[0], 204)
        self.assertFalse(os.path.exists(self.dump_path))

    async def test_no_borra_si_otro_enlace_activo_comparte_el_relpath(self):
        a = await self.create_link()
        b = await self.create_link()
        await self.api("DELETE", f"/v1/links/{a['id']}")
        self.run_cleanup()
        self.assertTrue(os.path.exists(self.dump_path))
        await self.api("DELETE", f"/v1/links/{b['id']}")
        self.assertFalse(os.path.exists(self.dump_path))

    async def test_no_borra_con_sesion_abierta_de_otro_enlace_cerrado(self):
        a = await self.create_link(ttl_seconds=60)
        b = await self.create_link(ttl_seconds=60)
        await self.open_session(b)
        await self.api("DELETE", f"/v1/links/{a['id']}")
        self.clock.advance(61)
        self.run_cleanup()
        self.assertTrue(os.path.exists(self.dump_path))
        self.clock.advance(7200)
        self.run_cleanup()
        self.assertFalse(os.path.exists(self.dump_path))

    async def test_no_borra_con_stream_activo_y_reintenta_en_la_siguiente_pasada(self):
        link = await self.create_link()
        self.ctx.active.enter(link["id"])
        self.assertEqual((await self.api("DELETE", f"/v1/links/{link['id']}"))[0], 204)
        self.assertTrue(os.path.exists(self.dump_path))
        self.run_cleanup()
        self.assertTrue(os.path.exists(self.dump_path))
        self.ctx.active.exit(link["id"])
        self.run_cleanup()
        self.assertFalse(os.path.exists(self.dump_path))
        self.assertEqual(self.reasons(), ["link_revoked"])

    async def test_archivo_sin_enlaces_solo_por_retencion(self):
        extra = self.write_dump("srv/huerfano.sql.gz", b"x" * 50)
        reciente = self.clock() - HORA
        for path in (extra, self.dump_path):
            os.utime(path, (reciente, reciente))
        self.run_cleanup()
        self.assertTrue(os.path.exists(extra))
        stamp = self.clock() - 25 * HORA
        os.utime(extra, (stamp, stamp))
        self.run_cleanup()
        self.assertFalse(os.path.exists(extra))
        self.assertTrue(os.path.exists(self.dump_path))
        self.assertEqual(self.reasons(), ["retention"])

    async def test_reintenta_si_el_borrado_falla(self):
        link = await self.create_link(ttl_seconds=60)
        self.clock.advance(61)
        os.chmod(os.path.dirname(self.dump_path), 0o500)
        try:
            if os.access(os.path.dirname(self.dump_path), os.W_OK):
                self.skipTest("el usuario ignora permisos de directorio")
            self.run_cleanup()
            self.assertTrue(os.path.exists(self.dump_path))
        finally:
            os.chmod(os.path.dirname(self.dump_path), 0o700)
        self.run_cleanup()
        self.assertFalse(os.path.exists(self.dump_path))
        self.assertEqual(self.reasons(), ["link_expired"])
        del link

    async def test_revocar_cerrado_es_idempotente_y_desconocido_404(self):
        link = await self.create_link()
        self.assertEqual((await self.api("DELETE", f"/v1/links/{link['id']}"))[0], 204)
        self.assertEqual((await self.api("DELETE", f"/v1/links/{link['id']}"))[0], 204)
        self.assertEqual((await self.api("DELETE", f"/v1/links/{link['id']}?keep_file=1"))[0], 204)
        status, data = await self.api("DELETE", "/v1/links/ffffffffffff")
        self.assertEqual((status, data["error"]["code"]), (404, "not_found"))
        events = [e["event"] for e in self.audit_events()]
        self.assertEqual(events.count("link_revoked"), 1)

    async def test_revocar_un_enlace_agotado_cierra_su_gracia_y_borra(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        await self.fetch(loc)
        self.assertTrue(os.path.exists(self.dump_path))
        await self.api("DELETE", f"/v1/links/{link['id']}")
        self.assertFalse(os.path.exists(self.dump_path))

    async def test_create_link_y_borrado_comparten_candado(self):
        link = await self.create_link(ttl_seconds=60)
        self.clock.advance(61)
        self.run_cleanup()
        self.assertFalse(os.path.exists(self.dump_path))
        status, data = await self.api("POST", "/v1/links",
                                      {"relpath": "srv/a.sql.gz", "created_by": "t", "job_id": "j"})
        self.assertEqual((status, data["error"]["code"]), (404, "file_not_found"))
        del link

    async def sleep(self, seconds):
        import asyncio
        await asyncio.sleep(seconds)
