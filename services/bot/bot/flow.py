# =============================================================================
# services/bot/bot/flow.py — Lógica pura: asistente de /dump, teclados como datos y mensajes HTML
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import html
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .durations import DurationError, format_duration, parse_duration
from .gitlab_client import sanitize_search
from .packages import PackageOutcome, PackageRequest

WIZARD_TTL = 600
WIZARD_MAX = 50
PAGE_SIZE = 8
TTL_CHOICES = (3600, 14400, 86400)
MAX_CHOICES = (1, 3, 5)
MAX_DOWNLOADS_LIMIT = 20
STDERR_CHARS = 1500
COPY_TEXT_MAX = 256
PURGE_NOTE = "Al revocar o al vencer un enlace se elimina el dump."
REVOKED_NOTE = "Enlace revocado; el dump se eliminará."

NAME_RE = {
    "server": re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z"),
    "container": re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z"),
    "db": re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z"),
}
LINK_ID_RE = re.compile(r"^[0-9a-f]{12}\Z")
_CB_WIZARD = re.compile(r"^w\|([0-9a-f]{8})\|(s|c|d|t|m|pg|b|go|x)\|([0-9]{0,9})\Z")
_CB_CODE = re.compile(r"^k\|([0-9a-f]{8})\|(p|r|pg|tg|br|q|f|wh|t|m|go|b|x)\|([0-9]{0,9})\Z")
_CB_REVOKE = re.compile(r"^r\|([0-9a-f]{12})\Z")

ERROR_MESSAGES = {
    "E_USAGE": "El runner rechazó los argumentos.",
    "E_CONFIG": "Configuración del servidor incompleta o inválida.",
    "E_INVALID_NAME": "Nombre de servidor, contenedor o base inválido.",
    "E_SERVER_NOT_FOUND": "El servidor no existe en la configuración.",
    "E_NOT_DISCOVERED": "Contenedor o base no encontrados en el servidor.",
    "E_LOCKED": "Ya hay un dump en curso para ese servidor.",
    "E_SSH": "No se pudo conectar por SSH al servidor.",
    "E_DUMP_FAILED": "El comando de dump falló en el servidor.",
    "E_TRUNCATED": "El dump quedó incompleto y se descartó.",
    "E_DISK": "No hay espacio libre suficiente para el dump.",
    "E_INTERNAL": "Error interno del runner.",
    "E_TIMEOUT": "El dump superó el tiempo máximo y se canceló.",
    "E_BAD_OUTPUT": "El runner devolvió una respuesta inesperada.",
    "E_RUNNER_START": "No se pudo ejecutar el runner.",
    "E_CANCELLED": "El dump se canceló.",
}
GATEWAY_MESSAGES = {
    "unauthorized": "El gateway rechazó el token del bot.",
    "invalid_request": "El gateway rechazó la solicitud del enlace.",
    "file_not_found": "El gateway no encuentra el archivo del dump.",
    "not_found": "El enlace no existe.",
    "internal": "Error interno del gateway.",
    "unreachable": "No se pudo conectar con el gateway.",
    "timeout": "El gateway no respondió a tiempo.",
    "bad_response": "El gateway devolvió una respuesta inválida.",
}


GITLAB_MESSAGES = {
    "E_USAGE": "El servicio de GitLab rechazó los argumentos.",
    "E_CONFIG": "Configuración de GitLab incompleta o inválida, o el token no es de solo lectura (debe tener solo el alcance read_api).",
    "E_INVALID_NAME": "Nombre de proyecto o referencia inválido.",
    "E_NOT_ALLOWED": "Ese proyecto no está permitido en el catálogo.",
    "E_AUTH": "Problema con el token de GitLab: GitLab lo rechazó o venció.",
    "E_FORBIDDEN": "Problema con el token de GitLab: no tiene permiso sobre ese proyecto.",
    "E_NOT_FOUND": "El proyecto o la referencia no existe en GitLab.",
    "E_RATE_LIMITED": "GitLab limitó las peticiones; reintenta en unos minutos.",
    "E_GITLAB": "GitLab respondió con un error.",
    "E_NETWORK": "No se pudo conectar con GitLab.",
    "E_TOO_LARGE": "El código supera el tamaño máximo permitido.",
    "E_TRUNCATED": "La descarga quedó incompleta y se descartó.",
    "E_DISK": "No hay espacio libre suficiente para el código.",
    "E_INTERNAL": "Error interno del servicio de GitLab.",
    "E_TIMEOUT": "El código tardó más del límite y se canceló.",
    "E_BAD_OUTPUT": "El servicio de GitLab devolvió una respuesta inesperada.",
    "E_RUNNER_START": "No se pudo ejecutar el servicio de GitLab.",
    "E_CANCELLED": "La descarga del código se canceló.",
}
WHAT_LABEL = {"code": "código", "db": "BD", "both": "código + BD"}
KIND_LABEL = {"code": "código", "db": "BD"}
KIND_ICON = {"code": "📦", "db": "🗄"}
SEARCH_HINT = "Usa letras, números y . _ / @ + -"
LINKS_TEXT_MAX = 3800


