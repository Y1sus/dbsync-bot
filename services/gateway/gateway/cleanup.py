# =============================================================================
# services/gateway/gateway/cleanup.py — Limpieza periódica de enlaces y dumps
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import logging
import os
import stat

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


def _delete(ctx, relpath, path, reason, now):
    with ctx.store.locked():
        if relpath in ctx.store.referenced_relpaths(now):
            return False
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return False
            os.unlink(path)
        except OSError:
            return False
    ctx.audit.log("file_deleted", relpath=relpath, reason=reason)
    return True


def run_once(ctx, now=None):
    now = ctx.now() if now is None else now
    cfg = ctx.config
    closed, purged = ctx.store.sweep(now, cfg.closed_rows_days * 86400)
    for link_id, reason in closed:
        ctx.audit.log("link_expired", link_id, reason=reason)

    files = sorted(_scan(cfg.dumps_root), key=lambda item: item[2])
    referenced = ctx.store.referenced_relpaths(now)
    deleted = 0
    remaining = []
    for relpath, path, mtime, size in files:
        too_old = now - mtime > cfg.retention_hours * 3600
        if too_old and relpath not in referenced and _delete(ctx, relpath, path, "retention", now):
            deleted += 1
        else:
            remaining.append((relpath, path, size))

    limit = int(cfg.max_dumps_gb * 1024 ** 3)
    total = sum(size for _, _, size in remaining)
    for relpath, path, size in remaining:
        if total <= limit:
            break
        if relpath not in referenced and _delete(ctx, relpath, path, "quota", now):
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
