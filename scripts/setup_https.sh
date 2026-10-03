#!/usr/bin/env bash
# =============================================================================
# scripts/setup_https.sh — HTTPS con DuckDNS + nginx + Let's Encrypt
# Ejecutar EN EL VPS como root:
#   bash setup_https.sh <subdominio> <token>
#
# Ejemplo:
#   bash setup_https.sh mibot-trading abc123def456
#   → Resultado: https://mibot-trading.duckdns.org
# =============================================================================
set -euo pipefail

SUBDOMAIN="${1:-}"
TOKEN="${2:-}"
VPS_IP="24.199.87.217"
BOT_DIR="/root/trading_bot_v11"
WEBAPP_PORT="8080"

# ── Validación ────────────────────────────────────────────────────────────────
if [[ -z "$SUBDOMAIN" || -z "$TOKEN" ]]; then
  echo ""
  echo "USO: bash setup_https.sh <subdominio> <token>"
  echo ""
  echo "Pasos previos:"
  echo "  1. Ve a https://www.duckdns.org y entra con Google/GitHub"
  echo "  2. En 'add domain' escribe un nombre (ej: mibot-trading)"
  echo "  3. Copia el TOKEN que aparece arriba de la página"
  echo "  4. Asegúrate de que la IP del subdominio sea: $VPS_IP"
  echo ""
  echo "Luego ejecuta:"
  echo "  bash setup_https.sh mibot-trading TU_TOKEN_AQUI"
  echo ""
  exit 1
fi

DOMAIN="${SUBDOMAIN}.duckdns.org"
WEBAPP_URL="https://${DOMAIN}"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  Setup HTTPS — DuckDNS + nginx + Let's Encrypt  ║"
echo "╚══════════════════════════════════════════════════╝"
echo "  Dominio:  $DOMAIN"
echo "  IP:       $VPS_IP"
echo "  Webapp:   $WEBAPP_URL"
echo ""

# ── 1. Actualizar DuckDNS con la IP del VPS ───────────────────────────────────
echo "[1/8] Apuntando DuckDNS → $VPS_IP ..."
RESP=$(curl -s "https://www.duckdns.org/update?domains=${SUBDOMAIN}&token=${TOKEN}&ip=${VPS_IP}")
if [[ "$RESP" == "OK" ]]; then
  echo "      ✓ DuckDNS actualizado ($DOMAIN → $VPS_IP)"
else
  echo "      ✗ Error DuckDNS: $RESP"
  echo "      Verifica tu TOKEN y subdominio."
  exit 1
fi

# Esperar propagación DNS
echo "      Esperando 15s para propagación DNS..."
sleep 15

# ── 2. Instalar dependencias ──────────────────────────────────────────────────
echo "[2/8] Instalando nginx, certbot..."
apt-get update -qq
apt-get install -y nginx certbot python3-certbot-nginx python3-pip curl 2>&1 | tail -5
pip3 install certbot-dns-duckdns -q 2>&1 | tail -3
echo "      ✓ Instalaciones completadas"

# ── 3. Guardar credenciales DuckDNS para certbot ─────────────────────────────
echo "[3/8] Configurando credenciales certbot..."
mkdir -p /root/.secrets
cat > /root/.secrets/duckdns.ini << EOF
dns_duckdns_token=${TOKEN}
EOF
chmod 600 /root/.secrets/duckdns.ini
echo "      ✓ Credenciales guardadas"

# ── 4. Obtener certificado Let's Encrypt ─────────────────────────────────────
echo "[4/8] Obteniendo certificado SSL (puede tardar ~60s)..."
certbot certonly \
  --authenticator dns-duckdns \
  --dns-duckdns-credentials /root/.secrets/duckdns.ini \
  --dns-duckdns-propagation-seconds 60 \
  -d "${DOMAIN}" \
  --non-interactive \
  --agree-tos \
  --email "bot@${DOMAIN}" \
  --no-eff-email \
  2>&1 | tail -10

CERT_PATH="/etc/letsencrypt/live/${DOMAIN}"
if [[ ! -f "${CERT_PATH}/fullchain.pem" ]]; then
  echo "      ✗ Error obteniendo certificado. Revisa los logs arriba."
  exit 1
fi
echo "      ✓ Certificado obtenido: $CERT_PATH"