def esc(value) -> str:
    return html.escape(str(value), quote=False)


@dataclass(frozen=True)
class Button:
    text: str
    data: str = ""
    copy_text: str | None = None


@dataclass
class View:
    text: str
    keyboard: list = field(default_factory=list)


def _btn(text, data):
    assert len(data.encode()) <= 64
    return Button(str(text)[:60], data)


def _copy_btn(text, value):
    return Button(str(text)[:60], copy_text=value)


def human_size(n) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def fmt_elapsed(seconds) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    return f"{seconds // 60} min {seconds % 60} s"


def fmt_expiry(iso, now=None) -> str:
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return esc(iso)
    now = now or datetime.now(timezone.utc)
    left = int((dt - now).total_seconds())
    rel = f"en {format_duration(left)}" if left > 0 else "vencido"
    return f"{dt:%Y-%m-%d %H:%M} UTC ({rel})"


def req_target(req) -> str:
    if isinstance(req, PackageRequest):
        return f"{esc(req.project)}@{esc(req.ref)}"
    return f"{esc(req.server)}/{esc(req.db)}"


# ---------- Comandos y argumentos ----------

class UsageError(ValueError):
    pass


@dataclass(frozen=True)
class DumpArgs:
    server: str
    container: str
    db: str
    ttl: int
    max_downloads: int


USAGE_DUMP = "Uso: /dump &lt;servidor&gt; &lt;contenedor&gt; &lt;db&gt; [ttl=2h] [max=3]"


def parse_dump_args(text, default_ttl, default_max) -> DumpArgs:
    names, ttl, mx = [], default_ttl, default_max
    for tok in (text or "").split():
        low = tok.lower()
        if low.startswith("ttl="):
            try:
                ttl = parse_duration(tok[4:])
            except DurationError as exc:
                raise UsageError(esc(exc)) from None
        elif low.startswith("max="):
            val = tok[4:]
            if not (val.isdigit() and val.isascii()) or not 1 <= int(val) <= MAX_DOWNLOADS_LIMIT:
                raise UsageError(f"max debe ser un entero entre 1 y {MAX_DOWNLOADS_LIMIT}.")
            mx = int(val)
        else:
            names.append(tok)
    if len(names) != 3:
        raise UsageError(USAGE_DUMP)
    for kind, value in zip(("server", "container", "db"), names):
        if not NAME_RE[kind].match(value):
            raise UsageError(f"Nombre inválido para {kind}: <code>{esc(value[:40])}</code>")
    return DumpArgs(names[0], names[1], names[2], ttl, mx)


def parse_revoke_arg(text) -> str:
    arg = (text or "").strip()
    if not LINK_ID_RE.match(arg):
        raise UsageError("Uso: /revoke &lt;id&gt; (12 caracteres hexadecimales, míralo en /links)")
    return arg


def command_name(text):
    """Extrae '/cmd' sin argumentos ni @bot; None si no es comando."""
    if not text or not text.startswith("/"):
        return None
    head = text.split(None, 1)[0][1:].split("@", 1)[0].lower()
    return head or None


# ---------- Asistente ----------

@dataclass
class Wizard:
    id: str
    user_id: int
    ttl: int
    max_downloads: int
    step: str = "server"
    servers: list = field(default_factory=list)
    containers: list = field(default_factory=list)
    dbs: list = field(default_factory=list)
    server: str = ""
    container: str = ""
    engine: str = ""
    db: str = ""
    page: int = 0
    busy: bool = False
    deadline: float = 0.0


@dataclass
class Step:
    kind: str  # view | fetch_containers | fetch_dbs | run | cancel | invalid
    notice: str = ""


class WizardStore:
    def __init__(self, ttl=WIZARD_TTL, clock=time.monotonic, max_items=WIZARD_MAX):
        self.ttl = ttl
        self.clock = clock
        self.max_items = max_items
        self._items = {}

    def _purge(self):
        now = self.clock()
        for wid in [k for k, w in self._items.items() if w.deadline <= now]:
            del self._items[wid]

    def create(self, user_id, ttl, max_downloads) -> Wizard:
        self._purge()
        while len(self._items) >= self.max_items:
            del self._items[min(self._items, key=lambda k: self._items[k].deadline)]
        w = Wizard(secrets.token_hex(4), user_id, ttl, max_downloads)
        w.deadline = self.clock() + self.ttl
        self._items[w.id] = w
        return w

    def get(self, wid, user_id):
        self._purge()
        w = self._items.get(wid)
        if w is None or w.user_id != user_id:
            return None
        w.deadline = self.clock() + self.ttl
        return w

    def drop(self, wid):
        self._items.pop(wid, None)

    def __len__(self):
        return len(self._items)


