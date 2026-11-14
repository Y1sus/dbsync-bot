# =============================================================================
# services/bot/tests/test_gitlab_client.py — Pruebas del cliente de GitLab: subproceso, validación de source-result y secretos
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import json
import os
import sys
import tempfile
import unittest

from bot import flow
from bot.gitlab_client import GITLAB_CODES, GitlabClient, sanitize_search
from bot.redact import Redactor
from bot.runner_client import RunnerError

FETCH = os.path.join(os.path.dirname(__file__), "fakes", "fake_fetch.py")
SHA = "c" * 64
COMMIT = "d" * 40
ARCHIVE = {"schema_version": 1, "ok": True, "command": "archive", "job_id": "j-code", "project": "p", "ref": "main",
           "commit": COMMIT, "format": "zip", "file": "/d/_gitlab/p.zip", "relpath": "_gitlab/p.zip", "filename": "p.zip",
           "size_bytes": 5, "sha256": SHA, "duration_s": 1, "verified": {"archive": True}}


def make(mode="ok", timeout=5, **env):
    return GitlabClient(FETCH, timeout=timeout, kill_grace=0.1, redactor=Redactor(["glpat" "-SECRET-123"]),
                        env={"FAKE_FETCH_MODE": mode, **env})


def parse(client, payload, command, code=0):
    return client._parse_json(code, json.dumps(payload), command)


class SpawnTests(unittest.IsolatedAsyncioTestCase):
    async def test_lookups_and_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = os.path.join(tmp, "args")
            c = make(FAKE_ARGS_FILE=args)
            projects = await c.catalog()
            self.assertEqual([p["name"] for p in projects], ["alpha-app", "<b>evil&proj</b>", "solo-codigo"])
            self.assertEqual(projects[0]["db"], {"server": "alpha", "container": "mysql8", "db": "appdb"})
            res = await c.branches("alpha-app")
            names = [r["name"] for r in res["refs"]]
            self.assertIn("main", names)
            self.assertNotIn("<i>mala</i>&rama", names)
            self.assertTrue(all(len(r["sha"]) == 40 for r in res["refs"]))
            found = await c.branches("alpha-app", "b03")
            self.assertEqual([r["name"] for r in found["refs"]], ["feat/b03"])
            self.assertEqual([r["name"] for r in (await c.tags("alpha-app"))["refs"]], ["v1.0.0", "v1.1.0"])
            with open(args) as fh:
                lines = fh.read().splitlines()
            self.assertEqual(lines[1], "gitlab branches --project alpha-app")
            self.assertEqual(lines[2], "gitlab branches --project alpha-app --search b03")

    async def test_archive_passes_exact_argv_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = os.path.join(tmp, "args")
            res = await make(FAKE_ARGS_FILE=args).archive("alpha-app", "feat/b01", "tar.gz", "tg1-aaaa1111-code")
            self.assertEqual((res["format"], res["size_bytes"], len(res["commit"])), ("tar.gz", 654321, 40))
            self.assertTrue(res["relpath"].startswith("_gitlab/"))
            with open(args) as fh:
                self.assertEqual(fh.read().strip(), "gitlab archive --project alpha-app --ref feat/b01 --format tar.gz --job-id tg1-aaaa1111-code")

    async def test_error_output_is_redacted_and_coded(self):
        c = make("err", FAKE_FETCH_ERR="E_FORBIDDEN", FAKE_FETCH_MESSAGE="token glpat" "-SECRET-123 y glpat" "-OTRO-999 sin permiso")
        with self.assertRaises(RunnerError) as cm:
            await c.archive("alpha-app", "main", "zip", "j-code")
        self.assertEqual(cm.exception.code, "E_FORBIDDEN")
        self.assertNotIn("glpat" "-SECRET", cm.exception.message)
        self.assertNotIn("glpat" "-OTRO", cm.exception.message)

    async def test_bad_outputs_are_e_bad_output(self):
        for mode in ("garbage", "empty", "badjson"):
            with self.assertRaises(RunnerError, msg=mode) as cm:
                await make(mode).catalog()
            self.assertEqual(cm.exception.code, "E_BAD_OUTPUT", mode)

    async def test_hang_times_out_and_kills_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "pid")
            with self.assertRaises(RunnerError) as cm:
                await make("hang", timeout=0.1, FAKE_GL_PID_FILE=pid_file).archive("alpha-app", "main", "zip", "j-code")
            self.assertEqual(cm.exception.code, "E_TIMEOUT")
            with open(pid_file) as fh:
                self.assertTrue(dead(int(fh.read())))

    async def test_flooding_stdout_is_cut_with_gitlab_wording(self):
        with self.assertRaises(RunnerError) as cm:
            await make("flood_out", timeout=10).catalog()
        self.assertEqual(cm.exception.code, "E_BAD_OUTPUT")
        self.assertIn("servicio de GitLab produjo demasiada salida por stdout", cm.exception.message)
        self.assertNotIn("xxxx", cm.exception.message)

    async def test_missing_fetch_is_runner_start_error(self):
        c = GitlabClient("/no/existe.py", python="/no/python")
        with self.assertRaises(RunnerError) as cm:
            await c.catalog()
        self.assertEqual(cm.exception.code, "E_RUNNER_START")


