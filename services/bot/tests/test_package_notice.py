# =============================================================================
# services/bot/tests/test_package_notice.py — Pruebas del aviso final de paquetes: resumen, archivos, parcial, fallos y secretos
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import io
import logging
import re
import unittest

from bot import flow, notify
from bot.gateway_client import GatewayError
from bot.notify import report_job
from bot.packages import PackageOutcome, PackageRequest, Part
from bot.redact import Redactor, install_logging
from tests.test_notify import Sink, StubJobs, buttons, strip_code

HOSTILE = "<b>x&y</b>"
URL = "https://d.test/d/abc_DEF-123?a=1&b=<2>"
REQ = PackageRequest(42, HOSTILE, "feat/<i>", "d" * 40, "zip", "both", 3600, 3, ("a", "b", "c"))
COMMIT = "d" * 40
CODE = {"commit": COMMIT, "filename": "pro<j>.zip", "size_bytes": 5 * 1024 ** 2, "relpath": "_gitlab/pro.zip", "sha256": "c" * 64}
DB = {"filename": "f.sql.gz", "size_bytes": 2 * 1024 ** 2, "relpath": "alpha/f.sql.gz", "sha256": "a" * 64}
LINK = {"id": "0123456789ab", "url": URL, "expires_at": "2099-01-01T00:00:00Z", "max_downloads": 3,
        "files": [{"index": 0, "filename": "pro.zip", "kind": "code", "size_bytes": 1, "downloads_completed": 0},
                  {"index": 1, "filename": "f.sql.gz", "kind": "db", "size_bytes": 1, "downloads_completed": 0}]}
OK_CODE, OK_DB = Part("code", True, CODE), Part("db", True, DB)


def fail(kind, code, message=""):
    return Part(kind, False, code=code, message=message)


async def notice(outcome, req=REQ):
    sink = Sink()
    await report_job(StubJobs(outcome), req, "j", sink, delays=(0, 0))
    return sink


class PackageNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_both(self):
        sink = await notice(PackageOutcome("ok", "j", [OK_CODE, OK_DB], LINK))
        (_, text, kb), = sink.of("send")
        lines = text.split("\n")
        self.assertEqual(lines[0], "✅ Listo · &lt;b&gt;x&amp;y&lt;/b&gt;@feat/&lt;i&gt; · código + BD")
        self.assertIn("📦 código: <code>pro&lt;j&gt;.zip</code> · 5.0 MB", text)
        self.assertIn("🗄 BD: <code>f.sql.gz</code> · 2.0 MB", text)
        self.assertLess(text.index("📦"), text.index("🗄"))
        self.assertIn("Descargas permitidas: 3 por archivo", text)
        self.assertIn("Vence:", text)
        self.assertNotIn("<b>x", text)
        self.assertNotIn("<i>", text)

    async def test_url_only_in_code_copy_button_and_revoke(self):
        sink = await notice(PackageOutcome("ok", "j", [OK_CODE, OK_DB], LINK))
        (_, text, kb), = sink.of("send")
        self.assertNotIn("http", strip_code(text))
        self.assertIn("<code>https://d.test/d/abc_DEF-123?a=1&amp;b=&lt;2&gt;</code>", text)
        self.assertEqual([b.copy_text for b in buttons(kb) if b.copy_text], [URL])
        self.assertEqual([flow.parse_callback(b.data) for b in buttons(kb) if not b.copy_text], [("r", "0123456789ab")])

    async def test_progress_edit_is_short_without_link_or_detail(self):
        sink = await notice(PackageOutcome("ok", "j", [OK_CODE, OK_DB], LINK))
        final = sink.of("edit")[-1][1]
        self.assertLess(len(final), 160)
        self.assertNotIn("http", final)
        self.assertNotIn("zip", final)
        self.assertEqual([e[0] for e in sink.events][-2:], ["send", "edit"])

    async def test_summary_labels_for_single_part(self):
        sink = await notice(PackageOutcome("ok", "j", [OK_CODE], LINK), PackageRequest(1, "p", "main", "", "zip", "code", 60, 1))
        self.assertTrue(sink.of("send")[0][1].startswith("✅ Listo · p@main · código\n"))
        sink = await notice(PackageOutcome("ok", "j", [OK_DB], LINK), PackageRequest(1, "p", "main", "", "zip", "db", 60, 1, ("a", "b", "c")))
        self.assertTrue(sink.of("send")[0][1].startswith("✅ Listo · p@main · BD\n"))

    async def test_partial_db_failed_names_which_and_why(self):
        sink = await notice(PackageOutcome("partial", "j", [OK_CODE, fail("db", "E_SSH")], LINK))
        text = sink.of("send")[0][1]
        self.assertTrue(text.split("\n")[0].startswith("⚠️ Entrega parcial · "))
        self.assertIn("código listo, BD falló", text.split("\n")[0])
        self.assertIn("❌ BD (E_SSH): No se pudo conectar por SSH al servidor.", text)
        self.assertIn("pro&lt;j&gt;.zip", text)
        self.assertNotIn("f.sql.gz", text)
        self.assertIn("<code>https://", text)
        self.assertEqual(len(buttons(sink.of("send")[0][2])), 2)
        self.assertIn("parcial", sink.of("edit")[-1][1])

    async def test_partial_code_failed_uses_gitlab_message(self):
        sink = await notice(PackageOutcome("partial", "j", [fail("code", "E_AUTH"), OK_DB], LINK))
        text = sink.of("send")[0][1]
        self.assertIn("código falló, BD listo", text.split("\n")[0])
        self.assertIn("❌ código (E_AUTH): Problema con el token de GitLab", text)
        self.assertIn("f.sql.gz", text)

    async def test_total_failure_has_no_link_or_buttons(self):
        sink = await notice(PackageOutcome("failed", "j", [fail("code", "E_NOT_FOUND"), fail("db", "E_TIMEOUT", "superó 30 s")]))
        (_, text, kb), = sink.of("send")
        self.assertTrue(text.startswith("❌ Entrega falló · "))
        self.assertIn("código y BD", text.split("\n")[0])
        self.assertIn("❌ código (E_NOT_FOUND): El proyecto o la referencia no existe en GitLab.", text)
        self.assertIn("❌ BD (E_TIMEOUT)", text)
        self.assertIn("superó 30 s", text)
        self.assertNotIn("http", text)
        self.assertFalse(kb)
        self.assertIn("falló", sink.of("edit")[-1][1])

    async def test_link_failed_lists_existing_files_and_reason(self):
        err = GatewayError(None, "unreachable", "")
        sink = await notice(PackageOutcome("link_failed", "j", [OK_CODE, fail("db", "E_SSH")], gateway_error=err))
        (_, text, kb), = sink.of("send")
        self.assertTrue(text.startswith("⚠️ Archivos listos, pero el enlace falló"))
        self.assertIn("pro&lt;j&gt;.zip", text)
        self.assertIn("conectar con el gateway", text)
        self.assertIn("BD (E_SSH)", text)
        self.assertNotIn("http", text)
        self.assertFalse(kb)

    async def test_secrets_in_part_messages_never_reach_the_user(self):
        secret = "glpat" "-SECRETO-123456"
        parts = [fail("code", "E_AUTH", f"token {secret} rechazado"), fail("db", "E_SSH", f"x {secret}")]
        sink = await notice(PackageOutcome("failed", "j", parts))
        self.assertNotIn("glpat", sink.of("send")[0][1])
        self.assertNotIn("glpat", sink.of("edit")[-1][1])

    async def test_cancel_notice_for_packages(self):
        import asyncio
        sink = Sink()
        started = asyncio.Event()

        class Hang:
            async def run(self, *a, **k):
                started.set()
                await asyncio.sleep(30)

        task = asyncio.ensure_future(report_job(Hang(), REQ, "j", sink, delays=(0,)))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(sink.of("send")[0][1].startswith("❌ Entrega falló · E_CANCELLED"))
        self.assertIn("cancelada", sink.of("edit")[-1][1])

    async def test_long_url_omits_copy_button(self):
        link = {**LINK, "url": "https://d.test/" + "a" * 300}
        sink = await notice(PackageOutcome("ok", "j", [OK_CODE, OK_DB], link))
        self.assertFalse([b for b in buttons(sink.of("send")[0][2]) if b.copy_text])
        self.assertIn("<code>https://d.test/", sink.of("send")[0][1])

    async def test_started_text_for_package(self):
        text = flow.fmt_started(REQ)
        self.assertTrue(text.startswith("Preparando "))
        self.assertIn("código + BD", text)
        self.assertNotIn("<b>x", text)


def moved(commit):
    return Part("code", True, {**CODE, "commit": commit})


class MovedRefTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_commit_has_no_warning(self):
        sink = await notice(PackageOutcome("ok", "j", [OK_CODE, OK_DB], LINK))
        text = sink.of("send")[0][1]
        self.assertNotIn("avanzó", text)
        self.assertTrue(text.split("\n")[0].endswith("@feat/&lt;i&gt; · código + BD"))

    async def test_different_commit_warns_and_uses_real_commit(self):
        sink = await notice(PackageOutcome("ok", "j", [moved("e" * 40), OK_DB], LINK))
        lines = sink.of("send")[0][1].split("\n")
        self.assertEqual(lines[0], "✅ Listo · &lt;b&gt;x&amp;y&lt;/b&gt;@feat/&lt;i&gt; (eeeeeeee) · código + BD")
        self.assertEqual(lines[1], "⚠️ La rama avanzó mientras se preparaba: se entregó eeeeeeee (el que viste era dddddddd)")
        self.assertIn("<code>https://", sink.of("send")[0][1])

    async def test_empty_confirmed_sha_does_not_warn(self):
        req = PackageRequest(42, "p", "main", "", "zip", "both", 3600, 3, ("a", "b", "c"))
        sink = await notice(PackageOutcome("ok", "j", [moved("e" * 40), OK_DB], LINK), req)
        self.assertNotIn("avanzó", sink.of("send")[0][1])
        self.assertTrue(sink.of("send")[0][1].startswith("✅ Listo · p@main · código + BD\n"))

    async def test_db_only_never_warns(self):
        req = PackageRequest(42, "p", "main", "d" * 40, "zip", "db", 3600, 3, ("a", "b", "c"))
        sink = await notice(PackageOutcome("ok", "j", [OK_DB], LINK), req)
        self.assertNotIn("avanzó", sink.of("send")[0][1])

    async def test_both_with_difference_applies_to_code_only_and_partial(self):
        sink = await notice(PackageOutcome("partial", "j", [moved("f" * 40), fail("db", "E_SSH")], LINK))
        text = sink.of("send")[0][1]
        self.assertIn("se entregó ffffffff (el que viste era dddddddd)", text)
        self.assertIn("BD (E_SSH)", text)
        self.assertEqual(text.count("avanzó"), 1)

    async def test_failed_code_part_does_not_warn_and_link_failed_does(self):
        sink = await notice(PackageOutcome("partial", "j", [fail("code", "E_AUTH"), OK_DB], LINK))
        self.assertNotIn("avanzó", sink.of("send")[0][1])
        err = GatewayError(None, "unreachable", "")
        sink = await notice(PackageOutcome("link_failed", "j", [moved("e" * 40), OK_DB], gateway_error=err))
        self.assertIn("se entregó eeeeeeee", sink.of("send")[0][1])

    async def test_hostile_names_stay_escaped_with_warning(self):
        req = PackageRequest(42, "<script>", "<img onerror=1>", "d" * 40, "zip", "code", 3600, 3)
        sink = await notice(PackageOutcome("ok", "j", [moved("e" * 40)], LINK), req)
        text = sink.of("send")[0][1]
        self.assertNotIn("<script>", text)
        self.assertNotIn("<img", text)
        self.assertIn("&lt;script&gt;@&lt;img onerror=1&gt; (eeeeeeee)", text)


class RetryPolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_defaults_are_three_attempts_with_1s_and_3s(self):
        self.assertEqual(notify.RETRY_DELAYS, (1, 3))
        self.assertLessEqual(notify.CANCEL_NOTICE_TIMEOUT, 2)
        self.assertLess(sum(notify.CANCEL_RETRY_DELAYS), notify.CANCEL_NOTICE_TIMEOUT)

    async def test_permanent_failure_makes_three_attempts_waiting_1s_then_3s(self):
        import unittest.mock as mock
        waits = []

        async def fake_sleep(d):
            waits.append(d)

        sink = Sink(fail_send=True)
        with mock.patch.object(notify.asyncio, "sleep", fake_sleep), self.assertLogs("bot.notify", "WARNING"):
            await report_job(StubJobs(PackageOutcome("failed", "j", [fail("code", "E_AUTH")])), REQ, "j", sink)
        self.assertEqual(len(sink.of("send")), 3)
        self.assertEqual(waits, [1, 3])

    async def test_cancel_notice_cannot_block_shutdown(self):
        import asyncio
        import unittest.mock as mock

        class HangingSink(Sink):
            async def send(self, text, keyboard=None):
                await asyncio.sleep(30)

        started = asyncio.Event()

        class Hang:
            async def run(self, *a, **k):
                started.set()
                await asyncio.sleep(30)

        sink = HangingSink()
        with mock.patch.object(notify, "CANCEL_NOTICE_TIMEOUT", 0.05):
            task = asyncio.ensure_future(report_job(Hang(), REQ, "j", sink))
            await started.wait()
            task.cancel()
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertLess(loop.time() - t0, 0.5)


class RedactTests(unittest.TestCase):
    def test_gitlab_tokens_are_masked_in_logs(self):
        r = Redactor()
        for text in ("x glpat" "-abcDEF123_-xyz y", "gldt" "-AAAA1111bbbb", "PRIVATE-TOKEN: glpat" "-0123456789abcdefghij"):
            out = r(text)
            self.assertNotRegex(out, r"gl[a-z]{2,4}-[A-Za-z0-9]")
        stream = io.StringIO()
        install_logging(r, stream=stream)
        try:
            logging.getLogger("x").error("falló con glpat" "-ZZZZ-9999 y más")
            try:
                raise RuntimeError("tok glpat" "-TRACE-777")
            except RuntimeError:
                logging.getLogger("x").exception("boom")
        finally:
            logging.getLogger().handlers.clear()
        # El traceback imprime la línea de código fuente (con el literal partido): se comprueban
        # los VALORES de los secretos, no la cadena "glpat" suelta.
        for secret in ("glpat" "-ZZZZ-9999", "glpat" "-TRACE-777"):
            self.assertNotIn(secret, stream.getvalue())
        self.assertIn("***", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