def parse_callback(data):
    """Devuelve ('w'|'k', wid, action, arg) o ('r', link_id) o None si es inválido."""
    if not isinstance(data, str) or len(data.encode()) > 64:
        return None
    m = _CB_WIZARD.match(data)
    if m:
        arg = int(m.group(3)) if m.group(3) else None
        return ("w", m.group(1), m.group(2), arg)
    m = _CB_CODE.match(data)
    if m:
        return ("k", m.group(1), m.group(2), int(m.group(3)) if m.group(3) else None)
    m = _CB_REVOKE.match(data)
    if m:
        return ("r", m.group(1))
    return None


_BACK = {"container": "server", "db": "container", "confirm": "db"}
_FETCH_OPTIONS = {"s": "servers", "c": "containers", "d": "dbs"}


def set_servers(w, names):
    w.servers = list(names)
    w.step = "server"
    w.page = 0


def set_containers(w, items):
    w.busy = False
    w.containers = list(items)
    w.step = "container"
    w.page = 0


def set_dbs(w, engine, names):
    w.busy = False
    w.engine = engine
    w.dbs = list(names)
    w.step = "db"
    w.page = 0


def release(w):
    w.busy = False


def advance(w, action, arg) -> Step:
    if action == "x":
        return Step("cancel")
    if w.busy:
        return Step("invalid", "Espera, estoy consultando.")
    if action in _FETCH_OPTIONS:
        options = getattr(w, _FETCH_OPTIONS[action])
        expected = {"s": "server", "c": "container", "d": "db"}[action]
        if w.step != expected or arg is None or arg >= len(options):
            return Step("invalid", "Opción no válida.")
        if action == "s":
            w.server = options[arg]
            w.busy = True
            return Step("fetch_containers")
        if action == "c":
            w.container = options[arg]["name"]
            w.engine = options[arg]["engine"]
            w.busy = True
            return Step("fetch_dbs")
        w.db = options[arg]
        w.step = "confirm"
        w.page = 0
        return Step("view")
    if action == "pg":
        total = {"server": len(w.servers), "container": len(w.containers), "db": len(w.dbs)}.get(w.step)
        if total is None or arg is None or arg * PAGE_SIZE >= max(total, 1):
            return Step("invalid", "Página no válida.")
        w.page = arg
        return Step("view")
    if action == "t":
        if w.step != "confirm" or arg not in TTL_CHOICES:
            return Step("invalid", "Opción no válida.")
        w.ttl = arg
        return Step("view")
    if action == "m":
        if w.step != "confirm" or arg not in MAX_CHOICES:
            return Step("invalid", "Opción no válida.")
        w.max_downloads = arg
        return Step("view")
    if action == "b":
        prev = _BACK.get(w.step)
        if prev is None:
            return Step("cancel")
        w.step = prev
        w.page = 0
        return Step("view")
    if action == "go":
        return Step("run") if w.step == "confirm" else Step("invalid", "Opción no válida.")
    return Step("invalid", "Opción no válida.")


def _paged(items, labels, action, page, wid):
    start = page * PAGE_SIZE
    rows = [[_btn(labels[i], f"w|{wid}|{action}|{i}")] for i in range(start, min(start + PAGE_SIZE, len(items)))]
    nav = []
    if page > 0:
        nav.append(_btn("‹ Anterior", f"w|{wid}|pg|{page - 1}"))
    if (page + 1) * PAGE_SIZE < len(items):
        nav.append(_btn("Siguiente ›", f"w|{wid}|pg|{page + 1}"))
    if nav:
        rows.append(nav)
    return rows


def _footer(w, back=True):
    row = []
    if back:
        row.append(_btn("Atrás", f"w|{w.id}|b|"))
    row.append(_btn("Cancelar", f"w|{w.id}|x|"))
    return [row]


