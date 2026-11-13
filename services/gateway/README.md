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

## Decisiones

- Del token solo se guarda su sha256; el `id` de 12 hex es el identificador público.
- Se cuenta una descarga la primera vez que sale el último byte (`write_eof` sin error) por sesión (TTL 2 h). Una sesión contada sigue sirviendo rangos hasta su TTL; una descarga repetida dentro de la misma sesión no suma otra.
- Revocar un enlace cierra sus sesiones (el contrato no lo precisa).
- Cuota: cuenta solo `*.sql.gz` finales; nunca borra archivos referenciados, aunque exceda.
- El gateway debe correr con el mismo UID que el runner (dumps 0600 en directorio 0700).
- Sin log de acceso de aiohttp (las rutas contienen el token).

## Pruebas

```bash
cd services/gateway
../../.venv/bin/python -m unittest discover -s tests -t .   # puerta, < 2 s
../../.venv/bin/python evals/e2e_gateway.py                 # eval e2e, exit 0/1
```
