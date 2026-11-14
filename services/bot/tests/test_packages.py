# =============================================================================
# services/bot/tests/test_packages.py — Pruebas de paquetes: código y/o BD en paralelo, entrega parcial, cupo, cancelación
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

from bot import flow
from bot.audit import Audit
from bot.gateway_client import GatewayClient
from bot.gitlab_client import GitlabClient
from bot.jobs import DumpRequest, JobManager
from bot.packages import PackageRequest, sub_job_ids
from bot.redact import Redactor, install_logging
from bot.runner_client import JOB_ID_RE, RunnerClient, RunnerError
from tests.fakes.gateway import TOKEN, FakeGateway

HERE = os.path.dirname(__file__)
FETCH = os.path.join(HERE, "fakes", "fake_fetch.py")
RUNNER = os.path.join(HERE, "fakes", "fake_runner.sh")
DB = ("alpha", "mysql8", "appdb")
SHA = "a" * 64


def req(what="both", **over):
    args = dict(user_id=42, project="alpha-app", ref="main", sha="d" * 40, fmt="zip", what=what, ttl=3600, max_downloads=2,
                db=DB if what in ("db", "both") else None)
    args.update(over)
    return PackageRequest(**args)


class StubGitlab:
    def __init__(self, delay=0.0, error=None):
        self.delay, self.error, self.calls, self.spans, self.killed = delay, error, [], [], False

    async def archive(self, project, ref, fmt, job_id):
        loop = asyncio.get_running_loop()
        start = loop.time()
        self.calls.append((project, ref, fmt, job_id))
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.spans.append((start, loop.time()))
        if self.error:
            raise self.error
        return {"relpath": f"_gitlab/{job_id}.zip", "filename": f"{job_id}.zip", "size_bytes": 654321, "sha256": "c" * 64}

    def kill_all(self):
        self.killed = True


class StubRunner:
    def __init__(self, delay=0.0, error=None):
        self.delay, self.error, self.calls, self.spans, self.killed = delay, error, [], [], False

    async def dump(self, server, container, db, job_id, engine=None):
        loop = asyncio.get_running_loop()
        start = loop.time()
        self.calls.append((server, container, db, job_id))
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.spans.append((start, loop.time()))
        if self.error:
            raise self.error
        return {"relpath": f"{server}/{job_id}.sql.gz", "filename": f"{job_id}.sql.gz", "size_bytes": 1234567, "sha256": SHA,
                "duration_s": 1, "server": server, "container": container, "db": db}

    def kill_all(self):
        self.killed = True


