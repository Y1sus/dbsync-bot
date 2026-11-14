# =============================================================================
# services/gitlab/gitlab_fetch/verify.py — Verificación acotada de zip y tar.gz (sin extraer nada)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import gzip
import os
import struct
import tarfile
import zipfile
import zlib
from dataclasses import dataclass

from .errors import FetchError

BLOCK = 64 * 1024
ABS_MAX_BYTES = 4 * 1024 ** 3
EOCD_SIG, EOCD64_SIG, LOC64_SIG, CD_SIG = b"PK\x05\x06", b"PK\x06\x06", b"PK\x06\x07", b"PK\x01\x02"


@dataclass(frozen=True)
class Limits:
    max_entries: int = 100_000
    ratio: int = 8
    min_bytes: int = 16 * 1024 * 1024

    def cap(self, compressed_size):
        return min(max(compressed_size * self.ratio, self.min_bytes), ABS_MAX_BYTES)


def _truncated(detail):
    return FetchError("E_TRUNCATED", f"El archivo descargado está incompleto o dañado ({detail})")


def _too_many(limits):
    return FetchError("E_TOO_LARGE", f"El archivo tiene más de {limits.max_entries} entradas; se rechaza sin extraerlo")


def _too_big():
    return FetchError("E_TOO_LARGE", "El archivo se expande más de lo permitido (posible bomba de compresión); se rechaza sin extraerlo")


def _count_zip_entries(fh, size, limits):
    """Lee el EOCD y recorre el directorio central sin acumular memoria; falla antes de abrir con ZipFile."""
    tail_len = min(size, 65536 + 22)
    fh.seek(size - tail_len)
    tail = fh.read(tail_len)
    idx = tail.rfind(EOCD_SIG)
    if idx < 0 or len(tail) - idx < 22:
        raise _truncated("zip sin fin de directorio")
    eocd_pos = size - tail_len + idx
    cd_size = struct.unpack_from("<I", tail, idx + 12)[0]
    end_of_cd = eocd_pos
    if struct.unpack_from("<H", tail, idx + 10)[0] == 0xFFFF or cd_size == 0xFFFFFFFF:
        if eocd_pos < 20:
            raise _truncated("zip64 inválido")
        fh.seek(eocd_pos - 20)
        loc = fh.read(20)
        if loc[:4] != LOC64_SIG:
            raise _truncated("zip64 inválido")
        rec_pos = struct.unpack_from("<Q", loc, 8)[0]
        fh.seek(rec_pos)
        rec = fh.read(56)
        if rec_pos >= size or rec[:4] != EOCD64_SIG or len(rec) < 56:
            raise _truncated("zip64 inválido")
        cd_size = struct.unpack_from("<Q", rec, 40)[0]
        end_of_cd = rec_pos
    start = end_of_cd - cd_size
    if start < 0:
        raise _truncated("directorio central fuera de rango")
    pos, count = start, 0
    while pos < end_of_cd:
        fh.seek(pos)
        head = fh.read(46)
        if len(head) < 46 or head[:4] != CD_SIG:
            raise _truncated("directorio central dañado")
        n, m, k = struct.unpack_from("<HHH", head, 28)
        pos += 46 + n + m + k
        count += 1
        if count > limits.max_entries:
            raise _too_many(limits)
    if pos != end_of_cd:
        raise _truncated("directorio central dañado")
    return count


def verify_zip(path, limits, check):
    size = os.path.getsize(path)
    cap = limits.cap(size)
    try:
        with open(path, "rb") as fh:
            _count_zip_entries(fh, size, limits)
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if not infos:
                raise _truncated("zip vacío")
            if sum(i.file_size for i in infos) > cap:
                raise _too_big()
            total = 0
            for info in infos:
                if info.is_dir():
                    continue
                with zf.open(info) as member:
                    while True:
                        check()
                        block = member.read(BLOCK)
                        if not block:
                            break
                        total += len(block)
                        if total > cap:
                            raise _too_big()
    except (zipfile.BadZipFile, EOFError, OSError, zlib.error, NotImplementedError, RuntimeError, struct.error):
        raise _truncated("zip ilegible")


def verify_targz(path, limits, check):
    cap = limits.cap(os.path.getsize(path))
    count = 0
    try:
        with tarfile.open(path, "r:gz") as tf:
            while True:
                check()
                if tf.offset > cap:
                    raise _too_big()
                member = tf.next()
                tf.members.clear()
                if member is None:
                    break
                count += 1
                if count > limits.max_entries:
                    raise _too_many(limits)
            gz = tf.fileobj
            while True:
                check()
                block = gz.read(BLOCK)
                if not block:
                    break
                if gz.tell() > cap:
                    raise _too_big()
            if gz.tell() > cap:
                raise _too_big()
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError, zlib.error):
        raise _truncated("tar.gz ilegible")
    if count == 0:
        raise _truncated("tar.gz vacío")


def verify(path, fmt, limits=Limits(), check=lambda: None):
    (verify_zip if fmt == "zip" else verify_targz)(path, limits, check)
