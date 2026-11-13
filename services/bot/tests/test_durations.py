# =============================================================================
# services/bot/tests/test_durations.py — Pruebas del parseo de duraciones
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import unittest

from bot.durations import DurationError, format_duration, parse_duration


class DurationTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_duration("90m"), 5400)
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("1d"), 86400)
        self.assertEqual(parse_duration(" 60S "), 60)
        self.assertEqual(parse_duration("7d"), 604800)

    def test_limits(self):
        for bad in ("59s", "0m", "8d", "169h", "10081m"):
            with self.assertRaises(DurationError, msg=bad):
                parse_duration(bad)

    def test_invalid_formats(self):
        for bad in ("", "h", "2", "2 h", "-1h", "1.5h", "2w", "1h30m", "abc", "١h", "9999999999h", None):
            with self.assertRaises(DurationError, msg=str(bad)):
                parse_duration(bad)

    def test_format(self):
        self.assertEqual(format_duration(30), "30 s")
        self.assertEqual(format_duration(3600), "1 h")
        self.assertEqual(format_duration(5400), "1 h 30 min")
        self.assertEqual(format_duration(90000), "1 d 1 h")
