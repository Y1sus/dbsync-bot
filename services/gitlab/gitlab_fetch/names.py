# =============================================================================
# services/gitlab/gitlab_fetch/names.py — Validación estricta de referencias, formato y job_id; slugs para nombres de archivo
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import re

from .errors import FetchError

REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
JOB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
FORMATS = ("zip", "tar.gz")


def valid_ref(ref):
    if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
        return False
    if ".." in ref or "//" in ref or "/." in ref:
        return False
    return not (ref.endswith("/") or ref.endswith(".") or ref.endswith(".lock"))


def check_ref(ref):
    if not valid_ref(ref):
        raise FetchError("E_INVALID_NAME", "Referencia inválida (solo letras, números, . _ - y /)")
    return ref


def check_job_id(job_id):
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise FetchError("E_INVALID_NAME", "job-id inválido")
    return job_id


def check_format(fmt):
    if fmt not in FORMATS:
        raise FetchError("E_INVALID_NAME", "Formato inválido (zip|tar.gz)")
    return fmt


def check_search(text):
    if text is None:
        return None
    if len(text) > 100 or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise FetchError("E_INVALID_NAME", "Texto de búsqueda inválido")
    return text


def slug(text, limit):
    out = re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-.")[:limit].strip("-.")
    return out or "x"


def archive_filename(project_name, ref, commit, job_id, fmt):
    return f"{slug(project_name, 40)}-{slug(ref, 60)}-{commit[:8]}-{job_id}.{fmt}"
