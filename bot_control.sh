#!/bin/bash
# ============================================================
# bot_control.sh — Control rápido del Trading Bot en VPS
# ============================================================
# Uso: bash bot_control.sh [comando]
#
# Comandos disponibles:
#   status    — estado completo del bot
#   start     — arrancar el bot
#   stop      — parar el bot
#   restart   — reiniciar el bot
#   logs      — ver logs en vivo (Ctrl+C para salir)
#   logs50    — últimas 50 líneas del log
#   capital   — ver capital actual del estado
#   cb        — estado del circuit breaker
#   trades    — últimos 10 trades del log
#   update    — actualizar strategy_params.json desde PC
# ============================================================

BOT_DIR="/home/ubuntu/trading_bot_v11"
LOG_FILE="$BOT_DIR/logs/trading_bot.log"
SERVICE="trading_bot"

cmd="${1:-status}"

case "$cmd" in

  status)
    echo ""
    echo "════════════════════════════════════════"
    echo "  TRADING BOT v11 — Estado del sistema"
    echo "════════════════════════════════════════"
    echo ""
    sudo systemctl status $SERVICE --no-pager -l
    echo ""
    echo "── Uso de recursos ──────────────────────"
    PID=$(sudo systemctl show $SERVICE --property=MainPID --value 2>/dev/null)
    if [ "$PID" != "0" ] && [ -n "$PID" ]; then
        ps -p $PID -o pid,pcpu,pmem,etime --no-headers 2>/dev/null | \
        awk '{printf "  PID: %s | CPU: %s%% | MEM: %s%% | Uptime: %s\n", $1,$2,$3,$4}'
    fi
    echo ""
    echo "── Capital actual ───────────────────────"
    if [ -f "$BOT_DIR/data/capital_state.json" ]; then
        cat "$BOT_DIR/data/capital_state.json" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f'  Capital: \${d.get(\"capital\",0):.2f}')
print(f'  Actualizado: {d.get(\"actualizado\",\"N/A\")}')"
    else
        echo "  capital_state.json no existe aun (normal al inicio)"
    fi
    echo ""
    ;;

  start)
    echo "Arrancando bot..."
    sudo systemctl start $SERVICE
    sleep 2
    sudo systemctl is-active $SERVICE && echo "OK — Bot corriendo" || echo "ERROR — revisa logs"
    ;;

  stop)
    echo "Parando bot..."
    sudo systemctl stop $SERVICE
    echo "Bot detenido"
    ;;

  restart)
    echo "Reiniciando bot..."
    sudo systemctl restart $SERVICE
    sleep 2
    sudo systemctl is-active $SERVICE && echo "OK — Bot reiniciado" || echo "ERROR — revisa logs"
    ;;

  logs)
    echo "Mostrando logs en vivo (Ctrl+C para salir)..."
    tail -f "$LOG_FILE"
    ;;

  logs50)
    echo "Últimas 50 líneas del log:"
    tail -50 "$LOG_FILE"
    ;;

  capital)
    echo ""
    if [ -f "$BOT_DIR/data/capital_state.json" ]; then
        python3 -c "
import json
with open('$BOT_DIR/data/capital_state.json') as f:
    d = json.load(f)
print(f'Capital actual : \${d.get(\"capital\", 0):.2f}')
print(f'Actualizado    : {d.get(\"actualizado\", \"N/A\")}')"
    else
        echo "  Sin datos de capital aun"
    fi
    echo ""
    ;;

  cb)
    echo ""
    echo "── Circuit Breaker ──────────────────────"
    grep -i "circuit.breaker\|cb_activo\|\[CB\]" "$LOG_FILE" 2>/dev/null | tail -10 || \
    echo "  Sin eventos de circuit breaker en el log"
    echo ""
    ;;

  trades)
    echo ""
    echo "── Últimos 10 trades ────────────────────"
    if [ -f "$BOT_DIR/data/trades/trades_log.json" ]; then
        python3 -c "
import json
with open('$BOT_DIR/data/trades/trades_log.json') as f:
    trades = json.load(f)
for t in trades[-10:]:
    emoji = '✅' if t.get('resultado') == 'GANADORA' else '🔴'
    print(f'  {emoji} {t.get(\"par\",\"?\")} | {t.get(\"estrategia\",\"?\")} | PnL=\${t.get(\"pnl\",0):+.4f} | {t.get(\"closed_at\",\"\")[:16]}')"
    else
        echo "  Sin trades registrados aun"
    fi
    echo ""
    ;;

  update)
    echo "Para actualizar parametros desde tu PC corre en PowerShell:"
    echo ""
    echo '  scp data\calibration\strategy_params.json ubuntu@IP:/home/ubuntu/trading_bot_v11/data/calibration/'
    echo '  ssh ubuntu@IP "sudo systemctl restart trading_bot"'
    echo ""
    ;;

  *)
    echo "Uso: bash bot_control.sh [status|start|stop|restart|logs|logs50|capital|cb|trades|update]"
    ;;
esac
