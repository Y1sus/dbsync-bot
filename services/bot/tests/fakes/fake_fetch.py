# =============================================================================
# services/bot/tests/fakes/fake_fetch.py — fetch.py falso que imita source-result.schema.json, guiado por FAKE_FETCH_MODE
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
# FAKE_FETCH_MODE: ok | moved (archive devuelve otro commit) | err | err_all | garbage | empty | badjson | hang | slow
#   err y hang aplican solo a `archive`; err_all falla también catalog/branches/tags.
# FAKE_FETCH_ERR: código E_* para err/err_all (default E_AUTH); FAKE_FETCH_MESSAGE: texto del error
# FAKE_FETCH_MODE_FILE: si existe, su contenido reemplaza a FAKE_FETCH_MODE (cambio en caliente)
# FAKE_ARGS_FILE: registra los argumentos recibidos; FAKE_TS_FILE: registra inicio y fin de archive
# FAKE_GL_PID_FILE: en hang, pid del hijo (como un proceso de red)

import hashlib
import json
import os
import re
import subprocess
import sys
import time

DEFAULT = "main"
DB = {"server": "alpha", "container": "mysql8", "db": "appdb"}
HOSTILE_DB = {"server": "alpha", "container": "mysql8", "db": "<b>evil&db</b>"}
PROJECTS = [
    {"name": "alpha-app", "path": "grp/alpha-app", "has_db": True, "db": DB},
    {"name": "<b>evil&proj</b>", "path": "grp/evil", "has_db": True, "db": HOSTILE_DB},
    {"name": "solo-codigo", "path": "grp/solo", "has_db": False},
]
BRANCHES = [f"feat/b{i:02d}" for i in range(19)] + [DEFAULT, "<i>mala</i>&rama"]
TAGS = ["v1.0.0", "v1.1.0"]


def sha(name):
    return hashlib.sha1(name.encode()).hexdigest()


def refs(names):
    return [{"name": n, "default": n == DEFAULT, "protected": False, "sha": sha(n)} for n in names]


def emit(obj, code=0):
    print(json.dumps(obj))
    sys.exit(code)


def error(code, message=None):
    msg = message or os.environ.get("FAKE_FETCH_MESSAGE", "fallo simulado")
    emit({"schema_version": 1, "ok": False, "command": cmd, "error": {"code": code, "message": msg}}, 1)


def stamp(tag):
    path = os.environ.get("FAKE_TS_FILE")
    if path:
        with open(path, "a") as fh:
            fh.write(f"gitlab {tag} {time.time()}\n")


argv = sys.argv[1:]
cmd = argv[0] if argv else ""
opts = {}
rest = argv[1:]
while len(rest) >= 2:
    opts[rest[0].lstrip("-")] = rest[1]
    rest = rest[2:]
if os.environ.get("FAKE_ARGS_FILE"):
    with open(os.environ["FAKE_ARGS_FILE"], "a") as fh:
        fh.write("gitlab " + " ".join(argv) + "\n")
mode = os.environ.get("FAKE_FETCH_MODE", "ok")
mode_file = os.environ.get("FAKE_FETCH_MODE_FILE")
if mode_file and os.path.exists(mode_file):
    mode = open(mode_file).read().strip()
err_code = os.environ.get("FAKE_FETCH_ERR", "E_AUTH")

if mode == "flood_out":
    sys.stdout.write("x" * 3_000_000)
    sys.stdout.flush()
    time.sleep(300)
if mode == "empty":
    sys.exit(1)
if mode == "garbage":
    print("esto no es json")
    sys.exit(0)
if mode == "badjson":
    print(json.dumps({"schema_version": 1, "ok": True, "command": cmd}))
    sys.exit(0)
if mode == "err_all":
    error(err_code)
if cmd == "catalog":
    emit({"schema_version": 1, "ok": True, "command": "catalog", "projects": PROJECTS})
if cmd == "branches":
    q = opts.get("search")
    names = [n for n in BRANCHES if q is None or q.lower() in n.lower()]
    emit({"schema_version": 1, "ok": True, "command": "branches", "project": opts.get("project", ""), "refs": refs(names), "truncated": q is None and len(names) > 30})
if cmd == "tags":
    emit({"schema_version": 1, "ok": True, "command": "tags", "project": opts.get("project", ""), "refs": refs(TAGS), "truncated": False})
if cmd == "archive":
    stamp("start")
    if mode == "err":
        stamp("end")
        error(err_code)
    if mode == "hang":
        child = subprocess.Popen(["sleep", "300"])
        if os.environ.get("FAKE_GL_PID_FILE"):
            open(os.environ["FAKE_GL_PID_FILE"], "w").write(str(child.pid))
        child.wait()
    if mode == "slow":
        time.sleep(float(os.environ.get("FAKE_SLEEP", "0.3")))
    slug = re.sub(r"[^A-Za-z0-9]+", "-", opts["project"]).strip("-")
    ref_slug = re.sub(r"[^A-Za-z0-9]+", "-", opts["ref"]).strip("-")
    commit = sha(opts["ref"] + ("+1" if mode == "moved" else ""))
    fmt = opts["format"]
    filename = f"{slug}_{ref_slug}_{commit[:8]}_{opts['job-id']}.{fmt}"
    stamp("end")
    emit({"schema_version": 1, "ok": True, "command": "archive", "job_id": opts["job-id"], "project": opts["project"],
          "ref": opts["ref"], "commit": commit, "format": fmt, "file": f"/dumps/_gitlab/{filename}",
          "relpath": f"_gitlab/{filename}", "filename": filename, "size_bytes": 654321, "sha256": "c" * 64,
          "duration_s": 2, "verified": {"archive": True}})
sys.exit(2)
