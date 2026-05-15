#!/bin/bash
# deploy_vps.sh — Despliega los últimos cambios locales al VPS y reinicia el bot
#
# Uso (desde PowerShell local):
#   bash deploy_vps.sh VPS_IP [VPS_USER]
#   Ej: bash deploy_vps.sh 167.99.x.x root
#
# O ejecutar directamente en el VPS:
#   bash /root/trading_bot_v11/deploy_vps.sh local
#
# ────────────────────────────────────────────────────────────────────────────

VPS_IP="${1:-}"
VPS_USER="${2:-root}"
BOT_DIR="/root/trading_bot_v11"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

FILES_TO_SYNC=(
  "agents/risk_execution_agent/risk_execution_agent.py"
  "cerrar_huerfanos.py"
)

if [ "$VPS_IP" = "local" ]; then
  echo "Modo local: verificando integridad de archivos..."
  for f in "${FILES_TO_SYNC[@]}"; do
    if [ -f "$BOT_DIR/$f" ]; then
      echo "  ✅ $f presente"
    else
      echo "  ❌ $f FALTANTE"
    fi
  done
  echo ""
  echo "Reiniciando servicio..."
  sudo systemctl restart trading_bot
  sleep 2
  sudo systemctl status trading_bot --no-pager -l
  exit 0
fi

if [ -z "$VPS_IP" ]; then
  echo "Uso: bash deploy_vps.sh <VPS_IP> [VPS_USER]"
  echo "  o: bash deploy_vps.sh local  (ejecutar en el propio VPS)"
  exit 1
fi

echo "Desplegando a ${VPS_USER}@${VPS_IP}:${BOT_DIR}"
echo "────────────────────────────────────────────────"

for f in "${FILES_TO_SYNC[@]}"; do
  echo -n "  Enviando $f ... "
  scp "$LOCAL_DIR/$f" "${VPS_USER}@${VPS_IP}:${BOT_DIR}/$f" \
    && echo "✅" || echo "❌ FALLO"
done

echo ""
echo "Reiniciando servicio en VPS..."
ssh "${VPS_USER}@${VPS_IP}" "
  echo '--- Cerrando trades huérfanos ---'
  cd $BOT_DIR && source venv/bin/activate && python3 cerrar_huerfanos.py

  echo ''
  echo '--- Reiniciando trading_bot ---'
  sudo systemctl restart trading_bot
  sleep 3
  sudo systemctl status trading_bot --no-pager -l | head -30
"

echo ""
echo "════════════════════════════════════════════"
echo "  Deploy completado. Verificar logs con:"
echo "  ssh ${VPS_USER}@${VPS_IP} 'journalctl -u trading_bot -n 50 --no-pager'"
echo "════════════════════════════════════════════"
