# =============================================================================
# services/gateway/tests/test_internal.py — Pruebas de la API interna: auth, validaciones, esquemas y revocación
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import os

from .support import API_TOKEN, GatewayCase, load_defs, validate


class AuthTests(GatewayCase):
    async def test_401_sin_token_o_incorrecto(self):
        rutas = [("GET", "/v1/health"), ("GET", "/v1/links"), ("POST", "/v1/links"),
                 ("GET", "/v1/links/abc"), ("DELETE", "/v1/links/abc"), ("GET", "/no-existe")]
        defs = load_defs()
        for method, path in rutas:
            for token in (None, "incorrecto", API_TOKEN[:-1], API_TOKEN + "x", "ñandú"):
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                async with self.http.request(method, self.int + path, headers=headers) as resp:
                    body = await resp.json()
                    self.assertEqual(resp.status, 401, f"{method} {path} {token}")
                    self.assertEqual(validate(body, defs["Error"], defs), [])
                    self.assertEqual(body["error"]["code"], "unauthorized")
        async with self.http.get(self.int + "/v1/health", headers={"Authorization": API_TOKEN}) as resp:
            self.assertEqual(resp.status, 401)
        async with self.http.get(self.int + "/v1/health", headers={"Authorization": "bearer " + API_TOKEN}) as resp:
            self.assertEqual(resp.status, 200)

    async def test_ruta_inexistente_con_token_404_json(self):
        status, body = await self.api("GET", "/v1/nada")
        self.assertEqual((status, body["error"]["code"]), (404, "not_found"))


class CrearTests(GatewayCase):
    async def test_respuestas_cumplen_esquema(self):
        defs = load_defs()
        link = await self.create_link(sha256="b" * 64, label="mi respaldo", max_downloads=3, ttl_seconds=120)
        self.assertEqual(validate(link, defs["LinkCreated"], defs), [])
        self.assertTrue(link["url"].startswith("https://dumps.example.test/d/"))
        self.assertEqual(len(link["token"]), 43)
        self.assertEqual((link["size_bytes"], link["filename"], link["state"]), (1000, "a.sql.gz", "active"))
        self.assertEqual((link["created_by"], link["label"], link["max_downloads"]), ("tester", "mi respaldo", 3))
        self.assertFalse(link["download_in_progress"])
        _, one = await self.api("GET", f"/v1/links/{link['id']}")
        self.assertEqual(validate(one, defs["LinkRecord"], defs), [])
        self.assertNotIn("url", one)
        _, listing = await self.api("GET", "/v1/links")
        self.assertEqual(validate(listing, defs["LinkList"], defs), [])
        _, health = await self.api("GET", "/v1/health")
        self.assertEqual(validate(health, defs["Health"], defs), [])
        self.assertEqual(health["links_active"], 1)
        self.assertGreater(health["disk_free_mb"], 0)

    async def test_token_solo_se_devuelve_al_crear_y_se_guarda_hash(self):
        link = await self.create_link()
        _, one = await self.api("GET", f"/v1/links/{link['id']}")
        _, listing = await self.api("GET", "/v1/links?state=all")
        self.assertNotIn(link["token"], str(one) + str(listing))
        rows = self.ctx.store._db.execute("SELECT * FROM links").fetchall()
        self.assertNotIn(link["token"], str([tuple(r) for r in rows]))
        self.assertEqual(len(rows[0]["token_hash"]), 64)

    async def test_defaults(self):
        link = await self.create_link()
        self.assertEqual(link["max_downloads"], 1)
        self.assertEqual(link["expires_at"][:10], "2027-01-15")
        self.assertIsNone(link["sha256"])
        self.assertIsNone(link["label"])

    async def test_invalid_request(self):
        base = {"relpath": "srv/a.sql.gz", "created_by": "t", "job_id": "j"}
        malos = [
            {}, {**base, "extra": 1}, {**base, "relpath": 5}, {**base, "created_by": ""},
            {**base, "created_by": "x" * 65}, {**base, "job_id": 3}, {**base, "label": "x" * 121},
            {**base, "ttl_seconds": 59}, {**base, "ttl_seconds": 604801}, {**base, "ttl_seconds": True},
            {**base, "ttl_seconds": "3600"}, {**base, "max_downloads": 0}, {**base, "max_downloads": 21},
            {**base, "sha256": "A" * 64}, {**base, "sha256": "abc"}, {**base, "relpath": "../x.sql.gz"},
            {**base, "relpath": "/abs.sql.gz"}, {**base, "relpath": "srv/a.sql.gz.part"},
            [1, 2], "texto",
        ]
        for body in malos:
            status, data = await self.api("POST", "/v1/links", body)
            self.assertEqual((status, data["error"]["code"]), (400, "invalid_request"), body)
        async with self.http.post(self.int + "/v1/links", data=b"{no json",
                                  headers={"Authorization": f"Bearer {API_TOKEN}"}) as resp:
            self.assertEqual(resp.status, 400)

    async def test_file_not_found_symlink_y_part(self):
        self.write_dump("srv/x.sql.gz.part", b"parcial")
        os.symlink(self.dump_path, os.path.join(self.dumps, "srv", "ln.sql.gz"))
        base = {"created_by": "t", "job_id": "j"}
        status, data = await self.api("POST", "/v1/links", {**base, "relpath": "srv/nope.sql.gz"})
        self.assertEqual((status, data["error"]["code"]), (404, "file_not_found"))
        status, data = await self.api("POST", "/v1/links", {**base, "relpath": "srv/ln.sql.gz"})
        self.assertEqual((status, data["error"]["code"]), (404, "file_not_found"))
        status, data = await self.api("POST", "/v1/links", {**base, "relpath": "srv/x.sql.gz.part"})
        self.assertEqual(status, 400)
        self.assertEqual((await self.api("GET", "/v1/links?state=all"))[1]["links"], [])

    async def test_archivo_vacio_rechazado(self):
        self.write_dump("srv/vacio.sql.gz", b"")
        status, data = await self.api("POST", "/v1/links",
                                      {"relpath": "srv/vacio.sql.gz", "created_by": "t", "job_id": "j"})
        self.assertEqual((status, data["error"]["code"]), (400, "invalid_request"))


