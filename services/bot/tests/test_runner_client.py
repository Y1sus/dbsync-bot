# =============================================================================
# services/bot/tests/test_runner_client.py — Pruebas del cliente del runner con scripts bash falsos
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import os
import tempfile
import time
import unittest

from bot.runner_client import RunnerClient, RunnerError, validate_result

FAKE = os.path.join(os.path.dirname(__file__), "fakes", "fake_runner.sh")
GOOD_ID = "tg1-abcd1234"


def pid_alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def read_pid(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return ""


async def wait_pid(path):
    for _ in range(150):
        if read_pid(path):
            return int(read_pid(path))
        await asyncio.sleep(0.02)
    raise AssertionError("el runner falso no escribió su pid")


async def wait_dead(pid, limit=2.0):
    end = time.monotonic() + limit
    while pid_alive(pid) and time.monotonic() < end:
        await asyncio.sleep(0.02)
    return not pid_alive(pid)


class RunnerClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.args_file = os.path.join(self.tmp.name, "args")
        self.pid_file = os.path.join(self.tmp.name, "pid")

    def client(self, mode="ok", timeout=5, **extra):
        env = {"FAKE_MODE": mode, "FAKE_ARGS_FILE": self.args_file, "FAKE_PID_FILE": self.pid_file, **extra}
        return RunnerClient(FAKE, timeout=timeout, env=env, kill_grace=0.3)

    async def assert_flood_rejected(self, mode, stream, kib):
        with self.assertRaises(RunnerError) as cm:
            await self.client(mode, timeout=10).dump("a", "b", "c", GOOD_ID)
        self.assertEqual(cm.exception.code, "E_BAD_OUTPUT")
        msg = cm.exception.message
        self.assertIn(stream, msg)
        self.assertIn(f"{kib} KiB", msg)
        self.assertNotIn("xxxx", msg)
        self.assertLess(len(msg), 200)
        pid = int(read_pid(self.pid_file))
        for _ in range(50):
            if not pid_alive(pid):
                break
            await asyncio.sleep(0.01)
        self.assertFalse(pid_alive(pid), "quedó un proceso huérfano")

    async def test_stdout_over_cap_kills_group_and_reports_size_only(self):
        await self.assert_flood_rejected("flood_out", "stdout", 1024)

    async def test_stderr_over_cap_kills_group_and_reports_size_only(self):
        await self.assert_flood_rejected("flood_err", "stderr", 256)

    async def test_slow_output_within_cap_still_works(self):
        res = await self.client("trickle").dump("a", "b", "c", GOOD_ID)
        self.assertEqual(res["filename"], "f.sql.gz")

    def test_caps_are_one_mib_and_256_kib(self):
        from bot import runner_client
        self.assertEqual((runner_client.STDOUT_MAX, runner_client.STDERR_MAX), (1024 * 1024, 256 * 1024))

    def recorded(self):
        with open(self.args_file) as fh:
            return fh.read().splitlines()

    async def test_listings(self):
        c = self.client()
        self.assertEqual([s["name"] for s in await c.servers()], ["alpha", "beta"])
        conts = await c.containers("alpha")
        self.assertEqual(conts[1]["db_count"], None)
        res = await c.dbs("alpha", "mysql8")
        self.assertEqual(res["databases"][1], "<b>evil&db</b>")
        self.assertEqual(self.recorded(), ["servers", "containers --server alpha", "dbs --server alpha --container mysql8"])

    async def test_dump_args_and_result(self):
        r = await self.client().dump("alpha", "mysql8", "appdb", GOOD_ID, engine="mysql")
        self.assertEqual(r["job_id"], GOOD_ID)
        self.assertEqual(r["relpath"], "alpha/f.sql.gz")
        self.assertEqual(self.recorded(), [f"dump --server alpha --container mysql8 --db appdb --job-id {GOOD_ID} --engine mysql"])

    async def test_no_shell_interpretation(self):
        marker = os.path.join(self.tmp.name, "pwned")
        await self.client().dump("alpha", "c; touch " + marker, "$(touch " + marker + ")", GOOD_ID)
        self.assertFalse(os.path.exists(marker))
        self.assertIn("--container c; touch", self.recorded()[0])

    async def test_invalid_job_id_rejected(self):
        for bad in ("", "-x", "a b", "a;b", "x" * 65, "ñ", "a\n"):
            with self.assertRaises(RunnerError, msg=bad) as cm:
                await self.client().dump("a", "b", "c", bad)
            self.assertEqual(cm.exception.code, "E_USAGE")
        self.assertFalse(os.path.exists(self.args_file))

    async def test_error_json_exit1(self):
        with self.assertRaises(RunnerError) as cm:
            await self.client("err").dump("a", "b", "c", GOOD_ID)
        self.assertEqual(cm.exception.code, "E_SSH")
        self.assertIn("Connection refused", cm.exception.stderr)

    async def test_exit2_usage(self):
        with self.assertRaises(RunnerError) as cm:
            await self.client("usage").dump("a", "b", "c", GOOD_ID)
        self.assertEqual(cm.exception.code, "E_USAGE")

    async def test_empty_garbage_and_incomplete_output(self):
        for mode, snippet in (("empty", "no devolvió"), ("garbage", "ilegible"), ("badjson", "Falta el campo")):
            with self.assertRaises(RunnerError, msg=mode) as cm:
                await self.client(mode).dump("a", "b", "c", GOOD_ID)
            self.assertEqual(cm.exception.code, "E_BAD_OUTPUT")
            self.assertIn(snippet, cm.exception.message)

    async def test_runner_not_executable(self):
        with self.assertRaises(RunnerError) as cm:
            await RunnerClient("/nonexistent/runner.sh").servers()
        self.assertEqual(cm.exception.code, "E_RUNNER_START")

    async def test_timeout_kills_whole_process_group(self):
        t0 = time.monotonic()
        with self.assertRaises(RunnerError) as cm:
            await self.client("hang", timeout=0.06).dump("a", "b", "c", GOOD_ID)
        self.assertEqual(cm.exception.code, "E_TIMEOUT")
        self.assertLess(time.monotonic() - t0, 3)
        child = await wait_pid(self.pid_file)
        self.assertTrue(await wait_dead(child), "el hijo (tipo ssh) sigue vivo")

    async def test_cancel_kills_group(self):
        c = self.client("hang", timeout=30)
        task = asyncio.create_task(c.dump("a", "b", "c", GOOD_ID))
        child = await wait_pid(self.pid_file)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(await wait_dead(child))
        self.assertEqual(c._procs, set())

    async def test_kill_all(self):
        c = self.client("hang", timeout=30)
        task = asyncio.create_task(c.dump("a", "b", "c", GOOD_ID))
        child = await wait_pid(self.pid_file)
        c.kill_all()
        with self.assertRaises(RunnerError):
            await task
        self.assertTrue(await wait_dead(child))

    async def test_environment_inherited(self):
        os.environ["DBSYNC_TEST_INHERIT"] = "1"
        self.addCleanup(os.environ.pop, "DBSYNC_TEST_INHERIT", None)
        c = RunnerClient(FAKE, env={"FAKE_MODE": "ok"})
        self.assertEqual(c._environment()["DBSYNC_TEST_INHERIT"], "1")

    async def test_stderr_redacted(self):
        c = RunnerClient(FAKE, env={"FAKE_MODE": "err"}, redactor=lambda t: t.replace("refused", "***"))
        with self.assertRaises(RunnerError) as cm:
            await c.dump("a", "b", "c", GOOD_ID)
        self.assertEqual(cm.exception.code, "E_SSH")


class ValidateTests(unittest.TestCase):
    def dump(self, **over):
        d = {"schema_version": 1, "ok": True, "command": "dump", "job_id": "j", "server": "a", "container": "b", "engine": "mysql",
             "db": "c", "file": "/d/a/f.sql.gz", "relpath": "a/f.sql.gz", "filename": "f.sql.gz", "size_bytes": 10,
             "sha256": "a" * 64, "duration_s": 0, "verified": {"gzip": True, "trailer": True}}
        d.update(over)
        return d

    def test_valid(self):
        self.assertEqual(validate_result(self.dump(), "dump")["db"], "c")

    def test_invalid_variants(self):
        bad = [
            self.dump(schema_version=2), self.dump(ok="yes"), self.dump(command="servers"), self.dump(sha256="XYZ"),
            self.dump(size_bytes=0), self.dump(size_bytes=True), self.dump(duration_s=-1), self.dump(engine="oracle"),
            self.dump(verified={"gzip": True}), self.dump(verified={"gzip": True, "trailer": False}),
            self.dump(relpath="../etc/x"), self.dump(relpath="/abs/x"), self.dump(filename=3), "texto", [], None,
        ]
        for b in bad:
            with self.assertRaises(RunnerError, msg=repr(b)) as cm:
                validate_result(b, "dump")
            self.assertEqual(cm.exception.code, "E_BAD_OUTPUT")

    def test_missing_each_required_field(self):
        for f in ("job_id", "server", "container", "engine", "db", "file", "relpath", "filename", "size_bytes", "sha256", "duration_s", "verified"):
            d = self.dump()
            del d[f]
            with self.assertRaises(RunnerError, msg=f):
                validate_result(d, "dump")

    def test_error_result_maps_code(self):
        err = {"schema_version": 1, "ok": False, "command": "dump", "error": {"code": "E_DISK", "message": "m", "stderr": "s"}}
        with self.assertRaises(RunnerError) as cm:
            validate_result(err, "dump")
        self.assertEqual((cm.exception.code, cm.exception.stderr), ("E_DISK", "s"))
        err["error"]["code"] = "E_NUEVO"
        with self.assertRaises(RunnerError) as cm:
            validate_result(err, "dump")
        self.assertEqual(cm.exception.code, "E_INTERNAL")

    def test_listing_shapes(self):
        with self.assertRaises(RunnerError):
            validate_result({"schema_version": 1, "ok": True, "command": "servers", "servers": [{"name": "a"}]}, "servers")
        with self.assertRaises(RunnerError):
            validate_result({"schema_version": 1, "ok": True, "command": "dbs", "server": "a", "container": "b", "engine": "mysql", "databases": [1]}, "dbs")
        with self.assertRaises(RunnerError):
            validate_result({"schema_version": 1, "ok": True, "command": "containers", "server": "a",
                             "containers": [{"name": "x", "engine": "mysql", "image": "i", "db_count": "2"}]}, "containers")
