# =============================================================================
# services/bot/bot/packages.py — Paquete de entrega: código (GitLab) y/o BD (runner) en paralelo hacia un enlace único
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import logging
from dataclasses import dataclass, field

from .gateway_client import GatewayError
from .runner_client import RunnerError

log = logging.getLogger("bot.packages")
WHAT = ("code", "db", "both")
JOB_ID_BASE_MAX = 56


@dataclass(frozen=True)
class PackageRequest:
    user_id: int
    project: str
    ref: str
    sha: str
    fmt: str
    what: str
    ttl: int
    max_downloads: int
    db: tuple | None = None  # (server, container, db) exacto del catálogo


@dataclass
class Part:
    kind: str  # code | db
    ok: bool
    result: dict | None = None
    code: str = ""
    message: str = ""


@dataclass
class PackageOutcome:
    kind: str  # ok | partial | failed | link_failed
    job_id: str
    parts: list = field(default_factory=list)
    link: dict | None = None
    gateway_error: GatewayError | None = None
    code: str = ""

    @property
    def good(self):
        return [p for p in self.parts if p.ok]

    @property
    def bad(self):
        return [p for p in self.parts if not p.ok]


def sub_job_ids(job_id):
    base = job_id[:JOB_ID_BASE_MAX]
    return f"{base}-code", f"{base}-db"


async def _guard(kind, coro):
    try:
        return Part(kind, True, await coro)
    except RunnerError as exc:
        return Part(kind, False, code=exc.code, message=exc.message)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.error("Fallo inesperado en la parte %s del paquete", kind, exc_info=True)
        return Part(kind, False, code="E_INTERNAL", message="Error interno del bot.")


async def run_parts(req, job_id, runner, gitlab):
    """Corre código y BD a la vez; si se cancela, cancela y espera a ambas (cada una mata su grupo de procesos)."""
    code_id, db_id = sub_job_ids(job_id)
    tasks = []
    if req.what in ("code", "both"):
        tasks.append(asyncio.ensure_future(_guard("code", gitlab.archive(req.project, req.ref, req.fmt, code_id))))
    if req.what in ("db", "both"):
        server, container, db = req.db
        tasks.append(asyncio.ensure_future(_guard("db", runner.dump(server, container, db, db_id))))
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    kinds = [k for k, w in (("code", "code"), ("db", "db")) if req.what in (w, "both")]
    return [r if isinstance(r, Part) else _failed_part(k, r) for k, r in zip(kinds, results)]


def _failed_part(kind, exc):
    """Una parte cancelada o rota por su cuenta no tira a la otra: se reporta como fallo de esa parte."""
    code = "E_CANCELLED" if isinstance(exc, asyncio.CancelledError) else "E_INTERNAL"
    return Part(kind, False, code=code, message="Error interno del bot." if code == "E_INTERNAL" else "")


def file_specs(parts):
    """Archivos del enlace en orden: código y luego BD."""
    order = {"code": 0, "db": 1}
    return [{"relpath": p.result["relpath"], "sha256": p.result["sha256"]}
            for p in sorted((p for p in parts if p.ok), key=lambda p: order[p.kind])]
