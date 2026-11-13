# =============================================================================
# services/bot/bot/runner_client.py — Cliente del runner: subproceso, timeout, grupo de procesos y validación JSON
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import json
import os
import re
import logging
import signal

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
SHA_RE = re.compile(r"^[0-9a-f]{64}\Z")
LOCAL_CODES = ("E_TIMEOUT", "E_BAD_OUTPUT", "E_RUNNER_START", "E_CANCELLED")
RUNNER_CODES = (
    "E_USAGE", "E_CONFIG", "E_INVALID_NAME", "E_SERVER_NOT_FOUND", "E_NOT_DISCOVERED",
    "E_LOCKED", "E_SSH", "E_DUMP_FAILED", "E_TRUNCATED", "E_DISK", "E_INTERNAL",
)
STDERR_TAIL_LINES = 15
log = logging.getLogger("bot.runner")


class RunnerError(Exception):
    def __init__(self, code, message="", stderr=""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.stderr = stderr


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _need(obj, field, kind):
    if field not in obj:
        raise RunnerError("E_BAD_OUTPUT", f"Falta el campo '{field}' en la respuesta del runner.")
    value = obj[field]
    if kind is int:
        ok = _is_int(value)
    elif kind is bool:
        ok = isinstance(value, bool)
    else:
        ok = isinstance(value, kind) and not isinstance(value, bool)
    if not ok:
        raise RunnerError("E_BAD_OUTPUT", f"El campo '{field}' tiene un tipo inválido.")
    return value


def validate_result(result, command):
    """Comprobación manual de job-result.schema.json. Devuelve el dict o lanza RunnerError."""
    if not isinstance(result, dict):
        raise RunnerError("E_BAD_OUTPUT", "La respuesta del runner no es un objeto JSON.")
    if result.get("schema_version") != 1 or isinstance(result.get("schema_version"), bool):
        raise RunnerError("E_BAD_OUTPUT", "Versión de esquema del runner no soportada.")
    ok = _need(result, "ok", bool)
    if not ok:
        err = result.get("error")
        if not isinstance(err, dict):
            raise RunnerError("E_BAD_OUTPUT", "Respuesta de error sin detalle.")
        code = err.get("code")
        code = code if code in RUNNER_CODES else "E_INTERNAL"
        message = err.get("message") if isinstance(err.get("message"), str) else ""
        stderr = err.get("stderr") if isinstance(err.get("stderr"), str) else ""
        raise RunnerError(code, message, stderr)
    if result.get("command") != command:
        raise RunnerError("E_BAD_OUTPUT", "El runner respondió a otro comando.")
    if command == "servers":
        items = _need(result, "servers", list)
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("name"), str) or not it["name"]:
                raise RunnerError("E_BAD_OUTPUT", "Lista de servidores inválida.")
            if not isinstance(it.get("host"), str) or not isinstance(it.get("access_mode"), str):
                raise RunnerError("E_BAD_OUTPUT", "Lista de servidores inválida.")
    elif command == "containers":
        _need(result, "server", str)
        for it in _need(result, "containers", list):
            if not isinstance(it, dict) or not isinstance(it.get("name"), str) or not it["name"]:
                raise RunnerError("E_BAD_OUTPUT", "Lista de contenedores inválida.")
            if it.get("engine") not in ("mysql", "postgresql") or not isinstance(it.get("image"), str):
                raise RunnerError("E_BAD_OUTPUT", "Lista de contenedores inválida.")
            if it.get("db_count") is not None and not _is_int(it.get("db_count")):
                raise RunnerError("E_BAD_OUTPUT", "Lista de contenedores inválida.")
    elif command == "dbs":
        _need(result, "server", str)
        _need(result, "container", str)
        if result.get("engine") not in ("mysql", "postgresql"):
            raise RunnerError("E_BAD_OUTPUT", "Motor de base de datos inválido.")
        for name in _need(result, "databases", list):
            if not isinstance(name, str) or not name:
                raise RunnerError("E_BAD_OUTPUT", "Lista de bases de datos inválida.")
    elif command == "dump":
        for f in ("job_id", "server", "container", "db", "file", "relpath", "filename"):
            _need(result, f, str)
        if result.get("engine") not in ("mysql", "postgresql"):
            raise RunnerError("E_BAD_OUTPUT", "Motor de base de datos inválido.")
        if not _need(result, "size_bytes", int) >= 1 or not _need(result, "duration_s", int) >= 0:
            raise RunnerError("E_BAD_OUTPUT", "Tamaño o duración inválidos.")
        if not SHA_RE.match(_need(result, "sha256", str)):
            raise RunnerError("E_BAD_OUTPUT", "sha256 inválido en la respuesta del runner.")
        ver = _need(result, "verified", dict)
        if ver.get("gzip") is not True or ver.get("trailer") is not True:
            raise RunnerError("E_BAD_OUTPUT", "El dump no quedó verificado.")
        rel = result["relpath"]
        if rel.startswith("/") or ".." in rel.split("/"):
            raise RunnerError("E_BAD_OUTPUT", "relpath inválido en la respuesta del runner.")
    return result


