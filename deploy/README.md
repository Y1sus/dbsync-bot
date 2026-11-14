<!--
  deploy/README.md — Runbook de despliegue de dbsync-bot con docker compose
  Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
-->
# Despliegue

Los comandos marcados **(sudo)** los corre una persona con sudo; el usuario `yisus` del VPS no lo tiene sin contraseña.

## 0. Probar antes en este equipo (sin VPS)

```bash
scripts/init_deploy.sh        # carpetas, secrets/gateway_token, .env
docker compose up -d --build  # bot + gateway locales; el gateway en 127.0.0.1:18090
docker compose logs -f bot
docker compose down
```

Para probar con un servidor de dev real usa una carpeta que solo lo contenga:

```bash
mkdir -p /tmp/servers-dev && install -m 600 servers/devlion.env /tmp/servers-dev/
DBSYNC_SERVERS_DIR=/tmp/servers-dev docker compose up -d
```

La llave dedicada es `secrets/ssh/dbsync_bot_ed25519`. Su pública se agrega al
`authorized_keys` del servidor de dev. Luego se fijan sus llaves de host:

```bash
scripts/pin_known_hosts.sh devlion            # muestra huellas; compáralas con la consola del proveedor
scripts/pin_known_hosts.sh --write devlion    # las agrega a secrets/known_hosts
```

## 1. Variables

`.env` (copia de `.env.example`): `DBSYNC_UID`/`DBSYNC_GID` deben ser los del dueño de `data/` y
`secrets/` (en el VPS, `id -u` y `id -g` de `yisus`, ambos 1000), `GATEWAY_PUBLIC_BASE_URL`,
`BOT_ALLOWED_USER_IDS` y `GATEWAY_HOST_PORT` (18090: el 18080 ya lo usa engram).

## 2. VPS

```bash
ssh -A yisus@147.182.254.199
git clone git@github.com:Y1sus/dbsync-bot.git ~/dbsync-bot && cd ~/dbsync-bot
```

Desde tu equipo, copia los secretos (nunca por git) y solo los servidores que quieras exponer:

```bash
scp secrets/telegram_token yisus@147.182.254.199:~/dbsync-bot/secrets/
scp secrets/ssh/dbsync_bot_ed25519 yisus@147.182.254.199:~/dbsync-bot/secrets/ssh/
```

En el VPS:

```bash
scripts/init_deploy.sh                      # crea data/, gateway_token y .env; revisa .env
mkdir -p servers-prod && chmod 700 servers-prod     # y copia ahí los .env elegidos (chmod 600)
echo "DBSYNC_SERVERS_DIR=./servers-prod" >> .env
scripts/pin_known_hosts.sh --write <servidor>   # tras verificar las huellas
docker compose up -d --build
docker compose ps && docker compose logs --tail 20 bot
```

## 3. nginx y TLS **(sudo)**

Primero las zonas de límites y el formato de log sin token (contexto `http`), luego el vhost:

```bash
sudo cp deploy/nginx/dumps-limits.conf /etc/nginx/conf.d/dumps-limits.conf
```


DNS: registro A de `dumps.synkra.app` hacia `147.182.254.199` antes de certbot.

```bash
sudo cp deploy/nginx/dumps.synkra.app /etc/nginx/sites-available/dumps.synkra.app
sudo ln -s /etc/nginx/sites-available/dumps.synkra.app /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d dumps.synkra.app
```

Si el vhost ya existe con TLS (certbot lo modificó), no lo reemplaces: edítalo y agrega solo las
líneas nuevas del archivo de `deploy/nginx/` (`client_*_timeout`, `send_timeout`, `access_log`,
`error_log`, y dentro de `location /d/` los `limit_*`, `Connection ""` y `proxy_hide_header Server`).
Luego `sudo nginx -t && sudo systemctl reload nginx`.

Verificación: `curl -sI https://dumps.synkra.app/d/x` responde `404`, y `curl -sI https://dumps.synkra.app/v2/health` también (la API interna no se publica).

## 4. Después del despliegue

- Revoca el token en @BotFather (`/revoke`), guarda el nuevo en `secrets/telegram_token` (600) y `docker compose restart bot`.
- `/setjoingroups` → Disable en @BotFather.
- En los servidores de producción, usuario de BD de solo lectura en su `.env` (hoy todos usan `root`).

## 4b. Código de GitLab (token y catálogo)

El servicio de código necesita un token de GitLab con alcance **solo lectura** (`read_api`; **no** `api`,
que también escribe) y el catálogo de proyectos. El servicio **se niega a operar** con un token que tenga
cualquier otro alcance (consulta `GET /personal_access_tokens/self` antes de cada operación) y solo hace
peticiones `GET`. Ninguno de los dos archivos se sube con `deploy.sh`:

```bash
scp secrets/gitlab_token yisus@147.182.254.199:~/dbsync-bot/secrets/gitlab_token
scp projects.json        yisus@147.182.254.199:~/dbsync-bot/projects.json
ssh yisus@147.182.254.199 'chmod 600 ~/dbsync-bot/secrets/gitlab_token'
```

El token vence (máximo 365 días): renuévalo antes y reemplaza el archivo (`docker compose restart bot`).
`projects.json` es la lista cerrada de proyectos descargables y su BD asociada (plantilla en `deploy/projects.example.json`).

## 5. Operación

| Qué | Comando |
|---|---|
| Estado y logs | `docker compose ps`, `docker compose logs -f bot gateway` |
| Auditoría | `data/bot/audit.jsonl`, `data/gateway/audit.jsonl` |
| Actualizar desde tu equipo | `scripts/deploy.sh yisus@147.182.254.199` (`--dry-run` muestra los pasos; usa el HEAD, exige árbol limpio) |
| Volver atrás | `docker tag dbsync-bot-gateway:previous dbsync-bot-gateway:local; docker tag dbsync-bot-bot:previous dbsync-bot-bot:local; docker compose stop gateway; cp data/gateway/gateway.db.bak data/gateway/gateway.db; rm -f data/gateway/gateway.db-wal data/gateway/gateway.db-shm; docker compose up -d --no-build` (los enlaces creados después del respaldo se pierden) |
| Detener | `docker compose down` (los dumps de `data/dumps` se conservan hasta que la limpieza los borre) |

El bot hace polling con el token: **dos instancias con el mismo token chocan** (409). Detén el smoke local antes de levantar el compose.
