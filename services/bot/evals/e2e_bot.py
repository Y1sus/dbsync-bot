# =============================================================================
# services/bot/evals/e2e_bot.py — Eval end a end: bot real contra Telegram, gateway y runner falsos
# Uso: python evals/e2e_bot.py   (exit 0 si pasan todas las comprobaciones, 1 si falla alguna)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import json
import os
import signal
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from aiohttp.test_utils import TestServer  # noqa: E402

from fake_telegram import FakeTelegram  # noqa: E402
from tests.fakes.gateway import TOKEN as GW_TOKEN, FakeGateway  # noqa: E402

BOT_TOKEN = "123456:TEST"
OWNER, STRANGER = 42, 777
RUNNER = os.path.join(ROOT, "tests", "fakes", "fake_runner.sh")
PASS_THRESHOLD = 1.0
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'OK' if ok else 'FALLA'}] {name}" + (f"  -> {detail}" if detail and not ok else ""), flush=True)


async def until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.03)
    return False


def buttons(msg):
    markup = (msg or {}).get("markup") or {}
    return [b for row in markup.get("inline_keyboard", []) for b in row]


def find_button(msg, pred):
    return next((b for b in buttons(msg) if pred(b)), None)


class Harness:
    def __init__(self, tg, gw, tmp):
        self.tg, self.gw, self.tmp = tg, gw, tmp
        self.mode_file = os.path.join(tmp, "mode")

    def set_runner_mode(self, mode):
        with open(self.mode_file, "w") as fh:
            fh.write(mode)

    def latest(self, chat_id=OWNER):
        keys = [k for k in self.tg.messages if k[0] == chat_id]
        return self.tg.messages[max(keys, key=lambda k: k[1])] if keys else None

    def n_out(self):
        return sum(len(self.tg.calls_of(m)) for m in ("sendMessage", "editMessageText", "answerCallbackQuery", "deleteMessage"))

    async def command(self, text, user=OWNER, **kw):
        before = {k for k in self.tg.messages}
        await self.tg.push_text(user, text, **kw)
        ok = await until(lambda: {k for k in self.tg.messages} - before)
        new = {k for k in self.tg.messages} - before
        return self.tg.messages[max(new, key=lambda k: k[1])] if ok else None

    async def click(self, msg, button, user=OWNER, expect_change=True):
        key = (msg["chat"]["id"], msg["message_id"])
        old = self.tg.messages[key]["text"], json.dumps(self.tg.messages[key]["markup"])
        await self.tg.push_callback(user, button["callback_data"], key[0], key[1])
        if not expect_change:
            return None
        await until(lambda: (self.tg.messages[key]["text"], json.dumps(self.tg.messages[key]["markup"])) != old)
        return self.tg.messages[key]


