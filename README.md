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
| 2 | `contracts/` | Hecha: `job-result`, `link` y `gateway-api` |
| 3 | `services/gateway` | Hecha: enlaces, descargas con `Range`, limpieza |
| 4 | `services/bot` | Hecha: bot de Telegram (aiogram) |
| 5 | `docker-compose.yml` y despliegue | Hecha en local; falta desplegar en el VPS (ver `deploy/README.md`) |

## Estructura

```
contracts/                esquemas y contrato HTTP que comparten los servicios
services/runner/          dump no interactivo (bash): runner.sh, lib/, tests/, evals/
services/gateway/         enlaces temporales y descargas (Python + aiohttp)
services/bot/             bot de Telegram (Python + aiogram), invoca el runner
integration/              eval runner -> bot -> gateway real
docker-compose.yml        gateway + bot (usuario no root, solo lectura, límites de recursos)
deploy/                   runbook y vhost nginx de dumps.synkra.app
servers/example.env       plantilla de servidor (los .env reales no se commitean)
secrets/                  token del bot, llave SSH dedicada y known_hosts (ignorado por git)
scripts/test.sh           puerta de todos los servicios en paralelo (~2 s); --eval agrega evals
.githooks/pre-commit      corre scripts/test.sh antes de cada commit
```

## Pruebas

```bash
git config core.hooksPath .githooks   # una vez por clon
python3 -m venv .venv && .venv/bin/pip install -r services/gateway/requirements.txt -r services/bot/requirements.txt
scripts/test.sh                       # puerta: runner, gateway, bot e integración, sin red ni Docker
scripts/test.sh --eval                # + evals: MySQL 8.0 y PostgreSQL 17/18 reales, gateway y bot
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
