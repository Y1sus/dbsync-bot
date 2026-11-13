# =============================================================================
# integration/e2e_stack.py — Eval de integración runner -> bot -> gateway real
# Corre el runner real (ssh/docker simulados), el JobManager del bot y el
# gateway real como proceso, y recorre el ciclo completo de un enlace.
# Umbral de aprobación: 100 % de comprobaciones.
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import hashlib
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import time

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "services", "bot"))

from bot.audit import Audit  # noqa: E402
from bot.gateway_client import GatewayClient  # noqa: E402
from bot.jobs import DumpRequest, JobManager  # noqa: E402
from bot.runner_client import RunnerClient  # noqa: E402

SHIMS = os.path.join(ROOT, "services", "runner", "tests", "shims")
RUNNER = os.path.join(ROOT, "services", "runner", "runner.sh")
GATEWAY_TOKEN = "gw-token-de-prueba-0123456789abcdef"
PASS = []
FAIL = []


def check(desc, cond, detail=""):
    (PASS if cond else FAIL).append(desc)
    print(f"  [{'OK' if cond else 'FALLO'}] {desc}" + (f"  ({detail})" if detail and not cond else ""))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_http(url, headers=None, tries=100):
    async with aiohttp.ClientSession() as http:
        for _ in range(tries):
            try:
                async with http.get(url, headers=headers) as r:
                    if r.status < 500:
                        return True
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.1)
    return False


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


