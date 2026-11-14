# =============================================================================
# services/bot/tests/test_notify.py — Pruebas del aviso final: mensaje nuevo con push, progreso corto, enlace solo en <code>
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import io
import json
import logging
import os
import re
import tempfile
import unittest

from aiohttp.test_utils import TestServer

from bot import flow
from bot.audit import Audit
from bot.gateway_client import GatewayClient, GatewayError
from bot.jobs import DumpRequest, JobManager, Outcome
from bot import notify
from bot.notify import report_job
from bot.redact import Redactor, install_logging
from bot.runner_client import RunnerClient
from tests.fakes.gateway import TOKEN, FakeGateway

FAKE = os.path.join(os.path.dirname(__file__), "fakes", "fake_runner.sh")
BOT_TOKEN = "123456:TEST"
HOSTILE = "<b>x&y</b>"
REQ = DumpRequest(42, HOSTILE, "c", HOSTILE, 3600, 2)
URL = "https://d.test/d/abc_DEF-123?a=1&b=<2>"
DUMP = {"db": HOSTILE, "server": HOSTILE, "container": "c", "filename": "f.sql.gz", "size_bytes": 5 * 1024 ** 2,
        "sha256": "ab" * 32, "duration_s": 75}
LINK = {"id": "0123456789ab", "url": URL, "expires_at": "2099-01-01T00:00:00Z", "max_downloads": 2,
        "files": [{"index": 0, "filename": "f.sql.gz", "kind": "db", "size_bytes": 1, "downloads_completed": 0}]}


class Sink:
    def __init__(self, fail_send=False, fail_edit=False, error=None):
        self.events = []
        self.fail_send, self.fail_edit = fail_send, fail_edit
        self.error = error or RuntimeError("red caída")

    async def edit(self, text, keyboard=None):
        self.events.append(("edit", text, keyboard))
        if self.fail_edit:
            raise self.error

    async def send(self, text, keyboard=None):
        self.events.append(("send", text, keyboard))
        if self.fail_send:
            raise self.error

    def of(self, kind):
        return [e for e in self.events if e[0] == kind]


class StubJobs:
    def __init__(self, outcome=None, exc=None):
        self.outcome, self.exc = outcome, exc

    async def run(self, req, job_id, on_queued=None, on_start=None):
        for cb, args in ((on_queued, (1,)), (on_start, ())):
            try:
                await cb(*args)
            except Exception:
                pass
        if self.exc:
            raise self.exc
        return self.outcome


def strip_code(text):
    return re.sub(r"<code>.*?</code>", "", text, flags=re.S)


def buttons(kb):
    return [b for row in kb or [] for b in row]


class SuccessNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def run_ok(self, link=None):
        sink = Sink()
        out = Outcome("ok", "j", DUMP, link or LINK)
        await report_job(StubJobs(out), REQ, "j", sink)
        return sink

    async def test_new_message_with_summary_first_line(self):
        sink = await self.run_ok()
        (_, text, kb), = sink.of("send")
        first = text.split("\n")[0]
        self.assertEqual(first, "✅ Dump listo · &lt;b&gt;x&amp;y&lt;/b&gt;/&lt;b&gt;x&amp;y&lt;/b&gt; · 5.0 MB")
        self.assertNotIn("<b>x", text)

    async def test_url_only_inside_code_and_escaped(self):
        sink = await self.run_ok()
        text = sink.of("send")[0][1]
        self.assertNotIn("http", strip_code(text))
        self.assertIn("<code>https://d.test/d/abc_DEF-123?a=1&amp;b=&lt;2&gt;</code>", text)
        self.assertNotIn("<2>", text)

    async def test_keyboard_has_copy_text_exact_url_and_no_url_buttons(self):
        sink = await self.run_ok()
        kb = sink.of("send")[0][2]
        copy = [b for b in buttons(kb) if b.copy_text is not None]
        self.assertEqual([b.copy_text for b in copy], [URL])
        self.assertTrue(all(not hasattr(b, "url") for b in buttons(kb)))
        revoke = [b for b in buttons(kb) if b.copy_text is None]
        self.assertEqual([flow.parse_callback(b.data) for b in revoke], [("r", "0123456789ab")])
        self.assertTrue(all(len(b.data.encode()) <= 64 for b in revoke))

    async def test_long_url_omits_copy_button_but_keeps_code(self):
        sink = await self.run_ok({**LINK, "url": "https://d.test/" + "a" * 300})
        kb = sink.of("send")[0][2]
        self.assertFalse([b for b in buttons(kb) if b.copy_text is not None])
        self.assertIn("<code>https://d.test/", sink.of("send")[0][1])

    async def test_progress_edited_short_without_link(self):
        sink = await self.run_ok()
        final = sink.of("edit")[-1]
        self.assertNotIn("http", final[1])
        self.assertNotIn("d.test", final[1])
        self.assertLess(len(final[1]), 160)
        self.assertIsNone(final[2])
        self.assertEqual([e[0] for e in sink.events][-2:], ["send", "edit"])

    async def test_mentions_ttl_and_downloads(self):
        text = (await self.run_ok()).of("send")[0][1]
        self.assertIn("Vence:", text)
        self.assertIn("2 descarga(s)", text)


class FailureNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, outcome=None, exc=None, first=None, contains=()):
        sink = Sink()
        task = asyncio.ensure_future(report_job(StubJobs(outcome, exc), REQ, "j", sink))
        try:
            await task
        except asyncio.CancelledError:
            pass
        (_, text, kb), = sink.of("send")
        self.assertTrue(text.split("\n")[0].startswith(first), text)
        for needle in contains:
            self.assertIn(needle, text)
        self.assertNotIn("http", strip_code(text))
        final = sink.of("edit")[-1][1]
        self.assertLess(len(final), 160)
        self.assertNotIn("Connection", final)
        self.assertEqual([e[0] for e in sink.events][-2:], ["send", "edit"])
        return sink

    async def test_runner_failure(self):
        out = Outcome("dump_failed", "j", code="E_SSH", message="m", stderr="Connection refused <1>")
        await self.check(out, first="❌ Dump falló · E_SSH", contains=("<pre>Connection refused &lt;1&gt;</pre>",))

    async def test_timeout(self):
        out = Outcome("dump_failed", "j", code="E_TIMEOUT", message="superó 30 s")
        await self.check(out, first="❌ Dump falló · E_TIMEOUT", contains=("superó 30 s",))

    async def test_runner_reported_cancel(self):
        await self.check(Outcome("dump_failed", "j", code="E_CANCELLED"), first="❌ Dump falló · E_CANCELLED")

    async def test_task_cancel_still_notifies_then_reraises(self):
        sink = Sink()
        started = asyncio.Event()

        class Hang:
            async def run(self, req, job_id, on_queued=None, on_start=None):
                started.set()
                await asyncio.sleep(30)

        task = asyncio.ensure_future(report_job(Hang(), REQ, "j", sink))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(sink.of("send")[0][1].startswith("❌ Dump falló · E_CANCELLED"))
        self.assertIn("cancelado", sink.of("edit")[-1][1])

    async def test_link_failed_with_existing_dump(self):
        out = Outcome("link_failed", "j", DUMP, None, gateway_error=GatewayError(None, "unreachable", ""))
        await self.check(out, first="⚠️ Dump listo, pero el enlace falló", contains=("dump se generó", "conectar con el gateway"))

    async def test_queued_progress_is_an_edit_not_a_push(self):
        sink = Sink()
        await report_job(StubJobs(Outcome("ok", "j", DUMP, LINK)), REQ, "j", sink)
        edits = [e[1] for e in sink.of("edit")]
        self.assertTrue(edits[0].startswith("Hay un dump en curso"))
        self.assertTrue(edits[1].startswith("Generando dump"))
        self.assertEqual(len(sink.of("send")), 1)


class FlakySink(Sink):
    """Falla los primeros `fails` envíos y luego entrega."""

    def __init__(self, fails):
        super().__init__()
        self.fails = fails

    async def send(self, text, keyboard=None):
        self.events.append(("send", text, keyboard))
        if len([e for e in self.events if e[0] == "send"]) <= self.fails:
            raise self.error


def setUpModule():
    global _saved_delays
    _saved_delays = notify.RETRY_DELAYS
    notify.RETRY_DELAYS = (0.0, 0.0)


def tearDownModule():
    notify.RETRY_DELAYS = _saved_delays


class DeliveryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_failure_is_retried_and_link_delivered(self):
        sink = FlakySink(fails=2)
        with self.assertLogs("bot.notify", "WARNING"):
            await report_job(StubJobs(Outcome("ok", "j", DUMP, LINK)), REQ, "j", sink)
        self.assertEqual(len(sink.of("send")), 3)
        self.assertIn(URL.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), sink.of("send")[-1][1])
        self.assertIn("El detalle está en el mensaje siguiente", sink.of("edit")[-1][1])

    async def test_retries_are_bounded(self):
        sink = Sink(fail_send=True)
        with self.assertLogs("bot.notify", "WARNING"):
            await report_job(StubJobs(Outcome("ok", "j", DUMP, LINK)), REQ, "j", sink)
        self.assertEqual(len(sink.of("send")), 3)

    async def test_send_failure_keeps_progress_short_and_without_link(self):
        sink = Sink(fail_send=True)
        with self.assertLogs("bot.notify", "WARNING"):
            out = await report_job(StubJobs(Outcome("ok", "j", DUMP, LINK)), REQ, "j", sink, delays=(0, 0))
        self.assertEqual(out.kind, "ok")
        final = sink.of("edit")[-1]
        self.assertNotIn("http", final[1])
        self.assertNotIn("siguiente", final[1])
        self.assertIsNone(final[2])
        self.assertEqual(len(sink.of("send")), 3)

    async def test_total_failure_does_not_raise_and_does_not_leak_token(self):
        stream = io.StringIO()
        install_logging(Redactor([BOT_TOKEN, TOKEN]), stream=stream)
        self.addCleanup(logging.getLogger().handlers.clear)
        err = RuntimeError(f"POST https://api.telegram.org/bot{BOT_TOKEN}/sendMessage bloqueado Bearer {TOKEN}")
        sink = Sink(fail_send=True, fail_edit=True, error=err)
        out = await report_job(StubJobs(Outcome("ok", "j", DUMP, LINK)), REQ, "j", sink, delays=(0, 0))
        self.assertEqual(out.kind, "ok")
        log = stream.getvalue()
        self.assertIn("No se pudo enviar el aviso final", log)
        for secret in (BOT_TOKEN, "TEST", TOKEN):
            self.assertNotIn(secret, log)


class BackgroundJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_completes_without_any_reachable_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = FakeGateway()
            server = TestServer(gw.app)
            await server.start_server()
            gateway = GatewayClient(str(server.make_url("")).rstrip("/"), TOKEN, timeout=2)
            audit_path = os.path.join(tmp, "audit.jsonl")
            jm = JobManager(RunnerClient(FAKE, timeout=5, env={"FAKE_MODE": "ok"}), gateway, Audit(audit_path, Redactor()), 1)
            sink = Sink(fail_send=True, fail_edit=True)
            task = jm.spawn(report_job(jm, DumpRequest(42, "alpha", "c", "appdb", 3600, 1), "tg1-aaaa1111", sink, delays=(0, 0)))
            await asyncio.wait_for(asyncio.shield(task), 5)
            await gateway.close()
            await server.close()
            self.assertEqual(jm.running, {})
            self.assertEqual(len(gw.links), 1)
            with open(audit_path) as fh:
                events = [json.loads(line)["event"] for line in fh]
            self.assertEqual(events, ["job_start", "job_end", "link_created"])
            self.assertTrue(task.done() and task.exception() is None)

    async def test_real_flow_notice_has_summary_and_code_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = FakeGateway()
            server = TestServer(gw.app)
            await server.start_server()
            gateway = GatewayClient(str(server.make_url("")).rstrip("/"), TOKEN, timeout=2)
            jm = JobManager(RunnerClient(FAKE, timeout=5, env={"FAKE_MODE": "ok"}), gateway, Audit(os.path.join(tmp, "a"), Redactor()), 1)
            sink = Sink()
            await report_job(jm, DumpRequest(42, "alpha", "c", "appdb", 3600, 1), "tg1-aaaa1111", sink)
            await gateway.close()
            await server.close()
            text = sink.of("send")[0][1]
            self.assertTrue(text.startswith("✅ Dump listo · alpha/appdb · 1.2 MB\n"))
            self.assertNotIn("http", strip_code(text))
            self.assertEqual(len([b for b in buttons(sink.of("send")[0][2]) if b.copy_text]), 1)


if __name__ == "__main__":
    unittest.main()
