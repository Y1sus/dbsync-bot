# =============================================================================
# services/gateway/gateway/tokens.py — Generación y verificación de tokens, ids y sesiones
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import hashlib
import hmac
import re
import secrets

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
LINK_ID_RE = re.compile(r"^[0-9a-f]{12}$")
SESSION_RE = re.compile(r"^[0-9a-f]{32}$")


def new_token():
    return secrets.token_urlsafe(32)


def new_link_id():
    return secrets.token_hex(6)


def new_session_id():
    return secrets.token_hex(16)


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_well_formed(token):
    return isinstance(token, str) and TOKEN_RE.match(token) is not None


def verify(token, stored_hash):
    return hmac.compare_digest(hash_token(token).encode(), stored_hash.encode())
