# =============================================================================
# services/gateway/evals/e2e_gateway.py — Eval end-to-end: gateway real, rangos paralelos y memoria
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
"""Levanta `python -m gateway` con puertos efímeros y un dump de ~30 MB.

Umbrales de aprobación (exit 0 solo si todos pasan):
  - sha256 del reensamblado (4 rangos en paralelo, orden invertido) == original
  - downloads_completed == 1 y un solo download_completed en la auditoría
  - crecimiento del pico RSS (VmHWM) durante el streaming <= 15 MB (el archivo pesa ~30 MB)
  - SIGTERM termina el proceso con código 0 en <= 8 s
  - un lector lento no acapara más de 2 streams de un mismo enlace (el tercero recibe 503), otro enlace
    sigue sirviendo íntegro, y revocar corta los streams lentos y luego borra el archivo
  - permisos: data dir 0700 y gateway.db/-wal/-shm/audit.jsonl en 0600
  - ciclo de vida (gracia y limpieza de 1-2 s por entorno): el dump se borra tras revocar, tras expirar y
    tras agotar (solo después de la gracia), el dump grande se borra tras su descarga, un enlace activo
    y `keep_file=1` conservan el archivo, y la auditoría registra `file_deleted` con su `reason`
"""

import asyncio
import hashlib
import json
import os
import signal
import socket
import subprocess
import sqlite3
import sys
import tempfile
import time

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIZE = 30 * 1024 * 1024 + 12345
PARTS = 4
MAX_RSS_GROWTH_MB = 15
API_TOKEN = "token-eval-inventado-0123456789"
GRACE = 2
CLEANUP = 1
WAIT_GONE_S = 8


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def proc_status_mb(pid, field):
    with open(f"/proc/{pid}/status", encoding="ascii") as fh:
        for line in fh:
            if line.startswith(field + ":"):
                return int(line.split()[1]) / 1024
    return 0.0


def reset_peak(pid):
    """Reinicia VmHWM del proceso (clear_refs=5) para medir el pico solo del streaming."""
    with open(f"/proc/{pid}/clear_refs", "w", encoding="ascii") as fh:
        fh.write("5")


async def wait_ready(url, headers, timeout=15):
    deadline = time.monotonic() + timeout
    async with aiohttp.ClientSession() as http:
        while time.monotonic() < deadline:
            try:
                async with http.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return True
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.1)
    return False


def small_dump(dumps, name):
    path = os.path.join(dumps, "srv", name)
    with open(path, "wb") as fh:
        fh.write(os.urandom(4096))
    os.chmod(path, 0o600)
    return path


