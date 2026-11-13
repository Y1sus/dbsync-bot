# =============================================================================
# services/bot/tests/test_config.py — Pruebas de configuración y lectura de secretos
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import os
import tempfile
import unittest

from bot.config import ConfigError, load_config, parse_user_ids, read_secret

BASE = {
    "BOT_TOKEN_FILE": "/x/t", "BOT_ALLOWED_USER_IDS": "42", "BOT_GATEWAY_URL": "http://gateway:8081/",
    "BOT_GATEWAY_TOKEN_FILE": "/x/g",
}


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        c = load_config(dict(BASE))
        self.assertEqual(c.allowed_user_ids, frozenset({42}))
        self.assertEqual((c.default_ttl, c.default_max_downloads, c.job_timeout, c.max_jobs), (3600, 1, 1800, 1))
        self.assertEqual(c.gateway_url, "http://gateway:8081")
        self.assertIsNone(c.telegram_api_base)
        self.assertEqual(c.runner_path, "/app/runner/runner.sh")

    def test_missing_required(self):
        for name in ("BOT_TOKEN_FILE", "BOT_ALLOWED_USER_IDS", "BOT_GATEWAY_URL", "BOT_GATEWAY_TOKEN_FILE"):
            env = dict(BASE)
            del env[name]
            with self.assertRaises(ConfigError, msg=name) as cm:
                load_config(env)
            self.assertIn(name, str(cm.exception))

    def test_user_ids(self):
        self.assertEqual(parse_user_ids("1, 2,3,"), frozenset({1, 2, 3}))
        for bad in ("", " , ", "abc", "1,x", "-5", "0", "1.5", "١"):
            with self.assertRaises(ConfigError, msg=bad):
                parse_user_ids(bad)

    def test_numeric_ranges(self):
        for name, val in (("BOT_DEFAULT_TTL_SECONDS", "10"), ("BOT_DEFAULT_TTL_SECONDS", "99999999"),
                          ("BOT_DEFAULT_MAX_DOWNLOADS", "0"), ("BOT_DEFAULT_MAX_DOWNLOADS", "21"),
                          ("BOT_MAX_JOBS", "0"), ("BOT_JOB_TIMEOUT_SECONDS", "abc")):
            with self.assertRaises(ConfigError, msg=name + val):
                load_config({**BASE, name: val})
        c = load_config({**BASE, "BOT_MAX_JOBS": "2", "BOT_DEFAULT_TTL_SECONDS": "7200", "BOT_DEFAULT_MAX_DOWNLOADS": "3"})
        self.assertEqual((c.max_jobs, c.default_ttl, c.default_max_downloads), (2, 7200, 3))

    def test_gateway_url_invalid(self):
        with self.assertRaises(ConfigError):
            load_config({**BASE, "BOT_GATEWAY_URL": "gateway:8081"})

    def test_api_base(self):
        self.assertEqual(load_config({**BASE, "BOT_TELEGRAM_API_BASE": "http://127.0.0.1:9"}).telegram_api_base, "http://127.0.0.1:9")

    def test_read_secret(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s")
            with open(p, "w") as fh:
                fh.write("  abc:def\n")
            self.assertEqual(read_secret(p, "x"), "abc:def")
            with open(p, "w") as fh:
                fh.write("\n")
            with self.assertRaises(ConfigError):
                read_secret(p, "x")
            with self.assertRaises(ConfigError) as cm:
                read_secret(os.path.join(d, "nope"), "BOT_TOKEN_FILE")
            self.assertNotIn(d, str(cm.exception))