def render(w) -> View:
    if w.step == "server":
        if not w.servers:
            return View("No hay servidores configurados.", _footer(w, back=False))
        labels = list(w.servers)
        return View("Elige el servidor:", _paged(w.servers, labels, "s", w.page, w.id) + _footer(w, back=False))
    if w.step == "container":
        head = f"Servidor: <b>{esc(w.server)}</b>\n"
        if not w.containers:
            return View(head + "No hay contenedores de base de datos.", _footer(w))
        labels = [
            f"{c['name']} ({c['engine']}" + (f", {c['db_count']} BD)" if c.get("db_count") is not None else ")")
            for c in w.containers
        ]
        return View(head + "Elige el contenedor:", _paged(w.containers, labels, "c", w.page, w.id) + _footer(w))
    if w.step == "db":
        head = f"Servidor: <b>{esc(w.server)}</b>\nContenedor: <b>{esc(w.container)}</b> ({esc(w.engine)})\n"
        if not w.dbs:
            return View(head + "No hay bases de datos.", _footer(w))
        return View(head + "Elige la base de datos:", _paged(w.dbs, list(w.dbs), "d", w.page, w.id) + _footer(w))
    text = (
        "Confirma el dump:\n"
        f"Servidor: <b>{esc(w.server)}</b>\n"
        f"Contenedor: <b>{esc(w.container)}</b> ({esc(w.engine)})\n"
        f"Base: <b>{esc(w.db)}</b>\n"
        f"Vigencia del enlace: <b>{format_duration(w.ttl)}</b>\n"
        f"Descargas máximas: <b>{w.max_downloads}</b>"
    )
    ttl_row = [_btn(("• " if t == w.ttl else "") + format_duration(t), f"w|{w.id}|t|{t}")
               for t in TTL_CHOICES]
    max_row = [_btn(("• " if n == w.max_downloads else "") + str(n), f"w|{w.id}|m|{n}") for n in MAX_CHOICES]
    kb = [ttl_row, max_row, [_btn("Generar dump", f"w|{w.id}|go|")]] + _footer(w)
    return View(text, kb)


def render_error(w, text) -> View:
    view = render(w)
    return View(f"{text}\n\n{view.text}", view.keyboard)


# ---------- Mensajes ----------

def fmt_help() -> View:
    return View(
        "Comandos:\n"
        "/dump: asistente para generar un dump\n"
        "/dump &lt;servidor&gt; &lt;contenedor&gt; &lt;db&gt; [ttl=2h] [max=3]: directo\n"
        "/codigo: entrega código de GitLab, la BD o ambos en un enlace\n"
        "/servers: servidores configurados\n"
        "/links: enlaces activos\n"
        "/revoke &lt;id&gt;: revoca un enlace\n"
        "/status: estado de los jobs\n"
        "/help: esta ayuda"
    )


def fmt_servers(servers) -> View:
    if not servers:
        return View("No hay servidores configurados.")
    lines = [f"<b>{esc(s['name'])}</b> ({esc(s['host'])}, {esc(s['access_mode'])})" for s in servers]
    return View("Servidores:\n" + "\n".join(lines))


def _link_block(rec, now=None):
    label = f" · {esc(rec['label'])}" if rec.get("label") else ""
    mx = esc(rec.get("max_downloads", "?"))
    lines = [f"<code>{esc(rec['id'])}</code>{label}", f"  expira {fmt_expiry(rec.get('expires_at'), now)}"]
    for f in sorted(rec["files"], key=lambda f: f["index"]):
        icon = KIND_ICON.get(f["kind"], "")
        lines.append(f"  {icon} <code>{esc(f['filename'])}</code> ({human_size(f['size_bytes'])}), "
                     f"descargas {esc(f['downloads_completed'])}/{mx}")
    return "\n".join(lines)


def fmt_links(links, now=None, limit=10, notice="") -> View:
    head = f"{notice}\n\n" if notice else ""
    if not links:
        return View(head + "No hay enlaces activos.")
    shown = list(links[:limit])
    while True:
        body = "\n".join(_link_block(rec, now) for rec in shown)
        extra = f"\ny {len(links) - len(shown)} más." if len(links) > len(shown) else ""
        text = head + "Enlaces activos:\n" + body + extra + f"\n\n{PURGE_NOTE}"
        if len(text) <= LINKS_TEXT_MAX or len(shown) <= 1:
            break
        shown.pop()
    kb = [[_btn(f"Revocar {rec['id']}", f"r|{rec['id']}")] for rec in shown]
    return View(text, kb)


def fmt_status(running, waiting, now=None) -> View:
    if not running:
        return View("Libre: no hay dumps en curso.")
    now = now if now is not None else time.monotonic()
    lines = [f"{esc(j.label) if j.label else esc(j.server) + '/' + esc(j.container) + '/' + esc(j.db)} "
             f"({fmt_elapsed(now - j.started)})" for j in running]
    text = "En curso:\n" + "\n".join(lines)
    if waiting:
        text += f"\nEn cola: {waiting}"
    return View(text)


def fmt_started(req) -> str:
    if isinstance(req, PackageRequest):
        return f"Preparando <b>{req_target(req)}</b> ({WHAT_LABEL[req.what]})…"
    return f"Generando dump de <b>{esc(req.db)}</b> ({esc(req.server)}/{esc(req.container)})…"


def fmt_queued(position) -> str:
    return f"Hay un dump en curso. Tu solicitud queda en cola (posición {position}) y arranca sola."


