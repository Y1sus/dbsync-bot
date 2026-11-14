# =============================================================================
# services/gateway/tests/test_multi.py — Pruebas de enlaces multi-archivo (v2): conteo por archivo, rutas y borrado
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import os

from gateway import cleanup

from .support import GatewayCase


class MultiBase(GatewayCase):
    CONFIG = {"max_streams": 30, "max_streams_per_link": 30}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.zip_payload = os.urandom(600)
        self.zip_path = self.write_dump("_gitlab/c.zip", self.zip_payload)

    async def two(self, **kwargs):
        return await self.create_link(files=[{"relpath": "srv/a.sql.gz", "sha256": "1" * 64},
                                             {"relpath": "_gitlab/c.zip", "sha256": "2" * 64}], **kwargs)

    async def files(self, link):
        return (await self.link_state(link))["files"]

    async def wait_counts(self, link, expected):
        for _ in range(300):
            got = [f["downloads_completed"] for f in await self.files(link)]
            if got == expected:
                return got
            await asyncio.sleep(0.01)
        return got

    def reasons(self):
        return [(e["relpath"], e["reason"]) for e in self.audit_events() if e["event"] == "file_deleted"]


class ConteoPorArchivoTests(MultiBase):
    async def test_registro_v2_con_dos_archivos(self):
        link = await self.two(max_downloads=2)
        self.assertEqual([(f["index"], f["relpath"], f["filename"], f["kind"], f["size_bytes"], f["sha256"],
                           f["downloads_completed"], f["state"]) for f in link["files"]],
                         [(0, "srv/a.sql.gz", "a.sql.gz", "db", 1000, "1" * 64, 0, "active"),
                          (1, "_gitlab/c.zip", "c.zip", "code", 600, "2" * 64, 0, "active")])
        self.assertEqual(link["max_downloads"], 2)
        _, listing = await self.api("GET", "/v2/links")
        self.assertEqual(listing["links"][0]["files"], link["files"])

    async def test_conteo_cobertura_y_presupuesto_son_independientes(self):
        link = await self.two(max_downloads=3)
        loc0, loc1 = await self.open_session(link, 0), await self.open_session(link, 1)
        self.assertRegex(loc0, rf"^/d/{link['token']}/0/f/[0-9a-f]{{32}}$")
        self.assertRegex(loc1, rf"^/d/{link['token']}/1/f/[0-9a-f]{{32}}$")
        self.assertNotEqual(loc0.rsplit("/", 1)[1], loc1.rsplit("/", 1)[1])
        await self.fetch(loc0, "bytes=500-")
        await self.fetch(loc1, "bytes=0-299")
        await self.idle()
        self.assertEqual([f["downloads_completed"] for f in await self.files(link)], [0, 0])
        await self.fetch(loc1, "bytes=300-")
        self.assertEqual(await self.wait_counts(link, [0, 1]), [0, 1])
        await self.fetch(loc0, "bytes=0-499")
        self.assertEqual(await self.wait_counts(link, [1, 1]), [1, 1])
        _, _, body = await self.fetch(loc0)
        self.assertEqual(body, self.payload)
        _, _, body = await self.fetch(loc1)
        self.assertEqual(body, self.zip_payload)
        self.assertEqual((await self.fetch(loc0))[0], 410)
        self.assertEqual((await self.fetch(loc1))[0], 410)

    async def test_presupuesto_de_un_archivo_no_afecta_al_otro(self):
        link = await self.two(max_downloads=5)
        loc0, loc1 = await self.open_session(link, 0), await self.open_session(link, 1)
        for _ in range(2):
            self.assertEqual((await self.fetch(loc0))[0], 200)
        status, _, body = await self.fetch(loc0)
        self.assertEqual(status, 410)
        self.assertIn("Esta descarga ya envió todo lo permitido".encode(), body)
        for _ in range(2):
            self.assertEqual((await self.fetch(loc1))[0], 200)
        self.assertEqual((await self.fetch(loc1))[0], 410)

    async def test_el_enlace_pasa_a_exhausted_solo_cuando_ambos_agotan(self):
        link = await self.two()
        loc0, loc1 = await self.open_session(link, 0), await self.open_session(link, 1)
        await self.fetch(loc0)
        await self.wait_counts(link, [1, 0])
        data = await self.link_state(link)
        self.assertEqual((data["state"], [f["state"] for f in data["files"]]), ("active", ["exhausted", "active"]))
        self.assertEqual([x["id"] for x in (await self.api("GET", "/v2/links"))[1]["links"]], [link["id"]])
        async with self.http.get(f"{self.pub}/d/{link['token']}") as resp:
            self.assertEqual(resp.status, 200)
        async with self.http.post(f"{self.pub}/d/{link['token']}/0", allow_redirects=False) as resp:
            self.assertEqual(resp.status, 410)
            self.assertIn("Este archivo ya se descargó", await resp.text())
        self.assertEqual((await self.post(link, index=1))[0], 303)
        await self.fetch(loc1)
        await self.wait_counts(link, [1, 1])
        data = await self.link_state(link)
        self.assertEqual((data["state"], [f["state"] for f in data["files"]]), ("exhausted", ["exhausted"] * 2))
        async with self.http.get(f"{self.pub}/d/{link['token']}") as resp:
            self.assertEqual(resp.status, 410)
            self.assertIn("Este enlace ya se usó", await resp.text())

    async def test_sesion_de_un_archivo_no_sirve_en_la_ruta_de_otro(self):
        link = await self.two(max_downloads=2)
        loc0, loc1 = await self.open_session(link, 0), await self.open_session(link, 1)
        sid0 = loc0.rsplit("/", 1)[1]
        async with self.http.get(f"{self.pub}/d/{link['token']}/1/f/{sid0}") as resp:
            self.assertEqual(resp.status, 404)
        self.assertEqual((await self.fetch(loc1, "bytes=0-9"))[0], 206)

    async def test_auditoria_con_indice_y_kind_sin_token(self):
        link = await self.two()
        loc1 = await self.open_session(link, 1)
        await self.fetch(loc1)
        await self.wait_counts(link, [0, 1])
        await self.idle()
        events = self.audit_events()
        created = [e for e in events if e["event"] == "link_created"][0]
        self.assertEqual(created["files"], 2)
        for name in ("download_started", "download_completed"):
            event = [e for e in events if e["event"] == name][0]
            self.assertEqual((event["file_index"], event["kind"]), (1, "code"))
        text = "\n".join(str(e) for e in events)
        self.assertNotIn(link["token"], text)

    async def test_cuatro_archivos_y_tipos_por_sufijo(self):
        self.write_dump("_gitlab/d.tar.gz", b"t" * 50)
        self.write_dump("srv/e.sql.gz", b"e" * 60)
        link = await self.create_link(files=[{"relpath": "srv/a.sql.gz"}, {"relpath": "_gitlab/c.zip"},
                                             {"relpath": "_gitlab/d.tar.gz"}, {"relpath": "srv/e.sql.gz"}])
        self.assertEqual([f["kind"] for f in link["files"]], ["db", "code", "code", "db"])
        self.assertEqual([f["index"] for f in link["files"]], [0, 1, 2, 3])
        for index in range(4):
            self.assertEqual((await self.post(link, index=index))[0], 303)


