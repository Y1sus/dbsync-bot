# =============================================================================
# services/bot/tests/test_isolation.py — La lógica pura no debe importar aiogram
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import sys
import unittest

import bot.audit, bot.auth, bot.config, bot.durations, bot.flow, bot.gateway_client  # noqa: E401,F401
import bot.jobs, bot.redact, bot.runner_client  # noqa: E401,F401


class IsolationTests(unittest.TestCase):
    def test_aiogram_not_imported(self):
        self.assertNotIn("aiogram", sys.modules)

    def test_package_init_is_light(self):
        import bot
        self.assertFalse(hasattr(bot, "handlers"))
