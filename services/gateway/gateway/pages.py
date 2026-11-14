# =============================================================================
# services/gateway/gateway/pages.py — Páginas públicas (HTML y CSS propios, sin JavaScript) y su CSP con hash
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import base64
import hashlib
import html

CSS = (
    ":root{color-scheme:light dark;--bg:#f2f5fa;--card:#ffffff;--fg:#141b2b;--muted:#46506a;--line:#d3dae8;"
    "--code:#eef2f9;--accent:#1747c4;--accent-h:#123aa0;--on-accent:#ffffff;--ring:#1747c4;"
    "--ok-bg:#dff5e8;--ok-fg:#0a5128;--warn-bg:#fdf0d0;--warn-fg:#713f00;"
    "--err-bg:#fde3e3;--err-fg:#8a1b1b;--info-bg:#e3e9f6;--info-fg:#2a3550}"
    "@media (prefers-color-scheme:dark){:root{--bg:#0d1220;--card:#171e2e;--fg:#e8edf6;--muted:#b4bfd6;"
    "--line:#2d3852;--code:#0f1524;--accent:#7aa7ff;--accent-h:#a3c0ff;--on-accent:#06122c;--ring:#a3c0ff;"
    "--ok-bg:#123524;--ok-fg:#86e6ab;--warn-bg:#3a2a06;--warn-fg:#f6cd78;"
    "--err-bg:#411a1a;--err-fg:#ffb0b0;--info-bg:#232d45;--info-fg:#c8d3ea}}"
    "*{box-sizing:border-box}"
    "html{-webkit-text-size-adjust:100%}"
    "body{margin:0;min-height:100vh;display:flex;background:var(--bg);color:var(--fg);"
    "font:16px/1.55 system-ui,-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif}"
    ".wrap{margin:auto;width:100%;max-width:34rem;padding:1rem}"
    ".card{background:var(--card);border:1px solid var(--line);border-radius:1.25rem;padding:1.5rem 1.25rem;"
    "box-shadow:0 1px 2px rgba(15,23,42,.06),0 10px 28px rgba(15,23,42,.08)}"
    ".hero{display:flex;align-items:center;justify-content:center;width:3.5rem;height:3.5rem;"
    "border-radius:1rem;margin-bottom:1rem}"
    ".hero .ico{width:1.75rem;height:1.75rem}"
    ".ico{width:1.1em;height:1.1em;flex:none;fill:none;stroke:currentColor;stroke-width:2;"
    "stroke-linecap:round;stroke-linejoin:round}"
    ".badge{display:inline-flex;align-items:center;gap:.4rem;padding:.2rem .7rem .2rem .55rem;"
    "border:1px solid currentColor;border-radius:999px;font-size:.875rem;font-weight:650}"
    ".t-ok{background:var(--ok-bg);color:var(--ok-fg)}"
    ".t-warn{background:var(--warn-bg);color:var(--warn-fg)}"
    ".t-err{background:var(--err-bg);color:var(--err-fg)}"
    ".t-info{background:var(--info-bg);color:var(--info-fg)}"
    "h1{margin:.9rem 0 .4rem;font-size:1.5rem;line-height:1.25;letter-spacing:-.01em}"
    "p{margin:0 0 .75rem}"
    ".lead{color:var(--muted)}"
    ".hint{margin:1rem 0 0;padding-top:1rem;border-top:1px solid var(--line);color:var(--muted)}"
    ".meta{margin:1.25rem 0 1.5rem;display:grid;gap:.9rem}"
    ".meta dt{margin:0 0 .2rem;font-size:.8125rem;font-weight:650;letter-spacing:.04em;"
    "text-transform:uppercase;color:var(--muted)}"
    ".meta dd{margin:0;overflow-wrap:anywhere}"
    ".mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace;"
    "font-size:.9rem;background:var(--code);border:1px solid var(--line);border-radius:.5rem;"
    "padding:.15rem .4rem;overflow-wrap:anywhere;word-break:break-all}"
    ".rest{display:block;color:var(--muted);font-size:.9375rem}"
    ".btn{display:flex;align-items:center;justify-content:center;gap:.6rem;width:100%;min-height:3.5rem;"
    "padding:.9rem 1.25rem;border:0;border-radius:.9rem;background:var(--accent);color:var(--on-accent);"
    "font:inherit;font-size:1.125rem;font-weight:650;cursor:pointer}"
    ".btn .ico{width:1.35rem;height:1.35rem}"
    ".btn:hover{background:var(--accent-h)}"
    ".btn:focus-visible{outline:3px solid var(--ring);outline-offset:3px}"
    ".files{margin:1.25rem 0 0;padding:0;list-style:none;display:grid;gap:.9rem}"
    ".file{border:1px solid var(--line);border-radius:1rem;padding:1rem}"
    ".file-head{display:flex;align-items:center;gap:.75rem;margin-bottom:.75rem}"
    ".kind{display:flex;align-items:center;justify-content:center;flex:none;width:2.5rem;height:2.5rem;"
    "border-radius:.7rem}"
    ".kind .ico{width:1.4rem;height:1.4rem}"
    ".kindname{font-size:.8125rem;font-weight:650;letter-spacing:.04em;text-transform:uppercase;"
    "color:var(--muted)}"
    ".file-name{min-width:0;overflow-wrap:anywhere}"
    ".file .meta{margin:0 0 1rem}"
    ".when{margin:1.25rem 0 0;color:var(--muted)}"
    ".foot{margin:1rem .5rem 0;text-align:center;font-size:.875rem;color:var(--muted)}"
    "@media (min-width:34rem){.card{padding:2rem}h1{font-size:1.75rem}}"
    "@media (forced-colors:active){.btn{border:2px solid ButtonText}}"
)

