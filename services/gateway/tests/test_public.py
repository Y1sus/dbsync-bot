# =============================================================================
# services/gateway/tests/test_public.py — Pruebas del listener público: conteo, sesiones, rangos, cabeceras y expiración
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import os

from .support import GatewayCase

SECURITY = ["Cache-Control", "X-Robots-Tag", "Referrer-Policy", "X-Content-Type-Options"]


class ConteoTests(GatewayCase):
    async def test_get_y_head_no_cuentan(self):
        link = await self.create_link()
        url = f"{self.pub}/d/{link['token']}"
        for _ in range(3):
            async with self.http.get(url) as resp:
                self.assertEqual(resp.status, 200)
            async with self.http.head(url) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(await resp.read(), b"")
        data = await self.link_state(link)
        self.assertEqual((data["downloads_completed"], data["state"]), (0, "active"))
        events = [e["event"] for e in self.audit_events()]
        self.assertEqual(events.count("landing_view"), 3)

    async def test_post_y_descarga_completa_cuenta_una(self):
        link = await self.create_link(max_downloads=2)
        loc = await self.open_session(link)
        self.assertRegex(loc, rf"^/d/{link['token']}/f/[0-9a-f]{{32}}$")
        status, headers, body = await self.fetch(loc)
        self.assertEqual((status, body), (200, self.payload))
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual((await self.link_state(link))["downloads_completed"], 1)
        await self.fetch(loc)
        self.assertEqual((await self.link_state(link))["downloads_completed"], 1)
        events = [e["event"] for e in self.audit_events()]
        self.assertEqual(events.count("download_completed"), 1)
        self.assertEqual(events.count("download_started"), 2)

    async def test_dos_rangos_cuentan_una(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        s1, h1, b1 = await self.fetch(loc, "bytes=0-499")
        self.assertEqual((s1, h1["Content-Range"]), (206, "bytes 0-499/1000"))
        self.assertEqual((await self.link_state(link))["downloads_completed"], 0)
        s2, h2, b2 = await self.fetch(loc, "bytes=500-")
        self.assertEqual((s2, h2["Content-Range"]), (206, "bytes 500-999/1000"))
        self.assertEqual(b1 + b2, self.payload)
        self.assertEqual((await self.link_state(link))["downloads_completed"], 1)

    async def test_ultimo_rango_primero_y_sesion_sigue(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        s, _, b = await self.fetch(loc, "bytes=-100")
        self.assertEqual((s, b), (206, self.payload[-100:]))
        state = await self.link_state(link)
        self.assertEqual((state["downloads_completed"], state["state"]), (1, "exhausted"))
        s, _, b = await self.fetch(loc, "bytes=0-899")
        self.assertEqual((s, b), (206, self.payload[:900]))
        self.assertEqual((await self.link_state(link))["downloads_completed"], 1)

    async def test_abortada_no_cuenta(self):
        with open(self.dump_path, "r+b") as fh:
            fh.truncate(64 * 1024 * 1024)
        link = await self.create_link()
        loc = await self.open_session(link)
        resp = await self.http.get(self.pub + loc)
        await resp.content.readexactly(1024)
        resp.close()
        for _ in range(200):
            if any(e["event"] == "download_aborted" for e in self.audit_events()):
                break
            await asyncio.sleep(0.01)
        events = [e["event"] for e in self.audit_events()]
        self.assertIn("download_aborted", events)
        self.assertNotIn("download_completed", events)
        state = await self.link_state(link)
        self.assertEqual((state["downloads_completed"], state["state"]), (0, "active"))
        self.assertFalse(state["download_in_progress"])

    async def test_post_tras_agotar_410_y_sesion_sigue(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        await self.fetch(loc)
        async with self.http.post(f"{self.pub}/d/{link['token']}", allow_redirects=False) as resp:
            self.assertEqual(resp.status, 410)
        async with self.http.get(f"{self.pub}/d/{link['token']}") as resp:
            self.assertEqual(resp.status, 410)
        s, _, b = await self.fetch(loc, "bytes=0-9")
        self.assertEqual((s, b), (206, self.payload[:10]))

    async def test_post_concurrente_una_sola_sesion(self):
        link = await self.create_link(max_downloads=5)

        async def post():
            async with self.http.post(f"{self.pub}/d/{link['token']}", allow_redirects=False) as resp:
                return resp.status, resp.headers["Location"]

        results = await asyncio.gather(*[post() for _ in range(12)])
        self.assertEqual({r[0] for r in results}, {303})
        self.assertEqual(len({r[1] for r in results}), 1)
        count = self.ctx.store._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(count, 1)

    async def test_descargas_concurrentes_cuentan_una(self):
        link = await self.create_link(max_downloads=5)
        loc = await self.open_session(link)
        await asyncio.gather(*[self.fetch(loc) for _ in range(6)])
        self.assertEqual((await self.link_state(link))["downloads_completed"], 1)


class EstadosTests(GatewayCase):
    async def test_expiracion(self):
        link = await self.create_link(ttl_seconds=60)
        loc = await self.open_session(link)
        self.clock.advance(61)
        async with self.http.get(f"{self.pub}/d/{link['token']}") as resp:
            self.assertEqual(resp.status, 410)
        async with self.http.post(f"{self.pub}/d/{link['token']}", allow_redirects=False) as resp:
            self.assertEqual(resp.status, 410)
        self.assertEqual((await self.link_state(link))["state"], "expired")
        s, _, b = await self.fetch(loc)
        self.assertEqual((s, b), (200, self.payload))
        self.clock.advance(7200)
        s, _, _ = await self.fetch(loc)
        self.assertEqual(s, 410)

    async def test_expiracion_sin_sesion_previa(self):
        link = await self.create_link(ttl_seconds=60)
        self.clock.advance(60)
        async with self.http.post(f"{self.pub}/d/{link['token']}", allow_redirects=False) as resp:
            self.assertEqual(resp.status, 410)

    async def test_revocado(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        status, _ = await self.api("DELETE", f"/v1/links/{link['id']}")
        self.assertEqual(status, 204)
        for method, url in [("GET", f"/d/{link['token']}"), ("POST", f"/d/{link['token']}"), ("GET", loc)]:
            async with self.http.request(method, self.pub + url, allow_redirects=False) as resp:
                self.assertEqual(resp.status, 410, url)
        self.assertEqual((await self.link_state(link))["state"], "revoked")

    async def test_desconocido_404(self):
        for url in ["/d/" + "A" * 43, "/d/corto", "/otra", "/d/" + "A" * 43 + "/f/" + "0" * 32]:
            async with self.http.get(self.pub + url) as resp:
                self.assertEqual(resp.status, 404, url)
        async with self.http.post(f"{self.pub}/d/{'A' * 43}", allow_redirects=False) as resp:
            self.assertEqual(resp.status, 404)

    async def test_sesion_ajena_o_inexistente_404(self):
        a = await self.create_link()
        b = await self.create_link()
        loc_a = await self.open_session(a)
        sid = loc_a.rsplit("/", 1)[1]
        async with self.http.get(f"{self.pub}/d/{b['token']}/f/{sid}") as resp:
            self.assertEqual(resp.status, 404)
        async with self.http.get(f"{self.pub}/d/{a['token']}/f/{'0' * 32}") as resp:
            self.assertEqual(resp.status, 404)

    async def test_archivo_borrado_410(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        os.unlink(self.dump_path)
        s, _, _ = await self.fetch(loc)
        self.assertEqual(s, 410)

    async def test_archivo_reemplazado_por_symlink_410(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        os.unlink(self.dump_path)
        os.symlink("/etc/hostname", self.dump_path)
        s, _, _ = await self.fetch(loc)
        self.assertEqual(s, 410)


class RangoHttpTests(GatewayCase):
    async def test_416_y_multirango(self):
        link = await self.create_link(max_downloads=3)
        loc = await self.open_session(link)
        for rng in ["bytes=1000-", "bytes=5-2", "bytes=abc", "bytes=-0"]:
            s, h, _ = await self.fetch(loc, rng)
            self.assertEqual((s, h["Content-Range"]), (416, "bytes */1000"), rng)
        s, h, b = await self.fetch(loc, "bytes=0-1,5-6")
        self.assertEqual((s, b), (200, self.payload))
        self.assertNotIn("Content-Range", h)
        self.assertEqual((await self.link_state(link))["downloads_completed"], 1)
        s, _, b = await self.fetch(loc, "bytes=0-0")
        self.assertEqual((s, b), (206, self.payload[:1]))

    async def test_head_archivo_sin_efectos(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        s, h, b = await self.fetch(loc, method="HEAD")
        self.assertEqual((s, b, h["Content-Length"]), (200, b"", "1000"))
        self.assertEqual(h["Accept-Ranges"], "bytes")
        s, h, _ = await self.fetch(loc, "bytes=0-9", method="HEAD")
        self.assertEqual((s, h["Content-Length"]), (206, "10"))
        self.assertEqual((await self.link_state(link))["downloads_completed"], 0)
        self.assertNotIn("download_started", [e["event"] for e in self.audit_events()])


class CabecerasTests(GatewayCase):
    async def test_cabeceras_en_todas_las_respuestas(self):
        link = await self.create_link(max_downloads=2)
        loc = await self.open_session(link)
        urls = [(f"/d/{link['token']}", "GET"), (f"/d/{link['token']}", "HEAD"), (loc, "GET"),
                (loc, "HEAD"), ("/d/" + "A" * 43, "GET"), ("/nada", "GET"), (loc, "DELETE")]
        for url, method in urls:
            async with self.http.request(method, self.pub + url) as resp:
                for name in SECURITY:
                    self.assertIn(name, resp.headers, f"{method} {url} {resp.status}")
                self.assertEqual(resp.headers["Cache-Control"], "no-store")
                self.assertEqual(resp.headers["X-Robots-Tag"], "noindex")
                self.assertEqual(resp.headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        async with self.http.get(self.pub + loc, headers={"Range": "bytes=9999-"}) as resp:
            self.assertEqual(resp.status, 416)
            self.assertEqual(resp.headers["Cache-Control"], "no-store")

    async def test_pagina_escapa_datos_y_es_estatica(self):
        self.write_dump("srv/<b>&x.sql.gz", b"datos")
        link = await self.create_link(relpath="srv/<b>&x.sql.gz", sha256="a" * 64)
        async with self.http.get(f"{self.pub}/d/{link['token']}") as resp:
            page = await resp.text()
            self.assertEqual(resp.headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("&lt;b&gt;&amp;x.sql.gz", page)
        self.assertNotIn("<b>", page)
        self.assertIn('<form method="post">', page)
        self.assertIn("a" * 64, page)
        for prohibido in ("<script", "http://", "https://", "src="):
            self.assertNotIn(prohibido, page)
        loc = await self.open_session(link)
        _, headers, _ = await self.fetch(loc, "bytes=0-1")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="_b__x.sql.gz"')

    async def test_token_nunca_en_auditoria(self):
        link = await self.create_link()
        async with self.http.get(f"{self.pub}/d/{link['token']}") as resp:
            await resp.read()
        loc = await self.open_session(link)
        await self.fetch(loc)
        await self.api("DELETE", f"/v1/links/{link['id']}")
        with open(os.path.join(self.data, "audit.jsonl"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn(link["token"], text)
        sid = loc.rsplit("/", 1)[1]
        self.assertNotIn(sid, text)
        events = {e["event"] for e in self.audit_events()}
        self.assertTrue({"link_created", "landing_view", "download_started", "download_completed",
                         "link_revoked"} <= events)
        for entry in self.audit_events():
            for key in ("ts", "event", "link_id", "ip", "ua", "bytes"):
                self.assertIn(key, entry)

    async def test_ip_desde_x_real_ip(self):
        link = await self.create_link()
        headers = {"X-Real-IP": "203.0.113.9", "User-Agent": "prueba/1"}
        async with self.http.get(f"{self.pub}/d/{link['token']}", headers=headers) as resp:
            await resp.read()
        async with self.http.get(f"{self.pub}/d/{link['token']}", headers={"X-Real-IP": "basura"}) as resp:
            await resp.read()
        views = [e for e in self.audit_events() if e["event"] == "landing_view"]
        self.assertEqual((views[0]["ip"], views[0]["ua"]), ("203.0.113.9", "prueba/1"))
        self.assertEqual(views[1]["ip"], "127.0.0.1")
