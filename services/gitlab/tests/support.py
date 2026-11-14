# =============================================================================
# services/gitlab/tests/support.py — Utilidades de prueba: entorno temporal, archivos de muestra, ejecución del CLI y validador del esquema
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import atexit
import contextlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from gitlab_fetch import cli, output
from tests.fakes.gitlab_fake import SHA, TOKEN, FakeGitLab

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT.parents[1] / "contracts" / "source-result.schema.json").read_text())

CATALOG = {"projects": [
    {"name": "demo", "path": "grp/demo", "db": {"server": "devimed", "container": "mysql8", "db": "demo_dev"}},
    {"name": "My App", "path": "grp/sub/my-app"},
]}


def make_zip(entries=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in (entries if entries is not None else {"a.txt": b"hola" * 500}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def make_targz(entries=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (entries if entries is not None else {"a.txt": b"hola" * 500}).items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _check(schema, value, defs):
    if "$ref" in schema:
        return _check(defs[schema["$ref"].split("/")[-1]], value, defs)
    errors = []
    if "oneOf" in schema:
        hits = [s for s in schema["oneOf"] if not _check(s, value, defs)]
        if len(hits) != 1:
            errors.append(f"oneOf: {len(hits)} coincidencias")
    types = {"object": dict, "string": str, "boolean": bool, "integer": int, "array": list}
    if "type" in schema and (not isinstance(value, types[schema["type"]])
                             or (schema["type"] == "integer" and isinstance(value, bool))):
        return [f"tipo {schema['type']}"]
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        errors.append(f"const {schema['const']!r} != {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"enum {value!r}")
    if "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append(f"pattern {schema['pattern']}")
    if "minimum" in schema and value < schema["minimum"]:
        errors.append("minimum")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"falta {key}")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors += _check(sub, value[key], defs)
    if isinstance(value, list) and "items" in schema:
        for item in value:
            errors += _check(schema["items"], item, defs)
    return errors


def schema_errors(doc):
    return _check(SCHEMA, doc, SCHEMA["$defs"])


class Env:
    """Directorio temporal con catálogo, token y entorno apuntando al GitLab falso."""

    def __init__(self, fake, catalog=CATALOG):
        self.tmp = tempfile.mkdtemp(prefix="gitlab-test-")
        self.dumps = os.path.join(self.tmp, "dumps")
        self.token_file = os.path.join(self.tmp, "token")
        self.projects_file = os.path.join(self.tmp, "projects.json")
        Path(self.token_file).write_text(TOKEN + "\n")
        Path(self.projects_file).write_text(json.dumps(catalog))
        self.env = {
            "GITLAB_BASE_URL": fake.url, "GITLAB_ALLOW_HTTP": "1", "GITLAB_TOKEN_FILE": self.token_file,
            "GITLAB_PROJECTS_FILE": self.projects_file, "DBSYNC_DUMPS_DIR": self.dumps,
            "DBSYNC_MIN_FREE_MB": "1", "GITLAB_READ_TIMEOUT_SECONDS": "5",
        }
        self.sleeps = []

    def run(self, *argv, **overrides):
        env = {**self.env, **overrides}
        out, err = io.StringIO(), io.StringIO()
        output._secrets.clear()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), env, self.sleeps.append)
        lines = out.getvalue().splitlines()
        doc = json.loads(lines[0]) if lines else None
        return code, doc, out.getvalue(), err.getvalue()

    def files(self):
        d = os.path.join(self.dumps, "_gitlab")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


_shared = []


def shared_fake():
    """Un solo GitLab falso por proceso de pruebas: arrancar uno por clase cuesta demasiado."""
    if not _shared:
        _shared.append(FakeGitLab())
        atexit.register(_shared[0].close)
    return _shared[0]


class FakeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = shared_fake()

    def setUp(self):
        self.fake.reset()
        self.e = Env(self.fake)
        self.addCleanup(self.e.cleanup)
        self.fake.add_project("grp/demo", commits={"develop": SHA, "feat/x": SHA, "v1.0": SHA},
                              archives={"zip": make_zip(), "tar.gz": make_targz()})

    def call(self, *argv, **overrides):
        code, doc, out, err = self.e.run(*argv, **overrides)
        self.assertEqual(schema_errors(doc), [], doc)
        return code, doc, out, err