async def wait_gone(path, timeout=WAIT_GONE_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not os.path.exists(path):
            return True
        await asyncio.sleep(0.1)
    return not os.path.exists(path)


async def lifecycle(http, pub, internal, auth, dumps, data, big_path):
    """Borrado real de archivos tras revocar, expirar y agotar, con gracia y limpieza pequeñas."""
    out = {}
    api = f"http://127.0.0.1:{internal}"

    async def new_link(path, **extra):
        body = {"relpath": "srv/" + os.path.basename(path), "created_by": "eval", "job_id": "eval-lc", **extra}
        async with http.post(f"{api}/v1/links", headers=auth, json=body) as resp:
            return await resp.json()

    async def post(link):
        url = f"http://127.0.0.1:{pub}/d/" + link["url"].rsplit("/d/", 1)[1]
        async with http.post(url, allow_redirects=False) as resp:
            return f"http://127.0.0.1:{pub}{resp.headers['Location']}"

    activo = small_dump(dumps, "activo.sql.gz")
    await new_link(activo, ttl_seconds=3600)

    revocado = small_dump(dumps, "revocado.sql.gz")
    link = await new_link(revocado)
    await post(link)
    async with http.delete(f"{api}/v1/links/{link['id']}", headers=auth) as resp:
        out["revoke_status"] = resp.status
    out["revoke_deleted"] = await wait_gone(revocado, 2)

    conservado = small_dump(dumps, "conservado.sql.gz")
    link = await new_link(conservado)
    async with http.delete(f"{api}/v1/links/{link['id']}?keep_file=1", headers=auth) as resp:
        out["keep_status"] = resp.status

    expirado = small_dump(dumps, "expirado.sql.gz")
    link = await new_link(expirado, ttl_seconds=60)
    con = sqlite3.connect(os.path.join(data, "gateway.db"), timeout=10)
    con.execute("UPDATE links SET expires_at = expires_at - 7200 WHERE id = ?", (link["id"],))
    con.commit()
    con.close()
    out["expire_deleted"] = await wait_gone(expirado)

    agotado = small_dump(dumps, "agotado.sql.gz")
    link = await new_link(agotado)
    session = await post(link)
    async with http.get(session) as resp:
        await resp.read()
    out["exhaust_kept_during_grace"] = os.path.exists(agotado)
    started = time.monotonic()
    out["exhaust_deleted"] = await wait_gone(agotado)
    out["exhaust_after_s"] = round(time.monotonic() - started, 1)
    async with http.get(session, headers={"Range": "bytes=0-9"}) as resp:
        out["session_after_delete_status"] = resp.status

    out["big_deleted"] = await wait_gone(big_path, WAIT_GONE_S + GRACE)
    out["active_kept"] = os.path.exists(activo)
    out["keep_kept"] = os.path.exists(conservado)
    return out


def file_modes(data):
    modes = {"data_dir": os.stat(data).st_mode & 0o777}
    for name in os.listdir(data):
        modes[name] = os.stat(os.path.join(data, name)).st_mode & 0o777
    return modes


def spawn(tmp, name):
    """Gateway aparte con la configuración por defecto de topes (por enlace 2, global 4)."""
    base = os.path.join(tmp, name)
    dumps, data = os.path.join(base, "dumps"), os.path.join(base, "data")
    os.makedirs(os.path.join(dumps, "srv"), mode=0o700)
    token_file = os.path.join(base, "api.token")
    with open(token_file, "w", encoding="utf-8") as fh:
        fh.write(API_TOKEN)
    pub, internal = free_port(), free_port()
    env = {
        "PATH": os.environ.get("PATH", ""), "GATEWAY_DUMPS_ROOT": dumps, "GATEWAY_DATA_DIR": data,
        "GATEWAY_PUBLIC_BASE_URL": f"http://127.0.0.1:{pub}", "GATEWAY_API_TOKEN_FILE": token_file,
        "GATEWAY_PUBLIC_PORT": str(pub), "GATEWAY_INTERNAL_PORT": str(internal),
        "GATEWAY_BIND_HOST": "127.0.0.1", "GATEWAY_CLEANUP_INTERVAL_SECONDS": str(CLEANUP),
    }
    proc = subprocess.Popen([sys.executable, "-m", "gateway"], cwd=ROOT, env=env, stderr=subprocess.DEVNULL)
    return proc, dumps, pub, internal


async def slow_reader_case(tmp):
    """Un lector lento no acapara más de 2 streams de un enlace; otro enlace sigue y revocar corta."""
    proc, dumps, pub, internal = spawn(tmp, "lento")
    auth = {"Authorization": f"Bearer {API_TOKEN}"}
    out = {}
    try:
        if not await wait_ready(f"http://127.0.0.1:{internal}/v1/health", auth):
            raise RuntimeError("el gateway lento no arrancó")
        big = os.path.join(dumps, "srv", "lento.sql.gz")
        with open(big, "wb") as fh:
            fh.truncate(256 * 1024 * 1024)
        os.chmod(big, 0o600)
        other = small_dump(dumps, "otro.sql.gz")
        payload = open(other, "rb").read()
        async with aiohttp.ClientSession() as http:
            async def link(name):
                async with http.post(f"http://127.0.0.1:{internal}/v1/links", headers=auth, json={
                        "relpath": f"srv/{name}", "created_by": "eval", "job_id": "eval-slow",
                        "max_downloads": 5}) as resp:
                    return await resp.json()

            async def session(item):
                path = "/d/" + item["url"].rsplit("/d/", 1)[1]
                async with http.post(f"http://127.0.0.1:{pub}{path}", allow_redirects=False) as resp:
                    return f"http://127.0.0.1:{pub}{resp.headers['Location']}"

            a, b = await link("lento.sql.gz"), await link("otro.sql.gz")
            loc_a, loc_b = await session(a), await session(b)
            readers = []
            for _ in range(2):
                resp = await http.get(loc_a)
                await resp.content.readexactly(1)
                readers.append(resp)
            async with http.get(loc_a) as resp:
                out["third_status"] = resp.status
                out["third_retry_after"] = "Retry-After" in resp.headers
            async with http.get(loc_b) as resp:
                out["other_status"] = resp.status
                out["other_ok"] = await resp.read() == payload
            async with http.delete(f"http://127.0.0.1:{internal}/v1/links/{a['id']}", headers=auth) as resp:
                out["revoke_status"] = resp.status
            cut = 0
            for resp in readers:
                got = 1
                try:
                    while True:
                        block = await asyncio.wait_for(resp.content.read(1 << 20), 5)
                        if not block:
                            break
                        got += len(block)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
                cut += got < 256 * 1024 * 1024
                resp.close()
            out["readers_cut"] = cut
            out["slow_file_deleted"] = await wait_gone(big)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
    return out


async def run(tmp):
    dumps, data = os.path.join(tmp, "dumps"), os.path.join(tmp, "data")
    os.makedirs(os.path.join(dumps, "srv"), mode=0o700)
    payload_path = os.path.join(dumps, "srv", "grande.sql.gz")
    digest = hashlib.sha256()
    with open(payload_path, "wb") as fh:
        remaining = SIZE
        while remaining:
            block = os.urandom(min(remaining, 1024 * 1024))
            fh.write(block)
            digest.update(block)
            remaining -= len(block)
    os.chmod(payload_path, 0o600)
    token_file = os.path.join(tmp, "api.token")
    with open(token_file, "w", encoding="utf-8") as fh:
        fh.write(API_TOKEN)
    pub, internal = free_port(), free_port()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GATEWAY_DUMPS_ROOT": dumps, "GATEWAY_DATA_DIR": data,
        "GATEWAY_PUBLIC_BASE_URL": f"http://127.0.0.1:{pub}", "GATEWAY_API_TOKEN_FILE": token_file,
        "GATEWAY_PUBLIC_PORT": str(pub), "GATEWAY_INTERNAL_PORT": str(internal),
        "GATEWAY_BIND_HOST": "127.0.0.1",
        "GATEWAY_POST_DOWNLOAD_GRACE_SECONDS": str(GRACE), "GATEWAY_CLEANUP_INTERVAL_SECONDS": str(CLEANUP),
        "GATEWAY_MAX_STREAMS_PER_LINK": str(PARTS),
    }
    proc = subprocess.Popen([sys.executable, "-m", "gateway"], cwd=ROOT, env=env,
                            stderr=subprocess.PIPE, text=True)
    results = {}
    auth = {"Authorization": f"Bearer {API_TOKEN}"}
    try:
        if not await wait_ready(f"http://127.0.0.1:{internal}/v1/health", auth):
            raise RuntimeError("el gateway no arrancó")
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"http://127.0.0.1:{internal}/v1/links", headers=auth,
                json={"relpath": "srv/grande.sql.gz", "created_by": "eval", "job_id": "eval-1",
                      "sha256": digest.hexdigest()},
            ) as resp:
                link = await resp.json()
            path = link["url"].split(f":{pub}", 1)[1]
            async with http.post(f"http://127.0.0.1:{pub}{path}", allow_redirects=False) as resp:
                location = resp.headers["Location"]

            await asyncio.sleep(0.2)
            baseline = proc_status_mb(proc.pid, "VmRSS")
            reset_peak(proc.pid)

            step = SIZE // PARTS
            bounds = [(i * step, SIZE - 1 if i == PARTS - 1 else (i + 1) * step - 1) for i in range(PARTS)]

            async def part(lo, hi):
                async with http.get(f"http://127.0.0.1:{pub}{location}",
                                    headers={"Range": f"bytes={lo}-{hi}"}) as resp:
                    assert resp.status == 206, resp.status
                    body = bytearray()
                    while True:
                        block = await resp.content.read(32 * 1024)
                        if not block:
                            return lo, bytes(body)
                        body += block
                        await asyncio.sleep(0.002)

            started = time.monotonic()
            chunks = await asyncio.gather(*[part(lo, hi) for lo, hi in reversed(bounds)])
            elapsed = time.monotonic() - started
            peak = proc_status_mb(proc.pid, "VmHWM")

            joined = hashlib.sha256()
            for _, body in sorted(chunks):
                joined.update(body)
            async with http.get(f"http://127.0.0.1:{internal}/v1/links/{link['id']}", headers=auth) as resp:
                state = await resp.json()
            results["sha256_ok"] = joined.hexdigest() == digest.hexdigest()
            results["downloads_completed"] = state["downloads_completed"]
            results["rss_growth_mb"] = round(peak - baseline, 1)
            results["duration_s"] = round(elapsed, 2)
            results["throughput_mb_s"] = round(SIZE / 1048576 / elapsed, 1)
            results["lifecycle"] = await lifecycle(http, pub, internal, auth, dumps, data, payload_path)
            results["modes"] = file_modes(data)
    finally:
        proc.send_signal(signal.SIGTERM)
        started = time.monotonic()
        try:
            code = proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = -9
        results["shutdown_code"] = code
        results["shutdown_s"] = round(time.monotonic() - started, 2)
        stderr = proc.stderr.read()
        proc.stderr.close()
    results["slow"] = await slow_reader_case(tmp)
    with open(os.path.join(data, "audit.jsonl"), encoding="utf-8") as fh:
        entries = [json.loads(line) for line in fh]
    events = [entry["event"] for entry in entries]
    results["audit_completed"] = sum(
        1 for e in entries if e["event"] == "download_completed" and e["bytes"] == SIZE)
    results["delete_reasons"] = sorted(
        (os.path.basename(e["relpath"]), e["reason"]) for e in entries if e["event"] == "file_deleted")
    results["token_leak"] = link["url"].rsplit("/", 1)[1] in stderr or link["url"].rsplit("/", 1)[1] in open(
        os.path.join(data, "audit.jsonl"), encoding="utf-8").read()
    return results


