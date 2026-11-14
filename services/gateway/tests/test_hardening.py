# =============================================================================
# services/gateway/tests/test_hardening.py — Pruebas de endurecimiento: cabeceras, limitador, tope de streams y origen
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio

from .support import GatewayCase

REQUIRED = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class CabecerasTests(GatewayCase):
    def check(self, resp, label):
        for name, value in REQUIRED.items():
            self.assertEqual(resp.headers.get(name), value, f"{label} {name}")
        csp = resp.headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'none'", csp, label)
        self.assertNotIn("unsafe-inline", csp, label)
        server = resp.headers.get("Server", "")
        self.assertNotIn("aiohttp", server, label)
        self.assertNotIn("Python", server, label)

    async def test_todas_las_respuestas_publicas(self):
        link = await self.create_link(max_downloads=3)
        loc = await self.open_session(link)
        tok = link["token"]
        casos = [
            ("GET", f"/d/{tok}", {}, 200), ("HEAD", f"/d/{tok}", {}, 200),
            ("POST", f"/d/{tok}", {}, 303), ("GET", loc, {}, 200), ("HEAD", loc, {}, 200),
            ("GET", loc, {"Range": "bytes=0-9"}, 206), ("GET", loc, {"Range": "bytes=5000-"}, 416),
            ("GET", "/d/" + "A" * 43, {}, 404), ("GET", "/nada", {}, 404), ("GET", "/d/corto", {}, 404),
            ("GET", f"/d/{tok}/f/{'0' * 32}", {}, 404), ("DELETE", f"/d/{tok}", {}, 405),
            ("POST", f"/d/{tok}", {"Sec-Fetch-Site": "cross-site"}, 403),
        ]
        for method, url, headers, expected in casos:
            async with self.http.request(method, self.pub + url, headers=headers, allow_redirects=False) as resp:
                await resp.read()
                self.assertEqual(resp.status, expected, f"{method} {url}")
                self.check(resp, f"{method} {url} {resp.status}")
        self.clock.advance(7300)
        for url in (f"/d/{tok}", loc):
            async with self.http.get(self.pub + url) as resp:
                self.assertEqual(resp.status, 410)
                self.check(resp, f"410 {url}")

    async def test_429_y_503_llevan_cabeceras_y_retry_after(self):
        self.ctx.limiter.limit = 1
        for _ in range(2):
            async with self.http.get(self.pub + "/nada") as resp:
                await resp.read()
        async with self.http.get(self.pub + "/nada") as resp:
            self.assertEqual(resp.status, 429)
            self.assertIn("Retry-After", resp.headers)
            self.check(resp, "429")

    async def test_hsts_solo_con_x_forwarded_proto_https(self):
        link = await self.create_link()
        for headers, esperado in [({}, None), ({"X-Forwarded-Proto": "http"}, None),
                                  ({"X-Forwarded-Proto": "https"}, "max-age=31536000"),
                                  ({"X-Forwarded-Proto": "HTTPS"}, "max-age=31536000")]:
            for url in (f"/d/{link['token']}", "/nada"):
                async with self.http.get(self.pub + url, headers=headers) as resp:
                    await resp.read()
                    self.assertEqual(resp.headers.get("Strict-Transport-Security"), esperado, (headers, url))

    async def test_api_interna_no_expone_version(self):
        async with self.http.get(self.int + "/v2/health") as resp:
            self.assertNotIn("aiohttp", resp.headers.get("Server", ""))
            self.assertNotIn("Python", resp.headers.get("Server", ""))


