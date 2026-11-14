# =============================================================================
# services/bot/evals/e2e_bot.py — Eval end a end: bot real contra Telegram, gateway y runner falsos
# Uso: python evals/e2e_bot.py   (exit 0 si pasan todas las comprobaciones, 1 si falla alguna)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import hashlib
import json
import os
import re
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
FETCH = os.path.join(ROOT, "tests", "fakes", "fake_fetch.py")
GL_SECRET = "glpat" "-EVALSECRET-999"
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


def strip_code(text):
    return re.sub(r"<code>.*?</code>", "", text, flags=re.S)


def new_sends(tg, start):
    """Mensajes nuevos desde `start`, sin contar el de progreso inicial."""
    return [c["params"] for c in tg.calls[start:] if c["method"] == "sendMessage" and not c["params"].get("text", "").startswith(("Generando dump", "Preparando"))]


async def job_command(h, text, start):
    """Lanza un /dump y devuelve la clave del mensaje de progreso, aunque el resultado llegue enseguida."""
    await h.tg.push_text(OWNER, text)
    found = []

    def seen():
        found[:] = [c for c in h.tg.calls[start:] if c["method"] == "sendMessage" and c["params"].get("text", "").startswith("Generando dump")]
        return bool(found)

    return (OWNER, found[0]["message_id"]) if await until(seen) else None


def notify_ok(p):
    return str(p.get("disable_notification", "false")).lower() in ("false", "none", "")


def check_result_message(tg, start, key, label, expect_summary):
    """El resultado debe llegar como sendMessage nuevo, con push, y el progreso editarse corto."""
    sends = new_sends(tg, start)
    res = [p for p in sends if p.get("text", "").startswith(expect_summary)]
    check(f"{label}: el resultado llega como sendMessage nuevo", len(res) == 1, str([p.get("text", "")[:60] for p in sends]))
    p = res[0] if res else {"text": ""}
    check(f"{label}: sin disable_notification (push activo)", res and notify_ok(p), str(p.get("disable_notification")))
    check(f"{label}: primer renglón es el resumen", p["text"].split("\n")[0].startswith(expect_summary), p["text"][:80])
    progress = tg.messages[key]
    check(f"{label}: el progreso se editó a un estado final corto sin URL",
          not progress["text"].startswith(("Generando", "Preparando")) and len(progress["text"]) < 160 and "http" not in progress["text"], progress["text"])
    return p


def check_link_message(p, label):
    text = p["text"]
    check(f"{label}: la URL va solo dentro de <code>", "https://dumps.test/d/" in text and "http" not in strip_code(text), text)
    markup = json.loads(p["reply_markup"]) if p.get("reply_markup") else {"inline_keyboard": []}
    btns = [b for row in markup["inline_keyboard"] for b in row]
    copy = [b for b in btns if "copy_text" in b]
    url = re.search(r"<code>(https://dumps\.test/d/[^<]+)</code>", text)
    check(f"{label}: botón copy_text con la URL exacta", len(copy) == 1 and url and copy[0]["copy_text"]["text"] == url.group(1), str(btns))
    check(f"{label}: ningún botón de tipo url", not any("url" in b for b in btns), str(btns))
    check(f"{label}: queda el botón de revocar (callback)", any(str(b.get("callback_data", "")).startswith("r|") for b in btns), str(btns))
    lp = json.loads(p.get("link_preview_options", "{}"))
    check(f"{label}: vista previa deshabilitada", lp.get("is_disabled") is True, str(lp))


class Harness:
    def __init__(self, tg, gw, tmp):
        self.tg, self.gw, self.tmp = tg, gw, tmp
        self.mode_file = os.path.join(tmp, "mode")
        self.fetch_mode_file = os.path.join(tmp, "fetch_mode")

    def set_runner_mode(self, mode):
        with open(self.mode_file, "w") as fh:
            fh.write(mode)

    def set_fetch_mode(self, mode):
        with open(self.fetch_mode_file, "w") as fh:
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



