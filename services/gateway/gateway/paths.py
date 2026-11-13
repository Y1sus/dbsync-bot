# =============================================================================
# services/gateway/gateway/paths.py — Validación y apertura segura de dumps dentro de la raíz
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import os
import stat

SUFFIX = ".sql.gz"


class PathError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_relpath(relpath):
    if not isinstance(relpath, str) or not relpath:
        raise PathError("invalid_request", "relpath debe ser un texto no vacío")
    if len(relpath) > 1024 or "\x00" in relpath or "\\" in relpath:
        raise PathError("invalid_request", "relpath contiene caracteres no permitidos")
    if relpath.startswith("/"):
        raise PathError("invalid_request", "relpath debe ser relativo")
    parts = relpath.split("/")
    for part in parts:
        if part in ("", ".", "..") or part.startswith("."):
            raise PathError("invalid_request", "relpath contiene componentes no permitidos")
    if not relpath.endswith(SUFFIX):
        raise PathError("invalid_request", "relpath debe terminar en .sql.gz")
    return parts


def _candidate(root, relpath):
    parts = validate_relpath(relpath)
    real_root = os.path.realpath(root)
    candidate = os.path.join(real_root, *parts)
    if not candidate.startswith(real_root + os.sep):
        raise PathError("invalid_request", "relpath sale de la raíz de dumps")
    return candidate


def _check_regular(candidate):
    try:
        info = os.lstat(candidate)
    except FileNotFoundError:
        raise PathError("file_not_found", "El archivo no existe") from None
    except OSError:
        raise PathError("file_not_found", "El archivo no es accesible") from None
    if stat.S_ISLNK(info.st_mode) or os.path.realpath(candidate) != candidate:
        raise PathError("file_not_found", "El archivo no puede ser un enlace simbólico")
    if not stat.S_ISREG(info.st_mode):
        raise PathError("file_not_found", "El archivo no es regular")
    return info


def resolve_dump(root, relpath):
    candidate = _candidate(root, relpath)
    info = _check_regular(candidate)
    return candidate, info.st_size


def open_dump(root, relpath):
    """Abre el dump para lectura; devuelve (fd, tamaño actual)."""
    candidate = _candidate(root, relpath)
    _check_regular(candidate)
    try:
        fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise PathError("file_not_found", "El archivo no es accesible") from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise PathError("file_not_found", "El archivo no es regular")
    return fd, info.st_size