def _tail(text, limit=STDERR_CHARS):
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def fmt_failure(code, message, stderr, what="el dump") -> str:
    base = ERROR_MESSAGES.get(code, "Error desconocido.")
    text = f"Falló {what} ({esc(code)}).\n{esc(base)}"
    return text + _failure_detail(code, message, stderr)


def _failure_detail(code, message, stderr) -> str:
    text = ""
    if message and code in ("E_TIMEOUT", "E_BAD_OUTPUT", "E_RUNNER_START"):
        text += f"\n{esc(message)}"
    tail = _tail(stderr)
    if tail:
        text += f"\n<pre>{esc(tail)}</pre>"
    return text


def _target(d) -> str:
    return f"{esc(d['server'])}/{esc(d['db'])}"


_req_target = req_target


def fmt_job_success(outcome, req, now=None) -> View:
    """Aviso final de éxito: primer renglón de resumen y enlace solo dentro de <code>."""
    d, link = outcome.dump, outcome.link
    url = str(link["url"])
    text = (
        f"✅ Dump listo · {_target(d)} · {human_size(d['size_bytes'])}\n"
        f"Archivo: <code>{esc(d['filename'])}</code>\n"
        f"sha256: <code>{esc(d['sha256'][:12])}…</code>\n"
        f"Duración: {fmt_elapsed(d['duration_s'])}\n"
        f"Vence: {fmt_expiry(link.get('expires_at'), now)} o tras "
        f"{esc(link.get('max_downloads', req.max_downloads))} descarga(s)\n"
        f"Enlace (tócalo para copiarlo):\n<code>{esc(url)}</code>"
    )
    kb = []
    if len(url) <= COPY_TEXT_MAX:
        kb.append([_copy_btn("📋 Copiar enlace", url)])
    kb.append([_btn("Revocar enlace", f"r|{link['id']}")])
    return View(text, kb)


def fmt_job_failure(outcome, req) -> View:
    base = ERROR_MESSAGES.get(outcome.code, "Error desconocido.")
    text = f"❌ Dump falló · {esc(outcome.code)}\n{_req_target(req)}\n{esc(base)}"
    return View(text + _failure_detail(outcome.code, outcome.message, outcome.stderr))


def fmt_job_link_failure(outcome) -> View:
    d = outcome.dump
    return View(f"⚠️ Dump listo, pero el enlace falló · {_target(d)} · {human_size(d['size_bytes'])}\n"
                + fmt_link_failure(outcome))


def fmt_job_cancelled(req) -> View:
    if isinstance(req, PackageRequest):
        return View(f"❌ Entrega falló · E_CANCELLED\n{req_target(req)}\nLa entrega se canceló porque el bot se reinició.")
    return View(f"❌ Dump falló · E_CANCELLED\n{_req_target(req)}\nEl dump se canceló porque el bot se reinició.")


def fmt_progress_done(kind, req, notified=True) -> str:
    """Estado final corto del mensaje de progreso (sin enlace ni detalle)."""
    pkg = isinstance(req, PackageRequest)
    icon = {"ok": "✅ Entrega terminada" if pkg else "✅ Dump terminado",
            "partial": "⚠️ Entrega parcial",
            "link_failed": "⚠️ Entrega terminada sin enlace" if pkg else "⚠️ Dump terminado sin enlace",
            "cancelled": "🚫 Entrega cancelada" if pkg else "🚫 Dump cancelado"}.get(
        kind, "❌ Entrega falló" if pkg else "❌ Dump falló")
    tail = " El detalle está en el mensaje siguiente." if notified else ""
    return f"{icon}: {_req_target(req)}.{tail}"


def fmt_revoked(link_id) -> str:
    return f"Enlace <code>{esc(link_id)}</code> revocado; el dump se eliminará."


def fmt_link_failure(outcome) -> str:
    err = outcome.gateway_error
    reason = GATEWAY_MESSAGES.get(err.code, f"El gateway respondió con un error ({esc(err.status)}).")
    return (
        "El dump se generó, pero no se pudo crear el enlace.\n"
        f"Archivo: <code>{esc(outcome.dump['filename'])}</code> ({human_size(outcome.dump['size_bytes'])}), "
        "sigue en el servidor del bot.\n"
        f"Motivo: {esc(reason)}\n"
        "Vuelve a lanzar /dump para generar otro enlace."
    )


def fmt_gateway_error(err) -> str:
    return "Error del gateway: " + esc(GATEWAY_MESSAGES.get(err.code, f"respuesta {err.status}"))


# ---------- Paquetes (código y/o BD) ----------

def part_error_text(part) -> str:
    base = (GITLAB_MESSAGES if part.kind == "code" else ERROR_MESSAGES).get(part.code, "Error desconocido.")
    extra = f" {esc(part.message)}" if part.message and part.code in ("E_TIMEOUT", "E_BAD_OUTPUT", "E_RUNNER_START") else ""
    return f"{esc(base)}{extra}"


