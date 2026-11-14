# =============================================================================
# services/gitlab/gitlab_fetch/archive.py — Resolución de commit y descarga en streaming verificada del archivo de código
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import errno
import hashlib
import os
import re
import stat
import shutil
import signal
import time
from urllib.parse import quote

from .errors import FetchError
from .names import archive_filename
from .client import declared_length
from .verify import Limits, verify

BLOCK = 64 * 1024
STALE_PART_SECONDS = 3600
SOURCE_DIR = "_gitlab"
SHA_RE = re.compile(r"[0-9a-f]{40}")


def project_target(client, project, tail, query=""):
    return client.api_target(f"/projects/{quote(project['path'], safe='')}/repository{tail}", query)


def resolve_commit(client, project, ref):
    target = project_target(client, project, f"/commits/{quote(ref, safe='')}")
    try:
        data = client.get_json(target)
    except FetchError as exc:
        if exc.code == "E_NOT_FOUND":
            raise FetchError("E_NOT_FOUND", "La referencia no existe en el proyecto")
        raise
    sha = data.get("id") if isinstance(data, dict) else None
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise FetchError("E_GITLAB", "GitLab no devolvió un commit válido para la referencia")
    return sha


def prepare_dir(dumps_dir, min_free_mb):
    dest = os.path.join(dumps_dir, SOURCE_DIR)
    try:
        os.makedirs(dest, mode=0o700, exist_ok=True)
        os.chmod(dest, 0o700)
        _purge_stale_parts(dest)
        free = shutil.disk_usage(dest).free
    except OSError:
        raise FetchError("E_DISK", "No se pudo preparar el directorio de descargas")
    if free < min_free_mb * 1024 * 1024:
        raise FetchError("E_DISK", f"Espacio libre insuficiente (mínimo {min_free_mb} MB)")
    return dest


def _purge_stale_parts(dest, now=None):
    now = time.time() if now is None else now
    for entry in os.scandir(dest):
        if not entry.name.endswith(".part"):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(st.st_mode) and now - st.st_mtime > STALE_PART_SECONDS:
                os.unlink(entry.path)
        except OSError:
            continue


def _stream(client, target, part, max_bytes):
    conn, resp = client.get(target)
    sha, size = hashlib.sha256(), 0
    try:
        length = declared_length(resp)
        if length is not None and length > max_bytes:
            raise FetchError("E_TOO_LARGE", "El archivo supera el tope permitido")
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            while True:
                client.check_deadline()
                try:
                    block = resp.read1(BLOCK)
                except OSError as exc:
                    raise FetchError("E_NETWORK", f"Se cortó la descarga ({type(exc).__name__})")
                except Exception as exc:
                    raise FetchError("E_NETWORK", f"Descarga incompleta ({type(exc).__name__})")
                if not block:
                    break
                size += len(block)
                if size > max_bytes:
                    raise FetchError("E_TOO_LARGE", "El archivo supera el tope permitido")
                sha.update(block)
                out.write(block)
        if length is not None and size != length:
            raise FetchError("E_TRUNCATED", "La descarga terminó antes de lo anunciado")
    finally:
        conn.close()
    return size, sha.hexdigest()


def fetch_archive(cfg, client, project, ref, fmt, job_id):
    started = time.monotonic()
    dest = prepare_dir(cfg.dumps_dir, cfg.min_free_mb)
    commit = resolve_commit(client, project, ref)
    filename = archive_filename(project["name"], ref, commit, job_id, fmt)
    final, part = os.path.join(dest, filename), os.path.join(dest, filename + ".part")
    if os.path.lexists(part):
        os.unlink(part)
    target = project_target(client, project, f"/archive.{fmt}", f"sha={commit}")
    try:
        size, digest = _stream(client, target, part, cfg.max_archive_mb * 1024 * 1024)
        if size == 0:
            raise FetchError("E_TRUNCATED", "GitLab devolvió un archivo vacío")
        verify(part, fmt, Limits(cfg.verify_max_entries, cfg.verify_ratio, cfg.verify_min_bytes), client.check_deadline)
        os.replace(part, final)
    except OSError as exc:
        _drop(part)
        if exc.errno == errno.ENOSPC:
            raise FetchError("E_DISK", "Sin espacio en disco durante la descarga")
        raise FetchError("E_INTERNAL", f"Error de archivo ({type(exc).__name__})")
    except BaseException:
        _drop(part)
        raise
    return {
        "job_id": job_id, "project": project["name"], "ref": ref, "commit": commit, "format": fmt,
        "file": final, "relpath": f"{SOURCE_DIR}/{filename}", "filename": filename,
        "size_bytes": size, "sha256": digest, "duration_s": int(time.monotonic() - started),
        "verified": {"archive": True},
    }


def _drop(path):
    """Borra el .part sin que la alarma de plazo total interrumpa el borrado."""
    old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)
