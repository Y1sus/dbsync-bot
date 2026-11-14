# =============================================================================
# services/bot/tests/test_flow.py — Pruebas del asistente, parseo de argumentos y formato de mensajes
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import unittest
from datetime import datetime, timezone

from bot import flow
from bot.gateway_client import GatewayError
from bot.jobs import DumpRequest, Outcome

SERVERS = ["alpha", "beta"]
CONTAINERS = [{"name": "mysql8", "engine": "mysql", "db_count": 2}, {"name": "pg", "engine": "postgresql", "db_count": None}]
HOSTILE = '<script>alert(1)</script>&"\''


def all_buttons(view):
    return [b for row in view.keyboard for b in row]


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class ParseArgsTests(unittest.TestCase):
    def test_full(self):
        a = flow.parse_dump_args("a b c ttl=2h max=3", 3600, 1)
        self.assertEqual((a.server, a.container, a.db, a.ttl, a.max_downloads), ("a", "b", "c", 7200, 3))

    def test_defaults_and_order(self):
        a = flow.parse_dump_args("max=5 a b c", 3600, 1)
        self.assertEqual((a.ttl, a.max_downloads), (3600, 5))

    def test_errors(self):
        for bad in ("", "a b", "a b c d", "a b c ttl=10s", "a b c ttl=xx", "a b c max=0", "a b c max=21", "a b c max=x",
                    "-a b c", "a b/c d", "a b c;rm", "a $b c"):
            with self.assertRaises(flow.UsageError, msg=bad):
                flow.parse_dump_args(bad, 3600, 1)

    def test_revoke_arg(self):
        self.assertEqual(flow.parse_revoke_arg(" abcdef012345 "), "abcdef012345")
        for bad in (None, "", "ABCDEF012345", "abc", "abcdef0123456", "../etc/pass"):
            with self.assertRaises(flow.UsageError):
                flow.parse_revoke_arg(bad)

    def test_command_name(self):
        self.assertEqual(flow.command_name("/Dump@mybot a b c"), "dump")
        self.assertIsNone(flow.command_name("hola"))
        self.assertIsNone(flow.command_name("/"))


class CallbackTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(flow.parse_callback("w|abcdef01|s|3"), ("w", "abcdef01", "s", 3))
        self.assertEqual(flow.parse_callback("w|abcdef01|go|"), ("w", "abcdef01", "go", None))
        self.assertEqual(flow.parse_callback("r|0123456789ab"), ("r", "0123456789ab"))

    def test_invalid_or_tampered(self):
        for bad in (None, 5, "", "x|1", "w|ABCDEF01|s|1", "w|abcdef01|zz|1", "w|abcdef01|s|-1", "w|abcdef01|s|1;", "r|short",
                    "w|abcdef01|s|" + "9" * 10, "w|abcdef01|s|1|extra", "r|0123456789ab\n", "w|abcdef01|s|1" + "x" * 80):
            self.assertIsNone(flow.parse_callback(bad), bad)


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.store = flow.WizardStore(ttl=600, clock=self.clock)
        self.w = self.store.create(42, 3600, 1)
        flow.set_servers(self.w, SERVERS)

    def test_happy_path(self):
        w = self.w
        self.assertEqual(flow.advance(w, "s", 1).kind, "fetch_containers")
        self.assertEqual(w.server, "beta")
        flow.set_containers(w, CONTAINERS)
        self.assertEqual(flow.advance(w, "c", 0).kind, "fetch_dbs")
        self.assertEqual((w.container, w.engine), ("mysql8", "mysql"))
        flow.set_dbs(w, "mysql", ["appdb", "other"])
        self.assertEqual(flow.advance(w, "d", 1).kind, "view")
        self.assertEqual(w.step, "confirm")
        self.assertEqual(flow.advance(w, "t", 14400).kind, "view")
        self.assertEqual(flow.advance(w, "m", 5).kind, "view")
        self.assertEqual((w.ttl, w.max_downloads, w.db), (14400, 5, "other"))
        self.assertEqual(flow.advance(w, "go", None).kind, "run")

    def test_back_and_cancel(self):
        w = self.w
        flow.advance(w, "s", 0)
        flow.set_containers(w, CONTAINERS)
        flow.advance(w, "c", 1)
        flow.set_dbs(w, "postgresql", ["x"])
        flow.advance(w, "d", 0)
        for expected in ("db", "container", "server"):
            self.assertEqual(flow.advance(w, "b", None).kind, "view")
            self.assertEqual(w.step, expected)
        self.assertEqual(flow.advance(w, "b", None).kind, "cancel")
        self.assertEqual(flow.advance(w, "x", None).kind, "cancel")

    def test_out_of_order_and_tampered_actions(self):
        w = self.w
        for action, arg in (("go", None), ("d", 0), ("c", 0), ("t", 3600), ("m", 1), ("s", 99), ("s", None), ("pg", 5), ("zz", 1)):
            self.assertEqual(flow.advance(w, action, arg).kind, "invalid", (action, arg))
        flow.advance(w, "s", 0)
        flow.set_containers(w, CONTAINERS)
        flow.advance(w, "c", 0)
        flow.set_dbs(w, "mysql", ["a"])
        flow.advance(w, "d", 0)
        self.assertEqual(flow.advance(w, "t", 123).kind, "invalid")
        self.assertEqual(flow.advance(w, "m", 99).kind, "invalid")
        self.assertEqual(flow.advance(w, "s", 0).kind, "invalid")

    def test_double_tap_while_fetching_is_ignored(self):
        w = self.w
        self.assertEqual(flow.advance(w, "s", 0).kind, "fetch_containers")
        self.assertEqual(flow.advance(w, "s", 1).kind, "invalid")
        self.assertEqual(w.server, "alpha")
        flow.set_containers(w, CONTAINERS)
        self.assertEqual(flow.advance(w, "c", 0).kind, "fetch_dbs")
        self.assertEqual(flow.advance(w, "c", 1).kind, "invalid")
        self.assertEqual(w.container, "mysql8")
        self.assertEqual(flow.advance(w, "x", None).kind, "cancel")

    def test_release_after_failed_fetch_allows_retry(self):
        w = self.w
        flow.advance(w, "s", 0)
        flow.release(w)
        self.assertEqual(flow.advance(w, "s", 1).kind, "fetch_containers")
        self.assertEqual(w.server, "beta")

    def test_expiry_and_ownership(self):
        wid = self.w.id
        self.assertIs(self.store.get(wid, 42), self.w)
        self.assertIsNone(self.store.get(wid, 7))
        self.clock.t = 599
        self.assertIsNotNone(self.store.get(wid, 42))
        self.clock.t = 599 + 601
        self.assertIsNone(self.store.get(wid, 42))
        self.assertEqual(len(self.store), 0)

    def test_store_cap_and_drop(self):
        store = flow.WizardStore(clock=self.clock, max_items=3)
        ids = []
        for i in range(5):
            self.clock.t += 1
            ids.append(store.create(1, 3600, 1).id)
        self.assertEqual(len(store), 3)
        self.assertIsNone(store.get(ids[0], 1))
        store.drop(ids[4])
        self.assertIsNone(store.get(ids[4], 1))

    def test_paging(self):
        w = self.w
        flow.set_servers(w, [f"s{i}" for i in range(20)])
        v = flow.render(w)
        self.assertEqual(len([r for r in v.keyboard if r[0].data.startswith(f"w|{w.id}|s|")]), flow.PAGE_SIZE)
        self.assertEqual(flow.advance(w, "pg", 2).kind, "view")
        self.assertEqual(flow.advance(w, "pg", 3).kind, "invalid")
        v = flow.render(w)
        self.assertIn("w|%s|s|16" % w.id, [b.data for b in all_buttons(v)])
        self.assertEqual(flow.advance(w, "s", 17).kind, "fetch_containers")
        self.assertEqual(w.server, "s17")

    def test_every_callback_fits_64_bytes_and_parses(self):
        w = self.w
        flow.set_servers(w, [f"s{i}" for i in range(500)])
        views = [flow.render(w)]
        flow.advance(w, "s", 499)
        flow.set_containers(w, CONTAINERS)
        views.append(flow.render(w))
        flow.advance(w, "c", 0)
        flow.set_dbs(w, "mysql", [HOSTILE * 5])
        views.append(flow.render(w))
        flow.advance(w, "d", 0)
        views.append(flow.render(w))
        for v in views:
            for b in all_buttons(v):
                self.assertLessEqual(len(b.data.encode()), 64)
                self.assertIsNotNone(flow.parse_callback(b.data), b.data)

    def test_render_escapes_hostile_names(self):
        w = self.w
        flow.advance(w, "s", 0)
        w.server = HOSTILE
        flow.set_containers(w, [{"name": HOSTILE, "engine": "mysql", "db_count": 1}])
        flow.advance(w, "c", 0)
        flow.set_dbs(w, "mysql", [HOSTILE])
        flow.advance(w, "d", 0)
        for step in ("confirm", "db", "container"):
            w.step = step
            text = flow.render(w).text
            self.assertNotIn("<script>", text)
            self.assertIn("&lt;script&gt;", text)

    def test_render_error_keeps_keyboard(self):
        v = flow.render_error(self.w, "falló")
        self.assertTrue(v.text.startswith("falló"))
        self.assertTrue(all_buttons(v))

    def test_confirm_shows_defaults(self):
        w = self.store.create(1, 7200, 3)
        w.step, w.server, w.container, w.db = "confirm", "a", "b", "c"
        v = flow.render(w)
        self.assertIn("2 h", v.text)
        self.assertIn("Descargas máximas: <b>3</b>", v.text)
        self.assertTrue(any(b.text.startswith("• ") and b.data.endswith("|m|3") for b in all_buttons(v)))


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.req = DumpRequest(42, "a<b", "c", HOSTILE, 3600, 2)
        self.dump = {"db": HOSTILE, "server": "a", "container": "c", "filename": "f<1>.sql.gz", "size_bytes": 5 * 1024 * 1024,
                     "sha256": "ab" * 32, "duration_s": 75}
        self.link = {"id": "0123456789ab", "url": "https://d.test/d/tok", "expires_at": "2026-11-13T18:00:00Z", "max_downloads": 2}

    def test_human_size(self):
        self.assertEqual(flow.human_size(512), "512 B")
        self.assertEqual(flow.human_size(1536), "1.5 KB")
        self.assertEqual(flow.human_size(5 * 1024 ** 2), "5.0 MB")
        self.assertEqual(flow.human_size(3 * 1024 ** 4), "3072.0 GB")

    def test_success(self):
        now = datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc)
        text = flow.fmt_job_success(Outcome("ok", "j", self.dump, self.link), self.req, now).text
        for needle in ("5.0 MB", "abababababab…", "1 min 15 s", "2026-11-13 18:00 UTC (en 2 h)", "<code>https://d.test/d/tok</code>", "2 descarga(s)"):
            self.assertIn(needle, text)
        self.assertNotIn("<script>", text)
        self.assertNotIn("f<1>", text)

    def test_revoked_text_does_not_promise_immediate_delete(self):
        for text in (flow.REVOKED_NOTE, flow.fmt_revoked("0123456789ab")):
            self.assertIn("revocado; el dump se eliminará", text)
            self.assertNotIn("eliminado", text)

    def test_links_view_mentions_purge_and_notice(self):
        rec = [{"id": "0123456789ab", "filename": "f", "expires_at": "2026-11-13T18:00:00Z", "downloads_completed": 0, "max_downloads": 1}]
        self.assertIn("Al revocar o al vencer un enlace se elimina el dump.", flow.fmt_links(rec).text)
        v = flow.fmt_links([], notice=flow.REVOKED_NOTE)
        self.assertTrue(v.text.startswith(flow.REVOKED_NOTE))

    def test_failure_has_code_pre_and_no_traceback(self):
        text = flow.fmt_failure("E_SSH", "x", "línea <1>\nConnection refused")
        self.assertIn("E_SSH", text)
        self.assertIn("<pre>línea &lt;1&gt;\nConnection refused</pre>", text)
        self.assertNotIn("Traceback", text)
        self.assertIn("SSH", text)

    def test_failure_truncates_stderr(self):
        text = flow.fmt_failure("E_DUMP_FAILED", "", "x" * 10000)
        self.assertLess(len(text), 2000)
        self.assertNotIn("<pre>", flow.fmt_failure("E_DISK", "", ""))

    def test_all_codes_have_message(self):
        from bot.runner_client import LOCAL_CODES, RUNNER_CODES
        for code in RUNNER_CODES + LOCAL_CODES:
            self.assertIn(code, flow.ERROR_MESSAGES)

    def test_link_failure_mentions_dump_exists(self):
        o = Outcome("link_failed", "j", self.dump, None, gateway_error=GatewayError(None, "unreachable", ""))
        text = flow.fmt_link_failure(o)
        self.assertIn("dump se generó", text)
        self.assertIn("conectar con el gateway", text)

    def test_links_view(self):
        recs = [{"id": f"{i:012x}", "filename": HOSTILE, "expires_at": "2026-11-13T18:00:00Z", "downloads_completed": 1, "max_downloads": 3}
                for i in range(12)]
        v = flow.fmt_links(recs)
        self.assertEqual(len(v.keyboard), 10)
        self.assertIn("y 2 más", v.text)
        self.assertNotIn("<script>", v.text)
        self.assertEqual(flow.parse_callback(v.keyboard[0][0].data), ("r", "000000000000"))
        self.assertEqual(flow.fmt_links([]).keyboard, [])

    def test_servers_and_status(self):
        v = flow.fmt_servers([{"name": HOSTILE, "host": "h", "access_mode": "docker"}])
        self.assertNotIn("<script>", v.text)
        self.assertIn("Libre", flow.fmt_status([], 0).text)
        from bot.jobs import RunningJob
        job = RunningJob("j", 1, "a", "b", HOSTILE, started=0.0)
        text = flow.fmt_status([job], 2, now=65.0).text
        self.assertIn("1 min 5 s", text)
        self.assertIn("En cola: 2", text)
        self.assertNotIn("<script>", text)