def main():
    with tempfile.TemporaryDirectory() as tmp:
        res = asyncio.run(run(tmp))
    checks = [
        ("sha256 del reensamblado coincide", res["sha256_ok"]),
        ("downloads_completed == 1", res["downloads_completed"] == 1),
        ("un solo download_completed en auditoría", res["audit_completed"] == 1),
        (f"crecimiento RSS {res['rss_growth_mb']} MB <= {MAX_RSS_GROWTH_MB} MB",
         res["rss_growth_mb"] <= MAX_RSS_GROWTH_MB),
        (f"SIGTERM limpio (código {res['shutdown_code']}, {res['shutdown_s']} s)",
         res["shutdown_code"] == 0 and res["shutdown_s"] <= 8),
        ("el token no aparece en logs ni auditoría", not res["token_leak"]),
    ]
    sl, modes = res["slow"], res["modes"]
    checks += [
        ("lector lento: el tercer stream del mismo enlace recibe 503 con Retry-After",
         sl["third_status"] == 503 and sl["third_retry_after"]),
        ("lector lento: otro enlace sigue sirviendo íntegro", sl["other_status"] == 200 and sl["other_ok"]),
        ("revocar corta los dos streams lentos", sl["revoke_status"] == 204 and sl["readers_cut"] == 2),
        ("revocar borra luego el archivo del lector lento", sl["slow_file_deleted"]),
        ("data dir 0700", modes["data_dir"] == 0o700),
        ("gateway.db, -wal, -shm y audit.jsonl en 0600",
         all(v == 0o600 for k, v in modes.items() if k != "data_dir") and "gateway.db-wal" in modes),
    ]
    lc = res["lifecycle"]
    razones = dict(res["delete_reasons"])
    checks += [
        ("revocar: 204 y el archivo se borra de inmediato", lc["revoke_status"] == 204 and lc["revoke_deleted"]),
        ("revocar con keep_file=1 conserva el archivo", lc["keep_status"] == 204 and lc["keep_kept"]),
        ("expirar: el archivo se borra en la siguiente pasada", lc["expire_deleted"]),
        ("agotar: el archivo sigue durante la gracia", lc["exhaust_kept_during_grace"]),
        (f"agotar: se borra tras la gracia ({lc['exhaust_after_s']} s)", lc["exhaust_deleted"]),
        ("tras borrar, la sesión ya no sirve (404)", lc["session_after_delete_status"] == 404),
        ("el dump grande se borra tras su descarga", lc["big_deleted"]),
        ("un enlace activo conserva su archivo", lc["active_kept"]),
        ("file_deleted con reason correcto",
         razones.get("revocado.sql.gz") == "link_revoked" and razones.get("expirado.sql.gz") == "link_expired"
         and razones.get("agotado.sql.gz") == "link_exhausted"
         and razones.get("grande.sql.gz") == "link_exhausted"
         and "activo.sql.gz" not in razones and "conservado.sql.gz" not in razones),
    ]
    print(f"Archivo: {SIZE / 1048576:.1f} MiB, {PARTS} rangos en paralelo (orden invertido), "
          f"{res['throughput_mb_s']} MB/s en {res['duration_s']} s (cliente lento)")
    for label, ok in checks:
        print(f"  [{'OK' if ok else 'FALLA'}] {label}")
    passed = all(ok for _, ok in checks)
    print("RESULTADO:", "APROBADO" if passed else "REPROBADO")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
