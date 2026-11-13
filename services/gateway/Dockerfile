# =============================================================================
# services/gateway/Dockerfile — Imagen del gateway (usuario no root UID 10001)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --system --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin gateway \
    && mkdir -p /data /dumps \
    && chown 10001:10001 /data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway ./gateway

USER 10001:10001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request as u;t=open(os.environ['GATEWAY_API_TOKEN_FILE']).read().strip();r=u.Request('http://127.0.0.1:%s/v1/health'%os.environ.get('GATEWAY_INTERNAL_PORT','8081'),headers={'Authorization':'Bearer '+t});sys.exit(0 if u.urlopen(r,timeout=4).status==200 else 1)"]

CMD ["python", "-m", "gateway"]