class RutasTests(MultiBase):
    async def test_compatibilidad_de_un_archivo(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        self.assertRegex(loc, rf"^/d/{link['token']}/f/[0-9a-f]{{32}}$")
        sid = loc.rsplit("/", 1)[1]
        status, _, body = await self.fetch(loc)
        self.assertEqual((status, body), (200, self.payload))
        status, _, body = await self.fetch(f"/d/{link['token']}/0/f/{sid}", "bytes=0-9")
        self.assertEqual((status, body), (206, self.payload[:10]))
        self.assertEqual((await self.fetch(loc, method="HEAD"))[0], 200)
        self.assertEqual((await self.counted(link))["downloads_completed"], 1)
        other = await self.create_link()
        self.assertEqual((await self.open_session(other, 0)).split("/")[3], "0")

    async def test_post_sin_indice_en_multiarchivo_404_y_no_abre_sesion(self):
        link = await self.two()
        status, _ = await self.post(link)
        self.assertEqual(status, 404)
        self.assertEqual(self.ctx.store._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        sid = (await self.open_session(link, 0)).rsplit("/", 1)[1]
        async with self.http.get(f"{self.pub}/d/{link['token']}/f/{sid}") as resp:
            self.assertEqual(resp.status, 404)

    async def test_indice_inexistente_404(self):
        link = await self.create_link()
        for index in (1, 2, 3, 4, 9, 10, -1):
            status, _ = await self.post(link, index=index)
            self.assertEqual(status, 404, index)
        async with self.http.get(f"{self.pub}/d/{link['token']}/3/f/{'0' * 32}") as resp:
            self.assertEqual(resp.status, 404)
        async with self.http.get(f"{self.pub}/d/{link['token']}/7/f/{'0' * 32}") as resp:
            self.assertEqual(resp.status, 404)

    async def test_get_a_la_ruta_post_de_indice_es_405(self):
        link = await self.two()
        async with self.http.get(f"{self.pub}/d/{link['token']}/0") as resp:
            self.assertEqual(resp.status, 405)


class LimitadorMultiTests(MultiBase):
    CONFIG = {"max_streams": 30, "max_streams_per_link": 30, "miss_limit": 3, "block_seconds": 50}

    async def test_indices_inexistentes_cuentan_para_el_limitador(self):
        link = await self.create_link()
        headers = {"X-Real-IP": "198.51.100.9"}
        for index in (1, 2, 3, 9):
            status, _ = await self.post(link, headers=headers, index=index)
            self.assertEqual(status, 404)
        status, resp_headers = await self.post(link, headers=headers, index=0)
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", resp_headers)


class TopeTotalDelEnlaceTests(MultiBase):
    BLOCK = 64 * 1024
    CONFIG = {"max_streams": 6, "max_streams_per_link": 2}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        for path in (self.dump_path, self.zip_path):
            with open(path, "r+b") as fh:
                fh.truncate(64 * 1024 * 1024)

    async def hold(self, loc):
        resp = await self.http.get(self.pub + loc)
        self.assertEqual(resp.status, 200)
        await resp.content.readexactly(1024)
        return resp

    async def test_un_enlace_de_dos_archivos_no_pasa_de_dos_streams_en_total(self):
        self.write_dump("srv/otro.sql.gz", b"o" * 700)
        link = await self.two(max_downloads=5)
        other = await self.create_link(relpath="srv/otro.sql.gz", max_downloads=5)
        loc0, loc1 = await self.open_session(link, 0), await self.open_session(link, 1)
        held = [await self.hold(loc0), await self.hold(loc1)]
        self.assertEqual((self.ctx.active.count(link["id"]), self.ctx.active.total), (2, 2))
        for loc in (loc0, loc1):
            async with self.http.get(self.pub + loc) as resp:
                self.assertEqual(resp.status, 503)
                self.assertIn("Retry-After", resp.headers)
            async with self.http.head(self.pub + loc) as resp:
                self.assertEqual(resp.status, 200)
        self.assertEqual(self.ctx.active.total, 2)
        status, _, body = await self.fetch(await self.open_session(other))
        self.assertEqual((status, body), (200, b"o" * 700))
        for resp in held:
            resp.close()

    async def test_dos_streams_del_mismo_archivo_tambien_llenan_el_tope_del_enlace(self):
        link = await self.two(max_downloads=5)
        loc0, loc1 = await self.open_session(link, 0), await self.open_session(link, 1)
        held = [await self.hold(loc0), await self.hold(loc0)]
        async with self.http.get(self.pub + loc1) as resp:
            self.assertEqual(resp.status, 503)
        for resp in held:
            resp.close()
        for _ in range(300):
            if self.ctx.active.count(link["id"]) == 0:
                break
            await asyncio.sleep(0.01)
        self.assertEqual((await self.fetch(loc1, "bytes=0-9"))[0], 206)

    async def test_el_cupo_se_libera_para_el_otro_archivo(self):
        link = await self.two(max_downloads=5)
        loc0, loc1 = await self.open_session(link, 0), await self.open_session(link, 1)
        first, second = await self.hold(loc0), await self.hold(loc1)
        first.close()
        for _ in range(300):
            if self.ctx.active.count(link["id"], 0) == 0:
                break
            await asyncio.sleep(0.01)
        third = await self.hold(loc1)
        self.assertEqual(self.ctx.active.count(link["id"]), 2)
        second.close()
        third.close()


class BorradoMultiTests(MultiBase):
    def run_cleanup(self):
        return cleanup.run_once(self.ctx)

    async def test_el_dump_agotado_se_borra_mientras_el_zip_sigue_sirviendo(self):
        grace = self.ctx.config.post_download_grace
        link = await self.two()
        loc0, loc1 = await self.open_session(link, 0), await self.open_session(link, 1)
        await self.fetch(loc0)
        await self.wait_counts(link, [1, 0])
        await self.idle()
        self.run_cleanup()
        self.assertTrue(os.path.exists(self.dump_path))
        self.clock.advance(grace)
        self.run_cleanup()
        self.assertFalse(os.path.exists(self.dump_path))
        self.assertTrue(os.path.exists(self.zip_path))
        self.assertEqual(self.reasons(), [("srv/a.sql.gz", "link_exhausted")])
        async with self.http.get(f"{self.pub}/d/{link['token']}") as resp:
            page = await resp.text()
        self.assertEqual(page.count('<button class="btn"'), 1)
        self.assertIn(f'action="/d/{link["token"]}/1"', page)
        status, _, body = await self.fetch(loc1)
        self.assertEqual((status, body), (200, self.zip_payload))

    async def test_el_zip_agotado_primero_se_borra_y_el_dump_sigue(self):
        link = await self.two()
        loc0, loc1 = await self.open_session(link, 0), await self.open_session(link, 1)
        await self.fetch(loc1)
        await self.wait_counts(link, [0, 1])
        await self.idle()
        self.clock.advance(self.ctx.config.post_download_grace)
        self.run_cleanup()
        self.assertFalse(os.path.exists(self.zip_path))
        self.assertTrue(os.path.exists(self.dump_path))
        self.assertEqual(self.reasons(), [("_gitlab/c.zip", "link_exhausted")])
        self.assertEqual((await self.fetch(loc0, "bytes=0-9"))[0], 206)

    async def test_revocar_libera_ambos_archivos(self):
        link = await self.two()
        await self.open_session(link, 0)
        self.assertEqual((await self.api("DELETE", f"/v2/links/{link['id']}"))[0], 204)
        self.assertFalse(os.path.exists(self.dump_path))
        self.assertFalse(os.path.exists(self.zip_path))
        self.assertEqual(sorted(self.reasons()), [("_gitlab/c.zip", "link_revoked"), ("srv/a.sql.gz", "link_revoked")])

    async def test_vencer_libera_ambos_archivos(self):
        await self.two(ttl_seconds=60)
        self.clock.advance(61)
        self.run_cleanup()
        self.assertFalse(os.path.exists(self.dump_path))
        self.assertFalse(os.path.exists(self.zip_path))
        self.assertEqual(sorted(r for _, r in self.reasons()), ["link_expired", "link_expired"])

    async def test_keep_file_conserva_todos_los_archivos_del_enlace(self):
        link = await self.two()
        self.assertEqual((await self.api("DELETE", f"/v2/links/{link['id']}?keep_file=1"))[0], 204)
        self.run_cleanup()
        self.assertTrue(os.path.exists(self.dump_path))
        self.assertTrue(os.path.exists(self.zip_path))

    async def test_un_archivo_compartido_con_otro_enlace_vigente_no_se_borra(self):
        a = await self.two()
        b = await self.create_link(relpath="_gitlab/c.zip", max_downloads=3)
        await self.api("DELETE", f"/v2/links/{a['id']}")
        self.assertFalse(os.path.exists(self.dump_path))
        self.assertTrue(os.path.exists(self.zip_path))
        await self.api("DELETE", f"/v2/links/{b['id']}")
        self.assertFalse(os.path.exists(self.zip_path))

    async def test_sesion_abierta_o_stream_activo_retrasan_el_borrado_de_ese_archivo(self):
        link = await self.two(ttl_seconds=60)
        await self.open_session(link, 1)
        self.ctx.active.enter(link["id"], 0)
        self.clock.advance(61)
        self.run_cleanup()
        self.assertTrue(os.path.exists(self.dump_path))
        self.assertTrue(os.path.exists(self.zip_path))
        self.ctx.active.exit(link["id"], 0)
        self.run_cleanup()
        self.assertFalse(os.path.exists(self.dump_path))
        self.assertTrue(os.path.exists(self.zip_path))
        self.clock.advance(7200)
        self.run_cleanup()
        self.assertFalse(os.path.exists(self.zip_path))

    async def test_scan_recorre_los_tres_sufijos_y_respeta_las_protecciones(self):
        stamp = self.clock() - 100 * 3600
        for rel in ("_gitlab/viejo.zip", "_gitlab/viejo.tar.gz", "srv/viejo.sql.gz"):
            os.utime(self.write_dump(rel, b"x" * 20), (stamp, stamp))
        intactos = [self.write_dump(rel, b"x" * 20) for rel in
                    ("_gitlab/.oculto.zip", "_gitlab/nota.txt", ".locks/x.zip")]
        for path in intactos:
            os.utime(path, (stamp, stamp))
        os.utime(self.dump_path, (self.clock(), self.clock()))
        os.utime(self.zip_path, (self.clock(), self.clock()))
        self.run_cleanup()
        for rel in ("_gitlab/viejo.zip", "_gitlab/viejo.tar.gz", "srv/viejo.sql.gz"):
            self.assertFalse(os.path.exists(os.path.join(self.dumps, rel)), rel)
        for path in intactos:
            self.assertTrue(os.path.exists(path), path)
        self.assertEqual(sorted(r for _, r in self.reasons()), ["retention"] * 3)
