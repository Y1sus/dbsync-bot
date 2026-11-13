<!--
  contracts/gateway-api.md — Contrato HTTP del gateway (interno y público)
  Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
-->
# Gateway: contrato HTTP

Dos listeners independientes en el mismo proceso. Los esquemas JSON están en
`link.schema.json` (`$defs`).

## API interna `:8081` (solo red de Docker, nunca publicada ni detrás de nginx)

Todas las rutas exigen `Authorization: Bearer <token>` (comparación en tiempo
constante). Sin token o token incorrecto: `401` con `Error.unauthorized`.

| Método y ruta | Cuerpo | Respuesta |
|---|---|---|
| `POST /v1/links` | `LinkCreateRequest` | `201 LinkCreated`. `400 invalid_request`, `404 file_not_found` |
| `GET /v1/links?state=active\|all` | | `200 LinkList` (default `active`, más recientes primero) |
| `GET /v1/links/{id}` | | `200 LinkRecord` o `404 not_found` |
| `DELETE /v1/links/{id}[?delete_file=1]` | | `204`. Revoca el enlace; con `delete_file=1` borra también el dump si ningún otro enlace activo lo usa |
| `GET /v1/health` | | `200 Health` |

Validaciones de `POST /v1/links`: `relpath` debe resolver (con `realpath`) dentro
de la raíz de dumps, ser un archivo regular `*.sql.gz` y no ser un enlace
simbólico. El gateway toma `size_bytes` del disco. Códigos de error:

