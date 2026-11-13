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
"""

import asyncio
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIZE = 30 * 1024 * 1024 + 12345
PARTS = 4
MAX_RSS_GROWTH_MB = 15
API_TOKEN = "token-eval-inventado-0123456789"


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
    with open(os.path.join(data, "audit.jsonl"), encoding="utf-8") as fh:
        events = [json.loads(line)["event"] for line in fh]
    results["audit_completed"] = events.count("download_completed")
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
    print(f"Archivo: {SIZE / 1048576:.1f} MiB, {PARTS} rangos en paralelo (orden invertido), "
          f"{res['throughput_mb_s']} MB/s en {res['duration_s']} s (cliente lento)")
    for label, ok in checks:
        print(f"  [{'OK' if ok else 'FALLA'}] {label}")
    passed = all(ok for _, ok in checks)
    print("RESULTADO:", "APROBADO" if passed else "REPROBADO")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
