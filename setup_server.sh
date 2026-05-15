#!/bin/bash
# ============================================================
# setup_server.sh — Trading Bot v11
# Corre en el servidor Ubuntu 24.04 después del deploy
# ============================================================
set -e  # salir si cualquier comando falla

REMOTE_DIR="${1:-/root/trading_bot_v11}"
VENV_DIR="$REMOTE_DIR/venv"
SERVICE_NAME="trading_bot"

echo ""
echo "============================================================"
echo "  SETUP SERVIDOR — Trading Bot v11"
echo "  Ubuntu 24.04 | Python 3.12"
echo "============================================================"
echo ""

cd "$REMOTE_DIR"

# ── 1. Actualizar sistema e instalar dependencias del sistema ────────────────
echo "[1/6] Instalando dependencias del sistema..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    libssl-dev \
    libffi-dev \
    git \
    curl \
    htop \
    screen
echo "  Dependencias del sistema OK"

# ── 2. Crear entorno virtual Python ─────────────────────────────────────────
echo ""
echo "[2/6] Creando entorno virtual Python..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
echo "  Entorno virtual OK — $(python --version)"

# ── 3. Instalar dependencias Python ─────────────────────────────────────────
echo ""
echo "[3/6] Instalando dependencias Python..."
pip install -r "$REMOTE_DIR/requirements.txt" -q
echo "  Dependencias Python OK"

# ── 4. Crear directorios necesarios y permisos ──────────────────────────────
echo ""
echo "[4/6] Configurando directorios y permisos..."
mkdir -p "$REMOTE_DIR/logs"
mkdir -p "$REMOTE_DIR/data/backtesting"
mkdir -p "$REMOTE_DIR/data/calibration"
mkdir -p "$REMOTE_DIR/data/trades"
mkdir -p "$REMOTE_DIR/data/historical"

# Verificar que .env existe
if [ -f "$REMOTE_DIR/.env" ]; then
    chmod 600 "$REMOTE_DIR/.env"
    echo "  .env encontrado y permisos restringidos"
else
    echo "  ADVERTENCIA: .env no encontrado — el bot no funcionara sin credenciales"
fi

# Verificar strategy_params.json
if [ -f "$REMOTE_DIR/data/calibration/strategy_params.json" ]; then
    echo "  strategy_params.json OK"
else
    echo "  ADVERTENCIA: strategy_params.json no encontrado en data/calibration/"
fi
echo "  Directorios OK"

# ── 5. Verificar credenciales del .env ──────────────────────────────────────
echo ""
echo "[5/6] Verificando configuracion..."
if [ -f "$REMOTE_DIR/.env" ]; then
    source "$REMOTE_DIR/.env" 2>/dev/null || true

    check_var() {
        if [ -z "${!1}" ]; then
            echo "  FALTA: $1"
        else
            echo "  OK   : $1 = ${!1:0:8}..."
        fi
    }

    check_var "OANDA_ACCESS_TOKEN"
    check_var "OANDA_ACCOUNT_ID"
    check_var "OANDA_ENVIRONMENT"
    check_var "DEEPSEEK_API_KEY"
    check_var "TELEGRAM_BOT_TOKEN"
    check_var "TELEGRAM_CHAT_ID"
fi

# Test rápido de imports
echo ""
echo "  Verificando imports Python..."
"$VENV_DIR/bin/python" -c "
import sys
ok = True
mods = ['pandas','numpy','ta','openai','oandapyV20','httpx','tenacity','dotenv','telegram']
for m in mods:
    try:
        __import__(m)
        print(f'  OK   : {m}')
    except ImportError as e:
        print(f'  FALTA: {m} — {e}')
        ok = False
sys.exit(0 if ok else 1)
"

# ── 6. Instalar servicio systemd ────────────────────────────────────────────
echo ""
echo "[6/6] Configurando servicio systemd..."

# Copiar el archivo de servicio
sudo cp "$REMOTE_DIR/trading_bot.service" /etc/systemd/system/

# Recargar systemd
sudo systemctl daemon-reload

# Habilitar para arranque automático
sudo systemctl enable "$SERVICE_NAME"

# Iniciar el servicio
sudo systemctl start "$SERVICE_NAME"

# Esperar 3 segundos y verificar estado
sleep 3
STATUS=$(sudo systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo "unknown")

echo ""
echo "============================================================"
echo "  SETUP COMPLETADO"
echo "============================================================"
echo ""
echo "  Estado del servicio: $STATUS"
echo ""
if [ "$STATUS" = "active" ]; then
    echo "  El bot esta corriendo"
    echo ""
    echo "  Comandos utiles en el servidor:"
    echo "    sudo systemctl status trading_bot     # estado"
    echo "    sudo systemctl stop trading_bot       # parar"
    echo "    sudo systemctl restart trading_bot    # reiniciar"
    echo "    tail -f $REMOTE_DIR/logs/trading_bot.log  # logs vivo"
else
    echo "  ADVERTENCIA: El servicio no arranco correctamente"
    echo "  Revisa el log: sudo journalctl -u trading_bot -n 50"
fi
echo ""