def dead(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


class PackageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit_path = os.path.join(self.tmp.name, "audit.jsonl")
        self.redactor = Redactor(["123456:TEST", TOKEN])
        self.log_stream = io.StringIO()
        install_logging(self.redactor, stream=self.log_stream)
        self.addCleanup(logging.getLogger().handlers.clear)
        self.gw = FakeGateway()
        self.server = TestServer(self.gw.app)
        await self.server.start_server()
        self.gateway = GatewayClient(str(self.server.make_url("")).rstrip("/"), TOKEN, timeout=2, redactor=self.redactor)

    async def asyncTearDown(self):
        await self.gateway.close()
        await self.server.close()

    def manager(self, runner=None, gitlab=None, max_jobs=1):
        return JobManager(runner or StubRunner(), self.gateway, Audit(self.audit_path, self.redactor), max_jobs, gitlab or StubGitlab())

    def audit(self):
        with open(self.audit_path) as fh:
            return [json.loads(line) for line in fh]

    async def test_both_ok_one_link_with_code_then_db(self):
        jm = self.manager()
        out = await jm.run(req(), "tg1-aaaa1111")
        self.assertEqual(out.kind, "ok")
        body = self.gw.requests[-1]["body"]
        self.assertEqual([f["relpath"] for f in body["files"]], ["_gitlab/tg1-aaaa1111-code.zip", "alpha/tg1-aaaa1111-db.sql.gz"])
        self.assertEqual([f["sha256"] for f in body["files"]], ["c" * 64, SHA])
        self.assertEqual((body["max_downloads"], body["ttl_seconds"], body["label"], body["created_by"]), (2, 3600, "alpha-app@main", "42"))
        self.assertEqual(len(self.gw.links), 1)
        self.assertEqual([f["kind"] for f in out.link["files"]], ["code", "db"])
        events = [e["event"] for e in self.audit()]
        self.assertEqual(events, ["job_start", "job_end", "link_created"])
        self.assertEqual(jm.running, {})

    async def test_sub_job_ids_are_distinct_and_valid(self):
        a, b = sub_job_ids("tg1-aaaa1111")
        self.assertNotEqual(a, b)
        long_id = "tg" + "9" * 70
        for sub in (*sub_job_ids("tg1-aaaa1111"), *sub_job_ids(long_id)):
            self.assertRegex(sub, JOB_ID_RE.pattern.replace("\\Z", "$"))
            self.assertLessEqual(len(sub), 64)
        gl, rn = StubGitlab(), StubRunner()
        await self.manager(rn, gl).run(req(), "tg1-aaaa1111")
        self.assertEqual((gl.calls[0][3], rn.calls[0][3]), ("tg1-aaaa1111-code", "tg1-aaaa1111-db"))
        self.assertEqual(gl.calls[0][:3], ("alpha-app", "main", "zip"))
        self.assertEqual(rn.calls[0][:3], DB)

    async def test_code_only_and_db_only_do_not_touch_the_other_side(self):
        gl, rn = StubGitlab(), StubRunner()
        jm = self.manager(rn, gl)
        out = await jm.run(req("code"), "tg1-aaaa0001")
        self.assertEqual((out.kind, len(out.link["files"]), len(rn.calls)), ("ok", 1, 0))
        out = await jm.run(req("db"), "tg1-aaaa0002")
        self.assertEqual((out.kind, len(out.link["files"]), len(gl.calls)), ("ok", 1, 1))

    async def test_both_run_in_parallel(self):
        gl, rn = StubGitlab(0.05), StubRunner(0.05)
        out = await self.manager(rn, gl).run(req(), "tg1-aaaa1111")
        self.assertEqual(out.kind, "ok")
        (gs, ge), (rs, re_) = gl.spans[0], rn.spans[0]
        self.assertLess(max(gs, rs), min(ge, re_))

    async def test_partial_when_db_fails_and_when_code_fails(self):
        out = await self.manager(StubRunner(error=RunnerError("E_SSH", "x"))).run(req(), "tg1-aaaa0001")
        self.assertEqual(out.kind, "partial")
        self.assertEqual([(p.kind, p.ok, p.code) for p in out.parts], [("code", True, ""), ("db", False, "E_SSH")])
        self.assertEqual([f["relpath"].split("/")[0] for f in self.gw.requests[-1]["body"]["files"]], ["_gitlab"])
        out = await self.manager(gitlab=StubGitlab(error=RunnerError("E_AUTH", "x"))).run(req(), "tg1-aaaa0002")
        self.assertEqual(out.kind, "partial")
        self.assertEqual([(p.kind, p.ok, p.code) for p in out.parts], [("code", False, "E_AUTH"), ("db", True, "")])
        self.assertEqual([f["relpath"].split("/")[0] for f in self.gw.requests[-1]["body"]["files"]], ["alpha"])
        self.assertEqual(len(self.gw.links), 2)

    async def test_total_failure_creates_no_link(self):
        jm = self.manager(StubRunner(error=RunnerError("E_SSH", "x")), StubGitlab(error=RunnerError("E_NOT_FOUND", "x")))
        out = await jm.run(req(), "tg1-aaaa0001")
        self.assertEqual(out.kind, "failed")
        self.assertEqual({p.code for p in out.parts}, {"E_SSH", "E_NOT_FOUND"})
        self.assertEqual([r for r in self.gw.requests if r["method"] == "POST"], [])
        self.assertFalse(self.audit()[-1]["ok"])

    async def test_a_part_cancelled_on_its_own_does_not_sink_the_other(self):
        class Cancelled(StubRunner):
            async def dump(self, *a, **k):
                raise asyncio.CancelledError()

        out = await self.manager(Cancelled()).run(req(), "tg1-aaaa0001")
        self.assertEqual((out.kind, [(p.kind, p.code) for p in out.bad]), ("partial", [("db", "E_CANCELLED")]))
        self.assertEqual(len(self.gw.links), 1)

    async def test_unexpected_exception_in_a_part_is_contained(self):
        out = await self.manager(StubRunner(error=ValueError("boom"))).run(req(), "tg1-aaaa0001")
        self.assertEqual((out.kind, out.bad[0].code), ("partial", "E_INTERNAL"))

    async def test_link_failure_keeps_part_results(self):
        self.gw.mode = "error500"
        out = await self.manager().run(req(), "tg1-aaaa0001")
        self.assertEqual(out.kind, "link_failed")
        self.assertEqual(len(out.good), 2)
        self.assertEqual(out.gateway_error.status, 500)
        self.assertEqual(self.audit()[-1]["event"], "link_failed")
        self.gw.mode = "ok"
        self.gw.existing_files = set()
        out = await self.manager().run(req(), "tg1-aaaa0002")
        self.assertEqual((out.kind, out.gateway_error.code), ("link_failed", "file_not_found"))

    async def test_one_slot_per_package_and_shared_with_dumps(self):
        gl, rn = StubGitlab(0.025), StubRunner(0.025)
        jm = self.manager(rn, gl, max_jobs=1)
        peak = [0]

        async def watch():
            while True:
                peak[0] = max(peak[0], len(jm.running))
                await asyncio.sleep(0.005)

        watcher = asyncio.create_task(watch())
        queued = []

        async def on_queued(pos):
            queued.append(pos)

        a = jm.spawn(jm.run(req(), "tg1-aaaa0001"))
        await asyncio.sleep(0.01)
        b = jm.spawn(jm.run(req("db"), "tg1-aaaa0002", on_queued))
        c = jm.spawn(jm.run(DumpRequest(42, "alpha", "mysql8", "appdb", 3600, 1), "tg1-aaaa0003", on_queued))
        await asyncio.sleep(0.01)
        self.assertEqual(jm.status()[1], 2)
        outs = await asyncio.gather(a, b, c)
        watcher.cancel()
        self.assertEqual([o.kind for o in outs], ["ok", "ok", "ok"])
        self.assertEqual(peak[0], 1)
        self.assertEqual(queued, [1, 2])
        spans = sorted(gl.spans + rn.spans)
        self.assertEqual(len(spans), 4)

    async def test_status_shows_package_label(self):
        jm = self.manager(gitlab=StubGitlab(0.05))
        task = jm.spawn(jm.run(req(), "tg1-aaaa0001"))
        await asyncio.sleep(0.02)
        running, _ = jm.status()
        await task
        self.assertEqual(running[0].label, "alpha-app@main (both)")

    async def test_cancel_cancels_both_parts_and_audits(self):
        gl, rn = StubGitlab(30), StubRunner(30)
        jm = self.manager(rn, gl)
        task = jm.spawn(jm.run(req(), "tg1-aaaa0001"))
        await asyncio.sleep(0.03)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((len(gl.spans), len(rn.spans)), (1, 1))
        self.assertEqual(jm.running, {})
        self.assertFalse(jm._sem.locked())
        self.assertEqual(self.audit()[-1]["code"], "E_CANCELLED")

    async def test_shutdown_kills_both_clients(self):
        gl, rn = StubGitlab(30), StubRunner(30)
        jm = self.manager(rn, gl)
        jm.spawn(jm.run(req(), "tg1-aaaa0001"))
        await asyncio.sleep(0.03)
        await jm.shutdown()
        self.assertTrue(gl.killed and rn.killed)

    async def test_secrets_never_reach_logs_or_audit(self):
        err = RunnerError("E_AUTH", "token glpat" "-ABCDEF-123456 rechazado")
        await self.manager(gitlab=StubGitlab(error=err)).run(req(), "tg1-aaaa0001")
        logging.getLogger("bot.gitlab").error("fallo con glpat" "-ABCDEF-123456 y Bearer %s", TOKEN)
        with open(self.audit_path) as fh:
            audit = fh.read()
        for secret in ("glpat" "-ABCDEF", "123456:TEST", TOKEN, "dumps.test"):
            self.assertNotIn(secret, audit)
            self.assertNotIn(secret, self.log_stream.getvalue())


class RealProcessTests(unittest.IsolatedAsyncioTestCase):
    """Procesos reales (fetch.py y runner falsos): el timeout y la cancelación matan los grupos de ambas partes."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gw = FakeGateway()
        self.server = TestServer(self.gw.app)
        await self.server.start_server()
        self.gateway = GatewayClient(str(self.server.make_url("")).rstrip("/"), TOKEN, timeout=2)
        self.pids = {"gl": os.path.join(self.tmp.name, "gl"), "rn": os.path.join(self.tmp.name, "rn")}

    async def asyncTearDown(self):
        await self.gateway.close()
        await self.server.close()

    def manager(self, gl_mode, rn_mode, timeout):
        gitlab = GitlabClient(FETCH, timeout=timeout, kill_grace=0.1, env={"FAKE_FETCH_MODE": gl_mode, "FAKE_GL_PID_FILE": self.pids["gl"]})
        runner = RunnerClient(RUNNER, timeout=timeout, kill_grace=0.1, env={"FAKE_MODE": rn_mode, "FAKE_PID_FILE": self.pids["rn"]})
        return JobManager(runner, self.gateway, Audit(os.path.join(self.tmp.name, "a.jsonl"), Redactor()), 1, gitlab)

    def alive_children(self):
        out = []
        for path in self.pids.values():
            if os.path.exists(path):
                with open(path) as fh:
                    out.append(dead(int(fh.read())))
        return out

    async def test_timeout_kills_both_parts_and_creates_no_link(self):
        out = await self.manager("hang", "hang", 0.12).run(req(), "tg1-aaaa1111")
        self.assertEqual((out.kind, sorted((p.kind, p.code) for p in out.parts)), ("failed", [("code", "E_TIMEOUT"), ("db", "E_TIMEOUT")]))
        self.assertEqual(len(self.alive_children()), 2)
        self.assertTrue(all(self.alive_children()), "quedaron procesos hijos vivos")
        self.assertEqual(self.gw.links, {})
        self.assertNotIn("runner", flow.part_error_text(next(p for p in out.parts if p.kind == "code")))

    async def test_cancel_kills_both_process_groups(self):
        jm = self.manager("hang", "hang", 30)
        task = jm.spawn(jm.run(req(), "tg1-aaaa1111"))
        for _ in range(100):
            if all(os.path.exists(p) for p in self.pids.values()):
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(self.alive_children()), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(50):
            if all(self.alive_children()):
                break
            await asyncio.sleep(0.01)
        self.assertTrue(all(self.alive_children()), "quedaron procesos hijos vivos")


if __name__ == "__main__":
    unittest.main()
