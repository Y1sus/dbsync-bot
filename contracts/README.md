<!--
  contracts/README.md — Contratos entre servicios
  Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
-->
# contracts

Esquemas que ambos lados importan. Un cambio incompatible incrementa
`schema_version` y se actualizan productor y consumidores en el mismo cambio.

| Archivo | Productor | Consumidor | Versión |
|---|---|---|---|
| `job-result.schema.json` | `services/runner` | `services/bot` | 1 |
| `link.schema.json` + `gateway-api.md` | `services/gateway` | `services/bot` | v1 |

## Archivos de dump y permisos

`relpath` es la ruta canónica para otros servicios: resuélvela contra tu propio
montaje del volumen de dumps (`file` es absoluta en el sistema del runner). Los
archivos son 0600 y el directorio 0700, así que el gateway debe correr con el
mismo UID que el runner (restricción para el compose de la fase 5).
