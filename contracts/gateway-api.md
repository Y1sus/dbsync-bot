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
| `DELETE /v1/links/{id}[?keep_file=1]` | | `204`. Revoca el enlace, cierra sus sesiones y **borra el dump** salvo que otro enlace vigente lo use o se pase `keep_file=1`. Es idempotente (revocar uno ya cerrado también intenta borrar el archivo). `404 not_found` si el id no existe |
| `GET /v1/health` | | `200 Health` |

Validaciones de `POST /v1/links`: `relpath` debe resolver (con `realpath`) dentro
de la raíz de dumps, ser un archivo regular `*.sql.gz` y no ser un enlace
simbólico. El gateway toma `size_bytes` del disco. Códigos de error:

| Caso | Respuesta |
|---|---|
| Campo faltante o inválido, campos extra, `..`, componentes ocultos (`.locks`), `\`, NUL, extensión distinta de `.sql.gz` (incluye `*.part`) | `400 invalid_request` |
| Archivo vacío | `400 invalid_request` |
| `relpath` que no tiene exactamente la forma `<servidor>/<archivo>.sql.gz` (anidado) | `400 invalid_request` |
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
- Una descarga **se cuenta una sola vez, cuando la unión de los bytes enviados en la
  sesión cubre el archivo completo** (`[0, size)`), sin importar el orden ni el número de
  rangos: `downloads_completed += 1`. Gestores de descarga y navegadores móviles piden el
  último rango primero o en paralelo, y una sonda como `Range: bytes=-1` **no cuenta**
  (cubre 1 byte). El gateway guarda por sesión los intervalos enviados (fusionados; se
  incluyen los bytes ya enviados de una conexión abortada) y cuenta al completarse la cobertura.
  La sesión **no se cierra** al contar: sigue sirviendo `Range` durante una gracia posterior
  (`GATEWAY_POST_DOWNLOAD_GRACE_SECONDS`, default 900) o hasta su vencimiento, lo que
  ocurra primero. Al contar, la sesión pasa a vencer en
  `min(vencimiento actual, instante del conteo + gracia)`; así el dump se puede borrar
  poco después de la descarga.
- **Presupuesto de bytes por sesión:** una sesión puede enviar como máximo `2 × size`
  bytes en total (cubre reanudaciones y solapes legítimos, no varias copias completas).
  Al abrir cada respuesta se reserva su longitud contra el presupuesto; si no cabe,
  `410` con una página propia ("Esta descarga ya envió todo lo permitido"). El límite es
  estricto (sin tolerancia). Los bytes reservados y no enviados (conexión abortada) se devuelven.
- **Vencimiento de la sesión:** `min(inicio + 7200 s, expires_at del enlace + gracia)`. Una
  descarga iniciada justo antes de expirar el enlace puede seguir hasta la gracia; no se
  abren ni reanudan sesiones indefinidamente después.
- Un nuevo `POST` con una sesión abierta (contada o no) la reutiliza mientras el enlace
  no haya expirado y `downloads_completed < max_downloads`. Pasado `expires_at`, agotado
  o revocado, `POST` responde `410` aunque exista una sesión abierta: nadie obtiene la URL
  de sesión de nuevo fuera de la ventana elegida. Quien ya tiene la URL de sesión sigue
  recibiendo `Range` hasta que vence la sesión (ver gracia arriba).
- Revocar un enlace cierra sus sesiones: los rangos pendientes responden `410` y **las
  descargas en curso se cortan** (el bucle de envío comprueba la revocación entre bloques).
- Solo se aceptan rangos simples (`bytes=a-b`, `bytes=a-`, `bytes=-n`). Un rango
  inválido o fuera de límites devuelve `416` con `Content-Range: bytes */<size>`.
  Un `Range` con varios rangos se ignora y se responde `200` con el archivo completo.
- El gateway implementa su propio streaming con rangos (no `FileResponse`) para saber
  cuándo salió el último byte.

## Ciclo de vida y auditoría

- **El dump se borra cuando su enlace termina** (caducidad, agotamiento o revocación
  manual), sin esperar la retención. La regla es **sin estado y se evalúa en cada pasada de
  limpieza** (no es un evento de transición: los eventos se pierden en un reinicio o una
  caída). Un archivo se borra cuando se cumple todo lo siguiente: su `relpath` tiene al
  menos una fila de enlace y **todas** son terminales (`expired`, `exhausted`, `revoked`);
  ninguna sesión está abierta (la gracia posterior al conteo cuenta como abierta); y ninguna
  descarga está en curso. Se evalúa bajo el mismo candado que usa `POST /v1/links`, y la
  ruta de revocación llama a esa misma función. El evento `file_deleted` lleva `reason`
  (`link_expired`, `link_exhausted`, `link_revoked`, `retention` o `quota`).
- Respaldo para dumps que nunca tuvieron enlace (p. ej. falló la creación): se borran
  tras `GATEWAY_RETENTION_HOURS` (default 24) y, si el total supera
  `GATEWAY_MAX_DUMPS_GB`, los más viejos primero.
- Tarea periódica (`GATEWAY_CLEANUP_INTERVAL_SECONDS`, default 60): marca
  `expired`/`exhausted`, borra los dumps que correspondan y elimina filas con más de
  7 días de cerradas.
- Alcance de la limpieza: **solo** archivos finales `*.sql.gz` regulares. Nunca `*.part`
  (el runner puede estar escribiéndolos), nunca `.locks/`, nunca enlaces simbólicos.
- `DATA_DIR/audit.jsonl`: una línea JSON por evento (`link_created`, `link_revoked`,
  `landing_view`, `download_started`, `download_completed`, `download_aborted`,
  `file_deleted`, `link_expired`, `rate_limited`, `origin_rejected`) con `ts`, `link_id`,
  `ip` (`X-Real-IP`), `ua` y `bytes`. Nunca se registra el token, solo el `id`.
- **La auditoría no puede llenar el disco:** `audit.jsonl` rota al superar
  `GATEWAY_AUDIT_MAX_MB` (default 50) conservando `GATEWAY_AUDIT_KEEP` (default 5)
  archivos; los eventos disparables por un anónimo (`origin_rejected`, `rate_limited`)
  se limitan a 5 por IP por minuto y el resto se resume en un contador
  (`suppressed`) en el siguiente evento permitido. `ua` se trunca a 120 caracteres.

## Páginas y endurecimiento del listener público

Es la única superficie expuesta a internet; todo lo siguiente es obligatorio.

- **Páginas con estilo** (HTML y CSS propios, sin JavaScript ni recursos externos):
  disponible (descarga), expirado, agotado, revocado, sesión vencida, archivo ya no
  disponible, no encontrado, método no permitido, demasiadas solicitudes y error. Tema
  claro y oscuro (`prefers-color-scheme`), adaptable a móvil, con foco visible y
  contraste suficiente. La página de descarga muestra nombre, tamaño legible, SHA-256,
  expiración (UTC y tiempo restante calculado en el servidor) y descargas restantes.
- **Sin fuga en previews:** el `<title>` y la descripción son genéricos (nunca el nombre
  del archivo); así WhatsApp y Slack no muestran el nombre del dump al desplegar el enlace.
- **CSP estricta:** `default-src 'none'; style-src 'sha256-<hash del CSS>'; img-src data:;
  form-action 'self'; base-uri 'none'; frame-ancestors 'none'`. Sin `unsafe-inline`. Un
  hash en `style-src` no cubre atributos: **ninguna página usa `style=`** (tampoco dentro
  del SVG en línea) y todas comparten una única constante de CSS. Una prueba extrae el
  `<style>` de una página renderizada, recalcula su sha256 en base64 y lo compara con el
  de la cabecera.
- **Cabeceras en toda respuesta:** `Cache-Control: no-store`, `X-Robots-Tag: noindex`,
  `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`,
  `Permissions-Policy: camera=(), microphone=(), geolocation=()`,
  `Cross-Origin-Resource-Policy: same-origin`, `Cross-Origin-Opener-Policy: same-origin`
  y, cuando llega `X-Forwarded-Proto: https`, `Strict-Transport-Security: max-age=31536000`.
- **Límite de intentos fallidos por IP** (`X-Real-IP`): más de
  `GATEWAY_MISS_LIMIT` (default 20) respuestas `404` en 60 s bloquean a esa IP durante
  `GATEWAY_BLOCK_SECONDS` (default 300) con `429` y `Retry-After`. Cuentan **todos** los `404`,
  incluidos los del router (token mal formado o de otra longitud, sesión inválida, rutas
  inexistentes). Las respuestas válidas no cuentan. La tabla de IPs tiene tamaño acotado.
- **Topes de descargas simultáneas:** global `GATEWAY_MAX_STREAMS` (default 4) y **por
  enlace** `GATEWAY_MAX_STREAMS_PER_LINK` (default 2): si se supera cualquiera, `503` con
  `Retry-After` (el VPS tiene 1 vCPU; el tope por enlace evita que quien tenga un solo
  enlace ocupe todos los cupos). `HEAD` no cuenta.
- **Velocidad mínima:** un stream que, pasados 30 s, promedia menos de
  `GATEWAY_MIN_RATE_BYTES` (default 32768 B/s), o tarda más de `GATEWAY_MAX_STREAM_SECONDS`
  (default 7200), se aborta y libera su cupo. El timeout de escritura por bloque baja a 20 s.
- **Verificación de origen en `POST`** (anti CSRF/prefetch). Con `Referrer-Policy: no-referrer`
  los navegadores envían `Origin: null` en el `POST` del formulario, así que **`Origin: null`
  se acepta**. Regla, en este orden: (1) si `Sec-Fetch-Site` existe y no es `same-origin` ni
  `none`, `403` (`same-site` también se rechaza: otras apps de `*.synkra.app` lo son);
  (2) si no, si `Origin` existe, no es `null` y su host difiere del de
  `GATEWAY_PUBLIC_BASE_URL`, `403`; (3) sin ninguna de las dos cabeceras se acepta (clientes
  no navegador, `curl`). El rechazo registra `origin_rejected`.
- **Precisiones de implementación** (derivadas de la revisión): `keep_file` se guarda en la
  fila del enlace (esquema SQLite v2, migración automática desde v1) para que la regla de
  borrado sin estado lo respete; el limitador agrupa IPv6 por /64
  (las direcciones IPv4 mapeadas se normalizan a su IPv4 antes de agrupar); un cliente que no recibe datos durante 60 s (escritura)
  ve su descarga abortada y libera el cupo de streams; la cabecera `Server` es `gateway`
  (no revela versiones). Una IP bloqueada tampoco puede usar enlaces válidos mientras dure
  el bloqueo (detrás de un NAT compartido afecta a quienes lo comparten).
- Los valores dinámicos de las páginas pasan por `html.escape`; el nombre del archivo en
  `Content-Disposition` se sanea; la URL de redirección del `POST` se construye solo con
  el token ya validado (nunca con `Host` ni cabeceras del cliente).

## Entorno del gateway

`GATEWAY_DUMPS_ROOT`, `GATEWAY_DATA_DIR`, `GATEWAY_PUBLIC_BASE_URL`,
`GATEWAY_API_TOKEN_FILE`, `GATEWAY_PUBLIC_PORT` (8080), `GATEWAY_INTERNAL_PORT` (8081),
`GATEWAY_RETENTION_HOURS` (24), `GATEWAY_MAX_DUMPS_GB` (10; en el VPS compartido se fija
cerca de 8), `GATEWAY_BIND_HOST` (`0.0.0.0`; en Docker el aislamiento lo da la red),
`GATEWAY_CLEANUP_INTERVAL_SECONDS` (60), `GATEWAY_POST_DOWNLOAD_GRACE_SECONDS` (900),
`GATEWAY_MISS_LIMIT` (20), `GATEWAY_BLOCK_SECONDS` (300), `GATEWAY_MAX_STREAMS` (4),
`GATEWAY_MAX_STREAMS_PER_LINK` (2), `GATEWAY_MIN_RATE_BYTES` (32768),
`GATEWAY_MAX_STREAM_SECONDS` (7200), `GATEWAY_AUDIT_MAX_MB` (50) y `GATEWAY_AUDIT_KEEP` (5).
El evento `link_expired` lleva un campo extra `reason` (`expired` o `exhausted`).

## Entorno del bot

`BOT_TOKEN_FILE`, `BOT_ALLOWED_USER_IDS` (coma), `BOT_RUNNER_PATH`, `BOT_GATEWAY_URL`,
`BOT_GATEWAY_TOKEN_FILE`, `BOT_DATA_DIR`, `BOT_DEFAULT_TTL_SECONDS` (3600),
`BOT_DEFAULT_MAX_DOWNLOADS` (1), `BOT_JOB_TIMEOUT_SECONDS` (1800), `BOT_MAX_JOBS` (1).
`BOT_DATA_DIR` vale `/data` por defecto. `BOT_TELEGRAM_API_BASE` es opcional y solo para
pruebas (apunta a un Telegram falso). El polling pide solo `message` y `callback_query`.

Códigos de error locales del bot, que no vienen del runner: `E_TIMEOUT`, `E_BAD_OUTPUT`,
`E_RUNNER_START` y `E_CANCELLED`. En aiogram 3.31 `start_polling(drop_pending_updates=True)`
se ignora, por eso el arranque llama a `bot.delete_webhook(drop_pending_updates=True)`.

## Permisos de datos

El proceso fija `umask 077` al arrancar y asegura que `GATEWAY_DATA_DIR` sea `0700`, de modo
que `gateway.db`, `-wal`, `-shm` y `audit.jsonl*` queden `0600`. Un bearer con bytes no ASCII
responde `401` (no `500`).

## Restricciones de despliegue (fase 5)

- El puerto público del gateway se publica como `127.0.0.1:<PUERTO>:8080`; nginx es el
  único cliente, y por eso `X-Real-IP` (que nginx fija) es confiable. **El `8081` no se
  publica nunca**: solo es alcanzable dentro de la red de Docker.
- El gateway y el runner corren con el mismo UID (los dumps son 0600 y el directorio 0700).

## Notas para el bot

- Al enviar el enlace, el bot deshabilita la vista previa
  (`link_preview_options.is_disabled=true`): si no, los servidores de Telegram piden la
  página intermedia (ruido en `landing_view` y fuga de la URL a un tercero).
- **El enlace no debe abrirse al tocarlo.** Va dentro de `<code>` (en Telegram, tocar
  texto monoespaciado lo copia) y el mensaje lleva un botón inline con `copy_text`
  ("Copiar enlace"). No se usan botones de tipo `url` ni el enlace como texto plano
  (Telegram lo volvería un hipervínculo que abre el navegador).
- **Aviso al terminar un dump:** editar un mensaje no genera notificación push. Al
  terminar (éxito, fallo, timeout o cancelación) el bot envía un mensaje **nuevo**, con
  notificación activa, cuyo primer renglón resume el resultado ("✅ Dump listo: ..." o
  "❌ Dump falló: ..."). El mensaje de progreso se edita a un estado final corto y no
  repite el enlace. El dump sigue ejecutándose aunque el usuario salga del chat. Si el
  envío del aviso falla (red, Telegram), se reintenta 3 veces con espera (1 s y 3 s): es
  el único mensaje que lleva el enlace.
- Revocar (botón o `/revoke`) usa `DELETE /v1/links/{id}` sin `keep_file`: el dump se
  borra en cuanto nada lo necesite. El mensaje al usuario dice "enlace revocado; el dump se
  eliminará" (no promete borrado inmediato: una sesión abierta o una descarga en curso lo
  pueden retrasar unos minutos).
- El bot nunca imprime ni registra el token del bot ni el del gateway.
