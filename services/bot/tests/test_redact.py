# =============================================================================
# services/bot/tests/test_redact.py — Pruebas de redacción de tokens en logs y tracebacks
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import io
import logging
import unittest

from bot.redact import Redactor, install_logging

TOKEN = "123456:TEST"
GW = "gw-secret-token"


def capture(redactor):
    stream = io.StringIO()
    install_logging(redactor, stream=stream)
    return stream


class RedactTests(unittest.TestCase):
    def tearDown(self):
        logging.getLogger().handlers.clear()

    def test_bot_url_and_secrets(self):
        r = Redactor([TOKEN, GW])
        out = r(f"POST https://api.telegram.org/bot{TOKEN}/getUpdates y {GW} y Bearer abc.def")
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(GW, out)
        self.assertNotIn("abc.def", out)
        self.assertIn("/bot***/getUpdates", out)

    def test_unknown_token_shape_still_masked(self):
        r = Redactor()
        out = r("https://x/bot999999:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/send y 999999:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        self.assertNotIn("AAAAAAAA", out)

    def test_traceback_redacted(self):
        stream = capture(Redactor([TOKEN]))
        log = logging.getLogger("aiohttp.client")
        try:
            try:
                raise ConnectionError(f"Cannot connect to host api.telegram.org:443 ssl:default [/bot{TOKEN}/getMe]")
            except ConnectionError as inner:
                raise RuntimeError(f"ClientConnectorError https://api.telegram.org/bot{TOKEN}/getMe") from inner
        except RuntimeError:
            log.error("Fallo con %s", f"https://api.telegram.org/bot{TOKEN}/getMe", exc_info=True)
        text = stream.getvalue()
        self.assertIn("Traceback", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("TEST", text)

    def test_real_aiohttp_exceptions_redacted(self):
        import aiohttp
        from multidict import CIMultiDict, CIMultiDictProxy
        from yarl import URL
        url = URL(f"https://api.telegram.org/bot{TOKEN}/getMe")
        info = aiohttp.RequestInfo(url, "POST", CIMultiDictProxy(CIMultiDict()), url)
        errors = (
            aiohttp.ClientResponseError(info, (), status=502, message="Bad Gateway"),
            aiohttp.InvalidURL(str(url)),
            aiohttp.ClientConnectionError(f"Cannot connect to host api.telegram.org [{url}]"),
        )
        stream = capture(Redactor([TOKEN]))
        for err in errors:
            try:
                raise err
            except Exception:
                logging.getLogger("aiogram.dispatcher").exception("Failed to fetch updates - %s: %s", type(err).__name__, err)
        text = stream.getvalue()
        self.assertIn("ClientResponseError", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("TEST", text)

    def test_exc_info_instance_and_args(self):
        stream = capture(Redactor([TOKEN]))
        exc = ValueError(f"token {TOKEN}")
        logging.getLogger("aiogram.event").error("Cause %s", TOKEN, exc_info=exc)
        logging.getLogger("x").warning("sin args %s %d", GW, 3)
        self.assertNotIn(TOKEN, stream.getvalue())
        self.assertIn(" 3", stream.getvalue())

    def test_short_secret_ignored(self):
        self.assertEqual(Redactor(["ab"])("ab cd"), "ab cd")
