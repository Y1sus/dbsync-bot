<!--
  services/gateway/README.md — Gateway de descargas temporales (enlaces de 1 a 4 archivos)
  Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
-->
# gateway

Servicio asyncio (aiohttp + SQLite) con dos listeners en un proceso. Contrato en
`contracts/gateway-api.md` y `contracts/link.schema.json` (API interna **v2**: `LinkRecord.files[]`).

## Rutas

- Público `:8080` (`GATEWAY_PUBLIC_PORT`):
  - `GET|HEAD /d/{token}`: página con un botón por archivo (no cuenta como descarga).
  - `POST /d/{token}/{n}` (n = 0 a 3): abre o reutiliza la sesión de ese archivo y responde `303` a `/d/{token}/{n}/f/{sesion}`.
  - `GET|HEAD /d/{token}/{n}/f/{sesion}`: entrega con `Range`.
  - Compatibilidad solo para enlaces de **un** archivo: `POST /d/{token}` (303 a `/d/{token}/f/{sesion}`) y `GET|HEAD /d/{token}/f/{sesion}` equivalen al índice 0. En un enlace de varios archivos esas dos rutas responden `404`; un índice inexistente también.
- Interno `:8081` (`GATEWAY_INTERNAL_PORT`), con `Authorization: Bearer`, nunca se publica:
  - `POST /v2/links` (`files`: 1 a 4 `{relpath, sha256?}`, `max_downloads` por archivo), `GET /v2/links?state=active|all`, `GET /v2/links/{id}`, `DELETE /v2/links/{id}[?keep_file=1]`, `GET /v2/health`. `/v1` ya no existe (404).
  - `relpath` es exactamente `<fuente>/<archivo>` (fuente = servidor de BD o `_gitlab`); sufijos `.sql.gz` (`db`), `.zip` y `.tar.gz` (`code`). El estado del enlace es `exhausted` solo si todos los archivos agotaron.

## Entorno

| Variable | Default | Uso |
|---|---|---|
| `GATEWAY_DUMPS_ROOT` | `/dumps` | raíz de datos (dumps del runner y `_gitlab/`) |
| `GATEWAY_DATA_DIR` | `/data` | `gateway.db` (SQLite WAL) y `audit.jsonl` |
| `GATEWAY_PUBLIC_BASE_URL` | (obligatorio) | prefijo de las URL entregadas |
| `GATEWAY_API_TOKEN_FILE` | (obligatorio) | token bearer (mínimo 16 caracteres) |
| `GATEWAY_RETENTION_HOURS` | `24` | retención de archivos sin enlace por mtime |
| `GATEWAY_MAX_DUMPS_GB` | `10` | cuota total de archivos finales |
| `GATEWAY_BIND_HOST` | `0.0.0.0` | interfaz de escucha |
| `GATEWAY_CLEANUP_INTERVAL_SECONDS` | `60` | periodo de limpieza |
| `GATEWAY_POST_DOWNLOAD_GRACE_SECONDS` | `900` | gracia de la sesión tras contar la descarga (0 a 7200) |
| `GATEWAY_MISS_LIMIT` | `20` | 404 por IP en 60 s antes de bloquear |
| `GATEWAY_BLOCK_SECONDS` | `300` | duración del bloqueo (`429` + `Retry-After`) |
| `GATEWAY_MAX_STREAMS` | `6` | descargas simultáneas globales; `503` + `Retry-After` (`HEAD` exento) |
| `GATEWAY_MAX_STREAMS_PER_LINK` | `2` | descargas simultáneas **en total por enlace** (todos sus archivos; `503` al superarlo) |
| `GATEWAY_MIN_RATE_BYTES` | `32768` | tras 15 s, un stream con promedio menor se aborta |
| `GATEWAY_MAX_STREAM_SECONDS` | `7200` | duración máxima de un stream |
| `GATEWAY_AUDIT_MAX_MB` | `50` | tamaño a partir del cual rota `audit.jsonl` |
| `GATEWAY_AUDIT_KEEP` | `5` | archivos rotados que se conservan (`audit.jsonl.1` a `.N`) |

Perillas solo de `Config` (pruebas): `miss_window` (60 s), `limiter_max_ips` (10000), `write_timeout` (10 s por bloque), `min_rate_after` (15 s), `session_ttl` (7200 s), `stale_part_seconds` (7200).

## Decisiones

