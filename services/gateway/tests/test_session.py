# =============================================================================
# services/gateway/tests/test_session.py — Pruebas de sesiones: cobertura de bytes, presupuesto, vencimiento y cortes
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import json
import os

import aiohttp

from gateway import cleanup

from .support import API_TOKEN, GatewayCase


class CoberturaHttpTests(GatewayCase):
    CONFIG = {"max_streams": 30, "max_streams_per_link": 30}

    def session_row(self):
        return self.ctx.store._db.execute("SELECT * FROM sessions").fetchone()

    async def test_sonda_de_un_byte_no_cuenta(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        for rng in ("bytes=-1", "bytes=0-0", "bytes=999-", "bytes=-100"):
            status, _, body = await self.fetch(loc, rng)
            self.assertEqual(status, 206, rng)
        await self.idle()
        self.assertEqual((await self.link_state(link))["downloads_completed"], 0)
        self.assertEqual(json.loads(self.session_row()["ranges"]), [[0, 1], [900, 1000]])

    async def test_cobertura_en_cualquier_orden_y_con_solapes(self):
        link = await self.create_link(max_downloads=3)
        loc = await self.open_session(link)
        for rng in ("bytes=600-799", "bytes=0-299", "bytes=250-650", "bytes=800-"):
            await self.fetch(loc, rng)
        state = await self.counted(link)
        self.assertEqual(state["downloads_completed"], 1)
        await self.fetch(loc)
        await self.idle()
        self.assertEqual((await self.link_state(link))["downloads_completed"], 1)

    async def test_evasion_con_size_menos_uno_cuenta_por_total_enviado(self):
        link = await self.create_link(max_downloads=3)
        loc = await self.open_session(link)
        status, _, body = await self.fetch(loc, "bytes=0-998")
        self.assertEqual((status, len(body)), (206, 999))
        await self.idle()
        self.assertEqual((await self.link_state(link))["downloads_completed"], 0)
        self.assertEqual(self.session_row()["sent_total"], 999)
        await self.fetch(loc, "bytes=0-998")
        state = await self.counted(link)
        self.assertEqual(state["downloads_completed"], 1)
        await self.idle()
        self.assertEqual(self.session_row()["sent_total"], 1998)
        status, _, _ = await self.fetch(loc, "bytes=0-998")
        self.assertEqual(status, 410)
        await self.idle()
        self.assertEqual((await self.link_state(link))["downloads_completed"], 1)

    async def test_sondas_pequenas_acumuladas_no_cuentan(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        for _ in range(6):
            await self.fetch(loc, "bytes=-1")
        await self.fetch(loc, "bytes=0-9")
        await self.idle()
        self.assertEqual((await self.link_state(link))["downloads_completed"], 0)
        self.assertEqual(self.session_row()["sent_total"], 16)

    async def test_reanudacion_del_90_por_ciento_cuenta_una_vez(self):
        link = await self.create_link(max_downloads=4)
        loc = await self.open_session(link)
        await self.fetch(loc, "bytes=0-899")
        await self.idle()
        self.assertEqual((await self.link_state(link))["downloads_completed"], 0)
        await self.fetch(loc, "bytes=900-")
        self.assertEqual((await self.counted(link))["downloads_completed"], 1)
        await self.idle()
        await self.fetch(loc, "bytes=0-899")
        await self.idle()
        self.assertEqual((await self.link_state(link))["downloads_completed"], 1)

    async def test_dos_lecturas_de_90_por_ciento_cuentan_por_total(self):
        link = await self.create_link(max_downloads=4)
        loc = await self.open_session(link)
        await self.fetch(loc, "bytes=0-899")
        await self.fetch(loc, "bytes=0-899")
        self.assertEqual((await self.counted(link))["downloads_completed"], 1)

    async def test_lecturas_concurrentes_de_size_menos_uno_cuentan_una_y_respetan_el_presupuesto(self):
        link = await self.create_link(max_downloads=5)
        loc = await self.open_session(link)
        results = await asyncio.gather(*[self.fetch(loc, "bytes=0-998") for _ in range(6)])
        statuses = sorted(r[0] for r in results)
        self.assertEqual((statuses.count(206), statuses.count(410)), (2, 4))
        self.assertEqual((await self.counted(link))["downloads_completed"], 1)
        await self.idle()
        row = self.session_row()
        self.assertEqual((row["sent_total"], row["budget_used"], row["counted"]), (1998, 1998, 1))
        self.assertEqual((await self.link_state(link))["downloads_completed"], 1)

    async def test_hueco_de_un_byte_impide_contar(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        await self.fetch(loc, "bytes=0-499")
        await self.fetch(loc, "bytes=501-")
        await self.idle()
        self.assertEqual((await self.link_state(link))["downloads_completed"], 0)
        await self.fetch(loc, "bytes=499-500")
        self.assertEqual((await self.counted(link))["downloads_completed"], 1)

    async def test_bytes_de_una_conexion_abortada_cuentan_y_se_devuelve_lo_no_enviado(self):
        with open(self.dump_path, "r+b") as fh:
            fh.truncate(32 * 1024 * 1024)
        self.ctx.config = self.ctx.config.__class__(**{**self.ctx.config.__dict__, "block_size": 64 * 1024})
        link = await self.create_link()
        loc = await self.open_session(link)
        resp = await self.http.get(self.pub + loc)
        await resp.content.readexactly(1024)
        resp.close()
        await self.idle()
        row = self.session_row()
        ranges = json.loads(row["ranges"])
        sent = ranges[0][1]
        self.assertEqual(ranges[0][0], 0)
        self.assertLess(sent, 32 * 1024 * 1024)
        self.assertEqual(row["budget_used"], sent)
        status, _, _ = await self.fetch(loc, f"bytes={sent}-")
        self.assertEqual(status, 206)
        self.assertEqual((await self.counted(link))["downloads_completed"], 1)


class PresupuestoTests(GatewayCase):
    CONFIG = {"max_streams": 30, "max_streams_per_link": 30}

    async def test_dos_copias_completas_y_la_tercera_410(self):
        link = await self.create_link(max_downloads=5)
        loc = await self.open_session(link)
        for _ in range(2):
            status, _, body = await self.fetch(loc)
            self.assertEqual((status, body), (200, self.payload))
        status, _, body = await self.fetch(loc)
        self.assertEqual(status, 410)
        self.assertIn("Esta descarga ya envió todo lo permitido".encode(), body)
        status, _, _ = await self.fetch(loc, method="HEAD")
        self.assertEqual(status, 200)
        status, _, _ = await self.fetch(loc, "bytes=2000-")
        self.assertEqual(status, 416)

    async def test_sondas_pequenas_caben_pero_no_otra_copia_completa(self):
        link = await self.create_link(max_downloads=5)
        loc = await self.open_session(link)
        self.assertEqual((await self.fetch(loc, "bytes=-1"))[0], 206)
        self.assertEqual((await self.fetch(loc, "bytes=0-9"))[0], 206)
        self.assertEqual((await self.fetch(loc))[0], 200)
        self.assertEqual((await self.fetch(loc, "bytes=0-499"))[0], 206)
        self.assertEqual((await self.fetch(loc))[0], 410)

    async def test_head_y_416_no_consumen_presupuesto(self):
        link = await self.create_link(max_downloads=5)
        loc = await self.open_session(link)
        for _ in range(5):
            await self.fetch(loc, method="HEAD")
            await self.fetch(loc, "bytes=5000-")
        self.assertEqual(self.ctx.store._db.execute("SELECT budget_used FROM sessions").fetchone()[0], 0)
        for _ in range(2):
            self.assertEqual((await self.fetch(loc))[0], 200)

    async def test_descargas_concurrentes_no_pasan_del_presupuesto_ni_cuentan_dos_veces(self):
        link = await self.create_link(max_downloads=5)
        loc = await self.open_session(link)
        results = await asyncio.gather(*[self.fetch(loc) for _ in range(12)])
        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses.count(200), 2)
        self.assertEqual(statuses.count(410), 10)
        state = await self.counted(link)
        self.assertEqual(state["downloads_completed"], 1)
        await self.idle()
        row = self.ctx.store._db.execute("SELECT budget_used, counted FROM sessions").fetchone()
        self.assertEqual((row["budget_used"], row["counted"]), (2000, 1))

    async def test_reanudaciones_parciales_caben_en_el_presupuesto(self):
        link = await self.create_link(max_downloads=5)
        loc = await self.open_session(link)
        for _ in range(4):
            self.assertEqual((await self.fetch(loc, "bytes=0-499"))[0], 206)
        self.assertEqual((await self.fetch(loc, "bytes=0-499"))[0], 410)


class VencimientoTests(GatewayCase):
    def expiry(self, link):
        return self.ctx.store._db.execute(
            "SELECT expires_at FROM sessions WHERE link_id=?", (link["id"],)).fetchone()[0]

    async def test_sesion_de_enlace_corto_vence_con_el_enlace_mas_la_gracia(self):
        grace = self.ctx.config.post_download_grace
        link = await self.create_link(ttl_seconds=60)
        loc = await self.open_session(link)
        self.assertEqual(self.expiry(link), self.clock() + 60 + grace)
        self.clock.advance(60 + grace - 1)
        self.assertEqual((await self.fetch(loc, "bytes=0-9"))[0], 206)
        self.clock.advance(1)
        self.assertEqual((await self.fetch(loc, "bytes=0-9"))[0], 410)

    async def test_enlace_largo_limitado_por_inicio_mas_7200(self):
        link = await self.create_link(ttl_seconds=604800)
        await self.open_session(link)
        self.assertEqual(self.expiry(link), self.clock() + 7200)

    async def test_gracia_por_defecto_900(self):
        self.assertEqual(self.ctx.config.post_download_grace, 900)


class CortesTests(GatewayCase):
    BLOCK = 64 * 1024
    CONFIG = {"max_streams": 10, "max_streams_per_link": 2, "min_rate_bytes": 10 ** 9, "max_stream_seconds": 600}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        with open(self.dump_path, "r+b") as fh:
            fh.truncate(64 * 1024 * 1024)

    async def stream(self, loc):
        resp = await self.http.get(self.pub + loc)
        self.assertEqual(resp.status, 200)
        await resp.content.readexactly(1024)
        return resp

    async def drain_until_cut(self, resp):
        try:
            for _ in range(2000):
                if not await resp.content.read(256 * 1024):
                    return True
        except aiohttp.ClientError:
            return True
        return False

    async def aborted_reason(self):
        for _ in range(300):
            events = [e for e in self.audit_events() if e["event"] == "download_aborted"]
            if events:
                return events[-1].get("reason")
            await asyncio.sleep(0.01)

    async def test_revocar_corta_la_descarga_en_curso_y_libera_el_cupo(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        resp = await self.stream(loc)
        self.assertEqual((await self.api("DELETE", f"/v2/links/{link['id']}"))[0], 204)
        self.assertTrue(await self.drain_until_cut(resp))
        self.assertEqual(await self.aborted_reason(), "revoked")
        await self.idle()
        self.assertEqual(self.ctx.active.total, 0)
        self.assertNotIn(link["id"], self.ctx.revoked)
        self.assertTrue(os.path.exists(self.dump_path))
        cleanup.run_once(self.ctx)
        self.assertFalse(os.path.exists(self.dump_path))
        resp.close()

    async def test_revocar_sin_streams_no_deja_estado_en_memoria(self):
        link = await self.create_link()
        await self.api("DELETE", f"/v2/links/{link['id']}")
        self.assertEqual(self.ctx.revoked, set())

    async def test_tope_por_enlace_y_otro_enlace_sigue_sirviendo(self):
        self.write_dump("srv/b.sql.gz", b"b" * 5000)
        a = await self.create_link(max_downloads=5)
        b = await self.create_link(relpath="srv/b.sql.gz", max_downloads=5)
        loc_a, loc_b = await self.open_session(a), await self.open_session(b)
        first, second = await self.stream(loc_a), await self.stream(loc_a)
        async with self.http.get(self.pub + loc_a) as resp:
            self.assertEqual(resp.status, 503)
            self.assertIn("Retry-After", resp.headers)
        async with self.http.head(self.pub + loc_a) as resp:
            self.assertEqual(resp.status, 200)
        status, _, body = await self.fetch(loc_b)
        self.assertEqual((status, body), (200, b"b" * 5000))
        self.assertEqual(self.ctx.active.count(a["id"]), 2)
        first.close()
        second.close()

    async def test_velocidad_minima_aborta_tras_la_ventana(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        resp = await self.stream(loc)
        self.clock.advance(self.ctx.config.min_rate_after + 1)
        self.assertTrue(await self.drain_until_cut(resp))
        self.assertEqual(await self.aborted_reason(), "slow_client")
        await self.idle()
        self.assertEqual(self.ctx.active.total, 0)
        resp.close()

    async def test_velocidad_suficiente_no_aborta(self):
        self.ctx.config = self.ctx.config.__class__(**{**self.ctx.config.__dict__, "min_rate_bytes": 1})
        link = await self.create_link()
        loc = await self.open_session(link)
        resp = await self.stream(loc)
        self.clock.advance(self.ctx.config.min_rate_after + 1)
        data = await resp.content.read(256 * 1024)
        self.assertTrue(data)
        self.assertEqual([e for e in self.audit_events() if e["event"] == "download_aborted"], [])
        resp.close()

    async def test_duracion_maxima(self):
        self.ctx.config = self.ctx.config.__class__(**{**self.ctx.config.__dict__, "min_rate_bytes": 0})
        link = await self.create_link()
        loc = await self.open_session(link)
        resp = await self.stream(loc)
        self.clock.advance(self.ctx.config.max_stream_seconds + 1)
        self.assertTrue(await self.drain_until_cut(resp))
        self.assertEqual(await self.aborted_reason(), "max_duration")
        resp.close()


class InternaBearerTests(GatewayCase):
    async def test_bearer_con_bytes_no_ascii_da_401_limpio(self):
        for raw in (b"Bearer \xff\xfe", b"Bearer \xc3\x28", b"Bearer " + b"\xed\xa0\x80", "Bearer ñandú".encode()):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.gw.internal_port)
            writer.write(b"GET /v2/health HTTP/1.1\r\nHost: x\r\nAuthorization: " + raw + b"\r\nConnection: close\r\n\r\n")
            await writer.drain()
            data = await reader.read()
            writer.close()
            self.assertTrue(data.startswith(b"HTTP/1.1 401"), (raw, data[:60]))
            self.assertNotIn(b"Traceback", data)
        status, _ = await self.api("GET", "/v2/health", token=API_TOKEN)
        self.assertEqual(status, 200)


class RelpathAnidadoTests(GatewayCase):
    async def test_post_links_rechaza_anidados_con_400(self):
        self.write_dump("srv/sub/x.sql.gz", b"datos")
        for rel in ("srv/sub/x.sql.gz", "a/b/c.sql.gz", "solo.sql.gz"):
            status, data = await self.api("POST", "/v2/links", {"files": [{"relpath": rel}], "created_by": "t", "job_id": "j"})
            self.assertEqual((status, data["error"]["code"]), (400, "invalid_request"), rel)
