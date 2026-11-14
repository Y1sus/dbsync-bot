# =============================================================================
# services/gateway/gateway/cleanup.py — Limpieza periódica de enlaces y dumps
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import logging
import os
import stat

from . import paths

log = logging.getLogger("gateway.cleanup")

SUFFIX = ".sql.gz"


def _scan(root):
    """Dumps finales regulares en <raiz>/<servidor>/*.sql.gz; sin symlinks ni ocultos."""
    found = []
    try:
        servers = list(os.scandir(root))
    except OSError:
        return found
    for server in servers:
        if server.name.startswith(".") or server.is_symlink() or not server.is_dir(follow_symlinks=False):
            continue
        try:
            entries = list(os.scandir(server.path))
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or not entry.name.endswith(SUFFIX):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                found.append((f"{server.name}/{entry.name}", entry.path, info.st_mtime, info.st_size))
    return found


def _unlink(ctx, relpath, path):
    try:
        if path is None:
            path = paths.resolve_dump(ctx.config.dumps_root, relpath)[0]
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return False
        os.unlink(path)
    except (paths.PathError, OSError):
        return False
    return True


def _busy(ctx, status):
    return any(ctx.active.busy(link_id) for link_id in status["link_ids"])


def delete_finished(ctx, relpath, now, reason=None, path=None):
    """Regla sin estado: borra el dump si todos sus enlaces son terminales y nada lo usa.

    Devuelve la razón de borrado o None. Se evalúa bajo el candado de create_link.
    """
    with ctx.store.locked():
        status = ctx.store.file_status(relpath, now)
        if not status["all_terminal"] or status["kept"] or status["open_sessions"] or _busy(ctx, status):
            return None
        _, link_id, state = status["last"]
        reason = reason or f"link_{state}"
        if not _unlink(ctx, relpath, path):
            return None
        ctx.audit.log("file_deleted", link_id, relpath=relpath, reason=reason)
    return reason


def delete_fallback(ctx, relpath, path, reason, now):
    """Respaldo (retención/cuota): solo dumps sin ninguna fila de enlace."""
    with ctx.store.locked():
        status = ctx.store.file_status(relpath, now)
        if status["rows"] or _busy(ctx, status):
            return False
        if not _unlink(ctx, relpath, path):
            return False
        ctx.audit.log("file_deleted", relpath=relpath, reason=reason)
    return True


def run_once(ctx, now=None):
    now = ctx.now() if now is None else now
    cfg = ctx.config
    ctx.revoked.difference_update([i for i in list(ctx.revoked) if not ctx.active.busy(i)])
    closed = ctx.store.close_expired(now)
    for link_id, reason in closed:
        ctx.audit.log("link_expired", link_id, reason=reason)

    files = sorted(_scan(cfg.dumps_root), key=lambda item: item[2])
    deleted = 0
    remaining = []
    for relpath, path, mtime, size in files:
        if delete_finished(ctx, relpath, now, path=path):
            deleted += 1
        else:
            remaining.append((relpath, path, mtime, size))
    purged = ctx.store.purge_closed(now, cfg.closed_rows_days * 86400)

    leftovers = []
    for relpath, path, mtime, size in remaining:
        if now - mtime > cfg.retention_hours * 3600 and delete_fallback(ctx, relpath, path, "retention", now):
            deleted += 1
        else:
            leftovers.append((relpath, path, size))

    limit = int(cfg.max_dumps_gb * 1024 ** 3)
    total = sum(size for _, _, size in leftovers)
    for relpath, path, size in leftovers:
        if total <= limit:
            break
        if delete_fallback(ctx, relpath, path, "quota", now):
            total -= size
            deleted += 1
    return {"links_closed": len(closed), "rows_purged": purged, "files_deleted": deleted}


async def loop(ctx):
    while True:
        await asyncio.sleep(ctx.config.cleanup_interval)
        try:
            result = await asyncio.to_thread(run_once, ctx)
            if any(result.values()):
                log.info("Limpieza: %s", result)
        except Exception:
            log.exception("Falló la limpieza periódica")
