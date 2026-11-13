# Destino: /etc/nginx/sites-available/dumps.synkra.app (symlink en sites-enabled)
# Vhost del nginx del host para el gateway de dbsync-bot: proxy a 127.0.0.1:18090.
# Solo se publican las rutas /d/; la API interna (8081) no pasa por aquí. certbot añade el 443 y el TLS.
# Autor: Jesús Montalvo · jmontalvo_m@outlook.com

server {
    server_name dumps.synkra.app;

    # El único cuerpo que llega es el POST del botón (vacío)
    client_max_body_size 1k;

    location /d/ {
        proxy_pass http://127.0.0.1:18090;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        # El gateway toma la IP de X-Real-IP: solo es confiable porque nginx es su único cliente
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Descargas grandes: sin buffer en disco del host y con tiempo suficiente
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_connect_timeout 5s;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location / {
        return 404;
    }

    listen 80;
    listen [::]:80;
}