async def scenarios(h):
    tg, gw = h.tg, h.gw

    print("Arranque")
    check("getMe llamado", len(tg.calls_of("getMe")) >= 1)
    check("deleteWebhook con drop_pending_updates", any(str(c["params"].get("drop_pending_updates")).lower() == "true" for c in tg.calls_of("deleteWebhook")), str(tg.calls_of("deleteWebhook")))
    check("setMyCommands registrado", len(tg.calls_of("setMyCommands")) >= 1)

    print("Ayuda y servidores")
    m = await h.command("/start")
    check("/start responde con ayuda", m and "/dump" in m["text"])
    m = await h.command("/servers")
    check("/servers lista alpha y beta", m and "alpha" in m["text"] and "beta" in m["text"])

    print("Asistente /dump por botones")
    m = await h.command("/dump")
    check("asistente ofrece servidores como botones", m and find_button(m, lambda b: b["text"] == "alpha"))
    m = await h.click(m, find_button(m, lambda b: b["text"] == "alpha"))
    check("paso contenedor", m and "contenedor" in m["text"] and find_button(m, lambda b: b["text"].startswith("mysql8")))
    m = await h.click(m, find_button(m, lambda b: b["text"].startswith("mysql8")))
    check("paso base de datos", m and find_button(m, lambda b: b["text"] == "appdb"))
    check("HTML hostil como texto de botón, no como marcado", find_button(m, lambda b: b["text"] == "<b>evil&db</b>"))
    m = await h.click(m, find_button(m, lambda b: b["text"] == "appdb"))
    check("confirmación muestra TTL y descargas por defecto", m and "1 h" in m["text"] and "Descargas máximas: <b>1</b>" in m["text"], m and m["text"])
    m = await h.click(m, find_button(m, lambda b: b["text"] == "4 h"))
    m = await h.click(m, find_button(m, lambda b: b["text"] == "3"))
    check("botones cambian TTL y descargas", m and "4 h" in m["text"] and "<b>3</b>" in m["text"])
    n_posts = len([r for r in gw.requests if r["method"] == "POST"])
    await h.click(m, find_button(m, lambda b: b["text"] == "Generar dump"), expect_change=False)
    key = (OWNER, m["message_id"])
    ok = await until(lambda: "https://dumps.test/d/" in tg.messages[key]["text"])
    final = tg.messages[key]
    check("el mensaje se edita con la URL del enlace", ok, final["text"])
    check("resultado incluye tamaño, sha256 abreviado, duración y expiración", all(s in final["text"] for s in ("1.2 MB", "aaaaaaaaaaaa…", "3 s", "Expira:")), final["text"])
    lp = json.loads(final["params"].get("link_preview_options", "{}"))
    check("vista previa deshabilitada en el mensaje con la URL", lp.get("is_disabled") is True, str(lp))
    posts = [r for r in gw.requests if r["method"] == "POST"][n_posts:]
    check("gateway recibió ttl=14400 max=3 created_by=42", posts and posts[0]["body"]["ttl_seconds"] == 14400 and posts[0]["body"]["max_downloads"] == 3 and posts[0]["body"]["created_by"] == "42", str(posts))
    check("job_id válido en el enlace", posts and __import__("re").fullmatch(r"tg[0-9]+-[0-9a-f]{8}", posts[0]["body"]["job_id"]))

    print("Asistente: HTML hostil en la confirmación y cancelar")
    m = await h.command("/dump")
    m = await h.click(m, find_button(m, lambda b: b["text"] == "alpha"))
    m = await h.click(m, find_button(m, lambda b: b["text"].startswith("mysql8")))
    m = await h.click(m, find_button(m, lambda b: b["text"] == "<b>evil&db</b>"))
    check("nombre hostil escapado en el mensaje", m and "&lt;b&gt;evil&amp;db&lt;/b&gt;" in m["text"], m and m["text"])
    m = await h.click(m, find_button(m, lambda b: b["text"] == "Cancelar"))
    check("cancelar cierra el asistente", m and "cancelado" in m["text"])

    print("Forma directa")
    n_posts = len([r for r in gw.requests if r["method"] == "POST"])
    m = await h.command("/dump a b c ttl=2h max=3")
    check("responde 'Generando dump'", m is not None)
    key = (OWNER, m["message_id"])
    ok = await until(lambda: "https://dumps.test/d/" in tg.messages[key]["text"])
    check("la forma directa termina con URL", ok, tg.messages[key]["text"])
    posts = [r for r in gw.requests if r["method"] == "POST"][n_posts:]
    check("ttl=2h max=3 llegan al gateway", posts and posts[0]["body"]["ttl_seconds"] == 7200 and posts[0]["body"]["max_downloads"] == 3 and posts[0]["body"]["relpath"] == "a/f.sql.gz", str(posts))
    m = await h.command("/dump a b c ttl=5s")
    check("duración inválida da error de uso sin ejecutar", m and "60s y 7d" in m["text"])

    print("/status, /links y revocar")
    m = await h.command("/status")
    check("/status libre", m and "Libre" in m["text"])
    m = await h.command("/links")
    revoke_btns = buttons(m)
    check("/links lista enlaces con botón de revocar", len(revoke_btns) >= 2 and "Revocar" in revoke_btns[0]["text"], m and m["text"])
    link_id = revoke_btns[0]["callback_data"].split("|")[1]
    m2 = await h.click(m, revoke_btns[0])
    check("revocar actualiza la lista", m2 and link_id not in m2["text"], m2 and m2["text"])
    check("gateway marcó el enlace como revocado", gw.gw.links[link_id]["state"] == "revoked")
    other = revoke_btns[1]["callback_data"].split("|")[1]
    m3 = await h.command(f"/revoke {other}")
    check("/revoke <id> revoca", m3 and "revocado" in m3["text"] and gw.gw.links[other]["state"] == "revoked")
    m3 = await h.command("/revoke 000000000000")
    check("/revoke de id inexistente informa", m3 and "no existe" in m3["text"])
    m3 = await h.command("/revoke zzz")
    check("/revoke con id inválido da uso", m3 and "Uso" in m3["text"])

    print("Errores sin traceback")
    h.set_runner_mode("err")
    m = await h.command("/dump a b c")
    key = (OWNER, m["message_id"])
    ok = await until(lambda: "E_SSH" in tg.messages[key]["text"])
    t = tg.messages[key]["text"]
    check("error del runner muestra código E_SSH y mensaje", ok and "SSH" in t, t)
    check("stderr en bloque <pre>", "<pre>" in t and "Connection refused" in t, t)
    check("sin traceback ni rutas internas", "Traceback" not in t and "File \"" not in t)
    h.set_runner_mode("garbage")
    m = await h.command("/dump a b c")
    key = (OWNER, m["message_id"])
    ok = await until(lambda: "E_BAD_OUTPUT" in tg.messages[key]["text"])
    check("salida ilegible del runner da error genérico claro", ok, tg.messages[key]["text"])
    h.set_runner_mode("ok")
    gw.gw.mode = "error500"
    m = await h.command("/dump a b c")
    key = (OWNER, m["message_id"])
    ok = await until(lambda: "no se pudo crear el enlace" in tg.messages[key]["text"])
    check("si el enlace falla, dice que el dump existe", ok and "dump se generó" in tg.messages[key]["text"], tg.messages[key]["text"])
    gw.gw.mode = "ok"
    m = await h.command("/links")
    check("el bot sigue vivo tras los errores", m is not None)

    print("Callback manipulado")
    m = await h.command("/dump")
    before = len(tg.calls_of("answerCallbackQuery"))
    await tg.push_callback(OWNER, "w|zzzzzzzz|s|1", OWNER, m["message_id"])
    await tg.push_callback(OWNER, "basura", OWNER, m["message_id"])
    await tg.push_callback(OWNER, "w|00000000|s|1", OWNER, m["message_id"])
    ok = await until(lambda: len(tg.calls_of("answerCallbackQuery")) >= before + 3)
    check("callbacks inválidos o expirados se contestan sin romper", ok)

    print("Autorización: usuario ajeno no recibe nada")
    m = await h.command("/dump")
    wizard_msg = dict(m)
    key = (OWNER, m["message_id"])
    snap_text = tg.messages[key]["text"]
    out_before = h.n_out()
    await tg.push_text(STRANGER, "/start")
    await tg.push_text(STRANGER, "/dump a b c")
    await tg.push_text(STRANGER, "/links")
    await tg.push_text(STRANGER, "hola")
    await tg.push_text(STRANGER, "/start", edited=True)
    await tg.push_text(OWNER, "/start", chat_id=-100123, chat_type="group")
    await tg.push_text(OWNER, "/start", edited=True)
    await tg.push_callback(STRANGER, find_button(wizard_msg, lambda b: b["text"] == "alpha")["callback_data"], OWNER, m["message_id"])
    sentinel = await h.command("/status")
    check("el centinela del dueño sí responde", sentinel is not None)
    check("ninguna llamada de salida por updates ajenos (solo la del centinela)", h.n_out() == out_before + 1, f"{h.n_out() - out_before} llamadas")
    check("nada enviado a chats ajenos", all(k[0] == OWNER for k in tg.messages))
    check("el asistente del dueño no avanzó por el callback ajeno", tg.messages[key]["text"] == snap_text)
    check("no hubo trabajo del runner/gateway por ajenos", not any(r["body"] and r["body"].get("created_by") == str(STRANGER) for r in gw.requests))