# ── 5. Configurar nginx ───────────────────────────────────────────────────────
echo "[5/8] Configurando nginx..."

cat > /etc/nginx/sites-available/trading-webapp << NGINX_CONF
# Trading Bot v11 — Webapp (Telegram Mini App)
# Generado por setup_https.sh

server {
    listen 80;
    server_name ${DOMAIN};

    # Redirigir todo HTTP → HTTPS
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name ${DOMAIN};

    # Certificados Let's Encrypt
    ssl_certificate     ${CERT_PATH}/fullchain.pem;
    ssl_certificate_key ${CERT_PATH}/privkey.pem;

    # Seguridad SSL moderna
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;

    # Headers de seguridad requeridos para Telegram Mini App
    add_header X-Frame-Options SAMEORIGIN;
    add_header X-Content-Type-Options nosniff;
    add_header Content-Security-Policy "default-src 'self' https://telegram.org https://cdn.jsdelivr.net 'unsafe-inline' 'unsafe-eval'; connect-src 'self' https://telegram.org;";

    # Proxy al FastAPI (uvicorn en puerto ${WEBAPP_PORT})
    location / {
        proxy_pass         http://127.0.0.1:${WEBAPP_PORT};
        proxy_http_version 1.1;
        proxy_set_header   Upgrade \$http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 86400s;   # soporte para SSE (Server-Sent Events)
        proxy_buffering    off;
    }

    # Archivos estáticos con caché
    location /static/ {
        proxy_pass      http://127.0.0.1:${WEBAPP_PORT}/static/;
        proxy_cache_valid 200 1h;
        add_header Cache-Control "public, max-age=3600";
    }
}
NGINX_CONF

# Activar sitio
ln -sf /etc/nginx/sites-available/trading-webapp /etc/nginx/sites-enabled/
# Desactivar default para evitar conflictos
rm -f /etc/nginx/sites-enabled/default

# Verificar config
nginx -t
systemctl reload nginx
echo "      ✓ nginx configurado y recargado"

# ── 6. Actualizar WEBAPP_URL en .env ─────────────────────────────────────────
echo "[6/8] Actualizando WEBAPP_URL en .env..."
ENV_FILE="${BOT_DIR}/.env"
if grep -q "^WEBAPP_URL=" "$ENV_FILE" 2>/dev/null; then
  sed -i "s|^WEBAPP_URL=.*|WEBAPP_URL=${WEBAPP_URL}|" "$ENV_FILE"
else
  echo "WEBAPP_URL=${WEBAPP_URL}" >> "$ENV_FILE"
fi
echo "      ✓ WEBAPP_URL=${WEBAPP_URL}"

# ── 7. Configurar auto-renovación DuckDNS + certbot ──────────────────────────
echo "[7/8] Configurando renovación automática..."

# Cron para actualizar IP en DuckDNS cada 5 minutos
CRON_DUCKDNS="*/5 * * * * curl -s 'https://www.duckdns.org/update?domains=${SUBDOMAIN}&token=${TOKEN}&ip=' > /var/log/duckdns.log 2>&1"
(crontab -l 2>/dev/null | grep -v duckdns; echo "$CRON_DUCKDNS") | crontab -

# certbot auto-renewal ya se configura solo con certbot (timer systemd o cron)
# Verificar que certbot timer esté activo
systemctl enable certbot.timer 2>/dev/null || true
systemctl start  certbot.timer 2>/dev/null || true

echo "      ✓ Renovación automática configurada"

# ── 8. Reiniciar webapp con nueva URL ────────────────────────────────────────
echo "[8/8] Reiniciando servicios..."
systemctl restart webapp.service 2>/dev/null || true
systemctl status webapp.service --no-pager -l | head -8

# Abrir puerto 443 si hay ufw activo
if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 443/tcp
  ufw allow 80/tcp
  echo "      ✓ Puertos 80/443 abiertos en ufw"
fi

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  HTTPS configurado correctamente                ║"
echo "╠══════════════════════════════════════════════════╣"
echo "║  Mini App:  ${WEBAPP_URL}"
echo "║  Telegram:  /grafica"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "Verifica que funciona:"
echo "  curl -sk ${WEBAPP_URL}/api/status | python3 -m json.tool"
echo ""
