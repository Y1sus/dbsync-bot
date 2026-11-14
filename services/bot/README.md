<!--
  services/bot/README.md — Bot de Telegram de dbsync-bot
  Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
-->
# bot

Orquesta el flujo: recibe comandos por Telegram (long polling, sin puertos), invoca
el runner (`services/runner`), crea el enlace en el gateway (`contracts/gateway-api.md`)
y responde con la URL. Solo atiende a los ids de `BOT_ALLOWED_USER_IDS`.

## Comandos

| Comando | Qué hace |
|---|---|
| `/start`, `/help` | ayuda |
| `/servers` | servidores configurados |
| `/dump` | asistente con botones: servidor, contenedor, base, confirmación (TTL 1/4/24 h, descargas 1/3/5) |
| `/dump <servidor> <contenedor> <db> [ttl=2h] [max=3]` | forma directa (`ttl` de 60s a 7d, `max` de 1 a 20) |
| `/links` | enlaces activos, con botón para revocar; revocar o vencer elimina el dump |
| `/revoke <id>` | revoca el enlace y el gateway borra el dump (`DELETE /v1/links/{id}` sin `keep_file`) |
| `/status` | job en curso o libre |

El estado del asistente vive en memoria con un id de 8 hex (el `callback_data` de
Telegram admite 64 bytes) y expira a los 10 minutos sin uso. Los botones llevan
índices, nunca nombres.

## Entorno

| Variable | Default | Uso |
|---|---|---|
| `BOT_TOKEN_FILE` | obligatoria | archivo con el token del bot |
| `BOT_ALLOWED_USER_IDS` | obligatoria | ids de Telegram separados por coma |
| `BOT_GATEWAY_URL` | obligatoria | API interna del gateway (`http://gateway:8081`) |
| `BOT_GATEWAY_TOKEN_FILE` | obligatoria | archivo con el bearer token del gateway |
| `BOT_RUNNER_PATH` | `/app/runner/runner.sh` | ruta del runner |
| `BOT_DATA_DIR` | `/data` | `audit.jsonl` |
| `BOT_DEFAULT_TTL_SECONDS` | `3600` | TTL por defecto (60 a 604800) |
| `BOT_DEFAULT_MAX_DOWNLOADS` | `1` | descargas por defecto (1 a 20) |
| `BOT_JOB_TIMEOUT_SECONDS` | `1800` | límite de cada llamada al runner |
| `BOT_MAX_JOBS` | `1` | dumps simultáneos; el resto espera en cola con aviso |
| `BOT_TELEGRAM_API_BASE` | (vacío) | solo pruebas: apunta a un servidor Telegram falso |

El runner hereda el entorno del bot (`DBSYNC_SERVERS_DIR`, `DBSYNC_DUMPS_DIR`,
`DBSYNC_KNOWN_HOSTS`, ...). Si falta algo obligatorio, el bot sale con código 2 y un
mensaje claro, sin traceback.

## Diseño

- Lógica pura sin aiogram: `config`, `durations`, `redact`, `audit`, `auth`,
  `runner_client`, `gateway_client`, `jobs`, `flow`. Solo `handlers.py` y `main.py`
  importan aiogram.
- Autorización: middleware sobre `Update`. Descarta en silencio todo lo que no sea un
  mensaje nuevo o un callback de un usuario permitido en chat privado (extraños, grupos,
  mensajes editados, canales, inline). Audita `user_id`, tipo y motivo, nunca el
  contenido, con límite de una línea por usuario por minuto.
- Runner: `create_subprocess_exec` con lista de argumentos, `start_new_session=True`.
  Al vencer el timeout, cancelar o apagar: SIGTERM al grupo, y SIGKILL al grupo
  (`os.killpg`) para cosechar ssh. Valida `job-result.schema.json` a mano.
  Códigos locales extra: `E_TIMEOUT`, `E_BAD_OUTPUT`, `E_RUNNER_START`, `E_CANCELLED`.
- Si el dump existe pero el enlace falla, el mensaje lo dice.
- Enlace que se copia, no se abre: la URL va solo dentro de `<code>` (tocar monoespaciado
  copia) y un botón `copy_text` ("📋 Copiar enlace", máx. 256 caracteres; si la URL es más
  larga se omite el botón). Nunca botones `url` ni la URL como texto plano ni en otro mensaje.
  `flow.Button` lleva `copy_text` como dato; `handlers._button` lo traduce a `CopyTextButton`.
- Aviso al terminar (`notify.py`, sin aiogram): editar no genera push, así que el resultado
  (éxito, fallo del runner, timeout, cancelación, enlace fallido) sale como mensaje NUEVO con
  `disable_notification=False` y primer renglón de resumen ("✅ Dump listo · servidor/base ·
  tamaño" o "❌ Dump falló · código"). El mensaje "Generando dump…" se edita a un estado
  final corto sin enlace. La cola y el inicio de un job en cola siguen siendo ediciones.
  El job corre en una tarea de fondo (`JobManager.spawn`): sigue aunque el usuario salga del
  chat. Si el aviso nuevo falla (red, bot bloqueado) se registra solo el tipo de error, el
  progreso igual se cierra corto y el job no se rompe. El enlace no se reenvía por otra vía
  (`/links` no muestra la URL): hay que relanzar `/dump`.
- Revocar (botón o `/revoke`) no pasa `keep_file`: el gateway borra el dump. El texto dice
  "Enlace revocado; el dump se eliminará" porque una sesión abierta o una descarga en curso
  pueden retrasar el borrado unos minutos.
- Seguridad de logs: `redact.py` (filtro en el handler raíz) enmascara el token del bot
  (`/bot<token>/`), el del gateway y cualquier `Bearer`, también en tracebacks.
- Auditoría: `BOT_DATA_DIR/audit.jsonl` (0600) con `command`, `job_start`, `job_end`,
  `link_created`, `link_failed`, `link_revoked`, `update_rejected`. Sin URLs ni tokens.
- Todos los mensajes salen con `parse_mode=HTML`, `html.escape` y vista previa
  deshabilitada (`link_preview_options.is_disabled=true`).

## Pruebas y eval

```bash
cd services/bot
PY=../../.venv/bin/python
$PY -m unittest discover -s tests -t .     # puerta: offline, sin aiogram, ~1.5 s
$PY evals/e2e_bot.py                        # eval e2e: bot real vs Telegram, gateway y runner falsos
```

El eval arranca `python -m bot` como subproceso con `BOT_TELEGRAM_API_BASE` hacia
`evals/fake_telegram.py`; exit 0 solo si pasan el 100 % de las comprobaciones.

## Docker

Contexto de build en la raíz del repo (copia `services/runner/` y `services/bot/`):

```bash
docker build -f services/bot/Dockerfile -t dbsync-bot .
```

Corre como UID 10001; el gateway y el runner deben compartir UID con los dumps (0600).
