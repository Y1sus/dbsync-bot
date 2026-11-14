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
        args = dict(files=[{"relpath": "alpha/f.sql.gz", "sha256": SHA}], created_by=42, job_id="tg1-aaaa1111", ttl_seconds=7200, max_downloads=3, label="a/b/c")
        args.update(over)
        return await self.client.create_link(**args)

    async def test_create_sends_contract_body_and_bearer(self):
        rec = await self.create()
        self.assertTrue(rec["url"].startswith("https://dumps.test/d/"))
        req = self.gw.requests[-1]
        self.assertEqual(req["auth"], f"Bearer {TOKEN}")
        self.assertEqual(req["path"], "/v2/links")
        self.assertEqual(req["body"], {"files": [{"relpath": "alpha/f.sql.gz", "sha256": SHA}], "created_by": "42", "job_id": "tg1-aaaa1111",
                                       "ttl_seconds": 7200, "max_downloads": 3, "label": "a/b/c"})
        self.assertEqual(rec["files"][0]["kind"], "db")
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

    async def test_revoke_deletes_file_by_default_and_is_idempotent(self):
        rec = await self.create()
        await self.client.revoke(rec["id"])
        await self.client.revoke(rec["id"])
        dels = [r for r in self.gw.requests if r["method"] == "DELETE"]
        self.assertEqual([r["query"] for r in dels], [{}, {}])
        self.assertEqual(self.gw.deleted_files, {"alpha/f.sql.gz"})

    async def test_revoke_keep_file_sends_flag(self):
        rec = await self.create()
        await self.client.revoke(rec["id"], keep_file=True)
        self.assertEqual(self.gw.requests[-1]["query"], {"keep_file": "1"})
        self.assertEqual(self.gw.deleted_files, set())

    async def test_revoke_rejects_malformed_id_without_request(self):
        n = len(self.gw.requests)
        with self.assertRaises(GatewayError):
            await self.client.revoke("../v1/health")
        self.assertEqual(len(self.gw.requests), n)

    async def test_multi_file_link_keeps_order_and_per_file_data(self):
        files = [{"relpath": "_gitlab/p.zip", "sha256": SHA}, {"relpath": "alpha/f.sql.gz", "sha256": SHA}]
        rec = await self.create(files=files)
        self.assertEqual([(f["index"], f["kind"]) for f in rec["files"]], [(0, "code"), (1, "db")])
        listed = (await self.client.list_links())[0]
        self.assertEqual([f["filename"] for f in listed["files"]], ["p.zip", "f.sql.gz"])
        await self.client.revoke(rec["id"])
        self.assertEqual(self.gw.deleted_files, {"_gitlab/p.zip", "alpha/f.sql.gz"})

    async def test_rejects_invalid_file_sets(self):
        for bad in ([], [{"relpath": "a/b.sql.gz", "sha256": SHA}] * 2, [{"relpath": f"a/{i}.zip", "sha256": SHA} for i in range(5)],
                    [{"relpath": "a/b/c.zip", "sha256": SHA}]):
            with self.assertRaises(GatewayError) as cm:
                await self.create(files=bad)
            self.assertEqual(cm.exception.code, "invalid_request")

    async def test_record_without_files_is_bad_response(self):
        await self.create()
        self.gw.mode = "nofiles"
        with self.assertRaises(GatewayError) as cm:
            await self.client.list_links()
        self.assertEqual(cm.exception.code, "bad_response")

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
            await self.create(files=[{"relpath": "../x.sql.gz", "sha256": SHA}])
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
        c = GatewayClient(self.url, TOKEN, timeout=0.05)
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
            return {"id": "0123456789ab", "files": [{"index": 0, "filename": "f", "kind": "db", "size_bytes": 1, "downloads_completed": 0}]}
        self.client._request = fake_request
        with self.assertRaises(GatewayError) as cm:
            await self.create()
        self.assertEqual(cm.exception.code, "bad_response")

    async def test_health(self):
        self.assertTrue((await self.client.health())["ok"])
