# =============================================================================
# services/bot/bot/gitlab_client.py — Cliente del servicio de GitLab (fetch.py): subproceso y validación de source-result
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import re
import sys

from .runner_client import JOB_ID_RE, SHA_RE, RunnerClient, RunnerError, _is_int

GITLAB_CODES = (
    "E_USAGE", "E_CONFIG", "E_INVALID_NAME", "E_NOT_ALLOWED", "E_AUTH", "E_FORBIDDEN", "E_NOT_FOUND",
    "E_RATE_LIMITED", "E_GITLAB", "E_NETWORK", "E_TOO_LARGE", "E_TRUNCATED", "E_DISK", "E_INTERNAL",
)
COMMIT_RE = re.compile(r"^[0-9a-f]{40}\Z")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,199}\Z")
SEARCH_MAX = 60
LOOKUP_TIMEOUT = 120
FORMATS = ("zip", "tar.gz")


def sanitize_search(text) -> str:
    """Deja solo caracteres de nombres de rama; nunca empieza con guion (evita opciones del CLI)."""
    return re.sub(r"[^A-Za-z0-9._/@+-]", "", str(text or "")).lstrip("-")[:SEARCH_MAX]


def _bad(msg):
    return RunnerError("E_BAD_OUTPUT", f"Respuesta inválida del servicio de GitLab: {msg}.")


def _str(obj, field):
    v = obj.get(field)
    if not isinstance(v, str) or not v:
        raise _bad(f"campo '{field}'")
    return v


def _validate_catalog(result):
    projects = result.get("projects")
    if not isinstance(projects, list):
        raise _bad("projects")
    for p in projects:
        if not isinstance(p, dict):
            raise _bad("proyecto")
        _str(p, "name"), _str(p, "path")
        if not isinstance(p.get("has_db"), bool):
            raise _bad("has_db")
        db = p.get("db")
        if p["has_db"]:
            if not isinstance(db, dict):
                raise _bad("db")
            for f in ("server", "container", "db"):
                _str(db, f)


def _validate_refs(result):
    _str(result, "project")
    if not isinstance(result.get("truncated"), bool) or not isinstance(result.get("refs"), list):
        raise _bad("refs")
    for r in result["refs"]:
        if not isinstance(r, dict):
            raise _bad("ref")
        _str(r, "name")
        if not isinstance(r.get("sha"), str):
            raise _bad("sha")
    # Solo se ofrecen referencias con caracteres seguros: el nombre viaja como argumento del CLI
    result["refs"] = [r for r in result["refs"] if REF_RE.match(r["name"])]


def _validate_archive(result):
    for f in ("job_id", "project", "ref", "file", "relpath", "filename"):
        _str(result, f)
    if not COMMIT_RE.match(str(result.get("commit", ""))):
        raise _bad("commit")
    if result.get("format") not in FORMATS:
        raise _bad("format")
    if not _is_int(result.get("size_bytes")) or result["size_bytes"] < 1:
        raise _bad("size_bytes")
    if not _is_int(result.get("duration_s")) or result["duration_s"] < 0:
        raise _bad("duration_s")
    if not SHA_RE.match(str(result.get("sha256", ""))):
        raise _bad("sha256")
    ver = result.get("verified")
    if not isinstance(ver, dict) or ver.get("archive") is not True:
        raise _bad("el archivo no quedó verificado")
    parts = result["relpath"].split("/")
    if len(parts) != 2 or parts[0] != "_gitlab" or not parts[1] or parts[1].startswith(".") or not (
            parts[1].endswith(".zip") or parts[1].endswith(".tar.gz")):
        raise _bad("relpath")


class GitlabClient(RunnerClient):
    """Mismo manejo de subprocesos que el runner; ejecuta fetch.py con el intérprete actual."""

    label = "el servicio de GitLab"

    def __init__(self, path, timeout=1800, env=None, kill_grace=3.0, redactor=str, python=None):
        super().__init__(path, timeout, env, kill_grace, redactor)
        self.python = python or sys.executable

    def _argv(self, args):
        return [self.python, "-I", self.path, *args]

    def _validate(self, result, command):
        if not isinstance(result, dict):
            raise _bad("no es un objeto JSON")
        if result.get("schema_version") != 1 or isinstance(result.get("schema_version"), bool):
            raise _bad("versión de esquema no soportada")
        if not isinstance(result.get("ok"), bool):
            raise _bad("campo 'ok'")
        if not result["ok"]:
            err = result.get("error")
            if not isinstance(err, dict):
                raise _bad("error sin detalle")
            code = err.get("code") if err.get("code") in GITLAB_CODES else "E_INTERNAL"
            msg = err.get("message") if isinstance(err.get("message"), str) else ""
            raise RunnerError(code, self._redact(msg), "")
        if result.get("command") != command:
            raise _bad("respondió a otro comando")
        {"catalog": _validate_catalog, "branches": _validate_refs, "tags": _validate_refs,
         "archive": _validate_archive}[command](result)
        return result

    async def catalog(self):
        return (await self.run(["catalog"], "catalog", LOOKUP_TIMEOUT))["projects"]

    async def branches(self, project, search=None):
        args = ["branches", "--project", project]
        if search:
            args += ["--search", search]
        return await self.run(args, "branches", LOOKUP_TIMEOUT)

    async def tags(self, project):
        return await self.run(["tags", "--project", project], "tags", LOOKUP_TIMEOUT)

    async def archive(self, project, ref, fmt, job_id):
        if not JOB_ID_RE.match(job_id) or fmt not in FORMATS or not REF_RE.match(ref):
            raise RunnerError("E_USAGE", "Parámetros inválidos.")
        return await self.run(["archive", "--project", project, "--ref", ref, "--format", fmt, "--job-id", job_id], "archive")
