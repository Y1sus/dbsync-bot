# =============================================================================
# services/bot/tests/test_jobs.py — Pruebas de jobs: flujo dump a enlace, cola, errores, auditoría y secretos
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import io
import json
import logging
import os
import tempfile
import unittest

from aiohttp.test_utils import TestServer

from bot.audit import Audit
from bot.gateway_client import GatewayClient
from bot.jobs import DumpRequest, JobManager, new_job_id
from bot.redact import Redactor, install_logging
from bot.runner_client import JOB_ID_RE, RunnerClient
from tests.fakes.gateway import TOKEN, FakeGateway

FAKE = os.path.join(os.path.dirname(__file__), "fakes", "fake_runner.sh")
BOT_TOKEN = "123456:TEST"
REQ = DumpRequest(42, "alpha", "mysql8", "appdb", 7200, 3)


class JobIdTests(unittest.TestCase):
    def test_format(self):
        for uid in (None, 0, 987654321, -5, "x"):
            self.assertRegex(new_job_id(uid), JOB_ID_RE.pattern.replace("^", "^").replace("\\Z", "$"))
        self.assertTrue(new_job_id(12).startswith("tg12-"))
        self.assertNotEqual(new_job_id(1), new_job_id(1))
        self.assertLessEqual(len(new_job_id(10 ** 15)), 64)


class JobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit_path = os.path.join(self.tmp.name, "audit.jsonl")
        self.redactor = Redactor([BOT_TOKEN, TOKEN])
        self.log_stream = io.StringIO()
        install_logging(self.redactor, stream=self.log_stream)
        self.addCleanup(logging.getLogger().handlers.clear)
        self.gw = FakeGateway()
        self.server = TestServer(self.gw.app)
        await self.server.start_server()
        self.gateway = GatewayClient(str(self.server.make_url("")).rstrip("/"), TOKEN, timeout=2, redactor=self.redactor)
        self.args_file = os.path.join(self.tmp.name, "args")

    async def asyncTearDown(self):
        await self.gateway.close()
        await self.server.close()

    def manager(self, mode="ok", max_jobs=1, timeout=5, **env):
        runner = RunnerClient(FAKE, timeout=timeout, kill_grace=0.2, redactor=self.redactor,
                              env={"FAKE_MODE": mode, "FAKE_ARGS_FILE": self.args_file, **env})
        return JobManager(runner, self.gateway, Audit(self.audit_path, self.redactor), max_jobs)

    def audit_events(self):
        with open(self.audit_path) as fh:
            return [json.loads(l) for l in fh]

    async def test_full_flow_dump_to_link(self):
        jm = self.manager()
        out = await jm.run(REQ, "tg1-aaaa1111")
        self.assertEqual(out.kind, "ok")
        body = self.gw.requests[-1]["body"]
        self.assertEqual((body["relpath"], body["sha256"], body["created_by"], body["job_id"]),
                         ("alpha/f.sql.gz", "a" * 64, "42", "tg1-aaaa1111"))
        self.assertEqual((body["ttl_seconds"], body["max_downloads"], body["label"]), (7200, 3, "alpha/mysql8/appdb"))
        self.assertTrue(out.link["url"].startswith("https://dumps.test/d/"))
        self.assertEqual([e["event"] for e in self.audit_events()], ["job_start", "job_end", "link_created"])
        self.assertEqual(jm.running, {})

    async def test_engine_passed_when_known(self):
        await self.manager().run(DumpRequest(42, "a", "b", "c", 3600, 1, engine="postgresql"), "tg1-aaaa1111")
        with open(self.args_file) as fh:
            self.assertIn("--engine postgresql", fh.read())

    async def test_runner_error_is_reported_without_link(self):
        out = await self.manager("err").run(REQ, "tg1-aaaa1111")
        self.assertEqual((out.kind, out.code), ("dump_failed", "E_SSH"))
        self.assertIn("Connection refused", out.stderr)
        self.assertEqual([r for r in self.gw.requests if r["method"] == "POST"], [])
        self.assertFalse(self.audit_events()[-1]["ok"])

    async def test_timeout_outcome(self):
        out = await self.manager("hang", timeout=0.15).run(REQ, "tg1-aaaa1111")
        self.assertEqual(out.code, "E_TIMEOUT")
        self.assertEqual(self.audit_events()[-1]["code"], "E_TIMEOUT")

    async def test_link_failure_keeps_dump_info(self):
        self.gw.existing_files = set()
        out = await self.manager().run(REQ, "tg1-aaaa1111")
        self.assertEqual(out.kind, "link_failed")
        self.assertEqual(out.dump["filename"], "f.sql.gz")
        self.assertEqual(out.gateway_error.code, "file_not_found")
        self.assertEqual(self.audit_events()[-1]["event"], "link_failed")

    async def test_gateway_500_and_unreachable(self):
        self.gw.mode = "error500"
        out = await self.manager().run(REQ, "tg1-aaaa1111")
        self.assertEqual((out.kind, out.gateway_error.status), ("link_failed", 500))
        dead = GatewayClient("http://127.0.0.1:1", TOKEN, timeout=1)
        try:
            jm = self.manager()
            jm.gateway = dead
            out = await jm.run(REQ, "tg1-bbbb2222")
        finally:
            await dead.close()
        self.assertEqual(out.gateway_error.code, "unreachable")

    async def test_queue_respects_max_jobs_one(self):
        jm = self.manager("slow", max_jobs=1, FAKE_SLEEP="0.12")
        peak, queued_pos, started = [0], [], []

        async def watch():
            while True:
                peak[0] = max(peak[0], len(jm.running))
                await asyncio.sleep(0.01)

        async def queued(pos):
            queued_pos.append(pos)

        async def start():
            started.append(1)

        watcher = asyncio.create_task(watch())
        a = jm.spawn(jm.run(REQ, "tg1-aaaa0001", queued, start))
        await asyncio.sleep(0.03)
        b = jm.spawn(jm.run(REQ, "tg1-aaaa0002", queued, start))
        await asyncio.sleep(0.03)
        self.assertEqual(jm.status()[1], 1)
        oa, ob = await asyncio.gather(a, b)
        watcher.cancel()
        self.assertEqual((oa.kind, ob.kind), ("ok", "ok"))
        self.assertEqual(peak[0], 1)
        self.assertEqual(queued_pos, [1])
        self.assertEqual(started, [1])
        self.assertEqual(jm.status(), ([], 0))

    async def test_max_jobs_two_runs_in_parallel(self):
        jm = self.manager("slow", max_jobs=2, FAKE_SLEEP="0.12")
        peak = [0]

        async def watch():
            while True:
                peak[0] = max(peak[0], len(jm.running))
                await asyncio.sleep(0.01)

        watcher = asyncio.create_task(watch())
        outs = await asyncio.gather(*(jm.run(REQ, f"tg1-aaaa000{i}") for i in range(3)))
        watcher.cancel()
        self.assertTrue(all(o.kind == "ok" for o in outs))
        self.assertEqual(peak[0], 2)

    async def test_queue_callback_failure_does_not_break_job(self):
        jm = self.manager("slow", FAKE_SLEEP="0.06")

        async def boom(*a):
            raise RuntimeError("telegram caído")

        a = jm.spawn(jm.run(REQ, "tg1-aaaa0001"))
        await asyncio.sleep(0.02)
        out = await jm.run(REQ, "tg1-aaaa0002", boom, boom)
        await a
        self.assertEqual(out.kind, "ok")

    async def test_shutdown_cancels_jobs_and_kills_runner(self):
        jm = self.manager("hang", timeout=30, FAKE_PID_FILE=os.path.join(self.tmp.name, "pid"))
        task = jm.spawn(jm.run(REQ, "tg1-aaaa0001"))
        await asyncio.sleep(0.15)
        self.assertEqual(len(jm.running), 1)
        await jm.shutdown()
        self.assertTrue(task.cancelled() or task.done())
        self.assertEqual(jm.running, {})
        self.assertEqual(self.audit_events()[-1]["code"], "E_CANCELLED")

    async def test_cancel_releases_slot(self):
        jm = self.manager("hang", timeout=30)
        task = jm.spawn(jm.run(REQ, "tg1-aaaa0001"))
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(jm._sem.locked())

    async def test_no_secret_in_logs_or_audit(self):
        log = logging.getLogger("aiogram.client")
        log.error("fallo https://api.telegram.org/bot%s/getUpdates auth Bearer %s", BOT_TOKEN, TOKEN)
        jm = self.manager("err")
        await jm.run(REQ, "tg1-aaaa0001")
        self.gw.mode = "ok"
        await self.manager().run(REQ, "tg1-aaaa0002")
        with open(self.audit_path) as fh:
            audit = fh.read()
        for secret in (BOT_TOKEN, TOKEN, "TEST"):
            self.assertNotIn(secret, audit)
            self.assertNotIn(secret, self.log_stream.getvalue())
        self.assertNotIn("dumps.test", audit)

    async def test_audit_redacts_string_fields(self):
        Audit(self.audit_path, self.redactor).write("x", note=f"token {BOT_TOKEN}", n=3)
        rec = self.audit_events()[-1]
        self.assertEqual((rec["note"], rec["n"]), ("token ***", 3))
        self.assertEqual(oct(os.stat(self.audit_path).st_mode & 0o777), "0o600")
