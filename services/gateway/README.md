<!--
  services/gateway/README.md — Gateway de descargas temporales de dumps
  Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
-->
# gateway

Servicio asyncio (aiohttp + SQLite) con dos listeners en un proceso. Contrato en
`contracts/gateway-api.md` y `contracts/link.schema.json`.

- Público `:8080` (`GATEWAY_PUBLIC_PORT`): `/d/{token}` (página intermedia, `POST` abre sesión) y `/d/{token}/f/{sesion}` (entrega con `Range`).
- Interno `:8081` (`GATEWAY_INTERNAL_PORT`): `/v1/links`, `/v1/health`, con `Authorization: Bearer`. Nunca se publica.

## Entorno

| Variable | Default | Uso |
|---|---|---|
| `GATEWAY_DUMPS_ROOT` | `/dumps` | raíz de dumps del runner |
| `GATEWAY_DATA_DIR` | `/data` | `gateway.db` (SQLite WAL) y `audit.jsonl` |
| `GATEWAY_PUBLIC_BASE_URL` | (obligatorio) | prefijo de las URL entregadas |
| `GATEWAY_API_TOKEN_FILE` | (obligatorio) | token bearer (mínimo 16 caracteres) |
| `GATEWAY_RETENTION_HOURS` | `24` | retención de dumps por mtime |
| `GATEWAY_MAX_DUMPS_GB` | `10` | cuota total de `*.sql.gz` finales |
| `GATEWAY_BIND_HOST` | `0.0.0.0` | interfaz de escucha |
| `GATEWAY_CLEANUP_INTERVAL_SECONDS` | `60` | periodo de limpieza |
| `GATEWAY_POST_DOWNLOAD_GRACE_SECONDS` | `900` | gracia de la sesión tras contar la descarga (0 a 7200) |
| `GATEWAY_MISS_LIMIT` | `20` | 404 por IP en 60 s antes de bloquear |
| `GATEWAY_BLOCK_SECONDS` | `300` | duración del bloqueo (`429` + `Retry-After`) |
| `GATEWAY_MAX_STREAMS` | `4` | descargas simultáneas; `503` + `Retry-After` al superarlo (`HEAD` exento) |
| `GATEWAY_MAX_STREAMS_PER_LINK` | `2` | descargas simultáneas por enlace (`503` al superarlo) |
| `GATEWAY_MIN_RATE_BYTES` | `32768` | tras 30 s, un stream con promedio menor se aborta |
| `GATEWAY_MAX_STREAM_SECONDS` | `7200` | duración máxima de un stream |
| `GATEWAY_AUDIT_MAX_MB` | `50` | tamaño a partir del cual rota `audit.jsonl` |
| `GATEWAY_AUDIT_KEEP` | `5` | archivos rotados que se conservan (`audit.jsonl.1` a `.N`) |

Perillas solo de `Config` (pruebas): `miss_window` (60 s), `limiter_max_ips` (10000), `write_timeout` (20 s por bloque), `min_rate_after` (30 s), `session_ttl` (7200 s).

## Decisiones

- Del token solo se guarda su sha256; el `id` de 12 hex es el identificador público.
- Se cuenta una descarga la primera vez que sale el último byte (`write_eof` sin error) por sesión (TTL 2 h). La descarga se cuenta cuando la unión de intervalos enviados en la sesión cubre `[0, size)` (`sessions.ranges`, máximo 64 intervalos fusionados; si se excede se descarta el más corto, lo que solo subestima). Una sonda `Range: bytes=-1` no cuenta. Al contar, la sesión vence en `min(vencimiento, conteo + gracia)`; repetir dentro de la sesión no suma otra. La sesión vence en `min(inicio + 7200, expires_at + gracia)`.
- Presupuesto por sesión de `2 x size + 64 B` (los 64 B toleran sondas y reanudaciones mínimas; desviación del contrato, que dice `2 x size`): se reserva al abrir cada respuesta (410 si no cabe) y se devuelve lo no enviado, todo atómico en SQLite (`reserve_bytes`/`record_sent`). Esquema v3 (migración automática desde v1 y v2).
- Revocar un enlace cierra sus sesiones.
- Cuota y retención son respaldo solo para dumps sin ninguna fila de enlace.
- El gateway debe correr con el mismo UID que el runner (dumps 0600 en directorio 0700).
- Sin log de acceso de aiohttp (las rutas contienen el token).

## Borrado de dumps

Regla sin estado evaluada en cada pasada (`cleanup.delete_finished`) y por la revocación, bajo el candado de `create_link`: se borra el archivo si su `relpath` tiene filas de enlace y todas son terminales, sin sesión abierta (la gracia cuenta) y sin stream en curso. Si algo lo retrasa, lo recoge la siguiente pasada. `DELETE /v1/links/{id}?keep_file=1` guarda `keep_file` en la fila (esquema v2, migración automática desde v1): la regla no borra ese archivo; al purgarse las filas (7 días) queda sin enlaces y lo recoge la retención. Auditoría `file_deleted` con `reason`: `link_expired|link_exhausted|link_revoked|retention|quota`.

## Páginas y endurecimiento

- `pages.py`: HTML y CSS propios, sin JavaScript ni recursos externos; una sola constante `CSS`, sin `style=`; CSP con `sha256` calculado de esa constante al importar. `<title>` y descripción genéricos (sin nombre de archivo).
- Cabeceras en toda respuesta pública (incluye `Permissions-Policy`, CORP, COOP, HSTS con `X-Forwarded-Proto: https`); `Server: gateway` (no expone versiones).
- Limitador de 404 por IP (`limiter.py`, tabla acotada, IPv6 agrupado por /64) en el middleware, de modo que cuenta también los 404 del router.
- Verificación de origen en `POST`: `Origin: null` se acepta; `Sec-Fetch-Site` distinto de `same-origin`/`none` se rechaza; `Origin` de otro host se rechaza. Se compara solo el host.
- Streams: tope global y por enlace (503), velocidad mínima, duración máxima, timeout de escritura de 20 s y corte al revocar (`ctx.revoked`, comprobado entre bloques).
- Auditoría: rotación por tamaño, `origin_rejected` y `rate_limited` limitados a 5 por IP por minuto con contador `suppressed`, `ua` a 120 caracteres. Eventos normales nunca se limitan.
- Origin: se compara (esquema, host, puerto) normalizados contra `GATEWAY_PUBLIC_BASE_URL`; un `Origin` con ruta, usuario, barra invertida o espacios se rechaza.
- Permisos: `umask 077` y `data_dir` 0700 al arrancar; `gateway.db` se crea 0600 (`-wal`/`-shm` heredan). `relpath` solo `<servidor>/<archivo>.sql.gz`.
- El cupo de streams se reserva bajo el candado de borrado antes de abrir el archivo: cierra la carrera entre borrado y descarga.

## Pruebas

```bash
cd services/gateway
../../.venv/bin/python -m unittest discover -s tests -t .   # puerta, < 2 s
../../.venv/bin/python evals/e2e_gateway.py                 # eval e2e, exit 0/1
```
