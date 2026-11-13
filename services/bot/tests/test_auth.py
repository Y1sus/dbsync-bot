# =============================================================================
# services/bot/tests/test_auth.py — Pruebas de la decisión de autorización de updates
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import unittest

from bot.auth import RejectLimiter, decide

ALLOWED = frozenset({42})


class AuthTests(unittest.TestCase):
    def test_authorized_private_message_and_callback(self):
        self.assertTrue(decide("message", 42, "private", ALLOWED).ok)
        self.assertTrue(decide("callback_query", 42, "private", ALLOWED).ok)

    def test_foreign_user(self):
        for kind in ("message", "callback_query"):
            d = decide(kind, 7, "private", ALLOWED)
            self.assertFalse(d.ok)
            self.assertEqual(d.reason, "usuario_no_autorizado")

    def test_group_and_other_chats(self):
        for ct in ("group", "supergroup", "channel", None):
            self.assertFalse(decide("message", 42, ct, ALLOWED).ok, ct)

    def test_edited_and_other_kinds_rejected_even_for_owner(self):
        for kind in ("edited_message", "channel_post", "inline_query", "my_chat_member", "unknown"):
            self.assertEqual(decide(kind, 42, "private", ALLOWED).reason, "tipo_no_permitido")

    def test_no_user(self):
        self.assertFalse(decide("message", None, "private", ALLOWED).ok)

    def test_limiter(self):
        now = [0.0]
        lim = RejectLimiter(window=60, clock=lambda: now[0])
        self.assertTrue(lim.should_log(1))
        self.assertFalse(lim.should_log(1))
        self.assertTrue(lim.should_log(2))
        now[0] = 61
        self.assertTrue(lim.should_log(1))
