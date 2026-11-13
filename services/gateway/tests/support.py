# =============================================================================
# services/gateway/tests/support.py — Arnés de pruebas: gateway real en puertos efímeros y validador de esquema
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import hashlib
import json
import os
import tempfile
import unittest

import aiohttp

from gateway.app import Gateway
from gateway.config import Config
from gateway.context import build_context

API_TOKEN = "token-de-prueba-0123456789abcdef"
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "contracts", "link.schema.json")
BASE_TIME = 1_800_000_000


class FakeClock:
    def __init__(self):
        self.t = BASE_TIME

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def load_defs():
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)["$defs"]


def validate(instance, schema, defs, path="$"):
    """Validador mínimo de JSON Schema para los campos que usa el contrato."""
    errors = []
    if "$ref" in schema:
        return validate(instance, defs[schema["$ref"].split("/")[-1]], defs, path)
    for sub in schema.get("allOf", []):
        errors += validate(instance, sub, defs, path)
    types = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list, "null": type(None)}
    if "type" in schema:
        wanted = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        ok = any(
            isinstance(instance, types[t]) and not (t == "integer" and isinstance(instance, bool))
            for t in wanted
        )
        if not ok:
            return errors + [f"{path}: tipo {type(instance).__name__} no cumple {wanted}"]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} fuera de enum")
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: debe ser {schema['const']!r}")
    if "pattern" in schema:
        import re
        if not re.search(schema["pattern"], instance):
            errors.append(f"{path}: no cumple el patrón")
    if "minimum" in schema and instance < schema["minimum"]:
        errors.append(f"{path}: menor al mínimo")
    if isinstance(instance, dict):
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}: falta {name}")
        for name, sub in schema.get("properties", {}).items():
            if name in instance:
                errors += validate(instance[name], sub, defs, f"{path}.{name}")
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errors += validate(item, schema["items"], defs, f"{path}[{i}]")
    return errors


class GatewayCase(unittest.IsolatedAsyncioTestCase):
    FILE_SIZE = 1000
    BLOCK = 128

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.dumps = os.path.join(root, "dumps")
        self.data = os.path.join(root, "data")
        os.makedirs(os.path.join(self.dumps, "srv"), mode=0o700)
        os.makedirs(os.path.join(self.dumps, ".locks"), mode=0o700)
        self.payload = os.urandom(self.FILE_SIZE)
        self.dump_path = self.write_dump("srv/a.sql.gz", self.payload)
        token_file = os.path.join(root, "api.token")
        with open(token_file, "w", encoding="utf-8") as fh:
            fh.write(API_TOKEN + "\n")
        self.config = Config(
            dumps_root=self.dumps, data_dir=self.data, public_base_url="https://dumps.example.test",
            api_token_file=token_file, public_port=0, internal_port=0, bind_host="127.0.0.1",
            cleanup_interval=3600, block_size=self.BLOCK,
        )
        self.clock = FakeClock()
        self.ctx = build_context(self.config, self.clock)
        self.gw = Gateway(self.ctx)
        await self.gw.start()
        self.pub = f"http://127.0.0.1:{self.gw.public_port}"
        self.int = f"http://127.0.0.1:{self.gw.internal_port}"
        self.http = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.http.close()
        await self.gw.stop()
        self.ctx.close()
        self.tmp.cleanup()

    def write_dump(self, relpath, data):
        path = os.path.join(self.dumps, relpath)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        os.chmod(path, 0o600)
        return path

    async def api(self, method, path, body=None, token=API_TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with self.http.request(method, self.int + path, json=body, headers=headers) as resp:
            text = await resp.text()
            return resp.status, (json.loads(text) if text else None)

    async def create_link(self, **overrides):
        body = {"relpath": "srv/a.sql.gz", "created_by": "tester", "job_id": "job-1"}
        body.update(overrides)
        status, data = await self.api("POST", "/v1/links", body)
        self.assertEqual(status, 201, data)
        data["token"] = data["url"].rsplit("/d/", 1)[1]
        return data

    async def open_session(self, link):
        async with self.http.post(f"{self.pub}/d/{link['token']}", allow_redirects=False) as resp:
            self.assertEqual(resp.status, 303)
            return resp.headers["Location"]

    async def fetch(self, location, rng=None, method="GET"):
        headers = {"Range": rng} if rng else {}
        async with self.http.request(method, self.pub + location, headers=headers) as resp:
            return resp.status, dict(resp.headers), await resp.read()

    async def link_state(self, link):
        return (await self.api("GET", f"/v1/links/{link['id']}"))[1]

    def audit_events(self):
        with open(os.path.join(self.data, "audit.jsonl"), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    @staticmethod
    def sha(data):
        return hashlib.sha256(data).hexdigest()