async def startup_failures():
    print("Arranque con configuración incompleta")
    tmp = tempfile.mkdtemp(prefix="bot-eval-cfg-")
    base = {k: v for k, v in os.environ.items() if not k.startswith("BOT_")}
    for label, extra, needle in (
        ("sin variables", {}, "BOT_GATEWAY_URL"),
        ("sin lista de usuarios", {"BOT_GATEWAY_URL": "http://x:1", "BOT_TOKEN_FILE": "/nope", "BOT_GATEWAY_TOKEN_FILE": "/nope"}, "BOT_ALLOWED_USER_IDS"),
        ("archivo de token inexistente", {"BOT_GATEWAY_URL": "http://x:1", "BOT_TOKEN_FILE": "/nope", "BOT_GATEWAY_TOKEN_FILE": "/nope",
                                          "BOT_ALLOWED_USER_IDS": "1"}, "BOT_TOKEN_FILE"),
    ):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "bot", cwd=ROOT, env={**base, "BOT_DATA_DIR": tmp, **extra},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out = (await asyncio.wait_for(proc.communicate(), 20))[0].decode()
        check(f"aborta con error claro ({label})", proc.returncode == 2 and needle in out and "Traceback" not in out, f"exit={proc.returncode} {out[-200:]}")


async def crash_redaction():
    print("Caída tras leer los tokens (Telegram inalcanzable)")
    tmp = tempfile.mkdtemp(prefix="bot-eval-crash-")
    for name, content in (("bot_token", BOT_TOKEN), ("gw_token", GW_TOKEN)):
        with open(os.path.join(tmp, name), "w") as fh:
            fh.write(content + "\n")
    env = {**os.environ, "BOT_TOKEN_FILE": os.path.join(tmp, "bot_token"), "BOT_GATEWAY_TOKEN_FILE": os.path.join(tmp, "gw_token"),
           "BOT_ALLOWED_USER_IDS": "42", "BOT_GATEWAY_URL": "http://127.0.0.1:1", "BOT_DATA_DIR": tmp,
           "BOT_TELEGRAM_API_BASE": "http://127.0.0.1:1"}
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "bot", cwd=ROOT, env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out = (await asyncio.wait_for(proc.communicate(), 60))[0].decode()
    check("sale con código distinto de 0", proc.returncode not in (0, None), f"exit={proc.returncode}")
    check("el traceback final no filtra el token", BOT_TOKEN not in out and "TEST" not in out and GW_TOKEN not in out, out[-500:])
    check("el error queda registrado", "error" in out.lower() or "ERROR" in out, out[-300:])


