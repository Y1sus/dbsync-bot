# =============================================================================
# services/bot/bot/redact.py — Redacción de tokens en logs, tracebacks y mensajes de error
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import logging
import re

MASK = "***"
_BOT_URL = re.compile(r"/(file/)?bot[^/\s'\"<>]+")
_BEARER = re.compile(r"(?i)(bearer\s+)[^\s'\"]+")
_GITLAB_TOKEN = re.compile(r"\b(?:glpat|gldt|glptt|glrt|glcbt|glimt|glsoat|glffct|glagent|gloas)-[\w-]+")
_TOKEN_SHAPE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")


class Redactor:
    def __init__(self, secrets=()):
        self._secrets = set()
        for s in secrets:
            self.add(s)

    def add(self, secret):
        if secret and len(secret) >= 4:
            self._secrets.add(secret)

    def __call__(self, text) -> str:
        text = str(text)
        for s in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(s, MASK)
        text = _BOT_URL.sub(lambda m: "/" + (m.group(1) or "") + "bot" + MASK, text)
        text = _BEARER.sub(lambda m: m.group(1) + MASK, text)
        text = _GITLAB_TOKEN.sub(MASK, text)
        return _TOKEN_SHAPE.sub(MASK, text)


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: Redactor):
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redactor(record.getMessage())
        record.args = None
        if record.exc_info:
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = self.redactor(text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self.redactor(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redactor(record.stack_info)
        return True


def install_logging(redactor: Redactor, level=logging.INFO, stream=None) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactingFilter(redactor))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)
    return handler