def fmt_gitlab_failure(exc) -> str:
    """Error de una consulta al servicio de GitLab (catálogo, ramas, tags)."""
    base = GITLAB_MESSAGES.get(exc.code, "Error desconocido.")
    extra = f"\n{esc(exc.message)}" if exc.message and exc.code in ("E_TIMEOUT", "E_BAD_OUTPUT", "E_RUNNER_START") else ""
    return f"Falló la consulta a GitLab ({esc(exc.code)}).\n{esc(base)}{extra}"


def _part_failures(outcome):
    return [f"❌ {KIND_LABEL[p.kind]} ({esc(p.code)}): {part_error_text(p)}" for p in outcome.bad]


def _part_files(outcome):
    order = {"code": 0, "db": 1}
    return [
        f"{KIND_ICON[p.kind]} {KIND_LABEL[p.kind]}: <code>{esc(p.result['filename'])}</code> · {human_size(p.result['size_bytes'])}"
        for p in sorted(outcome.good, key=lambda p: order[p.kind])
    ]


def _summary(outcome):
    return " + ".join(KIND_LABEL[k] for k in ("code", "db") if any(p.kind == k and p.ok for p in outcome.parts))


def _moved_ref(outcome, req):
    """(commit8 real, sha8 confirmado) si el código entregado no es el que vio el usuario; si no, None."""
    code = next((p for p in outcome.good if p.kind == "code"), None)
    seen = (req.sha or "")[:8]
    if code is None or not seen:
        return None
    real = str(code.result.get("commit", ""))[:8]
    return (real, seen) if real and real != seen else None


def fmt_package_result(outcome, req, now=None) -> View:
    target = req_target(req)
    moved = _moved_ref(outcome, req)
    warning = []
    if moved:
        target += f" ({esc(moved[0])})"
        warning = [f"⚠️ La rama avanzó mientras se preparaba: se entregó {esc(moved[0])} (el que viste era {esc(moved[1])})"]
    if outcome.kind == "failed":
        names = " y ".join(KIND_LABEL[p.kind] for p in outcome.parts)
        return View("\n".join([f"❌ Entrega falló · {target} · {names}", *_part_failures(outcome)]))
    files = _part_files(outcome)
    fails = _part_failures(outcome)
    if outcome.kind == "link_failed":
        err = outcome.gateway_error
        reason = GATEWAY_MESSAGES.get(err.code, f"El gateway respondió con un error ({esc(err.status)}).")
        lines = [f"⚠️ Archivos listos, pero el enlace falló · {target}", *warning, *files, *fails,
                 f"Motivo: {esc(reason)}",
                 "Los archivos siguen en el servidor del bot. Vuelve a lanzar /codigo para generar el enlace."]
        return View("\n".join(lines))
    link = outcome.link
    url = str(link["url"])
    if outcome.kind == "partial":
        status = ", ".join(f"{KIND_LABEL[p.kind]} {'listo' if p.ok else 'falló'}" for p in outcome.parts)
        first = f"⚠️ Entrega parcial · {target} · {status}"
    else:
        first = f"✅ Listo · {target} · {_summary(outcome)}"
    lines = [first, *warning, *files, *fails,
             f"Descargas permitidas: {esc(link.get('max_downloads', req.max_downloads))} por archivo",
             f"Vence: {fmt_expiry(link.get('expires_at'), now)}",
             f"Enlace (tócalo para copiarlo):\n<code>{esc(url)}</code>"]
    kb = []
    if len(url) <= COPY_TEXT_MAX:
        kb.append([_copy_btn("📋 Copiar enlace", url)])
    kb.append([_btn("Revocar enlace", f"r|{link['id']}")])
    return View("\n".join(lines), kb)


# ---------- Asistente /codigo ----------

@dataclass
class CodeWizard:
    id: str
    user_id: int
    ttl: int
    max_downloads: int
    step: str = "project"
    projects: list = field(default_factory=list)
    project: dict | None = None
    branches: list = field(default_factory=list)
    branches_truncated: bool = False
    tags: list | None = None
    tags_truncated: bool = False
    results: list = field(default_factory=list)
    results_truncated: bool = False
    mode: str = "branches"  # branches | tags | search
    query: str = ""
    ref: str = ""
    sha: str = ""
    fmt: str = "zip"
    what: str = "code"
    page: int = 0
    busy: bool = False
    deadline: float = 0.0
    chat_id: int | None = None
    message_id: int | None = None