async def sigterm_during_dump(h, proc, pid_file):
    print("SIGTERM con un dump en curso")
    tg = h.tg
    h.set_runner_mode("hang")
    m = await h.command("/dump a b c")
    key = (OWNER, m["message_id"])
    started = await until(lambda: os.path.exists(pid_file) and open(pid_file).read().strip(), 10)
    check("el runner colgado está en marcha", started)
    child = int(open(pid_file).read()) if started else None
    status = await h.command("/status")
    check("/status muestra el job en curso", status and "a/b/c" in status["text"], status and status["text"])
    proc.send_signal(signal.SIGTERM)
    try:
        code = await asyncio.wait_for(proc.wait(), 20)
    except asyncio.TimeoutError:
        proc.kill()
        code = None
    check("SIGTERM con job en curso sale con exit 0", code == 0, f"exit={code}")
    def dead():
        try:
            with open(f"/proc/{child}/stat") as fh:
                return fh.read().rsplit(")", 1)[1].split()[0] == "Z"
        except FileNotFoundError:
            return True
    check("el proceso hijo del runner murió", child is not None and await until(dead, 5))
    check("el mensaje del job se editó a cancelado", "cancel" in tg.messages[key]["text"], tg.messages[key]["text"])


async def main():
    await startup_failures()
    await crash_redaction()
    tmp = tempfile.mkdtemp(prefix="bot-eval-")
    data_dir = os.path.join(tmp, "data")
    for name, content in (("bot_token", BOT_TOKEN), ("gw_token", GW_TOKEN)):
        with open(os.path.join(tmp, name), "w") as fh:
            fh.write(content + "\n")
    tg = FakeTelegram(BOT_TOKEN)
    api_base = await tg.start()
    gw = FakeGateway()
    gw_server = TestServer(gw.app)
    await gw_server.start_server()
    gw_url = str(gw_server.make_url("")).rstrip("/")

    class GW:
        pass

    gwh = GW()
    gwh.gw, gwh.requests = gw, gw.requests
    h = Harness(tg, gwh, tmp)
    h.set_runner_mode("ok")
    env = {
        **os.environ,
        "BOT_TOKEN_FILE": os.path.join(tmp, "bot_token"), "BOT_ALLOWED_USER_IDS": str(OWNER), "BOT_RUNNER_PATH": RUNNER,
        "BOT_GATEWAY_URL": gw_url, "BOT_GATEWAY_TOKEN_FILE": os.path.join(tmp, "gw_token"), "BOT_DATA_DIR": data_dir,
        "BOT_TELEGRAM_API_BASE": api_base, "BOT_JOB_TIMEOUT_SECONDS": "30", "FAKE_MODE_FILE": h.mode_file,
        "FAKE_PID_FILE": os.path.join(tmp, "child.pid"),
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "bot", cwd=ROOT, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
    )
    log_lines = []

    async def pump():
        async for line in proc.stdout:
            log_lines.append(line.decode("utf-8", "replace"))

    pump_task = asyncio.create_task(pump())
    try:
        if not await until(lambda: tg.calls_of("getUpdates"), 15):
            check("el bot arrancó y hace polling", False, "".join(log_lines)[-800:])
        else:
            await scenarios(h)
            await sigterm_during_dump(h, proc, env["FAKE_PID_FILE"])
    finally:
        print("Apagado y secretos")
        if proc.returncode is None:
            proc.send_signal(signal.SIGTERM)
        try:
            code = await asyncio.wait_for(proc.wait(), 15)
        except asyncio.TimeoutError:
            proc.kill()
            code = None
        await pump_task
        check("el bot terminó (exit 0)", code == 0, f"exit={code}")
        out = "".join(log_lines)
        check("el token del bot no aparece en la salida del bot", BOT_TOKEN not in out and "TEST" not in out)
        check("el token del gateway no aparece en la salida del bot", GW_TOKEN not in out)
        check("sin Traceback en la salida del bot", "Traceback" not in out, out[-600:])
        check("el log dice el username del bot", "@fake_bot" in out)
        audit_path = os.path.join(data_dir, "audit.jsonl")
        audit = open(audit_path).read() if os.path.exists(audit_path) else ""
        events = [json.loads(l) for l in audit.splitlines()]
        kinds = {e["event"] for e in events}
        check("auditoría con comandos, jobs, enlaces y rechazos", {"command", "job_start", "job_end", "link_created", "link_revoked", "update_rejected"} <= kinds, str(kinds))
        check("auditoría sin tokens ni URLs de descarga", BOT_TOKEN not in audit and GW_TOKEN not in audit and "dumps.test" not in audit)
        rejected = [e for e in events if e["event"] == "update_rejected"]
        check("rechazos sin contenido del mensaje", rejected and all(set(e) <= {"ts", "event", "user_id", "kind", "reason"} for e in rejected))
        await gw_server.close()
        await tg.stop()

    passed = sum(1 for _, ok in results if ok)
    ratio = passed / len(results) if results else 0
    print(f"\nResultado: {passed}/{len(results)} comprobaciones (umbral {PASS_THRESHOLD:.0%})")
    for name, ok in results:
        if not ok:
            print(f"  FALLA: {name}")
    return 0 if ratio >= PASS_THRESHOLD else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