- Del token solo se guarda su sha256; el `id` de 12 hex es el identificador público.
- **Modelo (SQLite v5):** `links` (vigencia, `max_downloads` por archivo, `keep_file` por enlace), `link_files` (índice, `relpath`, tamaño, `sha256`, `kind`, `downloads_completed`) y `sessions` ligadas a (enlace, archivo), cada una con su cobertura de bytes, su total enviado (`sent_total`) y su presupuesto.
- **Conteo:** una descarga de un archivo se cuenta una vez por sesión cuando la unión de intervalos enviados cubre `[0, size)` **o** el total de bytes enviados en la sesión alcanza `size` (lo primero que ocurra; cierra el hueco de pedir `bytes=0-(size-2)` repetido). Intervalos en `sessions.ranges` y total en `sessions.sent_total`, actualizados de forma atómica en `record_sent` (máximo 64 intervalos fusionados; si se excede se descarta el más corto, lo que solo subestima la cobertura; máximo 64 intervalos fusionados; si se excede se descarta el más corto, lo que solo subestima). `Range: bytes=-1` no cuenta. Una sonda de 1 byte o pequeñas no cuentan. Al contar, la sesión vence en `min(vencimiento, conteo + gracia)`. La sesión vence en `min(inicio + 7200, expires_at + gracia)`.
- **Presupuesto por sesión (y archivo):** `2 x size` estricto; se reserva al abrir cada respuesta (410 con página propia si no cabe) y se devuelve lo no enviado, atómico en SQLite (`reserve_bytes` y `record_sent`).
- Revocar un enlace cierra sus sesiones y corta las descargas en curso.
- Cuota y retención son respaldo solo para archivos sin ninguna fila de enlace.
- El gateway debe correr con el mismo UID que el runner (archivos 0600 en directorio 0700).
- Sin log de acceso de aiohttp (las rutas contienen el token).

## Migración de `gateway.db`

Al arrancar, `Store` detecta `PRAGMA user_version` y migra solo, en una transacción por paso (v1 a v2 a v3 a v4 a v5): cada enlace v3 se convierte en un enlace de un archivo (índice 0, `kind` según el sufijo) sin perder contadores, estado, vigencia, `keep_file` ni sesiones (con su cobertura y presupuesto). El paso v4 a v5 solo añade `sessions.sent_total` (inicializada con `budget_used`, que con el servicio parado es lo ya enviado). **Es irreversible** (v3 a v4 reconstruye `links` y `sessions`, y v5 añade una columna): una versión anterior del gateway se niega a abrir una base v5. Para volver atrás hay que restaurar una copia de `gateway.db`, `-wal` y `-shm` hecha con el servicio parado. Una versión de esquema desconocida aborta el arranque.

## Borrado de archivos

Regla sin estado **por archivo** evaluada en cada pasada (`cleanup.delete_finished`) y por la revocación, bajo el candado de `create_link`: un archivo se borra si ningún enlace vigente (no vencido ni revocado) lo tiene con descargas disponibles, no hay sesión abierta de ese archivo (la gracia cuenta) y no hay descarga en curso. Un dump agotado se borra aunque el zip del mismo enlace siga disponible; vencer o revocar libera todos los archivos del enlace. `DELETE /v2/links/{id}?keep_file=1` guarda `keep_file` en el enlace y conserva todos sus archivos; al purgarse las filas (7 días) quedan sin enlaces y los recoge la retención. `_scan` recorre `<fuente>/*.{sql.gz,zip,tar.gz}` (nunca `*.part`, ocultos, `.locks`, symlinks). Aparte, la pasada borra los `*.part` regulares con más de 2 h de mtime bajo `<fuente>/` (nunca uno reciente, ni symlinks, ni ocultos, ni fuera de la raíz) con `file_deleted` y `reason` `stale_part`. Auditoría `file_deleted` con `relpath` y `reason`: `link_expired|link_exhausted|link_revoked|retention|quota|stale_part`.

## Páginas y endurecimiento

- `pages.py`: HTML y CSS propios, sin JavaScript ni recursos externos; una sola constante `CSS`, sin `style=`; CSP con `sha256` calculado de esa constante al importar. La página de descarga lista cada archivo (icono de tipo en SVG con clases, nombre, tamaño, SHA-256, descargas restantes de ese archivo y su botón); un archivo agotado o ya borrado muestra su estado y no ofrece botón. `<title>` ("Descarga lista") y descripción genéricos: ningún nombre de archivo ni de proyecto fuera del cuerpo.
- Cabeceras en toda respuesta pública (incluye `Permissions-Policy`, CORP, COOP, HSTS con `X-Forwarded-Proto: https`); `Server: gateway`.
- Limitador de 404 por IP (`limiter.py`, tablas acotadas, IPv6 por /64, IPv4 mapeada normalizada) en el middleware: cuenta también los 404 del router y los de índice inexistente.
- Verificación de origen en `POST`: `Origin: null` se acepta; `Sec-Fetch-Site` distinto de `same-origin`/`none` se rechaza; `Origin` se compara (esquema, host, puerto) normalizados contra `GATEWAY_PUBLIC_BASE_URL`, y con ruta, usuario, barra invertida o espacios se rechaza.
- Streams: tope global y por enlace (total de sus archivos; 503), velocidad mínima, duración máxima, timeout de escritura de 20 s y corte al revocar (`ctx.revoked`, comprobado entre bloques).
- Auditoría: rotación por tamaño; `origin_rejected` y `rate_limited` limitados a 5 por IP por minuto con contador `suppressed`; `ua` a 120 caracteres; eventos de descarga con `file_index` y `kind`; `link_created` con `files`. Nunca el token.
- Permisos: `umask 077` y `data_dir` 0700 al arrancar; `gateway.db` se crea 0600 (`-wal`/`-shm` heredan).
- El cupo de streams se reserva bajo el candado de borrado antes de abrir el archivo: cierra la carrera entre borrado y descarga.

## Pruebas

```bash
cd services/gateway
../../.venv/bin/python -m unittest discover -s tests -t .   # puerta, < 2 s
../../.venv/bin/python evals/e2e_gateway.py                 # eval e2e, exit 0/1
```