class CodeStore(WizardStore):
    def create(self, user_id, ttl, max_downloads) -> CodeWizard:
        self._purge()
        while len(self._items) >= self.max_items:
            del self._items[min(self._items, key=lambda k: self._items[k].deadline)]
        w = CodeWizard(secrets.token_hex(4), user_id, ttl, max_downloads)
        w.deadline = self.clock() + self.ttl
        self._items[w.id] = w
        return w

    def find_search(self, user_id):
        """Asistente del usuario que espera el texto de búsqueda (el más reciente), o None si venció."""
        self._purge()
        found = [w for w in self._items.values()
                 if isinstance(w, CodeWizard) and w.user_id == user_id and w.step == "search" and not w.busy]
        return max(found, key=lambda w: w.deadline) if found else None


def set_catalog(w, projects):
    w.projects = list(projects)
    w.step = "project"
    w.page = 0


def _default_first(refs):
    return sorted(refs, key=lambda r: not r.get("default"))


def set_branches(w, result):
    w.busy = False
    w.branches = _default_first(result["refs"])
    w.branches_truncated = bool(result.get("truncated"))
    w.tags, w.mode, w.step, w.page = None, "branches", "ref", 0


def set_tags(w, result):
    w.busy = False
    w.tags = list(result["refs"])
    w.tags_truncated = bool(result.get("truncated"))
    w.mode, w.step, w.page = "tags", "ref", 0


def set_results(w, result):
    w.busy = False
    w.results = _default_first(result["refs"])
    w.results_truncated = bool(result.get("truncated"))
    w.mode, w.step, w.page = "search", "ref", 0


def _ref_list(w):
    return {"branches": w.branches, "tags": w.tags or [], "search": w.results}[w.mode]


def sha8(sha) -> str:
    return sha[:8] if sha else "?"


def apply_search(w, text) -> Step:
    if w.step != "search" or w.busy:
        return Step("invalid")
    q = sanitize_search(text)
    if not q:
        return Step("invalid", f"Texto no válido. {SEARCH_HINT}")
    w.query = q
    w.busy = True
    return Step("fetch_search")


_CODE_BACK = {"ref": "project", "format": "ref", "what": "format", "confirm": "what", "search": "ref"}


def advance_code(w, action, arg) -> Step:
    if action == "x":
        return Step("cancel")
    if w.busy:
        return Step("invalid", "Espera, estoy consultando.")
    if action == "p":
        if w.step != "project" or arg is None or arg >= len(w.projects):
            return Step("invalid", "Opción no válida.")
        w.project = w.projects[arg]
        w.busy = True
        return Step("fetch_branches")
    if action == "r":
        lst = _ref_list(w)
        if w.step != "ref" or arg is None or arg >= len(lst):
            return Step("invalid", "Opción no válida.")
        w.ref, w.sha = lst[arg]["name"], lst[arg].get("sha", "")
        w.step, w.page = "format", 0
        return Step("view")
    if action == "pg":
        total = len(w.projects) if w.step == "project" else len(_ref_list(w)) if w.step == "ref" else None
        if total is None or arg is None or arg * PAGE_SIZE >= max(total, 1):
            return Step("invalid", "Página no válida.")
        w.page = arg
        return Step("view")
    if action in ("tg", "br", "q"):
        if w.step != "ref":
            return Step("invalid", "Opción no válida.")
        if action == "br":
            w.mode, w.page = "branches", 0
            return Step("view")
        if action == "tg":
            if w.tags is None:
                w.busy = True
                return Step("fetch_tags")
            w.mode, w.page = "tags", 0
            return Step("view")
        w.step = "search"
        return Step("search_prompt")
    if action == "f":
        if w.step != "format" or arg not in (0, 1):
            return Step("invalid", "Opción no válida.")
        w.fmt = ("zip", "tar.gz")[arg]
        w.step = "what"
        return Step("view")
    if action == "wh":
        has_db = bool(w.project and w.project.get("has_db"))
        if w.step != "what" or arg not in (0, 1, 2) or (arg and not has_db):
            return Step("invalid", "Opción no válida.")
        w.what = ("code", "db", "both")[arg]
        w.step = "confirm"
        return Step("view")
    if action == "t":
        if w.step != "confirm" or arg not in TTL_CHOICES:
            return Step("invalid", "Opción no válida.")
        w.ttl = arg
        return Step("view")
    if action == "m":
        if w.step != "confirm" or arg not in MAX_CHOICES:
            return Step("invalid", "Opción no válida.")
        w.max_downloads = arg
        return Step("view")
    if action == "b":
        prev = _CODE_BACK.get(w.step)
        if prev is None:
            return Step("cancel")
        if w.step == "ref":
            w.project, w.tags = None, None
        w.step, w.page = prev, 0
        return Step("view")
    if action == "go":
        return Step("run") if w.step == "confirm" else Step("invalid", "Opción no válida.")
    return Step("invalid", "Opción no válida.")


def build_package_request(w) -> PackageRequest:
    db = None
    if w.what in ("db", "both"):
        d = w.project["db"]
        db = (d["server"], d["container"], d["db"])
    return PackageRequest(w.user_id, w.project["name"], w.ref, w.sha, w.fmt, w.what, w.ttl, w.max_downloads, db)