async def code_walk(h, project, ref, what, tags=False):
    """Recorre /codigo por botones hasta la pantalla de confirmación."""
    m = await h.command("/codigo")
    m = await h.click(m, find_button(m, lambda b: b["text"] == project))
    if tags:
        m = await h.click(m, find_button(m, lambda b: b["text"] == "🏷 Tags"))
    m = await h.click(m, find_button(m, lambda b: b["text"] == ref))
    m = await h.click(m, find_button(m, lambda b: b["text"] == "• zip"))
    return await h.click(m, find_button(m, lambda b: b["text"] == what))


async def code_run(h, m, expect_prefix):
    tg = h.tg
    start = len(tg.calls)
    await h.click(m, find_button(m, lambda b: b["text"] == "Generar entrega"), expect_change=False)
    key = (OWNER, m["message_id"])
    await until(lambda: any(p.get("text", "").startswith(expect_prefix) for p in new_sends(tg, start)))
    await until(lambda: tg.messages[key]["text"][:1] in "✅⚠❌")
    return start, key


def sha8(name):
    return hashlib.sha1(name.encode()).hexdigest()[:8]


async def code_scenarios(h):
    tg, gw = h.tg, h.gw
    DEST = "alpha/mysql8/appdb"

    print("/codigo: navegación por botones, paginación y búsqueda por texto")
    m = await h.command("/codigo")
    check("el catálogo se ofrece como botones", m and find_button(m, lambda b: b["text"] == "alpha-app · BD") and find_button(m, lambda b: b["text"] == "solo-codigo"), m and m["text"])
    check("HTML hostil del catálogo solo como texto de botón", find_button(m, lambda b: b["text"] == "<b>evil&proj</b> · BD"))
    m = await h.click(m, find_button(m, lambda b: b["text"] == "alpha-app · BD"))
    ref_btns = [b for b in buttons(m) if b["callback_data"].split("|")[2] == "r"]
    check("la rama por defecto va primero y marcada", ref_btns and ref_btns[0]["text"] == "main ★", str([b["text"] for b in ref_btns]))
    check("ramas paginadas de 8 con ▶ y sin la rama hostil", len(ref_btns) == 8 and find_button(m, lambda b: b["text"] == "▶") and not find_button(m, lambda b: "mala" in b["text"]))
    check("botones 🏷 Tags y 🔎 Buscar presentes", find_button(m, lambda b: b["text"] == "🏷 Tags") and find_button(m, lambda b: b["text"] == "🔎 Buscar"))
    m = await h.click(m, find_button(m, lambda b: b["text"] == "▶"))
    check("la segunda página ofrece ◀", find_button(m, lambda b: b["text"] == "◀") is not None)
    wiz_key = (OWNER, m["message_id"])
    m = await h.click(m, find_button(m, lambda b: b["text"] == "🔎 Buscar"))
    check("buscar pide el texto en el siguiente mensaje", m and "siguiente mensaje" in m["text"], m and m["text"])
    out_before = h.n_out()
    await tg.push_text(STRANGER, "feat/b03")
    await tg.push_text(OWNER, "zzmarker")
    ok = await until(lambda: "Sin resultados" in tg.messages[wiz_key]["text"])
    check("el texto del dueño se usa como búsqueda (sin resultados)", ok, tg.messages[wiz_key]["text"])
    check("el texto de un ajeno se ignora sin ninguna salida", h.n_out() == out_before + 1, f"{h.n_out() - out_before} llamadas")
    m = tg.messages[wiz_key]
    m = await h.click(m, find_button(m, lambda b: b["text"] == "🔎 Buscar"))
    await tg.push_text(OWNER, "  feat/b03 <>&; \n")
    ok = await until(lambda: find_button(tg.messages[wiz_key], lambda b: b["text"] == "feat/b03") is not None)
    m = tg.messages[wiz_key]
    check("la búsqueda sanitizada devuelve la rama", ok and "<script>" not in m["text"], m["text"])
    m = await h.click(m, find_button(m, lambda b: b["text"] == "feat/b03"))
    m = await h.click(m, find_button(m, lambda b: b["text"] == "• zip"))
    m = await h.click(m, find_button(m, lambda b: b["text"] == "📦🗄 Ambos"))
    check("confirmación: destino exacto de la BD", m and f"<code>{DEST}</code>" in m["text"], m and m["text"])
    check("confirmación: proyecto@referencia (sha8) del resultado de búsqueda", m and f"<b>alpha-app@feat/b03</b> (<code>{sha8('feat/b03')}</code>)" in m["text"], m and m["text"])
    m = await h.click(m, find_button(m, lambda b: b["text"] == "4 h"))
    m = await h.click(m, find_button(m, lambda b: b["text"] == "3"))
    check("confirmación: vigencia y descargas por archivo", "<b>4 h</b>" in m["text"] and "por archivo: <b>3</b>" in m["text"], m["text"])
    n_posts = len([r for r in gw.requests if r["method"] == "POST"])
    start, key = await code_run(h, m, "✅ Listo")
    p = check_result_message(tg, start, key, "código+BD", "✅ Listo · alpha-app@feat/b03 · código + BD")
    check_link_message(p, "código+BD")
    check("lista cada archivo con tipo, nombre y tamaño", "📦 código: <code>alpha-app_feat-b03_" in p["text"] and "🗄 BD: <code>f.sql.gz</code> · 1.2 MB" in p["text"], p["text"])
    check("descargas permitidas por archivo", "Descargas permitidas: 3 por archivo" in p["text"], p["text"])
    post = [r for r in gw.requests if r["method"] == "POST"][n_posts:]
    files = post[0]["body"]["files"] if post else []
    check("UN enlace v2 con código y luego BD", len(post) == 1 and post[0]["path"] == "/v2/links" and [f["relpath"].split("/")[0] for f in files] == ["_gitlab", "alpha"], str(post))
    check("max_downloads=3 ttl=14400 label proyecto@rama", post and post[0]["body"]["max_downloads"] == 3 and post[0]["body"]["ttl_seconds"] == 14400 and post[0]["body"]["label"] == "alpha-app@feat/b03", str(post))
    check("job_id del enlace válido", post and re.fullmatch(r"tg[0-9]+-[0-9a-f]{8}", post[0]["body"]["job_id"]))
    check("la búsqueda no se envía como mensaje del bot ni queda en el aviso", "zzmarker" not in p["text"])
    both_link_id = next(l["id"] for l in gw.gw.links.values() if len(l["files"]) == 2)

    print("/codigo: tags y entrega parcial en las dos direcciones")
    h.set_fetch_mode("err")
    m = await code_walk(h, "alpha-app · BD", "v1.1.0", "📦🗄 Ambos", tags=True)
    check("confirmación por tag muestra su sha8", f"alpha-app@v1.1.0</b> (<code>{sha8('v1.1.0')}</code>)" in m["text"], m["text"])
    start, key = await code_run(h, m, "⚠️ Entrega parcial")
    p = check_result_message(tg, start, key, "parcial (código falló)", "⚠️ Entrega parcial · alpha-app@v1.1.0 · código falló, BD listo")
    check("parcial: dice qué falló y por qué (token de GitLab)", "❌ código (E_AUTH): Problema con el token de GitLab" in p["text"], p["text"])
    check_link_message(p, "parcial (código falló)")
    check("parcial: el enlace solo lleva la BD", "🗄 BD:" in p["text"] and "📦 código:" not in p["text"])
    last = [r for r in gw.requests if r["method"] == "POST"][-1]
    check("parcial: el gateway recibió solo la BD", [f["relpath"].split("/")[0] for f in last["body"]["files"]] == ["alpha"], str(last["body"]))
    h.set_fetch_mode("ok")
    h.set_runner_mode("err")
    m = await code_walk(h, "alpha-app · BD", "main ★", "📦🗄 Ambos")
    start, key = await code_run(h, m, "⚠️ Entrega parcial")
    p = check_result_message(tg, start, key, "parcial (BD falló)", "⚠️ Entrega parcial · alpha-app@main · código listo, BD falló")
    check("parcial: BD falló por SSH", "❌ BD (E_SSH): No se pudo conectar por SSH al servidor." in p["text"] and "📦 código:" in p["text"], p["text"])
    check_link_message(p, "parcial (BD falló)")
    print("/codigo: la rama avanza entre la confirmación y la descarga")
    h.set_fetch_mode("moved")
    h.set_runner_mode("ok")
    m = await code_walk(h, "alpha-app · BD", "main ★", "📦🗄 Ambos")
    start, key = await code_run(h, m, "✅ Listo")
    p = new_sends(tg, start)[-1]
    real8 = hashlib.sha1(b"main+1").hexdigest()[:8]
    check("rama avanzada: el resumen usa el commit real", p["text"].split("\n")[0] == f"✅ Listo · alpha-app@main ({real8}) · código + BD", p["text"])
    check("rama avanzada: renglón de aviso con ambos commits", f"⚠️ La rama avanzó mientras se preparaba: se entregó {real8} (el que viste era {sha8('main')})" in p["text"], p["text"])
    check_link_message(p, "rama avanzada")
    h.set_fetch_mode("ok")
    print("/codigo: fallo total y enlace fallido")
    h.set_fetch_mode("err")
    h.set_runner_mode("err")
    n_posts = len([r for r in gw.requests if r["method"] == "POST"])
    m = await code_walk(h, "alpha-app · BD", "main ★", "📦🗄 Ambos")
    start, key = await code_run(h, m, "❌ Entrega falló")
    p = new_sends(tg, start)[-1]
    check("fallo total: solo aviso de error, sin enlace ni botones", "http" not in p["text"] and not p.get("reply_markup") and "E_AUTH" in p["text"] and "E_SSH" in p["text"], p["text"])
    check("fallo total: no se creó ningún enlace", len([r for r in gw.requests if r["method"] == "POST"]) == n_posts)
    check("fallo total: el aviso nuevo tiene push y el progreso quedó corto", notify_ok(p) and len(tg.messages[key]["text"]) < 160, tg.messages[key]["text"])
    h.set_fetch_mode("ok")
    h.set_runner_mode("ok")
    gw.gw.mode = "error500"
    m = await code_walk(h, "alpha-app · BD", "main ★", "📦 Código")
    start, key = await code_run(h, m, "⚠️ Archivos listos")
    p = new_sends(tg, start)[-1]
    check("enlace fallido con archivos existentes lo dice", "pro" not in p["text"] and "📦 código:" in p["text"] and "enlace falló" in p["text"] and "http" not in p["text"], p["text"])
    gw.gw.mode = "ok"

    print("/codigo: solo código en un proyecto sin BD")
    m = await h.command("/codigo")
    m = await h.click(m, find_button(m, lambda b: b["text"] == "solo-codigo"))
    m = await h.click(m, find_button(m, lambda b: b["text"] == "main ★"))
    m = await h.click(m, find_button(m, lambda b: b["text"] == "• zip"))
    check("sin BD solo se ofrece Código", [b["text"] for b in buttons(m)][:1] == ["📦 Código"] and not find_button(m, lambda b: "Base de datos" in b["text"]), str([b["text"] for b in buttons(m)]))
    m = await h.click(m, find_button(m, lambda b: b["text"] == "📦 Código"))
    start, key = await code_run(h, m, "✅ Listo")
    p = check_result_message(tg, start, key, "solo código", "✅ Listo · solo-codigo@main · código")
    check_link_message(p, "solo código")

    print("/links con archivos y revocar un enlace multi-archivo")
    m = await h.command("/links")
    check("/links lista cada archivo con su conteo de descargas", m and "📦" in m["text"] and "🗄" in m["text"] and re.search(r"descargas 0/3", m["text"]), m and m["text"])
    n_del = len([r for r in gw.requests if r["method"] == "DELETE"])
    await h.command(f"/revoke {both_link_id}")
    dels = [r for r in gw.requests if r["method"] == "DELETE"][n_del:]
    check("revocar el enlace de dos archivos hace DELETE /v2 sin keep_file", dels and dels[0]["path"] == f"/v2/links/{both_link_id}" and "keep_file" not in dels[0]["query"], str(dels))
    check("el gateway marcó ambos archivos para borrar", {r.split("/")[0] for r in gw.gw.deleted_files} >= {"_gitlab", "alpha"})

    print("/codigo: errores de consulta y secretos")
    h.set_fetch_mode("err_all")
    m = await h.command("/codigo")
    check("catálogo con error de token muestra mensaje claro", m and "E_AUTH" in m["text"] and "token de GitLab" in m["text"], m and m["text"])
    h.set_fetch_mode("garbage")
    m = await h.command("/codigo")
    check("salida inválida de fetch.py da error genérico", m and "E_BAD_OUTPUT" in m["text"] and "Traceback" not in m["text"], m and m["text"])
    h.set_fetch_mode("ok")
    everything = json.dumps(tg.calls)
    check("ningún mensaje ni llamada contiene glpat", "glpat" not in everything)
    m = await h.command("/codigo")
    stale = find_button(m, lambda b: b["text"] == "alpha-app · BD")
    await h.click(m, stale)
    await tg.push_callback(OWNER, "k|00000000|p|0", OWNER, m["message_id"])
    ok = await until(lambda: any("sesión expiró" in c["params"].get("text", "") for c in tg.calls_of("editMessageText")[-3:]))
    check("callback de /codigo expirado se contesta sin romper", ok)


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
    start = len(tg.calls)
    await h.click(m, find_button(m, lambda b: b["text"] == "Generar dump"), expect_change=False)
    key = (OWNER, m["message_id"])
    ok = await until(lambda: any("https://dumps.test/d/" in p.get("text", "") for p in new_sends(tg, start)) and "terminado" in tg.messages[key]["text"])
    check("el resultado con la URL llega en un mensaje nuevo", ok, str(new_sends(tg, start)))
    p = check_result_message(tg, start, key, "wizard", "✅ Dump listo · alpha/appdb · 1.2 MB")
    check_link_message(p, "wizard")
    check("resultado incluye sha256 abreviado, duración y vencimiento", all(x in p["text"] for x in ("aaaaaaaaaaaa…", "3 s", "Vence:", "3 descarga")), p["text"])
    edits = [c for c in tg.calls[start:] if c["method"] == "editMessageText"]
    check("ninguna edición lleva la URL (editar no avisa)", edits and not any("https://" in c["params"].get("text", "") for c in edits))
    order = tg.order("sendMessage", "editMessageText")
    check("el aviso se envía antes de cerrar el progreso", order and tg.calls[[i for i in order if i >= start][-1]]["method"] == "editMessageText")
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
    start = len(tg.calls)
    key = await job_command(h, "/dump a b c ttl=2h max=3", start)
    check("responde 'Generando dump'", key is not None)
    ok = await until(lambda: any("https://dumps.test/d/" in p.get("text", "") for p in new_sends(tg, start)) and "terminado" in tg.messages[key]["text"])
    check("la forma directa termina con la URL en mensaje nuevo", ok, str(new_sends(tg, start)))
    p = check_result_message(tg, start, key, "directo", "✅ Dump listo · a/c · 1.2 MB")
    check_link_message(p, "directo")
    posts = [r for r in gw.requests if r["method"] == "POST"][n_posts:]
    check("ttl=2h max=3 llegan al gateway", posts and posts[0]["body"]["ttl_seconds"] == 7200 and posts[0]["body"]["max_downloads"] == 3 and posts[0]["body"]["files"][0]["relpath"] == "a/f.sql.gz", str(posts))
    m = await h.command("/dump a b c ttl=5s")
    check("duración inválida da error de uso sin ejecutar", m and "60s y 7d" in m["text"])

    print("Revocar desde el aviso final")
    start = len(tg.calls)
    key = await job_command(h, "/dump a b c", start)
    await until(lambda: any("https://dumps.test/d/" in p.get("text", "") for p in new_sends(tg, start)))
    res_call = next(c for c in tg.calls[start:] if c["method"] == "sendMessage" and "https://dumps.test/d/" in c["params"].get("text", ""))
    res_msg = tg.messages[(OWNER, res_call["message_id"])]
    rbtn = find_button(res_msg, lambda b: b.get("callback_data", "").startswith("r|"))
    rid = rbtn["callback_data"].split("|")[1]
    after = await h.click(res_msg, rbtn)
    check("revocar desde el aviso deja el mensaje sin la URL", after and "https://" not in after["text"] and "revocado; el dump se eliminará" in after["text"], after and after["text"])
    dels = [r for r in gw.requests if r["method"] == "DELETE" and r["path"].endswith(rid)]
    check("el botón del aviso hace DELETE sin keep_file", dels and "keep_file" not in dels[-1]["query"], str(dels))

    print("/status, /links y revocar")
    m = await h.command("/status")
    check("/status libre", m and "Libre" in m["text"])
    m = await h.command("/links")
    revoke_btns = buttons(m)
    check("/links lista enlaces con botón de revocar", len(revoke_btns) >= 2 and "Revocar" in revoke_btns[0]["text"], m and m["text"])
    link_id = revoke_btns[0]["callback_data"].split("|")[1]
    m2 = await h.click(m, revoke_btns[0])
    check("revocar actualiza la lista", m2 and link_id not in m2["text"], m2 and m2["text"])
    check("el texto dice que el dump se eliminará, sin prometer borrado inmediato", m2 and "el dump se eliminará" in m2["text"] and "eliminado" not in m2["text"], m2 and m2["text"])
    check("la lista avisa que al revocar o vencer se elimina el dump", "se elimina el dump" in m["text"], m["text"])
    check("gateway marcó el enlace como revocado", gw.gw.links[link_id]["state"] == "revoked")
    dels = [r for r in gw.requests if r["method"] == "DELETE" and r["path"].endswith(link_id)]
    check("DELETE sin keep_file (el gateway borra el dump)", dels and all("keep_file" not in r["query"] for r in dels), str(dels))
    check("el dump quedó marcado para borrarse", gw.gw.links[link_id]["files"][0]["relpath"] in gw.gw.deleted_files)
    other = revoke_btns[1]["callback_data"].split("|")[1]
    m3 = await h.command(f"/revoke {other}")
    check("/revoke <id> revoca y avisa que el dump se eliminará", m3 and "revocado; el dump se eliminará" in m3["text"] and gw.gw.links[other]["state"] == "revoked", m3 and m3["text"])
    m3 = await h.command("/revoke 000000000000")
    check("/revoke de id inexistente informa", m3 and "no existe" in m3["text"])
    m3 = await h.command("/revoke zzz")
    check("/revoke con id inválido da uso", m3 and "Uso" in m3["text"])

    print("Errores sin traceback")
    h.set_runner_mode("err")
    start = len(tg.calls)
    key = await job_command(h, "/dump a b c", start)
    ok = await until(lambda: any("E_SSH" in p.get("text", "") for p in new_sends(tg, start)) and "falló" in tg.messages[key]["text"])
    sends = new_sends(tg, start)
    t = sends[0]["text"] if sends else ""
    check("error del runner llega como mensaje nuevo con push", ok and notify_ok(sends[0]), str(sends))
    check("primer renglón: '❌ Dump falló · E_SSH'", t.split("\n")[0] == "❌ Dump falló · E_SSH", t)
    check("error del runner muestra mensaje de SSH", "SSH" in t, t)
    check("stderr en bloque <pre>", "<pre>" in t and "Connection refused" in t, t)
    check("sin traceback ni rutas internas", "Traceback" not in t and "File \"" not in t)
    check("progreso editado a estado final corto", len(tg.messages[key]["text"]) < 160 and "Connection" not in tg.messages[key]["text"], tg.messages[key]["text"])
    h.set_runner_mode("garbage")
    start = len(tg.calls)
    await job_command(h, "/dump a b c", start)
    ok = await until(lambda: any("E_BAD_OUTPUT" in p.get("text", "") for p in new_sends(tg, start)))
    check("salida ilegible del runner da error genérico claro", ok, str(new_sends(tg, start)))
    h.set_runner_mode("ok")
    gw.gw.mode = "error500"
    start = len(tg.calls)
    await job_command(h, "/dump a b c", start)
    ok = await until(lambda: any("no se pudo crear el enlace" in p.get("text", "") for p in new_sends(tg, start)))
    sends = new_sends(tg, start)
    check("si el enlace falla, el aviso nuevo dice que el dump existe", ok and "dump se generó" in sends[0]["text"] and sends[0]["text"].startswith("⚠️ Dump listo"), str(sends))
    gw.gw.mode = "ok"
    m = await h.command("/links")
    check("el bot sigue vivo tras los errores", m is not None)

    await code_scenarios(h)

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
    start = len(tg.calls)
    key = await job_command(h, "/dump a b c", start)
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
    sends = new_sends(tg, start)
    check("la cancelación también avisa con mensaje nuevo y push", sends and sends[-1]["text"].startswith("❌ Dump falló · E_CANCELLED") and notify_ok(sends[-1]), str(sends))


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
    h.set_fetch_mode("ok")
    env = {
        **os.environ,
        "BOT_TOKEN_FILE": os.path.join(tmp, "bot_token"), "BOT_ALLOWED_USER_IDS": str(OWNER), "BOT_RUNNER_PATH": RUNNER,
        "BOT_GATEWAY_URL": gw_url, "BOT_GATEWAY_TOKEN_FILE": os.path.join(tmp, "gw_token"), "BOT_DATA_DIR": data_dir,
        "BOT_TELEGRAM_API_BASE": api_base, "BOT_JOB_TIMEOUT_SECONDS": "30", "FAKE_MODE_FILE": h.mode_file,
        "FAKE_PID_FILE": os.path.join(tmp, "child.pid"),
        "BOT_GITLAB_FETCH": FETCH, "FAKE_FETCH_MODE_FILE": h.fetch_mode_file, "FAKE_FETCH_ERR": "E_AUTH",
        "FAKE_FETCH_MESSAGE": f"token {GL_SECRET} rechazado",
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
        check("ningún glpat en la salida del bot", "glpat" not in out and GL_SECRET not in out)
        check("sin Traceback en la salida del bot", "Traceback" not in out, out[-600:])
        check("el log dice el username del bot", "@fake_bot" in out)
        audit_path = os.path.join(data_dir, "audit.jsonl")
        audit = open(audit_path).read() if os.path.exists(audit_path) else ""
        events = [json.loads(l) for l in audit.splitlines()]
        kinds = {e["event"] for e in events}
        check("auditoría con comandos, jobs, enlaces y rechazos", {"command", "job_start", "job_end", "link_created", "link_revoked", "update_rejected"} <= kinds, str(kinds))
        check("auditoría sin tokens ni URLs de descarga", BOT_TOKEN not in audit and GW_TOKEN not in audit and "dumps.test" not in audit and "glpat" not in audit)
        check("la búsqueda no se audita como texto", "zzmarker" not in audit and "<>" not in audit)
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
