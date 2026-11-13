# =============================================================================
# services/bot/Dockerfile — Imagen del bot (contexto de build: raíz del repo)
# Construcción: docker build -f services/bot/Dockerfile -t dbsync-bot .
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
FROM python:3.14-slim

# ssh exige que el UID exista en /etc/passwd: se construye con el UID del dueño de los volúmenes
ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BOT_RUNNER_PATH=/app/runner/runner.sh \
    BOT_DATA_DIR=/data

# flock viene en util-linux, ya incluido en la imagen base
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client jq bash tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid ${APP_GID} bot \
    && useradd --uid ${APP_UID} --gid ${APP_GID} --no-create-home --shell /usr/sbin/nologin bot \
    && mkdir -p /app /data \
    && chown ${APP_UID}:${APP_GID} /data

WORKDIR /app

COPY services/bot/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY services/runner/ /app/runner/
COPY services/bot/bot/ /app/bot/
RUN chmod 0755 /app/runner/runner.sh

# tini cosecha los ssh huérfanos que deja el matado del grupo de procesos
ENTRYPOINT ["/usr/bin/tini", "--"]
USER ${APP_UID}:${APP_GID}
CMD ["python", "-m", "bot"]
