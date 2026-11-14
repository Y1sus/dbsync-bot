# =============================================================================
# services/gateway/tests/test_pages.py — Pruebas de las páginas públicas: CSP con hash, accesibilidad y escape
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import base64
import hashlib
import re
import time
import unittest

from gateway import pages
from gateway.streaming import safe_filename

from .support import GatewayCase

KINDS = ["expired", "exhausted", "revoked", "session", "budget", "file", "notfound", "method", "busy", "forbidden",
         "bad", "error", "limited"]


def style_of(doc):
    found = re.findall(r"<style>(.*?)</style>", doc, re.S)
    assert len(found) == 1, found
    return found[0]


def csp_hash(css):
    return "sha256-" + base64.b64encode(hashlib.sha256(css.encode("utf-8")).digest()).decode("ascii")


def luminance(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class PaginasUnitTests(unittest.TestCase):
    def check_doc(self, doc, nombre=None):
        self.assertNotIn("style=", doc)
        for prohibido in ("<script", "http://", "https://", "src=", "<link", "<iframe", "javascript:", "@import",
                          "url("):
            self.assertNotIn(prohibido, doc)
        self.assertNotRegex(doc, r"\son[a-z]+=")
        self.assertIn('<html lang="es">', doc)
        self.assertIn('<meta name="robots" content="noindex">', doc)
        self.assertIn('name="viewport"', doc)
        self.assertIn(f'<meta name="description" content="{pages.DESCRIPTION}">', doc)
        self.assertEqual(style_of(doc), pages.CSS)
        if nombre:
            head = doc.split("</head>")[0]
            self.assertNotIn(nombre, head)

    def test_todas_las_paginas_cumplen_las_reglas(self):
        for kind in KINDS:
            status, doc = pages.render(kind, retry_after=90)
            self.check_doc(doc)
            self.assertGreaterEqual(status, 400, kind)
        status, doc = pages.landing("x.sql.gz", 1000, "a" * 64, "2027-01-15 08:00 UTC", 3600, 1)
        self.assertEqual(status, 200)
        self.check_doc(doc)

    def test_hash_de_la_csp_coincide_con_el_css(self):
        self.assertIn(f"style-src '{csp_hash(pages.CSS)}'", pages.CSP)
        self.assertEqual(
            pages.CSP,
            f"default-src 'none'; style-src '{csp_hash(pages.CSS)}'; img-src data:; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.assertNotIn("unsafe", pages.CSP)

    def test_contraste_aa_en_tema_claro_y_oscuro(self):
        css = pages.CSS
        claro = dict(re.findall(r"--([a-z-]+):(#[0-9a-f]{6})", css.split("@media")[0]))
        oscuro = dict(re.findall(r"--([a-z-]+):(#[0-9a-f]{6})", css.split("@media (prefers-color-scheme:dark)")[1]
                                 .split("*{box-sizing")[0]))
        self.assertIn("(prefers-color-scheme:dark)", css)
        for nombre, tema in (("claro", claro), ("oscuro", {**claro, **oscuro})):
            pares = [("fg", "bg"), ("fg", "card"), ("muted", "card"), ("muted", "bg"), ("muted", "code"),
                     ("fg", "code"), ("on-accent", "accent"), ("on-accent", "accent-h"),
                     ("ok-fg", "ok-bg"), ("warn-fg", "warn-bg"), ("err-fg", "err-bg"), ("info-fg", "info-bg")]
            for a, b in pares:
                self.assertGreaterEqual(contrast(tema[a], tema[b]), 4.5, f"{nombre} {a}/{b}")
            self.assertGreaterEqual(contrast(tema["ring"], tema["card"]), 3.0, f"{nombre} ring")

    def test_texto_de_tiempo_y_tamano(self):
        casos = {0: "menos de 1 min", 59: "menos de 1 min", 60: "1 min", 3540: "59 min", 3600: "1 h",
                 3660: "1 h 1 min", 86400: "1 d", 90000: "1 d 1 h", -5: "menos de 1 min"}
        for seconds, esperado in casos.items():
            self.assertEqual(pages.remaining_text(seconds), esperado, seconds)
        self.assertEqual(pages.human_size(1000), "1000 B")
        self.assertEqual(pages.human_size(1536), "1.5 KiB")
        self.assertEqual(pages.human_size(5 * 1024 ** 3), "5.0 GiB")

    def test_html_hostil_se_escapa(self):
        hostil = '<img src=x onerror=alert(1)>"\'&'
        _, doc = pages.landing(hostil + ".sql.gz", 10, hostil, hostil, 10, 1)
        self.assertNotIn("<img", doc)
        self.assertIn("&lt;img", doc)
        self.assertNotIn("onerror=alert(1)>", doc)


class PaginasHttpTests(GatewayCase):
    async def test_hash_de_la_cabecera_csp_coincide_con_el_style_renderizado(self):
        link = await self.create_link()
        urls = [f"/d/{link['token']}", "/nada", f"/d/{'A' * 43}"]
        for url in urls:
            async with self.http.get(self.pub + url) as resp:
                doc = await resp.text()
                csp = resp.headers["Content-Security-Policy"]
            self.assertIn(f"style-src '{csp_hash(style_of(doc))}'", csp, url)
            self.assertNotIn("style=", doc)

    async def test_pagina_de_descarga_muestra_los_datos(self):
        self.write_dump("srv/mi-respaldo.sql.gz", b"d" * 2048)
        link = await self.create_link(relpath="srv/mi-respaldo.sql.gz", sha256="ab" * 32, ttl_seconds=3540,
                                      max_downloads=3)
        async with self.http.get(f"{self.pub}/d/{link['token']}") as resp:
            page = await resp.text()
            self.assertEqual(resp.headers["Content-Type"], "text/html; charset=utf-8")
        stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(self.clock() + 3540))
        for esperado in ("mi-respaldo.sql.gz", "2.0 KiB", "ab" * 32, stamp, "expira en 59 min",
                         "Descargas restantes", "Descargar archivo", '<form method="post">'):
            self.assertIn(esperado, page)
        self.assertRegex(page, r"Descargas restantes</dt><dd>3</dd>")
        self.assertIn('class="mono"', page)

    async def test_title_y_descripcion_no_filtran_el_nombre(self):
        nombre = "cliente-confidencial-2027"
        self.write_dump(f"srv/{nombre}.sql.gz", b"datos")
        link = await self.create_link(relpath=f"srv/{nombre}.sql.gz", label="etiqueta-secreta")
        loc = await self.open_session(link)
        for url in (f"/d/{link['token']}", f"/d/{'A' * 43}", loc):
            async with self.http.get(self.pub + url) as resp:
                if resp.status == 200 and url == loc:
                    continue
                page = await resp.text()
            head = page.split("</head>")[0]
            self.assertNotIn(nombre, head, url)
            self.assertNotIn("etiqueta-secreta", page, url)
            self.assertRegex(head, r"<title>[^<]+</title>")
        otro = await self.create_link(relpath=f"srv/{nombre}.sql.gz")
        async with self.http.get(f"{self.pub}/d/{otro['token']}") as resp:
            self.assertIn(nombre, await resp.text())

    async def test_html_hostil_en_nombre_y_sha_queda_escapado(self):
        nombre = "<img src=x onerror=alert(1)>&\"x"
        self.write_dump(f"srv/{nombre}.sql.gz", b"datos")
        link = await self.create_link(relpath=f"srv/{nombre}.sql.gz", label="<script>alert(1)</script>")
        async with self.http.get(f"{self.pub}/d/{link['token']}") as resp:
            page = await resp.text()
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;&amp;&quot;x.sql.gz", page)
        self.assertNotIn("<img", page)
        self.assertNotIn("<script", page)
        loc = await self.open_session(link)
        _, headers, _ = await self.fetch(loc, "bytes=0-1")
        self.assertEqual(headers["Content-Disposition"],
                         f'attachment; filename="{safe_filename(nombre + ".sql.gz")}"')
        self.assertRegex(headers["Content-Disposition"], r'^attachment; filename="[A-Za-z0-9._-]+"$')

    async def test_paginas_de_estado(self):
        link = await self.create_link(ttl_seconds=60)
        agotado = await self.create_link()
        revocado = await self.create_link()
        loc = await self.open_session(agotado)
        await self.fetch(loc)
        await self.api("DELETE", f"/v1/links/{revocado['id']}?keep_file=1")
        self.clock.advance(61)
        esperados = [(link, "Este enlace ya expiró"), (agotado, "Este enlace ya se usó"),
                     (revocado, "Este enlace fue revocado")]
        for item, texto in esperados:
            async with self.http.get(f"{self.pub}/d/{item['token']}") as resp:
                self.assertEqual(resp.status, 410)
                self.assertIn(texto, await resp.text())
        self.clock.advance(7200)
        async with self.http.get(self.pub + loc) as resp:
            self.assertEqual(resp.status, 410)
            self.assertIn("La sesión de descarga venció", await resp.text())
        async with self.http.delete(f"{self.pub}/d/{link['token']}") as resp:
            self.assertEqual(resp.status, 405)
            self.assertIn("Esta acción no está permitida", await resp.text())
        import os
        os.unlink(self.dump_path)
        nuevo = await self.fail_open()
        del nuevo

    async def fail_open(self):
        self.write_dump("srv/b.sql.gz", b"xyz")
        link = await self.create_link(relpath="srv/b.sql.gz")
        loc = await self.open_session(link)
        import os
        os.unlink(self.dump_path.replace("a.sql.gz", "b.sql.gz"))
        async with self.http.get(self.pub + loc) as resp:
            self.assertEqual(resp.status, 410)
            self.assertIn("El archivo ya no está disponible", await resp.text())
        return link
