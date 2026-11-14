<!--
  services/gitlab/README.md — Contrato de uso del servicio de GitLab
  Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
-->
# gitlab

CLI Python (solo biblioteca estándar) que descarga el código fuente de un proyecto de
GitLab (zip o tar.gz de una rama o tag). Salida: **una línea JSON por stdout**
(contrato `contracts/source-result.schema.json`), logs por stderr. Código de salida:
`0` éxito, `1` fallo operativo, `2` uso incorrecto.

```bash
fetch.py catalog
fetch.py branches --project NOMBRE [--search Q]
fetch.py tags     --project NOMBRE [--search Q]
fetch.py archive  --project NOMBRE --ref=REF [--format zip|tar.gz] --job-id ID
```

`--project` es el `name` del catálogo (lista cerrada); un path libre da `E_NOT_ALLOWED`.
Pasa la referencia como `--ref=REF` (con `=`): un valor que empieza con `-` sin `=` lo
rechaza el analizador de argumentos (exit 2); con `=` lo rechaza la validación
(`E_INVALID_NAME`). Ambos casos se bloquean antes de tocar la red.

El archivo queda en `$DBSYNC_DUMPS_DIR/_gitlab/<proyecto>-<ref>-<sha8>-<job_id>.<fmt>`
(archivo 0600, directorio 0700, `umask 077`) y solo aparece tras verificar el zip o tar.gz
completo. `relpath` es `_gitlab/<archivo>`.

## Entorno

| Variable | Default | Uso |
|---|---|---|
| `GITLAB_BASE_URL` | `https://gitlab.com` | debe ser `https://` |
| `GITLAB_ALLOW_HTTP` | `0` | `1` permite `http://` (solo pruebas) |
| `GITLAB_TOKEN_FILE` | (obligatorio salvo `catalog`) | archivo con el token (se hace strip) |
| `GITLAB_PROJECTS_FILE` | `<repo>/deploy/projects.json` | catálogo `{"projects":[{name,path,db?}]}` |
| `DBSYNC_DUMPS_DIR` | `<repo>/dumps` | raíz de salida |
| `GITLAB_MAX_ARCHIVE_MB` | `500` | tope de tamaño, también sin `Content-Length` |
| `GITLAB_TIMEOUT_SECONDS` | `300` | plazo de reloj de pared de TODA la ejecución (alarma SIGALRM) |
| `GITLAB_READ_TIMEOUT_SECONDS` | `30` | timeout de conexión y de lectura por bloque |
| `DBSYNC_MIN_FREE_MB` | `1024` | espacio libre mínimo antes de descargar |
| `GITLAB_VERIFY_MAX_ENTRIES` | `100000` | tope de entradas del zip/tar.gz (solo bajarlo en pruebas) |
| `GITLAB_VERIFY_RATIO` | `8` | bytes descomprimidos máximos = ratio x tamaño comprimido (tope absoluto 4 GiB) |
| `GITLAB_VERIFY_MIN_BYTES` | `16777216` | piso del tope descomprimido para archivos diminutos |

## Garantías de seguridad

- **Solo lectura:** `GitLabClient` únicamente emite `GET`; `request()` con otro método
  lanza `ReadOnlyError` sin tocar la red (el token real puede tener alcance de escritura).
- **Commit fijado:** se resuelve la ref a un commit de 40 hex y se descarga `archive?sha=<commit>`.
- **Nombre de archivo** solo con slugs validados, `sha8` y `job_id`; nunca de `Content-Disposition`.
- **Sin redirecciones a otro host/puerto/esquema** (no se reenvía el token); al mismo origen
  se siguen hasta 3.
- **Token** solo en la cabecera `PRIVATE-TOKEN`; se redacta `glpat-...` y el valor (y sus
  variantes codificadas) en stdout, stderr y mensajes de error.
- Referencias: `^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$`, sin `..`, `//`, `/.`, sin terminar en
  `/`, `.` o `.lock`; se codifican con `quote(ref, safe="")`.
- Reintentos: `429` espera `Retry-After` (máx. 60 s) una vez y luego `E_RATE_LIMITED`;
  `5xx` reintenta 2 veces (esperas de 2 s y 4 s) y luego `E_GITLAB`; red caída o timeout `E_NETWORK`.

## Límites de robustez (GitLab hostil o lento)

- **Memoria:** ninguna respuesta JSON supera 2 MiB (`E_GITLAB`). `branches`/`tags` procesan
  cada página y conservan solo `name` (máx. 200 caracteres; si no, se omite), `sha` (40 hex),
  `default` y `protected`, con tope de 300 refs; la salida queda acotada (< 200 KB).
- **Plazo total (dos capas):** lecturas en bloques de 64 KiB con `check_deadline()` entre
  lecturas (JSON, detalle de error y descarga) y una alarma `SIGALRM` global armada en
  `cli.main` que cubre cabeceras goteadas, cuerpos goteados y la verificación; vence con
  `E_NETWORK` y el `.part` se borra (el borrado bloquea la señal para no interrumpirse).
- **Verificación sin extraer nada:** zip: se recorre el directorio central (EOCD, zip64
  incluido) y se rechaza con más de 100 000 entradas antes de abrirlo; después se leen las
  entradas en streaming verificando CRC con tope de bytes descomprimidos acumulado (cubre
  entradas solapadas). tar.gz: una sola pasada con `tf.next()` descartando `tf.members`,
  mismo tope de entradas y de bytes (se comprueba antes de descomprimir cada miembro) y la
  cola del gzip se lee hasta el final para detectar truncado. Tope: `min(max(8 x comprimido,
  16 MiB), 4 GiB)`. Si se supera: `E_TOO_LARGE`.
- **`.part` huérfanos:** al arrancar `archive` se borran los `*.part` regulares de `_gitlab/`
  con más de 1 h; nunca uno reciente ni un enlace simbólico.
- **`Content-Length`:** duplicado, contradictorio (`N, N+5`) o no ASCII es respuesta inválida
  (`E_GITLAB`).

## Errores

`E_USAGE` (exit 2), `E_CONFIG`, `E_INVALID_NAME`, `E_NOT_ALLOWED`, `E_AUTH` (401),
`E_FORBIDDEN` (403), `E_NOT_FOUND` (404), `E_RATE_LIMITED`, `E_GITLAB`, `E_NETWORK`,
`E_TOO_LARGE`, `E_TRUNCATED`, `E_DISK`, `E_INTERNAL`. Con `E_USAGE` sin comando válido el
campo `command` vale `catalog` (el esquema no admite otro valor).

## Pruebas y evals

```bash
# Puerta: offline, determinista, < 0.5 s (GitLab falso en 127.0.0.1)
cd services/gitlab && python -m unittest discover -s tests -t .

# Eval real (lee GitLab real, solo GET, máx. 2 descargas; umbral: 0 fallos)
GITLAB_TOKEN_FILE=... GITLAB_PROJECTS_FILE=... \
GITLAB_EVAL_SMALL_PROJECT=<name> GITLAB_EVAL_BIG_PROJECT=<name> python evals/eval_real.py
```