class ListarYRevocarTests(GatewayCase):
    async def test_listado_orden_y_estados(self):
        first = await self.create_link(ttl_seconds=60)
        self.clock.advance(1)
        second = await self.create_link()
        self.clock.advance(1)
        third = await self.create_link()
        _, active = await self.api("GET", "/v1/links")
        self.assertEqual([x["id"] for x in active["links"]], [third["id"], second["id"], first["id"]])
        self.clock.advance(100)
        await self.api("DELETE", f"/v1/links/{second['id']}")
        _, active = await self.api("GET", "/v1/links")
        self.assertEqual([x["id"] for x in active["links"]], [third["id"]])
        _, everything = await self.api("GET", "/v1/links?state=all")
        states = {x["id"]: x["state"] for x in everything["links"]}
        self.assertEqual(states, {first["id"]: "expired", second["id"]: "revoked", third["id"]: "active"})
        status, data = await self.api("GET", "/v1/links?state=otro")
        self.assertEqual((status, data["error"]["code"]), (400, "invalid_request"))

    async def test_get_y_delete_desconocidos(self):
        for path in ("/v1/links/ffffffffffff", "/v1/links/noesid"):
            for method in ("GET", "DELETE"):
                status, data = await self.api(method, path)
                self.assertEqual((status, data["error"]["code"]), (404, "not_found"))

    async def test_delete_idempotente_conserva_archivo(self):
        link = await self.create_link()
        self.assertEqual((await self.api("DELETE", f"/v1/links/{link['id']}"))[0], 204)
        self.assertEqual((await self.api("DELETE", f"/v1/links/{link['id']}"))[0], 204)
        self.assertTrue(os.path.exists(self.dump_path))

    async def test_delete_file_si_nadie_mas_lo_usa(self):
        link = await self.create_link()
        self.assertEqual((await self.api("DELETE", f"/v1/links/{link['id']}?delete_file=1"))[0], 204)
        self.assertFalse(os.path.exists(self.dump_path))
        self.assertIn("file_deleted", [e["event"] for e in self.audit_events()])

    async def test_delete_file_respeta_otro_enlace_activo(self):
        a = await self.create_link()
        b = await self.create_link()
        await self.api("DELETE", f"/v1/links/{a['id']}?delete_file=1")
        self.assertTrue(os.path.exists(self.dump_path))
        await self.api("DELETE", f"/v1/links/{b['id']}?delete_file=1")
        self.assertFalse(os.path.exists(self.dump_path))

    async def test_download_in_progress(self):
        with open(self.dump_path, "r+b") as fh:
            fh.truncate(64 * 1024 * 1024)
        link = await self.create_link()
        loc = await self.open_session(link)
        resp = await self.http.get(self.pub + loc)
        await resp.content.readexactly(1024)
        self.assertTrue((await self.link_state(link))["download_in_progress"])
        resp.close()
