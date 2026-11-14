# =============================================================================
# integration/e2e_stack.py — Eval de integración runner -> bot -> gateway real
# Corre el runner real (ssh/docker simulados), el JobManager del bot y el
# gateway real como proceso, y recorre el ciclo completo de un enlace.
# Umbral de aprobación: 100 % de comprobaciones. Con --full agrega el borrado
# automático (esperas de varios segundos); sin él corre en menos de 1 s.
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import zipfile

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "services", "bot"))

from bot.audit import Audit  # noqa: E402
from bot.gitlab_client import GitlabClient  # noqa: E402
from bot.packages import PackageRequest  # noqa: E402
from bot.gateway_client import GatewayClient  # noqa: E402
from bot.jobs import DumpRequest, JobManager  # noqa: E402
from bot.runner_client import RunnerClient  # noqa: E402

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GL_TESTS = os.path.join(ROOT, "services", "gitlab", "tests")
SHIMS = os.path.join(ROOT, "services", "runner", "tests", "shims")
RUNNER = os.path.join(ROOT, "services", "runner", "runner.sh")
GATEWAY_TOKEN = "gw-token-de-prueba-0123456789abcdef"
FULL = "--full" in sys.argv
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
             "GATEWAY_BIND_HOST": "127.0.0.1", "GATEWAY_CLEANUP_INTERVAL_SECONDS": "1",
             "GATEWAY_POST_DOWNLOAD_GRACE_SECONDS": "4"})
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

        gl_fake_mod = _load("gitlab_fake", os.path.join(GL_TESTS, "fakes", "gitlab_fake.py"))
        fakegl = gl_fake_mod.FakeGitLab()
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("proj/readme.txt", b"hola " * 20000)
            zf.writestr("proj/src/app.php", b"<?php echo 1;" * 500)
        zip_bytes = zbuf.getvalue()
        fakegl.add_project("grp/proj", commits={"main": gl_fake_mod.SHA}, archives={"zip": zip_bytes},
                           branches=["main"])
        gl_token = os.path.join(tmp, "gitlab_token")
        with open(gl_token, "w") as f:
            f.write(gl_fake_mod.TOKEN + "\n")
        os.chmod(gl_token, 0o600)
        gl_catalog = os.path.join(tmp, "projects.json")
        with open(gl_catalog, "w") as f:
            json.dump({"projects": [{"name": "proj", "path": "grp/proj",
                                     "db": {"server": "gate", "container": "fakemysql", "db": "appdb"}}]}, f)
        gitlab = GitlabClient(os.path.join(ROOT, "services", "gitlab", "fetch.py"), timeout=60, env={
            "GITLAB_BASE_URL": fakegl.url, "GITLAB_ALLOW_HTTP": "1", "GITLAB_TOKEN_FILE": gl_token,
            "GITLAB_PROJECTS_FILE": gl_catalog, "DBSYNC_DUMPS_DIR": os.path.join(tmp, "dumps"),
            "DBSYNC_MIN_FREE_MB": "1", "GITLAB_MAX_ARCHIVE_MB": "50"})
        pkg_jobs = JobManager(runner, gateway, audit, max_jobs=1, gitlab=gitlab)

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
        check("el dump del runner quedó 0600", oct(os.stat(file_path).st_mode & 0o777) == "0o600")
        check("el enlace usa la URL pública configurada", link["url"].startswith(base + "/d/"))
        size = dump["size_bytes"]
        check("el tamaño del enlace coincide con el dump", link["files"][0]["size_bytes"] == size)

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
            check("después de GET/HEAD: 0 descargas contadas", rec["files"][0]["downloads_completed"] == 0)

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
                  (await gateway.list_links("all"))[0]["files"][0]["downloads_completed"] == 0)

            print("== 4. descarga en dos rangos (el último primero)")
            half = size // 2
            async with http.get(session_url, headers={"Range": "bytes=-1"}) as r:
                await r.read()
                check("sonda Range bytes=-1: 206", r.status == 206)
            check("la sonda de 1 byte no cuenta como descarga",
                  (await gateway.list_links("all"))[0]["files"][0]["downloads_completed"] == 0)
            async with http.get(session_url, headers={"Range": f"bytes={half}-"}) as r:
                part2 = await r.read()
                check("rango final: 206 con Content-Range", r.status == 206
                      and r.headers.get("Content-Range") == f"bytes {half}-{size - 1}/{size}")
            mid = (await gateway.list_links("all"))[0]
            check("con solo la mitad final la cobertura no está completa: aún no cuenta", mid["files"][0]["downloads_completed"] == 0)
            async with http.get(session_url, headers={"Range": f"bytes=0-{half - 1}"}) as r:
                part1 = await r.read()
                check("rango inicial: 206", r.status == 206)
            blob = part1 + part2
            check("el reensamblado coincide con sha256 del runner", hashlib.sha256(blob).hexdigest() == dump["sha256"])
            rec = (await gateway.list_links("all"))[0]
            check("al completarse la cobertura de bytes cuenta una sola vez", rec["files"][0]["downloads_completed"] == 1, rec)
            check("el enlace queda agotado", rec["state"] == "exhausted")

            print("== 5. la sesión sigue sirviendo; un POST nuevo no")
            async with http.get(session_url, headers={"Range": "bytes=0-9"}) as r:
                check("reanudar rango en la misma sesión: 206", r.status == 206 and len(await r.read()) == 10)
            async with http.get(session_url, headers={"Range": "bytes=11-"}) as r:
                rest = await r.read()
                check("resto del archivo dentro del presupuesto (2 x tamaño): 206", r.status == 206 and len(rest) == size - 11)
            async with http.get(session_url, headers={"Range": "bytes=0-0"}) as r:
                check("presupuesto de la sesión agotado: 410", r.status == 410, r.status)
            rec = (await gateway.list_links("all"))[0]
            check("repetir en la sesión no suma", rec["files"][0]["downloads_completed"] == 1)
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


            async def new_job(tag, max_dl=1):
                o = await jobs.run(DumpRequest(user_id=506679030, server="gate", container="fakemysql", db="appdb",
                                               ttl=3600, max_downloads=max_dl), f"tg-{tag}-abcd1234")
                assert o.kind == "ok", o
                return o, os.path.join(tmp, "dumps", o.dump["relpath"]), re.search(r"/d/([^/?#]+)$", o.link["url"]).group(1)

            async def gone(path, secs):
                for _ in range(int(secs * 10)):
                    if not os.path.exists(path):
                        return True
                    await asyncio.sleep(0.1)
                return False

            if FULL:
                print("== 9. el dump se borra solo cuando el enlace termina")

                o, f, _ = await new_job("rev")
                check("dump existe antes de revocar", os.path.exists(f))
                await gateway.revoke(o.link["id"])
                check("revocar borra el dump (sin keep_file)", await gone(f, 4))

                o, f, _ = await new_job("exp")
                con = sqlite3.connect(os.path.join(tmp, "gw-data", "gateway.db"))
                con.execute("UPDATE links SET expires_at = expires_at - 7200 WHERE id = ?", (o.link["id"],))
                con.commit()
                con.close()
                check("caducar el enlace borra el dump", await gone(f, 4))

                o, f, t = await new_job("quiet")
                await asyncio.sleep(2.5)
                check("un enlace vigente sin descargar conserva su dump", os.path.exists(f))

                o, f, t = await new_job("exh")
                async with http.post(f"{base}/d/{t}", allow_redirects=False) as r:
                    s9 = base + r.headers["Location"]
                async with http.get(s9) as r:
                    await r.read()
                check("agotado: el dump sigue durante la gracia", os.path.exists(f))
                check("agotado: se borra al terminar la gracia", await gone(f, 9))
                async with http.get(s9) as r:
                    check("sesión vencida tras la gracia: ya no sirve (410, o 404 si la limpieza la purgó)", r.status in (404, 410), r.status)


            print("== 10. verificación de origen en el POST")
            o, f, t = await new_job("origin", max_dl=5)
            cases = [
                ("Origin: null + same-origin (navegador real)", {"Origin": "null", "Sec-Fetch-Site": "same-origin"}, 303),
                ("sin cabeceras (curl)", {}, 303),
                ("Sec-Fetch-Site: none", {"Sec-Fetch-Site": "none"}, 303),
                ("Sec-Fetch-Site: cross-site", {"Sec-Fetch-Site": "cross-site"}, 403),
                ("Sec-Fetch-Site: same-site", {"Sec-Fetch-Site": "same-site"}, 403),
                ("Origin de otro host", {"Origin": "https://evil.example"}, 403),
            ]
            for desc, headers, want in cases:
                async with http.post(f"{base}/d/{t}", headers=headers, allow_redirects=False) as r:
                    check(f"POST {desc}: {want}", r.status == want, r.status)

            print("== 11. paquete 'Ambos': runner + servicio GitLab real + bot + gateway (enlace de 2 archivos)")
            preq = PackageRequest(user_id=506679030, project="proj", ref="main", sha=gl_fake_mod.SHA[:8], fmt="zip",
                                  what="both", ttl=3600, max_downloads=1, db=("gate", "fakemysql", "appdb"))
            pk = await pkg_jobs.run(preq, "tg-pkg-abcd1234")
            check("el paquete termina 'ok' (código + BD)", pk.kind == "ok", f"{pk.kind} {[(p.kind, p.ok, p.code) for p in pk.parts]}")
            files = pk.link["files"]
            check("un solo enlace con 2 archivos: código y BD", [f["kind"] for f in files] == ["code", "db"], files)
            check("el zip vive en _gitlab/ y el dump en su servidor", files[0]["relpath"].startswith("_gitlab/") and files[1]["relpath"].startswith("gate/"))
            pcommit = pk.parts[0].result["commit"] if pk.parts[0].kind == "code" else None
            check("el servicio fijó el commit confirmado", pcommit == gl_fake_mod.SHA)
            ptoken = re.search(r"/d/([^/?#]+)$", pk.link["url"]).group(1)
            zip_path = os.path.join(tmp, "dumps", files[0]["relpath"])
            db_path = os.path.join(tmp, "dumps", files[1]["relpath"])
            check("sha256 del zip y del dump coinciden con lo registrado", sha(zip_path) == files[0]["sha256"] and sha(db_path) == files[1]["sha256"])
            r = await http.get(f"{base}/d/{ptoken}")
            body = await r.text()
            check("la página lista los 2 archivos con un botón cada uno", r.status == 200 and body.count("<button") == 2 and files[0]["filename"] in body and files[1]["filename"] in body)
            check("el título no revela nombres", files[0]["filename"] not in body.split("</title>")[0])
            for path, want, desc in ((f"/d/{ptoken}", 404, "POST sin índice en enlace de 2 archivos: 404"),
                                     (f"/d/{ptoken}/2", 404, "POST con índice inexistente (2): 404"),
                                     (f"/d/{ptoken}/9", 404, "POST con índice inexistente (9): 404")):
                async with http.post(base + path, allow_redirects=False) as r:
                    check(desc, r.status == want, r.status)
            sess = []
            for n in (0, 1):
                async with http.post(f"{base}/d/{ptoken}/{n}", headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"}, allow_redirects=False) as r:
                    check(f"POST del archivo {n}: 303", r.status == 303, r.status)
                    sess.append(base + r.headers["Location"])

            async def fetch_all(url):
                async with http.get(url) as r:
                    return r.status, await r.read()

            (s0, b0), (s1, b1) = await asyncio.gather(fetch_all(sess[0]), fetch_all(sess[1]))
            check("descarga paralela de los 2 archivos: 200 y 200", (s0, s1) == (200, 200))
            check("sha256 de lo descargado == lo registrado (zip y dump)", hashlib.sha256(b0).hexdigest() == files[0]["sha256"] and hashlib.sha256(b1).hexdigest() == files[1]["sha256"])
            prec = [l for l in await gateway.list_links("all") if l["id"] == pk.link["id"]][0]
            check("conteo por archivo [1, 1] y enlace agotado", [f["downloads_completed"] for f in prec["files"]] == [1, 1] and prec["state"] == "exhausted", prec)

            print("== 12. entrega parcial: GitLab rechaza el token, la BD sí sale")

            def deny_archive(h, rq):
                if "/archive." in rq["raw_path"]:
                    h.reply(403, b'{"message":"403 Forbidden"}')
                    return True
                return None

            fakegl.responder = deny_archive
            pp = await pkg_jobs.run(PackageRequest(user_id=506679030, project="proj", ref="main", sha=gl_fake_mod.SHA[:8], fmt="zip",
                                                   what="both", ttl=3600, max_downloads=1, db=("gate", "fakemysql", "appdb")), "tg-part-abcd1234")
            fakegl.responder = None
            check("resultado 'partial' con el aviso del fallo de código", pp.kind == "partial" and [p.code for p in pp.bad] == ["E_FORBIDDEN"], f"{pp.kind} {[(p.kind, p.code) for p in pp.parts]}")
            check("el enlace conserva solo la BD", [f["kind"] for f in pp.link["files"]] == ["db"])
            async with http.get(pp.link["url"]) as r:
                check("el enlace parcial se sirve (200)", r.status == 200)
            await gateway.revoke(pp.link["id"])
            fakegl.responder = deny_archive
            pf = await pkg_jobs.run(PackageRequest(user_id=506679030, project="proj", ref="main", sha="", fmt="zip",
                                                   what="code", ttl=3600, max_downloads=1), "tg-fail-abcd1234")
            fakegl.responder = None
            check("solo código y falla: 'failed' sin enlace", pf.kind == "failed" and pf.link is None, pf.kind)
            check("GitLab recibió únicamente peticiones GET (nunca escritura, borrado ni creación)", set(fakegl.methods()) == {"GET"}, set(fakegl.methods()))
            check("antes de operar se comprobaron los alcances del token", len(fakegl.scope_requests) >= 3)

            if FULL:
                print("== 13. borrado por archivo en un enlace de 2 archivos")
                pk2 = await pkg_jobs.run(PackageRequest(user_id=506679030, project="proj", ref="main", sha="", fmt="zip",
                                                        what="both", ttl=3600, max_downloads=1, db=("gate", "fakemysql", "appdb")), "tg-del-abcd1234")
                f2 = pk2.link["files"]
                zp2, dp2 = (os.path.join(tmp, "dumps", f["relpath"]) for f in f2)
                t2 = re.search(r"/d/([^/?#]+)$", pk2.link["url"]).group(1)
                async with http.post(f"{base}/d/{t2}/1", allow_redirects=False) as r:
                    dl_url = base + r.headers["Location"]
                async with http.get(dl_url) as r:
                    await r.read()
                check("solo se descargó el dump: el zip sigue disponible", os.path.exists(zp2))
                check("el dump agotado se borra tras la gracia", await gone(dp2, 9))
                check("mientras el zip sigue en el enlace vigente, no se borra", os.path.exists(zp2))
                await gateway.revoke(pk2.link["id"])
                check("revocar el enlace libera el zip restante", await gone(zp2, 4))

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
    finally:
        try:
            fakegl.close()
        except NameError:
            pass
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
