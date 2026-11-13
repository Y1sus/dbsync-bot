# =============================================================================
# services/gateway/tests/test_cleanup.py — Pruebas de la limpieza periódica: alcance, retención y cuota
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import os
import dataclasses

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

    async def test_borra_viejos_sin_referencia(self):
        viejo = self.make("srv/viejo.sql.gz", 30)
        nuevo = self.make("srv/nuevo.sql.gz", 1)
        os.unlink(self.dump_path)
        result = self.run_cleanup()
        self.assertFalse(os.path.exists(viejo))
        self.assertTrue(os.path.exists(nuevo))
        self.assertEqual(result["files_deleted"], 1)
        self.assertIn("file_deleted", [e["event"] for e in self.audit_events()])

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

    async def test_no_borra_con_enlace_activo_ni_sesion_abierta(self):
        con_enlace = self.make("srv/viejo-activo.sql.gz", 50)
        con_sesion = self.make("srv/viejo-sesion.sql.gz", 50)
        sin_ref = self.make("srv/viejo-libre.sql.gz", 50)
        os.unlink(self.dump_path)
        await self.create_link(relpath="srv/viejo-activo.sql.gz", ttl_seconds=3 * 86400)
        link = await self.create_link(relpath="srv/viejo-sesion.sql.gz", ttl_seconds=60)
        await self.open_session(link)
        self.clock.advance(120)
        self.run_cleanup()
        self.assertTrue(os.path.exists(con_enlace))
        self.assertTrue(os.path.exists(con_sesion))
        self.assertFalse(os.path.exists(sin_ref))
        self.clock.advance(7200)
        self.run_cleanup()
        self.assertFalse(os.path.exists(con_sesion))
        self.assertTrue(os.path.exists(con_enlace))

    async def test_cuota_evicta_el_mas_viejo_sin_tocar_referenciados(self):
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

    async def test_cuota_no_borra_referenciados_aunque_exceda(self):
        self.ctx.config = dataclasses.replace(self.ctx.config, max_dumps_gb=0.000001)
        await self.create_link()
        self.run_cleanup()
        self.assertTrue(os.path.exists(self.dump_path))

    async def test_marca_expirados_y_purga_filas_viejas(self):
        link = await self.create_link(ttl_seconds=60)
        otro = await self.create_link(max_downloads=1)
        loc = await self.open_session(otro)
        await self.fetch(loc)
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