class LimitadorHttpTests(GatewayCase):
    CONFIG = {"miss_limit": 3, "block_seconds": 50, "limiter_max_ips": 5}

    async def miss(self, url, ip="198.51.100.1", method="GET", rng=None):
        headers = {"X-Real-IP": ip, **({"Range": rng} if rng else {})}
        async with self.http.request(method, self.pub + url, headers=headers) as resp:
            await resp.read()
            return resp.status, dict(resp.headers)

    async def test_cuentan_404_de_handler_y_de_router(self):
        link = await self.create_link()
        otros = [f"/d/{'A' * 43}", "/nada", "/d/corto", f"/d/{link['token']}/f/{'0' * 32}"]
        for url in otros[:3]:
            self.assertEqual((await self.miss(url))[0], 404)
        self.assertEqual((await self.miss(otros[3]))[0], 404)
        status, headers = await self.miss(otros[0])
        self.assertEqual(status, 429)
        self.assertEqual(headers["Retry-After"], "50")
        self.assertEqual((await self.miss(f"/d/{link['token']}"))[0], 429)
        events = [e for e in self.audit_events() if e["event"] == "rate_limited"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["ip"], "198.51.100.1")

    async def test_cada_origen_de_404_cuenta_por_separado(self):
        for url in ("/nada", "/d/corto", f"/d/{'B' * 43}", "/d/" + "C" * 43 + "/f/" + "1" * 32):
            await self.miss(url)
        self.assertEqual((await self.miss("/nada"))[0], 429)
        status, _ = await self.miss("/nada", ip="198.51.100.2")
        self.assertEqual(status, 404)

    async def test_respuestas_validas_no_cuentan(self):
        link = await self.create_link(max_downloads=20)
        loc = await self.open_session(link)
        for _ in range(12):
            self.assertEqual((await self.miss(f"/d/{link['token']}"))[0], 200)
            self.assertEqual((await self.miss(loc, method="HEAD"))[0], 200)
            self.assertEqual((await self.miss(loc, rng="bytes=0-9"))[0], 206)
        self.assertEqual((await self.miss("/nada"))[0], 404)

    async def test_desbloqueo_por_tiempo(self):
        for _ in range(4):
            await self.miss("/nada")
        self.assertEqual((await self.miss("/nada"))[0], 429)
        self.clock.advance(49)
        self.assertEqual((await self.miss("/nada"))[0], 429)
        self.clock.advance(1)
        self.assertEqual((await self.miss("/nada"))[0], 404)

    async def test_ventana_de_60_segundos(self):
        for _ in range(3):
            await self.miss("/nada")
        self.clock.advance(61)
        for _ in range(3):
            self.assertEqual((await self.miss("/nada"))[0], 404)

    async def test_tabla_acotada(self):
        for i in range(40):
            await self.miss("/nada", ip=f"203.0.113.{i + 1}")
        self.assertLessEqual(self.ctx.limiter.size(), 5)

    async def test_pagina_429_amigable(self):
        for _ in range(4):
            await self.miss("/nada")
        async with self.http.get(self.pub + "/nada", headers={"X-Real-IP": "198.51.100.1"}) as resp:
            page = await resp.text()
        self.assertEqual(resp.status, 429)
        self.assertIn("Demasiadas solicitudes", page)
        self.assertIn("Vuelve a intentarlo en", page)


class TopeDeStreamsTests(GatewayCase):
    BLOCK = 64 * 1024
    CONFIG = {"max_streams": 2}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        with open(self.dump_path, "r+b") as fh:
            fh.truncate(64 * 1024 * 1024)

    async def start_stream(self, loc):
        resp = await self.http.get(self.pub + loc)
        self.assertEqual(resp.status, 200)
        await resp.content.readexactly(1024)
        return resp

    async def test_503_con_retry_after_y_head_exento(self):
        link = await self.create_link(max_downloads=5)
        loc = await self.open_session(link)
        first = await self.start_stream(loc)
        second = await self.start_stream(loc)
        self.assertEqual(self.ctx.active.total, 2)
        async with self.http.get(self.pub + loc) as resp:
            self.assertEqual(resp.status, 503)
            self.assertIn("Retry-After", resp.headers)
            for name, value in REQUIRED.items():
                self.assertEqual(resp.headers.get(name), value, f"503 {name}")
            self.assertNotIn("unsafe-inline", resp.headers["Content-Security-Policy"])
            self.assertNotIn("aiohttp", resp.headers.get("Server", ""))
            self.assertNotIn("Python", resp.headers.get("Server", ""))
            self.assertIn("Estamos ocupados", await resp.text())
        async with self.http.head(self.pub + loc) as resp:
            self.assertEqual(resp.status, 200)
        async with self.http.get(self.pub + loc, headers={"Range": "bytes=0-9"}) as resp:
            self.assertEqual(resp.status, 503)
        self.assertEqual(self.ctx.active.total, 2)
        first.close()
        for _ in range(300):
            if self.ctx.active.total < 2:
                break
            await asyncio.sleep(0.01)
        async with self.http.get(self.pub + loc, headers={"Range": "bytes=0-9"}) as resp:
            self.assertEqual(resp.status, 206)
        second.close()

    async def test_el_cupo_se_libera_con_416_y_archivo_ausente(self):
        link = await self.create_link(max_downloads=5)
        loc = await self.open_session(link)
        for _ in range(5):
            async with self.http.get(self.pub + loc, headers={"Range": "bytes=999999999999-"}) as resp:
                self.assertEqual(resp.status, 416)
        self.assertEqual(self.ctx.active.total, 0)


