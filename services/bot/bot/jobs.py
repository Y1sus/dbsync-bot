# =============================================================================
# services/bot/bot/jobs.py — Orquesta un dump: runner, enlace en el gateway, cola y auditoría
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import contextlib
import logging
import secrets
import time
from dataclasses import dataclass, field

from .gateway_client import GatewayError
from .packages import PackageOutcome, PackageRequest, file_specs, run_parts
from .runner_client import JOB_ID_RE, RunnerError

log = logging.getLogger("bot.jobs")


def new_job_id(update_id=None) -> str:
    prefix = f"tg{int(update_id)}" if isinstance(update_id, int) and update_id >= 0 else "tg"
    job_id = f"{prefix}-{secrets.token_hex(4)}"
    assert JOB_ID_RE.match(job_id)
    return job_id


@dataclass(frozen=True)
class DumpRequest:
    user_id: int
    server: str
    container: str
    db: str
    ttl: int
    max_downloads: int
    engine: str | None = None


@dataclass
class Outcome:
    kind: str  # ok | dump_failed | link_failed
    job_id: str
    dump: dict | None = None
    link: dict | None = None
    code: str = ""
    message: str = ""
    stderr: str = ""
    gateway_error: GatewayError | None = None


@dataclass
class RunningJob:
    job_id: str
    user_id: int
    server: str
    container: str
    db: str
    started: float = field(default_factory=time.monotonic)
    label: str = ""


class JobManager:
    def __init__(self, runner, gateway, audit, max_jobs=1, gitlab=None):
        self.runner = runner
        self.gitlab = gitlab
        self.gateway = gateway
        self.audit = audit
        self._sem = asyncio.Semaphore(max_jobs)
        self.running = {}
        self.waiting = 0
        self._tasks = set()

    def spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def shutdown(self):
        for task in list(self._tasks):
            task.cancel()
        self.runner.kill_all()
        if self.gitlab is not None:
            self.gitlab.kill_all()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def status(self):
        return list(self.running.values()), self.waiting

    @contextlib.asynccontextmanager
    async def _slot(self, on_queued, on_start):
        queued = self._sem.locked()
        if queued:
            self.waiting += 1
            try:
                if on_queued:
                    await self._notify(on_queued, self.waiting)
                await self._sem.acquire()
            finally:
                self.waiting -= 1
        else:
            await self._sem.acquire()
        try:
            if queued and on_start:
                await self._notify(on_start)
            yield
        finally:
            self._sem.release()

    async def run(self, req, job_id: str, on_queued=None, on_start=None):
        """Un dump o un paquete: ambos ocupan un solo cupo de max_jobs."""
        async with self._slot(on_queued, on_start):
            if isinstance(req, PackageRequest):
                return await self._execute_package(req, job_id)
            return await self._execute(req, job_id)

    @staticmethod
    async def _notify(callback, *args):
        try:
            await callback(*args)
        except Exception:
            log.warning("Fallo al notificar el estado del job", exc_info=True)

    async def _execute(self, req, job_id) -> Outcome:
        job = RunningJob(job_id, req.user_id, req.server, req.container, req.db)
        self.running[job_id] = job
        self.audit.write("job_start", user_id=req.user_id, job_id=job_id,
                         server=req.server, container=req.container, db=req.db)
        try:
            try:
                dump = await self.runner.dump(req.server, req.container, req.db, job_id, req.engine)
            except RunnerError as exc:
                self.audit.write("job_end", user_id=req.user_id, job_id=job_id, ok=False, code=exc.code)
                return Outcome("dump_failed", job_id, code=exc.code, message=exc.message, stderr=exc.stderr)
            except asyncio.CancelledError:
                self.audit.write("job_end", user_id=req.user_id, job_id=job_id, ok=False, code="E_CANCELLED")
                raise
            except Exception:
                log.error("Fallo inesperado al ejecutar el runner", exc_info=True)
                self.audit.write("job_end", user_id=req.user_id, job_id=job_id, ok=False, code="E_INTERNAL")
                return Outcome("dump_failed", job_id, code="E_INTERNAL", message="Error interno del bot.")
            self.audit.write("job_end", user_id=req.user_id, job_id=job_id, ok=True, code="OK",
                             size_bytes=dump["size_bytes"], duration_s=dump["duration_s"])
            label = f"{req.server}/{req.container}/{req.db}"
            try:
                link = await self.gateway.create_link(
                    files=[{"relpath": dump["relpath"], "sha256": dump["sha256"]}], created_by=req.user_id,
                    job_id=job_id, ttl_seconds=req.ttl, max_downloads=req.max_downloads, label=label,
                )
            except GatewayError as exc:
                self.audit.write("link_failed", user_id=req.user_id, job_id=job_id, code=exc.code, status=exc.status)
                return Outcome("link_failed", job_id, dump=dump, gateway_error=exc)
            self.audit.write("link_created", user_id=req.user_id, job_id=job_id, link_id=link["id"],
                             ttl_seconds=req.ttl, max_downloads=req.max_downloads)
            return Outcome("ok", job_id, dump=dump, link=link)
        finally:
            self.running.pop(job_id, None)

    async def _execute_package(self, req: PackageRequest, job_id) -> PackageOutcome:
        label = f"{req.project}@{req.ref}"
        job = RunningJob(job_id, req.user_id, req.project, req.ref, req.what, label=f"{label} ({req.what})")
        self.running[job_id] = job
        self.audit.write("job_start", user_id=req.user_id, job_id=job_id, project=req.project, ref=req.ref, what=req.what)
        try:
            try:
                parts = await run_parts(req, job_id, self.runner, self.gitlab)
            except asyncio.CancelledError:
                self.audit.write("job_end", user_id=req.user_id, job_id=job_id, ok=False, code="E_CANCELLED")
                raise
            codes = {p.kind: ("OK" if p.ok else p.code) for p in parts}
            self.audit.write("job_end", user_id=req.user_id, job_id=job_id, ok=all(p.ok for p in parts), parts=codes)
            if not any(p.ok for p in parts):
                return PackageOutcome("failed", job_id, parts)
            try:
                link = await self.gateway.create_link(
                    files=file_specs(parts), created_by=req.user_id, job_id=job_id,
                    ttl_seconds=req.ttl, max_downloads=req.max_downloads, label=label,
                )
            except GatewayError as exc:
                self.audit.write("link_failed", user_id=req.user_id, job_id=job_id, code=exc.code, status=exc.status)
                return PackageOutcome("link_failed", job_id, parts, gateway_error=exc)
            self.audit.write("link_created", user_id=req.user_id, job_id=job_id, link_id=link["id"],
                             ttl_seconds=req.ttl, max_downloads=req.max_downloads, files=len(link["files"]))
            return PackageOutcome("ok" if all(p.ok for p in parts) else "partial", job_id, parts, link=link)
        finally:
            self.running.pop(job_id, None)