def _tail(text, lines=STDERR_TAIL_LINES):
    return "\n".join(text.strip().splitlines()[-lines:])


class RunnerClient:
    def __init__(self, path, timeout=1800, env=None, kill_grace=3.0, redactor=str):
        self.path = path
        self.timeout = timeout
        self.env = env
        self.kill_grace = kill_grace
        self._redact = redactor
        self._procs = set()

    def _environment(self):
        merged = dict(os.environ)
        if self.env:
            merged.update(self.env)
        return merged

    def _kill_group(self, pid, sig):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    async def _terminate(self, proc):
        self._kill_group(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), self.kill_grace)
        except asyncio.TimeoutError:
            pass
        self._kill_group(proc.pid, signal.SIGKILL)
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            pass

    def kill_all(self):
        for proc in list(self._procs):
            self._kill_group(proc.pid, signal.SIGKILL)

    async def run(self, args, command, timeout=None):
        timeout = self.timeout if timeout is None else timeout
        try:
            proc = await asyncio.create_subprocess_exec(
                self.path, *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._environment(),
                start_new_session=True,
            )
        except OSError as exc:
            raise RunnerError("E_RUNNER_START", f"No se pudo ejecutar el runner: {exc.strerror}.") from None
        self._procs.add(proc)
        try:
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout)
            except asyncio.TimeoutError:
                await self._terminate(proc)
                raise RunnerError("E_TIMEOUT", f"El runner superó el límite de {int(timeout)} s.") from None
            except asyncio.CancelledError:
                await asyncio.shield(self._terminate(proc))
                raise
        finally:
            if proc.returncode is None:
                self._kill_group(proc.pid, signal.SIGKILL)
            self._procs.discard(proc)
        stderr_text = self._redact(_tail(err.decode("utf-8", "replace")))
        return self._parse(proc.returncode, out.decode("utf-8", "replace"), stderr_text, command)

    def _parse(self, code, stdout, stderr_text, command):
        try:
            return self._parse_json(code, stdout, command)
        except RunnerError as exc:
            if exc.code == "E_BAD_OUTPUT" and stderr_text:
                log.warning("Respuesta inválida del runner (salida %s). stderr: %s", code, stderr_text)
            raise

    def _parse_json(self, code, stdout, command):
        lines = [l for l in stdout.splitlines() if l.strip()]
        if not lines:
            raise RunnerError("E_BAD_OUTPUT", f"El runner no devolvió respuesta (salida {code}).")
        try:
            result = json.loads(lines[-1])
        except ValueError:
            raise RunnerError("E_BAD_OUTPUT", f"El runner devolvió una respuesta ilegible (salida {code}).") from None
        validate_result(result, command)
        if code != 0:
            raise RunnerError("E_BAD_OUTPUT", f"El runner reportó éxito con salida {code}.")
        return result

    async def servers(self):
        return (await self.run(["servers"], "servers"))["servers"]

    async def containers(self, server):
        return (await self.run(["containers", "--server", server], "containers"))["containers"]

    async def dbs(self, server, container):
        return await self.run(["dbs", "--server", server, "--container", container], "dbs")

    async def dump(self, server, container, db, job_id, engine=None):
        if not JOB_ID_RE.match(job_id):
            raise RunnerError("E_USAGE", "job_id inválido.")
        args = ["dump", "--server", server, "--container", container, "--db", db, "--job-id", job_id]
        if engine:
            args += ["--engine", engine]
        return await self.run(args, "dump")
