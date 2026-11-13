<!--
  services/runner/README.md — Contrato de uso del runner
  Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
-->
# runner

Script bash no interactivo. Entrada por flags, salida de **una línea JSON por
stdout** (contrato `contracts/job-result.schema.json`), logs por stderr.
Código de salida: `0` éxito, `1` fallo operativo, `2` uso incorrecto.

```bash
runner.sh servers
runner.sh containers --server prod
runner.sh dbs        --server prod --container mysql8 [--engine mysql]
runner.sh dump       --server prod --container mysql8 --db appdb [--job-id ID]
```

El dump queda en `$DBSYNC_DUMPS_DIR/<servidor>/<servidor>_<db>_<fecha>.sql.gz`
(archivo 0600, directorio 0700) y solo aparece si pasó la verificación completa.

## Entorno

| Variable | Default | Uso |
|---|---|---|
| `DBSYNC_SERVERS_DIR` | `<repo>/servers` | donde viven los `.env` de servidores |
| `DBSYNC_DUMPS_DIR` | `<repo>/dumps` | destino de dumps y locks |
| `DBSYNC_KNOWN_HOSTS` | (obligatorio) | `known_hosts` fijado con las llaves de host |
| `DBSYNC_INSECURE_HOSTKEY` | `0` | `1` desactiva la verificación (solo pruebas) |
| `DBSYNC_MIN_FREE_MB` | `1024` | espacio libre mínimo antes de iniciar |
| `DBSYNC_SSH_BIN` | `ssh` | permite el shim de pruebas |

## Códigos de error

`E_USAGE`, `E_CONFIG`, `E_INVALID_NAME`, `E_SERVER_NOT_FOUND`, `E_NOT_DISCOVERED`,
`E_LOCKED`, `E_SSH`, `E_DUMP_FAILED`, `E_TRUNCATED`, `E_DISK`, `E_INTERNAL`.

## Cómo verifica un dump

`ssh | tee archivo.part | gzip -dc | tail -n 40`. Se revisan los cuatro códigos de
salida del pipeline; luego, que el archivo no esté vacío y que el final contenga
`-- Dump completed` (MySQL) o `-- PostgreSQL database dump complete` (PostgreSQL).
Solo entonces `.part` se renombra al nombre final.

## Servidores

Los `.env` se cargan con `source`, igual que en dbsync: son configuración de
confianza y deben quedar en `chmod 600`. Usa un usuario de BD de solo lectura.