def _kpaged(items, labels, action, page, wid):
    start = page * PAGE_SIZE
    rows = [[_btn(labels[i], f"k|{wid}|{action}|{i}")] for i in range(start, min(start + PAGE_SIZE, len(items)))]
    nav = []
    if page > 0:
        nav.append(_btn("◀", f"k|{wid}|pg|{page - 1}"))
    if (page + 1) * PAGE_SIZE < len(items):
        nav.append(_btn("▶", f"k|{wid}|pg|{page + 1}"))
    if nav:
        rows.append(nav)
    return rows


def _kfooter(w, back=True):
    row = [_btn("Atrás", f"k|{w.id}|b|")] if back else []
    row.append(_btn("Cancelar", f"k|{w.id}|x|"))
    return [row]


def _what_line(w):
    return WHAT_LABEL[w.what]


def render_code(w) -> View:
    if w.step == "project":
        if not w.projects:
            return View("No hay proyectos en el catálogo.", _kfooter(w, back=False))
        labels = [p["name"] + (" · BD" if p.get("has_db") else "") for p in w.projects]
        return View("Elige el proyecto:", _kpaged(w.projects, labels, "p", w.page, w.id) + _kfooter(w, back=False))
    head = f"Proyecto: <b>{esc(w.project['name'])}</b>\n"
    if w.step == "search":
        return View(head + "Escribe en el siguiente mensaje el texto de la rama a buscar "
                    f"(vence en {WIZARD_TTL // 60} min). {SEARCH_HINT}", _kfooter(w))
    if w.step == "ref":
        title = {"branches": "Ramas", "tags": "Tags", "search": f"Ramas que coinciden con «{esc(w.query)}»"}[w.mode]
        lst = _ref_list(w)
        truncated = {"branches": w.branches_truncated, "tags": w.tags_truncated, "search": w.results_truncated}[w.mode]
        text = head + f"{title}:\n" + ("" if lst else "Sin resultados.\n") + "Elige la referencia:"
        if truncated:
            text += "\nHay más; usa 🔎 Buscar."
        labels = [r["name"] + (" ★" if r.get("default") else "") for r in lst]
        rows = _kpaged(lst, labels, "r", w.page, w.id)
        toggle = _btn("🌿 Ramas", f"k|{w.id}|br|") if w.mode != "branches" else _btn("🏷 Tags", f"k|{w.id}|tg|")
        rows.append([toggle, _btn("🔎 Buscar", f"k|{w.id}|q|")])
        return View(text, rows + _kfooter(w))
    head += f"Referencia: <b>{esc(w.ref)}</b> (<code>{esc(sha8(w.sha))}</code>)\n"
    if w.step == "format":
        row = [_btn(("• " if w.fmt == name else "") + name, f"k|{w.id}|f|{i}") for i, name in enumerate(("zip", "tar.gz"))]
        return View(head + "Formato del código:", [row] + _kfooter(w))
    if w.step == "what":
        rows = [[_btn("📦 Código", f"k|{w.id}|wh|0")]]
        if w.project.get("has_db"):
            rows += [[_btn("🗄 Base de datos", f"k|{w.id}|wh|1")], [_btn("📦🗄 Ambos", f"k|{w.id}|wh|2")]]
        return View(head + "¿Qué entregar?", rows + _kfooter(w))
    text = (
        "Confirma la entrega:\n"
        f"Proyecto: <b>{esc(w.project['name'])}@{esc(w.ref)}</b> (<code>{esc(sha8(w.sha))}</code>)\n"
        f"Entrega: <b>{_what_line(w)}</b>\n"
    )
    if w.what in ("code", "both"):
        text += f"Formato: <b>{esc(w.fmt)}</b>\n"
    if w.what in ("db", "both"):
        d = w.project["db"]
        text += f"Base de datos (destino exacto): <code>{esc(d['server'])}/{esc(d['container'])}/{esc(d['db'])}</code>\n"
    text += (
        f"Vigencia del enlace: <b>{format_duration(w.ttl)}</b>\n"
        f"Descargas máximas por archivo: <b>{w.max_downloads}</b>"
    )
    ttl_row = [_btn(("• " if t == w.ttl else "") + format_duration(t), f"k|{w.id}|t|{t}") for t in TTL_CHOICES]
    max_row = [_btn(("• " if n == w.max_downloads else "") + str(n), f"k|{w.id}|m|{n}") for n in MAX_CHOICES]
    return View(text, [ttl_row, max_row, [_btn("Generar entrega", f"k|{w.id}|go|")]] + _kfooter(w))


def render_code_error(w, text) -> View:
    view = render_code(w)
    return View(f"{text}\n\n{view.text}", view.keyboard)
