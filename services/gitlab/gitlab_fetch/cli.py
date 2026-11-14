# =============================================================================
# services/gitlab/gitlab_fetch/cli.py — Análisis de argumentos y despacho de comandos con salida JSON
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import argparse
import os
import signal
import sys
import time

from . import archive, catalog, names, refs, scope
from .client import GitLabClient
from .config import from_env
from .errors import FetchError
from .output import emit, emit_error, log, register_secret

COMMANDS = ("catalog", "branches", "tags", "archive")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise FetchError("E_USAGE", "Uso incorrecto: " + message, exit_code=2)


def build_parser():
    parser = _Parser(prog="fetch.py", add_help=False)
    sub = parser.add_subparsers(dest="command", parser_class=_Parser)
    sub.add_parser("catalog", add_help=False)
    for name in ("branches", "tags"):
        p = sub.add_parser(name, add_help=False)
        p.add_argument("--project", required=True)
        p.add_argument("--search")
    p = sub.add_parser("archive", add_help=False)
    p.add_argument("--project", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--format", default="zip")
    p.add_argument("--job-id", required=True)
    return parser


def _on_alarm(signum, frame):
    raise FetchError("E_NETWORK", "Se agotó el tiempo total de la operación (GITLAB_TIMEOUT_SECONDS)")


def _arm(seconds):
    try:
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    except ValueError:
        pass


def _disarm():
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, signal.SIG_IGN)
    except ValueError:
        pass


def _run(args, env, sleep):
    cfg = from_env(env)
    if args.command != "catalog":
        _arm(cfg.timeout_s)
    projects = catalog.load(cfg.projects_file)
    if args.command == "catalog":
        return {"projects": catalog.public_view(projects)}
    project = catalog.find(projects, args.project)
    if args.command == "archive":
        names.check_ref(args.ref)
        names.check_format(args.format)
        names.check_job_id(args.job_id)
    else:
        names.check_search(args.search)
    token = cfg.read_token()
    register_secret(token)
    client = GitLabClient(cfg.base_url, token, cfg.read_timeout_s, cfg.timeout_s, sleep=sleep)
    scope.ensure_read_only(client)
    if args.command == "archive":
        log(f"archive {project['name']}@{args.ref} ({args.format})")
        return archive.fetch_archive(cfg, client, project, args.ref, args.format, args.job_id)
    listing, truncated = refs.list_refs(client, project, args.command, args.search)
    return {"project": project["name"], "refs": listing, "truncated": truncated}


def main(argv=None, env=None, sleep=time.sleep):
    os.umask(0o077)
    env = os.environ if env is None else env
    argv = sys.argv[1:] if argv is None else list(argv)
    command = argv[0] if argv and argv[0] in COMMANDS else "catalog"
    try:
        args = build_parser().parse_args(argv)
        if args.command is None:
            raise FetchError("E_USAGE", "Falta el comando (catalog|branches|tags|archive)", exit_code=2)
        command = args.command
        try:
            fields = _run(args, env, sleep)
        finally:
            _disarm()
        emit(command, True, **fields)
        return 0
    except FetchError as exc:
        emit_error(command, exc.code, exc.message)
        return exc.exit_code
    except Exception as exc:
        log(f"error interno: {type(exc).__name__}")
        emit_error(command, "E_INTERNAL", f"Error interno ({type(exc).__name__})")
        return 1