class ClienteLentoTests(GatewayCase):
    BLOCK = 64 * 1024
    CONFIG = {"write_timeout": 0.15}

    async def test_cliente_que_no_lee_se_aborta_y_libera_el_cupo(self):
        with open(self.dump_path, "r+b") as fh:
            fh.truncate(256 * 1024 * 1024)
        link = await self.create_link()
        loc = await self.open_session(link)
        resp = await self.http.get(self.pub + loc)
        await resp.content.readexactly(1)
        for _ in range(400):
            if any(e["event"] == "download_aborted" for e in self.audit_events()):
                break
            await asyncio.sleep(0.01)
        aborted = [e for e in self.audit_events() if e["event"] == "download_aborted"]
        self.assertEqual(aborted[0]["reason"], "TimeoutError")
        await self.idle()
        self.assertEqual(self.ctx.active.total, 0)
        self.assertEqual((await self.link_state(link))["downloads_completed"], 0)
        resp.close()


class OrigenTests(GatewayCase):
    async def test_matriz_origin_y_sec_fetch_site(self):
        link = await self.create_link(max_downloads=5)
        base = "https://dumps.example.test"
        casos = [
            ({}, 303), ({"Origin": "null"}, 303), ({"Sec-Fetch-Site": "same-origin"}, 303),
            ({"Sec-Fetch-Site": "none"}, 303), ({"Sec-Fetch-Site": "same-origin", "Origin": "null"}, 303),
            ({"Sec-Fetch-Site": "same-origin", "Origin": base}, 303), ({"Origin": base}, 303),
            ({"Origin": base.upper()}, 303), ({"Origin": "https://dumps.example.test:443"}, 303),
            ({"Origin": "http://dumps.example.test"}, 403), ({"Origin": "https://dumps.example.test:9999"}, 403),
            ({"Origin": "http://dumps.example.test:9999"}, 403),
            ({"Origin": "https://evil.test\\@dumps.example.test"}, 403),
            ({"Origin": "http://evil.com\\@dumps.example.test"}, 403),
            ({"Origin": "https://evil.test@dumps.example.test"}, 403),
            ({"Origin": "https://dumps.example.test/ruta"}, 403),
            ({"Sec-Fetch-Site": "cross-site"}, 403), ({"Sec-Fetch-Site": "same-site"}, 403),
            ({"Sec-Fetch-Site": ""}, 403), ({"Sec-Fetch-Site": "cross-site", "Origin": base}, 403),
            ({"Sec-Fetch-Site": "same-site", "Origin": "null"}, 403),
            ({"Origin": "https://evil.test"}, 403), ({"Origin": "https://dumps.example.test.evil.test"}, 403),
            ({"Origin": "https://otra.example.test"}, 403), ({"Sec-Fetch-Site": "same-origin", "Origin": "https://evil.test"}, 403),
            ({"Origin": "no es una url"}, 403), ({"Origin": ""}, 403),
        ]
        rechazos = 0
        for headers, esperado in casos:
            status, _ = await self.post(link, {**headers, "User-Agent": "matriz/1"})
            self.assertEqual(status, esperado, headers)
            rechazos += esperado == 403
        events = [e for e in self.audit_events() if e["event"] == "origin_rejected"]
        self.assertGreater(rechazos, 5)
        self.assertEqual(len(events), 5)
        self.assertEqual(events[0]["ua"], "matriz/1")
        self.assertTrue(all(e["reason"].startswith(("origin=", "sec-fetch-site=")) for e in events))

    async def test_rechazo_no_abre_sesion_ni_cuenta_como_404(self):
        link = await self.create_link()
        for _ in range(40):
            self.assertEqual((await self.post(link, {"Sec-Fetch-Site": "cross-site"}))[0], 403)
        self.assertEqual(self.ctx.store._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        self.assertEqual((await self.post(link))[0], 303)

    async def test_token_y_sesion_nunca_en_auditoria_de_rechazos_y_bloqueos(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        await self.post(link, {"Sec-Fetch-Site": "cross-site", "Origin": f"https://x.test/d/{link['token']}"})
        self.ctx.limiter.limit = 1
        for _ in range(3):
            async with self.http.get(f"{self.pub}/d/{link['token']}/f/{'0' * 32}x") as resp:
                await resp.read()
        text = "\n".join(str(e) for e in self.audit_events())
        self.assertIn("origin_rejected", text)
        self.assertIn("rate_limited", text)
        self.assertNotIn(link["token"], text)
        self.assertNotIn(loc.rsplit("/", 1)[1], text)

    async def test_pagina_403_es_amigable(self):
        link = await self.create_link()
        async with self.http.post(f"{self.pub}/d/{link['token']}", headers={"Sec-Fetch-Site": "cross-site"}) as resp:
            page = await resp.text()
        self.assertEqual(resp.status, 403)
        self.assertIn("No pudimos verificar la solicitud", page)


class SuperficieTests(GatewayCase):
    async def test_crlf_en_nombre_de_archivo_no_inyecta_cabeceras(self):
        self.write_dump("srv/a\r\nX-Inyectada: 1.sql.gz", b"datos")
        link = await self.create_link(relpath="srv/a\r\nX-Inyectada: 1.sql.gz")
        loc = await self.open_session(link)
        status, headers, _ = await self.fetch(loc, "bytes=0-1")
        self.assertEqual(status, 206)
        self.assertNotIn("X-Inyectada", headers)
        self.assertNotIn("\n", headers["Content-Disposition"])
        self.assertNotIn("\r", headers["Content-Disposition"])

    async def test_range_abusivo(self):
        link = await self.create_link()
        loc = await self.open_session(link)
        for rng in ["bytes=0-" + "9" * 30, "bytes=" + "9" * 30 + "-", "bytes=-" + "9" * 30 + "0", "bytes=0-1,"
                    + ",".join(["2-3"] * 500), "bytes=0-0\r\nX: y"]:
            try:
                status, headers, _ = await self.fetch(loc, rng)
            except Exception:
                continue
            self.assertIn(status, (200, 206, 416), rng[:30])
        status, _, body = await self.fetch(loc, "bytes=-99999999999999999")
        self.assertEqual((status, body), (206, self.payload))

    async def test_404_indistinguibles_entre_token_malformado_y_desconocido(self):
        cuerpos = set()
        for url in ("/d/" + "A" * 43, "/d/corto", "/nada", "/d/" + "A" * 43 + "/f/" + "0" * 32):
            async with self.http.get(self.pub + url) as resp:
                self.assertEqual(resp.status, 404)
                cuerpos.add(await resp.text())
        self.assertEqual(len(cuerpos), 1)

    async def test_sesion_ajena_404_y_no_revela_si_el_enlace_existe(self):
        a = await self.create_link()
        loc = await self.open_session(a)
        sid = loc.rsplit("/", 1)[1]
        async with self.http.get(f"{self.pub}/d/{'Z' * 43}/f/{sid}") as resp:
            self.assertEqual(resp.status, 404)

    async def test_nombre_fuera_de_la_raiz_no_se_lee(self):
        import os
        secreto = os.path.join(self.tmp.name, "secreto.sql.gz")
        with open(secreto, "wb") as fh:
            fh.write(b"secreto")
        link = await self.create_link()
        loc = await self.open_session(link)
        os.unlink(self.dump_path)
        os.symlink(secreto, self.dump_path)
        status, _, body = await self.fetch(loc)
        self.assertEqual(status, 410)
        self.assertNotIn(b"secreto", body)