async def main():
    tmp = tempfile.mkdtemp(prefix="e2e-stack-")
    for d in ("servers", "dumps", "gw-data", "bot-data"):
        os.makedirs(os.path.join(tmp, d), mode=0o700, exist_ok=True)
    key = os.path.join(tmp, "key")
    open(key, "w").close()
    open(os.path.join(tmp, "known_hosts"), "w").close()
    srv = os.path.join(tmp, "servers", "gate.env")
    with open(srv, "w") as f:
        f.write(f"SERVER_HOST=127.0.0.1\nSERVER_USER=tester\nSERVER_KEY={key}\nDB_USER=root\nDB_PASSWORD=pw-de-prueba\n")
    os.chmod(srv, 0o600)
    token_file = os.path.join(tmp, "gateway_token")
    with open(token_file, "w") as f:
        f.write(GATEWAY_TOKEN)
    os.chmod(token_file, 0o600)

    pub, internal = free_port(), free_port()
    base = f"http://127.0.0.1:{pub}"
    gw_log = open(os.path.join(tmp, "gateway.log"), "wb")
    gw = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "gateway", cwd=os.path.join(ROOT, "services", "gateway"),
        stdout=gw_log, stderr=gw_log,
        env={**os.environ, "GATEWAY_PUBLIC_BASE_URL": base, "GATEWAY_DUMPS_ROOT": os.path.join(tmp, "dumps"),
             "GATEWAY_DATA_DIR": os.path.join(tmp, "gw-data"), "GATEWAY_API_TOKEN_FILE": token_file,
             "GATEWAY_PUBLIC_PORT": str(pub), "GATEWAY_INTERNAL_PORT": str(internal),
             "GATEWAY_BIND_HOST": "127.0.0.1", "GATEWAY_CLEANUP_INTERVAL_SECONDS": "1"})
    gateway = GatewayClient(f"http://127.0.0.1:{internal}", GATEWAY_TOKEN)
    try:
        up = await wait_http(f"http://127.0.0.1:{internal}/v1/health", {"Authorization": f"Bearer {GATEWAY_TOKEN}"})
        check("gateway real arranca", up)
        if not up:
            return

        runner = RunnerClient(RUNNER, timeout=60, env={
            "DBSYNC_SERVERS_DIR": os.path.join(tmp, "servers"), "DBSYNC_DUMPS_DIR": os.path.join(tmp, "dumps"),
            "DBSYNC_KNOWN_HOSTS": os.path.join(tmp, "known_hosts"), "DBSYNC_SSH_BIN": os.path.join(SHIMS, "ssh"),
            "SHIM_LOG": os.path.join(tmp, "ssh.log"), "SHIM_PATH_PREFIX": SHIMS,
            "FAKE_ENVLOG": os.path.join(tmp, "envlog"), "DBSYNC_MIN_FREE_MB": "1"})
        audit = Audit(os.path.join(tmp, "bot-data", "audit.jsonl"))
        jobs = JobManager(runner, gateway, audit, max_jobs=1)

        print("== 1. runner real -> JobManager del bot -> gateway real")
        out = await jobs.run(DumpRequest(user_id=506679030, server="gate", container="fakemysql", db="appdb",
                                         ttl=3600, max_downloads=1), "tg1-abcd1234")
        check("el job termina en 'ok' (dump + enlace)", out.kind == "ok", f"{out.kind} {out.code} {out.message}")
        if out.kind != "ok":
            return
        link, dump = out.link, out.dump
        token = re.search(r"/d/([^/?#]+)$", link["url"]).group(1)
        file_path = os.path.join(tmp, "dumps", dump["relpath"])
        check("el gateway resuelve el relpath del runner (mismo archivo)", os.path.isfile(file_path))
        check("sha256 del runner coincide con el archivo", sha(file_path) == dump["sha256"])
        check("el enlace usa la URL pública configurada", link["url"].startswith(base + "/d/"))
        size = dump["size_bytes"]
        check("el tamaño del enlace coincide con el dump", link["size_bytes"] == size)

        async with aiohttp.ClientSession() as http:
            landing = f"{base}/d/{token}"

            print("== 2. GET y HEAD no cuentan")
            async with http.get(landing) as r:
                body = await r.text()
                check("GET intermedia: 200 y muestra el archivo", r.status == 200 and dump["filename"] in body)
                check("cabeceras de privacidad", r.headers.get("Cache-Control") == "no-store"
                      and "noindex" in r.headers.get("X-Robots-Tag", "")
                      and r.headers.get("Referrer-Policy") == "no-referrer")
            async with http.head(landing) as r:
                check("HEAD intermedia: 200", r.status == 200)
            async with http.get(f"{base}/d/{'x' * 43}") as r:
                check("token desconocido: 404", r.status == 404)
            rec = (await gateway.list_links("all"))[0]
            check("después de GET/HEAD: 0 descargas contadas", rec["downloads_completed"] == 0)

            print("== 3. POST abre sesión; dos POST reutilizan la misma")
            async with http.post(landing, allow_redirects=False) as r:
                check("POST: 303", r.status == 303)
                session_url = base + r.headers["Location"] if r.headers["Location"].startswith("/") else r.headers["Location"]
            async with http.post(landing, allow_redirects=False) as r:
                loc2 = r.headers.get("Location", "")
                check("segundo POST reutiliza la sesión", session_url.endswith(loc2))
            async with http.head(session_url) as r:
                check("HEAD de sesión: 200 y no cuenta", r.status == 200)
            check("HEAD de sesión no cuenta",
                  (await gateway.list_links("all"))[0]["downloads_completed"] == 0)

            print("== 4. descarga en dos rangos (el último primero)")
            half = size // 2
            async with http.get(session_url, headers={"Range": f"bytes={half}-"}) as r:
                part2 = await r.read()
                check("rango final: 206 con Content-Range", r.status == 206
                      and r.headers.get("Content-Range") == f"bytes {half}-{size - 1}/{size}")
            mid = (await gateway.list_links("all"))[0]
            check("el último byte cuenta una vez, aunque falte el primer rango", mid["downloads_completed"] == 1)
            async with http.get(session_url, headers={"Range": f"bytes=0-{half - 1}"}) as r:
                part1 = await r.read()
                check("rango inicial: 206", r.status == 206)
            blob = part1 + part2
            check("el reensamblado coincide con sha256 del runner", hashlib.sha256(blob).hexdigest() == dump["sha256"])
            rec = (await gateway.list_links("all"))[0]
            check("downloads_completed == 1 (no se duplica)", rec["downloads_completed"] == 1, rec)
            check("el enlace queda agotado", rec["state"] == "exhausted")

            print("== 5. la sesión sigue sirviendo; un POST nuevo no")
            async with http.get(session_url, headers={"Range": "bytes=0-9"}) as r:
                check("reanudar rango en la misma sesión: 206", r.status == 206 and len(await r.read()) == 10)
            async with http.get(session_url) as r:
                check("descarga completa en la sesión abierta: 200", r.status == 200
                      and hashlib.sha256(await r.read()).hexdigest() == dump["sha256"])
            rec = (await gateway.list_links("all"))[0]
            check("repetir en la sesión no suma", rec["downloads_completed"] == 1)
            async with http.post(landing, allow_redirects=False) as r:
                check("POST nuevo con el enlace agotado: 410", r.status == 410)

            print("== 6. fronteras de red y autenticación")
            async with http.get(f"{base}/v1/health") as r:
                check("la API interna no se sirve por el puerto público", r.status == 404)
            async with http.get(f"http://127.0.0.1:{internal}/v1/links") as r:
                check("API interna sin token: 401", r.status == 401)
            async with http.get(f"http://127.0.0.1:{internal}/v1/links",
                                headers={"Authorization": "Bearer incorrecto"}) as r:
                check("API interna con token incorrecto: 401", r.status == 401)

            print("== 7. enlace expirado y enlace revocado")
            out2 = await jobs.run(DumpRequest(user_id=506679030, server="gate", container="fakemysql", db="appdb",
                                              ttl=3600, max_downloads=1), "tg2-abcd1234")
            check("segundo job ok", out2.kind == "ok", f"{out2.kind} {out2.code}")
            tok2 = re.search(r"/d/([^/?#]+)$", out2.link["url"]).group(1)
            async with http.post(f"{base}/d/{tok2}", allow_redirects=False) as r:
                sess2 = base + r.headers["Location"]
            con = sqlite3.connect(os.path.join(tmp, "gw-data", "gateway.db"))
            con.execute("UPDATE links SET expires_at = expires_at - 7200 WHERE id = ?", (out2.link["id"],))
            con.commit()
            con.close()
            async with http.get(f"{base}/d/{tok2}") as r:
                check("enlace expirado (GET): 410", r.status == 410)
            async with http.post(f"{base}/d/{tok2}", allow_redirects=False) as r:
                check("enlace expirado con sesión abierta (POST): 410, sin obtener el archivo de nuevo", r.status == 410)
            async with http.get(sess2, headers={"Range": "bytes=0-9"}) as r:
                check("la sesión ya abierta termina su descarga aunque el enlace expire", r.status == 206)

            out3 = await jobs.run(DumpRequest(user_id=506679030, server="gate", container="fakemysql", db="appdb",
                                              ttl=3600, max_downloads=3), "tg3-abcd1234")
            tok3 = re.search(r"/d/([^/?#]+)$", out3.link["url"]).group(1)
            await gateway.revoke(out3.link["id"])
            async with http.post(f"{base}/d/{tok3}", allow_redirects=False) as r:
                check("enlace revocado: 410", r.status == 410)

        print("== 8. el token nunca queda en logs ni en auditorías")
        await asyncio.sleep(0.3)
        haystack = ""
        for p in (os.path.join(tmp, "gw-data", "audit.jsonl"), os.path.join(tmp, "bot-data", "audit.jsonl"),
                  os.path.join(tmp, "gateway.log")):
            if os.path.exists(p):
                haystack += open(p, errors="replace").read()
        check("tokens de enlace ausentes", token not in haystack and tok2 not in haystack and tok3 not in haystack)
        check("token del gateway ausente", GATEWAY_TOKEN not in haystack)
        check("la auditoría del gateway registra la descarga",
              '"download_completed"' in open(os.path.join(tmp, "gw-data", "audit.jsonl")).read())
        check("el dump del runner quedó 0600", oct(os.stat(file_path).st_mode & 0o777) == "0o600")
    finally:
        await gateway.close()
        if gw.returncode is None:
            gw.terminate()
            try:
                await asyncio.wait_for(gw.wait(), 5)
            except asyncio.TimeoutError:
                gw.kill()
        gw_log.close()
        shutil.rmtree(tmp, ignore_errors=True)


t0 = time.monotonic()
asyncio.run(main())
total = len(PASS) + len(FAIL)
print(f"\nResultado: {len(PASS)}/{total} comprobaciones en {time.monotonic() - t0:.1f} s (umbral 100%)")
sys.exit(0 if not FAIL and total else 1)
