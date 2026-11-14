# Destino: /etc/nginx/sites-available/dumps.synkra.app (symlink en sites-enabled)
# Requiere antes deploy/nginx/dumps-limits.conf en /etc/nginx/conf.d/.
# Vhost del nginx del host para el gateway de dbsync-bot: proxy a 127.0.0.1:18090.
# Solo se publican las rutas /d/; la API interna (8081) no pasa por aquí. certbot añade el 443 y el TLS.
# Autor: Jesús Montalvo · jmontalvo_m@outlook.com

server {
    server_name dumps.synkra.app;

    # No revelar la versión de nginx en cabeceras ni páginas de error
    server_tokens off;

    # El único cuerpo que llega es el POST del botón (vacío)
    client_max_body_size 1k;

    # Clientes lentos: se cortan pronto (un lector lento no debe ocupar conexiones)
    client_header_timeout 10s;
    client_body_timeout 10s;
    send_timeout 30s;

    # El log de acceso no guarda el token; el de errores solo registra lo crítico
    access_log /var/log/nginx/dumps.synkra.app.access.log dumps_safe;
    error_log  /var/log/nginx/dumps.synkra.app.error.log crit;

    location /d/ {
        limit_req zone=dumps_req burst=20 nodelay;
        limit_req_status 429;
        limit_conn dumps_conn 8;
        limit_conn_status 429;

        proxy_pass http://127.0.0.1:18090;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        # El gateway toma la IP de X-Real-IP: solo es confiable porque nginx es su único cliente
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_hide_header Server;

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