CSS_HASH = base64.b64encode(hashlib.sha256(CSS.encode("utf-8")).digest()).decode("ascii")
CSP = (
    f"default-src 'none'; style-src 'sha256-{CSS_HASH}'; img-src data:; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)

DESCRIPTION = "Enlace de descarga temporal y privado."
LANDING_DESCRIPTION = "Tus archivos están listos."

_ICONS = {
    "download": '<path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 20h16"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>',
    "ban": '<circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/>',
    "alert": '<path d="M12 3l10 18H2z"/><path d="M12 10v4"/><path d="M12 17.5v.01"/>',
    "filex": '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M9.5 12.5l5 5"/><path d="M14.5 12.5l-5 5"/>',
    "search": '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.2-4.2"/>',
    "code": '<path d="M8 8l-4 4 4 4"/><path d="M16 8l4 4-4 4"/><path d="M13.5 5l-3 14"/>',
    "database": '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/>'
                '<path d="M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>',
    "lock": '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/>',
}

# clave: (estado HTTP, título, tono, insignia, icono, encabezado, texto, sugerencia)
_STATIC = {
    "expired": (410, "Enlace expirado", "warn", "Expirado", "clock", "Este enlace ya expiró",
                "El tiempo para descargar este archivo terminó.",
                "Pide un enlace nuevo a quien te lo compartió."),
    "exhausted": (410, "Enlace agotado", "info", "Agotado", "check", "Este enlace ya se usó",
                  "Alcanzó su límite de descargas y ya no está disponible.",
                  "Si necesitas el archivo otra vez, pide un enlace nuevo."),
    "filedone": (410, "Archivo agotado", "info", "Agotado", "check", "Este archivo ya se descargó",
                 "Alcanzó su límite de descargas. Los demás archivos del enlace siguen disponibles.",
                 "Vuelve a la página del enlace para descargar el resto."),
    "revoked": (410, "Enlace revocado", "err", "Revocado", "ban", "Este enlace fue revocado",
                "Quien lo compartió lo desactivó, así que ya no se puede descargar.",
                "Si lo necesitas, pide un enlace nuevo."),
    "session": (410, "Sesión vencida", "warn", "Sesión vencida", "clock", "La sesión de descarga venció",
                "Por seguridad, cada sesión de descarga dura un tiempo limitado.",
                "Abre de nuevo el enlace original; si sigue vigente podrás continuar."),
    "budget": (410, "Límite de descarga", "warn", "Límite alcanzado", "ban", "Esta descarga ya envió todo lo permitido",
               "Cada sesión puede transferir como máximo el doble del tamaño del archivo.",
               "Si aún lo necesitas, pide un enlace nuevo a quien te lo compartió."),
    "file": (410, "Archivo no disponible", "warn", "No disponible", "filex", "El archivo ya no está disponible",
             "Fue eliminado del servidor y no se puede recuperar desde este enlace.",
             "Pide un enlace nuevo a quien te lo compartió."),
    "notfound": (404, "Enlace no encontrado", "info", "No encontrado", "search", "No encontramos este enlace",
                 "Puede estar incompleto o ser incorrecto.",
                 "Copia el enlace completo tal como te lo enviaron."),
    "method": (405, "Método no permitido", "warn", "No permitido", "ban", "Esta acción no está permitida",
               "Esta dirección no admite ese método.",
               "Abre el enlace desde tu navegador."),
    "busy": (503, "Servidor ocupado", "warn", "Servidor ocupado", "clock", "Estamos ocupados",
             "Hay muchas descargas en curso en este momento.",
             "Espera unos segundos y vuelve a intentarlo."),
    "budget": (410, "Límite de la sesión", "warn", "Límite alcanzado", "ban",
               "Esta descarga ya envió todo lo permitido",
               "Esta sesión alcanzó el máximo de datos que puede entregar de este archivo.",
               "Si no pudiste guardarlo, pide un enlace nuevo a quien te lo compartió."),
    "forbidden": (403, "Solicitud no permitida", "err", "Rechazada", "lock", "No pudimos verificar la solicitud",
                  "Por seguridad solo aceptamos descargas iniciadas desde la propia página del enlace.",
                  "Abre el enlace directamente en tu navegador y usa el botón Descargar."),
    "bad": (400, "Solicitud no válida", "warn", "No válida", "alert", "No pudimos procesar la solicitud",
            "La solicitud no es válida.",
            "Abre el enlace de nuevo desde tu navegador."),
    "error": (500, "Error del servidor", "err", "Error", "alert", "Algo salió mal",
              "Ocurrió un error inesperado de nuestro lado.",
              "Intenta de nuevo en unos minutos."),
}


def _icon(name):
    return f'<svg class="ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false">{_ICONS[name]}</svg>'


def _doc(title, inner, description=DESCRIPTION):
    return (
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        '<meta name="color-scheme" content="light dark">'
        f'<meta name="description" content="{html.escape(description)}">'
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f'<body><main class="wrap">{inner}'
        '<p class="foot">Enlace temporal y privado. No compartas esta dirección.</p></main></body></html>'
    )


def _head(tone, icon, badge):
    return (
        f'<div class="hero t-{tone}">{_icon(icon)}</div>'
        f'<span class="badge t-{tone}">{_icon(icon)}{html.escape(badge)}</span>'
    )


def human_size(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def remaining_text(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "menos de 1 min"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


KINDS = {"db": ("database", "Base de datos", "info"), "code": ("code", "Código", "ok")}


def _file_item(token, item, available):
    esc = html.escape
    icon, kind_label, tone = KINDS.get(item["kind"], KINDS["code"])
    left = item["downloads_left"]
    if left <= 0:
        state = ("info", "check", "Agotado")
    elif not available:
        state = ("warn", "filex", "No disponible")
    else:
        state = None
    sha = f'<code class="mono">{esc(item["sha256"])}</code>' if item["sha256"] else "No disponible"
    rows = [
        ("Tamaño", esc(human_size(item["size_bytes"]))),
        ("SHA-256", sha),
        ("Descargas restantes", esc(str(max(left, 0)))),
    ]
    meta = "".join(f"<div><dt>{esc(k)}</dt><dd>{v}</dd></div>" for k, v in rows)
    if state:
        action = f'<span class="badge t-{state[0]}">{_icon(state[1])}{state[2]}</span>'
    else:
        action = (
            f'<form method="post" action="/d/{esc(token)}/{int(item["index"])}">'
            f'<button class="btn" type="submit" aria-label="Descargar {esc(item["filename"])}">'
            f"{_icon('download')}Descargar</button></form>"
        )
    return (
        '<li class="file"><div class="file-head">'
        f'<div class="kind t-{tone}">{_icon(icon)}</div>'
        f'<div class="file-name"><div class="kindname">{esc(kind_label)}</div>'
        f'<code class="mono">{esc(item["filename"])}</code></div></div>'
        f'<dl class="meta">{meta}</dl>{action}</li>'
    )


def landing(token, items, expires_text, remaining):
    """Página de descarga. `items`: dicts con index, filename, kind, size_bytes, sha256, downloads_left, available."""
    esc = html.escape
    heading = "Tu archivo está listo" if len(items) == 1 else "Tus archivos están listos"
    lis = "".join(_file_item(token, item, item["available"]) for item in items)
    inner = (
        '<section class="card">'
        f'{_head("ok", "download", "Disponible")}'
        f"<h1>{heading}</h1>"
        '<p class="lead">Descárgalos antes de que expiren. Si una descarga se interrumpe, puedes volver a pulsar su botón.</p>'
        f'<p class="when">Expira {esc(expires_text)}<span class="rest">expira en {esc(remaining_text(remaining))}</span></p>'
        f'<ul class="files">{lis}</ul></section>'
    )
    return 200, _doc("Descarga lista", inner, LANDING_DESCRIPTION)


def render(kind, retry_after=None):
    """Devuelve (estado HTTP, HTML) de una página de estado."""
    if kind == "limited":
        status, title, tone, badge, icon = 429, "Demasiadas solicitudes", "warn", "Pausa temporal", "clock"
        heading = "Demasiadas solicitudes"
        text = "Detectamos muchos intentos fallidos y pausamos tu acceso por un rato."
        wait = remaining_text(retry_after or 0)
        hint = f"Vuelve a intentarlo en {wait}."
    else:
        status, title, tone, badge, icon, heading, text, hint = _STATIC[kind]
    esc = html.escape
    inner = (
        '<section class="card">'
        f"{_head(tone, icon, badge)}<h1>{esc(heading)}</h1>"
        f'<p class="lead">{esc(text)}</p><p class="hint">{esc(hint)}</p></section>'
    )
    return status, _doc(title, inner)