| Caso | Respuesta |
|---|---|
| Campo faltante o inválido, campos extra, `..`, componentes ocultos (`.locks`), `\`, NUL, extensión distinta de `.sql.gz` (incluye `*.part`) | `400 invalid_request` |
| Archivo vacío | `400 invalid_request` |
| No existe, enlace simbólico o no regular | `404 file_not_found` |

## Rutas públicas `:8080` (las únicas que nginx publica, en `dumps.synkra.app`)

| Método y ruta | Comportamiento |
|---|---|
| `GET /d/{token}` | Página intermedia (nombre, tamaño, sha256, expiración, descargas restantes) con un botón que hace `POST`. **No cuenta como descarga.** |
| `HEAD /d/{token}` | `200` sin cuerpo, sin efectos |
| `POST /d/{token}` | Abre (o reutiliza) una sesión de descarga y responde `303` a `/d/{token}/f/{sesion}` |
| `GET /d/{token}/f/{sesion}` | Entrega el archivo con `Range` (reanudable). Cabeceras: `Content-Disposition: attachment`, `Accept-Ranges: bytes`, `X-Content-Type-Options: nosniff` |
| `HEAD /d/{token}/f/{sesion}` | Cabeceras sin cuerpo, sin efectos |

Token desconocido: `404`. Token válido pero expirado, agotado o revocado: `410`.
La URL de sesión responde `410` mientras su fila existe y está vencida, y `404` cuando
la limpieza ya la purgó.
Todas las respuestas llevan `Cache-Control: no-store`, `X-Robots-Tag: noindex` y
`Referrer-Policy: no-referrer`.

### Regla de conteo (un enlace de 1 descarga sobrevive a los previews)

- Solo `POST` crea sesión; los previews de WhatsApp y Slack hacen `GET`/`HEAD` y no consumen nada.
- Una descarga **se cuenta una sola vez, la primera vez que sale el último byte del
  archivo** (`Range` incluido) sin error de transporte: `downloads_completed += 1`.
  Gestores de descarga y navegadores móviles piden el último rango primero o en
  paralelo, por eso la sesión **no se cierra** al contar: sigue sirviendo `Range`
  hasta que vence su TTL (2 h desde su inicio).
- Un nuevo `POST` con una sesión abierta (contada o no) la reutiliza mientras el enlace
  no haya expirado y `downloads_completed < max_downloads`. Pasado `expires_at`, agotado
  o revocado, `POST` responde `410` aunque exista una sesión abierta: nadie obtiene la URL
  de sesión de nuevo fuera de la ventana elegida. Quien ya tiene la URL de sesión sigue
  recibiendo `Range` hasta que vence su TTL (2 h desde su inicio).
- Revocar un enlace cierra sus sesiones: los rangos pendientes responden `410`.
- Una sonda `Range: bytes=-1` hecha por quien ya tiene la URL de sesión cuenta como
  descarga completa (el último byte salió). Es consecuencia deliberada de la regla.
- Solo se aceptan rangos simples (`bytes=a-b`, `bytes=a-`, `bytes=-n`). Un rango
  inválido o fuera de límites devuelve `416` con `Content-Range: bytes */<size>`.
  Un `Range` con varios rangos se ignora y se responde `200` con el archivo completo.
- El gateway implementa su propio streaming con rangos (no `FileResponse`) para saber
  cuándo salió el último byte.

## Ciclo de vida y auditoría

- Tarea periódica (60 s): marca `expired`/`exhausted`, borra filas con más de 7 días
  de cerradas y elimina dumps con más de `GATEWAY_RETENTION_HOURS` (default 24) o, si
  el tamaño total supera `GATEWAY_MAX_DUMPS_GB`, los más viejos primero.
- Alcance de la limpieza: **solo** archivos finales `*.sql.gz` regulares. Nunca `*.part`
  (el runner puede estar escribiéndolos), nunca `.locks/`, nunca enlaces simbólicos.
  Nunca se borra un archivo referenciado por un enlace activo o por una sesión abierta,
  ni siquiera por exceso de cuota.
- `DATA_DIR/audit.jsonl`: una línea JSON por evento (`link_created`, `link_revoked`,
  `landing_view`, `download_started`, `download_completed`, `download_aborted`,
  `file_deleted`, `link_expired`) con `ts`, `link_id`, `ip` (`X-Real-IP`), `ua` y `bytes`.
  Nunca se registra el token, solo el `id`.

## Entorno del gateway

`GATEWAY_DUMPS_ROOT`, `GATEWAY_DATA_DIR`, `GATEWAY_PUBLIC_BASE_URL`,
`GATEWAY_API_TOKEN_FILE`, `GATEWAY_PUBLIC_PORT` (8080), `GATEWAY_INTERNAL_PORT` (8081),
`GATEWAY_RETENTION_HOURS` (24), `GATEWAY_MAX_DUMPS_GB` (10; en el VPS compartido se fija
cerca de 8), `GATEWAY_BIND_HOST` (`0.0.0.0`; en Docker el aislamiento lo da la red),
`GATEWAY_CLEANUP_INTERVAL_SECONDS` (60). El evento `link_expired` lleva un campo extra
`reason` (`expired` o `exhausted`).

## Entorno del bot

`BOT_TOKEN_FILE`, `BOT_ALLOWED_USER_IDS` (coma), `BOT_RUNNER_PATH`, `BOT_GATEWAY_URL`,
`BOT_GATEWAY_TOKEN_FILE`, `BOT_DATA_DIR`, `BOT_DEFAULT_TTL_SECONDS` (3600),
`BOT_DEFAULT_MAX_DOWNLOADS` (1), `BOT_JOB_TIMEOUT_SECONDS` (1800), `BOT_MAX_JOBS` (1).
`BOT_DATA_DIR` vale `/data` por defecto. `BOT_TELEGRAM_API_BASE` es opcional y solo para
pruebas (apunta a un Telegram falso). El polling pide solo `message` y `callback_query`.

Códigos de error locales del bot, que no vienen del runner: `E_TIMEOUT`, `E_BAD_OUTPUT`,
`E_RUNNER_START` y `E_CANCELLED`. En aiogram 3.31 `start_polling(drop_pending_updates=True)`
se ignora, por eso el arranque llama a `bot.delete_webhook(drop_pending_updates=True)`.

## Restricciones de despliegue (fase 5)

- El puerto público del gateway se publica como `127.0.0.1:<PUERTO>:8080`; nginx es el
  único cliente, y por eso `X-Real-IP` (que nginx fija) es confiable. **El `8081` no se
  publica nunca**: solo es alcanzable dentro de la red de Docker.
- El gateway y el runner corren con el mismo UID (los dumps son 0600 y el directorio 0700).

## Notas para el bot

- Al enviar el enlace, el bot deshabilita la vista previa
  (`link_preview_options.is_disabled=true`): si no, los servidores de Telegram piden la
  página intermedia (ruido en `landing_view` y fuga de la URL a un tercero).
- El bot nunca imprime ni registra el token del bot ni el del gateway.
