<!--
  README.md — Descripción general de dbsync-bot
  Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
-->
# dbsync-bot

Dumps de bases de datos MySQL/MariaDB y PostgreSQL de servidores remotos, pensados
para operarse desde un bot de Telegram y compartirse con un enlace temporal. El
dump nunca se sube a ningún proyecto: se comprime, se verifica y se entrega.

Proyecto independiente, derivado de [dbsync](https://github.com/Y1sus/dbsync)
en el commit `4eed994`. `dbsync` sigue siendo la herramienta interactiva del
equipo y no se modifica desde aquí. Los arreglos de uno no se propagan al otro.

## Estado

| Fase | Servicio | Estado |
|---|---|---|
| 1 | `services/runner` | Hecha: runner no interactivo y endurecido |
| 2 | `contracts/` | `job-result` listo; `LinkRecord` pendiente de la fase 3 |
| 3 | `services/gateway` | Pendiente |
| 4 | `services/bot` | Pendiente |
| 5 | `docker-compose.yml` y despliegue | Pendiente |

## Estructura

```
contracts/                esquemas JSON que comparten los servicios
services/runner/          dump no interactivo (bash): runner.sh, lib/, tests/, evals/
servers/example.env       plantilla de servidor (los .env reales no se commitean)
scripts/test.sh           puerta: shellcheck + pruebas (<2 s); --eval agrega Docker real
.githooks/pre-commit      corre scripts/test.sh antes de cada commit
```

## Pruebas

```bash
git config core.hooksPath .githooks   # una vez por clon
scripts/test.sh                       # puerta, sin red ni Docker
scripts/test.sh --eval                # + evals con MySQL 8.0 y PostgreSQL 17/18 reales
```

## Qué cambió respecto a dbsync

La entrada vendrá de Telegram, así que se corrigieron fallos heredados que eran
tolerables en una terminal propia:

| Problema en dbsync | Corrección en el runner |
|---|---|
| `db_name` y nombres de archivo sin comillas en comandos remotos (inyección) | Regex estricta, membresía contra lo descubierto y scripts sin comillas simples |
| `mysqldump \| gzip > f` devolvía el código de gzip: dumps truncados reportaban éxito | `bash -o pipefail`, `gzip -dc` en línea y marcador de fin de dump obligatorio |
| `-e MYSQL_PWD='...'` en argv: visible en `ps` y roto con `'` en el password | Password por stdin del ssh; `docker exec -e VAR` hereda sin valor |
| `StrictHostKeyChecking=no` en todas las conexiones | `known_hosts` fijado obligatorio (`DBSYNC_KNOWN_HOSTS`) |
| `2>/dev/null` ocultaba todos los errores | stderr remoto en el JSON, con el password redactado |
| Copia temporal en `/tmp` de producción y luego `scp` | Streaming directo ssh a disco local: nada queda en producción |
| Sin protección contra ejecuciones simultáneas | Lock por servidor y chequeo de espacio libre |

Limitaciones conocidas: nombres de base con `$`, comillas o espacios se rechazan.
El modo `native+sudo` está cubierto por pruebas de construcción de comando, no
por un servidor real. La rotación de dumps viejos pasa al servicio de limpieza
(fases 3 y 5).
