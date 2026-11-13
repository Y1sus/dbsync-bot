# =============================================================================
# services/bot/tests/test_gateway_client.py — Pruebas del cliente del gateway contra un gateway falso en proceso
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import unittest

from aiohttp.test_utils import TestServer

from bot.gateway_client import GatewayClient, GatewayError
from tests.fakes.gateway import TOKEN, FakeGateway

SHA = "b" * 64


class GatewayClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gw = FakeGateway()
        self.server = TestServer(self.gw.app)
        await self.server.start_server()
        self.url = str(self.server.make_url("")).rstrip("/")
        self.client = GatewayClient(self.url, TOKEN, timeout=2)

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()

    async def create(self, **over):
        args = dict(relpath="alpha/f.sql.gz", sha256=SHA, created_by=42, job_id="tg1-aaaa1111", ttl_seconds=7200, max_downloads=3, label="a/b/c")
        args.update(over)
        return await self.client.create_link(**args)

    async def test_create_sends_contract_body_and_bearer(self):
        rec = await self.create()
        self.assertTrue(rec["url"].startswith("https://dumps.test/d/"))
        req = self.gw.requests[-1]
        self.assertEqual(req["auth"], f"Bearer {TOKEN}")
        self.assertEqual(req["body"], {"relpath": "alpha/f.sql.gz", "sha256": SHA, "created_by": "42", "job_id": "tg1-aaaa1111",
                                       "ttl_seconds": 7200, "max_downloads": 3, "label": "a/b/c"})
        self.assertIsInstance(req["body"]["created_by"], str)

    async def test_label_truncated(self):
        await self.create(label="x" * 500)
        self.assertEqual(len(self.gw.requests[-1]["body"]["label"]), 120)

    async def test_list_and_revoke(self):
        rec = await self.create()
        self.assertEqual([r["id"] for r in await self.client.list_links()], [rec["id"]])
        await self.client.revoke(rec["id"])
        self.assertEqual(await self.client.list_links(), [])
        with self.assertRaises(GatewayError) as cm:
            await self.client.revoke("0" * 12)
        self.assertEqual((cm.exception.status, cm.exception.code), (404, "not_found"))

    async def test_revoke_rejects_malformed_id_without_request(self):
        n = len(self.gw.requests)
        with self.assertRaises(GatewayError):
            await self.client.revoke("../v1/health")
        self.assertEqual(len(self.gw.requests), n)

    async def test_401_wrong_token(self):
        bad = GatewayClient(self.url, "otro", timeout=2)
        try:
            with self.assertRaises(GatewayError) as cm:
                await bad.list_links()
        finally:
            await bad.close()
        self.assertEqual((cm.exception.status, cm.exception.code), (401, "unauthorized"))

    async def test_400_and_404_on_create(self):
        with self.assertRaises(GatewayError) as cm:
            await self.create(relpath="../x.sql.gz")
        self.assertEqual((cm.exception.status, cm.exception.code), (400, "invalid_request"))
        self.gw.existing_files = set()
        with self.assertRaises(GatewayError) as cm:
            await self.create()
        self.assertEqual((cm.exception.status, cm.exception.code), (404, "file_not_found"))

    async def test_500(self):
        self.gw.mode = "error500"
        with self.assertRaises(GatewayError) as cm:
            await self.create()
        self.assertEqual((cm.exception.status, cm.exception.code), (500, "internal"))

    async def test_timeout(self):
        self.gw.mode = "slow"
        c = GatewayClient(self.url, TOKEN, timeout=0.1)
        try:
            with self.assertRaises(GatewayError) as cm:
                await c.health()
        finally:
            await c.close()
        self.assertEqual(cm.exception.code, "timeout")

    async def test_unreachable(self):
        c = GatewayClient("http://127.0.0.1:1", TOKEN, timeout=1)
        try:
            with self.assertRaises(GatewayError) as cm:
                await c.health()
        finally:
            await c.close()
        self.assertEqual(cm.exception.code, "unreachable")

    async def test_bad_response_without_url(self):
        async def fake_request(*a, **k):
            return {"id": "0123456789ab"}
        self.client._request = fake_request
        with self.assertRaises(GatewayError) as cm:
            await self.create()
        self.assertEqual(cm.exception.code, "bad_response")

    async def test_health(self):
        self.assertTrue((await self.client.health())["ok"])