def dead(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.c = make()

    def test_argv_uses_current_interpreter_isolated_and_list(self):
        self.assertEqual(self.c._argv(["catalog"]), [sys.executable, "-I", FETCH, "catalog"])

    def test_every_error_code_maps_and_has_spanish_message(self):
        for code in GITLAB_CODES:
            with self.assertRaises(RunnerError) as cm:
                parse(self.c, {"schema_version": 1, "ok": False, "command": "archive", "error": {"code": code, "message": "m"}}, "archive", 1)
            self.assertEqual(cm.exception.code, code)
            self.assertIn(code, flow.GITLAB_MESSAGES)
        for code in ("E_TIMEOUT", "E_BAD_OUTPUT", "E_RUNNER_START", "E_CANCELLED"):
            self.assertIn(code, flow.GITLAB_MESSAGES)

    def test_token_problems_say_gitlab_token(self):
        self.assertIn("token de GitLab", flow.GITLAB_MESSAGES["E_AUTH"])
        self.assertIn("token de GitLab", flow.GITLAB_MESSAGES["E_FORBIDDEN"])

    def test_unknown_error_code_becomes_internal(self):
        with self.assertRaises(RunnerError) as cm:
            parse(self.c, {"schema_version": 1, "ok": False, "command": "tags", "error": {"code": "E_RARO", "message": "x"}}, "tags", 1)
        self.assertEqual(cm.exception.code, "E_INTERNAL")

    def test_valid_archive_and_rejections(self):
        self.assertEqual(parse(self.c, ARCHIVE, "archive")["filename"], "p.zip")
        bad_cases = {
            "commit corto": {"commit": "abc"}, "formato": {"format": "rar"}, "sha": {"sha256": "x"}, "sin verificar": {"verified": {"archive": False}},
            "relpath anidado": {"relpath": "_gitlab/a/p.zip"}, "relpath otra fuente": {"relpath": "srv/p.zip"},
            "sufijo": {"relpath": "_gitlab/p.sql.gz"}, "tamaño": {"size_bytes": 0}, "duración": {"duration_s": -1},
            "versión": {"schema_version": 2},
        }
        for label, patch in bad_cases.items():
            with self.assertRaises(RunnerError, msg=label) as cm:
                parse(self.c, {**ARCHIVE, **patch}, "archive")
            self.assertEqual(cm.exception.code, "E_BAD_OUTPUT", label)

    def test_wrong_command_and_nonzero_exit_on_success(self):
        for code, command in ((0, "tags"), (1, "archive")):
            with self.assertRaises(RunnerError) as cm:
                parse(self.c, ARCHIVE, command, code)
            self.assertEqual(cm.exception.code, "E_BAD_OUTPUT")

    def test_catalog_requires_db_when_has_db(self):
        base = {"schema_version": 1, "ok": True, "command": "catalog"}
        parse(self.c, {**base, "projects": [{"name": "a", "path": "g/a", "has_db": False}]}, "catalog")
        with self.assertRaises(RunnerError):
            parse(self.c, {**base, "projects": [{"name": "a", "path": "g/a", "has_db": True}]}, "catalog")
        with self.assertRaises(RunnerError):
            parse(self.c, {**base, "projects": [{"name": "a", "path": "g/a", "has_db": "si"}]}, "catalog")

    def test_refs_drop_unsafe_names(self):
        refs = [{"name": n, "sha": COMMIT} for n in ("main", "-rf", "a b", "ok/x", "<b>", "v1.0")]
        res = parse(self.c, {"schema_version": 1, "ok": True, "command": "tags", "project": "p", "refs": refs, "truncated": False}, "tags")
        self.assertEqual([r["name"] for r in res["refs"]], ["main", "ok/x", "v1.0"])

    def test_sanitize_search(self):
        self.assertEqual(sanitize_search("  feat/<b>x</b>; rm -rf /  "), "feat/bx/brm-rf/")
        self.assertEqual(sanitize_search("--evil"), "evil")
        self.assertEqual(sanitize_search("x" * 500), "x" * 60)
        self.assertEqual(sanitize_search("<>;& \n"), "")
        self.assertEqual(sanitize_search(None), "")

    def test_archive_rejects_bad_params_without_spawning(self):
        import asyncio
        for args in (("p", "-x", "zip", "j"), ("p", "main", "rar", "j"), ("p", "main", "zip", "../x")):
            with self.assertRaises(RunnerError) as cm:
                asyncio.run(self.c.archive(*args))
            self.assertEqual(cm.exception.code, "E_USAGE")


if __name__ == "__main__":
    unittest.main()
