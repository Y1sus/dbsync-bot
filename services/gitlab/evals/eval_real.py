#!/usr/bin/env python3
# =============================================================================
# services/gitlab/evals/eval_real.py — Eval periódico contra GitLab real (solo GET). Umbral: 0 fallos. Máx. 3 invocaciones de archive (2 descargas reales)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

FETCH = str(Path(__file__).resolve().parents[1] / "fetch.py")
MAX_ARCHIVES = 3
BIG_MAX_MB = "5"
BIG_DEADLINE_S = 60
archives_done = 0
failures = []


def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)
    return cond


def fetch(env, *argv):
    global archives_done
    if argv[0] == "archive":
        if archives_done >= MAX_ARCHIVES:
            raise SystemExit("tope de descargas de archive alcanzado (GitLab.com limita a 5 por minuto)")
        archives_done += 1
    res = subprocess.run([sys.executable, FETCH, *argv], env=env, capture_output=True, text=True, timeout=400)
    try:
        doc = json.loads(res.stdout.splitlines()[0])
    except (IndexError, ValueError):
        doc = {"ok": False, "error": {"code": "SIN_JSON", "message": "salida inválida"}}
    return res.returncode, doc


def err(doc):
    e = doc.get("error") or {}
    return f"{e.get('code')}: {e.get('message')}"


def main():
    env = dict(os.environ)
    missing = [k for k in ("GITLAB_TOKEN_FILE", "GITLAB_PROJECTS_FILE", "GITLAB_EVAL_SMALL_PROJECT",
                           "GITLAB_EVAL_BIG_PROJECT") if not env.get(k)]
    if missing:
        print("faltan variables: " + ", ".join(missing))
        return 1
    dumps = tempfile.mkdtemp(prefix="gitlab-eval-")
    env.update({"DBSYNC_DUMPS_DIR": dumps, "GITLAB_MAX_ARCHIVE_MB": env.get("GITLAB_MAX_ARCHIVE_MB", "500")})
    small, big = env["GITLAB_EVAL_SMALL_PROJECT"], env["GITLAB_EVAL_BIG_PROJECT"]
    try:
        code, doc = fetch(env, "branches", "--project", small)
        check("branches: ok y al menos una rama", code == 0 and doc.get("ok") and doc["refs"], err(doc))
        default = next((r for r in doc.get("refs", []) if r.get("default")), None)
        check("branches: la rama por defecto va primero", bool(default) and doc["refs"][0] is default)
        if not default:
            return 1
        code, doc = fetch(env, "archive", "--project", small, f"--ref={default['name']}", "--job-id", "evalsmall")
        good = code == 0 and doc.get("ok")
        check("archive pequeño: ok", good, err(doc))
        if good:
            with zipfile.ZipFile(doc["file"]) as zf:
                check("archive pequeño: zip válido", zf.testzip() is None and len(zf.namelist()) > 0)
            check("archive pequeño: commit de 40 hex", len(doc["commit"]) == 40)
            check("archive pequeño: permisos 0600", oct(os.stat(doc["file"]).st_mode & 0o777) == "0o600")
        big_env = {**env, "GITLAB_MAX_ARCHIVE_MB": BIG_MAX_MB}
        code, doc = fetch(big_env, "branches", "--project", big)
        big_default = next((r for r in doc.get("refs", []) if r.get("default")), None)
        if check("proyecto grande: branches ok", bool(big_default), err(doc)):
            started = time.monotonic()
            code, doc = fetch(big_env, "archive", "--project", big, f"--ref={big_default['name']}", "--job-id", "evalbig")
            elapsed = time.monotonic() - started
            check("proyecto grande: E_TOO_LARGE", code == 1 and (doc.get("error") or {}).get("code") == "E_TOO_LARGE", err(doc))
            check(f"proyecto grande: aborta temprano (<{BIG_DEADLINE_S}s)", elapsed < BIG_DEADLINE_S, f"{elapsed:.1f}s")
            check("proyecto grande: no queda archivo ni .part", not glob.glob(os.path.join(dumps, "_gitlab", "*evalbig*")))
        code, doc = fetch(env, "archive", "--project", small, "--ref=rama-que-no-existe-xyz", "--job-id", "evalnf")
        check("ref inexistente: E_NOT_FOUND", (doc.get("error") or {}).get("code") == "E_NOT_FOUND", err(doc))
    finally:
        shutil.rmtree(dumps, ignore_errors=True)
    print(f"descargas de archive: {archives_done}/{MAX_ARCHIVES}; fallos: {len(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
